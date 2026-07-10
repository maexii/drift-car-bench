"""CAR-bench Track 2 agent using direct Cerebras SDK inference."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from a2a.helpers.proto_helpers import new_data_part, new_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import Role
from google.protobuf.json_format import MessageToDict

sys.path.insert(0, str(Path(__file__).parent.parent))
from logging_utils import configure_logger
from tool_call_types import ToolCall, ToolCallsData
from turn_metrics import (
    AVG_LLM_CALL_TIME_MS,
    COMPLETION_TOKENS,
    COST,
    MODEL,
    NUM_LLM_CALLS,
    NUM_PASSES,
    PROMPT_TOKENS,
    QUOTA_WAIT_TIME_MS,
    THINKING_TOKENS,
    TURN_METRICS_KEY,
)
sys.path.pop(0)

if __package__:
    from .cerebras_client import (
        DEFAULT_CEREBRAS_API_BASE,
        DEFAULT_EXECUTOR_MODEL,
        DEFAULT_EXECUTOR_REASONING_EFFORT,
        CerebrasCompletionClient,
        CerebrasTemplateError,
        MalformedModelResponseError,
        TokenUsage,
        add_token_usage,
    )
else:
    from cerebras_client import (
        DEFAULT_CEREBRAS_API_BASE,
        DEFAULT_EXECUTOR_MODEL,
        DEFAULT_EXECUTOR_REASONING_EFFORT,
        CerebrasCompletionClient,
        CerebrasTemplateError,
        MalformedModelResponseError,
        TokenUsage,
        add_token_usage,
    )


logger = configure_logger(role="agent_under_test", context="-")


@dataclass
class AgentInferenceResult:
    """Internal result for one benchmark-visible assistant step."""

    next_action: dict[str, Any]
    elapsed_ms: float
    token_usage: TokenUsage | None = None
    cost: float = 0.0
    internal_calls: int = 1
    quota_wait_ms: float = 0.0


class CARBenchAgentExecutor(AgentExecutor):
    """A2A executor that asks a Cerebras model for one CAR-bench next action."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_EXECUTOR_MODEL,
        api_base: str = DEFAULT_CEREBRAS_API_BASE,
        service_tier: str | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = DEFAULT_EXECUTOR_REASONING_EFFORT,
        max_completion_tokens: int = 1024,
        malformed_retries: int = 1,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.max_completion_tokens = max_completion_tokens
        self.malformed_retries = malformed_retries
        self.client = CerebrasCompletionClient(
            api_base=api_base,
            service_tier=service_tier,
            logger=logger.bind(role="agent_under_test", context="cerebras"),
        )
        self.ctx_id_to_messages: dict[str, list[dict[str, Any]]] = {}
        self.ctx_id_to_tools: dict[str, list[dict[str, Any]]] = {}
        self.ctx_id_to_turn_metrics: dict[str, dict[str, Any]] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        inbound_message = context.message
        ctx_logger = logger.bind(
            role="agent_under_test",
            context=f"ctx:{context.context_id[:8]}",
        )

        if context.context_id not in self.ctx_id_to_messages:
            self.ctx_id_to_messages[context.context_id] = []

        messages = self.ctx_id_to_messages[context.context_id]
        tools = self.ctx_id_to_tools.get(context.context_id, [])

        try:
            user_message_text, incoming_tool_results = self._parse_inbound_parts(
                inbound_message,
                context,
                messages,
            )
            if tools_from_msg := self._extract_tools(inbound_message):
                tools = tools_from_msg
                self.ctx_id_to_tools[context.context_id] = tools

            ctx_logger.info(
                "Received message",
                turn=len(messages) + 1,
                has_tools=bool(tools),
                num_tools=len(tools) if tools else 0,
                has_tool_results=bool(incoming_tool_results),
                message_preview=(
                    user_message_text[:100]
                    if user_message_text
                    else f"[{len(incoming_tool_results)} tool results]"
                    if incoming_tool_results
                    else ""
                ),
            )

            self._append_inbound_to_history(
                messages=messages,
                user_message_text=user_message_text,
                incoming_tool_results=incoming_tool_results,
            )

            inference_result = self._call_model_with_retries(
                context_id=context.context_id,
                messages=messages,
                tools=tools,
                ctx_logger=ctx_logger,
            )
            inference_result = self._verify_and_maybe_revise(
                context_id=context.context_id,
                messages=messages,
                tools=tools,
                inference_result=inference_result,
                ctx_logger=ctx_logger,
            )

            parts, assistant_message_for_history = self._build_a2a_response_parts(
                inference_result.next_action
            )
            messages.append(assistant_message_for_history)

            self._record_turn_metrics(
                context.context_id,
                inference_result.elapsed_ms,
                token_usage=inference_result.token_usage,
                cost=inference_result.cost,
                internal_calls=inference_result.internal_calls,
                quota_wait_ms=inference_result.quota_wait_ms,
            )
            response_message = new_message(
                parts=parts,
                context_id=context.context_id,
                role=Role.ROLE_AGENT,
            )

            has_tool_calls = bool(assistant_message_for_history.get("tool_calls"))
            if (
                not has_tool_calls
                and context.context_id in self.ctx_id_to_turn_metrics
            ):
                metrics = self._public_turn_metrics(
                    self.ctx_id_to_turn_metrics.pop(context.context_id)
                )
                response_message.metadata.update({TURN_METRICS_KEY: metrics})

            await event_queue.enqueue_event(response_message)

        except Exception as exc:
            ctx_logger.error("Cerebras agent error", error=str(exc), exc_info=True)
            response_message = new_message(
                parts=[new_text_part(f"Error processing request: {str(exc)}")],
                context_id=context.context_id,
                role=Role.ROLE_AGENT,
            )
            await event_queue.enqueue_event(response_message)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        logger.bind(
            role="agent_under_test",
            context=f"ctx:{context.context_id[:8]}",
        ).info("Canceling context")
        self.ctx_id_to_messages.pop(context.context_id, None)
        self.ctx_id_to_tools.pop(context.context_id, None)
        self.ctx_id_to_turn_metrics.pop(context.context_id, None)

    def _call_model_with_retries(
        self,
        *,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        ctx_logger,
        initial_correction: str | None = None,
    ) -> AgentInferenceResult:
        last_error: Exception | None = None
        correction = initial_correction
        total_duration_ms = 0.0
        total_token_usage: TokenUsage | None = None
        total_cost = 0.0
        total_quota_wait_ms = 0.0
        internal_calls = 0

        for attempt in range(self.malformed_retries + 1):
            prompt = build_next_action_prompt(
                messages=messages,
                tools=tools,
                correction=correction,
            )
            ctx_logger.debug(
                "Calling Cerebras executor",
                attempt=attempt + 1,
                model=self.model,
                num_messages=len(messages),
                num_tools=len(tools),
                prompt_chars=len(prompt),
                max_completion_tokens=self.max_completion_tokens,
                reasoning_effort=self.reasoning_effort,
                tool_names=[
                    tool.get("function", {}).get("name", "<unknown>")
                    for tool in tools[:10]
                ],
            )
            try:
                result = self.client.generate(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": CEREBRAS_DEVELOPER_INSTRUCTIONS,
                        },
                        {"role": "user", "content": prompt},
                    ],
                    response_schema=NEXT_ACTION_OUTPUT_SCHEMA,
                    response_schema_name="next_action",
                    max_completion_tokens=self.max_completion_tokens,
                    temperature=self.temperature,
                    reasoning_effort=self.reasoning_effort,
                )
                internal_calls += 1
                total_duration_ms += result.duration_ms
                total_cost += result.cost
                total_token_usage = add_token_usage(
                    total_token_usage,
                    result.token_usage,
                )
                total_quota_wait_ms += result.quota_wait_ms
                parsed = parse_next_action(result.text)
                # Enforce checks/action consistency, but never on the final
                # attempt: an inconsistent action is still better than an error.
                if attempt < self.malformed_retries and os.getenv(
                    "TRACK2_ENFORCE_CHECKS", "1"
                ) not in ("0", "false", "no"):
                    inconsistency = checks_inconsistency(parsed, messages)
                    if inconsistency is not None:
                        raise MalformedModelResponseError(inconsistency)
                if attempt < self.malformed_retries and os.getenv(
                    "TRACK2_PHASE_SEPARATION", "1"
                ) not in ("0", "false", "no"):
                    phase_error = phase_separation_error(parsed, messages)
                    if phase_error is not None:
                        raise MalformedModelResponseError(phase_error)
                ctx_logger.info(
                    "Cerebras response received",
                    action=parsed["action"],
                    num_tool_calls=len(parsed.get("tool_calls") or []),
                    model=result.model,
                    inference_ms=round(result.duration_ms, 1),
                    total_inference_ms=round(total_duration_ms, 1),
                    estimated_request_tokens=result.estimated_request_tokens,
                    cerebras_rate_limit_headers=(
                        result.rate_limit_headers.as_dict()
                        if result.rate_limit_headers is not None
                        else None
                    ),
                    input_tokens=(
                        total_token_usage.input_tokens
                        if total_token_usage is not None
                        else 0
                    ),
                    cached_input_tokens=(
                        total_token_usage.cached_input_tokens
                        if total_token_usage is not None
                        else 0
                    ),
                    output_tokens=(
                        total_token_usage.output_tokens
                        if total_token_usage is not None
                        else 0
                    ),
                    reasoning_tokens=(
                        total_token_usage.reasoning_output_tokens
                        if total_token_usage is not None
                        else 0
                    ),
                    attempt=attempt + 1,
                    quota_wait_ms=round(result.quota_wait_ms, 1),
                )
                return AgentInferenceResult(
                    next_action=parsed,
                    elapsed_ms=total_duration_ms,
                    token_usage=total_token_usage,
                    cost=total_cost,
                    internal_calls=max(internal_calls, 1),
                    quota_wait_ms=total_quota_wait_ms,
                )
            except (MalformedModelResponseError, json.JSONDecodeError) as exc:
                last_error = exc
                correction = (
                    "The previous model output was invalid. Return one JSON "
                    f"object matching the schema. Error: {exc}"
                )
                ctx_logger.warning(
                    f"Malformed Cerebras response: {str(exc)[:140]}",
                    attempt=attempt + 1,
                    retrying=attempt < self.malformed_retries,
                    error=str(exc),
                )
            except CerebrasTemplateError:
                raise

        raise MalformedModelResponseError(
            f"Cerebras did not produce a valid next-action JSON object: {last_error}"
        )

    def _verify_and_maybe_revise(
        self,
        *,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        inference_result: AgentInferenceResult,
        ctx_logger,
    ) -> AgentInferenceResult:
        """Stage-2 verifier: review state-changing actions with a second,
        compact Cerebras call; at most one revision round; never fails the turn."""
        if os.getenv("TRACK2_VERIFIER", "0") in ("0", "false", "no"):
            return inference_result
        action = inference_result.next_action
        state_changing = [
            tc["tool_name"]
            for tc in action.get("tool_calls") or []
            if _is_state_changing_tool(tc["tool_name"])
        ]
        if not state_changing:
            return inference_result
        # Risk gate: skip review for the boring-correct case — a single
        # state-changing call whose argument values are all literally traceable
        # to the conversation. Reviews stay for batched state changes
        # (over-eagerness suspicion) and untraceable values (invention suspicion).
        if os.getenv("TRACK2_VERIFIER_GATE", "1") not in ("0", "false", "no"):
            risk_context = _recent_tool_result_has_unknown(
                messages
            ) or _latest_user_message_deflects(messages)
            if (
                not risk_context
                and len(state_changing) <= 1
                and _argument_values_traceable(
                    action.get("tool_calls") or [], messages
                )
            ):
                ctx_logger.info(
                    "Verifier skipped by risk gate",
                    state_changing=state_changing,
                )
                return inference_result

        try:
            verifier_result = self.client.generate(
                model=self.model,
                messages=[
                    {"role": "system", "content": VERIFIER_INSTRUCTIONS},
                    {
                        "role": "user",
                        "content": build_verifier_prompt(
                            messages=messages, action=action
                        ),
                    },
                ],
                response_schema=VERIFIER_OUTPUT_SCHEMA,
                response_schema_name="action_review",
                max_completion_tokens=512,
                temperature=self.temperature,
                reasoning_effort=os.getenv(
                    "TRACK2_VERIFIER_REASONING_EFFORT", "low"
                ),
            )
            review = json.loads(verifier_result.text)
            problems = [p for p in review.get("problems") or [] if isinstance(p, str)]
            verdict = review.get("verdict")
        except Exception as exc:  # verifier must never break the turn
            ctx_logger.warning("Verifier failed, keeping action", error=str(exc))
            return inference_result

        combined_usage = add_token_usage(
            inference_result.token_usage, verifier_result.token_usage
        )
        combined = AgentInferenceResult(
            next_action=inference_result.next_action,
            elapsed_ms=inference_result.elapsed_ms + verifier_result.duration_ms,
            token_usage=combined_usage,
            cost=inference_result.cost + verifier_result.cost,
            internal_calls=inference_result.internal_calls + 1,
            quota_wait_ms=inference_result.quota_wait_ms
            + verifier_result.quota_wait_ms,
        )
        ctx_logger.info(
            "Verifier verdict",
            verdict=verdict,
            num_problems=len(problems),
            state_changing=state_changing,
        )
        if verdict != "revise" or not problems:
            return combined

        try:
            revision = self._call_model_with_retries(
                context_id=context_id,
                messages=messages,
                tools=tools,
                ctx_logger=ctx_logger,
                initial_correction=(
                    "An independent reviewer checked your proposed action and "
                    "found these problems: "
                    + "; ".join(problems[:4])
                    + ". Produce a corrected next action that fixes them."
                ),
            )
        except Exception as exc:
            ctx_logger.warning("Revision failed, keeping original", error=str(exc))
            return combined
        return AgentInferenceResult(
            next_action=revision.next_action,
            elapsed_ms=combined.elapsed_ms + revision.elapsed_ms,
            token_usage=add_token_usage(combined.token_usage, revision.token_usage),
            cost=combined.cost + revision.cost,
            internal_calls=combined.internal_calls + revision.internal_calls,
            quota_wait_ms=combined.quota_wait_ms + revision.quota_wait_ms,
        )

    def _parse_inbound_parts(
        self,
        inbound_message,
        context: RequestContext,
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]] | None]:
        user_message_text = None
        incoming_tool_results = None

        for part in inbound_message.parts:
            content_type = part.WhichOneof("content")
            if content_type == "text":
                text = part.text
                if "System:" in text and "\n\nUser:" in text:
                    parts_split = text.split("\n\nUser:", 1)
                    system_prompt = parts_split[0].replace("System:", "").strip()
                    user_message_text = parts_split[1].strip()
                    if not messages:
                        messages.append({"role": "system", "content": system_prompt})
                else:
                    user_message_text = text
            elif content_type == "data":
                data = MessageToDict(part.data)
                if "tool_results" in data:
                    incoming_tool_results = data["tool_results"]

        if not user_message_text and not incoming_tool_results:
            user_message_text = context.get_user_input()

        return user_message_text, incoming_tool_results

    @staticmethod
    def _extract_tools(inbound_message) -> list[dict[str, Any]] | None:
        for part in inbound_message.parts:
            if part.WhichOneof("content") != "data":
                continue
            data = MessageToDict(part.data)
            if "tools" in data:
                return data["tools"]
        return None

    @staticmethod
    def _append_inbound_to_history(
        *,
        messages: list[dict[str, Any]],
        user_message_text: str | None,
        incoming_tool_results: list[dict[str, Any]] | None,
    ) -> None:
        if messages and messages[-1].get("role") == "assistant" and messages[
            -1
        ].get("tool_calls"):
            prev_tool_calls = messages[-1]["tool_calls"]
            tool_results = _format_tool_results(
                prev_tool_calls=prev_tool_calls,
                incoming_tool_results=incoming_tool_results,
                fallback_text=user_message_text,
            )
            messages.extend(tool_results)
        else:
            messages.append({"role": "user", "content": user_message_text or ""})

    @staticmethod
    def _build_a2a_response_parts(
        assistant_content: dict[str, Any],
    ) -> tuple[list[Any], dict[str, Any]]:
        action = assistant_content["action"]
        if action == "respond":
            content = assistant_content.get("content", "")
            return [new_text_part(content)], {"role": "assistant", "content": content}

        tool_calls_for_history = []
        tool_calls_data = []
        for tool_call in assistant_content["tool_calls"]:
            call_id = f"call_{uuid4().hex[:12]}"
            name = tool_call["tool_name"]
            arguments = tool_call.get("arguments") or {}
            argument_json = json.dumps(arguments, separators=(",", ":"))
            tool_calls_for_history.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": argument_json,
                    },
                }
            )
            tool_calls_data.append(ToolCall(tool_name=name, arguments=arguments))

        parts = [
            new_data_part(
                ToolCallsData(tool_calls=tool_calls_data).model_dump()
            )
        ]
        return parts, {
            "role": "assistant",
            "content": None,
            "tool_calls": tool_calls_for_history,
        }

    def _record_turn_metrics(
        self,
        context_id: str,
        elapsed_ms: float,
        *,
        token_usage: TokenUsage | None = None,
        cost: float = 0.0,
        internal_calls: int = 1,
        quota_wait_ms: float = 0.0,
    ) -> None:
        metrics = self.ctx_id_to_turn_metrics.setdefault(
            context_id,
            {
                PROMPT_TOKENS: 0,
                COMPLETION_TOKENS: 0,
                COST: 0.0,
                MODEL: self.model,
                THINKING_TOKENS: 0,
                NUM_LLM_CALLS: 0,
                QUOTA_WAIT_TIME_MS: 0.0,
                "_total_llm_time_ms": 0.0,
            },
        )
        metrics[NUM_LLM_CALLS] += max(internal_calls, 1)
        if token_usage is not None:
            metrics[PROMPT_TOKENS] += token_usage.input_tokens
            metrics[COMPLETION_TOKENS] += token_usage.output_tokens
            metrics[THINKING_TOKENS] += token_usage.reasoning_output_tokens
        metrics[COST] += cost
        metrics["_total_llm_time_ms"] += elapsed_ms
        metrics[QUOTA_WAIT_TIME_MS] += quota_wait_ms
        num_calls = metrics[NUM_LLM_CALLS]
        metrics[AVG_LLM_CALL_TIME_MS] = round(
            metrics["_total_llm_time_ms"] / num_calls,
            1,
        )
        metrics[NUM_PASSES] = max(internal_calls, 1)

    @staticmethod
    def _public_turn_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
        public_metrics = dict(metrics)
        public_metrics.pop("_total_llm_time_ms", None)
        return public_metrics


