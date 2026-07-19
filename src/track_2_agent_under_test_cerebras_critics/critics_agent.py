"""Critic-chain variant of the Track 2 Cerebras CAR-bench agent.

Flow per benchmark turn: draft one action exactly like the ``_simple`` base,
then pass it through a fixed chain of specialised error critics (premature
action, policy violation, logical error, concealment, hallucination). Each
critic is one LLM call that either accepts the action or rejects it with a
concrete error description. On the first rejection a corrector call revises
the action using the rejected action plus the critique, and the revised
action re-enters the chain from the first critic. This repeats until the
whole chain accepts or the revision budget (``max_revisions``) is spent, in
which case the latest revision is used as-is.

Auxiliary calls fail soft: a critic whose output stays malformed counts as
accepting, and a corrector whose output stays malformed keeps the rejected
(but valid) action — neither ever fails the turn.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).parent.parent))
from track_2_agent_under_test_cerebras.cerebras_client import (
    MalformedModelResponseError,
)
from track_2_agent_under_test_cerebras_simple.simple_agent import (
    DEVELOPER_INSTRUCTIONS,
    NEXT_ACTION_OUTPUT_SCHEMA,
    SimpleCARBenchAgentExecutor,
    _transcript,
    build_next_action_prompt,
    parse_next_action,
)
sys.path.pop(0)


DEFAULT_MAX_REVISIONS = 3


@dataclass(frozen=True)
class Critic:
    """One specialised error class checked by the chain."""

    name: str
    definition: str


@dataclass(frozen=True)
class Critique:
    """A critic's rejection of a proposed action."""

    critic: str
    description: str


CRITICS: tuple[Critic, ...] = (
    Critic(
        name="premature_action",
        definition=(
            "The proposed action acts too early: it fires a state-changing "
            "tool, asks the user, or commits to an answer while required "
            "context is still missing that the assistant could have gathered "
            "first from available sources (status/read tools, the wiki/system "
            "policies, or the transcript)."
        ),
    ),
    Critic(
        name="policy_violation",
        definition=(
            "The proposed action violates a policy from the system/wiki "
            "message in the transcript, e.g. it skips a required confirmation "
            "or disambiguation step or acts outside the permitted scope."
        ),
    ),
    Critic(
        name="logical_error",
        definition=(
            "The proposed action draws a wrong conclusion from the available "
            "information: it contradicts known facts, tool results, or the "
            "user's stated intent, or its arguments do not follow from what "
            "is known in the transcript."
        ),
    ),
    Critic(
        name="concealment",
        definition=(
            "The proposed action implicitly hides an underlying flaw instead "
            "of surfacing it: e.g. it pretends success, silently works around "
            "a missing tool, parameter, or capability, or glosses over a "
            "failed step without telling the user transparently."
        ),
    ),
    Critic(
        name="hallucination",
        definition=(
            "The proposed action contains fabricated content that is untrue "
            "or not grounded in reality: invented tool observations, made-up "
            "device state, capabilities, or facts absent from the transcript "
            "and tool definitions."
        ),
    ),
)


CRITIC_INSTRUCTIONS = """You are a specialised reviewer for a CAR-bench in-car assistant.
You inspect one proposed assistant action for exactly one class of error.
Judge only that error class and ignore any other flaw.
Reject only when the error is clearly present; otherwise accept.
Return only JSON matching the requested schema."""


