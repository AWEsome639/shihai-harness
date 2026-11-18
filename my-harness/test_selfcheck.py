#!/usr/bin/env python3
"""Unit tests for `selfcheck.py` (SELFCHECK v1 gates G1, G3, G4, G7).

The executor callback is stubbed by a local shell: a temporary directory is
filled with the real task files shipped under
`data/tasks/data-hall-mep-routing/environment/filesystem/...`, and the same
`python3 - <op> <<'SELFCHECK_PROBE_EOF' ...` commands `selfcheck.py` would send
to a task container are executed there. So the module is exercised end to end
through `run`, exactly as it will run against a container — it never gets to
open a task file itself.

Run:

    python3 test_selfcheck.py          # prints PASS/FAIL per case
    pytest test_selfcheck.py           # also works

Real fixtures (no task names or answers are baked into `selfcheck.py`):

    REAL_TASK/output/solution.py            the shipped stub script
    data-center-site-model/.../example.ifc  a real ISO-10303-21 file
    landscape-sheet-revision/.../Water_Budget.xlsx  a real workbook
    fire-solve-convergence/.../site_met_avra_valley_2015-2024.csv  a real CSV
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import traceback
import zipfile

import selfcheck

_HERE = os.path.dirname(os.path.abspath(__file__))
PLE = os.path.dirname(_HERE)  # the competition repo clone sits beside this repo
if not os.path.isdir(os.path.join(PLE, "data", "tasks")):
  PLE = "/Users/a0000/.dsh/work/ple"
REAL_TASK = (
    PLE
    + "/data/tasks/data-hall-mep-routing/environment/filesystem"
    "/dc_model__hall_seed000_d000__mep_routing"
)
REAL_IFC = (
    PLE
    + "/data/tasks/data-center-site-model/environment/filesystem"
    "/frontier__DCBim__p6_002/docs/example.ifc"
)
REAL_XLSX = (
    PLE
    + "/data/tasks/landscape-sheet-revision/environment/filesystem"
    "/Reports-Studies/DSR_Ph3_Water_Budget.xlsx"
)
REAL_CSV = (
    PLE
    + "/data/tasks/fire-solve-convergence/environment/files/Projects"
    "/CFDME-2025-041_Ocotillo_WUI/Data/site_met_avra_valley_2015-2024.csv"
)

CASES = []


def case(fn):
  CASES.append((fn.__name__, fn))
  return fn


def expect(condition, message):
  if not condition:
    raise AssertionError(message)


class LocalContainer:
  """A `run` callback that executes probe commands in a local directory.

  Behaves like `Agent.bash` in `serve.py`: the reply is `exit code N` followed
  by the combined output, and every command is recorded for inspection.
  """

  def __init__(self, workdir):
    self.workdir = workdir
    self.commands = []

  def __call__(self, cmd):
    self.commands.append(cmd)
    proc = subprocess.run(
        cmd, shell=True, cwd=self.workdir, capture_output=True
    )
    text = (proc.stdout or b"") + (proc.stderr or b"")
    return proc.returncode, "exit code %d\n%s" % (
        proc.returncode,
        text.decode("utf-8", "replace"),
    )


def dead_container(cmd):
  """A `run` callback for a container with no python3."""
  return 127, "bash: line 1: python3: command not found"


def prepare(work):
  """Copies the real task workspace into `work` and returns it."""
  root = os.path.join(work, "task")
  shutil.copytree(REAL_TASK, root)
  os.makedirs(os.path.join(root, "output"), exist_ok=True)
  return root


def contract(root, deliverables, **extra):
  """Builds a contract for `root` with the given deliverables."""
  base = {
      "task_id": "selfcheck-test",
      "workdir": root,
      "deliverables": deliverables,
      "secondary": [{"kind": "final_message", "required": True}],
  }
  base.update(extra)
  return base


def deliverable(path, kind="", required=True):
  return {"path": path, "kind": kind, "required": required}


def gates(defects):
  return sorted({item["gate"] for item in defects})


def details(defects):
  return " | ".join(item["detail"] for item in defects)


def only(defects, gate, severity=None, contains=()):
  """Asserts the defect list holds exactly one matching defect."""
  expect(len(defects) == 1, "expected 1 defect, got %d: %s"
         % (len(defects), details(defects)))
  item = defects[0]
  expect(item["gate"] == gate, "expected gate %s, got %s" % (gate, item["gate"]))
  if severity is not None:
    expect(item["severity"] == severity, "expected severity %s, got %s"
           % (severity, item["severity"]))
  for needle in contains:
    blob = item["detail"] + " " + item["fix"] + " " + item["rule"]
    expect(needle in blob, "expected %r in the defect, got %r"
           % (needle, item["detail"]))
  return item


def write(root, path, data):
  """Writes bytes or text under `root` and returns the local path."""
  full = os.path.join(root, path)
  os.makedirs(os.path.dirname(full), exist_ok=True)
  mode = "wb" if isinstance(data, bytes) else "w"
  with open(full, mode) as handle:
    handle.write(data)
  return full


# ---------------------------------------------------------------- scenarios


@case
def green_all_gates_no_defects(work):
  """Every deliverable real and parseable: no defect at all."""
  root = prepare(work)
  shutil.copyfile(REAL_IFC, os.path.join(root, "output", "model.ifc"))
  shutil.copyfile(REAL_XLSX, os.path.join(root, "output", "report.xlsx"))
  shutil.copyfile(REAL_CSV, os.path.join(root, "output", "readings.csv"))
  run = LocalContainer(root)
  plan = contract(
      root,
      [
          deliverable("output/solution.py", "py"),
          deliverable("output/model.ifc", "ifc"),
          deliverable("output/report.xlsx", "xlsx"),
          deliverable("output/readings.csv", "csv"),
      ],
  )
  message = (
      "Wrote output/solution.py (the builder), output/model.ifc (IFC4 model),"
      " output/report.xlsx (workbook) and output/readings.csv (readings)."
  )
  defects = selfcheck.selfcheck(run, plan, final_message=message)
  expect(defects == [], "expected a green run, got: %s" % details(defects))
  expect(selfcheck.summary(defects) == "SELFCHECK: green", "green summary")
  expect(len(run.commands) == 2, "expected 2 probe execs (stat + scan), got %d"
         % len(run.commands))
  expect("python3 - stat" in run.commands[0], "first exec should be a stat")
  expect("python3 - scan" in run.commands[1], "second exec should be a scan")
  return defects


@case
def missing_deliverable_is_fatal(work):
  """G1: the exact path does not exist."""
  root = prepare(work)
  os.remove(os.path.join(root, "output", "solution.py"))
  run = LocalContainer(root)
  plan = contract(root, [deliverable("output/solution.py", "py")])
  defects = selfcheck.selfcheck(
      run, plan, final_message="Wrote output/solution.py."
  )
  only(defects, "G1", "fatal",
       contains=("does not exist", "output/solution.py"))
  return defects


@case
def directory_instead_of_file_is_fatal(work):
  """G1: the path is a directory, not a regular file."""
  root = prepare(work)
  target = os.path.join(root, "output", "solution.py")
  os.remove(target)
  os.makedirs(target)
  run = LocalContainer(root)
  plan = contract(root, [deliverable("output/solution.py", "py")])
  defects = selfcheck.selfcheck(
      run, plan, final_message="Wrote output/solution.py."
  )
  only(defects, "G1", "fatal", contains=("is a directory", "solution.py"))
  return defects


@case
def symlink_deliverable_is_fatal(work):
  """G1: symlinks are not graded even when they resolve."""
  root = prepare(work)
  target = os.path.join(root, "output", "solution.py")
  real = os.path.join(root, "output", "real_builder.py")
  os.rename(target, real)
  os.symlink("real_builder.py", target)
  run = LocalContainer(root)
  plan = contract(root, [deliverable("output/solution.py", "py")])
  defects = selfcheck.selfcheck(
      run, plan, final_message="Wrote output/solution.py."
  )
  only(defects, "G1", "fatal", contains=("symbolic link",))
  return defects


@case
def empty_file_is_fatal(work):
  """G1: a 0-byte deliverable is not gradable."""
  root = prepare(work)
  open(os.path.join(root, "output", "solution.py"), "w").close()
  run = LocalContainer(root)
  plan = contract(root, [deliverable("output/solution.py", "py")])
  defects = selfcheck.selfcheck(
      run, plan, final_message="Wrote output/solution.py."
  )
  only(defects, "G1", "fatal", contains=("0 bytes",))
  return defects


@case
def oversized_file_is_fatal(work):
  """G1: anything over 95 MiB scores zero, so it is a fatal finding."""
  root = prepare(work)
  target = os.path.join(root, "output", "solution.py")
  with open(target, "wb") as handle:
    handle.truncate(selfcheck.MAX_DELIVERABLE_BYTES + 1)
  run = LocalContainer(root)
  plan = contract(root, [deliverable("output/solution.py", "py")])
  defects = selfcheck.selfcheck(
      run, plan, final_message="Wrote output/solution.py."
  )
  only(defects, "G1", "fatal", contains=("95 MiB", "over the 95 MiB"))
  return defects


@case
def csv_header_missing_is_fatal(work):
  """G3: the first row is blank, so the CSV has no header."""
  root = prepare(work)
  write(root, "output/plan.csv", "\nstage,flow_gpm\nHYDRO,120\n")
  run = LocalContainer(root)
  plan = contract(
      root,
      [deliverable("output/solution.py", "py"), deliverable("output/plan.csv", "csv")],
      numeric_cols=["flow_gpm"],
  )
  defects = selfcheck.selfcheck(
      run,
      plan,
      final_message="Wrote output/solution.py and output/plan.csv.",
  )
  only(defects, "G3", "fatal", contains=("no header", "plan.csv"))
  return defects


@case
def placeholder_in_numeric_column_is_fatal(work):
  """G4: TBD in a numeric column invalidates the package."""
  root = prepare(work)
  write(
      root,
      "output/plan.csv",
      "stage,flow_gpm,velocity_fps\n"
      "HYDRO,120,5.0\n"
      "INITIAL_FLUSH,TBD,4.0\n"
      "CHEM_CLEAN,90,4.5\n",
  )
  run = LocalContainer(root)
  plan = contract(
      root,
      [deliverable("output/solution.py", "py"), deliverable("output/plan.csv", "csv")],
      vocab={"stage": ["HYDRO", "INITIAL_FLUSH", "CHEM_CLEAN"]},
      numeric_cols=["flow_gpm"],
  )
  defects = selfcheck.selfcheck(
      run,
      plan,
      final_message="Wrote output/solution.py and output/plan.csv.",
  )
  item = only(defects, "G4", "fatal",
              contains=("TBD", "flow_gpm", "row 3", "placeholder"))
  expect("plan.csv" in item["detail"], "detail should name the table")
  expect(item["fix"], "a fix line is required")
  return defects


@case
def value_outside_vocabulary_is_fatal(work):
  """G4: an enumerated cell outside the closed vocabulary."""
  root = prepare(work)
  write(
      root,
      "output/plan.csv",
      "stage,flow_gpm\nHYDRO,120\nFUSH,90\n",
  )
  run = LocalContainer(root)
  plan = contract(
      root,
      [deliverable("output/solution.py", "py"), deliverable("output/plan.csv", "csv")],
      vocab={"stage": ["HYDRO", "INITIAL_FLUSH", "CHEM_CLEAN"]},
  )
  defects = selfcheck.selfcheck(
      run,
      plan,
      final_message="Wrote output/solution.py and output/plan.csv.",
  )
  only(defects, "G4", "fatal",
       contains=("FUSH", "not in the closed vocabulary", "stage"))
  return defects


@case
def vocabulary_can_be_scoped_to_one_file(work):
  """G4: `"plan.csv:stage"` only checks that one table's column."""
  root = prepare(work)
  write(root, "output/keep.csv", "stage,flow_gpm\nHYDRO,120\n")
  write(root, "output/plan.csv", "stage,flow_gpm\nFUSH,120\n")
  run = LocalContainer(root)
  plan = contract(
      root,
      [
          deliverable("output/solution.py", "py"),
          deliverable("output/keep.csv", "csv"),
          deliverable("output/plan.csv", "csv"),
      ],
      vocab={"plan.csv:stage": ["HYDRO", "INITIAL_FLUSH"]},
  )
  defects = selfcheck.selfcheck(
      run,
      plan,
      final_message=(
          "Wrote output/solution.py, output/keep.csv and output/plan.csv."
      ),
  )
  item = only(defects, "G4", "fatal", contains=("FUSH", "stage"))
  expect("plan.csv" in item["detail"], "the scoped file is reported")
  expect("keep.csv" not in item["detail"], "keep.csv must not be scanned")
  return defects


