#!/usr/bin/env python3
"""Distill the full public-test-split runs into the report's evidence CSV.

Reads the gitignored `output/` tree (evaluator result JSONs + agent progress
logs) and writes `report/full_run_summary.csv`, the small artifact backing
every number in `report/report.tex`:

    uv run python scripts/build_run_summary.py

Each row is one full run (125 tasks x 3 trials). Scores and token/latency
statistics come from the result JSON; the per-step sequential-call audit and
the reliability-stack counters come from the matching progress log, which is
located by run start time (logs are named in local time, results in UTC).

See `report/full_run_summary.md` for column provenance and for two caveats
about the log-derived columns.
"""

from __future__ import annotations

import argparse
import collections
import csv
import gc
import json
import re
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "output/track_2_agent_under_test_cerebras"
LOGS = ROOT / "output/progress_logs"
DEFAULT_OUT = ROOT / "report/full_run_summary.csv"

ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Progress logs are stamped at run start in local time, results in UTC.
LOG_TZ_OFFSET = timedelta(hours=2)
LOG_MATCH_TOLERANCE_S = 180

# Runs cited in the report, keyed by result-file timestamp. Each mapping was
# verified against the run's scores and, where applicable, its log evidence.
ROLE = {
    "20260708-205040": "Table 3 row 1: prompt-only baseline",
    "20260710-201138": "Table 3 row 2: + guards, gated verifier",
    "20260712-141211": "Table 3 row 3: + catalog notice",
    "20260712-163201": "Table 3 row 4: + placeholder, toll (best run)",
    "20260712-200002": "Table 3 row 5: confirmation run",
    "20260715-143929": "Sec 6: post-guard run, sources == submitted image",
    "20260710-220633": "Sec 5 (i): workflow memory (negative)",
    "20260712-175232": "Sec 5 (iii): completeness verifier (negative)",
    "20260709-141014": "Sec 5: evaluator-truncation run (18.7%)",
}


def parse_log(path: Path) -> dict:
    """Per-step sequential-call audit plus reliability-stack counters.

    One baseline step spans from a "Received message" line to the next, so the
    LLM requests logged in between are that step's sequential calls.
    """
    hist: collections.Counter = collections.Counter()
    counters: collections.Counter = collections.Counter()
    calls = None

    with path.open(errors="ignore") as fh:
        for line in fh:
            line = ANSI.sub("", line)
            if "| agent_under_test |" not in line:
                continue
            msg = line.rsplit("|", 1)[-1].strip()

            if msg.startswith("Received message"):
                if calls is not None:
                    hist[calls] += 1
                calls = 0
            elif msg.startswith("Sending Cerebras request"):
                if calls is not None:
                    calls += 1
            elif msg.startswith("Verifier skipped by risk gate"):
                counters["gate_skips"] += 1
            elif msg.startswith("Verifier skipped: step call budget exhausted"):
                counters["budget_skips"] += 1
            elif msg.startswith("Verifier revision applied"):
                counters["verifier_revisions"] += 1
            elif msg.startswith("Verifier failed"):
                counters["verifier_failures"] += 1
            elif msg.startswith("Completeness revision applied"):
                counters["completeness_revisions"] += 1
            elif msg.startswith(("problems:", "Verifier verdict")):
                # Log schema changed mid-development: early runs emit
                # "Verifier verdict", later ones "problems:". Both mark one
                # verifier call that actually ran.
                counters["verifier_runs"] += 1

            if "phase-separation" in msg:
                counters["phase_sep_rejects"] += 1

    if calls is not None:
        hist[calls] += 1

    return {
        "steps": sum(hist.values()),
        "max_seq_calls_per_step": max(hist) if hist else 0,
        "steps_over_5_calls": sum(n for k, n in hist.items() if k > 5),
        "call_histogram": ";".join(f"{k}:{hist[k]}" for k in sorted(hist)),
        **counters,
    }


