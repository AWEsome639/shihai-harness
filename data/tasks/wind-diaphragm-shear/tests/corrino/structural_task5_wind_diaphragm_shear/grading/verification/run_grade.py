#!/usr/bin/env python3
"""Frozen grading entrypoint.

    python grading/verification/run_grade.py <submission.zip> --metrics-out metrics.json

Every exit path writes metrics with a numeric score and a machine-readable status, and the
process exits 0 - a grader that dies without a score turns an agent failure into a harness
failure.  CPU time is limited by rlimit inside the pinned environment, wall clock is only a
backstop.  The candidate's bytes are never imported or executed; the only thing read from the
zip is data, and every number the score depends on is recomputed from grading/data/.
"""
from __future__ import annotations

import json
import os
import resource
import signal
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# The scoring package is whichever directory actually holds grade.py: <task>/grading in the task
# folder, <task>/ in the harness copy, where grading/'s contents are mounted one level up. A fixed
# parent depth picks the wrong directory there and the import fails before any scoring runs.
HERE = Path(__file__).resolve().parent
GPKG = HERE.parent if (HERE.parent / "grade.py").is_file() else ROOT / "grading"
sys.path.insert(0, str(GPKG.parent))
PKG = ROOT.name
GRADE_MOD = f"{GPKG.name}.grade"

CPU_S = int(os.environ.get("DCWIND_GRADER_CPU_S", "1500"))
WALL_S = int(os.environ.get("DCWIND_GRADER_WALL_S", "2700"))


def write(out, payload):
    if out:
        Path(out).write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: payload.get(k) for k in
                      ("status", "combined_score", "parse_ok", "mean_p", "hard_rate",
                       "objective_raw", "installed_cost", "feasible_all_scenarios")}, indent=1))


def main():
    args = sys.argv[1:]
    if not args:
        write(None, {"status": "ERR_USAGE", "combined_score": 0.0, "parse_ok": False})
        return 0
    sub = args[0]
    out = None
    split = os.environ.get("DCWIND_SPLIT", "heldout")
    full = False
    for i, a in enumerate(args):
        if a == "--metrics-out" and i + 1 < len(args):
            out = args[i + 1]
        if a == "--split" and i + 1 < len(args):
            split = args[i + 1]
        if a == "--full":
            full = True
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_S, CPU_S + 30))
    except Exception:  # noqa: BLE001
        pass

    def on_alarm(signum, frame):  # noqa: ARG001
        write(out, {"status": "ERR_TIMEOUT", "combined_score": 0.0, "parse_ok": False,
                    "split": split})
        os._exit(0)

    try:
        signal.signal(signal.SIGALRM, on_alarm)
        signal.signal(signal.SIGXCPU, on_alarm)
        signal.alarm(WALL_S)
    except Exception:  # noqa: BLE001
        pass
    try:
        mod = __import__(GRADE_MOD, fromlist=["grade"])
        m = mod.grade(sub, split)
        if not full:
            m.pop("_detail", None)
        s = m.get("combined_score", 0.0)
        if not isinstance(s, (int, float)) or s != s:
            m["combined_score"] = 0.0
            m["status"] = "ERR_SCORE"
    except MemoryError:
        m = {"status": "ERR_MEMORY", "combined_score": 0.0, "parse_ok": False, "split": split}
    except Exception:  # noqa: BLE001
        m = {"status": "ERR_EXEC", "combined_score": 0.0, "parse_ok": False, "split": split,
             "traceback": traceback.format_exc()[-4000:]}
    try:
        signal.alarm(0)
    except Exception:  # noqa: BLE001
        pass
    write(out, m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