class CriticChainCARBenchAgentExecutor(SimpleCARBenchAgentExecutor):
    """``_simple`` drafting plus a critique-and-correct loop per turn."""

    def __init__(self, *, max_revisions: int = DEFAULT_MAX_REVISIONS, **kwargs: Any):
        if max_revisions < 0:
            raise ValueError("max_revisions must be >= 0")
        super().__init__(**kwargs)
        self.max_revisions = max_revisions

    def _choose_next_action(
        self,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        action = super()._choose_next_action(context_id, messages, tools)
        for _ in range(self.max_revisions):
            critique = self._first_critique(context_id, messages, tools, action)
            if critique is None:
                return action
            action = self._revise_action(context_id, messages, tools, action, critique)
        return action

    def _first_critique(
        self,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        action: dict[str, Any],
    ) -> Critique | None:
        """Run the critic chain in order; return the first rejection."""
        for critic in CRITICS:
            description = self._run_critic(context_id, messages, tools, action, critic)
            if description is not None:
                return Critique(critic=critic.name, description=description)
        return None

    def _run_critic(
        self,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        action: dict[str, Any],
        critic: Critic,
    ) -> str | None:
        """Return the critic's error description, or None when it accepts."""
        try:
            return self._structured_call(
                context_id,
                system=CRITIC_INSTRUCTIONS,
                build_prompt=lambda correction: build_critic_prompt(
                    messages=messages,
                    tools=tools,
                    action=action,
                    critic=critic,
                    correction=correction,
                ),
                schema=CRITIC_OUTPUT_SCHEMA,
                schema_name="critic_verdict",
                parse=parse_critic_verdict,
            )
        except MalformedModelResponseError:
            # A broken critic never fails the turn: fail open and accept.
            return None

    def _revise_action(
        self,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        action: dict[str, Any],
        critique: Critique,
    ) -> dict[str, Any]:
        """Ask the corrector for a revised action fixing the critique."""
        try:
            return self._structured_call(
                context_id,
                system=DEVELOPER_INSTRUCTIONS,
                build_prompt=lambda correction: build_corrector_prompt(
                    messages=messages,
                    tools=tools,
                    action=action,
                    critique=critique,
                    correction=correction,
                ),
                schema=NEXT_ACTION_OUTPUT_SCHEMA,
                schema_name="next_action",
                parse=parse_next_action,
            )
        except MalformedModelResponseError:
            # Persistent malformed revision: keep the rejected-but-valid action.
            return action

    def _structured_call(
        self,
        context_id: str,
        *,
        system: str,
        build_prompt: Callable[[str | None], str],
        schema: dict[str, Any],
        schema_name: str,
        parse: Callable[[str], Any],
    ) -> Any:
        """One schema-constrained model call with malformed-output retries."""
        last_error: Exception | None = None
        correction = None

        for _ in range(self.malformed_retries + 1):
            result = self.client.generate(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": build_prompt(correction)},
                ],
                response_schema=schema,
                response_schema_name=schema_name,
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
                return parse(result.text)
            except (MalformedModelResponseError, json.JSONDecodeError) as exc:
                last_error = exc
                correction = (
                    "The previous model output was invalid. Return one JSON "
                    f"object matching the schema. Error: {exc}"
                )

        raise MalformedModelResponseError(
            f"Cerebras did not produce a valid {schema_name} JSON object: {last_error}"
        )


def build_critic_prompt(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    action: dict[str, Any],
    critic: Critic,
    correction: str | None = None,
) -> str:
    prompt = {
        "task": (
            "Review the proposed assistant action for exactly one error "
            f"class: {critic.name}."
        ),
        "error_class": {"name": critic.name, "definition": critic.definition},
        "available_tools": tools,
        "conversation_transcript": _transcript(messages),
        "proposed_action": action,
        "rules": [
            "Judge only the error class defined in error_class; ignore other flaws.",
            "Reject only when the error is clearly present in the proposed action.",
            "When rejecting, describe the error concretely so a corrector can fix it.",
        ],
    }
    if correction:
        prompt["correction"] = correction
    return json.dumps(prompt, ensure_ascii=False, indent=2)


def build_corrector_prompt(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    action: dict[str, Any],
    critique: Critique,
    correction: str | None = None,
) -> str:
    # Reuse the base choose-action prompt so the corrector sees the exact same
    # tools/transcript/contract, extended with the rejected action + critique.
    prompt = json.loads(
        build_next_action_prompt(messages=messages, tools=tools, correction=correction)
    )
    prompt["task"] = (
        "A proposed assistant action for this CAR-bench turn was rejected by "
        "a reviewer. Produce one corrected next assistant action."
    )
    prompt["rejected_action"] = action
    prompt["rejection"] = {
        "critic": critique.critic,
        "error_description": critique.description,
    }
    prompt["rules"].append(
        "Fix the error described in rejection; keep everything that was already correct."
    )
    return json.dumps(prompt, ensure_ascii=False, indent=2)


def parse_critic_verdict(text: str) -> str | None:
    """Return the error description on reject, None on accept."""
    payload = json.loads(text)
    verdict = payload.get("verdict")

    if verdict == "accept":
        return None

    description = payload.get("error_description")
    if verdict == "reject" and isinstance(description, str) and description.strip():
        return description.strip()

    raise MalformedModelResponseError(f"Invalid critic verdict: {text[:200]}")


# Strict structured-output contract for critics: always emit both fields;
# error_description stays empty on accept.
CRITIC_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["verdict", "error_description"],
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "reject"]},
        "error_description": {
            "type": "string",
            "description": (
                "Concrete description of the found error when verdict is "
                "reject; otherwise empty."
            ),
        },
    },
    "additionalProperties": False,
}