def log_for(started_utc: datetime) -> Path | None:
    """Find the progress log whose start stamp matches this run."""
    want = started_utc + LOG_TZ_OFFSET
    best, best_gap = None, None
    for path in LOGS.glob("*__local_test_set.log"):
        stamp = path.name.split("__")[0]
        gap = abs((datetime.strptime(stamp, "%Y%m%d-%H%M%S") - want).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = path, gap
    if best_gap is not None and best_gap <= LOG_MATCH_TOLERANCE_S:
        return best
    return None


def summarize(path: Path) -> dict:
    with path.open() as fh:
        data = json.load(fh)

    meta, final = data["metadata"], data["final_result"]
    by_split = final["pass_power_k_scores_by_split"]

    def pass3(split: str) -> float:
        return round(by_split.get(split, {}).get("Pass^3", 0) * 100, 1)

    tokens: list[float] = []
    lat_trial: list[float] = []
    lat_turn: list[float] = []
    for items in final["detailed_results_by_split"].values():
        for item in items:
            if total := item.get("agent_total_tokens") or 0:
                tokens.append(total)
            ms = item.get("total_llm_latency_ms") or 0
            turns = item.get("num_a2a_turns") or 0
            if ms:
                lat_trial.append(ms / 1000)
                if turns:
                    lat_turn.append(ms / turns / 1000)

    started = datetime.fromisoformat(meta["started_at"]).replace(tzinfo=None)
    log_path = log_for(started)
    audit = parse_log(log_path) if log_path else {}
    counter = (lambda key: audit.get(key, 0)) if log_path else (lambda key: "")

    def stat(values, fn, scale=1, digits=2):
        return round(fn(values) / scale, digits) if values else ""

    tok = lambda fn: stat(tokens, fn, scale=1000, digits=1)

    del data
    gc.collect()

    return {
        "run_id": path.name[:15],
        "started_utc": meta["started_at"][:19],
        "wall_clock_min": round(meta.get("wall_time_seconds", 0) / 60, 1),
        "n_tasks": int(final["max_score"] / 3),
        "n_trials": int(final["max_score"]),
        "pass3_base": pass3("base"),
        "pass3_hallucination": pass3("hallucination"),
        "pass3_disambiguation": pass3("disambiguation"),
        "pass3_overall": round(final["pass_power_k_scores"]["Pass^3"] * 100, 1),
        "pass_at_1": round(final["pass_at_k_scores"]["Pass@1"] * 100, 1),
        "pass_at_3": round(final["pass_at_k_scores"]["Pass@3"] * 100, 1),
        "pass_rate": round(final["pass_rate"], 1),
        "trials_with_tokens": len(tokens),
        "tokens_mean_k": tok(statistics.mean),
        "tokens_median_k": tok(statistics.median),
        "tokens_max_k": tok(max),
        "llm_s_per_trial_mean": stat(lat_trial, statistics.mean),
        "llm_s_per_trial_median": stat(lat_trial, statistics.median),
        "llm_s_per_a2a_turn_mean": stat(lat_turn, statistics.mean),
        "steps": audit.get("steps", ""),
        "max_seq_calls_per_step": audit.get("max_seq_calls_per_step", ""),
        "steps_over_5_calls": audit.get("steps_over_5_calls", ""),
        "call_histogram": audit.get("call_histogram", ""),
        "gate_skips": counter("gate_skips"),
        "verifier_runs": counter("verifier_runs"),
        "verifier_revisions": counter("verifier_revisions"),
        "verifier_failures": counter("verifier_failures"),
        "completeness_revisions": counter("completeness_revisions"),
        "phase_sep_rejects": counter("phase_sep_rejects"),
        "budget_limited_skips": counter("budget_skips"),
        "user_simulator_model": meta.get("config", {}).get("user_model", ""),
        "progress_log": log_path.name if log_path else "",
        "report_role": ROLE.get(path.name[:15], ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    paths = sorted(RESULTS.glob("*local_test_set*.json"))
    if not paths:
        print(f"no full-run results under {RESULTS}", file=sys.stderr)
        return 1

    rows = []
    for i, path in enumerate(paths, 1):
        print(f"[{i}/{len(paths)}] {path.name[:15]}", file=sys.stderr, flush=True)
        rows.append(summarize(path))

    unmatched = [r["run_id"] for r in rows if not r["progress_log"]]
    if unmatched:
        print(f"warning: no progress log matched for {', '.join(unmatched)}", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {args.out.relative_to(ROOT)} ({len(rows)} runs)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