@case
def placeholders_inside_zip_members_are_fatal(work):
  """G4 also scans CSV members of a zip deliverable."""
  root = prepare(work)
  archive = os.path.join(root, "output", "submission.zip")
  with zipfile.ZipFile(archive, "w") as zf:
    zf.writestr("plan.csv", "stage,flow_gpm\nHYDRO,120\nCHEM_CLEAN,TBA\n")
    zf.writestr("MOP.md", "# Method of procedure\n")
  run = LocalContainer(root)
  plan = contract(
      root,
      [deliverable("output/submission.zip", "zip")],
      numeric_cols=["flow_gpm"],
  )
  defects = selfcheck.selfcheck(
      run, plan, final_message="Wrote output/submission.zip."
  )
  item = only(defects, "G4", "fatal", contains=("TBA", "plan.csv", "row 3"))
  expect("submission.zip" in item["detail"], "detail should name the archive")
  return defects


@case
def archive_listed_only_under_archives_is_still_scanned(work):
  """G4 reaches a zip declared in `archives` but not in `deliverables`."""
  root = prepare(work)
  with zipfile.ZipFile(os.path.join(root, "output", "submission.zip"), "w") as zf:
    zf.writestr("plan.csv", "stage,flow_gpm\nHYDRO,120\nFINAL_FILL,TBD\n")
  run = LocalContainer(root)
  plan = contract(
      root,
      [deliverable("output/solution.py", "py")],
      archives=[{"path": "output/submission.zip", "members": ["plan.csv"]}],
      numeric_cols=["flow_gpm"],
  )
  defects = selfcheck.selfcheck(
      run,
      plan,
      final_message="Wrote output/solution.py and output/submission.zip.",
  )
  item = only(defects, "G4", "fatal", contains=("TBD", "plan.csv"))
  expect("submission.zip" in item["detail"], "the archive is the reported file")
  return defects


