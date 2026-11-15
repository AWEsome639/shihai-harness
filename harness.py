# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Pi coding agent harness (`@earendil-works/pi-coding-agent`).

Runs one headless Pi session per task. Pi runs in the serve container while the
task's files live in the task container. `task-tools.ts` replaces Pi's built-in
`read`, `write`, `edit`, and `bash` tools with versions that operate on the task
container through `taskbridge.Relay`, and the task's MCP servers are proxied
through the same relay. Pi's agent loop handles model calls, tool execution,
retries, and conversation compaction, returning its last response as the final
message.

The model is configured from the environment variables set by the evaluator in
`models.json` for each task. Claude and Gemini models use their native APIs
under Pi's built-in provider names (`anthropic` and `google`) so Pi's catalog
supplies their limits, thinking support, and image input capabilities. Any other
model uses the OpenAI-compatible `MODEL_BASE_URL` and `MODEL_API_KEY` under a
custom `model` provider with `UNKNOWN_MODEL` limits.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
from typing import Any

import taskbridge

HERE = pathlib.Path(__file__).resolve().parent
# npm ci, from package-lock.json
PI = shutil.which("pi") or str(HERE / "node_modules" / ".bin" / "pi")
# pi's --thinking level: off, minimal, low, medium, high, xhigh or max
THINKING = "max"
# stop pi this long before the deadline (less on short tasks) so its answer
# arrives
STOP_BEFORE_SEC = 90
CATALOG_TIMEOUT_SEC = 30
# Request features the Google Cloud route for Claude refuses. pi leaves them
# out, and the Anthropic API works without them too.
COMPAT = {
    "supportsStrictTools": False,
    "supportsMidConvoEffort": False,
    "supportsMidConvoSystemMessages": False,
    "supportsMidConvoToolChanges": False,
}
# A model pi's catalog does not know gets these limits; tune them for yours.
UNKNOWN_MODEL = {"input": ["text"], "contextWindow": 128000, "maxTokens": 16384}
NOTE = (
    "You are working on an engineering task. Your tools act on the task's Linux"
    " container, whose working directory is {workdir}; it has no internet"
    " access. Read the whole instruction and the files it points to."
    "\n\nDeliverables are FILES, not prose. If the instruction names a directory"
    " (for example `output/`), create that directory and write every deliverable"
    " into it as a real file, in the format the instruction asks for. Your final"
    " reply is not a deliverable and is not graded as one - a memo, report, or"
    " answer that exists only in your reply scores zero. Before you finish, list"
    " the output directory and confirm each required file is there and non-empty."
    "\n\nWrite for a reviewer who scores each claim separately. Every conclusion you"
    " reach must appear in the deliverable as its own plain, checkable sentence - not"
    " implied, not buried in a paragraph, and not only in your reply. State the"
    " magnitude, the direction, and what it rules in or out."
    "\n\nBefore you finish, turn the instruction into a checklist of its acceptance"
    " criteria - every numeric threshold, every gate, every 'must' - and check the"
    " deliverable against each one. If the workspace has a validator, checker, or"
    " scoring script, run it. Do not declare the work done until every gate you can"
    " test passes; a model that is merely valid but violates a stated gate scores zero."
    "\n\nA clash checker is pre-installed at {workdir}/tools/clash_check.py."
    " Run `python3 {workdir}/tools/clash_check.py <model.ifc>` on any model you"
    " produce; it exits non-zero when bounded boxes overlap by more than 2 mm."
    " Iterate on the model until it reports HARD CLASHES: 0, not merely a lower count."
    "\n\nFixing one gate can break another: a leg you already passed is not a leg you"
    " still pass. Lengthening, rerouting or adding a service to satisfy a coverage or"
    " connectivity clause is exactly how a model that had no clashes acquires some."
    " After every change, re-run every gate you can test, in full, and report the"
    " numbers for all of them - not only the one you were fixing."
    "\nA gate in the instruction usually has several independent legs, and passing"
    " one is not passing the gate. Split each gate into its separate conditions,"
    " check them one by one, and say in your summary which you could test and which"
    " you could not - a leg you cannot test is not a leg that passes."
    "\n\nA route is one network, not a covering. Serving every row at high coverage"
    " and still failing connectivity is the classic way to get this wrong: stitch"
    " short segments along each row, and you have 12 separate networks where the"
    " gate wants 1. Draw each service as a single continuous run that starts at"
    " its service entry and is joined end to end through every row it serves."
    " Check the segment count, not only the coverage number: if one service comes"
    " out as more than one connected piece, it fails no matter how well it covers."
    "\n\nYou have a clash checker but no connectivity checker, so fixing one has"
    " repeatedly broken the other. Build the cheapest possible connectivity test"
    " yourself before you start routing: for each service, take its elements in"
    " order from the service entry and confirm that each one starts within a few"
    " hundred millimetres of where the previous one ended. Report that chain length"
    " alongside the clash count on every attempt. Route first as one continuous"
    " line that clears the boxes, then adjust - do not lay segments down row by row"
    " and hope the ends meet, because re-routing to join them is what puts the line"
    " through the equipment."
    "\n\nDo not substitute a checker you wrote for the number the gate states. In one"
    " run a model passed a hand-written gate script while the official scorer found"
    " three hard clashes, two services with no geometry within the stated distance of"
    " the service entry, and per-row coverage barely half the stated threshold. The"
    " hand-written script had tested the parts that were easy to compute - the file"
    " exists, the geometry is manifold - instead of the parts the gate actually"
    " scores. Measure the gate's own quantity: the scripted distance from the stated"
    " coordinate, the stated coverage fraction over the stated span, the stated"
    " element count. Being 'connected' is not the same as being fed, and a service"
    " that crosses the hall can still miss every row end. If you cannot compute a"
    " leg exactly, say which leg and why - that beats a green tick you cannot"
    " justify."
    "\n\nThe drawing set is the contract, not a mood board. The labels, counts,"
    " positions and part numbers written on it have to be reproduced one for one,"
    " because the gates are measured against what is drawn, not against whether your"
    " model agrees with itself. Read the schedule for how many rows there are and how"
    " many units each row holds: a model with the right total in the wrong rows is"
    " wrong in every row, and a route that serves your own invented rows serves none"
    " of the drawn ones. When the drawing and your first reading disagree, re-read the"
    " drawing."
    "\n\nWhen you are done, reply with a short summary of what you delivered and"
    " where you put it."
)


