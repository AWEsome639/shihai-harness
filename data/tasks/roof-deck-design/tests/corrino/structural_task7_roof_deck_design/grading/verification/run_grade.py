#!/usr/bin/env python3
"""Entrypoint: grade one candidate submission.zip.  Always writes metrics with a numeric score.

    python run_grade.py <submission.zip> --metrics-out metrics.json [--artifacts-out artifacts.json]

The split comes from DC_ROOF_SPLIT (heldout by default).  The grading
itself runs in a child process under CPU and memory rlimits; this parent uses only the standard
library, so a hostile file that crashes or exhausts the child still leaves a score of 0 behind.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent          # task folder
# grade.py sits in <task>/grading here and in <task>/ in the harness copy, where grading/'s
# contents are mounted one level up; resolve the package dir instead of assuming the depth.
GPKG = HERE.parent if (HERE.parent / "grade.py").is_file() else ROOT / "grading"
CPU_S = 900
MEM_B = 8 * 1024 ** 3
WALL_S = 2700


def _child(zip_path, out_path, split):
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_S, CPU_S + 10))
    resource.setrlimit(resource.RLIMIT_AS, (MEM_B, MEM_B))
    sys.path.insert(0, str(GPKG.parent))
    grade = __import__(f"{GPKG.name}.grade", fromlist=["grade"]).grade
    m = grade(zip_path, split)
    Path(out_path).write_text(json.dumps(m, default=float))


def main():
    args = sys.argv[1:]
    if args and args[0] == "--child":
        _child(args[1], args[2], args[3])
        return 0
    if not args:
        print(__doc__)
        return 0
    cand = args[0]
    metrics_out = "metrics.json"
    artifacts_out = None
    if "--metrics-out" in args:
        metrics_out = args[args.index("--metrics-out") + 1]
    if "--artifacts-out" in args:
        artifacts_out = args[args.index("--artifacts-out") + 1]
    split = os.environ.get("DC_ROOF_SPLIT", "heldout")
    if split not in ("dev", "heldout"):
        split = "heldout"
    result = {"combined_score": 0.0, "exec_status": "ERR_EXEC", "valid": 0, "split": split}
    with tempfile.TemporaryDirectory() as td:
        tmp_zip = Path(td) / "submission.zip"
        out = Path(td) / "m.json"
        try:
            src = Path(cand)
            if not src.is_file():
                raise FileNotFoundError(cand)
            if src.stat().st_size > 40 * 1024 * 1024:
                raise ValueError("candidate too large")
            tmp_zip.write_bytes(src.read_bytes())
            env = {"PATH": "/usr/bin:/bin", "HOME": td, "PYTHONDONTWRITEBYTECODE": "1", "OMP_NUM_THREADS": "1",
                   "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
            p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", str(tmp_zip), str(out), split],
                               capture_output=True, timeout=WALL_S, env=env, cwd=td)
            if out.exists():
                result = json.loads(out.read_text())
                result["valid"] = 1 if result.get("parse_ok") else 0
            else:
                tail = (p.stderr or b"")[-400:].decode("utf-8", "replace")
                status = "ERR_TIMEOUT" if p.returncode in (-9, -24) else "ERR_EXEC"
                result.update({"exec_status": status, "error": tail})
        except subprocess.TimeoutExpired:
            result.update({"exec_status": "ERR_TIMEOUT"})
        except Exception as e:  # noqa: BLE001
            result.update({"exec_status": "ERR_EXEC", "error": f"{type(e).__name__}: {e}"[:300]})
    s = result.get("combined_score", 0.0)
    try:
        s = float(s)
        if s != s:
            s = 0.0
    except Exception:  # noqa: BLE001
        s = 0.0
    result["combined_score"] = s
    Path(metrics_out).write_text(json.dumps(result, indent=1, default=float))
    if artifacts_out:
        Path(artifacts_out).write_text(json.dumps({"split": split}, indent=1))
    print(json.dumps({"combined_score": s, "exec_status": result.get("exec_status")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
