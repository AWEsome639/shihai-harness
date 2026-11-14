#!/usr/bin/env python3
"""Grade one dc_model submission inside a self-contained task package.

Called by ``frontier_eval/run_eval.sh``. Everything it needs is a sibling of
this file: the vendored ``dc_model`` grader, the reference model, and
``meta.json``.

    solution.py --> python solution.py <tmp>/model.ifc --> dcgrade --> metrics.json

The candidate may be a ``solution.py``, the directory holding one, or an IFC
that was already built; all three are accepted so a reviewer can grade a model
by hand without re-running anybody's script.

Two things it refuses to do:

* score the unmodified stub as a modelling failure. An agent that never wrote
  anything and an agent that wrote something wrong are different results, and
  ``exec_status`` is where that difference is recorded.
* write the built model next to the reference. The script runs in a scratch
  directory precisely so that a stray relative path in it cannot land on, or
  read, the answer key.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from dc_model.dcgrade.score import score_task_a, score_task_b  # noqa: E402


def _resolve_candidate(candidate: Path) -> tuple[Path | None, str]:
    """``(path, kind)`` where kind is ``script``, ``ifc`` or ``missing``."""
    if candidate.is_dir():
        for name in ("solution.py", "model.ifc"):
            hit = candidate / name
            if hit.is_file():
                return hit, "script" if hit.suffix == ".py" else "ifc"
        ifcs = sorted(candidate.glob("*.ifc"))
        if ifcs:
            return ifcs[0], "ifc"
        return None, "missing"
    if candidate.is_file():
        return candidate, "script" if candidate.suffix == ".py" else "ifc"
    return None, "missing"


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate", required=True, type=Path)
    ap.add_argument("--benchmark-dir", required=True, type=Path)
    ap.add_argument("--metrics-out", required=True, type=Path)
    ap.add_argument("--artifacts-out", type=Path, default=None)
    args = ap.parse_args(argv)

    meta = json.loads((HERE / "meta.json").read_text(encoding="utf-8"))
    reference = HERE / "reference" / "ground_truth.ifc"
    metrics = {"combined_score": 0.0, "valid": 0.0}

    def finish(reason: str, status: str, report: dict | None = None) -> int:
        metrics["exec_status"] = status
        metrics["reason"] = reason
        _write(args.metrics_out, metrics)
        if args.artifacts_out:
            _write(args.artifacts_out,
                   {"report": report or {}, "task": meta["world_id"]})
        print(json.dumps(metrics, indent=2))
        return 0                       # never non-zero; run_eval.sh owns the exit

    path, kind = _resolve_candidate(args.candidate)
    if path is None:
        return finish(f"no solution.py or .ifc under {args.candidate}",
                      "ERR_NO_SUBMISSION")

    scratch = Path(tempfile.mkdtemp(prefix="dcgrade_"))
    try:
        if kind == "script":
            source = path.read_text(encoding="utf-8", errors="replace")
            if hashlib.sha256(source.encode("utf-8")).hexdigest() == meta["stub_sha256"]:
                return finish("submission stub was never replaced",
                              "ERR_NO_SUBMISSION")
            built = scratch / "model.ifc"
            # The optional side file is output/reconciliation.json "next to
            # the IFC". The workspace the script was written in has output/,
            # so the scratch cwd gets one too; without it such scripts crash.
            (scratch / "output").mkdir()
            started = time.time()
            try:
                proc = subprocess.run(
                    [sys.executable, str(path), str(built)],
                    capture_output=True, text=True, cwd=str(scratch),
                    timeout=meta["script_timeout_s"])
            except subprocess.TimeoutExpired:
                return finish(
                    f"script exceeded {meta['script_timeout_s']}s", "ERR_TIMEOUT")
            metrics["script_seconds"] = round(time.time() - started, 1)
            if proc.returncode != 0:
                return finish(f"script exited {proc.returncode}: "
                              f"{proc.stderr[-1200:]}", "ERR_EXEC")
            if not built.is_file():
                return finish("script wrote no file at the output path",
                              "ERR_NO_OUTPUT")
            submission = built
        else:
            submission = path

        try:
            if meta["variant"] == "hall_fitout":
                report = score_task_a(str(submission), str(reference))
            else:
                report = score_task_b(str(submission), str(reference),
                                      l_opt_mm=meta.get("l_opt_mm"))
        except Exception as exc:                       # a grader crash is not a 0
            return finish(f"{type(exc).__name__}: {exc}", "ERR_GRADE")

        payload = report.to_dict()
        metrics["combined_score"] = float(report.score)
        metrics["valid"] = 1.0
        # the components, never only the fused score — a scalar cannot tell a
        # modelling failure from an API-fluency one
        for key in ("recovery_confusion", "semantics", "violation_rate",
                    "clash_count", "routed_length", "objective", "gates",
                    "secondary"):
            if key in payload:
                metrics[key] = payload[key]
        reason = "ok"
        if report.exec_status != "OK":
            reason = (payload.get("gate_0", {}).get("reason")
                      or report.exec_status)
        return finish(reason, report.exec_status, payload)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
