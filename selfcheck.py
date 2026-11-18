"""SELFCHECK v1 — task-agnostic deliverable self-check (gates G1, G3, G4, G7).

The module is pure: it takes an executor callback and one contract dictionary
and returns a list of defects. It never touches the local filesystem, so the
same code works where the task files live inside a container.

    defects = selfcheck(run, contract, final_message=final_message)

where `run` adapts the harness's own container tool (nothing else is needed):

    def run(cmd):
        text = await agent.bash(cmd, timeout_sec=120)   # "exit code N\n..."
        code, _, out = text.partition("\n")
        return int(code.split()[-1]), out

`run` is a callable `run(cmd: str) -> tuple[int, str]` that executes `cmd`
inside the task container (the harness builds one from `session.exec` or
`Agent.bash`) and returns the exit code with the combined output. Every check
below reaches the container through that callback:

| gate | what it checks |
|------|----------------|
| G1 | the deliverable exists at the exact path, is a regular file (not a directory, not a symlink), and is 0 < size <= 95 MiB |
| G3 | the format parses: `.csv` header, `.json` load, `.xlsx` sheet names, `.zip` members, `.py` `ast.parse`, `.ifc` `ISO-10303-21` header, `.md`/`.txt` non-empty UTF-8 |
| G4 | enumerated columns stay inside `contract["vocab"]`; `contract["numeric_cols"]` holds no placeholder (`TBD`, `TBA`, `NaN`, `Inf`, `<fill in>`, ...) |
| G7 | `final_message` is non-empty and names every required deliverable |

G0/G2/G5/G6 are deliberately not implemented: G0 needs the instruction text,
G2/G5/G6 need archive members and repeated runs, which are a separate budget
(`TASK-REQUIREMENTS.md` §5.3).

The container side is a single probe program (`PROBE_SOURCE`) that runs as
`python3 -` through `run`. It reads only the paths named in a JSON spec and
prints one JSON line of facts — never file contents — so a 90 MiB deliverable
still costs a few hundred bytes of output. The probe carries no task
knowledge: extensions, vocabularies and placeholders all come from the
contract.

Contract keys read here (`TASK-REQUIREMENTS.md` §5.2):

    workdir        base for relative deliverable paths ("/app/files/...")
    deliverables   [{"path": "...", "kind": "py", "required": true}]
    archives       [{"path": "...zip"}]  same checks; CSV members feed G4
    secondary      [{"kind": "final_message", "required": true}]
    vocab          {"stage": ["HYDRO", ...]}; key "plan.csv:stage" scopes one
                   column to one file when two tables share a column name
    numeric_cols   ["flow_gpm", ...]
    banned_tokens  placeholders, defaults to DEFAULT_BANNED_TOKENS
    final_message  optional copy of the message G7 inspects

Feedback for the model is rendered by `feedback()`, which follows the fixed
shape of `TASK-REQUIREMENTS.md` §5.4: at most one fatal per round, fatal first,
then fix, then note, at most 12 items per round.
"""

from __future__ import annotations

import base64
import json
import posixpath
import shlex
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "DEFAULT_BANNED_TOKENS",
    "MAX_DELIVERABLE_BYTES",
    "SEVERITY_ORDER",
    "feedback",
    "probe_command",
    "selfcheck",
    "summary",
]

# A `run` callback: the shell command to execute in the container, and the
# (exit code, combined output) it produced.
Run = Callable[[str], "tuple[int, str]"]

MI_B = 1024 * 1024
# A single deliverable over 95 MiB is not graded (`AGENTS.md` §243).
MAX_DELIVERABLE_BYTES = 95 * MI_B
# Placeholders that make a numeric cell unreadable. The task may override this
# with `contract["banned_tokens"]`.
DEFAULT_BANNED_TOKENS = (
    "TBD",
    "TBA",
    "to be determined",
    "to be advised",
    "NaN",
    "Inf",
    "-Inf",
    "Infinity",
    "XXX",
    "<fill in>",
)
SEVERITY_ORDER = ("fatal", "fix", "note")
_GATES = ("G1", "G3", "G4", "G7")
MAX_ITEMS = 12
_HEREDOC = "SELFCHECK_PROBE_EOF"

