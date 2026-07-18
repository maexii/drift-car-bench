# Track 2 Agent Under Test — Cerebras (Native)

A variant of `track_2_agent_under_test_cerebras_simple` with an adapted prompt
format. Instead of flattening tools and history into one JSON blob inside a
single user message and forcing a JSON envelope via structured output, this
variant keeps the model on its trained distribution:

- The chat history is sent as real `system`/`user`/`assistant`/`tool` messages
  (the per-context history is already stored in OpenAI chat format, so it is
  passed through verbatim).
- The CAR-bench tool definitions go through the native `tools` parameter, so
  gpt-oss emits tool calls in its Harmony tool-calling format.
- No `response_format` schema and no `arguments_json` string escaping: a reply
  with native tool calls becomes a `tool_calls` action, plain assistant text
  becomes a `respond` action.

## Where things live

| Concern | Location |
| --- | --- |
| Agent flow (`execute`), model call + malformed retry | `native_agent.py` (`NativeCARBenchAgentExecutor`) |
| System instructions, native-action parser | `native_agent.py` |
| A2A part parsing, history bookkeeping, response parts, turn metrics | inherited from `track_2_agent_under_test_cerebras/car_bench_agent.py` |
| Cerebras HTTP client (incl. native `tools` passthrough), retries, quota handling | `track_2_agent_under_test_cerebras/cerebras_client.py` |

## Turn flow

1. Inbound A2A message is parsed (user text or tool results) and appended to
   the per-context chat history.
2. One native Cerebras chat completion with `tools` decides the next action:
   native tool calls → `tool_calls`, assistant text → `respond`. Empty output
   or unparseable tool arguments are retried once with a correction message.
3. The action is converted to A2A parts and sent back. Turn metrics are
   attached when the turn ends with a `respond`.

## Running

```bash
CEREBRAS_API_KEY=... python src/track_2_agent_under_test_cerebras_native/server.py --host 127.0.0.1 --port 8080
```

Same CLI flags and `TRACK2_*` environment variables as the base variant.
Scenarios: `scenarios/track_2_agent_under_test_cerebras_native/`.
