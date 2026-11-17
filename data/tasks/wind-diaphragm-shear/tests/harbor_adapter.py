#!/usr/bin/env python3
"""Bridge the task's grader to Harbor, preserving its raw result."""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path


def normalize(payload: dict, returncode: int, optional_valid: bool) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Grader metrics must be an object")
    if returncode:
        raise ValueError(f"Grader process exited {returncode}")
    status = payload.get("exec_status", payload.get("status"))
    if not isinstance(status, str) or not status.strip():
        raise ValueError("Grader omitted execution status")
    status = status.strip()
    raw = payload.get("combined_score")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise ValueError("Grader omitted a finite numeric combined_score")
    if not 0.0 <= raw <= 1.0:
        raise ValueError("Grader score is outside [0, 1]")
    result = dict(payload)
    result["exec_status"] = status
    if "valid" not in result:
        if not optional_valid:
            raise ValueError("Required client validity field is missing")
        parse_ok = payload.get("parse_ok")
        if not isinstance(parse_ok, bool):
            raise ValueError("Client validity contract requires a boolean parse_ok")
        result["valid"] = 1.0 if status.lower() == "ok" and parse_ok else 0.0
        result["adapter_validity_basis"] = "Client contract omits valid; execution status, parse_ok and finite combined_score checked"
    valid = result["valid"]
    if not isinstance(valid, (bool, int, float)) or not math.isfinite(float(valid)):
        raise ValueError("Invalid client validity value")
    if not 0.0 <= float(valid) <= 1.0:
        raise ValueError("Client validity is outside [0, 1]")
    if status.lower() != "ok":
        result["client_reported_valid"] = valid
        result["valid"] = 0.0
    result["harbor_adapter_version"] = "corrino-b4-code-v1"
    result["harbor_reward"] = float(raw) if status.lower() == "ok" and float(valid) > 0 else 0.0
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--logs", type=Path, default=Path("/logs/verifier"))
    a = ap.parse_args()
    config = json.loads(a.config.read_text())
    a.logs.mkdir(parents=True, exist_ok=True)
    metrics = a.logs / "metrics.json"
    raw_metrics = a.logs / "client_metrics.json"
    artifacts = a.logs / "artifacts.json"
    reward = a.logs / "reward.txt"
    candidate = next((Path(p) for p in config["candidate_paths"] if Path(p).is_file()), None)
    if candidate is None:
        metrics.write_text(json.dumps({"combined_score": 0.0, "valid": 0.0,
                                      "exec_status": "ERR_NO_SUBMISSION"}) + "\n")
        reward.write_text("0.0\n")
        return 0
    grader = Path(config["grader"])
    if not grader.is_file():
        raise RuntimeError("Packaged grader entrypoint is missing")
    command = [sys.executable, "-B", str(grader), str(candidate),
               "--metrics-out", str(raw_metrics), "--artifacts-out", str(artifacts)]
    environment = dict(os.environ)
    environment.update(PYTHONDONTWRITEBYTECODE="1", OPENBLAS_NUM_THREADS="1",
                       OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", MPLCONFIGDIR="/tmp/matplotlib")
    try:
        with (a.logs / "verifier.log").open("w") as log:
            proc = subprocess.run(command, cwd=grader.parent.parent, env=environment,
                                  stdout=log, stderr=subprocess.STDOUT,
                                  timeout=config["timeout_seconds"], check=False)
        if not raw_metrics.is_file() or raw_metrics.stat().st_size > 32 * 1024**2:
            raise ValueError("Grader metrics missing or oversized")
        result = normalize(json.loads(raw_metrics.read_text()), proc.returncode,
                           bool(config["valid_optional"]))
        metrics.write_text(json.dumps(result, indent=2) + "\n")
        if not artifacts.exists():
            artifacts.write_text(json.dumps({"note": "Client grader does not emit a separate artifacts file",
                                             "raw_metrics": "client_metrics.json"}) + "\n")
        reward.write_text(f"{result['harbor_reward']:.17g}\n")
        print(json.dumps({"exec_status": result["exec_status"], "valid": result["valid"],
                          "combined_score": result["combined_score"]}))
        return 0
    except Exception as exc:
        status = "ERR_ADAPTER_TIMEOUT" if isinstance(exc, subprocess.TimeoutExpired) else "ERR_ADAPTER"
        metrics.write_text(json.dumps({"combined_score": 0.0, "valid": 0.0,
                                      "exec_status": status, "error": str(exc)[:600]}) + "\n")
        reward.write_text("0.0\n")
        print(json.dumps({"exec_status": status, "valid": 0.0, "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
