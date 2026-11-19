#!/usr/bin/env python3
"""Unit tests for the deliverable-first stage in `harness.py`.

Covers the four pieces that make the stage load-bearing: the plan budget, the
path parser, the skeleton chooser, and `ensure_deliverables` (it must create
what is missing, leave what exists alone, and never raise).

Run with `python3 test_plan.py`.
"""

from __future__ import annotations

import asyncio
import sys
import types


def _load():
  """Imports `harness` with the websocket protocol module stubbed out.

  Returns:
    The imported `harness` module.
  """
  if "taskbridge" not in sys.modules:
    stub = types.ModuleType("taskbridge")
    stub.Session = object
    stub.Finished = object
    stub.Relay = object
    stub.run_process = None
    stub.time_share = lambda left, share: 0
    sys.modules["taskbridge"] = stub
  import harness  # pylint: disable=import-outside-toplevel
  return harness


H = _load()


class FakeSession:
  """A container that reports `existing` paths and records writes."""

  def __init__(self, left, existing=(), workdir="/app", fail=()):
    self.left = left
    self.existing = set(existing)
    self.workdir = workdir
    self.fail = set(fail)
    self.written = []
    self.commands = []

  def time_left(self):
    """Returns the remaining seconds, or None for an unbounded task."""
    return self.left

  async def exec(self, command, timeout_sec=120):
    """Reports a path as present when `test -s` names one in `existing`."""
    self.commands.append(command)
    for path in self.fail:
      if path in command:
        raise RuntimeError(f"exec refused {path}")
    for path in self.existing:
      if path in command:
        return {"exit_code": 0, "output": "yes\n"}
    return {"exit_code": 1, "output": "no\n"}

  async def write_file(self, path, data, append=False):
    """Records the write and makes the path exist."""
    self.written.append((path, data))
    self.existing.add(path)
    return len(data)


CASES = []


def case(fn):
  """Registers one test case."""
  CASES.append(fn)
  return fn


@case
def case_module_names_are_imported():
  """Every module the stage calls at run time is imported at module level.

  A missing `import time` is invisible to a unit test that never enters
  `solve()`, and it cost two CI runs: the harness raised NameError two seconds
  in. This pins the module objects instead.
  """
  for name in ("time", "posixpath", "re", "asyncio", "os"):
    expect(hasattr(H, name), f"harness.{name} is not imported")
  return "time / posixpath / re / asyncio / os"


@case
def case_plan_budget():
  """The plan gets 10% of the time, clamped, and none when time is short."""
  expect(H.plan_seconds(FakeSession(None)) == float(H.PLAN_MIN_SEC),
         "an unbounded task gets the floor")
  expect(H.plan_seconds(FakeSession(900)) == 90.0, "10% of 900")
  expect(H.plan_seconds(FakeSession(20000)) == float(H.PLAN_MAX_SEC),
         "the cap applies above 12,000 s")
  expect(H.plan_seconds(FakeSession(300)) == 0.0,
         "no plan when the work phase would not fit")
  return "floor / 10% / cap / skip"


@case
def case_plan_paths():
  """The list is parsed out of the plan and de-duplicated."""
  plan = """## 交付物清单
- output/DSR_Ph3_Memo.docx | a signed memo, dated 10/31/2026
- output/response.md | the commit-or-not answer
  - output/DSR_Ph3_Memo.docx | duplicate, ignored
Random prose mentioning a sentence.
"""
  got = H.plan_paths(plan)
  expect(got == ["output/DSR_Ph3_Memo.docx", "output/response.md"],
         "two distinct paths, in order: %r" % (got,))
  expect(H.plan_paths("no files here") == [], "prose invents nothing")
  many = "\\n".join("- output/f%d.md | x" % i for i in range(40))
  expect(len(H.plan_paths(many)) == H.PLAN_MAX_FILES, "the list is capped")
  return "2 paths / none / cap"


@case
def case_skeleton_kinds():
  """Structured deliverables get a stub; prose ones must not."""
  expect(H.skeleton_for("output/a.json")[0] == "text", "json is text")
  expect(H.skeleton_for("output/a.zip")[0] == "zip", "zip is packed")
  expect(H.skeleton_for("output/a.py")[0] == "text", "a script is text")
  expect(H.skeleton_for("output/a.xyz") is None, "unknown has no stub")
  for prose in ("output/memo.md", "output/memo.docx", "output/memo.pdf",
                "output/notes.txt"):
    expect(H.skeleton_for(prose) is None,
           "%s is graded on wording; no placeholder" % prose)
  return "json / zip / py / none / prose refused"


@case
def case_ensures_missing_only():
  """A missing path gets a skeleton; an existing one is left alone."""
  session = FakeSession(900, existing={"/app/output/keep.json"},
                        workdir="/app")
  written = asyncio.run(H.ensure_deliverables(
      session, ["output/keep.json", "output/new.json"]))
  expect(written == ["output/new.json"], "only the missing one: %r" % (written,))
  expect(len(session.written) == 1, "one write")
  expect(session.written[0][0] == "/app/output/new.json",
         "relative paths resolve under the workdir")
  expect(session.written[0][1].strip(), "the skeleton is non-empty")
  return "existing kept, missing written"


@case
def case_plan_target_stays_inside_workdir():
  """A plan path with the workdir spelled in front resolves to one level."""
  expect(H.plan_target("output/a.md", "/app/files") == "/app/files/output/a.md",
         "a relative path joins the workdir")
  expect(H.plan_target("app/files/output/a.md", "/app/files")
         == "/app/files/output/a.md",
         "the workdir spelled in front is stripped once, not doubled")
  expect(H.plan_target("/app/files/output/a.md", "/app/files")
         == "/app/files/output/a.md", "an absolute path is kept")
  expect(H.plan_target("../../etc/passwd", "/app/files")
         == "/app/files/passwd",
         "an escaping path falls back to its basename")
  expect(H.plan_target("app/files", "/app/files") == "/app/files",
         "the workdir itself is not nested")
  return "relative / prefixed / absolute / escaping / self"


@case
def case_zip_skeleton_is_packed():
  """A .zip deliverable is built with the zip binary, not written as text."""
  session = FakeSession(900, workdir="/app")
  asyncio.run(H.ensure_deliverables(session, ["output/submission.zip"]))
  commands = " ".join(session.commands)
  expect("zip -q -r" in commands, "the archive is packed: %s" % commands)
  expect(not session.written, "no text file is written for a zip")
  return "zip -q -r"


@case
def case_skeleton_failure_does_not_raise():
  """A container that refuses a stub is logged, not fatal."""
  session = FakeSession(900, workdir="/app", fail={"output/bad.json"})
  written = asyncio.run(H.ensure_deliverables(
      session, ["output/bad.json", "output/good.json"]))
  expect(written == ["output/good.json"],
         "the good path still gets a stub: %r" % (written,))
  return "one refused, one written"


def expect(condition, message):
  """Raises the assertion message when `condition` is false."""
  if not condition:
    raise AssertionError(message)


def main():
  """Runs every case and reports one line each."""
  failures = 0
  for fn in CASES:
    try:
      detail = fn()
      print("PASS %-40s %s" % (fn.__name__, detail))
    except Exception as exc:  # noqa: BLE001 - report and keep going
      failures += 1
      print("FAIL %-40s %s: %s" % (fn.__name__, type(exc).__name__, exc))
  if failures:
    print("\n%d of %d CASES FAILED" % (failures, len(CASES)))
    return 1
  print("\nALL %d CASES PASSED" % len(CASES))
  return 0


if __name__ == "__main__":
  sys.exit(main())
