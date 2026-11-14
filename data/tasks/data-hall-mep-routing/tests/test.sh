#!/bin/bash
# Programmatic verifier: runs the evaluator in a scratch directory holding /tests/evaluator plus the
# agent's output/solution.py; its metrics.json becomes /logs/verifier/reward.json.
#   on an evaluator failure: GRADER_FAILED.txt, no reward (a 0, CANDIDATE_BROKE_GRADER.txt, if tests/canary clears it)
#   evaluator: bash frontier_eval/run_eval.sh python3 $BENCH $BENCH/output/solution.py
set -uo pipefail
mkdir -p /logs/verifier
# stop what the agent left running in this container (root, shared with the verifier) before writing anything
stop_leftovers() {
[ -f /.dockerenv ] || return 0
python3 -I -S - <<'PY'
import os, signal
def stat(pid):
    f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return f[0], int(f[1]), int(f[19])
if open("/proc/1/comm").read().strip() not in ("systemd", "init"):
    born, keep, pid = stat(1)[2], {1}, os.getpid()
    while pid > 1:
        keep.add(pid)
        try:
            pid = stat(pid)[1]
        except (OSError, ValueError, IndexError):
            break
    for _ in range(100):
        stopped = False
        for p in os.listdir("/proc"):
            if p.isdigit() and int(p) not in keep:
                try:
                    state, _, start = stat(int(p))
                    if state != "Z" and start - born > 3 * os.sysconf("SC_CLK_TCK"):
                        os.kill(int(p), signal.SIGKILL)
                        stopped = True
                except (OSError, ValueError, IndexError):
                    pass
        if not stopped:
            break
PY
}
stop_leftovers
# remove everything the agent phase left in /logs/verifier, of any type (Harbor's open test-stdout.txt stays)
find /logs/verifier -mindepth 1 -maxdepth 1 ! -name test-stdout.txt -exec rm -rf -- {} + 2>/dev/null
# remove Python start-up hooks (*.pth, sitecustomize, usercustomize) that no installed package's RECORD lists
purge_site_hooks() {
[ -f /.dockerenv ] || return 0
python3 -I -S - <<'PY'
import base64, csv, hashlib, os, shutil, site, sys
log = "/logs/verifier/site_hooks_removed.txt"
if os.path.lexists(log):
    os.unlink(log)
site_dirs = [d for d in site.getsitepackages() + [site.getusersitepackages()] if os.path.isdir(d)]
listed = {}
for d in site_dirs:
    for entry in os.listdir(d):
        record = os.path.join(d, entry, "RECORD")
        if entry.endswith(".dist-info") and os.path.isfile(record):
            with open(record, newline="", encoding="utf-8", errors="replace") as f:
                for row in csv.reader(f):
                    if row:
                        listed[os.path.normpath(os.path.join(d, row[0]))] = row[1] if len(row) > 1 else ""
def unlisted(path):
    if os.path.isdir(path) and not os.path.islink(path):
        return unlisted(os.path.join(path, "__init__.py"))
    digest = listed.get(os.path.normpath(path))
    if digest is None or os.path.islink(path):
        return True
    if not digest.startswith("sha256=") or not os.path.isfile(path):
        return False
    with open(path, "rb") as f:
        return digest != "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(f.read()).digest()).rstrip(b"=").decode()
hooks, path_dirs = [], set(site_dirs) | {d for d in sys.path if d}
for d in site_dirs:
    for entry in sorted(os.listdir(d)):
        p = os.path.join(d, entry)
        if entry.endswith(".pth"):
            hooks.append(p)
            if not unlisted(p) and os.path.isfile(p):
                with open(p, encoding="utf-8", errors="replace") as f:
                    path_dirs.update(os.path.join(d, line.strip()) for line in f
                                     if line.strip() and not line.startswith(("#", "import ", "import\t")))
for d in sorted(path_dirs):
    if os.path.isdir(d):
        hooks += [os.path.join(d, e) for e in sorted(os.listdir(d)) if e.split(".")[0] in ("sitecustomize", "usercustomize")]
removed, failed = [], []
for p in hooks:
    if unlisted(p):
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.unlink(p)
            removed.append(p)
        except OSError as e:
            failed.append(f"{p} NOT REMOVED: {e}")
if removed or failed:
    with open(log, "w", encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in removed + failed))
sys.exit(1 if failed else 0)
PY
}
purge_site_hooks || { echo "a Python start-up hook could not be removed (see site_hooks_removed.txt)" > /logs/verifier/GRADER_FAILED.txt; exit 0; }

