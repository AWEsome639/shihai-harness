#!/usr/bin/env python3
"""Checks the bounded SELFCHECK -> feedback -> work() loop in `serve.py`.

Stubs only (`openai`, `websockets`): no model call, no network, no container.
`work()` is wrapped, so every count below is a real `Agent.work()` entry, and
case 3 parses the user message that was appended, so the fed-back text is the
real `selfcheck.feedback()` payload in the §5.4 shape.

Cases:
  1. SELFCHECK unset / =0: neither the check nor a repair runs.
  2. =1 with green defects, and with only note/fix defects: no repair.
  3. =1 with a fatal defect: one repair round, work() twice, §5.4 payload fed.
  4. =1 with defects that never clear: MAX_SELFCHECK_ROUNDS stops the loop;
     an identical defect set twice stops it earlier (the repeat guard).
  5. the check raises: the current answer still goes out.
  6. less than two finish margins left: no repair round is entered.

Usage:
  python3 verify_loop.py
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import verify_wiring as wiring  # installs the stubs, then imports serve

serve = wiring.serve
ANSWER = wiring.ANSWER
CONTRACT = wiring.CONTRACT
REAL = wiring.real_selfcheck()
RESULTS = []

FATAL = {
    "gate": "G4", "severity": "fatal", "path": "out.csv",
    "detail": "line 12 flow_gpm = 'TBD'",
    "rule": "a cell reading TBD scores the whole package zero",
    "fix": "fill in the real flow, or leave it empty",
}
NOTE = {"gate": "G7", "severity": "note", "path": "out.csv",
        "detail": "no final_message", "rule": "state what you delivered",
        "fix": "add one line per deliverable"}


def check(name: str, ok: bool, detail: str) -> None:
  """Records and prints one assertion."""
  RESULTS.append((name, ok))
  print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")


def set_selfcheck(value, rounds=None):
  """Points the two switches at `value` / `rounds` (None clears them)."""
  if value is None:
    os.environ.pop("SELFCHECK", None)
  else:
    os.environ["SELFCHECK"] = value
  if rounds is None:
    os.environ.pop("MAX_SELFCHECK_ROUNDS", None)
  else:
    os.environ["MAX_SELFCHECK_ROUNDS"] = str(rounds)


def install(defect_fn, with_contract=True):
  """Installs a `selfcheck` module with the real feedback() and `defect_fn`."""
  module = types.ModuleType("selfcheck")
  module.selfcheck = defect_fn
  module.feedback = REAL.feedback
  module.summary = REAL.summary
  sys.modules["selfcheck"] = module
  sys.modules["contract"] = (
      wiring.fake_contract(lambda instruction, task_card=None: dict(CONTRACT))
      if with_contract else None)


def build(left=None, session_hook=None):
  """Returns (session, agent, counter) with work() counted and answers varied."""
  session = wiring.FakeSession(tempfile.mkdtemp())
  if left is not None:
    session.left = left
  agent = wiring.new_agent(session)
  counter = {"work": 0}

  async def complete(head="", **kwargs):
    counter["work"] += 1
    if session_hook:
      session_hook(counter["work"], session)
    return wiring.FakeReply(f"{ANSWER} (attempt {counter['work']})")

  agent.complete = complete
  real_work = agent.work

  async def counting_work():
    counter.setdefault("entered", 0)
    counter["entered"] = counter.get("entered", 0) + 1
    return await real_work()

  agent.work = counting_work
  return session, agent, counter


def run_case(left=None, session_hook=None):
  """Drives one `Agent.run()` and returns (answer, log, elapsed, counter)."""
  session, agent, counter = build(left, session_hook)
  answer, log, elapsed = wiring.drive(agent, session)
  return answer, log, elapsed, counter, agent


def case_off() -> None:
  """Case 1: with no flag, the check and the repair must not be reached."""
  print("\ncase 1  SELFCHECK unset / =0: nothing triggers")
  for label, value in (("unset", None), ("=0", "0")):
    set_selfcheck(value)
    calls = []
    install(lambda run, contract, final_message=None, max_items=None:
            calls.append(1) or [])
    answer, log, _, counter, agent = run_case()
    check(
        f"SELFCHECK {label}: no check, one work(), no contract",
        calls == [] and counter["entered"] == 1 and answer.startswith(ANSWER)
        and not hasattr(agent, "contract"),
        f"checks={len(calls)}, work={counter['entered']},"
        f" has_contract={hasattr(agent, 'contract')}, log={log.strip()!r}",
    )


def case_no_repair() -> None:
  """Case 2: green or non-fatal defects must not ask the model again."""
  print("\ncase 2  SELFCHECK=1, no fatal defect: no repair")
  set_selfcheck("1")
  for label, defects in (("green", []), ("fix", [dict(NOTE, severity="fix")]),
                         ("note", [NOTE])):
    calls = []
    install(lambda run, contract, final_message=None, max_items=None,
            box=defects: calls.append(1) or [dict(item) for item in box])
    answer, log, _, counter, agent = run_case()
    fed = [m for m in agent.messages
           if m.get("role") == "user" and "SELFCHECK found" in str(m)]
    check(
        f"{label}: work() once, no feedback turn",
        counter["entered"] == 1 and len(calls) == 1 and fed == []
        and answer.startswith(ANSWER),
        f"work={counter['entered']}, checks={len(calls)}, fed={len(fed)},"
        f" log={log.strip()!r}",
    )


def case_fatal() -> None:
  """Case 3: one fatal defect buys exactly one repair round, with the payload."""
  print("\ncase 3  SELFCHECK=1, fatal defect: one repair round")
  set_selfcheck("1")
  calls = []
  install(lambda run, contract, final_message=None, max_items=None:
          calls.append(1) or ([dict(FATAL)] if len(calls) == 1 else []))
  answer, log, _, counter, agent = run_case()
  fed = [m for m in agent.messages
         if m.get("role") == "user" and "SELFCHECK found" in str(m)]
  payload = None
  if fed:
    body = str(fed[0]["content"]).split("\n", 1)[-1]
    try:
      payload = json.loads(body)
    except ValueError:
      payload = None
  check(
      "fatal: work() twice (original + 1 repair)",
      counter["entered"] == 2 and len(calls) == 2,
      f"work={counter['entered']}, checks={len(calls)}, log={log.strip()!r}",
  )
  check(
      "one user turn appended, in the fixed §5.4 shape",
      len(fed) == 1 and isinstance(payload, dict)
      and payload.get("status") == "fail"
      and payload.get("gate") == "G4"
      and payload["items"][0]["severity"] == "fatal"
      and payload["items"][0]["fix"] == FATAL["fix"]
      and payload["items"][0]["rule"] == FATAL["rule"]
      and "fatal" in payload.get("next", ""),
      f"turns={len(fed)}, payload={payload!r}",
  )
  check(
      "the second answer is the one returned",
      answer == f"{ANSWER} (attempt 2)",
      f"answer={answer!r}",
  )


def case_budget() -> None:
  """Case 4: an endless defect stream stops at the cap or at the repeat."""
  print("\ncase 4  SELFCHECK=1, defects never clear: bounded")
  set_selfcheck("1")
  calls = []
  install(lambda run, contract, final_message=None, max_items=None:
          calls.append(1) or [dict(FATAL, detail=f"line {len(calls)} TBD")])
  answer, log, _, counter, agent = run_case()
  check(
      "MAX_SELFCHECK_ROUNDS=2 (default): work() 3, checks 2, then stop",
      counter["entered"] == 3 and len(calls) == 2
      and "known defects" in log and answer.startswith(ANSWER),
      f"work={counter['entered']}, checks={len(calls)}, log={log.strip()!r}",
  )

  calls.clear()
  install(lambda run, contract, final_message=None, max_items=None:
          calls.append(1) or [dict(FATAL)])
  _, log, _, counter, _ = run_case()
  check(
      "identical defects twice: repeat guard stops after 1 repair",
      counter["entered"] == 2 and len(calls) == 2
      and "same defects again" in log,
      f"work={counter['entered']}, checks={len(calls)}, log={log.strip()!r}",
  )

  calls.clear()
  install(lambda run, contract, final_message=None, max_items=None:
          calls.append(1) or [dict(FATAL, detail=f"row {len(calls)}")])
  set_selfcheck("1", rounds=1)
  _, log, _, counter, _ = run_case()
  set_selfcheck("1")
  check(
      "MAX_SELFCHECK_ROUNDS=1: work() 2, capped as asked",
      counter["entered"] == 2 and len(calls) == 1,
      f"work={counter['entered']}, checks={len(calls)}, log={log.strip()!r}",
  )


def case_check_raises() -> None:
  """Case 5: a failing check must not cost the answer."""
  print("\ncase 5  the check itself fails")
  set_selfcheck("1")

  def boom(run, contract, final_message=None, max_items=None):
    raise RuntimeError("selfcheck exploded")

  install(boom)
  answer, log, _, counter, _ = run_case()
  check(
      "raises: work() once, answer returned, only logged",
      counter["entered"] == 1 and answer.startswith(ANSWER)
      and "RuntimeError" in log and "repair round" not in log,
      f"work={counter['entered']}, answer={answer!r}, log={log.strip()!r}",
  )

  install(lambda run, contract, final_message=None, max_items=None: [])
  sys.modules["contract"] = wiring.fake_contract(
      lambda instruction, task_card=None: (_ for _ in ()).throw(
          ValueError("no contract")))
  answer, log, _, counter, _ = run_case()
  check(
      "no contract: work() once, answer returned, repair skipped",
      counter["entered"] == 1 and "no self.contract" in log
      and "repair round" not in log,
      f"work={counter['entered']}, answer={answer!r}, log={log.strip()!r}",
  )


def case_low_time() -> None:
  """Case 6: not enough time for another round means no round."""
  print("\ncase 6  low on time")
  set_selfcheck("1")
  calls = []

  def shrink(count, session):
    # finish_margin was min(180, 1000/4) = 180 at start; 100 < 2 * 180
    if count == 1:
      session.left = 100.0

  install(lambda run, contract, final_message=None, max_items=None:
          calls.append(1) or [dict(FATAL)])
  _, log, _, counter, _ = run_case(left=1000.0, session_hook=shrink)
  check(
      "left=100 < 2*180: no repair round, no check",
      counter["entered"] == 1 and calls == [] and "low on time" in log,
      f"work={counter['entered']}, checks={len(calls)},"
      f" log={log.strip()!r}",
  )


def main() -> int:
  print("stubs: openai, websockets (no model, no network)")
  case_off()
  case_no_repair()
  case_fatal()
  case_budget()
  case_check_raises()
  case_low_time()
  passed = sum(1 for _, ok in RESULTS if ok)
  print(f"\n{passed}/{len(RESULTS)} assertions passed")
  if passed != len(RESULTS):
    print("FAILED: " + ", ".join(n for n, ok in RESULTS if not ok))
    return 1
  print("ALL ASSERTIONS PASSED")
  return 0


if __name__ == "__main__":
  sys.exit(main())