# Contract statements quoted in every defect's `rule` field. They are generic
# restatements of the shared task rules (deliverable contract, 95 MiB limit,
# `final_message`), never a specific task's wording or answer.
RULE_G1 = (
    "Deliverables are graded at the exact paths named in the task statement;"
    " a missing path, a symlink, a directory, an empty file or a file over"
    " 95 MiB is not gradable."
)
RULE_G3 = (
    "Every deliverable must be readable in the format the task statement"
    " names: existing is not enough, the bytes must parse."
)
RULE_G4 = (
    "Enumerated columns may only use values from the closed vocabulary in the"
    " task statement, and numeric columns may not carry placeholders such as"
    " TBD/TBA/NaN/Inf."
)
RULE_G7 = (
    "The final message must state what was produced and where each"
    " deliverable is stored, naming every deliverable path."
)

# Per-check `rule` and `fix` text. Keys are check names emitted by the probe;
# an unknown name falls back to the generic G3 wording.
_CHECK_TEXT = {
    "csv_header": (
        RULE_G3,
        "rewrite the first line as a comma-separated header with one name per"
        " column",
    ),
    "csv_header_names": (
        RULE_G3,
        "give every column a unique, non-empty header name",
    ),
    "csv_rows": (
        RULE_G3,
        "the rows are not valid CSV (unbalanced quotes?); rewrite the file"
        " with csv.writer",
    ),
    "csv_encoding": (RULE_G3, "write the CSV as UTF-8"),
    "json_load": (
        RULE_G3,
        "the file is not valid JSON; rewrite it with json.dump",
    ),
    "xlsx_sheets": (
        RULE_G3,
        "save the workbook again as .xlsx so its sheet names can be read",
    ),
    "zip_members": (
        RULE_G3,
        "rebuild the archive with zipfile so its members can be listed",
    ),
    "zip_member_read": (
        RULE_G3,
        "a member of the archive cannot be read; rebuild the archive",
    ),
    "zip_member_encoding": (RULE_G3, "store archive members as UTF-8"),
    "py_syntax": (
        RULE_G3,
        "fix the syntax error so the script runs as delivered",
    ),
    "py_encoding": (RULE_G3, "write the script as UTF-8"),
    "ifc_header": (
        RULE_G3,
        "the file must start with an ISO-10303-21 STEP header line (write the"
        " model with ifcopenshell, not by hand)",
    ),
    "text_encoding": (RULE_G3, "write the file as UTF-8 text"),
    "text_empty": (RULE_G3, "the file carries no text at all"),
    "csv_truncated": (RULE_G3, "scanning stopped at the size cap"),
    "json_truncated": (RULE_G3, "scanning stopped at the size cap"),
    "py_truncated": (RULE_G3, "scanning stopped at the size cap"),
}