@case
def ifc_without_step_header_is_fatal(work):
  """G3: a .ifc deliverable must start with the ISO-10303-21 line."""
  root = prepare(work)
  write(root, "output/model.ifc", "not a step file\nDATA;\n")
  run = LocalContainer(root)
  plan = contract(
      root,
      [deliverable("output/solution.py", "py"), deliverable("output/model.ifc", "ifc")],
  )
  defects = selfcheck.selfcheck(
      run, plan, final_message="Wrote output/solution.py and output/model.ifc."
  )
  only(defects, "G3", "fatal", contains=("ISO-10303-21", "model.ifc"))
  return defects


@case
def broken_json_and_binary_markdown_are_fatal(work):
  """G3: the .json must load, the .md must be readable UTF-8 text."""
  root = prepare(work)
  write(root, "output/result.json", "{not json")
  write(root, "output/notes.md", b"\xff\xfe\x00\x01binary garbage")
  run = LocalContainer(root)
  plan = contract(
      root,
      [
          deliverable("output/solution.py", "py"),
          deliverable("output/result.json", "json"),
          deliverable("output/notes.md", "md"),
      ],
  )
  defects = selfcheck.selfcheck(
      run,
      plan,
      final_message=(
          "Wrote output/solution.py, output/result.json and output/notes.md."
      ),
  )
  expect(gates(defects) == ["G3"], "expected only G3, got %s" % gates(defects))
  text = details(defects)
  expect("result.json" in text, "json defect missing: %s" % text)
  expect("notes.md" in text, "markdown defect missing: %s" % text)
  expect(len(defects) == 2, "expected 2 G3 defects, got %d" % len(defects))
  return defects