BENCH=/tmp/benchmark
WORKSPACE=/app/files/dc_model__hall_seed000_d000__mep_routing
CANDIDATE=output/solution.py
rm -rf "$BENCH"; mkdir -p "$BENCH"
cp -a /tests/evaluator/. "$BENCH"/
# The candidate is copied as a plain file; a missing candidate, a symlink or a directory scores 0 without grading.
if [ -L "$WORKSPACE/$CANDIDATE" ] || [ ! -f "$WORKSPACE/$CANDIDATE" ] \
   || [ "$(realpath -e "$WORKSPACE/$CANDIDATE")" != "$(realpath -e "$WORKSPACE")/$CANDIDATE" ]; then
  echo "candidate $CANDIDATE is missing from the workspace, or is not a regular file (a symlink or a directory)" > /logs/verifier/eval.stderr.txt
  echo '{"reward": 0.0}' > /logs/verifier/reward.json
  exit 0
fi
mkdir -p "$BENCH/$(dirname "$CANDIDATE")" && cp "$WORKSPACE/$CANDIDATE" "$BENCH/$CANDIDATE"

# run_eval.sh always exits 0: the python3 it runs is a shim recording the evaluator's own exit status
SHIM=$(mktemp -d)
printf '#!/bin/sh\npython3 "$@"\nrc=$?\necho $rc > %s/exit\nexit $rc\n' "$SHIM" > "$SHIM/python3" && chmod 755 "$SHIM/python3"
evaluator_exit() { if [ "$1" != 0 ]; then echo "$1"; else cat "$SHIM/exit" 2>/dev/null || echo unrecorded; fi; rm -f "$SHIM/exit"; }
cd "$BENCH"
bash frontier_eval/run_eval.sh "$SHIM/python3" $BENCH $BENCH/output/solution.py > /logs/verifier/eval.stdout.txt 2> /logs/verifier/eval.stderr.txt
EVAL_EXIT=$?
# the evaluator may have run candidate code: stop what it left running, drop its FIFOs/links and the outcome files
stop_leftovers
find /logs/verifier -mindepth 1 -maxdepth 1 ! -type f ! -type d ! -name test-stdout.txt -exec rm -f -- {} + 2>/dev/null
find /logs/verifier -type l -delete 2>/dev/null
rm -rf /logs/verifier/reward.json /logs/verifier/reward.txt /logs/verifier/metrics.json /logs/verifier/GRADER_FAILED.txt /logs/verifier/CANARY_CHECK.txt /logs/verifier/CANDIDATE_BROKE_GRADER.txt /logs/verifier/canary_metrics.json /logs/verifier/artifacts.json /logs/verifier/canary.stdout.txt /logs/verifier/canary.stderr.txt
EVAL_EXIT=$(evaluator_exit "$EVAL_EXIT")
for s in stdout stderr; do [ -f "$BENCH/eval.$s.txt" ] && [ ! -L "$BENCH/eval.$s.txt" ] && cat "$BENCH/eval.$s.txt" >> "/logs/verifier/eval.$s.txt"; done
echo "evaluator exit: $EVAL_EXIT" >> /logs/verifier/eval.stdout.txt
[ -f "$BENCH/metrics.json" ] && cp "$BENCH/metrics.json" /logs/verifier/metrics.json
[ -f "$BENCH/artifacts.json" ] && cp "$BENCH/artifacts.json" /logs/verifier/artifacts.json