def provider() -> tuple[str, dict[str, Any]]:
  """Returns the Pi provider name and its `models.json` entry.

  Returns:
    A tuple of `(provider_name, provider_config)`.
  """
  if os.environ.get("ANTHROPIC_BASE_URL"):
    return "anthropic", {
        "baseUrl": os.environ["ANTHROPIC_BASE_URL"],
        "api": "anthropic-messages",
        "apiKey": "$ANTHROPIC_API_KEY",
        "compat": COMPAT,
    }
  if os.environ.get("GOOGLE_GEMINI_BASE_URL"):
    return "google", {
        "baseUrl": os.environ["GOOGLE_GEMINI_BASE_URL"] + "/v1beta",
        "api": "google-generative-ai",
        "apiKey": "$GEMINI_API_KEY",
    }
  return "model", {
      "baseUrl": os.environ["MODEL_BASE_URL"],
      "api": "openai-completions",
      "apiKey": "$MODEL_API_KEY",
  }


def in_catalog(name: str, model: str, env: dict[str, str]) -> bool:
  """Checks whether Pi's built-in model catalog recognizes `model`.

  If the probe hangs beyond `CATALOG_TIMEOUT_SEC` or fails, returns `False` so
  the model is configured explicitly with `UNKNOWN_MODEL` limits.

  Args:
    name: Provider name (`anthropic`, `google`, or `model`).
    model: Model identifier string.
    env: Subprocess environment variables for the Pi CLI probe.

  Returns:
    `True` if `(name, model)` appears in `pi --list-models`, else `False`.
  """
  try:
    out = subprocess.run(
        [PI, "--offline", "--list-models", model],
        env=env,
        capture_output=True,
        text=True,
        timeout=CATALOG_TIMEOUT_SEC,
        check=False,
    ).stdout
  except (subprocess.TimeoutExpired, OSError):
    return False
  return any(line.split()[:2] == [name, model] for line in out.splitlines())


def configure(
    agent_dir: pathlib.Path,
    relay: taskbridge.Relay,
    session: taskbridge.Session,
    env: dict[str, str],
) -> tuple[str, str]:
  """Writes Pi's model, settings, and MCP configuration files for a task.

  Args:
    agent_dir: Directory where Pi reads its configuration files.
    relay: Local HTTP relay for the task container and its MCP servers.
    session: Active task session.
    env: Environment dictionary passed to Pi subprocesses.

  Returns:
    A tuple of `(provider_name, model_name)`.
  """
  model = os.environ["MODEL_NAME"]
  name, config = provider()
  models = agent_dir / "models.json"
  # `pi --list-models` reads the provider here
  models.write_text(json.dumps({"providers": {name: config}}))
  if not in_catalog(name, model, env):
    config["models"] = [{"id": model, "name": model, **UNKNOWN_MODEL}]
    models.write_text(json.dumps({"providers": {name: config}}))
  settings = {
      "defaultProjectTrust": "never",
      "quietStartup": True,
      "cacheWarming": "off",
      "retry": {"maxRetries": 6, "maxAgentDelayMs": 60000},
  }
  (agent_dir / "settings.json").write_text(json.dumps(settings))
  servers = {
      s["name"]: {"url": relay.mcp_url(s["name"]), "timeout": 300}
      for s in session.mcp_servers
  }
  (agent_dir / "mcp.json").write_text(json.dumps({"mcpServers": servers}))
  return name, model