@case
def final_message_missing_is_fatal(work):
  """G7: no final message at all."""
  root = prepare(work)
  run = LocalContainer(root)
  plan = contract(root, [deliverable("output/solution.py", "py")])
  defects = selfcheck.selfcheck(run, plan, final_message=None)
  only(defects, "G7", "fatal", contains=("final_message", "empty"))
  return defects


@case
def final_message_must_name_every_deliverable(work):
  """G7: a message that never mentions four of the five deliverables."""
  root = prepare(work)
  shutil.copyfile(REAL_IFC, os.path.join(root, "output", "a.ifc"))
  shutil.copyfile(REAL_XLSX, os.path.join(root, "output", "b.xlsx"))
  shutil.copyfile(REAL_CSV, os.path.join(root, "output", "c.csv"))
  write(root, "output/d.json", '{"rows": 3}\n')
  run = LocalContainer(root)
  plan = contract(
      root,
      [
          deliverable("output/solution.py", "py"),
          deliverable("output/a.ifc", "ifc"),
          deliverable("output/b.xlsx", "xlsx"),
          deliverable("output/c.csv", "csv"),
          deliverable("output/d.json", "json"),
      ],
  )
  defects = selfcheck.selfcheck(
      run, plan, final_message="Wrote output/solution.py."
  )
  expect(gates(defects) == ["G7"], "expected only G7, got %s" % gates(defects))
  expect(len(defects) == 4, "expected 4 uncovered deliverables, got %d"
         % len(defects))
  for item in defects:
    expect(item["severity"] == "fix", "coverage gaps are fix, not fatal")
    expect(item["path"] != "output/solution.py", "solution.py is covered")
  return defects


