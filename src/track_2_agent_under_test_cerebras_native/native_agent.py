"""Native-format variant of the Track 2 Cerebras CAR-bench agent.

Where the ``_simple`` variant flattens tools and history into one JSON blob
inside a single user message and asks for a JSON envelope via structured
output, this variant stays on the model's trained distribution: the chat
history is sent as real system/user/assistant/tool messages and the CAR-bench
tool definitions go through the native ``tools`` parameter. The model answers
with native tool calls or plain assistant text — no output schema, no
``arguments_json`` string escaping.

Infrastructure (A2A part parsing, history bookkeeping, response-part
building, turn metrics) is inherited from ``CARBenchAgentExecutor``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from a2a.helpers.proto_helpers import new_message, new_text_part
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import Role

sys.path.insert(0, str(Path(__file__).parent.parent))
from track_2_agent_under_test_cerebras.car_bench_agent import CARBenchAgentExecutor
from track_2_agent_under_test_cerebras.cerebras_client import (
    MalformedModelResponseError,
)
from turn_metrics import TURN_METRICS_KEY
sys.path.pop(0)


DEVELOPER_INSTRUCTIONS = """You are an in-car voice assistant for CAR-bench.
Use only the provided tools; never invent tools, parameters, or tool results.
Either call tools or answer the user directly — your answer is spoken aloud, so keep it short, natural, and TTS-friendly.
If a capability or required information is unavailable, tell the user transparently.
Respect the confirmation and disambiguation policy from the wiki/system prompt."""


class NativeCARBenchAgentExecutor(CARBenchAgentExecutor):
    """One benchmark turn = one native chat completion with native tool calls."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        messages = self.ctx_id_to_messages.setdefault(context.context_id, [])
        if new_tools := self._extract_tools(context.message):
            self.ctx_id_to_tools[context.context_id] = new_tools
        tools = self.ctx_id_to_tools.get(context.context_id, [])

        try:
            # 1. Ingest the inbound turn (user text or tool results) into history.
            user_text, tool_results = self._parse_inbound_parts(
                context.message, context, messages
            )
            self._append_inbound_to_history(
                messages=messages,
                user_message_text=user_text,
                incoming_tool_results=tool_results,
            )

            # 2. Ask the model for the next action via native chat + tools.
            action = self._choose_next_action(context.context_id, messages, tools)

            # 3. Convert the action to A2A parts, extend history, reply.
            parts, assistant_message = self._build_a2a_response_parts(action)
            messages.append(assistant_message)
            await self._reply(context, event_queue, parts, assistant_message)
        except Exception as exc:
            await self._reply_error(context, event_queue, exc)

    def _choose_next_action(
        self,
        context_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Call the model once (retrying malformed output) and parse its action."""
        last_error: Exception | None = None
        correction = None

        for _ in range(self.malformed_retries + 1):
            # History is already stored in OpenAI chat format, so it is passed
            # through verbatim; the correction message only exists per attempt.
            call_messages = [
                {"role": "system", "content": DEVELOPER_INSTRUCTIONS},
                *messages,
            ]
            if correction:
                call_messages.append({"role": "user", "content": correction})
            result = self.client.generate(
                model=self.model,
                messages=call_messages,
                response_schema=None,
                response_schema_name=None,
                max_completion_tokens=self.max_completion_tokens,
                temperature=self.temperature,
                reasoning_effort=self.reasoning_effort,
                tools=tools or None,
            )
            self._record_turn_metrics(
                context_id,
                result.duration_ms,
                token_usage=result.token_usage,
                cost=result.cost,
                quota_wait_ms=result.quota_wait_ms,
            )
            try:
                return parse_native_action(result.tool_calls, result.text)
            except MalformedModelResponseError as exc:
                last_error = exc
                correction = (
                    "Your previous reply was invalid. Either call one of the "
                    "available tools or answer the user with plain text. "
                    f"Error: {exc}"
                )

        raise MalformedModelResponseError(
            f"Cerebras did not produce a valid next action: {last_error}"
        )

    async def _reply(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        parts: list[Any],
        assistant_message: dict[str, Any],
    ) -> None:
        response_message = new_message(
            parts=parts, context_id=context.context_id, role=Role.ROLE_AGENT
        )
        # Turn metrics are attached once the turn ends with a user-facing reply.
        if not assistant_message.get("tool_calls") and (
            context.context_id in self.ctx_id_to_turn_metrics
        ):
            metrics = self._public_turn_metrics(
                self.ctx_id_to_turn_metrics.pop(context.context_id)
            )
            response_message.metadata.update({TURN_METRICS_KEY: metrics})
        await event_queue.enqueue_event(response_message)

    async def _reply_error(
        self, context: RequestContext, event_queue: EventQueue, exc: Exception
    ) -> None:
        response_message = new_message(
            parts=[new_text_part(f"Error processing request: {exc}")],
            context_id=context.context_id,
            role=Role.ROLE_AGENT,
        )
        await event_queue.enqueue_event(response_message)


def parse_native_action(
    tool_calls: list[dict[str, Any]] | None, text: str
) -> dict[str, Any]:
    """Map a native completion to the executor's next-action dict."""
    if tool_calls:
        normalized = []
        for tool_call in tool_calls:
            function = tool_call.get("function") or {}
            name = function.get("name")
            if not isinstance(name, str) or not name:
                raise MalformedModelResponseError(
                    "each tool call requires a function name"
                )
            normalized.append(
                {"tool_name": name, "arguments": _tool_arguments(function)}
            )
        return {"action": "tool_calls", "tool_calls": normalized}

    if text and text.strip():
        return {"action": "respond", "content": text.strip()}

    raise MalformedModelResponseError(
        "model returned neither tool calls nor user-facing text"
    )


def _tool_arguments(function: dict[str, Any]) -> dict[str, Any]:
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as exc:
            raise MalformedModelResponseError(
                f"tool call arguments are not valid JSON: {exc}"
            ) from exc
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise MalformedModelResponseError("tool arguments must be a JSON object")
    return arguments