# The probe. It runs inside the task container and reads only the paths it is
# given, printing a single JSON line; nothing here knows about a task.
PROBE_SOURCE = r'''
import base64
import csv
import html
import io
import json
import os
import re
import stat as statmod
import sys
import zipfile

TEXT_CAP = 4 * 1024 * 1024
ROW_CAP = 200000
MEMBER_CAP = 20
_KINDS = {
    ".csv": "csv", ".json": "json", ".xlsx": "xlsx", ".xlsm": "xlsx",
    ".zip": "zip", ".py": "py", ".ifc": "ifc", ".md": "text", ".txt": "text",
}


def _stat(path):
  try:
    st = os.lstat(path)
  except OSError:
    return {"exists": False}
  link = statmod.S_ISLNK(st.st_mode)
  return {
      "exists": True,
      "islink": link,
      "isfile": statmod.S_ISREG(st.st_mode) and not link,
      "isdir": statmod.S_ISDIR(st.st_mode) and not link,
      "size": int(st.st_size),
  }


def _read(path, cap=TEXT_CAP):
  with open(path, "rb") as fh:
    data = fh.read(cap + 1)
  return data[:cap], len(data) > cap


def _ck(name, status, detail=""):
  return {"name": name, "status": status, "detail": str(detail)[:200]}


def _banned(value, banned):
  low = value.strip().casefold()
  if not low:
    return False
  for token in banned:
    if low == token:
      return True
    if re.search(r"(?<![0-9a-z])" + re.escape(token) + r"(?![0-9a-z])", low):
      return True
  return False


def _index(header):
  out = {}
  for pos, name in enumerate(header):
    if name and name not in out:
      out[name] = pos
  return out


def _scan_rows(rows, header, spec, basename):
  """Vocabulary and placeholder findings for one table."""
  out = []
  index = _index(header)
  cap = int(spec.get("violation_cap") or 6)
  for raw, allowed in (spec.get("vocab") or {}).items():
    target = str(raw)
    if ":" in target:
      fname, _, col = target.partition(":")
      if basename != fname:
        continue
      target = col
    pos = index.get(target)
    if pos is None:
      continue
    wanted = set(str(v).strip() for v in allowed)
    order, seen = [], {}
    for rno, row in enumerate(rows[1:], 2):
      cell = row[pos].strip() if pos < len(row) else ""
      if cell in wanted:
        continue
      if cell in seen:
        seen[cell][1] += 1
      else:
        seen[cell] = [rno, 1]
        order.append(cell)
    for cell in order[:cap]:
      rno, count = seen[cell]
      out.append({
          "kind": "vocab", "table": basename, "column": target,
          "value": cell if cell else "<empty>", "row": rno, "count": count,
          "distinct": len(order), "allowed": len(wanted),
      })
  banned = spec.get("banned") or []
  for col in (spec.get("numeric_cols") or []):
    col = str(col)
    pos = index.get(col)
    if pos is None:
      continue
    order, seen = [], {}
    for rno, row in enumerate(rows[1:], 2):
      cell = row[pos].strip() if pos < len(row) else ""
      if not cell or not _banned(cell, banned):
        continue
      if cell in seen:
        seen[cell][1] += 1
      else:
        seen[cell] = [rno, 1]
        order.append(cell)
    for cell in order[:cap]:
      rno, count = seen[cell]
      out.append({
          "kind": "placeholder", "table": basename, "column": col,
          "value": cell, "row": rno, "count": count, "distinct": len(order),
      })
  return out


def _table(text, basename, spec):
  """Header checks plus row findings for one CSV document."""
  try:
    rows = []
    for row in csv.reader(io.StringIO(text)):
      rows.append(row)
      if len(rows) >= ROW_CAP:
        break
  except csv.Error as exc:
    return [_ck("csv_rows", "fail", "not parseable as CSV: %s" % exc)], []
  if not rows or not rows[0]:
    return [_ck("csv_header", "fail",
                "the first row is empty, so the file has no header")], []
  header = [h.strip() for h in rows[0]]
  problems = []
  blanks = [n for n, h in enumerate(header) if not h]
  if blanks:
    problems.append("column %d has an empty name" % (blanks[0] + 1))
  dups = sorted(name for name in set(header) if name and header.count(name) > 1)
  if dups:
    problems.append("duplicate column %r" % dups[0])
  checks = [_ck("csv_header", "ok", ",".join(header)[:400])]
  if problems:
    checks.append(_ck("csv_header_names", "fix", "; ".join(problems)))
  return checks, _scan_rows(rows, header, spec, basename)


def _analyze_csv(path, spec):
  try:
    data, truncated = _read(path)
  except OSError as exc:
    return {"checks": [_ck("csv_rows", "fail", str(exc))], "violations": []}
  try:
    text = data.decode("utf-8-sig")
  except UnicodeDecodeError as exc:
    return {"checks": [_ck("csv_encoding", "fail", str(exc))], "violations": []}
  checks, violations = _table(text, os.path.basename(path), spec)
  if truncated:
    checks.append(_ck("csv_truncated", "note",
                      "file is over %d bytes; only the start was scanned"
                      % TEXT_CAP))
  return {"checks": checks, "violations": violations}


def _analyze_json(path, spec):
  try:
    data, truncated = _read(path)
  except OSError as exc:
    return {"checks": [_ck("json_load", "fail", str(exc))]}
  try:
    json.loads(data.decode("utf-8-sig"))
  except (UnicodeDecodeError, ValueError) as exc:
    return {"checks": [_ck("json_load", "fail", str(exc))]}
  checks = [_ck("json_load", "ok")]
  if truncated:
    checks.append(_ck("json_truncated", "note",
                      "file is over %d bytes" % TEXT_CAP))
  return {"checks": checks}


def _analyze_xlsx(path, spec):
  try:
    zf = zipfile.ZipFile(path)
  except (zipfile.BadZipFile, OSError) as exc:
    return {"checks": [_ck("xlsx_sheets", "fail", str(exc))]}
  try:
    xml = zf.read("xl/workbook.xml").decode("utf-8", "replace")
  except KeyError:
    return {"checks": [_ck("xlsx_sheets", "fail",
                           "no xl/workbook.xml, so this is not a workbook")]}
  names = [html.unescape(n) for n in
           re.findall(r'<sheet\b[^>]*?\bname="([^"]*)"', xml)]
  if not names:
    return {"checks": [_ck("xlsx_sheets", "fail",
                           "the workbook declares no sheets")]}
  return {"checks": [_ck("xlsx_sheets", "ok",
                         "%d sheets: %s" % (len(names), ", ".join(names[:8])))]}


def _analyze_zip(path, spec):
  checks, violations = [], []
  try:
    zf = zipfile.ZipFile(path)
    names = zf.namelist()
  except (zipfile.BadZipFile, OSError) as exc:
    return {"checks": [_ck("zip_members", "fail", str(exc))]}
  if not names:
    return {"checks": [_ck("zip_members", "fail",
                           "the archive lists no members")]}
  checks.append(_ck("zip_members", "ok",
                    "%d members: %s" % (len(names), ", ".join(names[:8]))))
  for name in [n for n in names if n.lower().endswith(".csv")][:MEMBER_CAP]:
    try:
      raw = zf.read(name)[:TEXT_CAP]
    except (KeyError, OSError, RuntimeError) as exc:
      checks.append(_ck("zip_member_read", "fix", "%s: %s" % (name, exc)))
      continue
    try:
      text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
      checks.append(_ck("zip_member_encoding", "fix", "%s: %s" % (name, exc)))
      continue
    sub_checks, sub_violations = _table(text, os.path.basename(name), spec)
    for check in sub_checks:
      if check["status"] != "ok":
        checks.append(_ck(check["name"], check["status"],
                          "%s: %s" % (name, check["detail"])))
    violations.extend(sub_violations)
  return {"checks": checks, "violations": violations}


def _analyze_py(path, spec):
  try:
    data, truncated = _read(path)
  except OSError as exc:
    return {"checks": [_ck("py_syntax", "fail", str(exc))]}
  if truncated:
    return {"checks": [_ck("py_truncated", "note",
                           "script is over %d bytes" % TEXT_CAP)]}
  try:
    text = data.decode("utf-8-sig")
  except UnicodeDecodeError as exc:
    return {"checks": [_ck("py_encoding", "fail", str(exc))]}
  try:
    compile(text, path, "exec")
  except SyntaxError as exc:
    return {"checks": [_ck("py_syntax", "fail",
                           "%s (line %s)" % (exc.msg, exc.lineno))]}
  return {"checks": [_ck("py_syntax", "ok")]}


def _analyze_ifc(path, spec):
  try:
    data, _ = _read(path, 4096)
  except OSError as exc:
    return {"checks": [_ck("ifc_header", "fail", str(exc))]}
  first = data.split(b"\n", 1)[0].decode("latin-1").strip()
  if "ISO-10303-21" not in first:
    return {"checks": [_ck("ifc_header", "fail",
                           "first line is %r" % first[:80])]}
  return {"checks": [_ck("ifc_header", "ok", first[:80])]}


def _analyze_text(path, spec):
  try:
    data, truncated = _read(path)
  except OSError as exc:
    return {"checks": [_ck("text_encoding", "fail", str(exc))]}
  try:
    text = data.decode("utf-8-sig")
  except UnicodeDecodeError as exc:
    return {"checks": [_ck("text_encoding", "fail", str(exc))]}
  if "\ufffd" in text:
    return {"checks": [_ck("text_encoding", "fail",
                           "contains U+FFFD replacement characters")]}
  ctrl = sum(1 for ch in text
             if ord(ch) < 32 and ch not in "\t\n\r\f\b")
  if text and (ctrl + text.count("\x00")) / float(len(text)) > 0.02:
    return {"checks": [_ck("text_encoding", "fail",
                           "looks like binary data, not text")]}
  if not text.strip():
    return {"checks": [_ck("text_empty", "fail",
                           "the file is empty or whitespace only")]}
  return {"checks": [_ck("text_encoding", "ok",
                         "%d characters" % len(text))]}


def _analyze(path, spec):
  kind = _KINDS.get(os.path.splitext(path)[1].lower())
  if kind is None:
    return {"kind": "", "checks": [], "violations": []}
  try:
    out = globals()["_analyze_" + kind](path, spec)
  except Exception as exc:
    out = {"checks": [_ck(kind + "_read", "fail",
                          "%s: %s" % (type(exc).__name__, exc))]}
  out["kind"] = kind
  return out


def main(argv):
  op = argv[0] if argv else ""
  if op == "stat":
    return {"ok": True, "files": dict((p, _stat(p)) for p in argv[1:])}
  if op == "scan":
    spec = json.loads(base64.b64decode(argv[1]).decode("utf-8"))
    spec["banned"] = [str(t).casefold() for t in (spec.get("banned") or [])]
    files = dict((p, _analyze(p, spec)) for p in (spec.get("paths") or []))
    return {"ok": True, "files": files}
  return {"ok": False, "error": "unknown op %r" % op}


try:
  RESULT = main(sys.argv[1:])
except Exception as exc:
  RESULT = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
sys.stdout.write(json.dumps(RESULT) + "\n")
'''


