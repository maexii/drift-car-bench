# Track 2 Agent Under Test — Cerebras Critic Chain

Variant of [`track_2_agent_under_test_cerebras_simple`](../track_2_agent_under_test_cerebras_simple/)
that runs every drafted action through a chain of adversarial error critics
before executing it.

## Mechanism

1. **Draft** — the action is chosen exactly like in the `_simple` base (one
   structured-output call returning `respond` or `tool_calls`).
2. **Critique** — the drafted action passes sequentially through five
   specialised critics, each one LLM call judging exactly one error class:
   - `premature_action` — acts (tool call, question, answer) while required
     context is still missing and could have been gathered first.
   - `policy_violation` — violates a policy from the system/wiki message,
     e.g. skips a required confirmation or disambiguation step.
   - `logical_error` — draws a wrong conclusion from the given information.
   - `concealment` — implicitly hides an underlying flaw (e.g. a missing
     tool/capability) instead of surfacing it transparently.
   - `hallucination` — contains fabricated content not grounded in the
     transcript or tool definitions (invented observations, state, facts).
3. **Correct** — on the first rejection, a corrector call receives the
   rejected action plus the critic's error description and produces a revised
   action in the same schema. The revision re-enters the chain from the first
   critic.
4. Repeat until the whole chain accepts or the revision budget is spent; the
   latest revision is then used as-is.

Auxiliary calls fail soft: a persistently malformed critic verdict counts as
accept, and a persistently malformed corrector output keeps the rejected (but
schema-valid) action — the turn itself never fails because of the chain.

## Cost

Each turn costs at least `1 + 5` LLM calls (draft + full accepting chain).
Every revision adds one corrector call plus a fresh chain pass, so the worst
case with the default budget of 3 is `1 + 3 × 6 + ~5` calls per turn. All
calls use the same model/settings as the base executor and are counted in the
turn metrics.

## Knobs

| Flag | Env var | Default | Meaning |
| --- | --- | --- | --- |
| `--max-revisions` | `TRACK2_MAX_REVISIONS` | `3` | Maximum correct-and-recheck rounds per turn; `0` disables the chain entirely. |

All flags of the `_simple` server (`--executor-model`, `--temperature`,
`--reasoning-effort`, `--max-completion-tokens`, `--malformed-retries`, …) are
available unchanged and also apply to the critic and corrector calls.

## Where things live

- `critics_agent.py` — executor (`CriticChainCARBenchAgentExecutor`), critic
  definitions (`CRITICS`), prompt builders, verdict schema/parser.
- `server.py` — A2A server wiring and CLI/env configuration.
- `scenarios/track_2_agent_under_test_cerebras_critics/` — local smoke and
  full test-set scenario files.

## Run

```bash
uv run python src/car_bench_a2a_runner.py \
  --scenario scenarios/track_2_agent_under_test_cerebras_critics/local_smoke.toml
```