@case
def optional_deliverable_missing_is_a_note(work):
  """A conditional deliverable that is absent must not raise a fatal."""
  root = prepare(work)
  run = LocalContainer(root)
  plan = contract(
      root,
      [
          deliverable("output/solution.py", "py"),
          deliverable("output/reconciliation.json", "json", required=False),
      ],
  )
  defects = selfcheck.selfcheck(
      run, plan, final_message="Wrote output/solution.py."
  )
  only(defects, "G1", "note", contains=("reconciliation.json",))
  return defects


@case
def dead_container_yields_one_note(work):
  """No python3 in the container: note, never an exception or a fake pass."""
  root = prepare(work)
  plan = contract(root, [deliverable("output/solution.py", "py")])
  defects = selfcheck.selfcheck(
      dead_container, plan, final_message="Wrote output/solution.py."
  )
  only(defects, "G1", "note", contains=("probe did not answer",))
  return defects


@case
def feedback_payload_follows_the_fixed_shape(work):
  """()` renders at most one fatal first, then fix/note, as §5.4."""
  root = prepare(work)
  write(
      root,
      "output/plan.csv",
      "stage,flow_gpm\nHYDRO,TBD\nFUSH,TBA\n",
  )
  run = LocalContainer(root)
  plan = contract(
      root,
      [deliverable("output/solution.py", "py"), deliverable("output/plan.csv", "csv")],
      vocab={"stage": ["HYDRO", "INITIAL_FLUSH"]},
      numeric_cols=["flow_gpm"],
  )
  defects = selfcheck.selfcheck(
      run, plan, final_message="Wrote output/solution.py and output/plan.csv."
  )
  expect(len(defects) >= 2, "expected several findings, got %s" % details(defects))
  payload = selfcheck.feedback(defects)
  expect(payload["status"] == "fail", "status should be fail")
  expect(payload["next"], "a next hint is required")
  expect(len(payload["items"]) <= selfcheck.MAX_ITEMS, "payload is capped")
  for item in payload["items"]:
    for key in ("gate", "severity", "path", "detail", "rule", "fix"):
      expect(item.get(key) not in (None, ""), "item is missing %r" % key)
  fatals = [i for i in payload["items"] if i["severity"] == "fatal"]
  expect(len(fatals) == 1, "one fatal per round (§5.4), got %d" % len(fatals))
  expect(payload["items"][0]["severity"] == "fatal", "fatal comes first")
  expect(selfcheck.feedback([]) == {
      "gate": "", "status": "pass", "items": [], "next": ""
  }, "a green run renders an empty pass payload")
  return defects


