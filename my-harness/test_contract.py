#!/usr/bin/env python3
"""Unit tests for `contract.py` (G0: instruction -> contract.json).

The hard case is the real one: the 29 dev task cards in
`../task_cards.jsonl`, each with the `instruction` the harness is handed and the
`deliverables` the grader reads. For every card, `extract(instruction, card)`
must carry **every** declared deliverable — a missing path is the one G0
defect that no later gate can recover from (`TASK-REQUIREMENTS.md` §5.3).

The second case is what G0 is actually for: the same 29 instructions with the
card withheld, where the prose alone has to produce the file paths. It is
reported, not asserted task by task — two instructions only say "put the files
under `output/`" — but a task whose card names a file and whose prose does not
is a regression, so those are asserted.

Run:

    python3 test_contract.py          # prints PASS/FAIL per case
    pytest test_contract.py           # also works

Nothing here opens a task directory, a test or a reference answer: the input
is `task_cards.jsonl` (instruction text plus public metadata).
"""

from __future__ import annotations

import json
import os
import posixpath
import sys
import traceback


# The graded harness lives at the repository root; `my-harness/` keeps copies of
# these modules for the alternate entry point. Import the root one explicitly and
# fail loudly otherwise - a stale copy once made "20/20 PASS" measure the wrong file.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import contract

_HERE = os.path.dirname(os.path.abspath(__file__))
CARDS = os.path.join(os.path.dirname(_HERE), "task_cards.jsonl")
if not os.path.isfile(CARDS):
  CARDS = "/Users/a0000/.dsh/work/ple/task_cards.jsonl"


def expect(condition, message):
  """Asserts and re-raises with the message the case is about.

  Args:
    condition: The assertion.
    message: What the failing assertion means.
  """
  if not condition:
    raise AssertionError(message)


def cards() -> list[dict]:
  """Loads the dev task cards.

  Returns:
    The 29 `task_cards.jsonl` rows, in file order.
  """
  expect(os.path.isfile(CARDS), "task_cards.jsonl not found at %s" % CARDS)
  with open(CARDS, encoding="utf-8") as handle:
    return [json.loads(line) for line in handle if line.strip()]


def validate(body: dict) -> None:
  """Checks the contract shape `selfcheck.py` consumes.

  Args:
    body: The contract returned by `contract.extract`.
  """
  expect(isinstance(body, dict), "contract must be a dict")
  expect(isinstance(body.get("deliverables"), list), "deliverables must be a list")
  allowed = set(contract.KINDS.values()) | {""}
  seen = set()
  for item in body["deliverables"]:
    expect(isinstance(item, dict), "deliverable must be a dict: %r" % (item,))
    for key in ("path", "kind", "required"):
      expect(key in item, "deliverable is missing %r: %r" % (key, item))
    expect(isinstance(item["path"], str) and item["path"], "empty path")
    expect(item["path"] not in seen, "duplicate path %r" % item["path"])
    seen.add(item["path"])
    expect(item["kind"] in allowed, "unknown kind %r" % item["kind"])
    expect(item["kind"] == contract.kind_of(item["path"]),
           "kind %r does not match extension of %r" % (item["kind"], item["path"]))
    expect(isinstance(item["required"], bool), "required must be a bool")
  expect(body.get("banned_tokens") == list(contract.BANNED_TOKENS),
         "banned_tokens must be the frozen placeholder list")
  for archive in body.get("archives") or []:
    expect(archive["kind"] if "kind" in archive else "zip", "archive kind")
    expect(archive["path"].endswith(".zip"), "archive path must be a zip")
    for member in archive.get("members") or []:
      expect("/" not in member, "member must be a bare name: %r" % member)
  for key in ("vocab", "units"):
    expect(not body.get(key) or isinstance(body[key], dict),
           "%s must be a dict" % key)
  expect(not body.get("numeric_cols") or isinstance(body["numeric_cols"], list),
         "numeric_cols must be a list")


