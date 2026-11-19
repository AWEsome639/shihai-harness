#!/usr/bin/env python3
"""Unit tests for the verify -> repair stage in `harness.py`.

The stage is model-driven, so these tests drive it with a fake `run_pi` and a
fake session: they pin the control flow (skip when there is no time, PASS ends
it, gaps buy exactly one repair session, a crashed reviewer changes nothing)
without calling a model or a container.

Run with `python3 test_review.py`.
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
  """The three session attributes the stage reads."""

  def __init__(self, left, instruction="TASK TEXT", final="WORKER SUMMARY"):
    self.left = left
    self.instruction = instruction
    self.final_message = final
    self.workdir = "/app"

  def time_left(self):
    """Returns the remaining seconds, or None for an unbounded task."""
    return self.left


class FakeRun:
  """What `run_pi` returns: the run tracker."""

  def __init__(self, last, replies=1):
    self.last = last
    self.replies = replies


def runner(replies):
  """Builds a `run_pi` that replays `replies` and records its calls."""
  calls = []

  async def run_pi(text, note, stop_before, max_sec=None):
    calls.append({"text": text, "note": note, "stop_before": stop_before,
                  "max_sec": max_sec})
    return FakeRun(replies[len(calls) - 1]), None

  return run_pi, calls


CASES = []


def case(fn):
  """Registers one test case."""
  CASES.append(fn)
  return fn


@case
def case_gaps_parse():
  """A verdict is PASS, or one line per gap with markers stripped."""
  gaps = H.review_gaps
  expect(gaps("PASS") == [], "PASS is not a gap")
  expect(gaps(" pass. ") == [], "a decorated PASS is not a gap")
  expect(gaps("") == [], "an empty verdict is not a gap")
  got = gaps("1. out/a.docx | missing section 4 | add it\n- out/b.pdf | no trailer | rebuild")
  expect(len(got) == 2, "two lines are two gaps: %r" % (got,))
  expect(got[0].startswith("out/a.docx"), "numbering is stripped: %r" % (got[0],))
  expect(got[1].startswith("out/b.pdf"), "bullets are stripped: %r" % (got[1],))
  many = gaps("\n".join("out/x%d | g | f" % i for i in range(50)))
  expect(len(many) == H.REVIEW_MAX_GAPS, "the list is capped: %d" % len(many))
  return "PASS / empty / 2 lines / cap"


@case
def case_budget_uses_only_spare_time():
  """The review budget is the worker's leftover time, never its working time."""
  expect(H.review_budget(FakeSession(None)) == float(H.REVIEW_MAX_SEC),
         "an unbounded task gets the cap")
  expect(H.review_budget(FakeSession(900)) == float(H.REVIEW_MAX_SEC),
         "900 s leaves more than the cap")
  # 150 s left, 90 s wrap-up reserve and 30 s slack: 30 s of spare, too little.
  expect(H.review_budget(FakeSession(150)) == 30.0, "spare time only")
  expect(H.review_budget(FakeSession(100)) == 0.0, "no spare time is zero")
  return "unbounded / 900 s / 150 s / 100 s"


@case
def case_skip_when_no_time():
  """With no spare time the stage runs nothing and keeps the answer."""
  run_pi, calls = runner([])
  session = FakeSession(120)
  out = asyncio.run(H.verify_and_repair(session, run_pi, "ANSWER"))
  expect(out == "ANSWER", "the answer is unchanged")
  expect(calls == [], "no session is started")
  return "no session started"


@case
def case_pass_ends_it():
  """A PASS verdict costs one session and changes nothing."""
  run_pi, calls = runner(["PASS"])
  session = FakeSession(900, final="WORKER SUMMARY")
  out = asyncio.run(H.verify_and_repair(session, run_pi, "WORKER SUMMARY"))
  expect(out == "WORKER SUMMARY", "PASS keeps the answer")
  expect(len(calls) == 1, "exactly one reviewer session: %d" % len(calls))
  expect("reviewer" in calls[0]["note"].lower(), "the reviewer note is used")
  expect("WORKER SUMMARY" in calls[0]["text"], "the worker summary is quoted")
  expect(session.final_message == "WORKER SUMMARY",
         "the reviewer's text never becomes the answer")
  return "1 session, answer unchanged"


