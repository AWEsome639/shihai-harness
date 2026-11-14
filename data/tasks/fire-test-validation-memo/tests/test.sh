#!/bin/bash
# Runs in Harbor's separate verifier container, after the agent finishes; the deliverables arrive as artifacts at their
# paths in the task container. This collects the deliverable, the vendor renderings and programmatic scores the task
# declares and the agent's final message, never through a symlink; then verify.py stages what the judge
# reads, and grader/grade.py asks the judge (JUDGE_BASE_URL, JUDGE_API_KEY, JUDGE_MODEL) about the rubric.
# A grading that fails twice writes GRADER_FAILED.txt and no reward, so the task is
# not measured.
set -u
# remove everything the agent phase left in /logs/verifier, of any type (Harbor's open test-stdout.txt stays)
find /logs/verifier -mindepth 1 -maxdepth 1 ! -name test-stdout.txt -exec rm -rf -- {} + 2>/dev/null
mkdir -p /logs/verifier/submission
if [ -L /app/files ] || [ -L /app/files/output ] || { [ -e /app/files/output ] \
     && [ "$(realpath -e /app/files/output)" != "$(realpath -e /app/files)/output" ]; }; then
  echo "output/ is a symlink or leads outside the workspace: not collected" > /logs/verifier/no_output.txt
elif [ -d /app/files/output ] && [ -n "$(ls -A /app/files/output 2>/dev/null)" ]; then
  cp -a /app/files/output/. /logs/verifier/submission/
else
  echo "agent produced no output/ directory" > /logs/verifier/no_output.txt
fi
# Files the agent created or changed anywhere in the world, judged against tests/world.md5; never through a symlink.
python3 -I -S - <<'PY'
import hashlib, os, pathlib, shutil
def md5_of(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
world = pathlib.Path("/app/files")
dest = pathlib.Path("/logs/verifier/submission/changed")
ref = dict()
for line in pathlib.Path("/tests/world.md5").read_text(encoding="utf-8").splitlines():
    if line and not line.startswith("#") and "  " in line:
        digest, rel = line.split("  ", 1)
        ref[rel] = digest
found = [] if world.is_symlink() else [pathlib.Path(d, f) for d, _, fs in os.walk(world, followlinks=False) for f in fs]
for p in sorted(found):
    if not p.is_file() or p.is_symlink():
        continue
    rel = p.relative_to(world).as_posix()
    if rel.startswith("output/"):
        continue
    if ref.get(rel) == md5_of(p):
        continue
    target = dest / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, target, follow_symlinks=False)
PY
# the agent's final message (the last agent step with text, or a finish tool's), as a new file
python3 -I -S - <<'PY'
import json, os, pathlib, shutil, stat
sub = pathlib.Path("/logs/verifier/submission")
out = sub / "FINAL_MESSAGE.md"
msg = None
MAX_TRAJECTORY = 256 << 20
LOG_TAIL = 16 << 20

def read_regular(path, cap, tail=False):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        size = os.fstat(fd).st_size
        if not stat.S_ISREG(os.fstat(fd).st_mode) or (size > cap and not tail):
            return None
        if size > cap:
            os.lseek(fd, size - cap, os.SEEK_SET)
        chunks, left = [], cap
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)

def text_of(message):
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return "\n".join(p["text"] for p in message
                          if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
    return ""

def discard(path):
    if os.path.lexists(path):
        shutil.rmtree(path) if os.path.isdir(path) and not os.path.islink(path) else os.unlink(path)

def write_new(path, text):
    discard(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)

traj = read_regular("/logs/agent/trajectory.json", MAX_TRAJECTORY)
if traj is not None:
    try:
        steps = json.loads(traj).get("steps") or []
        for step in reversed(steps):
            if not isinstance(step, dict) or step.get("source") != "agent":
                continue
            text = text_of(step.get("message")).strip()
            if text and text != "[tool call]":
                msg = text_of(step.get("message"))
                break
            done = [c.get("arguments", {}).get("message") for c in step.get("tool_calls") or []
                    if isinstance(c, dict) and c.get("function_name") == "finish" and isinstance(c.get("arguments"), dict)]
            done = [m for m in done if isinstance(m, str) and m.strip()]
            if done:
                msg = done[-1]
                break
    except Exception:
        msg = None
if msg is None:
    for log in sorted(pathlib.Path("/logs/agent").glob("*.txt")):
        for line in reversed((read_regular(log, LOG_TAIL, tail=True) or "").splitlines()):
            if line.startswith("{") and '"type":"result"' in line.replace(" ", ""):
                try:
                    msg = json.loads(line).get("result") or None
                except Exception:
                    msg = None
                if not isinstance(msg, str):
                    msg = None
                if msg:
                    break
        if msg:
            break
if msg:
    if sub.is_symlink() or not sub.is_dir():
        raise SystemExit("/logs/verifier/submission is not a directory")
    write_new(out, "# Agent's final message\n\n" + msg.rstrip() + "\n")
else:
    if sub.is_dir() and not sub.is_symlink():
        discard(out)
    write_new("/logs/verifier/no_final_message.txt", "no final agent message found under /logs/agent\n")
PY
cp /tests/task.json /logs/verifier/task.json
find /logs/verifier -type l -print -delete > /logs/verifier/symlinks_dropped.txt 2>/dev/null
[ -s /logs/verifier/symlinks_dropped.txt ] || rm -f /logs/verifier/symlinks_dropped.txt


for attempt in 1 2; do
  if python3 /tests/verify.py --submission /logs/verifier/submission --out /logs/verifier/grade.json \
       >> /logs/verifier/grade.log 2>&1; then
    python3 -I -S - <<'PY' && exit 0
import json, math
r = json.load(open("/logs/verifier/grade.json"))["reward"]
if not (isinstance(r, (int, float)) and math.isfinite(r) and 0 <= r <= 1):
    raise SystemExit(f"grade.json's reward {r!r} is not a number from 0 to 1")
json.dump({"reward": r}, open("/logs/verifier/reward.json", "w"))
PY
  fi
  sleep 5
done
tail -n 20 /logs/verifier/grade.log > /logs/verifier/GRADER_FAILED.txt
