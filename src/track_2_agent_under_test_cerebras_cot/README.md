# Track 2 Agent Under Test — Cerebras (Chain of Thought)

A prompt-only experiment on top of `track_2_agent_under_test_cerebras_simple`:
same single Cerebras call per benchmark turn, but the prompt instructs the
model to reason with an explicit chain of thought before choosing the action.

## What changed versus `_simple`

Only the prompting strategy — the executor flow, retry handling, and metrics
are identical:

- **Explicit CoT in the output schema.** `NEXT_ACTION_OUTPUT_SCHEMA` gains two
  required fields ordered *before* the action fields — `subproblems` (the
  decomposition of the current turn) and `chain_of_thought` (step-by-step
  reasoning) — so the model writes its reasoning before it commits to
  `respond`/`tool_calls`. Both fields are private: `parse_next_action` strips
  them, and the rules forbid mentioning them to the user.
- **Subproblem decomposition protocol.** The system prompt and a
  `reasoning_protocol` block in the user prompt require: restate the goal,
  break it into subproblems, solve each against transcript + available tools,
  check confirmation/disambiguation policy, then act.
- **Few-shot CoT examples.** Three worked examples in the prompt cover the
  CAR-bench task families: a multi-step tool-call turn (climate + window), a
  missing-capability turn (seat massage → transparent respond), and a
  disambiguation turn (two "Alex" contacts → clarifying question). A note
  marks their arguments as illustrative so the model still follows the real
  tool schemas.
- **`planning_tool` pointer.** CAR-bench supplies a benchmark-visible
  `planning_tool` (create/update/mark_steps). A rule tells the model it may
  use it for complex multi-step requests — as a normal tool call, not as a
  replacement for the CoT fields. (The `_planner` variant instead runs a
  private planner pass; here planning stays inside the single call.)
- **Token budget.** Default `TRACK2_MAX_COMPLETION_TOKENS` is 2048 (vs. 1024)
  because the visible CoT consumes completion tokens on top of the action
  JSON.

## Where things live

| Concern | Location |
| --- | --- |
| Agent flow (`execute`), model call + malformed retry | `cot_agent.py` (`CoTCARBenchAgentExecutor`) |
| CoT prompt builder, few-shot examples, output schema, action parser | `cot_agent.py` (own copies — edit freely, base variants unaffected) |
| A2A part parsing, history bookkeeping, response parts, turn metrics | inherited from `track_2_agent_under_test_cerebras/car_bench_agent.py` |
| Cerebras HTTP client, retries, quota handling, per-call logging | `track_2_agent_under_test_cerebras/cerebras_client.py` |

## Running

```bash
CEREBRAS_API_KEY=... python src/track_2_agent_under_test_cerebras_cot/server.py --host 127.0.0.1 --port 8080
```

Same CLI flags and `TRACK2_*` environment variables as the base variant.
Scenarios: `scenarios/track_2_agent_under_test_cerebras_cot/`.
