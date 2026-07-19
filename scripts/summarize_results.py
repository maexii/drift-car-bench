#!/usr/bin/env python3
"""Print a compact table of saved CAR-bench run results.

Reads the result payloads written by the scenario client (``output/<agent>/*.json``)
and prints, per run: number of samples (task trials evaluated), overall pass rate,
Pass^3, and Pass@3. Runs are grouped by number of samples (so only comparable runs
are ranked together) and ordered by Pass^3 descending within each group.

Usage: python scripts/summarize_results.py [--input-dir output]
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = REPO_ROOT / "output"


@dataclass(frozen=True)
class RunSummary:
    path: Path
    label: str
    scenario: str
    num_samples: int
    pass_rate: float | None
    pass_power_3: float | None
    pass_at_3: float | None
    tokens_per_task: float | None


def as_percent(value: object) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    score = float(value)
    return score * 100.0 if score <= 1.0 else score


def run_label(payload: dict, path: Path) -> str:
    metadata = payload.get("metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    agent = metadata.get("agent_name") or path.parent.name
    pieces = [str(agent)]
    if metadata.get("model"):
        pieces.append(str(metadata["model"]))
    if metadata.get("reasoning_effort"):
        pieces.append(str(metadata["reasoning_effort"]))
    return " / ".join(pieces)


def scenario_name(payload: dict) -> str:
    metadata = payload.get("metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    if metadata.get("scenario_name"):
        return str(metadata["scenario_name"]).split("/")[-1]
    if metadata.get("scenario_path"):
        return Path(str(metadata["scenario_path"])).name
    return "—"


def row_total_tokens(row: dict) -> float | None:
    total = row.get("agent_total_tokens")
    if isinstance(total, (int, float)):
        return float(total)

    # Older payloads: sum the per-turn metrics from the trajectory instead.
    trajectory = row.get("trajectory")
    if not isinstance(trajectory, list):
        return None
    tokens = 0.0
    saw_metrics = False
    for message in trajectory:
        metrics = message.get("turn_metrics") if isinstance(message, dict) else None
        if not isinstance(metrics, dict):
            continue
        saw_metrics = True
        for key in ("prompt_tokens", "completion_tokens", "thinking_tokens"):
            value = metrics.get(key)
            if isinstance(value, (int, float)):
                tokens += float(value)
    return tokens if saw_metrics else None


def tokens_per_task(final_result: dict) -> float | None:
    detailed = final_result.get("detailed_results_by_split")
    if not isinstance(detailed, dict):
        return None
    values = [
        tokens
        for rows in detailed.values()
        if isinstance(rows, list)
        for row in rows
        if isinstance(row, dict) and (tokens := row_total_tokens(row)) is not None
    ]
    return sum(values) / len(values) if values else None


def load_run(path: Path) -> RunSummary | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Skipping unreadable JSON: {path} ({exc})")
        return None
    if not isinstance(payload, dict):
        return None

    final_result = payload.get("final_result")
    if not isinstance(final_result, dict):
        final_result = payload if "pass_rate" in payload else None
    if final_result is None:
        print(f"Skipping JSON without a final result: {path}")
        return None

    max_score = final_result.get("max_score")
    num_samples = int(max_score) if isinstance(max_score, (int, float)) else 0

    power_scores = final_result.get("pass_power_k_scores")
    at_scores = final_result.get("pass_at_k_scores")
    power_scores = power_scores if isinstance(power_scores, dict) else {}
    at_scores = at_scores if isinstance(at_scores, dict) else {}

    return RunSummary(
        path=path,
        label=run_label(payload, path),
        scenario=scenario_name(payload),
        num_samples=num_samples,
        pass_rate=as_percent(final_result.get("pass_rate")),
        pass_power_3=as_percent(power_scores.get("Pass^3")),
        pass_at_3=as_percent(at_scores.get("Pass@3")),
        tokens_per_task=tokens_per_task(final_result),
    )


def fmt(value: float | None) -> str:
    return f"{value:5.1f}%" if value is not None else "     —"


def fmt_tokens(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:.0f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Directory containing saved result JSON files. Default: {DEFAULT_INPUT_DIR}",
    )
    args = parser.parse_args()

    if not args.input_dir.exists():
        raise SystemExit(f"No input directory found: {args.input_dir}")

    runs = [
        run
        for path in sorted(args.input_dir.rglob("*.json"))
        if (run := load_run(path)) is not None
    ]
    if not runs:
        raise SystemExit(f"No result JSON files found under {args.input_dir}")

    groups: dict[int, list[RunSummary]] = {}
    for run in runs:
        groups.setdefault(run.num_samples, []).append(run)

    label_width = max(len(run.label) for run in runs)
    scenario_width = max(len("scenario"), max(len(run.scenario) for run in runs))
    header = (
        f"{'run':<{label_width}}  {'scenario':<{scenario_width}}  "
        f"{'samples':>7}  {'pass rate':>9}  {'Pass^3':>7}  {'Pass@3':>7}  {'tok/task':>8}"
    )

    for num_samples in sorted(groups, reverse=True):
        group = sorted(
            groups[num_samples],
            key=lambda run: (
                run.pass_power_3 if run.pass_power_3 is not None else -1.0,
                run.pass_rate if run.pass_rate is not None else -1.0,
            ),
            reverse=True,
        )
        print(f"=== {num_samples} samples ===")
        print(header)
        for run in group:
            print(
                f"{run.label:<{label_width}}  {run.scenario:<{scenario_width}}  "
                f"{run.num_samples:>7}  {fmt(run.pass_rate):>9}  "
                f"{fmt(run.pass_power_3):>7}  {fmt(run.pass_at_3):>7}  "
                f"{fmt_tokens(run.tokens_per_task):>8}"
            )
        print()


if __name__ == "__main__":
    main()
