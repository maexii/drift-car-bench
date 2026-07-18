# Track 2 Agent: Cerebras k-ahead Voting Variant

Builds on [`track_2_agent_under_test_cerebras_simple`](../track_2_agent_under_test_cerebras_simple/)
— identical prompt, schema, and A2A plumbing — but wraps the choose-action step
in a **k-ahead self-consistency vote**: the model is sampled repeatedly, the
sampled actions are tallied, and an action is executed as soon as it leads the
runner-up by `vote_margin` (k) votes. If no action gets a k-lead within
`vote_max_samples` samples, the plurality winner is used (ties break toward
the action seen first).

## Vote keys

- **respond** samples pool into one vote regardless of wording; if respond
  wins, the content of the *first* respond sample is spoken.
- **tool_calls** samples compare by exact tool names + arguments
  (`json.dumps(..., sort_keys=True)`).

A sample that stays malformed through the inherited retry loop is skipped as a
non-vote; the turn only fails if every sample was malformed.

## Knobs

| Flag | Env var | Default |
| --- | --- | --- |
| `--vote-margin` | `TRACK2_VOTE_MARGIN` | `2` |
| `--vote-max-samples` | `TRACK2_VOTE_MAX_SAMPLES` | `5` |

All flags/env vars of the simple variant are supported unchanged.

## Cost and diversity notes

- Every turn costs at least `vote_margin` and at most `vote_max_samples` LLM
  calls (unanimous sampling stops after exactly `vote_margin` calls).
- Voting needs sampling diversity: with `temperature=0` all samples agree and
  the loop just makes `vote_margin` identical calls per turn. The default
  leaves temperature unset (Cerebras default), same as the simple variant.

## Where things live

- `voting_agent.py` — `VotingCARBenchAgentExecutor` (overrides only
  `_choose_next_action`), `action_vote_key()`.
- `server.py` — A2A server entry point / agent card.

## Run

```bash
CEREBRAS_API_KEY=... python src/track_2_agent_under_test_cerebras_voting/server.py \
  --host 127.0.0.1 --port 8080
```