@case
def case_gaps_buy_one_repair():
  """Gaps start exactly one repair session, whose summary becomes the answer."""
  run_pi, calls = runner([
      "out/a.docx | missing section 4 | add it",
      "repaired summary",
  ])
  session = FakeSession(900, final="WORKER SUMMARY")
  out = asyncio.run(H.verify_and_repair(session, run_pi, "WORKER SUMMARY"))
  expect(len(calls) == 2, "review then repair: %d" % len(calls))
  expect("missing section 4" in calls[1]["text"], "the gap reaches the repair")
  expect("engineer" in calls[1]["note"].lower(), "the worker note is restored")
  expect(out == "repaired summary",
         "the repair run's own summary is the answer: %r" % (out,))
  return "2 sessions, repaired answer returned"


@case
def case_prose_verdict_becomes_one_brief():
  """A verdict without pipes is still a brief: its prose is joined, not lost."""
  run_pi, calls = runner([
      "The submission zip does not exist.\nThe worker never created it.",
      "repaired",
  ])
  session = FakeSession(900, final="ANSWER")
  out = asyncio.run(H.verify_and_repair(session, run_pi, "ANSWER"))
  expect(len(calls) == 2, "a prose verdict buys a repair: %d" % len(calls))
  expect("submission zip does not exist" in calls[1]["text"],
         "the prose reaches the repair brief")
  expect(out == "repaired", "the repair's answer is returned")
  return "prose joined into one brief"


@case
def case_no_repair_without_time():
  """Gaps with too little time left do not start a repair session."""
  run_pi, calls = runner(["out/a.docx | missing | fix"])
  session = FakeSession(900, final="ANSWER")
  # the reviewer spends the budget: only the wrap-up margin is left
  async def spend_and_run(text, note, stop_before, max_sec=None):
    session.left = H.STOP_BEFORE_SEC + 10
    calls.append({"text": text, "note": note, "stop_before": stop_before})
    return FakeRun("out/a.docx | missing | fix"), None

  out = asyncio.run(H.verify_and_repair(session, spend_and_run, "ANSWER"))
  expect(out == "ANSWER", "the answer survives a skipped repair")
  expect(len(calls) == 1, "no repair session: %d" % len(calls))
  return "1 session, no repair"


@case
def case_crashed_reviewer_changes_nothing():
  """A reviewer that produced no reply is not a PASS and not a repair."""
  run_pi, calls = runner(["", ""])
  session = FakeSession(900, final="ANSWER")

  async def broken(text, note, stop_before, max_sec=None):
    calls.append({"text": text, "note": note})
    return FakeRun("", replies=0), None

  out = asyncio.run(H.verify_and_repair(session, broken, "ANSWER"))
  expect(out == "ANSWER", "the answer survives a dead reviewer")
  expect(len(calls) == 1, "no repair session: %d" % len(calls))
  return "1 session, answer kept"


@case
def case_stage_errors_are_catchable():
  """The stage raises, so `solve()` can keep the worker's answer."""
  async def boom(text, note, stop_before, max_sec=None):
    raise RuntimeError("relay died")

  session = FakeSession(900, final="ANSWER")
  try:
    asyncio.run(H.verify_and_repair(session, boom, "ANSWER"))
  except RuntimeError as exc:
    expect("relay died" in str(exc), "the error is the stage's own")
    return "raises RuntimeError for solve() to catch"
  raise AssertionError("a failing stage must not look like a PASS")


@case
def case_repair_prompt_is_plain():
  """The repair text lists every gap as a bullet under one heading."""
  text = H.repair_stdin("TASK", ["a | b | c", "d | e | f"])
  expect("TASK" in text and "- a | b | c" in text and "- d | e | f" in text,
         "both gaps are listed")
  expect("updated summary" in text, "it asks for an updated summary")
  return "two bullets under one heading"


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
