#!/usr/bin/env python3
"""Checks G0 (contract extraction) in `serve.py`: stubs, no model, no network.

Four cases, driven through the real `Agent.run()` loop with the model and MCP
faked out by `verify_wiring.py` (reused so both checks exercise the same loop):

  1. `SELFCHECK` unset (and `=0`): `contract.extract` is never called, and no
     `self.contract` is left behind.
  2. `SELFCHECK=1`: `extract` is called exactly once, with
     `self.session.instruction`, and with the card from `self.card` or
     `self.task_card` (`None` when neither exists).
  3. `SELFCHECK=1` and `extract` raises: the answer still comes back, selfcheck
     is skipped, and the log records the failure by exception type only.
  4. `SELFCHECK=1` with the real `contract.py`: selfcheck receives the extracted
     dict (not `None`), and the log has a count only - never the contract or the
     instruction, which must not be retained (fair play).

Usage:
  python3 verify_g0.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import verify_wiring as wiring  # noqa: E402  pylint: disable=wrong-import-position

ANSWER = wiring.ANSWER
CONTRACT = wiring.CONTRACT
RESULTS = []


def check(name: str, ok: bool, detail: str) -> None:
  """Records and prints one assertion."""
  RESULTS.append((name, ok))
  print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")


def recording_selfcheck(calls: list) -> types.ModuleType:
  """Returns a `selfcheck` module that records each call."""

  def record(run, contract, final_message=None, max_items=None):
    calls.append((contract, final_message))
    return []

  return wiring.fake_selfcheck(record)


def fake_extract(seen: list, result=CONTRACT):
  """Returns an `extract` that records its arguments and returns `result`."""

  def extract(instruction, task_card=None):
    seen.append((instruction, task_card))
    return dict(result) if isinstance(result, dict) else result

  return extract


def case_off() -> None:
  """Case 1: with the flag off, G0 must not run at all."""
  print("\ncase 1  SELFCHECK unset / =0")
  seen = []
  for label, value in (("unset", None), ("=0", "0")):
    seen.clear()
    if value is None:
      os.environ.pop("SELFCHECK", None)
    else:
      os.environ["SELFCHECK"] = value
    calls = []
    sys.modules["contract"] = wiring.fake_contract(fake_extract(seen))
    sys.modules["selfcheck"] = recording_selfcheck(calls)
    session = wiring.FakeSession(tempfile.mkdtemp())
    agent = wiring.new_agent(session)
    answer, log, _ = wiring.drive(agent, session)
    check(
        f"SELFCHECK {label}: extract never called, no contract set",
        not seen
        and calls == []
        and answer == ANSWER
        and not hasattr(agent, "contract"),
        f"extract_calls={len(seen)}, selfcheck_calls={len(calls)},"
        f" has_contract={hasattr(agent, 'contract')},"
        f" answer={answer!r}, log={log.strip()!r}",
    )


def case_on() -> None:
  """Case 2: one extract call, with the session's instruction and the card."""
  print("\ncase 2  SELFCHECK=1 extracts once")
  os.environ["SELFCHECK"] = "1"

  # 2a: the card hangs off `self.card`.
  seen = []
  calls = []
  sys.modules["contract"] = wiring.fake_contract(fake_extract(seen))
  sys.modules["selfcheck"] = recording_selfcheck(calls)
  session = wiring.FakeSession(tempfile.mkdtemp())
  agent = wiring.new_agent(session)
  agent.card = {"task_id": "card-task"}
  answer, log, _ = wiring.drive(agent, session)
  check(
      "extract called exactly once",
      len(seen) == 1,
      f"extract_calls={len(seen)}, log={log.strip()!r}",
  )
  check(
      "instruction is the session's, task_card is self.card",
      bool(seen)
      and seen[0][0] == session.instruction
      and seen[0][1] == {"task_id": "card-task"},
      f"instruction_matches={bool(seen) and seen[0][0] == session.instruction},"
      f" task_card={seen[0][1] if seen else None!r}",
  )

  # 2b: the card hangs off `self.task_card`.
  seen = []
  sys.modules["contract"] = wiring.fake_contract(fake_extract(seen))
  sys.modules["selfcheck"] = recording_selfcheck([])
  session = wiring.FakeSession(tempfile.mkdtemp())
  agent = wiring.new_agent(session)
  agent.task_card = {"task_id": "task-card"}
  wiring.drive(agent, session)
  check(
      "task_card read from self.task_card",
      bool(seen) and seen[0][1] == {"task_id": "task-card"},
      f"task_card={seen[0][1] if seen else None!r}",
  )

  # 2c: no card at all -> None, which extract() accepts.
  seen = []
  sys.modules["contract"] = wiring.fake_contract(fake_extract(seen))
  sys.modules["selfcheck"] = recording_selfcheck([])
  session = wiring.FakeSession(tempfile.mkdtemp())
  agent = wiring.new_agent(session)
  answer, _, _ = wiring.drive(agent, session)
  check(
      "no card on the agent: task_card is None",
      bool(seen) and seen[0][1] is None and answer == ANSWER,
      f"task_card={seen[0][1] if seen else 'no call'!r}",
  )