def file_deliverables(row: dict) -> list[str]:
  """Returns the card deliverables that name a real file, not a directory.

  Args:
    row: One task card.

  Returns:
    The paths whose extension this module knows.
  """
  return [path for path in row["deliverables"] if contract.kind_of(path)]


def case_real_cards() -> str:
  """G0 hard constraint: no card deliverable may be dropped.

  Returns:
    A one-line summary for the runner.
  """
  rows = cards()
  expect(len(rows) == 29, "expected 29 dev cards, found %d" % len(rows))
  missing = []
  extras = 0
  for row in rows:
    body = contract.extract(row["instruction"], row)
    validate(body)
    paths = [item["path"] for item in body["deliverables"]]
    for path in row["deliverables"]:
      if path not in paths:
        missing.append("%s: %s" % (row["task_id"], path))
    extras += len([path for path in paths if path not in row["deliverables"]])
    if row.get("final_message"):
      expect(body.get("secondary") == [{"kind": "final_message", "required": True}],
             "%s: final_message must be a secondary deliverable" % row["task_id"])
    if any(contract.kind_of(path) == "zip" for path in row["deliverables"]):
      expect(body.get("archives"), "%s: missing archives entry" % row["task_id"])
  expect(not missing, "dropped deliverables:\n  " + "\n  ".join(missing))
  return "%d/%d cards keep every deliverable (%d prose additions)" % (
      len(rows), len(rows), extras)


def case_instruction_only() -> str:
  """G0 without the card: the prose alone must name the files it grades.

  Returns:
    A one-line summary for the runner.
  """
  rows = cards()
  file_tasks = 0
  complete = 0
  directory_tasks = 0
  directory_with_path = 0
  missed = []
  for row in rows:
    body = contract.extract(row["instruction"], None)
    validate(body)
    got = [item["path"] for item in body["deliverables"]]
    bases = {posixpath.basename(path) for path in got}
    relatives = {contract._relative(path) for path in got}
    wanted = file_deliverables(row)
    if not wanted:
      directory_tasks += 1
      directory_with_path += bool(got)
      continue
    file_tasks += 1
    ok = all(
        posixpath.basename(path) in bases or contract._relative(path) in relatives
        for path in wanted
    )
    complete += ok
    if not ok:
      missed.append(row["task_id"])
  expect(not missed, "prose lost a graded file for: %s" % ", ".join(missed))
  return (
      "%d/%d file-level cards recovered from prose; %d/%d directory-level "
      "cards still yield a concrete path (%d/%d overall)"
      % (complete, file_tasks, directory_with_path, directory_tasks,
         complete + directory_with_path, len(rows))
  )


def case_plain_text() -> str:
  """A statement with no path invents nothing.

  Returns:
    A one-line summary for the runner.
  """
  body = contract.extract(
      "You are the commissioning engineer. Read the documents and answer the"
      " questions in a memo for the owner's signature."
  )
  validate(body)
  expect(body["deliverables"] == [], "a path was invented: %r" % body["deliverables"])
  for key in ("archives", "vocab", "numeric_cols", "units", "runtime_limit_s"):
    expect(key not in body, "%r should be absent, not guessed" % key)
  return "empty instruction yields no deliverables and no guessed keys"


def case_path_with_spaces() -> str:
  """A file name with spaces survives the prose reader.

  Returns:
    A one-line summary for the runner.
  """
  body = contract.extract(
      "Save it as Final Report v2.docx and hand it back.",
      {"task_id": "t", "workdir": "/filesystem"},
  )
  validate(body)
  paths = [item["path"] for item in body["deliverables"]]
  expect(paths == ["Final Report v2.docx"], "unexpected paths: %r" % paths)
  expect(body["deliverables"][0]["kind"] == "docx", "kind must come from the extension")
  expect(body["deliverables"][0]["required"] is True, "a stated path is required")
  second = contract.extract("Return your work as a memo named Memo to Callaway 2026.docx.")
  expect([item["path"] for item in second["deliverables"]] == ["Memo to Callaway 2026.docx"],
         "multi-word name not read: %r" % second["deliverables"])
  return "spaces and connectors survive: %r" % paths