class Run:
  """Tracks JSON stream events from a single Pi subprocess run."""

  def __init__(self, session: taskbridge.Session, model: str):
    self.session, self.model, self.replies = session, model, 0

  async def on_event(self, event: dict[str, Any]) -> None:
    """Processes a parsed JSON event from Pi, updating final message and usage.

    Args:
      event: Decoded JSON event dictionary from Pi's stdout stream.
    """
    kind, message = event.get("type"), event.get("message") or {}
    if kind == "message_end" and message.get("role") == "assistant":
      self.replies += 1
      text = "\n".join(
          b.get("text", "")
          for b in message.get("content") or []
          if b.get("type") == "text"
      )
      if text.strip():
        self.session.final_message = text.strip()
      if message.get("stopReason") == "error":
        await self.session.log(
            f"pi: model error: {message.get('errorMessage')}"
        )
      # input_tokens counts cached prompt tokens too
      u = message.get("usage") or {}
      await self.session.usage(
          self.model,
          input_tokens=u.get("input", 0)
          + u.get("cacheRead", 0)
          + u.get("cacheWrite", 0),
          output_tokens=u.get("output", 0),
          cache_read_tokens=u.get("cacheRead", 0),
      )
    elif kind == "tool_execution_start":
      await self.session.log(
          f"pi: {event.get('toolName')} {json.dumps(event.get('args'))[:300]}"
      )
    elif kind in ("auto_retry_start", "compaction_start"):
      await self.session.log(f"pi: {kind} {json.dumps(event)[:300]}")

  async def on_line(self, line: bytes) -> None:
    """Parses a raw stdout line from Pi as JSON and forwards it to `on_event`.

    Args:
      line: Raw line bytes emitted by Pi.
    """
    try:
      event = json.loads(line)
    except ValueError:
      return
    if isinstance(event, dict):
      await self.on_event(event)


async def solve(session: taskbridge.Session) -> str:
  """Runs Pi on a single task and returns the final response.

  Creates an isolated temporary agent home, configures tools and MCP servers to
  communicate through `taskbridge.Relay`, and feeds the task instruction on
  standard input.

  Args:
    session: Active task session.

  Returns:
    The final message produced by Pi.

  Raises:
    RuntimeError: If Pi exits before the model produces any assistant reply.
  """
  tool_src = pathlib.Path(__file__).with_name("tools") / "clash_check.py"
  if tool_src.is_file():
    try:
      await session.write_file(
          f"{session.workdir}/tools/clash_check.py", tool_src.read_text()
      )
    except Exception:  # a missing helper must not fail the run
      pass

  with tempfile.TemporaryDirectory(
      prefix="pi-",
      dir=os.environ.get("HOME") or None,
      ignore_cleanup_errors=True,
  ) as scratch:
    agent_dir, work = (
        pathlib.Path(scratch) / "agent",
        pathlib.Path(scratch) / "work",
    )
    agent_dir.mkdir()
    work.mkdir()
    async with taskbridge.Relay(session) as relay:
      env = relay.env()
      env.update(
          PI_CODING_AGENT_DIR=str(agent_dir),
          PI_OFFLINE="1",
          PI_SKIP_VERSION_CHECK="1",
          PI_TELEMETRY="0",
          NODE_NO_WARNINGS="1",
          TASK_WORKDIR=session.workdir,
      )
      name, model = await asyncio.to_thread(
          configure, agent_dir, relay, session, env
      )
      argv = [
          PI,
          "--print",
          "--mode",
          "json",
          "--no-session",
          "-nc",
          "-ns",
          "-np",
          "-na",
          "-e",
          str(HERE / "task-tools.ts"),
          "--provider",
          name,
          "--model",
          model,
          "--thinking",
          THINKING,
          "--append-system-prompt",
          NOTE.format(workdir=session.workdir),
      ]
      run = Run(session, model)
      done = await taskbridge.run_process(
          argv,
          session,
          env=env,
          cwd=str(work),
          on_line=run.on_line,
          stdin=session.instruction.encode(),
          stop_before_sec=STOP_BEFORE_SEC,
          log_prefix="pi: ",
      )
  if not run.replies:
    raise RuntimeError(
        f"pi exited with {done.code} before the model answered:"
        f" {done.stderr[-2000:]}"
    )
  return session.final_message or "(pi stopped without a final message)"
