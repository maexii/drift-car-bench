# Full-run evidence for the Track 2 technical report

`full_run_summary.csv` distills the 16 full public-test-split runs (125 tasks
x 3 trials each) that back every number in `report.tex`. The underlying
artifacts — ~1.0 GB of evaluator result JSONs and agent progress logs under
`output/` — are gitignored and exist only on the development machine.

## Provenance

| Column group | Source |
| --- | --- |
| `pass3_*`, `pass_at_*`, `pass_rate`, `wall_clock_min` | `final_result` of the evaluator result JSON |
| `tokens_*` | per-trial `agent_total_tokens` (prompt + completion + thinking, summed over all internal LLM calls) |
| `llm_s_*` | per-trial `total_llm_latency_ms`, divided by `num_a2a_turns` for the per-round figure |
| `steps`, `max_seq_calls_per_step`, `steps_over_5_calls`, `call_histogram` | per-step audit of the agent progress log: LLM requests counted between consecutive baseline steps |
| `gate_skips`, `verifier_*`, `phase_sep_rejects`, `completeness_revisions`, `budget_limited_skips` | agent progress-log counters |

`pass3_overall` is the unweighted mean of the three per-family Pass^3 scores,
which is how both the evaluator and Table 4 of the CAR-bench paper define the
overall score.

## Caveats when reading the log-derived columns

1. **The log schema changed during development.** `verifier_runs` counts
   `Verifier verdict` lines in runs before 2026-07-11 and `problems:` lines
   afterwards; both mark one verifier call that ran. `verifier_revisions` is
   only populated from 2026-07-11 on — a `0` in the earlier runs means the
   log line did not exist yet, not that the verifier never revised.
   The first four runs predate the gated verifier entirely.

2. **Runs before 2026-07-15 predate the shared per-step call budget**
   (commit `34ee375`). Their `max_seq_calls_per_step` is therefore observed
   behaviour, not an enforced bound: the best run has one step at 6 calls
   (1 of 1953), and the completeness-verifier run — a negative result,
   disabled in the submitted configuration — reaches 7 on 3 steps. Every run
   from 2026-07-15 on, including the one whose sources match the submitted
   image, stays at or below 5 with zero budget-limited skips.

## Regenerating

    uv run python scripts/build_run_summary.py

Requires the `output/` tree, which is gitignored and not part of the
repository. Nothing in the repository depends on the generated CSV.
