# Track 2 Agent: Cerebras Rationale-Carryover Variant

Builds on [`track_2_agent_under_test_cerebras_simple`](../track_2_agent_under_test_cerebras_simple/)
— identical A2A plumbing and base prompt — but extends the choose-action
structured output with a required **`rationale`** field: a short private note
explaining why the model chose the action and recording the specific facts
the next step will need (tool-result values, decisions made, pending
sub-steps). The rationale is stripped from the executed action, stored per
conversation, and the most recent `max_notes` notes are injected into the
next choose-action prompt as `internal_step_notes`.

The idea: each step sees the *reasoning* behind the previous steps instead of
only the bare action/observation transcript, so multi-step decisions (which
tool result mattered, what was already ruled out, what is still pending) stay
consistent across steps.

## Mechanics

- Rationale and action come from the **same** model call — no extra LLM calls
  or latency versus the simple variant (only slightly longer prompts/outputs).
- The rationale field is placed first in the output schema so the model writes
  its reasoning before committing to the action.
- Notes are numbered by step and never shown to the user; a blank/missing
  rationale is tolerated (no note stored) rather than failing the turn.
- Note storage is per `context_id`; only the prompt is truncated to the last
  `max_notes`, the stored history is not.

## Knobs

| Flag | Env var | Default |
| --- | --- | --- |
| `--max-notes` | `TRACK2_MAX_NOTES` | `12` |

All flags/env vars of the simple variant are supported unchanged.

## Where things live

- `rationale_agent.py` — `RationaleCARBenchAgentExecutor` (overrides only
  `_choose_next_action`), `build_rationale_prompt()`,
  `parse_rationale_action()`, `RATIONALE_ACTION_OUTPUT_SCHEMA`.
- `server.py` — A2A server entry point / agent card.

## Run

```bash
CEREBRAS_API_KEY=... python src/track_2_agent_under_test_cerebras_rationale/server.py \
  --host 127.0.0.1 --port 8080
```