def case_extract_fails() -> None:
  """Case 3: a raising extract must not disturb the finish path."""
  print("\ncase 3  extract raises")
  os.environ["SELFCHECK"] = "1"

  def boom(instruction, task_card=None):
    raise RuntimeError("cannot read the instruction: secret task text")

  calls = []
  sys.modules["contract"] = wiring.fake_contract(boom)
  sys.modules["selfcheck"] = recording_selfcheck(calls)
  session = wiring.FakeSession(tempfile.mkdtemp())
  agent = wiring.new_agent(session)
  answer, log, elapsed = wiring.drive(agent, session)
  check(
      "answer still returned, selfcheck skipped, failure logged",
      answer == ANSWER
      and calls == []
      and agent.contract is None
      and "g0: no contract" in log
      and "RuntimeError" in log,
      f"answer={answer!r}, selfcheck_calls={len(calls)},"
      f" contract={agent.contract!r}, log={log.strip()!r}",
  )
  check(
      "the exception message is not logged",
      "secret task text" not in log and "cannot read the instruction" not in log,
      f"log={log.strip()!r}",
  )


def case_real_contract() -> None:
  """Case 4: the real contract.py hands selfcheck a dict, not None."""
  print("\ncase 4  real contract.py feeds selfcheck")
  os.environ["SELFCHECK"] = "1"
  sys.modules.pop("contract", None)  # the real module, not a stub
  seen = []

  def record(run, contract, final_message=None, max_items=None):
    seen.append(contract)
    return []

  sys.modules["selfcheck"] = wiring.fake_selfcheck(record)
  session = wiring.FakeSession(tempfile.mkdtemp())
  session.instruction = (
      "Write the final table to `output/report.csv` with the columns stage and"
      " flow_gpm."
  )
  agent = wiring.new_agent(session)
  answer, log, _ = wiring.drive(agent, session)
  contract = seen[0] if seen else None
  check(
      "selfcheck got the extracted dict, with its deliverable",
      answer == ANSWER
      and isinstance(contract, dict)
      and [d.get("path") for d in contract.get("deliverables") or []]
      == ["output/report.csv"],
      f"contract_is_dict={isinstance(contract, dict)},"
      f" deliverables={(contract or {}).get('deliverables')!r},"
      f" self.contract_is_dict={isinstance(agent.contract, dict)}",
  )
  check(
      "log carries the count only, not the instruction or paths",
      "g0: extracted 1 deliverables" in log
      and session.instruction not in log
      and "report.csv" not in log,
      f"log={log.strip()!r}",
  )


def main() -> int:
  print("stubs: openai, websockets, model (no network); G0 wiring only")
  case_off()
  case_on()
  case_extract_fails()
  case_real_contract()
  passed = sum(1 for _, ok in RESULTS if ok)
  print(f"\n{passed}/{len(RESULTS)} assertions passed")
  if passed != len(RESULTS):
    print("FAILED: " + ", ".join(name for name, ok in RESULTS if not ok))
    return 1
  print("ALL ASSERTIONS PASSED")
  return 0


if __name__ == "__main__":
  sys.exit(main())