def probe_command(op: str, *args: str) -> str:
  """Builds the container command that runs the probe.

  The probe source travels on stdin (quoted heredoc), so no argument has to be
  escaped except the argv tokens themselves.

  Args:
    op: Probe operation, `stat` or `scan`.
    *args: Arguments for the operation (paths, or one base64 JSON spec).

  Returns:
    A shell command string for the `run` callback.
  """
  argv = " ".join(shlex.quote(str(arg)) for arg in (op,) + args)
  return "python3 - %s <<'%s'\n%s\n%s\n" % (
      argv,
      _HEREDOC,
      PROBE_SOURCE.strip("\n"),
      _HEREDOC,
  )


def _parse_output(output: Any) -> dict[str, Any] | None:
  """Returns the probe's JSON object from a command's output, or None.

  The harness may prefix the exit code line or append stderr, so the last
  line that parses as a JSON object wins.

  Args:
    output: Combined stdout/stderr text of the probe command.

  Returns:
    The decoded probe result, or None when nothing parseable came back.
  """
  for line in reversed(str(output or "").splitlines()):
    line = line.strip()
    if not line.startswith("{"):
      continue
    try:
      value = json.loads(line)
    except ValueError:
      continue
    if isinstance(value, dict):
      return value
  return None


def _probe(run: Run, op: str, *args: str) -> dict[str, Any] | None:
  """Runs one probe operation and returns its JSON result, or None.

  Args:
    run: Executor callback (shell command -> (exit code, output)).
    op: Probe operation name.
    *args: Operation arguments.

  Returns:
    The probe result dictionary, or None if the container or the probe failed
    (for example no `python3`, or a clipped output).
  """
  try:
    code, output = run(probe_command(op, *args))
  except Exception:  # noqa: BLE001 - a broken executor must not kill the run
    return None
  result = _parse_output(output)
  if result is None or not result.get("ok"):
    return None
  return result


