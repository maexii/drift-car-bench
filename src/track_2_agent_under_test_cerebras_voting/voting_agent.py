"""k-ahead-voting variant of the Track 2 simplified Cerebras CAR-bench agent.

Builds on ``SimpleCARBenchAgentExecutor``: prompting, parsing, and all A2A
plumbing are inherited unchanged. Only the action-selection step differs — the
single-sample choose-action call is repeated, sampled actions are tallied, and
an action is executed as soon as it leads the runner-up by ``vote_margin``
votes (bounded by ``vote_max_samples``; on hitting the cap the plurality
winner is used).

Vote keys: ``respond`` samples pool into one vote regardless of wording (the
first sampled content is spoken if respond wins); ``tool_calls`` samples
compare by exact tool names and arguments. Voting relies on sampling
diversity — with ``temperature=0`` every sample agrees and the loop simply
makes ``vote_margin`` identical calls per turn.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
from track_2_agent_under_test_cerebras.cerebras_client import (
    MalformedModelResponseError,
)
from track_2_agent_under_test_cerebras_simple.simple_agent import (
    SimpleCARBenchAgentExecutor,
)
sys.path.pop(0)


DEFAULT_VOTE_MARGIN = 2
DEFAULT_VOTE_MAX_SAMPLES = 5


class VotingCARBenchAgentExecutor(SimpleCARBenchAgentExecutor):
    """One benchmark turn = repeated model calls voting on the next action."""

    def __init__(
        self,
        *,
        vote_margin: int = DEFAULT_VOTE_MARGIN,
        vote_max_samples: int = DEFAULT_VOTE_MAX_SAMPLES,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if vote_margin < 1:
            raise ValueError("vote_margin must be >= 1")
        if vote_max_samples < vote_margin:
            raise ValueError("vote_max_samples must be >= vote_margin")
        self.vote_margin = vote_margin
        self.vote_max_samples = vote_max_samples

    def _choose_next_action(
        self,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Sample actions until one leads the runner-up by ``vote_margin``."""
        counts: dict[str, int] = {}
        representative: dict[str, dict[str, Any]] = {}
        last_error: MalformedModelResponseError | None = None

        for _ in range(self.vote_max_samples):
            try:
                action = super()._choose_next_action(context_id, messages, tools)
            except MalformedModelResponseError as exc:
                # A sample that stayed malformed through the inherited retries
                # is skipped; only fail the turn if no vote was ever collected.
                last_error = exc
                continue

            key = action_vote_key(action)
            representative.setdefault(key, action)
            counts[key] = counts.get(key, 0) + 1

            leader, lead = _leader_and_lead(counts)
            if lead >= self.vote_margin:
                return representative[leader]

        if not counts:
            raise MalformedModelResponseError(
                f"all {self.vote_max_samples} voting samples were malformed: "
                f"{last_error}"
            )
        leader, _ = _leader_and_lead(counts)
        return representative[leader]


def action_vote_key(action: dict[str, Any]) -> str:
    """Canonical vote key: respond pools by type, tool calls compare exactly."""
    if action["action"] == "respond":
        return "respond"
    return json.dumps(
        {"action": "tool_calls", "tool_calls": action["tool_calls"]},
        sort_keys=True,
        ensure_ascii=False,
    )


def _leader_and_lead(counts: dict[str, int]) -> tuple[str, int]:
    """Top vote key and its lead over the runner-up (0 votes if none).

    Ties break toward the key seen first: dicts preserve insertion order and
    ``max`` keeps the first of equal candidates.
    """
    leader = max(counts, key=counts.get)
    runner_up = max(
        (count for key, count in counts.items() if key != leader), default=0
    )
    return leader, counts[leader] - runner_up
