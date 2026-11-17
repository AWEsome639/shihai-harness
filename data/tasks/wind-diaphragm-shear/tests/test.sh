#!/usr/bin/env bash
# The verifier (delivered_test.sh -> harbor_adapter.py -> the task's grader) runs in the
# verifier image, which Harbor starts as a separate container holding only the artifact
# /filesystem/final_batch4__structural_task5_wind_diaphragm_shear__blind/output/submission.zip
# (and the agent's /logs/artifacts, which no grader reads). The adapter's reward
# (combined_score when exec_status is ok and valid is positive, else 0, a missing submission included)
# is re-scored by the final scoring rule (0.4 p + 0.6 [h=1] O', tests/paper_score.py), whose score is the reward. An artifact that is not a regular
# file is no submission. When the grader fails -- the adapter exits non-zero (the grader died, overflowed its output
# or ran past its timeout) or the grader reports a failure status -- the adapter grades an empty
# submission.zip: graded cleanly, the failure is this candidate's and its 0 stands
# (CANDIDATE_BROKE_GRADER.txt); otherwise the grader cannot run here and nothing was graded (GRADER_FAILED.txt, no
# reward).
set -u
# remove everything the agent phase left in /logs/verifier, of any type (Harbor's open test-stdout.txt stays)
find /logs/verifier -mindepth 1 -maxdepth 1 ! -name test-stdout.txt -exec rm -rf -- {} + 2>/dev/null
ART=/filesystem/final_batch4__structural_task5_wind_diaphragm_shear__blind/output/submission.zip
if [ -L "$ART" ] || { [ -e "$ART" ] && [ ! -f "$ART" ]; }; then
  rm -rf -- "$ART"
  if [ -L "$ART" ] || [ -e "$ART" ]; then
    echo "$ART is not a regular file and could not be removed: nothing was graded" > /logs/verifier/GRADER_FAILED.txt
    exit 0
  fi
  echo "$ART was not a regular file (a symlink or a directory): no submission" > /logs/verifier/artifact_refused.txt
fi
status_of() {
  python3 -I -S -c 'import json, sys; d = json.load(open(sys.argv[1])); print(d.get("exec_status", d.get("status", "?")))' "$1" 2>/dev/null || echo "?"
}
grader_failed() {  # <adapter exit> <exec_status>
  [ "$1" -ne 0 ] && return 0
  case "$2" in ERR_NO_SUBMISSION) return 1 ;; ERR_*|GRADER_ERROR|"?") return 0 ;; *) return 1 ;; esac
}
bash /tests/delivered_test.sh
rc=$?
status=$(status_of /logs/verifier/metrics.json)
if grader_failed "$rc" "$status"; then
  why="the adapter exited $rc with exec_status $status"
  canary=$(mktemp -d)
  python3 -I -S - "$canary" <<'PY'
import json, sys, zipfile
d = sys.argv[1]
zipfile.ZipFile(f"{d}/submission.zip", "w").close()
cfg = json.load(open("/tests/adapter_config.json"))
cfg.update(candidate_paths=[f"{d}/submission.zip"], timeout_seconds=60)
json.dump(cfg, open(f"{d}/adapter_config.json", "w"))
PY
  timeout 80 python -B /tests/harbor_adapter.py --config "$canary/adapter_config.json" --logs "$canary/logs" \
    > /logs/verifier/canary.stdout.txt 2>&1
  crc=$?
  cstatus=$(status_of "$canary/logs/metrics.json")
  cp "$canary/logs/metrics.json" /logs/verifier/canary_metrics.json 2>/dev/null
  if grader_failed "$crc" "$cstatus"; then
    echo "$why; on an empty submission.zip it exits $crc with exec_status $cstatus: the grader cannot run here" \
      > /logs/verifier/GRADER_FAILED.txt
    rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
  else
    echo "$why; it grades an empty submission.zip cleanly (exec_status $cstatus): the failure is this candidate's, scored 0" \
      > /logs/verifier/CANDIDATE_BROKE_GRADER.txt
    echo 0.0 > /logs/verifier/reward.txt
  fi
fi
# the paper's score (paper_score.py) of a grade the adapter wrote cleanly: its re-score reads the adapter's
# output (and, where it needs to, the submission, with the grader's own code) within a time limit; the adapter's reward
# stays as reward.delivered.txt and metrics.json as the adapter wrote it. A grade it cannot re-score is a grading that
# failed (GRADER_FAILED.txt, no reward).
if ! grader_failed "$rc" "$status"; then
  paper=$(mktemp /tmp/paper_score.XXXXXX)
  if ! { timeout -k 10 1200 python3 -I -B /tests/paper_score.py score --verifier /logs/verifier \
           --submission "$ART" --grader-dir /tests/ > "$paper" 2>> /logs/verifier/paper_score.log \
         && python3 -I -B -S /tests/paper_score.py apply-native --result "$paper" --verifier /logs/verifier \
           >> /logs/verifier/paper_score.log 2>&1; }; then
    { echo "the adapter graded cleanly (exec_status $status), but its grade could not be scored by the final scoring rule (0.4 p + 0.6 [h=1] O', tests/paper_score.py):"
      tail -n 20 /logs/verifier/paper_score.log; } > /logs/verifier/GRADER_FAILED.txt
    rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
  fi
fi
exit 0
