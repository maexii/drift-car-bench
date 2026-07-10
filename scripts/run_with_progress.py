#!/usr/bin/env python3
"""Run a CAR-bench scenario TOML with a live progress bar.

Wraps `uv run car-bench-run <scenario> --show-logs`, streams the full output
to a logfile, and renders a compact progress view in the terminal:

    uv run python scripts/run_with_progress.py scenarios/.../local_iter.toml

Progress markers parsed from the evaluator output:
  - "Running N tasks from <split> split"  -> denominator per split
  - lines starting with the check/cross emoji + "task_id=" -> one finished trial
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SPLITS = ("base", "hallucination", "disambiguation")
TASK_DONE_RE = re.compile(r"^(✅|❌) task_id=([a-z_]+_\d+)")
RUNNING_RE = re.compile(r"Running (\d+) tasks from ([a-z]+)_(?:train|test) split")
PHASE_RES = (
    re.compile(r"Loading tasks from HuggingFace: \S+ / (\S+)"),
    re.compile(r"(Running task \d+)"),
    re.compile(r"(Starting CAR-bench evaluation)"),
)


def fmt_secs(s: float) -> str:
    m, sec = divmod(int(s), 60)
    return f"{m:02d}:{sec:02d}"


def render(done: dict, total: dict, passed: dict, start: float, last_event: str) -> str:
    n_done = sum(done.values())
    n_total = sum(total.values()) or 0
    elapsed = time.time() - start
    if n_done and n_total:
        eta = elapsed / n_done * (n_total - n_done)
        eta_s = f" ETA {fmt_secs(eta)}"
    else:
        eta_s = ""
    width = 20
    frac = n_done / n_total if n_total else 0.0
    bar = "█" * int(frac * width) + "░" * (width - int(frac * width))
    per_split = " ".join(
        f"{sp[0]}:{passed[sp]}✓{done[sp] - passed[sp]}✗"
        for sp in SPLITS
    )
    line = (
        f"[{bar}] {n_done}/{n_total or '?'} ({100 * frac:3.0f}%) "
        f"{fmt_secs(elapsed)}{eta_s} {per_split} {last_event}"
    )
    # Auf Terminalbreite kappen, sonst bricht die Zeile um und '\r' greift nicht.
    cols = shutil.get_terminal_size((100, 20)).columns
    return line[: max(cols - 1, 20)]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    scenario = Path(sys.argv[1])
    cfg = tomllib.loads(scenario.read_text())["config"]
    trials = int(cfg.get("num_trials", 1))

    total: dict[str, int] = {}
    for sp in SPLITS:
        n = int(cfg.get(f"tasks_{sp}_num_tasks", -1))
        total[sp] = n * trials if n > 0 else 0  # 0 = unknown until runtime line

    log_dir = Path("output/progress_logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{datetime.now():%Y%m%d-%H%M%S}__{scenario.stem}.log"

    done: dict[str, int] = defaultdict(int)
    passed: dict[str, int] = defaultdict(int)
    last_event = "starte evaluator/agent..."
    start = time.time()

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"  # sonst puffern die Kindprozesse blockweise

    proc = subprocess.Popen(
        ["uv", "run", "car-bench-run", str(scenario), "--show-logs"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    def stop(*_):
        proc.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    # Reader-Thread: Zeilen in Queue, damit die Bar auch bei Stille tickt.
    lines: queue.Queue[str | None] = queue.Queue()

    def reader():
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()

    print(f"scenario: {scenario}")
    print(f"full log: {log_path}")
    in_summary = False
    eof = False
    with open(log_path, "w") as log:
        while not eof:
            drained = False
            try:
                while True:
                    line = lines.get(timeout=0.5)
                    drained = True
                    if line is None:
                        eof = True
                        break
                    log.write(line)
                    stripped = line.strip()

                    m = RUNNING_RE.search(stripped)
                    if m and m.group(2) in SPLITS:
                        total[m.group(2)] = int(m.group(1)) * trials

                    m = TASK_DONE_RE.match(stripped)
                    if m:
                        ok, tid = m.group(1) == "✅", m.group(2)
                        sp = tid.split("_")[0]
                        if sp in SPLITS:
                            done[sp] += 1
                            passed[sp] += ok
                            last_event = f"{'✓' if ok else '✗'} {tid}"
                    else:
                        for rx in PHASE_RES:
                            pm = rx.search(stripped)
                            if pm:
                                last_event = pm.group(1)
                                break

                    if "CAR-bench Results" in stripped:
                        in_summary = True
                        sys.stdout.write("\r\033[K\n")
                    if in_summary:
                        sys.stdout.write(line)
                    if lines.empty():
                        break
            except queue.Empty:
                pass
            if not in_summary:
                sys.stdout.write("\r\033[K" + render(done, total, passed, start, last_event))
                sys.stdout.flush()
            _ = drained

    rc = proc.wait()
    if not in_summary:
        sys.stdout.write("\r\033[K\n")
        print(f"run ended (exit {rc}) without results block — see log: {log_path}")
    else:
        print(f"\nfull log: {log_path}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
