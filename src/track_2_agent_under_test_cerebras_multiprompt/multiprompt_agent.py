"""Multi-prompt majority-voting variant of the Track 2 simplified Cerebras agent.

Builds on ``SimpleCARBenchAgentExecutor``: prompting, parsing, and all A2A
plumbing are inherited unchanged. Only the action-selection step differs: one
sample is drawn in parallel for each configured system prompt, sampled actions
are tallied, and the plurality winner is executed.

Unlike the temperature-based ``VotingCARBenchAgentExecutor``, diversity comes
from the system prompts themselves, so this variant is meaningful even at
``temperature=0``. Prompts are loaded from a directory of ``.md``/``.txt``
files (one file per voter, sorted by filename).

Vote keys are shared with the voting variant: ``respond`` samples pool into
one vote regardless of wording; ``tool_calls`` samples compare by exact tool
names and arguments. Ties break toward the earliest-listed prompt file, so
order files by how much you trust them.
"""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
from track_2_agent_under_test_cerebras.cerebras_client import (  # noqa: E402
    MalformedModelResponseError,
)
from track_2_agent_under_test_cerebras_simple.simple_agent import (  # noqa: E402
    NEXT_ACTION_OUTPUT_SCHEMA,
    SimpleCARBenchAgentExecutor,
    build_next_action_prompt,
    parse_next_action,
)
from track_2_agent_under_test_cerebras_voting.voting_agent import (  # noqa: E402
    action_vote_key,
)

sys.path.pop(0)


DEFAULT_PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPT_FILE_SUFFIXES = (".md", ".txt")


def load_system_prompts(
    prompts_dir: Path,
    max_prompts: int | None = None,
) -> list[str]:
    """Read one system prompt per ``.md``/``.txt`` file, sorted by filename."""
    if max_prompts is not None and max_prompts < 1:
        raise ValueError("max_prompts must be at least 1 when provided")

    files = sorted(
        path
        for path in prompts_dir.iterdir()
        if path.suffix in PROMPT_FILE_SUFFIXES and not path.name.startswith("README")
    )
    if max_prompts is not None:
        files = files[:max_prompts]

    prompts = [text for path in files if (text := path.read_text().strip())]
    if not prompts:
        raise ValueError(
            f"no non-empty {'/'.join(PROMPT_FILE_SUFFIXES)} prompt files in "
            f"{prompts_dir}"
        )
    return prompts


class MultiPromptVotingCARBenchAgentExecutor(SimpleCARBenchAgentExecutor):
    """One benchmark turn = one parallel model call per system prompt, then a vote."""

    def __init__(self, *, system_prompts: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not system_prompts:
            raise ValueError("system_prompts must not be empty")
        self.system_prompts = system_prompts
        self._metrics_lock = threading.Lock()

    def _record_turn_metrics(self, *args: Any, **kwargs: Any) -> None:
        # Samples run in worker threads; the inherited accumulator is
        # read-modify-write and needs the lock.
        with self._metrics_lock:
            super()._record_turn_metrics(*args, **kwargs)

    def _choose_next_action(
        self,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Sample one action per system prompt in parallel and majority-vote."""
        with ThreadPoolExecutor(max_workers=len(self.system_prompts)) as pool:
            futures = [
                pool.submit(self._sample_action, context_id, messages, tools, prompt)
                for prompt in self.system_prompts
            ]

        counts: dict[str, int] = {}
        representative: dict[str, dict[str, Any]] = {}
        last_error: MalformedModelResponseError | None = None

        # Tally in prompt order so ties break toward the earliest prompt file.
        for future in futures:
            try:
                action = future.result()
            except MalformedModelResponseError as exc:
                # A voter that stayed malformed through the retries abstains;
                # only fail the turn if every voter abstained.
                last_error = exc
                continue
            key = action_vote_key(action)
            representative.setdefault(key, action)
            counts[key] = counts.get(key, 0) + 1

        if not counts:
            raise MalformedModelResponseError(
                f"all {len(self.system_prompts)} prompt voters were malformed: "
                f"{last_error}"
            )
        winner = max(counts, key=counts.get)
        return representative[winner]

    def _sample_action(
        self,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_prompt: str,
    ) -> dict[str, Any]:
        """One voter: the inherited single-sample loop under a custom system prompt."""
        last_error: Exception | None = None
        correction = None

        for _ in range(self.malformed_retries + 1):
            result = self.client.generate(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": build_next_action_prompt(
                            messages=messages,
                            tools=tools,
                            correction=correction,
                        ),
                    },
                ],
                response_schema=NEXT_ACTION_OUTPUT_SCHEMA,
                response_schema_name="next_action",
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
                return parse_next_action(result.text)
            except (MalformedModelResponseError, json.JSONDecodeError) as exc:
                last_error = exc
                correction = (
                    "The previous model output was invalid. Return one JSON "
                    f"object matching the schema. Error: {exc}"
                )

        raise MalformedModelResponseError(
            f"Cerebras did not produce a valid next-action JSON object: {last_error}"
        )