@case
def module_is_pure_and_task_agnostic(work):
  """The policy layer touches no filesystem; no task fingerprint leaks in."""
  source = open(os.path.join(os.path.dirname(__file__), "selfcheck.py")).read()
  head = source.split("PROBE_SOURCE = r'''")[0]
  tail = source.split("'''", 2)[-1] if "PROBE_SOURCE = r'''" in source else ""
  for chunk, name in ((head, "policy layer"), (tail, "after the probe")):
    for banned in ("open(", "import os", "pathlib", "glob.", "os.path"):
      expect(banned not in chunk, "%s must not use %r" % (name, banned))
  expect("def selfcheck(" in source, "selfcheck() must stay exported")
  for slug in (
      "data-hall",
      "cooling-water",
      "flush_plan",
      "Halbray",
      "Callaway",
      "spillway",
      "frontier__DCBim",
  ):
    expect(slug not in source, "task fingerprint %r leaked into selfcheck.py"
           % slug)
  return []


def validate(defects):
  """Checks every defect has the documented shape."""
  for item in defects:
    expect(isinstance(item, dict), "defect must be a dict")
    for key in ("gate", "status", "severity", "path", "detail", "rule", "fix"):
      expect(key in item, "defect is missing %r: %r" % (key, item))
    expect(item["gate"] in ("G1", "G3", "G4", "G7"),
           "unexpected gate %r" % item["gate"])
    expect(item["severity"] in selfcheck.SEVERITY_ORDER,
           "unexpected severity %r" % item["severity"])
    expect(item["status"] == "fail", "status must be fail")
    expect(isinstance(item["detail"], str) and item["detail"],
           "detail must be a non-empty string")


@case
def docx_without_a_body_is_fatal(work):
    """G3 rejects a .docx that is a zip without word/document.xml."""
    root = prepare(work)
    with zipfile.ZipFile(os.path.join(root, "output", "report.docx"), "w") as zf:
        zf.writestr("foo.txt", "not a body\n")
    run = LocalContainer(root)
    plan = contract(root, [deliverable("output/report.docx", "docx")])
    defects = selfcheck.selfcheck(
        run, plan, final_message="Wrote output/report.docx."
    )
    item = only(defects, "G3", "fatal", contains=("word/document.xml",))
    expect("report.docx" in item["detail"], "detail should name the document")
    return defects


@case
def pdf_without_a_trailer_is_fatal(work):
    """G3 rejects a .pdf that has a header but no %%EOF trailer."""
    root = prepare(work)
    path = os.path.join(root, "output", "memo.pdf")
    with open(path, "wb") as fh:
        fh.write(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n")
    run = LocalContainer(root)
    plan = contract(root, [deliverable("output/memo.pdf", "pdf")])
    defects = selfcheck.selfcheck(
        run, plan, final_message="Wrote output/memo.pdf."
    )
    item = only(defects, "G3", "fatal", contains=("%%EOF",))
    expect("memo.pdf" in item["detail"], "detail should name the PDF")
    return defects


def main():
  failures = 0
  print("selfcheck.py: %d bytes, probe %d bytes; task data under %s"
        % (len(open(selfcheck.__file__).read()),
           len(selfcheck.PROBE_SOURCE), PLE))
  for name, fn in CASES:
    work = tempfile.mkdtemp(prefix="selfcheck-%s-" % name[:24])
    try:
      defects = fn(work)
      validate(defects)
      print("PASS %-44s %s" % (name, selfcheck.summary(defects)))
    except Exception as exc:  # noqa: BLE001 - report and keep going
      failures += 1
      print("FAIL %-44s %s: %s" % (name, type(exc).__name__, exc))
      traceback.print_exc(limit=3)
    finally:
      shutil.rmtree(work, ignore_errors=True)
  total = len(CASES)
  if failures:
    print("\n%d of %d CASES FAILED" % (failures, total))
    return 1
  print("\nALL %d CASES PASSED" % total)
  return 0


def test_selfcheck_suite():
  """pytest entry point: the command line suite must pass."""
  assert main() == 0


if __name__ == "__main__":
  sys.exit(main())
