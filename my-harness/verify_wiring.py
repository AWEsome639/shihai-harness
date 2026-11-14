#!/usr/bin/env python3
"""Checks the SELFCHECK wiring in `serve.py` with stubs: no model, no network.

The three required cases, driven through the real `Agent.run()` loop with
`complete()`/`connect_mcp()` faked out:

  1. `SELFCHECK` unset (and `SELFCHECK=0`): selfcheck is never called.
  2. `SELFCHECK=1` with `self.contract`: called exactly once, after the final
     answer exists. Without a contract it is skipped and logged.
  3. A selfcheck that raises, or one that hangs: the final answer still comes
     back (the timeout is small here so the test stays fast).

Case 2c runs the real `selfcheck.py` against a real file, so the adapter's
`run(cmd) -> (exit code, output)` shape is checked end to end, not just the
call site.

Usage:
  python3 verify_wiring.py
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def stub_openai() -> None:
  """Registers a minimal `openai`: serve.py builds a client at import time."""

  class OpenAIError(Exception):
    """Stand-in for openai.OpenAIError."""

  class APIError(OpenAIError):
    """Stand-in for openai.APIError."""

  class APIStatusError(APIError):
    """Stand-in for openai.APIStatusError."""

  class AsyncOpenAI:
    """Stand-in client factory: nothing is ever called on it here."""

    def __init__(self, *args, **kwargs):
      pass

  module = types.ModuleType("openai")
  module.OpenAIError = OpenAIError
  module.APIError = APIError
  module.APIStatusError = APIStatusError
  module.AsyncOpenAI = AsyncOpenAI
  module.OpenAI = AsyncOpenAI
  sys.modules["openai"] = module


def stub_websockets() -> None:
  """Registers the two names taskbridge imports from `websockets`."""

  class ConnectionClosed(Exception):
    """Stand-in for websockets.exceptions.ConnectionClosed."""

  async def serve(*args, **kwargs):
    raise AssertionError("the stub server must never be started")

  top = types.ModuleType("websockets")
  top.__path__ = []
  asyncio_pkg = types.ModuleType("websockets.asyncio")
  asyncio_pkg.__path__ = []
  server = types.ModuleType("websockets.asyncio.server")
  server.serve = serve
  exceptions = types.ModuleType("websockets.exceptions")
  exceptions.ConnectionClosed = ConnectionClosed
  top.asyncio = asyncio_pkg
  top.exceptions = exceptions
  asyncio_pkg.server = server
  sys.modules.update({
      "websockets": top,
      "websockets.asyncio": asyncio_pkg,
      "websockets.asyncio.server": server,
      "websockets.exceptions": exceptions,
  })


stub_openai()
stub_websockets()
import serve  # noqa: E402  pylint: disable=wrong-import-position

ANSWER = "Done: wrote out.csv with the stage and flow columns."
CONTRACT = {
    "workdir": "/app",
    "deliverables": [{"path": "out.csv", "kind": "csv"}],
}


class FakeSession:
  """The slice of `taskbridge.Session` that `Agent.run()` touches."""

  def __init__(self, workdir: str, left: float | None = None):
    self.workdir = workdir
    self.instruction = "write out.csv (columns stage,flow_gpm) in the workdir"
    self.final_message = ""
    self.limits = {}
    self.mcp_servers = []
    self.left = left

  def time_left(self):
    return self.left

  async def exec(self, command: str, timeout_sec: int = 120):
    """Runs the command locally, as the task container would."""
    done = subprocess.run(
        ["bash", "-c", command],
        cwd=self.workdir,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    return {
        "exit_code": done.returncode,
        "output": done.stdout + done.stderr,
        "timed_out": False,
        "truncated": False,
    }


class FakeReply:
  """A reply that carries the final answer and no tool calls."""

  def __init__(self, text: str):
    self.text, self.reasoning = text, ""
    self.calls, self.usage, self.finish = {}, None, "stop"
    self.extra = {}

  def message(self):
    return {"role": "assistant", "content": self.text}


def new_agent(session: FakeSession) -> serve.Agent:
  """Builds an `Agent` whose model call and MCP connect are faked out."""
  agent = serve.Agent.__new__(serve.Agent)
  agent.session = session
  agent.tools = []
  agent.mcp = {}
  agent.messages = []
  agent.prompt_tokens = 0
  agent.output_ceiling = 32_000
  agent.max_output = 32_000
  agent.token_limit = "max_completion_tokens"
  agent.effort = {}
  agent.responses = False
  agent.finish_margin = serve.FINISH_MARGIN_SEC
  agent.total = None
  agent.worker_done = False
  agent.stopped_for_check = False
  agent.rounds = 0

  async def connect_mcp():
    return ""

  async def complete(head="", **kwargs):
    return FakeReply(ANSWER)

  agent.connect_mcp = connect_mcp
  agent.complete = complete
  return agent


def fake_selfcheck(fn) -> types.ModuleType:
  """Returns a `selfcheck` module whose selfcheck() is `fn`."""
  module = types.ModuleType("selfcheck")
  module.selfcheck = fn
  module.summary = lambda defects: "SELFCHECK: green"
  return module


def fake_contract(fn) -> types.ModuleType:
  """Returns a `contract` module whose extract() is `fn`."""
  module = types.ModuleType("contract")
  module.extract = fn
  return module


def real_selfcheck() -> types.ModuleType:
  """Loads the real selfcheck.py under the name the wiring imports."""
  spec = importlib.util.spec_from_file_location(
      "selfcheck_real", os.path.join(HERE, "selfcheck.py"))
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def drive(agent: serve.Agent, session: FakeSession):
  """Runs Agent.run() and returns the answer, stderr, and elapsed seconds.

  The elapsed time is taken inside the loop, when `run()` returns, so it is not
  inflated by `asyncio.run` waiting for a thread an abandoned selfcheck left.
  """
  stderr = io.StringIO()
  box = {}

  async def main():
    started = time.monotonic()
    answer = await agent.run()
    box["elapsed"] = time.monotonic() - started
    return answer

  with contextlib.redirect_stderr(stderr):
    answer = asyncio.run(main())
  return answer, stderr.getvalue(), box.get("elapsed", 0.0)


RESULTS = []


def check(name: str, ok: bool, detail: str) -> None:
  """Records and prints one assertion."""
  RESULTS.append((name, ok))
  print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")


def case_no_flag() -> None:
  """Case 1: the flag is off, so the recording selfcheck must stay unused."""
  print("\ncase 1  SELFCHECK unset (default off)")
  calls = []

  def recording(run, contract, final_message=None, max_items=None):
    calls.append((contract, final_message))
    return []

  for label, value in (("unset", None), ("=0", "0")):
    calls.clear()
    if value is None:
      os.environ.pop("SELFCHECK", None)
    else:
      os.environ["SELFCHECK"] = value
    sys.modules["selfcheck"] = fake_selfcheck(recording)
    session = FakeSession(tempfile.mkdtemp())
    answer, log, _ = drive(new_agent(session), session)
    check(
        f"SELFCHECK {label}: selfcheck not called",
        calls == [] and answer == ANSWER,
        f"calls={len(calls)}, answer={answer!r}, log={log.strip()!r}",
    )


def case_flag_on() -> None:
  """Case 2: with the flag and a contract, selfcheck runs once."""
  print("\ncase 2  SELFCHECK=1")
  os.environ["SELFCHECK"] = "1"
  sys.modules["contract"] = fake_contract(
      lambda instruction, task_card=None: CONTRACT)
  calls = []

  def recording(run, contract, final_message=None, max_items=None):
    calls.append((dict(contract), final_message))
    return []

  sys.modules["selfcheck"] = fake_selfcheck(recording)
  session = FakeSession(tempfile.mkdtemp())
  agent = new_agent(session)
  agent.contract = CONTRACT
  answer, log, _ = drive(agent, session)
  check(
      "self.contract present: called exactly once",
      len(calls) == 1,
      f"calls={len(calls)}, answer={answer!r}, log={log.strip()!r}",
  )
  check(
      "called after the final answer, with that answer",
      bool(calls) and calls[0][0] == CONTRACT and calls[0][1] == ANSWER,
      f"contract_is_expected={bool(calls) and calls[0][0] == CONTRACT},"
      f" final_message={calls[0][1]!r}" if calls else "no call",
  )

  # Case 2b: G0 could not read a contract -> skipped, logged, answer returned.
  calls.clear()

  def no_contract(instruction, task_card=None):
    raise ValueError("nothing is declared")

  sys.modules["contract"] = fake_contract(no_contract)
  session = FakeSession(tempfile.mkdtemp())
  answer, log, _ = drive(new_agent(session), session)
  check(
      "no self.contract: skipped and logged",
      calls == [] and answer == ANSWER and "no self.contract" in log,
      f"calls={len(calls)}, answer={answer!r}, log={log.strip()!r}",
  )

  # Case 2c: the real selfcheck.py against a real deliverable file.
  workdir = tempfile.mkdtemp()
  with open(os.path.join(workdir, "out.csv"), "w", encoding="utf-8") as handle:
    handle.write("stage,flow_gpm\nHYDRO,12\n")
  sys.modules["selfcheck"] = real_selfcheck()
  contract = {
      "workdir": workdir,
      "deliverables": [{"path": "out.csv", "kind": "csv", "required": True}],
      "secondary": [{"kind": "final_message", "required": True}],
  }
  sys.modules["contract"] = fake_contract(
      lambda instruction, task_card=None: contract)
  session = FakeSession(workdir)
  agent = new_agent(session)
  agent.contract = contract
  answer, log, _ = drive(agent, session)
  check(
      "real selfcheck.py on a real file: green, adapter shape works",
      answer == ANSWER and "SELFCHECK: green" in log,
      f"answer={answer!r}, log={log.strip()!r}",
  )


def case_failures() -> None:
  """Case 3: a raising or hanging selfcheck must not break the run."""
  print("\ncase 3  selfcheck fails")
  os.environ["SELFCHECK"] = "1"
  sys.modules["contract"] = fake_contract(
      lambda instruction, task_card=None: CONTRACT)

  def boom(run, contract, final_message=None, max_items=None):
    raise RuntimeError("selfcheck exploded")

  sys.modules["selfcheck"] = fake_selfcheck(boom)
  session = FakeSession(tempfile.mkdtemp())
  agent = new_agent(session)
  agent.contract = CONTRACT
  answer, log, elapsed = drive(agent, session)
  check(
      "raises: answer still returned, only logged",
      answer == ANSWER and "RuntimeError" in log,
      f"answer={answer!r}, log={log.strip()!r}",
  )

  def hang(run, contract, final_message=None, max_items=None):
    time.sleep(1.5)
    return []

  sys.modules["selfcheck"] = fake_selfcheck(hang)
  # 7 s left -> a 7/4 s finish margin -> ~0.25 s for the check on purpose
  session = FakeSession(tempfile.mkdtemp(), left=7.0)
  agent = new_agent(session)
  agent.contract = CONTRACT
  answer, log, elapsed = drive(agent, session)
  check(
      "hangs: cut by asyncio.wait_for, answer still returned",
      answer == ANSWER and "TimeoutError" in log and elapsed < 1.0,
      f"answer={answer!r}, log={log.strip()!r},"
      f" run() returned in {elapsed:.2f}s (fake slept 1.5s)",
  )


def main() -> int:
  print("stubs: openai, websockets (no model, no network)")
  case_no_flag()
  case_flag_on()
  case_failures()
  passed = sum(1 for _, ok in RESULTS if ok)
  print(f"\n{passed}/{len(RESULTS)} assertions passed")
  if passed != len(RESULTS):
    print("FAILED: " + ", ".join(name for name, ok in RESULTS if not ok))
    return 1
  print("ALL ASSERTIONS PASSED")
  return 0


if __name__ == "__main__":
  sys.exit(main())
