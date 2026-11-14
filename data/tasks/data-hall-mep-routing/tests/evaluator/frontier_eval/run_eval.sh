#!/usr/bin/env bash
# Frontier-compatible eval entrypoint for one dc_model task.
# Usage: run_eval.sh <python> <benchmark_dir> [candidate]
#   <candidate> = the agent's solution.py, the directory holding it, or an
#                 already-built .ifc. Defaults to <benchmark_dir>/output.
# Writes metrics.json (combined_score, valid) in <benchmark_dir>. ALWAYS exits 0.
set -uo pipefail

PYTHON_CMD="${1:?usage: run_eval.sh <python> <benchmark_dir> [candidate]}"
BENCHMARK_DIR="${2:?usage: run_eval.sh <python> <benchmark_dir> [candidate]}"
CANDIDATE_PATH="${3:-}"

if [[ "${BENCHMARK_DIR}" != /* ]]; then
  BENCHMARK_DIR="$(cd "${BENCHMARK_DIR}" && pwd -P)"
fi
if [[ -z "${CANDIDATE_PATH}" ]]; then
  CANDIDATE_PATH="${BENCHMARK_DIR}/output"
elif [[ "${CANDIDATE_PATH}" != /* && ! -e "${CANDIDATE_PATH}" ]]; then
  CANDIDATE_PATH="${BENCHMARK_DIR}/${CANDIDATE_PATH}"
fi

METRICS_JSON="${BENCHMARK_DIR}/metrics.json"

"${PYTHON_CMD}" "${BENCHMARK_DIR}/frontier_eval/evaluate_submission.py" \
  --candidate "${CANDIDATE_PATH}" \
  --benchmark-dir "${BENCHMARK_DIR}" \
  --metrics-out "${METRICS_JSON}" \
  --artifacts-out "${BENCHMARK_DIR}/artifacts.json" \
  > "${BENCHMARK_DIR}/eval.stdout.txt" 2> "${BENCHMARK_DIR}/eval.stderr.txt" || true

if [[ ! -f "${METRICS_JSON}" ]]; then
  printf '{"combined_score": 0.0, "valid": 0.0, "reason": "grader produced no metrics.json"}\n' \
    > "${METRICS_JSON}"
fi
cat "${METRICS_JSON}"
exit 0