def case_one_of_vocab() -> str:
  """A "one of a, b, c" enumeration becomes a closed vocabulary.

  Returns:
    A one-line summary for the runner.
  """
  body = contract.extract(
      "Every row sets `discharge_route` is one of `SEWER`, `HAUL`, `NONE`, and"
      " the stage tokens are fixed.",
      {"task_id": "t"},
  )
  validate(body)
  expect(body.get("vocab") == {"discharge_route": ["SEWER", "HAUL", "NONE"]},
         "vocabulary not read: %r" % body.get("vocab"))
  piped = contract.extract('"foundation_decision": "SPREAD|IMPROVED_SPREAD"')
  expect(piped.get("vocab") == {
      "foundation_decision": ["SPREAD", "IMPROVED_SPREAD"]
  }, "pipe enumeration not read: %r" % piped.get("vocab"))
  return "one-of and A|B|C enums both become vocab"


def case_archive_members() -> str:
  """Archive members are read only when the stated count agrees.

  Returns:
    A one-line summary for the runner.
  """
  rows = {row["task_id"]: row for row in cards()}
  flush = contract.extract(
      rows["cooling-water-flush-plan"]["instruction"],
      rows["cooling-water-flush-plan"],
  )
  members = flush["archives"][0].get("members") or []
  expect(len(members) == 7, "flush plan states seven files, read %d" % len(members))
  expect("flush_plan.csv" in members and "MOP.md" in members,
         "members do not name the graded files: %r" % members)
  takeoff = contract.extract(
      rows["data-hall-device-takeoff"]["instruction"],
      rows["data-hall-device-takeoff"],
  )
  expect("members" not in takeoff["archives"][0],
         "an unconfirmed member list must be omitted, not guessed")
  return "7/7 flush members read; an unstated list is omitted"


def case_card_wins() -> str:
  """A relative spelling never becomes a second deliverable beside the card's.

  Returns:
    A one-line summary for the runner.
  """
  card = {
      "task_id": "t",
      "workdir": "/filesystem",
      "deliverables": ["/filesystem/final_batch__blind/output/submission.zip"],
      "input_files": [{"path": "/filesystem/project/task.md"}],
  }
  body = contract.extract(
      "You are working in `/filesystem/final_batch__blind`. Write the final"
      " deliverable to `/filesystem/final_batch__blind/output/submission.zip`."
      " Write `output/submission.zip` containing `plan.csv` and `log.md`.",
      card,
  )
  validate(body)
  paths = [item["path"] for item in body["deliverables"]]
  expect(paths == card["deliverables"],
         "the card path must win over the prose twin: %r" % paths)
  return "one zip path, no relative twin, members kept off the file list"


CASES = (
    ("real-cards: no deliverable dropped", case_real_cards),
    ("prose-only: graded files recovered", case_instruction_only),
    ("synthetic: plain text invents nothing", case_plain_text),
    ("synthetic: path with spaces", case_path_with_spaces),
    ("synthetic: one-of vocabulary", case_one_of_vocab),
    ("real-card: archive members", case_archive_members),
    ("real-card: card path wins", case_card_wins),
)


def main():
  failures = 0
  print("contract.py: %d bytes, %d cards under %s"
        % (len(open(contract.__file__).read()), len(cards()), CARDS))
  for name, fn in CASES:
    try:
      detail = fn()
      print("PASS %-44s %s" % (name, detail))
    except Exception as exc:  # noqa: BLE001 - report and keep going
      failures += 1
      print("FAIL %-44s %s: %s" % (name, type(exc).__name__, exc))
      traceback.print_exc(limit=3)
  if failures:
    print("\n%d of %d CASES FAILED" % (failures, len(CASES)))
    return 1
  print("\nALL %d CASES PASSED" % len(CASES))
  return 0


def test_contract_suite():
  """pytest entry point: the command line suite must pass."""
  assert main() == 0


if __name__ == "__main__":
  sys.exit(main())