def _resolve(workdir: str, path: str) -> str:
  """Resolves a contract path to an absolute container path.

  Args:
    workdir: Task working directory inside the container.
    path: Absolute or workdir-relative deliverable path.

  Returns:
    The normalized absolute path, or "" for an empty path.
  """
  path = str(path or "").strip()
  if not path:
    return ""
  if path.startswith("/"):
    return posixpath.normpath(path)
  if not workdir:
    return posixpath.normpath(path)
  return posixpath.normpath(posixpath.join(workdir, path))


def _deliverables(contract: Mapping[str, Any]) -> list[dict[str, Any]]:
  """Normalizes the contract's files into dicts with a path.

  `deliverables` come first, then any `archives` entry whose path is not
  already there: an archive is a graded file too, so it gets the same G1/G3
  checks and its CSV members feed G4. Archive *structure* (missing members,
  absolute or `..` paths, duplicate names) is G2, which is not implemented
  here.

  Args:
    contract: The task contract.

  Returns:
    A list of file dictionaries; a bare string becomes `{"path": ...}`.
  """
  out = []
  seen = set()
  entries = list(contract.get("deliverables") or [])
  for item in contract.get("archives") or []:
    if isinstance(item, str):
      item = {"path": item}
    if isinstance(item, Mapping):
      entries.append(dict(item, kind=item.get("kind") or "zip"))
  for item in entries:
    if isinstance(item, str):
      item = {"path": item}
    if not isinstance(item, Mapping):
      continue
    path = str(item.get("path") or "").strip()
    if not path or path in seen:
      continue
    seen.add(path)
    out.append(
        {
            "path": path,
            "kind": str(item.get("kind") or "").lower(),
            "required": bool(item.get("required", True)),
        }
    )
  return out