# metrics.json -> reward.json; every evaluator failure shape leaves GRADER_FAILED.txt and no reward, a candidate fault is a real 0
reward_step() {
python3 -I -S - "$@" <<'PY'
import json, math, pathlib, sys
out = pathlib.Path("/logs/verifier")
# CANARY: the header's rule; CANDIDATE_ERROR_TYPES: a malformed candidate's `error`; GRADER_FAULT_MARKER: in `details`
CANDIDATE_ERROR_TYPES = ()
CANARY = True
GRADER_FAULT_MARKER = None
CANARY_CLEARS_AT_GATE = {}
REWARD_SCALE = 1.0  # a reward is clamped to [0, REWARD_SCALE]
ON_CANARY = sys.argv[2:] == ["canary"]
LOG = "canary.stderr.txt" if ON_CANARY else "eval.stderr.txt"

def grader_failed(why: str, own_fault: bool = False, canary_says: str = "the pristine canary fails too") -> None:
    if ON_CANARY:
        check = out / "CANARY_CHECK.txt"
        first = check.read_text().strip()
        check.unlink()
        why = f"{first}; {canary_says}: {why}"
    elif CANARY and not own_fault:
        (out / "CANARY_CHECK.txt").write_text(why + "\n")
        sys.exit(0)
    (out / "GRADER_FAILED.txt").write_text(why + "\n")
    sys.exit(0)

def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

m = out / ("canary_metrics.json" if ON_CANARY else "metrics.json")
if not m.is_file():
    grader_failed(f"evaluator exit {sys.argv[1]} and no metrics.json (see {LOG})")
if sys.argv[1] != "0":
    grader_failed(f"evaluator exit {sys.argv[1]}: its metrics.json is not trusted (see {LOG})")
try:
    d = json.loads(m.read_text())
except ValueError as e:
    grader_failed(f"metrics.json unreadable: {e}")
if not isinstance(d, dict):
    grader_failed("metrics.json is not a JSON object")
status, reason, error = (str(d.get(k) or "") for k in ("exec_status", "reason", "error"))
score = num(d.get("combined_score"))
if score is not None and not math.isfinite(score):
    grader_failed(f"evaluator reported combined_score {score}")
if d.get("grader_exception"):
    grader_failed(f"evaluator reported grader_exception ({d.get('grader_exception_type')})")
if status.startswith("ERR_GRADE"):
    grader_failed(f"evaluator reported {status}: {reason}")
if reason.startswith("grader"):
    grader_failed(f"evaluator reported: {reason}")
if set(d) <= {"combined_score", "valid"} and score == 0.0 and num(d.get("valid")) == 0.0:
    grader_failed("evaluator wrote its fallback metrics (the grader process left none)")
if error and not error.startswith(CANDIDATE_ERROR_TYPES):
    grader_failed(f"evaluator reported error: {error}")
art = out / "artifacts.json"
if GRADER_FAULT_MARKER and art.is_file():
    try:
        details = json.loads(art.read_text()).get("details")
    except (ValueError, AttributeError):
        details = None
    for k, v in (details.items() if isinstance(details, dict) else ()):
        if GRADER_FAULT_MARKER in str(v):
            grader_failed(f"evaluator details report its own fault: {k} = {v}", own_fault=True)
if ON_CANARY:
    try:
        cand = json.loads((out / "metrics.json").read_text())
    except (OSError, ValueError):
        cand = None
    gate = CANARY_CLEARS_AT_GATE.get(str(cand.get("verdict") or "")) if isinstance(cand, dict) else None
    if not ((score or 0) > 0 or (gate is not None and d.get("gate_errors") == gate)):
        grader_failed(f"the pristine canary was not graded through (combined_score {score}, verdict "
                      f"{d.get('verdict') or d.get('exec_status') or '-'}), so whose crash it is cannot be told",
                      own_fault=True, canary_says="the canary cannot clear the grader")
    (out / "CANARY_CHECK.txt").rename(out / "CANDIDATE_BROKE_GRADER.txt")
    with open(out / "CANDIDATE_BROKE_GRADER.txt", "a") as f:
        f.write(f"the evaluator grades the pristine canary cleanly (combined_score {score}): "
                "the crash is this candidate's, scored 0\n")
    score = 0.0
score = 0.0 if score is None else min(max(score, 0.0), REWARD_SCALE)
(out / "reward.json").write_text(json.dumps({"reward": score}) + "\n")
PY
}
reward_step "$EVAL_EXIT"
if [ -f /logs/verifier/CANARY_CHECK.txt ]; then
  rm -rf "$BENCH"; mkdir -p "$BENCH"
  if cp -a /tests/evaluator/. "$BENCH"/ && mkdir -p "$BENCH/$(dirname "$CANDIDATE")" \
     && cp "/tests/canary/$(basename "$CANDIDATE")" "$BENCH/$CANDIDATE"; then
    cd "$BENCH"
    bash frontier_eval/run_eval.sh "$SHIM/python3" $BENCH $BENCH/output/solution.py > /logs/verifier/canary.stdout.txt 2> /logs/verifier/canary.stderr.txt
    CANARY_EXIT=$?; CANARY_EXIT=$(evaluator_exit "$CANARY_EXIT")
    for s in stdout stderr; do [ -f "$BENCH/eval.$s.txt" ] && [ ! -L "$BENCH/eval.$s.txt" ] && cat "$BENCH/eval.$s.txt" >> "/logs/verifier/canary.$s.txt"; done
    [ -f "$BENCH/metrics.json" ] && cp "$BENCH/metrics.json" /logs/verifier/canary_metrics.json
    reward_step "$CANARY_EXIT" canary
  else
    { cat /logs/verifier/CANARY_CHECK.txt; echo "; the pristine canary could not be staged, so whose crash it is cannot be told"; } \
      > /logs/verifier/GRADER_FAILED.txt
    rm -f /logs/verifier/CANARY_CHECK.txt
  fi
fi
