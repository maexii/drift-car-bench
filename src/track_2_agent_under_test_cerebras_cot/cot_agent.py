"""Chain-of-thought variant of the Track 2 Cerebras CAR-bench agent.

Same single-call flow as ``track_2_agent_under_test_cerebras_simple`` — one
model call per benchmark turn choosing respond or tool_calls — but the prompt
is adapted for explicit chain-of-thought reasoning:

- the model must first break the current turn into subproblems and reason
  step by step before committing to an action (the structured output carries
  ``subproblems`` and ``chain_of_thought`` fields ahead of ``action``);
- the prompt contains few-shot examples of good CoT covering the three
  CAR-bench task families (multi-step tool use, missing capability,
  disambiguation);
- the rules point at the benchmark-supplied ``planning_tool`` for complex
  multi-step requests.

The CoT fields are private scaffolding: they are stripped in
``parse_next_action`` and never reach the benchmark or the user.
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
Respect confirmation and disambiguation policy from the wiki/system prompt.

Reason before you act. In every output, first fill subproblems and
chain_of_thought, then choose the action:
1. Restate what the user needs right now (or what the latest tool results mean).
2. Break the request into small subproblems and list them in subproblems.
3. In chain_of_thought, solve each subproblem in order: note what the
   transcript already answers, which available tool resolves it, and what is
   still missing or ambiguous.
4. Check policy: does anything need confirmation or disambiguation first?
5. Only then commit to exactly one action that follows from the reasoning.
subproblems and chain_of_thought are private; never mention them or read
them to the user."""


class CoTCARBenchAgentExecutor(CARBenchAgentExecutor):
    """One benchmark turn = one CoT-prompted model call choosing respond or tool_calls."""

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


# Few-shot demonstrations of good chain-of-thought. Tool names mirror the real
# CAR-bench catalog but arguments are illustrative; the model must always
# follow the JSON schemas in available_tools, not these examples.
FEW_SHOT_EXAMPLES = [
    {
        "situation": (
            "User says: 'It's way too warm in here. Set it to 21 degrees and "
            "open the driver window halfway.'"
        ),
        "output": {
            "subproblems": [
                "Set the cabin temperature to 21 degrees Celsius.",
                "Open the driver-side window to 50 percent.",
                "Check whether either action needs confirmation per policy.",
            ],
            "chain_of_thought": (
                "Two independent device actions, both explicit with concrete "
                "values, so no disambiguation is needed. Subproblem 1: "
                "set_climate_temperature exists and takes the target "
                "temperature; the user gave 21, so no lookup is required. "
                "Subproblem 2: open_close_window controls window position; "
                "'halfway' maps to position 50 for the driver window. "
                "Subproblem 3: the policy requires confirmation only for "
                "safety-critical actions, which these are not. Nothing is "
                "missing, so I execute both tool calls now and report back "
                "after I see the results."
            ),
            "action": "tool_calls",
            "content": "",
            "tool_calls": [
                {
                    "tool_name": "set_climate_temperature",
                    "arguments_json": "{\"temperature\": 21}",
                },
                {
                    "tool_name": "open_close_window",
                    "arguments_json": "{\"window\": \"driver\", \"position\": 50}",
                },
            ],
        },
    },
    {
        "situation": (
            "User says: 'Turn on the seat massage for me, please.'"
        ),
        "output": {
            "subproblems": [
                "Find a tool that controls a seat massage function.",
                "If none exists, tell the user transparently instead of guessing.",
            ],
            "chain_of_thought": (
                "Subproblem 1: scanning available_tools, there are seat "
                "heating tools (get_seat_heating_level and its setter) but "
                "nothing for massage; no tool name or parameter mentions "
                "massage. Subproblem 2: since the capability does not exist, "
                "calling a similar-sounding tool like seat heating would be a "
                "hallucinated substitute and heating is not what was asked. "
                "The correct move is a short transparent response that this "
                "car cannot do seat massage, without inventing a workaround "
                "the user did not request."
            ),
            "action": "respond",
            "content": (
                "Sorry, I can't control a seat massage in this car. Anything "
                "else I can do for you?"
            ),
            "tool_calls": [],
        },
    },
    {
        "situation": (
            "User said: 'Call Alex.' The transcript already contains a "
            "get_contact_id_by_contact_name result listing two matches: "
            "'Alex Meyer' and 'Alexandra Kim'."
        ),
        "output": {
            "subproblems": [
                "Interpret the tool result: how many contacts match 'Alex'?",
                "Decide whether I can pick one or must ask the user.",
            ],
            "chain_of_thought": (
                "Subproblem 1: the lookup returned two distinct contacts, "
                "Alex Meyer and Alexandra Kim, so 'Alex' is ambiguous. "
                "Subproblem 2: nothing in the transcript indicates which one "
                "the user means, and the disambiguation policy forbids "
                "guessing between plausible matches. I must not call the "
                "phone tool yet; the next action is a short question offering "
                "both options."
            ),
            "action": "respond",
            "content": (
                "I found two contacts: Alex Meyer and Alexandra Kim. Which "
                "one should I call?"
            ),
            "tool_calls": [],
        },
    },
]


