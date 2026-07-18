# Track 2 Agent Under Test — Cerebras (Simple)

A readability-focused variant of `track_2_agent_under_test_cerebras`. The
executor `SimpleCARBenchAgentExecutor` subclasses `CARBenchAgentExecutor`
and keeps only the agent-interaction flow at this level; logging, metrics
plumbing, and A2A protocol handling stay in the base package.

## Where things live

| Concern | Location |
| --- | --- |
| Agent flow (`execute`), model call + malformed retry | `simple_agent.py` (`SimpleCARBenchAgentExecutor`) |
| Prompt builder, output schema, system instructions, action parser | `simple_agent.py` (own copies — edit freely, base variants unaffected) |
| A2A part parsing, history bookkeeping, response parts, turn metrics | inherited from `track_2_agent_under_test_cerebras/car_bench_agent.py` |
| Cerebras HTTP client, retries, quota handling, per-call logging | `track_2_agent_under_test_cerebras/cerebras_client.py` |

## Turn flow

1. Inbound A2A message is parsed (user text or tool results) and appended
   to the per-context chat history.
2. One Cerebras call with structured output decides the next action:
   `respond` (user-facing text) or `tool_calls`. Malformed JSON is retried
   once with a correction hint.
3. The action is converted to A2A parts and sent back. Turn metrics are
   attached when the turn ends with a `respond`.

## Improving the agent

Start in `simple_agent.py`:

- `DEVELOPER_INSTRUCTIONS` — system prompt.
- `build_next_action_prompt` — task framing, rules, transcript rendering.
- `NEXT_ACTION_OUTPUT_SCHEMA` — structured-output contract (e.g. add a
  reasoning/plan field).
- `SimpleCARBenchAgentExecutor._choose_next_action` — add extra passes
  (planner/critic) here; call `self._record_turn_metrics(...)` once per
  LLM call so cost accounting stays correct.

## Running

```bash
CEREBRAS_API_KEY=... python src/track_2_agent_under_test_cerebras_simple/server.py --host 127.0.0.1 --port 8080
```

Same CLI flags and `TRACK2_*` environment variables as the base variant.
Scenarios: `scenarios/track_2_agent_under_test_cerebras_simple/`.