def _format_tool_results(
    *,
    prev_tool_calls: list[dict[str, Any]],
    incoming_tool_results: list[dict[str, Any]] | None,
    fallback_text: str | None,
) -> list[dict[str, Any]]:
    if not incoming_tool_results:
        return [
            {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": fallback_text or "",
            }
            for tc in prev_tool_calls
        ]

    tool_call_by_name: dict[str, list[dict[str, Any]]] = {}
    for tc in prev_tool_calls:
        name = tc["function"]["name"]
        tool_call_by_name.setdefault(name, []).append(tc)

    tool_results = []
    for tr in incoming_tool_results:
        if not isinstance(tr, dict):
            tr = MessageToDict(tr)
        tr_name = tr.get("tool_name", tr.get("toolName", ""))
        matching_calls = tool_call_by_name.get(tr_name, [])
        if matching_calls:
            matched_tc = matching_calls.pop(0)
            tool_call_id = matched_tc["id"]
        else:
            tool_call_id = tr.get(
                "tool_call_id",
                tr.get("toolCallId", f"unknown_{tr_name}"),
            )
        tool_results.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": tr_name,
                "content": tr.get("content", ""),
            }
        )
    return tool_results


def build_next_action_prompt(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    correction: str | None = None,
) -> str:
    prompt = {
        "task": "Choose exactly one next assistant action for this CAR-bench turn.",
        "available_tools": tools,
        "conversation_transcript": _messages_for_prompt(messages),
        "output_contract": {
            "respond": "Use when speaking naturally to the user.",
            "tool_calls": "Use one or more supplied CAR-bench environment tools.",
        },
        "rules": [
            "Use only the tool definitions in available_tools.",
            "Do not invent tool observations.",
            "If a capability or parameter is unavailable, respond to the user transparently.",
            "Keep user-facing responses short and TTS-friendly.",
            "Respect all policies in the system/wiki message inside the transcript.",
        ],
    }
    if correction:
        prompt["correction"] = correction
    if _latest_user_message_deflects(messages):
        prompt["user_cannot_provide_notice"] = (
            "The user just signalled they cannot provide the information you "
            "asked for. Do not ask the user for it again. Either retrieve it "
            "with the available tools, or if no tool can provide it, state "
            "transparently that you cannot access it right now and offer what "
            "you can still do instead."
        )
    if _recent_tool_result_has_unknown(messages):
        prompt["unavailable_information_notice"] = (
            "A recent tool result reports a needed field as unknown/unavailable. "
            "You cannot retrieve that information. If the user's request depends "
            "on it, state transparently that you cannot retrieve it right now — "
            "do not ask the user to supply or decide it, do not re-call the tool "
            "expecting a different result, and do not assume or claim a value. "
            "Then still help with the parts of the request that do not depend on "
            "it: offer or perform a sensible fallback, and never phrase results "
            "as if you knew the unavailable value."
        )
    return json.dumps(prompt, ensure_ascii=False, indent=2)