def build_next_action_prompt(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    correction: str | None = None,
) -> str:
    prompt = {
        "task": (
            "Choose exactly one next assistant action for this CAR-bench "
            "turn. Reason first: fill subproblems and chain_of_thought "
            "before deciding between respond and tool_calls."
        ),
        "available_tools": tools,
        "conversation_transcript": _transcript(messages),
        "reasoning_protocol": [
            "Restate what the user needs right now, or what the latest tool results mean for the open request.",
            "Break the request into small subproblems; list them in subproblems.",
            "Solve each subproblem in chain_of_thought: what the transcript already answers, which available tool resolves it, what is still missing or ambiguous.",
            "Check confirmation and disambiguation policy before acting.",
            "Commit to the single action that follows from the reasoning.",
        ],
        "few_shot_examples": FEW_SHOT_EXAMPLES,
        "few_shot_note": (
            "The examples show the expected reasoning style. Their tool "
            "arguments are illustrative; always follow the exact JSON "
            "schemas in available_tools."
        ),
        "output_contract": {
            "subproblems": "Private decomposition of the current turn into small subproblems.",
            "chain_of_thought": "Private step-by-step reasoning that solves the subproblems and justifies the action.",
            "respond": "Use when speaking naturally to the user.",
            "tool_calls": "Use one or more supplied CAR-bench environment tools.",
        },
        "rules": [
            "Use only the tool definitions in available_tools.",
            "Do not invent tool observations.",
            "If a capability or parameter is unavailable, respond to the user transparently.",
            "Keep user-facing responses short and TTS-friendly.",
            "Respect all policies in the system/wiki message inside the transcript.",
            "subproblems and chain_of_thought are private; never mention or paraphrase them to the user.",
            "For complex multi-step requests you may call the provided planning_tool (if present in available_tools) to record and track the plan; it does not replace subproblems and chain_of_thought.",
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
    """Validate the model output; raise MalformedModelResponseError to retry.

    The private subproblems/chain_of_thought fields are dropped here — only
    the benchmark-visible action leaves this function.
    """
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


# Strict structured-output contract. Property order puts the private CoT
# fields ahead of the action fields so the model reasons before it commits
# to an action; unused fields stay empty.
NEXT_ACTION_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["subproblems", "chain_of_thought", "action", "content", "tool_calls"],
    "properties": {
        "subproblems": {
            "type": "array",
            "description": (
                "Private decomposition of the current turn into small "
                "subproblems, in the order they should be solved."
            ),
            "items": {"type": "string"},
        },
        "chain_of_thought": {
            "type": "string",
            "description": (
                "Private step-by-step reasoning solving each subproblem and "
                "justifying the chosen action. Never shown to the user."
            ),
        },
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
