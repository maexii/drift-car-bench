"""Hybrid Track 2 Cerebras CAR-bench agent.

Builds on ``SimpleCARBenchAgentExecutor`` so the outer A2A flow stays the
same: ingest one inbound turn, choose one next action, emit one benchmark-
visible reply. The hybrid logic lives inside ``_choose_next_action``:

- refresh or reuse a private plan,
- draft one candidate action via serialized k-ahead voting,
- run the existing critic chain with an editor on each rejection,
- persist private notes, pitfalls, and pending plan deltas for later turns.
"""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).parent.parent))
from track_2_agent_under_test_cerebras.cerebras_client import (
    MalformedModelResponseError,
)
from track_2_agent_under_test_cerebras_critics.critics_agent import (
    CRITICS,
    CRITIC_INSTRUCTIONS,
    Critic,
)
from track_2_agent_under_test_cerebras_planner.planner_agent import (
    PRIVATE_PLAN_OUTPUT_SCHEMA,
    parse_private_plan,
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


DEFAULT_MAX_NOTES = 12
DEFAULT_MAX_PITFALLS = 12
DEFAULT_VOTE_MARGIN = 2
DEFAULT_VOTE_MAX_SAMPLES = 5

HYBRID_DEVELOPER_INSTRUCTIONS = (
    DEVELOPER_INSTRUCTIONS
    + """
Use private_plan, internal_step_notes, accumulated_pitfall_warnings, and the
private reasoning fields only as internal guidance; never mention them to the
user.
Always fill the private reasoning fields honestly before choosing the action.
Before asking the user to choose between options, first call the relevant
get_/status tools and resolve the ambiguity from the current vehicle state and
context; only ask when it genuinely cannot be inferred.
Do not guess or set a default for a user-owned value. If such a required value
is missing, call get_user_preferences once first; if it supplies the value, use
it. Only ask the user when no stored preference exists.
Never use a preference or workaround to hide a missing tool, missing parameter,
or missing tool-result field; explain the limitation transparently instead."""
)

PRIVATE_PLANNER_INSTRUCTIONS = """You are a private CAR-bench planning layer.
Refresh or create internal guidance for the latest user turn.
Do not answer the user. Do not emit benchmark-visible planning_tool calls.
Return only JSON matching the requested private plan schema.
Base the plan only on the transcript, supplied tool definitions, prior private
plan, internal notes, accumulated pitfalls, and any pending plan delta."""


@dataclass(frozen=True)
class CandidateAction:
    """One executable next action plus its private reasoning scaffolding."""

    next_action: dict[str, Any]
    subproblems: tuple[str, ...]
    chain_of_thought: str
    rationale: str
    pitfalls_to_watch: tuple[str, ...] = ()
    checks: dict[str, bool] | None = None
    pitfall_warning: str = ""
    plan_delta: str = ""

    def all_pitfalls(self) -> list[str]:
        pitfalls = list(self.pitfalls_to_watch)
        if self.pitfall_warning:
            pitfalls.append(self.pitfall_warning)
        return pitfalls

    def without_transients(self) -> "CandidateAction":
        return CandidateAction(
            next_action=self.next_action,
            subproblems=self.subproblems,
            chain_of_thought=self.chain_of_thought,
            rationale=self.rationale,
            pitfalls_to_watch=self.pitfalls_to_watch,
            checks=self.checks,
        )


@dataclass(frozen=True)
class CriticVote:
    """One critic's structured judgment over the current candidate action."""

    verdict: str
    error_description: str
    pitfall_warning: str
    confidence: str

    @property
    def rejected(self) -> bool:
        return self.verdict == "reject"


class HybridCARBenchAgentExecutor(SimpleCARBenchAgentExecutor):
    """Simple outer flow with private planning, voting, and critics."""

    def __init__(
        self,
        *,
        max_notes: int = DEFAULT_MAX_NOTES,
        max_pitfalls: int = DEFAULT_MAX_PITFALLS,
        vote_margin: int = DEFAULT_VOTE_MARGIN,
        vote_max_samples: int = DEFAULT_VOTE_MAX_SAMPLES,
        vote_temperature: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if max_notes < 1:
            raise ValueError("max_notes must be >= 1")
        if max_pitfalls < 1:
            raise ValueError("max_pitfalls must be >= 1")
        if vote_margin < 1:
            raise ValueError("vote_margin must be >= 1")
        if vote_max_samples < vote_margin:
            raise ValueError("vote_max_samples must be >= vote_margin")
        self.max_notes = max_notes
        self.max_pitfalls = max_pitfalls
        self.vote_margin = vote_margin
        self.vote_max_samples = vote_max_samples
        self.vote_temperature = vote_temperature
        self.ctx_id_to_private_plan: dict[str, dict[str, Any]] = {}
        self.ctx_id_to_notes: dict[str, list[str]] = {}
        self.ctx_id_to_pitfalls: dict[str, list[str]] = {}
        self.ctx_id_to_pending_plan_delta: dict[str, str] = {}

    async def cancel(self, context, event_queue) -> None:
        self.ctx_id_to_private_plan.pop(context.context_id, None)
        self.ctx_id_to_notes.pop(context.context_id, None)
        self.ctx_id_to_pitfalls.pop(context.context_id, None)
        self.ctx_id_to_pending_plan_delta.pop(context.context_id, None)
        await super().cancel(context, event_queue)

    def _choose_next_action(
        self,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        notes = self.ctx_id_to_notes.setdefault(context_id, [])
        pitfalls = self.ctx_id_to_pitfalls.setdefault(context_id, [])
        private_plan = self._select_private_plan(
            context_id=context_id,
            messages=messages,
            tools=tools,
            notes=notes,
            pitfalls=pitfalls,
        )
        candidate = self._draft_candidate_action(
            context_id=context_id,
            messages=messages,
            tools=tools,
            private_plan=private_plan,
            notes=notes,
            pitfalls=pitfalls,
        )
        candidate, new_pitfalls, plan_delta = self._run_critic_chain(
            context_id=context_id,
            messages=messages,
            tools=tools,
            private_plan=private_plan,
            notes=notes,
            pitfalls=pitfalls,
            candidate=candidate,
        )
        self._persist_private_state(
            context_id=context_id,
            private_plan=private_plan,
            candidate=candidate,
            pitfalls=new_pitfalls,
            plan_delta=plan_delta,
        )
        return candidate.next_action

    def _select_private_plan(
        self,
        *,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        notes: list[str],
        pitfalls: list[str],
    ) -> dict[str, Any]:
        if _is_new_user_turn(messages):
            private_plan = self._review_or_create_private_plan(
                context_id=context_id,
                messages=messages,
                tools=tools,
                prior_private_plan=self.ctx_id_to_private_plan.get(context_id),
                notes=notes[-self.max_notes :],
                pitfalls=pitfalls[-self.max_pitfalls :],
                pending_plan_delta=self.ctx_id_to_pending_plan_delta.get(
                    context_id, ""
                ),
            )
            self.ctx_id_to_private_plan[context_id] = private_plan
            self.ctx_id_to_pending_plan_delta.pop(context_id, None)
            return private_plan

        if private_plan := self.ctx_id_to_private_plan.get(context_id):
            return private_plan

        private_plan = build_fallback_private_plan(messages)
        self.ctx_id_to_private_plan[context_id] = private_plan
        return private_plan

    def _review_or_create_private_plan(
        self,
        *,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        prior_private_plan: dict[str, Any] | None,
        notes: list[str],
        pitfalls: list[str],
        pending_plan_delta: str,
    ) -> dict[str, Any]:
        return self._structured_call(
            context_id,
            system=PRIVATE_PLANNER_INSTRUCTIONS,
            build_prompt=lambda correction: build_private_planner_prompt(
                messages=messages,
                tools=tools,
                prior_private_plan=prior_private_plan,
                notes=notes,
                pitfalls=pitfalls,
                pending_plan_delta=pending_plan_delta,
                correction=correction,
            ),
            schema=PRIVATE_PLAN_OUTPUT_SCHEMA,
            schema_name="private_plan",
            parse=parse_private_plan,
            temperature=self.temperature,
        )

    def _draft_candidate_action(
        self,
        *,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        private_plan: dict[str, Any],
        notes: list[str],
        pitfalls: list[str],
    ) -> CandidateAction:
        """Collect serialized draft samples now; parallelization can replace this."""

        counts: dict[str, int] = {}
        representatives: dict[str, CandidateAction] = {}
        last_error: MalformedModelResponseError | None = None
        effective_vote_temperature = (
            self.vote_temperature
            if self.vote_temperature is not None
            else self.temperature
        )

        for _ in range(self.vote_max_samples):
            try:
                candidate = self._structured_call(
                    context_id,
                    system=HYBRID_DEVELOPER_INSTRUCTIONS,
                    build_prompt=lambda correction: build_draft_prompt(
                        messages=messages,
                        tools=tools,
                        private_plan=private_plan,
                        notes=notes[-self.max_notes :],
                        pitfalls=pitfalls[-self.max_pitfalls :],
                        correction=correction,
                    ),
                    schema=DRAFT_ACTION_OUTPUT_SCHEMA,
                    schema_name="draft_candidate",
                    parse=parse_draft_candidate,
                    temperature=effective_vote_temperature,
                )
            except MalformedModelResponseError as exc:
                last_error = exc
                continue

            key = action_vote_key(candidate.next_action)
            representatives.setdefault(key, candidate)
            counts[key] = counts.get(key, 0) + 1
            leader, lead = _leader_and_lead(counts)
            if lead >= self.vote_margin:
                return representatives[leader]

        if not counts:
            raise MalformedModelResponseError(
                f"all {self.vote_max_samples} hybrid draft samples were malformed: "
                f"{last_error}"
            )
        leader, _ = _leader_and_lead(counts)
        return representatives[leader]

    def _run_critic_chain(
        self,
        *,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        private_plan: dict[str, Any],
        notes: list[str],
        pitfalls: list[str],
        candidate: CandidateAction,
    ) -> tuple[CandidateAction, list[str], str]:
        collected_pitfalls = candidate.all_pitfalls()
        pending_plan_delta = ""

        for critic in CRITICS:
            vote = self._run_critic_vote(
                context_id=context_id,
                messages=messages,
                tools=tools,
                candidate=candidate,
                critic=critic,
            )
            if vote.pitfall_warning:
                collected_pitfalls.append(vote.pitfall_warning)
            if not vote.rejected:
                continue

            candidate = self._edit_candidate(
                context_id=context_id,
                messages=messages,
                tools=tools,
                private_plan=private_plan,
                notes=notes,
                pitfalls=_dedupe_strings([*pitfalls, *collected_pitfalls]),
                candidate=candidate,
                critic=critic,
                vote=vote,
            )
            if candidate.pitfall_warning:
                collected_pitfalls.append(candidate.pitfall_warning)
            if candidate.plan_delta:
                pending_plan_delta = _merge_plan_delta(
                    pending_plan_delta,
                    candidate.plan_delta,
                )
            candidate = candidate.without_transients()

        return candidate, _dedupe_strings(collected_pitfalls), pending_plan_delta

    def _run_critic_vote(
        self,
        *,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        candidate: CandidateAction,
        critic: Critic,
    ) -> CriticVote:
        try:
            return self._structured_call(
                context_id,
                system=CRITIC_INSTRUCTIONS,
                build_prompt=lambda correction: build_critic_vote_prompt(
                    messages=messages,
                    tools=tools,
                    candidate=candidate,
                    critic=critic,
                    correction=correction,
                ),
                schema=CRITIC_VOTE_OUTPUT_SCHEMA,
                schema_name="critic_vote",
                parse=parse_critic_vote,
                temperature=self.temperature,
            )
        except MalformedModelResponseError:
            return CriticVote(
                verdict="accept",
                error_description="",
                pitfall_warning="",
                confidence="low",
            )

    def _edit_candidate(
        self,
        *,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        private_plan: dict[str, Any],
        notes: list[str],
        pitfalls: list[str],
        candidate: CandidateAction,
        critic: Critic,
        vote: CriticVote,
    ) -> CandidateAction:
        try:
            return self._structured_call(
                context_id,
                system=HYBRID_DEVELOPER_INSTRUCTIONS,
                build_prompt=lambda correction: build_editor_prompt(
                    messages=messages,
                    tools=tools,
                    private_plan=private_plan,
                    notes=notes[-self.max_notes :],
                    pitfalls=pitfalls[-self.max_pitfalls :],
                    candidate=candidate,
                    critic=critic,
                    vote=vote,
                    correction=correction,
                ),
                schema=EDITOR_OUTPUT_SCHEMA,
                schema_name="edited_candidate",
                parse=parse_editor_candidate,
                temperature=self.temperature,
            )
        except MalformedModelResponseError:
            return candidate

    def _structured_call(
        self,
        context_id: str,
        *,
        system: str,
        build_prompt: Callable[[str | None], str],
        schema: dict[str, Any],
        schema_name: str,
        parse: Callable[[str], Any],
        temperature: float | None,
    ) -> Any:
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
                temperature=temperature,
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

    def _persist_private_state(
        self,
        *,
        context_id: str,
        private_plan: dict[str, Any],
        candidate: CandidateAction,
        pitfalls: list[str],
        plan_delta: str,
    ) -> None:
        self.ctx_id_to_private_plan[context_id] = private_plan

        if candidate.rationale:
            notes = self.ctx_id_to_notes.setdefault(context_id, [])
            notes.append(f"step {len(notes) + 1}: {candidate.rationale}")

        stored_pitfalls = self.ctx_id_to_pitfalls.setdefault(context_id, [])
        for pitfall in _dedupe_strings(pitfalls):
            if pitfall not in stored_pitfalls:
                stored_pitfalls.append(pitfall)

        if plan_delta:
            self.ctx_id_to_pending_plan_delta[context_id] = _merge_plan_delta(
                self.ctx_id_to_pending_plan_delta.get(context_id, ""),
                plan_delta,
            )


def build_private_planner_prompt(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    prior_private_plan: dict[str, Any] | None,
    notes: list[str],
    pitfalls: list[str],
    pending_plan_delta: str,
    correction: str | None = None,
) -> str:
    prompt = {
        "task": "Review or create the private plan for the latest user turn.",
        "planning_mode": (
            "review_existing_plan"
            if prior_private_plan is not None
            else "create_new_plan"
        ),
        "available_tools": tools,
        "private_planning_contract": _planning_tool_shape(tools),
        "conversation_transcript": _transcript(messages),
        "prior_private_plan": prior_private_plan,
        "recent_internal_notes": list(notes),
        "accumulated_pitfall_warnings": list(pitfalls),
        "pending_plan_delta": pending_plan_delta,
        "rules": [
            "Return one private plan JSON object matching the requested schema.",
            "Use planning_tool-shaped reasoning as internal guidance only.",
            "Do not answer the user or emit benchmark-visible planning_tool calls.",
            "Do not invent tool observations, capabilities, or parameter values.",
            "Refresh the plan so the next drafting step can act without extra private replanning on tool-result continuations.",
            "Absorb pending_plan_delta into the refreshed plan when it is relevant.",
            "Keep the plan compact and actionable.",
        ],
    }
    if correction:
        prompt["correction"] = correction
    return json.dumps(prompt, ensure_ascii=False, indent=2)


def build_draft_prompt(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    private_plan: dict[str, Any],
    notes: list[str],
    pitfalls: list[str],
    correction: str | None = None,
) -> str:
    prompt = json.loads(
        build_next_action_prompt(messages=messages, tools=tools, correction=correction)
    )
    prompt["task"] = (
        "Choose exactly one next assistant action for this CAR-bench turn and "
        "fill the private reasoning fields first."
    )
    prompt["private_plan"] = private_plan
    prompt["internal_step_notes"] = list(notes)
    prompt["accumulated_pitfall_warnings"] = list(pitfalls)
    prompt["rules"].extend(
        [
            "First fill subproblems, chain_of_thought, rationale, pitfalls_to_watch, and checks honestly; then choose the action consistent with them.",
            "internal_step_notes are your own private notes from earlier steps; use them to stay consistent across steps and never quote them to the user.",
            "private_plan is internal guidance, not a tool result, and must never be mentioned to the user.",
            "Before asking the user to choose between options, first call the relevant get_/status tools and resolve the ambiguity from the current vehicle state and context; only ask when it genuinely cannot be inferred.",
            "Do not guess or set a default for a user-owned value. If such a required value is missing, call get_user_preferences once first; if it supplies the value, use it. Only ask the user when no stored preference exists.",
            "Never use a preference or workaround to hide a missing tool, missing parameter, or missing tool-result field; explain the limitation transparently instead.",
        ]
    )
    return json.dumps(prompt, ensure_ascii=False, indent=2)


def build_critic_vote_prompt(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    candidate: CandidateAction,
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
        "proposed_action": candidate.next_action,
        "rules": [
            "Judge only the error class defined in error_class; ignore other flaws.",
            "Reject only when the error is clearly present in the proposed action.",
            "When rejecting, describe the error concretely so an editor can fix it.",
            "Use pitfall_warning for a short future warning about this error pattern; otherwise leave it empty.",
        ],
    }
    if correction:
        prompt["correction"] = correction
    return json.dumps(prompt, ensure_ascii=False, indent=2)


def build_editor_prompt(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    private_plan: dict[str, Any],
    notes: list[str],
    pitfalls: list[str],
    candidate: CandidateAction,
    critic: Critic,
    vote: CriticVote,
    correction: str | None = None,
) -> str:
    prompt = json.loads(
        build_next_action_prompt(messages=messages, tools=tools, correction=correction)
    )
    prompt["task"] = (
        "A proposed assistant action was rejected by one reviewer. Produce one "
        "corrected candidate action."
    )
    prompt["private_plan"] = private_plan
    prompt["internal_step_notes"] = list(notes)
    prompt["accumulated_pitfall_warnings"] = list(pitfalls)
    prompt["candidate"] = candidate_payload(candidate)
    prompt["rejection"] = {
        "critic": critic.name,
        "error_description": vote.error_description,
        "pitfall_warning": vote.pitfall_warning,
        "confidence": vote.confidence,
    }
    prompt["rules"].extend(
        [
            "Fix the specific error described in rejection; keep everything that was already correct when possible.",
            "You may minimally edit the candidate or fully rewrite it if that is the cleanest fix.",
            "Return the full candidate action with fresh private reasoning fields.",
            "Never mention private_plan or internal_step_notes to the user.",
            "Before asking the user to choose between options, first call the relevant get_/status tools and resolve the ambiguity from the current vehicle state and context; only ask when it genuinely cannot be inferred.",
            "Do not guess or set a default for a user-owned value. If such a required value is missing, call get_user_preferences once first; if it supplies the value, use it. Only ask the user when no stored preference exists.",
            "Never use a preference or workaround to hide a missing tool, missing parameter, or missing tool-result field; explain the limitation transparently instead.",
        ]
    )
    return json.dumps(prompt, ensure_ascii=False, indent=2)


def candidate_payload(candidate: CandidateAction) -> dict[str, Any]:
    return {
        "subproblems": list(candidate.subproblems),
        "chain_of_thought": candidate.chain_of_thought,
        "rationale": candidate.rationale,
        "pitfalls_to_watch": list(candidate.pitfalls_to_watch),
        "pitfall_warning": candidate.pitfall_warning,
        "plan_delta": candidate.plan_delta,
        "checks": candidate.checks,
        **candidate.next_action,
    }


def parse_draft_candidate(text: str) -> CandidateAction:
    payload = json.loads(text)
    return CandidateAction(
        next_action=parse_next_action(text),
        subproblems=_parse_string_list(payload.get("subproblems"), field="subproblems"),
        chain_of_thought=_parse_required_string(
            payload.get("chain_of_thought"),
            field="chain_of_thought",
        ),
        rationale=_parse_required_string(payload.get("rationale"), field="rationale"),
        pitfalls_to_watch=tuple(
            _parse_string_list(
                payload.get("pitfalls_to_watch"), field="pitfalls_to_watch"
            )
        ),
        checks=_parse_checks(payload.get("checks")),
    )


def parse_critic_vote(text: str) -> CriticVote:
    payload = json.loads(text)
    verdict = payload.get("verdict")
    error_description = _parse_optional_string(payload.get("error_description"))
    pitfall_warning = _parse_optional_string(payload.get("pitfall_warning"))
    confidence = payload.get("confidence")

    if verdict == "accept" and confidence in {"low", "medium", "high"}:
        return CriticVote(
            verdict=verdict,
            error_description="",
            pitfall_warning=pitfall_warning,
            confidence=confidence,
        )
    if (
        verdict == "reject"
        and error_description
        and confidence in {"low", "medium", "high"}
    ):
        return CriticVote(
            verdict=verdict,
            error_description=error_description,
            pitfall_warning=pitfall_warning,
            confidence=confidence,
        )
    raise MalformedModelResponseError(f"Invalid critic vote: {text[:200]}")


def parse_editor_candidate(text: str) -> CandidateAction:
    payload = json.loads(text)
    return CandidateAction(
        next_action=parse_next_action(text),
        subproblems=_parse_string_list(payload.get("subproblems"), field="subproblems"),
        chain_of_thought=_parse_required_string(
            payload.get("chain_of_thought"),
            field="chain_of_thought",
        ),
        rationale=_parse_required_string(payload.get("rationale"), field="rationale"),
        pitfall_warning=_parse_optional_string(payload.get("pitfall_warning")),
        plan_delta=_parse_optional_string(payload.get("plan_delta")),
    )


def _parse_checks(value: Any) -> dict[str, bool]:
    required = {
        "resolved_from_status_or_context",
        "checked_user_preferences",
        "needs_user_clarification",
        "must_disclose_limitations",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise MalformedModelResponseError("checks must match the required shape")
    if not all(isinstance(value[key], bool) for key in required):
        raise MalformedModelResponseError("checks values must be booleans")
    return {key: value[key] for key in sorted(required)}


def _parse_string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MalformedModelResponseError(f"{field} must be a list of strings")
    return [item.strip() for item in value if item.strip()]


def _parse_required_string(value: Any, *, field: str) -> str:
    text = _parse_optional_string(value)
    if not text:
        raise MalformedModelResponseError(f"{field} must be a non-empty string")
    return text


def _parse_optional_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _planning_tool_shape(tools: list[dict[str, Any]]) -> dict[str, Any]:
    for tool in tools:
        if tool.get("function", {}).get("name") == "planning_tool":
            return tool
    return {
        "type": "function",
        "function": {
            "name": "planning_tool",
            "description": "Private schema reference only; do not execute this tool.",
            "parameters": copy.deepcopy(
                PRIVATE_PLAN_OUTPUT_SCHEMA["properties"]["planning_tool"]
            ),
        },
    }


def build_fallback_private_plan(messages: list[dict[str, Any]]) -> dict[str, Any]:
    latest_tool_names = [
        str(message.get("name"))
        for message in messages
        if message.get("role") == "tool" and message.get("name")
    ][-3:]
    observation_note = (
        f" Latest tool observations came from: {', '.join(latest_tool_names)}."
        if latest_tool_names
        else ""
    )
    return {
        "planning_tool": {
            "command": "create",
            "plan_id": "hybrid_continuation_without_cached_plan",
            "title": "Continue from transcript",
            "steps": [
                {
                    "step_description": (
                        "Review the benchmark-visible transcript, especially the latest tool observations."
                    ),
                    "step_dependent_on": [],
                },
                {
                    "step_description": (
                        "If the user goal still needs environment action, call only available CAR-bench tools; otherwise respond briefly to the user."
                    ),
                    "step_dependent_on": [0],
                },
            ],
        },
        "notes": (
            "No cached private plan was available for this continuation turn. "
            "Continue from transcript evidence only." + observation_note
        ),
        "risk_flags": ["missing_cached_private_plan"],
    }


def _is_new_user_turn(messages: list[dict[str, Any]]) -> bool:
    return bool(messages) and messages[-1].get("role") == "user"


def _merge_plan_delta(existing: str, new: str) -> str:
    parts = [part for part in (existing.strip(), new.strip()) if part]
    return "\n".join(parts)


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    return deduped


def action_vote_key(action: dict[str, Any]) -> str:
    if action["action"] == "respond":
        return "respond"
    return json.dumps(
        {"action": "tool_calls", "tool_calls": action["tool_calls"]},
        sort_keys=True,
        ensure_ascii=False,
    )


def _leader_and_lead(counts: dict[str, int]) -> tuple[str, int]:
    leader = max(counts, key=counts.get)
    runner_up = max(
        (count for key, count in counts.items() if key != leader),
        default=0,
    )
    return leader, counts[leader] - runner_up


CHECKS_SCHEMA = {
    "type": "object",
    "required": [
        "resolved_from_status_or_context",
        "checked_user_preferences",
        "needs_user_clarification",
        "must_disclose_limitations",
    ],
    "properties": {
        "resolved_from_status_or_context": {"type": "boolean"},
        "checked_user_preferences": {"type": "boolean"},
        "needs_user_clarification": {"type": "boolean"},
        "must_disclose_limitations": {"type": "boolean"},
    },
    "additionalProperties": False,
}


DRAFT_ACTION_OUTPUT_SCHEMA = {
    "type": "object",
    "required": [
        "subproblems",
        "chain_of_thought",
        "rationale",
        "pitfalls_to_watch",
        "checks",
        *NEXT_ACTION_OUTPUT_SCHEMA["required"],
    ],
    "properties": {
        "subproblems": {"type": "array", "items": {"type": "string"}},
        "chain_of_thought": {"type": "string"},
        "rationale": {"type": "string"},
        "pitfalls_to_watch": {"type": "array", "items": {"type": "string"}},
        "checks": copy.deepcopy(CHECKS_SCHEMA),
        **copy.deepcopy(NEXT_ACTION_OUTPUT_SCHEMA["properties"]),
    },
    "additionalProperties": False,
}


CRITIC_VOTE_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["verdict", "error_description", "pitfall_warning", "confidence"],
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "reject"]},
        "error_description": {
            "type": "string",
            "description": "Concrete rejection reason when verdict is reject; otherwise empty.",
        },
        "pitfall_warning": {
            "type": "string",
            "description": "Short future warning about this error pattern; otherwise empty.",
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "additionalProperties": False,
}


EDITOR_OUTPUT_SCHEMA = {
    "type": "object",
    "required": [
        "subproblems",
        "chain_of_thought",
        "rationale",
        "pitfall_warning",
        "plan_delta",
        *NEXT_ACTION_OUTPUT_SCHEMA["required"],
    ],
    "properties": {
        "subproblems": {"type": "array", "items": {"type": "string"}},
        "chain_of_thought": {"type": "string"},
        "rationale": {"type": "string"},
        "pitfall_warning": {"type": "string"},
        "plan_delta": {"type": "string"},
        **copy.deepcopy(NEXT_ACTION_OUTPUT_SCHEMA["properties"]),
    },
    "additionalProperties": False,
}