_DEFLECTION_RE = re.compile(
    r"look (it|that|this) up|don'?t have (it|that|this)"
    r"|i don'?t know (it|that|this)",
    re.IGNORECASE,
)


def _latest_user_message_deflects(messages: list[dict[str, Any]]) -> bool:
    """True if the latest user message deflects a question the assistant asked.

    Requires the preceding assistant message to end in a question mark so that
    ordinary new user requests (for example "Can you check the weather?") never
    count as deflections.
    """
    latest_user = None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "user" and latest_user is None:
            latest_user = message
            continue
        if latest_user is not None and message.get("role") == "assistant":
            asked_question = str(message.get("content", "")).rstrip().endswith("?")
            return asked_question and bool(
                _DEFLECTION_RE.search(str(latest_user.get("content", "")))
            )
        if latest_user is None and message.get("role") == "assistant":
            return False
    return False


def _recent_tool_result_has_unknown(messages: list[dict[str, Any]]) -> bool:
    """True if a tool result in the recent turn window reports an unknown field."""
    recent_tool_messages = [m for m in messages[-6:] if m.get("role") == "tool"]
    return any(
        '"unknown"' in str(m.get("content", "")).lower()
        for m in recent_tool_messages
    )


def _messages_for_prompt(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered = []
    for message in messages:
        item: dict[str, Any] = {
            "role": message.get("role"),
            "content": message.get("content"),
        }
        if message.get("tool_calls"):
            item["tool_calls"] = [
                {
                    "tool_name": tc.get("function", {}).get("name"),
                    "arguments": _parse_arguments(
                        tc.get("function", {}).get("arguments", {})
                    ),
                }
                for tc in message["tool_calls"]
            ]
        if message.get("role") == "tool":
            item["tool_call_id"] = message.get("tool_call_id")
            item["name"] = message.get("name")
        rendered.append(item)
    return rendered


def _parse_arguments(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
    return arguments


def parse_next_action(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise MalformedModelResponseError(
                f"No JSON object found in: {text[:200]}"
            )
        payload = json.loads(text[start : end + 1])

    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}

    if payload.get("action") == "respond":
        content = payload.get("content")
        if not isinstance(content, str):
            raise MalformedModelResponseError(
                "respond action requires string content"
            )
        return {"action": "respond", "content": content, "checks": checks}

    if payload.get("action") == "tool_calls":
        tool_calls = payload.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            raise MalformedModelResponseError(
                "tool_calls action requires non-empty tool_calls"
            )
        normalized = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                raise MalformedModelResponseError(
                    "each tool call must be an object"
                )
            tool_name = tool_call.get("tool_name")
            arguments = tool_call.get("arguments")
            if arguments is None and "arguments_json" in tool_call:
                arguments = _parse_tool_arguments_json(tool_call["arguments_json"])
            if arguments is None:
                arguments = {}
            if not isinstance(tool_name, str) or not tool_name:
                raise MalformedModelResponseError(
                    "each tool call requires tool_name"
                )
            if not isinstance(arguments, dict):
                raise MalformedModelResponseError(
                    "tool call arguments must be an object"
                )
            placeholder = _find_placeholder_argument(arguments)
            if placeholder is not None:
                raise MalformedModelResponseError(
                    f"tool call {tool_name} has placeholder argument value "
                    f"{placeholder!r}. Do not emit a tool call whose argument "
                    "depends on the result of another call in the same batch: "
                    "issue only the independent call now and make the dependent "
                    "call in the next turn once the result is available."
                )
            normalized.append({"tool_name": tool_name, "arguments": arguments})
        return {"action": "tool_calls", "tool_calls": normalized, "checks": checks}


_READ_ONLY_TOOL_PREFIXES = ("get_", "search_", "calculate_")
_READ_ONLY_TOOL_NAMES = {"think", "planning_tool"}


def _is_state_changing_tool(tool_name: str) -> bool:
    return not (
        tool_name.startswith(_READ_ONLY_TOOL_PREFIXES)
        or tool_name in _READ_ONLY_TOOL_NAMES
    )


def _history_has_tool_call(messages: list[dict[str, Any]], tool_name: str) -> bool:
    for message in messages:
        if message.get("role") == "tool" and message.get("name") == tool_name:
            return True
        for tool_call in message.get("tool_calls") or []:
            if (tool_call.get("function") or {}).get("name") == tool_name:
                return True
    return False


def _conversation_has_read_only_call(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        if message.get("role") == "tool" and not _is_state_changing_tool(
            str(message.get("name") or "")
        ):
            return True
        for tool_call in message.get("tool_calls") or []:
            name = (tool_call.get("function") or {}).get("name", "")
            if name and not _is_state_changing_tool(name):
                return True
    return False


def _tool_calls_since_last_user_message(messages: list[dict[str, Any]]) -> int:
    count = 0
    for message in reversed(messages):
        if message.get("role") == "user":
            break
        if message.get("role") == "tool":
            count += 1
        count += len(message.get("tool_calls") or [])
    return count


def phase_separation_error(
    parsed: dict[str, Any], messages: list[dict[str, Any]]
) -> str | None:
    """Enforce information gathering before execution (CAR-bench paper:
    'separating information gathering from execution' against premature actions)."""
    tool_calls = parsed.get("tool_calls") or []
    state_changing = [
        tc["tool_name"] for tc in tool_calls if _is_state_changing_tool(tc["tool_name"])
    ]
    if state_changing and not _conversation_has_read_only_call(messages):
        return (
            "phase-separation: you are about to execute state-changing calls "
            f"{state_changing} without having gathered any context in this "
            "conversation. First check the relevant get_/status/preferences "
            "tools, then execute."
        )
    missing = str((parsed.get("checks") or {}).get("missing_capability", "none"))
    if parsed.get("action") == "respond" and missing.strip().lower() in ("", "none"):
        content = str(parsed.get("content") or "").rstrip()
        if content.endswith("?") and _tool_calls_since_last_user_message(messages) == 0:
            return (
                "phase-separation: you are asking the user a question without "
                "having consulted any tool since their last message. First try "
                "to resolve it via the get_/status/preferences tools; ask only "
                "if that cannot resolve it."
            )
    return None


def _argument_values_traceable(
    tool_calls: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> bool:
    """True if every state-changing argument value literally appears in the
    conversation (user words, tool results, prior assistant turns). Booleans
    and empty values count as traceable (they mirror the requested toggle)."""
    context_tokens = set(
        re.findall(
            r"[a-z0-9]+",
            " ".join(str(m.get("content") or "") for m in messages).lower(),
        )
    )

    def value_ok(value: Any) -> bool:
        if value is None or isinstance(value, bool):
            return True
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, (int, float)):
            return str(value) in context_tokens
        if isinstance(value, str):
            tokens = re.findall(r"[a-z0-9]+", value.lower())
            return all(t in context_tokens for t in tokens)
        if isinstance(value, dict):
            return all(value_ok(v) for v in value.values())
        if isinstance(value, list):
            return all(value_ok(v) for v in value)
        return False

    for tool_call in tool_calls:
        if not _is_state_changing_tool(tool_call["tool_name"]):
            continue
        if not all(value_ok(v) for v in (tool_call.get("arguments") or {}).values()):
            return False
    return True


def checks_inconsistency(
    parsed: dict[str, Any], messages: list[dict[str, Any]]
) -> str | None:
    """Deterministic consistency check between self-reported checks and action.

    Returns a correction message when the chosen action contradicts the checks
    object, or None when consistent. Only state-changing tool calls are
    constrained; information gathering stays always allowed.
    """
    checks = parsed.get("checks") or {}
    tool_calls = parsed.get("tool_calls") or []
    state_changing = [
        tc["tool_name"] for tc in tool_calls if _is_state_changing_tool(tc["tool_name"])
    ]
    if not state_changing:
        return None

    missing = str(checks.get("missing_capability", "none")).strip().lower()
    source = str(checks.get("unspecified_value_source", "not_applicable")).strip().lower()

    if missing not in ("", "none"):
        return (
            "checks-inconsistency: you reported missing_capability="
            f"'{checks.get('missing_capability')}' but chose state-changing tool "
            f"calls {state_changing}. If the capability is truly missing, respond "
            "transparently to the user instead; if it is available, set "
            "missing_capability to 'none'."
        )
    if source == "stored_preference" and not _history_has_tool_call(
        messages, "get_user_preferences"
    ):
        return (
            "checks-inconsistency: you reported unspecified_value_source="
            "'stored_preference' but never called get_user_preferences. Call "
            "get_user_preferences first to read the stored preference before "
            "executing the action."
        )
    if source == "must_ask_user":
        return (
            "checks-inconsistency: you reported unspecified_value_source="
            f"'must_ask_user' but chose state-changing tool calls {state_changing}. "
            "Ask the user for the missing value instead, or correct "
            "unspecified_value_source if the value is actually known."
        )
    return None


_PLACEHOLDER_MARKERS = ("to_be_filled", "placeholder", "fill_me", "tbd")


def _find_placeholder_argument(value: Any) -> str | None:
    """Return the first argument value that looks like an unfilled placeholder."""
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered.startswith("<") and lowered.endswith(">"):
            return value
        if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
            return value
        return None
    if isinstance(value, dict):
        for item in value.values():
            found = _find_placeholder_argument(item)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_placeholder_argument(item)
            if found is not None:
                return found
    return None

    raise MalformedModelResponseError("action must be either respond or tool_calls")


def _parse_tool_arguments_json(arguments_json: Any) -> dict[str, Any]:
    if not isinstance(arguments_json, str):
        raise MalformedModelResponseError(
            "tool call arguments_json must be a string"
        )
    if not arguments_json.strip():
        return {}
    try:
        parsed = json.loads(arguments_json)
    except json.JSONDecodeError as exc:
        raise MalformedModelResponseError(
            f"tool call arguments_json is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise MalformedModelResponseError(
            "tool call arguments_json must decode to an object"
        )
    return parsed


NEXT_ACTION_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["checks", "action", "content", "tool_calls"],
    "properties": {
        "checks": {
            "type": "object",
            "description": "Fill these checks BEFORE choosing the action.",
            "required": [
                "missing_capability",
                "unspecified_value_source",
                "scope_ok",
            ],
            "properties": {
                "missing_capability": {
                    "type": "string",
                    "description": (
                        "'none', or the name/description of a tool or capability "
                        "the request needs but that is NOT in available_tools. If "
                        "not 'none': respond transparently that this is currently "
                        "unavailable; do not ask the user to supply the data the "
                        "missing tool would provide."
                    ),
                },
                "unspecified_value_source": {
                    "type": "string",
                    "enum": [
                        "not_applicable",
                        "user_gave_it",
                        "stored_preference",
                        "vehicle_status",
                        "must_ask_user",
                    ],
                    "description": (
                        "Where a required-but-unspecified value comes from. Check "
                        "get_user_preferences (stored_preference) and get_/status "
                        "tools (vehicle_status) before concluding must_ask_user."
                    ),
                },
                "scope_ok": {
                    "type": "boolean",
                    "description": (
                        "true only if the planned action covers exactly what the "
                        "user asked: no extra unrequested actions, no broader "
                        "scope than needed (for example one specific window, not "
                        "ALL)."
                    ),
                },
            },
            "additionalProperties": False,
        },
        "action": {"type": "string", "enum": ["respond", "tool_calls"]},
        "content": {
            "type": "string",
            "description": (
                "Natural user-facing assistant text when action is respond; "
                "otherwise empty."
            ),
        },
        "tool_calls": {
            "type": "array",
            "description": (
                "CAR-bench tool calls when action is tool_calls; otherwise empty."
            ),
            "items": {
                "type": "object",
                "required": ["tool_name", "arguments_json"],
                "properties": {
                    "tool_name": {"type": "string"},
                    "arguments_json": {
                        "type": "string",
                        "description": (
                            "JSON object string containing the tool arguments, "
                            "for example \"{}\" or \"{\\\"position\\\":50}\"."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


VERIFIER_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["problems", "verdict"],
    "properties": {
        "problems": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Concrete problems with the proposed action; empty if none."
            ),
        },
        "verdict": {"type": "string", "enum": ["approve", "revise"]},
    },
    "additionalProperties": False,
}

VERIFIER_INSTRUCTIONS = """You are a strict reviewer for an in-car assistant's proposed action.
Judge only what is in the provided context. List concrete problems first, then the verdict.
Approve when the action is exactly right; revise only for real, fixable problems."""


def build_verifier_prompt(
    *, messages: list[dict[str, Any]], action: dict[str, Any]
) -> str:
    recent = _messages_for_prompt(messages[-8:])
    prompt = {
        "task": "Review this proposed action before it is executed.",
        "recent_conversation": recent,
        "proposed_action": {
            "tool_calls": [
                {"tool_name": tc["tool_name"], "arguments": tc["arguments"]}
                for tc in action.get("tool_calls") or []
            ]
        },
        "review_checklist": [
            "Scope: does the action cover exactly what the user asked - no "
            "extra unrequested actions, no broader scope than needed (for "
            "example one specific window, not ALL)?",
            "Value provenance: is every argument of a state-changing call "
            "traceable to the user's words, a stored preference read via "
            "get_user_preferences, or a current status read - never invented "
            "or defaulted?",
            "Readiness: were preconditions and required user confirmations "
            "from the conversation respected?",
        ],
    }
    return json.dumps(prompt, ensure_ascii=False, indent=2)


CEREBRAS_DEVELOPER_INSTRUCTIONS = """You are an in-car assistant reasoning layer for CAR-bench.
Use only the supplied CAR-bench tool definitions.
Return only JSON matching the requested schema.
Never invent unavailable tools, parameters, or tool results.
For tool calls, put arguments in arguments_json as a JSON object string.
For missing capability or missing information, tell the user transparently.
Keep spoken responses short, natural, and TTS-friendly.
First fill the checks object honestly for the current turn; then choose the action consistent with those checks.
Before asking the user to choose between options, first call the relevant get_/status tools and resolve the ambiguity from the current vehicle state and context; only ask the user when it genuinely cannot be inferred.
Do not guess or set a default for a user-owned value (for example fan level, temperature, target). If the user requests an action but omits such a required value, ask for it instead of choosing one yourself.
Exception: when such a value is missing, first call get_user_preferences once; if a stored preference covers it, use that value without asking. Only ask when no stored preference exists.
Never use a preference or workaround to paper over a missing tool, missing parameter, or missing tool-result field — acknowledge those transparently instead.
Respect confirmation and disambiguation policy from the wiki/system prompt."""
