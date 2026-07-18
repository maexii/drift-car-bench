"""Simplified, readable variant of the Track 2 Cerebras CAR-bench agent.

Contains only the agent-interaction flow: ingest the inbound turn, ask the
model for one next action, reply. Infrastructure (A2A part parsing, history
bookkeeping, response-part building, turn metrics) is inherited from
``CARBenchAgentExecutor``; per-call logging happens inside
``CerebrasCompletionClient``.
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


DEVELOPER_INSTRUCTIONS = """You are an in-car assistant reasoning layer for CAR-bench.
Use only the supplied CAR-bench tool definitions.
Return only JSON matching the requested schema.
Never invent unavailable tools, parameters, or tool results.
For tool calls, put arguments in arguments_json as a JSON object string.
For missing capability or missing information, tell the user transparently.
Keep spoken responses short, natural, and TTS-friendly.
Respect confirmation and disambiguation policy from the wiki/system prompt."""


class SimpleCARBenchAgentExecutor(CARBenchAgentExecutor):
    """One benchmark turn = one model call choosing respond or tool_calls."""

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

            # 2. Ask the model for the next action (respond or tool_calls).
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
            result = self.client.generate(
                model=self.model,
                messages=[
                    {"role": "system", "content": DEVELOPER_INSTRUCTIONS},
                    {
                        "role": "user",
                        "content": build_next_action_prompt(
                            messages=messages, tools=tools, correction=correction
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


def build_next_action_prompt(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    correction: str | None = None,
) -> str:
    prompt = {
        "task": "Choose exactly one next assistant action for this CAR-bench turn.",
        "available_tools": tools,
        "conversation_transcript": _transcript(messages),
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
    return json.dumps(prompt, ensure_ascii=False, indent=2)


def _transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render the chat history as plain JSON for the prompt."""
    rendered = []
    for message in messages:
        item: dict[str, Any] = {
            "role": message.get("role"),
            "content": message.get("content"),
        }
        if message.get("role") == "tool":
            item["name"] = message.get("name")
        if message.get("tool_calls"):
            # History stores arguments as JSON strings; decode for readability.
            item["tool_calls"] = [
                {
                    "tool_name": tc["function"]["name"],
                    "arguments": json.loads(tc["function"]["arguments"]),
                }
                for tc in message["tool_calls"]
            ]
        rendered.append(item)
    return rendered


def parse_next_action(text: str) -> dict[str, Any]:
    """Validate the model output; raise MalformedModelResponseError to retry."""
    payload = json.loads(text)
    action = payload.get("action")

    if action == "respond" and isinstance(payload.get("content"), str):
        return {"action": "respond", "content": payload["content"]}

    if action == "tool_calls" and payload.get("tool_calls"):
        return {
            "action": "tool_calls",
            "tool_calls": [
                {"tool_name": tc["tool_name"], "arguments": _tool_arguments(tc)}
                for tc in payload["tool_calls"]
            ],
        }

    raise MalformedModelResponseError(f"Invalid next action: {text[:200]}")


def _tool_arguments(tool_call: dict[str, Any]) -> dict[str, Any]:
    arguments = json.loads(tool_call.get("arguments_json") or "{}")
    if not isinstance(arguments, dict):
        raise MalformedModelResponseError("tool arguments must be a JSON object")
    return arguments


# Strict structured-output contract: the model must always emit all three
# fields; unused ones stay empty.
NEXT_ACTION_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["action", "content", "tool_calls"],
    "properties": {
        "action": {"type": "string", "enum": ["respond", "tool_calls"]},
        "content": {
            "type": "string",
            "description": "User-facing text when action is respond; otherwise empty.",
        },
        "tool_calls": {
            "type": "array",
            "description": "Tool calls when action is tool_calls; otherwise empty.",
            "items": {
                "type": "object",
                "required": ["tool_name", "arguments_json"],
                "properties": {
                    "tool_name": {"type": "string"},
                    "arguments_json": {
                        "type": "string",
                        "description": (
                            "JSON object string with the tool arguments, "
                            "e.g. \"{}\" or \"{\\\"position\\\":50}\"."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}