class _Report:
  """Collects defects and keeps one fatal per round (SELFCHECK §5.4)."""

  def __init__(self) -> None:
    self.items: list[dict[str, Any]] = []

  def add(
      self,
      gate: str,
      severity: str,
      path: str,
      detail: str,
      rule: str,
      fix: str,
  ) -> None:
    """Adds one defect, demoting every fatal after the first one to `fix`.

    Ringing every bell at once buries the fix that matters, so a round reports
    one fatal; the rest stay visible as `fix` and the next round promotes them
    if they are still there (`TASK-REQUIREMENTS.md` §5.4).

    Args:
      gate: Gate id (`G1`, `G3`, `G4`, `G7`).
      severity: `fatal`, `fix` or `note`.
      path: Deliverable path the defect is about (contract-relative).
      detail: What was found, with the exact path/column/row.
      rule: The contract statement the finding violates.
      fix: One concrete corrective action.
    """
    if severity not in SEVERITY_ORDER:
      severity = "note"
    if severity == "fatal" and any(
        item["severity"] == "fatal" for item in self.items
    ):
      severity = "fix"
    self.items.append(
        {
            "gate": gate,
            "status": "fail",
            "severity": severity,
            "path": path,
            "detail": detail,
            "rule": rule,
            "fix": fix,
        }
    )

  def sorted(self) -> list[dict[str, Any]]:
    """Returns the defects ordered fatal -> fix -> note, then by gate.

    Returns:
      The defect list in feedback order.
    """
    return sorted(
        self.items,
        key=lambda item: (
            SEVERITY_ORDER.index(item["severity"]),
            _GATES.index(item["gate"]) if item["gate"] in _GATES else 99,
            item["path"],
            item["detail"],
        ),
    )


