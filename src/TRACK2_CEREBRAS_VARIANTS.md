# Track 2 Cerebras Variants

Quick index of the `track_2_agent_under_test_cerebras*` packages. Each is a
standalone A2A agent server; scenarios live under the matching
`scenarios/<package>/` directory.

- **`track_2_agent_under_test_cerebras`** — the starter template. One Cerebras
  `gpt-oss-120b` call per turn; tools and history are flattened into one JSON
  blob in a single user message, and a strict structured-output schema returns
  a `respond`/`tool_calls` envelope with `arguments_json` strings.
- **`track_2_agent_under_test_cerebras_simple`** — readability-focused rewrite
  of the base flow with identical prompting strategy. The executor keeps only
  the agent-interaction logic and inherits A2A/metrics plumbing, so prompt and
  schema experiments stay small and local.
- **`track_2_agent_under_test_cerebras_native`** — adapted prompt format: the
  history is sent as real chat messages and tools via the native `tools`
  parameter, so gpt-oss uses its trained Harmony tool-calling format instead
  of a JSON-blob prompt plus JSON envelope (no `arguments_json` escaping).
- **`track_2_agent_under_test_cerebras_voting`** — `_simple` plus k-ahead
  self-consistency voting: the choose-action call is sampled repeatedly and an
  action executes once it leads the runner-up by `TRACK2_VOTE_MARGIN` votes
  (cap `TRACK2_VOTE_MAX_SAMPLES`, then plurality); respond samples vote by
  action type, tool calls by exact name+arguments.
- **`track_2_agent_under_test_cerebras_rationale`** — `_simple` plus a private
  rationale carried between steps: every choose-action call also emits a short
  internal note (why this action + facts to remember), stored per conversation
  and fed back to the next call as `internal_step_notes` (last
  `TRACK2_MAX_NOTES`), so each step sees the reasoning behind the previous
  ones at no extra LLM-call cost.
- **`track_2_agent_under_test_cerebras_critics`** — `_simple` plus a chain of
  adversarial error critics: the drafted action passes sequentially through
  five single-error reviewers (premature action, policy violation, logical
  error, concealment, hallucination); the first rejection routes action +
  critique into a corrector whose revision re-enters the chain, until the
  chain accepts or `TRACK2_MAX_REVISIONS` rounds are spent.
- **`track_2_agent_under_test_cerebras_planner`** — planner/executor two-pass
  template: a private high-effort `gpt-oss` planner writes compact guidance
  after each user turn, then the executor produces the benchmark-visible
  action; the plan is reused across tool-result continuation turns.
- **`track_2_agent_under_test_cerebras_maxi`** — base template plus Max's
  quality passes: an optional verifier and completeness check can revise the
  drafted action within a per-step LLM-call budget
  (`TRACK2_MAX_CALLS_PER_STEP`), with an optional response scaffold for
  unknown-tool-result/deflection turns (`TRACK2_RESPONSE_SCAFFOLD`).
