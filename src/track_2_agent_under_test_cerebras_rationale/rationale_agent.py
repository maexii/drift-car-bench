"""Rationale-carryover variant of the Track 2 simplified Cerebras CAR-bench agent.

Builds on ``SimpleCARBenchAgentExecutor``: A2A plumbing, history handling, and
the base prompt are inherited. The choose-action call is extended so the model
must also emit a ``rationale`` — a short private note explaining why it chose
the action and recording the specific facts the next step will need (values
from tool results, decisions made, pending sub-steps). The rationale is
stripped from the executed action, stored per conversation, and the most
recent ``max_notes`` notes are shown to the next choose-action call as
``internal_step_notes``, so each step sees the reasoning behind the previous
ones instead of only the bare transcript.

Costs no extra LLM calls: rationale and action come from the same structured
output.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
from track_2_agent_under_test_cerebras.cerebras_client import (
    MalformedModelResponseError,
)
from track_2_agent_under_test_cerebras_simple.simple_agent import (
    DEVELOPER_INSTRUCTIONS,
    NEXT_ACTION_OUTPUT_SCHEMA,
    SimpleCARBenchAgentExecutor,
    build_next_action_prompt,
    parse_next_action,
)
sys.path.pop(0)


DEFAULT_MAX_NOTES = 12

RATIONALE_DEVELOPER_INSTRUCTIONS = DEVELOPER_INSTRUCTIONS + """
Always fill the rationale field first: a short private note (never shown to
the user) stating why you chose this action and the specific facts the next
step will need (values from tool results, user preferences, decisions made,
pending sub-steps). Your notes from previous steps are provided back to you
as internal_step_notes."""


class RationaleCARBenchAgentExecutor(SimpleCARBenchAgentExecutor):
    """Simple flow plus a private rationale note carried between steps."""

    def __init__(self, *, max_notes: int = DEFAULT_MAX_NOTES, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if max_notes < 1:
            raise ValueError("max_notes must be >= 1")
        self.max_notes = max_notes
        self.ctx_id_to_notes: dict[str, list[str]] = {}

    def _choose_next_action(
        self,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """One model call returning the action plus a stored rationale note."""
        notes = self.ctx_id_to_notes.setdefault(context_id, [])
        last_error: Exception | None = None
        correction = None

        for _ in range(self.malformed_retries + 1):
            result = self.client.generate(
                model=self.model,
                messages=[
                    {"role": "system", "content": RATIONALE_DEVELOPER_INSTRUCTIONS},
                    {
                        "role": "user",
                        "content": build_rationale_prompt(
                            messages=messages,
                            tools=tools,
                            notes=notes[-self.max_notes :],
                            correction=correction,
                        ),
                    },
                ],
                response_schema=RATIONALE_ACTION_OUTPUT_SCHEMA,
                response_schema_name="next_action_with_rationale",
                max_completion_tokens=self.max_completion_tokens,
                temperature=self.temperature,
                reasoning_effort=self.reasoning_effort,
            )
            self._record_turn_metrics(
                context_id,
                result.duration_ms,
                token_usage=result.token_usage,
                cost=result.cost,
                quota_wait_ms=result.quota_wait_ms,
            )
            try:
                action, rationale = parse_rationale_action(result.text)
            except (MalformedModelResponseError, json.JSONDecodeError) as exc:
                last_error = exc
                correction = (
                    "The previous model output was invalid. Return one JSON "
                    f"object matching the schema. Error: {exc}"
                )
                continue
            if rationale:
                # Number by position in the full note history; only the prompt
                # is truncated to the last max_notes, storage never is.
                notes.append(f"step {len(notes) + 1}: {rationale}")
            return action

        raise MalformedModelResponseError(
            f"Cerebras did not produce a valid next-action JSON object: {last_error}"
        )


def build_rationale_prompt(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    notes: list[str],
    correction: str | None = None,
) -> str:
    """The simple variant's prompt plus the private notes from previous steps."""
    prompt = json.loads(
        build_next_action_prompt(messages=messages, tools=tools, correction=correction)
    )
    prompt["internal_step_notes"] = list(notes)
    prompt["rules"].append(
        "internal_step_notes are your own private rationale notes from the "
        "previous steps of this conversation; use them to stay consistent "
        "across steps and never quote them to the user."
    )
    return json.dumps(prompt, ensure_ascii=False, indent=2)


def parse_rationale_action(text: str) -> tuple[dict[str, Any], str]:
    """Split the model output into the executable action and its private note."""
    action = parse_next_action(text)
    # Missing/blank rationale is tolerated (no note stored) rather than
    # failing an otherwise valid action.
    rationale = json.loads(text).get("rationale")
    return action, rationale.strip() if isinstance(rationale, str) else ""


# The simple variant's contract plus a required leading rationale field, so
# the model writes its reasoning before committing to the action.
RATIONALE_ACTION_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["rationale", *NEXT_ACTION_OUTPUT_SCHEMA["required"]],
    "properties": {
        "rationale": {
            "type": "string",
            "description": (
                "Private note (never shown to the user): why this action, plus "
                "the specific facts the next step will need (tool-result "
                "values, decisions made, pending sub-steps)."
            ),
        },
        **copy.deepcopy(NEXT_ACTION_OUTPUT_SCHEMA["properties"]),
    },
    "additionalProperties": False,
}