def _check_g1(
    report: _Report,
    deliverables: Sequence[Mapping[str, Any]],
    stats: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
  """G1: existence, regular file, 0 < size <= 95 MiB.

  Args:
    report: Defect collector.
    deliverables: Normalized deliverables.
    stats: Probe `stat` result keyed by absolute path.

  Returns:
    The deliverables that passed G1 and are therefore worth scanning.
  """
  usable = []
  for item in deliverables:
    path = item["_abs"]
    rec = stats.get(path) or {"exists": False}
    required = item["required"]
    missing = "fatal" if required else "note"
    if not rec.get("exists"):
      report.add(
          "G1",
          missing,
          item["path"],
          "%s does not exist in the task container" % path,
          RULE_G1,
          "write the deliverable to this exact path before sending done",
      )
      continue
    if rec.get("islink"):
      report.add(
          "G1",
          missing,
          item["path"],
          "%s is a symbolic link, and symlinks are not graded" % path,
          RULE_G1,
          "replace the link with a real file (cp the target over it)",
      )
      continue
    if rec.get("isdir"):
      report.add(
          "G1",
          missing,
          item["path"],
          "%s is a directory, not a file" % path,
          RULE_G1,
          "write the file at this path (a directory here cannot be graded)",
      )
      continue
    if not rec.get("isfile"):
      report.add(
          "G1",
          missing,
          item["path"],
          "%s is not a regular file" % path,
          RULE_G1,
          "write the deliverable as a plain file",
      )
      continue
    size = int(rec.get("size") or 0)
    if size <= 0:
      report.add(
          "G1",
          "fatal" if required else "note",
          item["path"],
          "%s is 0 bytes (empty)" % path,
          RULE_G1,
          "write the real content: re-run the step that produces it",
      )
      continue
    if size > MAX_DELIVERABLE_BYTES:
      report.add(
          "G1",
          "fatal" if required else "note",
          item["path"],
          "%s is %.1f MiB, over the 95 MiB (%.1f MiB) limit"
          % (path, size / MI_B, MAX_DELIVERABLE_BYTES / MI_B),
          RULE_G1,
          "shrink the deliverable below 95 MiB, or split it into separate files",
      )
      continue
    usable.append(item)
  return usable


def _check_g3(report: _Report, useful: Sequence[Mapping[str, Any]],
              scans: Mapping[str, Mapping[str, Any]]) -> None:
  """G3: per-extension format checks from the probe's `checks` lists.

  Args:
    report: Defect collector.
    useful: Deliverables that passed G1.
    scans: Probe `scan` result keyed by absolute path.
  """
  for item in useful:
    scan = scans.get(item["_abs"]) or {}
    for check in scan.get("checks") or []:
      status = check.get("status")
      if status == "ok":
        continue
      name = str(check.get("name") or "")
      rule, fix = _CHECK_TEXT.get(name, (RULE_G3, "rewrite the file so it parses"))
      severity = {"fail": "fatal", "note": "note"}.get(status, "fix")
      ext = posixpath.splitext(item["path"])[1].lower()
      detail = "%s (%s): %s" % (
          item["_abs"],
          ext.lstrip(".") or "-",
          check.get("detail") or name,
      )
      report.add("G3", severity, item["path"], detail, rule, fix)


def _check_g4(report: _Report, useful: Sequence[Mapping[str, Any]],
              scans: Mapping[str, Mapping[str, Any]],
              contract: Mapping[str, Any]) -> None:
  """G4: vocabulary closure and placeholder scans from the probe.

  Args:
    report: Defect collector.
    useful: Deliverables that passed G1.
    scans: Probe `scan` result keyed by absolute path.
    contract: The task contract (for `vocab` / `numeric_cols` labels).
  """
  vocab = contract.get("vocab") or {}
  numeric = [str(col) for col in (contract.get("numeric_cols") or [])]
  if not vocab and not numeric:
    return
  for item in useful:
    scan = scans.get(item["_abs"]) or {}
    for hit in scan.get("violations") or []:
      kind = hit.get("kind")
      table = hit.get("table") or posixpath.basename(item["_abs"])
      column = hit.get("column") or "?"
      value = hit.get("value")
      count = int(hit.get("count") or 1)
      where = "%s%s row %s: %s = %r" % (
          item["_abs"],
          "" if table == posixpath.basename(item["_abs"]) else " " + table,
          hit.get("row"),
          column,
          value,
      )
      if kind == "vocab":
        detail = "%s is not in the closed vocabulary (%s allowed values)" % (
            where,
            hit.get("allowed"),
        )
        fix = (
            "replace it with one of the allowed %s values from the task"
            " statement" % column
        )
      else:
        detail = "%s is a placeholder in a numeric column" % where
        fix = (
            "write the real number for %s, or leave the cell empty if the"
            " value does not apply" % column
        )
      if count > 1:
        detail += " (%d rows)" % count
      if int(hit.get("distinct") or 1) > 1:
        detail += "; %d distinct offending values in this column" % int(
            hit["distinct"]
        )
      report.add("G4", "fatal", item["path"], detail, RULE_G4, fix)


def _check_g7(
    report: _Report,
    contract: Mapping[str, Any],
    deliverables: Sequence[Mapping[str, Any]],
    final_message: str | None,
) -> None:
  """G7: the final message exists and names every required deliverable.

  Args:
    report: Defect collector.
    contract: The task contract.
    deliverables: Normalized deliverables.
    final_message: The message the harness will send, if already known.
  """
  secondary = contract.get("secondary") or []
  wanted = any(
      isinstance(entry, Mapping)
      and str(entry.get("kind") or "").lower() == "final_message"
      and entry.get("required", True)
      for entry in secondary
  )
  message = final_message
  if message is None:
    message = contract.get("final_message")
  if not wanted and message is None:
    return
  if not isinstance(message, str) or not message.strip():
    report.add(
        "G7",
        "fatal",
        "",
        "no final_message was produced (the done message is empty)",
        RULE_G7,
        "send a done message that lists each deliverable path and what it is",
    )
    return
  text = message.casefold()
  for item in deliverables:
    if not item["required"]:
      continue
    names = {
        item["path"],
        posixpath.basename(item["path"]),
        item["path"].lstrip("./"),
    }
    if any(name and name.casefold() in text for name in names):
      continue
    report.add(
        "G7",
        "fix",
        item["path"],
        "the final_message never mentions %s" % item["path"],
        RULE_G7,
        "add one line to final_message: %s - what it contains"
        % item["path"],
    )


def selfcheck(
    run: Run,
    contract: Mapping[str, Any],
    final_message: str | None = None,
    max_items: int | None = None,
) -> list[dict[str, Any]]:
  """Runs gates G1, G3, G4 and G7 against one task contract.

  Args:
    run: Executor callback `run(cmd) -> (exit_code, output)` that runs `cmd`
      inside the task container. Two probe calls are made: one `stat`, and one
      `scan` for the deliverables that survived G1.
    contract: The task contract (`TASK-REQUIREMENTS.md` §5.2).
    final_message: The message the harness will send, if known; falls back to
      `contract["final_message"]`.
    max_items: Optional cap on the returned list.

  Returns:
    Defects ordered fatal -> fix -> note; empty means every gate is green.
    Each defect is `{"gate", "status", "severity", "path", "detail", "rule",
    "fix"}`. A probe failure (no python3 in the container, clipped output)
    comes back as one `note`, never as an exception.
  """
  contract = contract or {}
  workdir = str(contract.get("workdir") or "")
  deliverables = _deliverables(contract)
  for item in deliverables:
    item["_abs"] = _resolve(workdir, item["path"])
  report = _Report()

  if not deliverables:
    report.add(
        "G1",
        "note",
        "",
        "the contract lists no deliverables, so G1/G3/G4 had nothing to check",
        RULE_G1,
        "fill contract['deliverables'] from the task statement before the run",
    )
    _check_g7(report, contract, deliverables, final_message)
    return _cap(report.sorted(), max_items)

  stats_result = _probe(run, "stat", *[item["_abs"] for item in deliverables])
  if stats_result is None:
    report.add(
        "G1",
        "note",
        "",
        "the container probe did not answer (no python3, or its output was"
        " clipped); G1/G3/G4 status is unknown",
        RULE_G1,
        "keep gone with the deliverables written by tools the task provides",
    )
    _check_g7(report, contract, deliverables, final_message)
    return _cap(report.sorted(), max_items)

  stats = stats_result.get("files") or {}
  usable = _check_g1(report, deliverables, stats)

  scans: Mapping[str, Mapping[str, Any]] = {}
  if usable:
    spec = {
        "paths": [item["_abs"] for item in usable],
        "vocab": contract.get("vocab") or {},
        "numeric_cols": [str(c) for c in (contract.get("numeric_cols") or [])],
        "banned": [
            str(token)
            for token in (
                contract.get("banned_tokens")
                if contract.get("banned_tokens")
                else DEFAULT_BANNED_TOKENS
            )
        ],
        "violation_cap": 4,
    }
    encoded = _encode(spec)
    scan_result = _probe(run, "scan", encoded)
    if scan_result is None:
      report.add(
          "G3",
          "note",
          "",
          "the container probe could not read the deliverables; G3/G4 status"
          " is unknown",
          RULE_G3,
          "re-run SELFCHECK once the container is responsive",
      )
    else:
      scans = scan_result.get("files") or {}
      _check_g3(report, usable, scans)
      _check_g4(report, usable, scans, contract)

  _check_g7(report, contract, deliverables, final_message)
  return _cap(report.sorted(), max_items)


def _encode(spec: Mapping[str, Any]) -> str:
  """Returns the base64 JSON form of a scan spec.

  Args:
    spec: The scan spec dictionary.

  Returns:
    Base64-encoded UTF-8 JSON, safe as one shell argument.
  """
  raw = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
  return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _cap(
    items: Sequence[dict[str, Any]], max_items: int | None
) -> list[dict[str, Any]]:
  """Truncates the defect list to the round budget.

  Args:
    items: Ordered defects.
    max_items: Cap, or None for the §5.4 default.

  Returns:
    At most `max_items` defects (default `MAX_ITEMS`).
  """
  limit = MAX_ITEMS if max_items is None else max(0, int(max_items))
  return list(items[:limit])


def feedback(
    defects: Sequence[Mapping[str, Any]], max_items: int = MAX_ITEMS
) -> dict[str, Any]:
  """Renders defects in the fixed feedback shape of §5.4.

  Args:
    defects: Defects returned by `selfcheck`.
    max_items: Maximum items in the payload.

  Returns:
    `{"gate", "status", "items", "next"}`: the gate of the first item,
    `pass` when there is nothing to report, the ordered items, and one line
    saying what to do next.
  """
  items = [dict(item) for item in defects[: max(0, int(max_items))]]
  if not items:
    return {"gate": "", "status": "pass", "items": [], "next": ""}
  keys = ("gate", "severity", "path", "detail", "rule", "fix")
  payload = [{"status": "fail", **{k: item.get(k, "") for k in keys}}
             for item in items]
  if any(item["severity"] == "fatal" for item in items):
    hint = "fix the fatal item first, then re-run SELFCHECK"
  else:
    hint = "clear the fix items before sending done"
  return {
      "gate": items[0]["gate"],
      "status": "fail",
      "items": payload,
      "next": hint,
  }


def summary(defects: Iterable[Mapping[str, Any]]) -> str:
  """Returns a one-line log summary of a defect list.

  Args:
    defects: Defects returned by `selfcheck`.

  Returns:
    A line such as `SELFCHECK: 1 fatal, 2 fix (G1, G4)` or `SELFCHECK: green`.
  """
  items = list(defects)
  if not items:
    return "SELFCHECK: green"
  counts = {}
  for item in items:
    counts[item["severity"]] = counts.get(item["severity"], 0) + 1
  gates = sorted({str(item["gate"]) for item in items})
  parts = ", ".join(
      "%d %s" % (counts[name], name)
      for name in SEVERITY_ORDER
      if counts.get(name)
  )
  return "SELFCHECK: %s (%s)" % (parts, ", ".join(gates))
