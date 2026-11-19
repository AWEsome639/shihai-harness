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
import posixpath
import re
import shutil
import subprocess
import tempfile
import time
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
# The independent review stage (AURA's Verify -> Repair). A second Pi session
# with none of the worker's history opens the deliverables against the task and
# answers PASS or one line per gap; a non-PASS verdict buys one repair session.
# It is opportunistic: it runs only on the time the worker did not use, so a
# task that needs every second is never cut short for it. `REVIEW=0` turns it
# off.
REVIEW = os.environ.get("REVIEW", "1").strip().lower() not in (
    "0", "off", "false", "no", "")
REVIEW_MAX_SEC = 240
REVIEW_MIN_SEC = 60
REVIEW_MAX_GAPS = 20
REPAIR_MIN_SEC = 120
# Deliverable-first (AURA's Plan stage, made load-bearing). The plan session
# spends a fixed slice of the budget naming every file the task wants and the
# least it may contain; the harness then puts a skeleton at every one of those
# paths before the worker writes a line, so a run that dies mid-thought still
# leaves a gradable file behind. `PLAN=0` turns the whole stage off.
PLAN = os.environ.get("PLAN", "1").strip().lower() not in (
    "0", "off", "false", "no", "")
PLAN_NAME = ".plan.md"
PLAN_SHARE = 0.10
PLAN_MAX_SEC = 1200
PLAN_MIN_SEC = 60
PLAN_MAX_FILES = 12
SKELETON_MIN_SEC = 60
# Hard stop for one task, whatever budget the evaluator hands out. A dev run
# once inherited a six-hour allowance and a single task sat for two hours; the
# skeletons are on disk long before this, so the cap only costs polish.
MAX_RUN_SEC = float(os.environ.get("MAX_RUN_SEC", "2700"))
# Request features the Google Cloud route for Claude refuses. pi leaves them
# out, and the Anthropic API works without them too.
PLANNER_NOTE = """You are the planning engineer on this task. Do NOT create or \
modify any deliverable and do not start the work: plan only.

Read the task, then every file it points to. Then write the plan to {plan} \
with the write tool. Keep it short and concrete:
- the deliverables the task names, with their exact paths,
- what a reviewer checks in each one (sections, values, units, dates, names),
- the hard constraints that must not be broken.

The LAST section of the file must be exactly this heading and one line per \
file, because a program reads it:

## 交付物清单
- <path> | <the minimal acceptable content, in one line>

List every file the task asks for, even the ones you are unsure about. Nothing \
after that section.

Then answer with one short paragraph naming the files you listed."""

WORK_PLAN_HEAD = """

---

## The plan

A planning pass wrote the plan to {plan}. Read it first with the read tool.

The files in its 交付物清单 already exist as skeletons at their exact paths. \
Your FIRST action, before any further reading or analysis, is to open each one \
and replace it with real content - even a short but correct version. A skeleton \
left in place scores zero, and so does a deliverable that is still only in your \
head when the time runs out. Improve them from there."""

REVIEWER_NOTE = """You are the independent reviewer on this task. You did \
not do the work, and you must not create, edit or delete any file: read only.

The task text you were given is followed by the worker's closing summary. Open \
the task again, then open every deliverable it names with bash and the read \
tool. For each one check that
- it exists at the exact path the task names and opens in the stated format,
- every required section, table, value, unit, date and name the task asks for \
is present, not paraphrased away or left only in the chat answer,
- nothing the task asked for is missing and no hard constraint is broken.

The final message is read as part of the answer, so a point that exists only \
in a file and nowhere in the task's own vocabulary still counts as missing.

Answer with exactly PASS if nothing is missing. Otherwise write one line per \
gap, in this shape:
<path> | <what is wrong or missing> | <the concrete fix>
List only gaps that would cost score, not style preferences. Do not fix \
anything yourself and do not rewrite the work."""

REVIEWER_PROMPT = """

---

## The worker's closing summary

{summary}

Open the deliverables now and review them against the task above."""

REPAIR_PROMPT = """

---

## An independent reviewer opened your deliverables and found these gaps

{gaps}

Fix each gap in the files, reopen them to confirm the fix, then answer with \
your updated summary of what you delivered and where it is."""

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
    "\n\nWhat is installed: run `pip list` before you decide a format is impossible."
    " The task images usually ship openpyxl, reportlab (write PDF), python-docx"
    " (read and write .docx), pdfplumber and pymupdf (read PDF), ifcopenshell,"
    " matplotlib, numpy, scipy, shapely, trimesh, ezdxf, and the `zip` and `unzip`"
    " binaries. A missing capability is almost always a missing import rather than a"
    " missing library - check the list before writing a format off."
    "\n\nMost of these tasks name an ARCHIVE as the deliverable - `output/submission.zip`"
    " or similar. If the instruction names a .zip, the zip itself is what is graded:"
    " a directory of loose files next to it scores zero. Build the archive with `zip -r`"
    " (or python zipfile), list its members with `unzip -l`, and re-check that the"
    " archive exists and is non-empty before you finish. Rebuild it after every edit -"
    " editing a file inside an already-built archive does not update the archive."
    "\n\nBefore you read the source documents, look at what the workspace"
    " already holds. If a file the instruction names as a deliverable already"
    " exists when you start, read its first twenty lines: `Submission stub`,"
    " `replace this whole file`, or a module-level `raise SystemExit` means it"
    " is a stub, and while it stands your score is zero however good the work"
    " in your head is. Overwrite it with a complete, runnable file in your"
    " first few steps - even one that writes a small but valid deliverable -"
    " and improve it from there. One run spent its entire budget decoding the"
    " drawing sheets and left the stub in place; the grader recorded exactly"
    " that and scored 0, so every reading it had bought was lost."
    "\n\nIf the deliverable is a script that writes another file, run it the"
    " way the grader will, with the command the instruction gives, and confirm"
    " the file it should write appears and parses. A script you never executed"
    " is a script you never knew worked: an unparsed IFC, a zero-byte model and"
    " the shipped stub all look the same to the grader."
    "\n\nWrite a first, valid deliverable EARLY and then improve it. A stub the"
    " instruction pre-creates and you never replace scores exactly zero, and so does"
    " a deliverable you were still about to write when time ran out. So: as soon as"
    " you know the output format, write one complete file that satisfies it, confirm"
    " it exists, and only then iterate on quality. Every later pass overwrites a file"
    " that already exists, never a stub that does not."
    "\n\nWhen the workspace ships source documents - a standard, a validation"
    " note, a contract - read them before drafting, and make the deliverable"
    " state every specific number, qualification and conclusion they contain,"
    " one per sentence. A rubric grades each such point separately, so a memo"
    " that reads well but omits half of them scores near zero: one run wrote a"
    " polished memo that carried one of forty-eight required points. Cover the"
    " set first, then improve the prose."
    "\n\nAn instruction that names several files wants all of them. Build every one"
    " to something real in your first passes - a minimal but valid model.ifc, a"
    " two-line reconciliation.json - and deepen them afterwards. One run spent"
    " seventy-four minutes on the drawing set and delivered one of three named files;"
    " the grader reads the file that is missing, not the work behind it."
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
    "\n\nWhere the service entry is, you have to read off the drawing. The task"
    " does not hand you its coordinates, and a run that assumes a location will"
    " assume the wrong wall: one recent attempt announced an east-wall entry at"
    " x=35500 mm when the entry was at (200, 3360) on the other side, then built a"
    " route that was internally consistent, clash-free, and worth zero. So give"
    " every number you use a provenance. Put the entry in one place at the top of"
    " your solution as ENTRY = (x, y) with a comment naming the drawing and the"
    " symbol you read it from, have every checker import that value rather than"
    " redefining it, and print it on the first line of every run. If you cannot"
    " read it, still deliver a complete submission: state the value you inferred,"
    " say what made the reading uncertain, and build the route anyway. Never"
    " answer a task with nothing. An approximate route that is honest about its"
    " assumptions scores something; an empty submission scores zero, and one"
    " recent run delivered exactly that - it could not read the entry, so it"
    " stopped, and the only thing the scorer received was nothing at all."
    "\n\nNo image library is not the same as no image. A 1600x1100 8-bit RGB PNG"
    " will decode with zlib and numpy alone; slice the corner where the entry"
    " symbol sits, print it as an ASCII mask, and cross-check the coordinates"
    " against the drawing's border dimensions. One run gave up here, claiming OCR"
    " was impossible, and then invented the coordinate instead."
    "\n\nMeasure the distance the gate measures. For every service, print the"
    " distance from ENTRY to its nearest element; anything over 2000 mm is a fail,"
    " and you may not write that all criteria pass while that number is over the"
    " limit. Being one connected component is not the connectivity gate - the gate"
    " wants a run fed by the entry, so print the component id of the element"
    " nearest ENTRY and require it to match the component carrying the rows."
    " Take the row count from the drawing as well: print the rows you read, the"
    " cabinets per row, and each row's y span, and if your total differs from the"
    " stated count, your coverage figure is measuring rows you invented."
    "\n\nLabel every gate you report as either read from an artifact or assumed."
    " An assumed gate is an unverified gate, and an unverified gate cannot be"
    " counted among the ones that pass. Sixteen of sixteen passing is only"
    " meaningful if each of the sixteen was measured against something outside"
    " your own script."
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
    self.last = ""

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
        # `last` is this run's own final text; `session.final_message` is the
        # task's answer, which a review run must not take over.
        self.last = text.strip()
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


def _audit(session, loop, final_message):
  """Builds the contract from the instruction and runs the four gates.

  Returns `(contract, defects)`; `defects` is a list of defect dicts, or `None`
  when the probe could not run at all.
  """
  import contract as _contract
  import selfcheck as _selfcheck

  def run_sync(cmd):
    """Runs one shell command in the container for the synchronous probe."""
    future = asyncio.run_coroutine_threadsafe(
        session.exec(cmd, timeout_sec=120), loop
    )
    result = future.result(timeout=180)
    return int(result.get("exit_code") or 0), str(result.get("output") or "")

  spec = _contract.extract(session.instruction)
  # Some instructions name a directory instead of a file ("save the files that make
  # up your answer under `output/`"). The extractor only follows named paths, so
  # synthesise that one deliverable rather than auditing nothing at all.
  # The gates need named files; a directory cannot satisfy G1 (a regular file is
  # required). When the instruction only says "save your files under output/",
  # check that directory for entries instead of pretending it is a deliverable.
  dir_output = "output/" in session.instruction
  defects = list(_selfcheck.selfcheck(
      run_sync, spec, final_message=final_message
  ) or [])
  if dir_output and not (spec or {}).get("deliverables"):
    code, out = run_sync("ls -A output 2>/dev/null | wc -l")
    try:
      count = int(str(out).strip().split()[0])
    except Exception:
      count = -1
    if count <= 0:  # only a problem when the directory is missing or empty
      defects.append({
          "gate": "G1",
          "path": "output/",
          "status": "fail",
          "detail": f"output/ holds {count} entr(y|ies)",
      })
  return spec, defects


def _log_audit(spec, defects):
  """Prints one line per gate, so a run log says what the audit found."""
  if defects is None:
    print("selfcheck: probe unavailable", flush=True)
    return
  deliverables = (spec or {}).get("deliverables") or []
  for _d in deliverables:
      print(f"contract: {_d.get('path')} kind={_d.get('kind')} required={_d.get('required')}", flush=True)
  print(
      f"selfcheck: {len(deliverables)} deliverable(s), {len(defects)} defect(s)",
      flush=True,
  )
  for defect in defects[:20]:
    if not isinstance(defect, dict):
      print(f"  {defect}", flush=True)
      continue
    print(
        "  gate {} {} {}: {}".format(
            defect.get("gate"),
            defect.get("path"),
            defect.get("status"),
            str(defect.get("detail"))[:160],
        ),
        flush=True,
    )


def plan_seconds(session: taskbridge.Session) -> float:
  """Returns the seconds the planning session may spend, 0 to skip it.

  Args:
    session: Active task session.

  Returns:
    `PLAN_SHARE` of the time left, clamped to `PLAN_MIN_SEC..PLAN_MAX_SEC`, and
    never so much that the work phase and the wrap-up cannot fit.
  """
  left = session.time_left()
  if left is None:
    return float(PLAN_MIN_SEC)
  room = left - STOP_BEFORE_SEC - 300
  if room < PLAN_MIN_SEC:
    return 0.0
  return max(float(PLAN_MIN_SEC),
             min(float(PLAN_MAX_SEC), PLAN_SHARE * left, room))


DELIVERABLE_RE = re.compile(
    r"([A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*"
    r"\.(?:zip|docx|pdf|md|json|csv|xlsx|py|ifc|txt|dxf|dwg|xlsm|pptx))")


def plan_paths(text: str) -> list[str]:
  """Extracts the deliverable paths a plan lists.

  Args:
    text: The plan file's contents.

  Returns:
    The distinct paths, in plan order, capped at `PLAN_MAX_FILES`. A token
    without a directory separator is kept only when it is a deliverable-looking
    name at the start of a bullet, so prose does not become a file list.
  """
  paths: list[str] = []
  for match in DELIVERABLE_RE.finditer(text or ""):
    path = match.group(1).strip(".-")
    if "/" not in path:
      continue
    if path not in paths:
      paths.append(path)
  return paths[:PLAN_MAX_FILES]


def skeleton_for(path: str) -> tuple[str, str] | None:
  """Returns how to stub `path`, or None when its extension has no stub.

  Args:
    path: A deliverable path from the plan.

  Returns:
    `(kind, payload)`: kind `text` writes the payload as the file, `zip` packs
    a README into the named archive, `docx`/`pdf` build a one-page document.
  """
  ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
  if ext in ("md", "txt"):
    return "text", f"# {path.rsplit('/', 1)[-1]}\n\nPlaceholder written by the harness; the worker replaces it.\n"
  if ext in ("json",):
    return "text", "{}\n"
  if ext in ("csv",):
    return "text", "item,value\n"
  if ext in ("py",):
    return "text", "\"\"\"Placeholder written by the harness; the worker replaces it.\"\"\"\n"
  if ext in ("zip",):
    return "zip", "README.md"
  if ext in ("docx",):
    return "docx", "Placeholder written by the harness; the worker replaces it."
  if ext in ("pdf",):
    return "pdf", "Placeholder written by the harness; the worker replaces it."
  return None


def plan_target(path: str, workdir: str) -> str:
  """Resolves a planned deliverable path to an absolute path inside `workdir`.

  Plans sometimes spell the deliverable as a container path without its leading
  slash (`app/files/output/memo.md` for a workdir of `/app/files`). Joining that
  blindly nests the file twice (`/app/files/app/files/output/...`), so a leading
  copy of the workdir is stripped, and a target that still lands outside the
  workdir falls back to the file's basename.

  Args:
    path: The path the plan listed.
    workdir: The task container's working directory.

  Returns:
    An absolute path that starts with `workdir`.
  """
  wd = posixpath.normpath(workdir)
  root = wd.lstrip("/")
  rel = str(path or "").strip().lstrip("/")
  if rel == root:
    return wd
  if rel.startswith(root + "/"):
    rel = rel[len(root) + 1:]
  target = posixpath.normpath(posixpath.join(wd, rel)) if rel else wd
  if target != wd and not target.startswith(wd + "/"):
    print(f"plan: {path} escapes {wd}; using its basename", flush=True)
    target = posixpath.join(wd, posixpath.basename(rel))
  return target


async def ensure_deliverables(
    session: taskbridge.Session, paths: list[str]
) -> list[str]:
  """Writes a skeleton at every listed path that is missing or empty.

  Used twice: right after the plan, so the run always has files on disk, and
  again after the work session, for anything the worker still did not deliver.

  Args:
    session: Active task session.
    paths: Deliverable paths from the plan, relative to the workdir or absolute.

  Returns:
    The paths a skeleton was written at. Any failure is logged and skipped: a
    stub that cannot be written must not end the run.
  """
  written: list[str] = []
  for path in paths:
    target = plan_target(path, session.workdir)
    try:
      result = await session.exec(f"test -s '{target}' && echo yes || echo no")
      if str(result.get("output", "")).strip().endswith("yes"):
        continue
      stub = skeleton_for(target)
      if stub is None:
        continue
      kind, payload = stub
      parent = target.rsplit("/", 1)[0] or session.workdir
      if kind == "text":
        await session.write_file(target, payload)
      elif kind == "zip":
        await session.exec(
            f"mkdir -p '{parent}/.skeleton' && printf '%s\\n' 'Placeholder written by the harness; the worker replaces it.' > '{parent}/.skeleton/{payload}'"
            f" && cd '{parent}/.skeleton' && zip -q -r '{target}' {payload}"
        )
      elif kind in ("docx", "pdf"):
        script = (
            "import docx\nd=docx.Document()\nd.add_paragraph(%r)\nd.save(%r)"
            % (payload, target)
            if kind == "docx"
            else "from reportlab.pdfgen import canvas\nc=canvas.Canvas(%r)\nc.drawString(72,720,%r)\nc.save()"
            % (target, payload)
        )
        await session.exec(f"python3 -c {script!r}")
      written.append(path)
      print(f"plan: skeleton {path}", flush=True)
    except Exception as exc:  # a stub is a bonus, never a failure
      print(f"plan: skeleton skipped for {path} ({exc!r})", flush=True)
  return written


def review_budget(session: taskbridge.Session) -> float:
  """Returns the seconds a review session may spend, 0 when it must not run.

  The budget is what the worker left over, minus the wrap-up reserve, so the
  review never shortens the work: on a task that uses every second it is 0 and
  the stage is skipped.

  Args:
    session: Active task session.

  Returns:
    The spare seconds, capped at `REVIEW_MAX_SEC`.
  """
  left = session.time_left()
  if left is None:
    return float(REVIEW_MAX_SEC)
  return max(0.0, min(float(REVIEW_MAX_SEC), left - STOP_BEFORE_SEC - 30))


def review_gaps(verdict: str) -> list[str]:
  """Parses a reviewer verdict into concrete gap lines.

  Args:
    verdict: The reviewer session's final text.

  Returns:
    One string per gap, empty when the reviewer answered PASS or wrote nothing,
    capped at `REVIEW_MAX_GAPS`. Bullet and numbering markers are stripped so
    the repair prompt reads as a plain list.
  """
  text = (verdict or "").strip()
  if text.strip("*_. \r\n\t").upper() in ("", "PASS"):
    return []
  gaps: list[str] = []
  for line in text.splitlines():
    line = line.strip().lstrip("-*•").strip()
    line = re.sub(r"^\d+[.)]\s*", "", line).strip()
    if line and line.upper() != "PASS":
      gaps.append(line[:400])
  gaps = gaps[:REVIEW_MAX_GAPS]
  if gaps and not any(" | " in gap for gap in gaps):
    # A reviewer that answered in prose rather than in the asked-for
    # `<path> | <gap> | <fix>` shape still read the files, and its prose is
    # often the actionable part ("there is no submission.zip anywhere"): keep
    # it as one brief rather than dropping the only signal the stage produced.
    gaps = [" ".join(gaps)[:1200]]
  return gaps


def reviewer_stdin(instruction: str, summary: str) -> str:
  """Builds the reviewer's task text: the task plus what the worker claims.

  Args:
    instruction: The task instruction, verbatim.
    summary: The worker's closing summary.

  Returns:
    The reviewer's standard input.
  """
  return instruction + REVIEWER_PROMPT.format(
      summary=summary or "(the worker gave no summary)")


def repair_stdin(instruction: str, gaps: list[str]) -> str:
  """Builds the repair session's task text: the task plus the gap list.

  Args:
    instruction: The task instruction, verbatim.
    gaps: The reviewer's gap lines.

  Returns:
    The repair session's standard input.
  """
  return instruction + REPAIR_PROMPT.format(
      gaps="\n".join(f"- {gap}" for gap in gaps))


async def verify_and_repair(
    session: taskbridge.Session,
    run_pi: Any,
    final: str,
) -> str:
  """Runs Verify, then at most one Repair, on the answer the worker returned.

  The reviewer is a fresh session: it carries none of the worker's history, so
  it opens the deliverables instead of trusting a summary that says they are
  done. Its verdict is `PASS` or one line per gap; gaps buy one repair session
  whose final message becomes the answer. Both stages spend only the time the
  worker left over, so this never shortens a task that needs every second.

  Args:
    session: Active task session.
    run_pi: The stage runner built by `solve`, `(text, note, stop_before,
      max_sec) -> (Run, Finished)`.
    final: The worker's closing summary, which is also the answer if no repair
      runs.

  Returns:
    The final answer: the repaired summary when a repair session ran and
    produced one, otherwise `final`.
  """
  budget = review_budget(session)
  if budget < REVIEW_MIN_SEC:
    print(f"review: {int(budget)} s spare; skipped", flush=True)
    return final
  print(f"review: budget {int(budget)} s", flush=True)
  worker_final = session.final_message
  reviewer, _ = await run_pi(
      reviewer_stdin(session.instruction, final),
      REVIEWER_NOTE,
      STOP_BEFORE_SEC + 20,
      max_sec=budget,
  )
  # The reviewer's text is a verdict, not the task's answer.
  session.final_message = worker_final
  if not reviewer.replies:
    print("review: no verdict; skipped", flush=True)
    return final
  gaps = review_gaps(reviewer.last)
  if not gaps:
    print("review: PASS", flush=True)
    return final
  print(f"review: {len(gaps)} gap(s)", flush=True)
  for gap in gaps:
    print(f"review: {gap}", flush=True)
  left = session.time_left()
  if left is not None and left < REPAIR_MIN_SEC + STOP_BEFORE_SEC:
    print("review: no time to repair", flush=True)
    return final
  print("review: repair session", flush=True)
  repaired, _ = await run_pi(
      repair_stdin(session.instruction, gaps),
      NOTE.format(workdir=session.workdir),
      STOP_BEFORE_SEC,
      max_sec=None if left is None else max(0.0, left - STOP_BEFORE_SEC),
  )
  return repaired.last or session.final_message or final


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
      base = [
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
      ]

      started = time.monotonic()

      async def run_pi(
          task_text: str,
          note: str,
          stop_before: float,
          max_sec: float | None = None,
      ) -> tuple[Run, taskbridge.Finished]:
        """Runs one Pi session with `note` appended to its system prompt.

        `stop_before` protects the task deadline, which is the only bound a
        stage should need. A stage with its own budget also passes `max_sec`:
        `taskbridge.time_share` shrinks a large reserve to a quarter of the
        time left, so a phase deadline cannot be expressed through
        `stop_before` alone - without this the plan could take three quarters
        of the task.

        Args:
          task_text: The session's standard input (the task, plus any role
            text the stage adds).
          note: The stage's system prompt addition.
          stop_before: Seconds to reserve before the task deadline.
          max_sec: Wall-clock cap for this phase, if it has one.

        Returns:
          The stream tracker and the finished process; `done.code` is -1 when
          the phase cap stopped it.
        """
        run = Run(session, model)
        call = taskbridge.run_process(
            base + ["--append-system-prompt", note],
            session,
            env=env,
            cwd=str(work),
            on_line=run.on_line,
            stdin=task_text.encode(),
            stop_before_sec=stop_before,
            log_prefix="pi: ",
        )
        # Every phase is capped twice: by its own budget and by the task's hard
        # stop, so an evaluator that hands out six hours cannot hold the run.
        cap = MAX_RUN_SEC - (time.monotonic() - started)
        budget = max(0.0, cap if max_sec is None else min(max_sec, cap))
        if budget <= 0:
          print("phase: task cap reached", flush=True)
          return run, taskbridge.Finished(-1, "task cap reached", True)
        try:
          return run, await asyncio.wait_for(call, budget)
        except asyncio.TimeoutError:
          print(f"phase: {int(budget)} s budget spent", flush=True)
          return run, taskbridge.Finished(-1, "phase budget spent", True)

      # PLAN: name every deliverable and its least acceptable content.
      plan_path = f"{session.workdir}/{PLAN_NAME}"
      plan_text = ""
      budget = plan_seconds(session) if PLAN else 0.0
      if budget >= PLAN_MIN_SEC:
        left = session.time_left()
        print(f"plan: budget {int(budget)} s", flush=True)
        await run_pi(
            session.instruction,
            PLANNER_NOTE.format(plan=plan_path),
            STOP_BEFORE_SEC,
            max_sec=budget,
        )
        try:
          plan_text = (await session.read_file(plan_path)).decode(
              "utf-8", "replace")
        except Exception as exc:  # no plan is a reason to work, not to stop
          print(f"plan: unreadable ({exc!r})", flush=True)
        files = plan_paths(plan_text)
        print(f"plan: {len(files)} deliverable(s) listed: {files}", flush=True)
        # The worker gets files to improve, never an empty output directory.
        await ensure_deliverables(session, files)

      work_text = session.instruction
      if plan_text:
        work_text += WORK_PLAN_HEAD.format(plan=plan_path)
      worked, done = await run_pi(
          work_text,
          NOTE.format(workdir=session.workdir),
          STOP_BEFORE_SEC,
      )
      if not worked.replies:
        raise RuntimeError(
            f"pi exited with {done.code} before the model answered:"
            f" {done.stderr[-2000:]}"
        )
      final = session.final_message or "(pi stopped without a final message)"
      # Hard assertion: every listed deliverable exists and is non-empty when
      # the worker stops. A run that answered but left a path empty scores the
      # same as one that never answered, so fill what is still missing.
      files = plan_paths(plan_text) if plan_text else []
      if files:
        left = session.time_left()
        if left is None or left > STOP_BEFORE_SEC + SKELETON_MIN_SEC:
          await ensure_deliverables(session, files)
        present = await session.exec(
            " ; ".join(
                "test -s '%s' && echo '%s +'" % (
                    plan_target(f, session.workdir), f)
                for f in files
            ) + " ; true"
        )
        print(f"plan: delivered {str(present.get('output', '')).strip()!r}",
              flush=True)
      if REVIEW:
        try:
          final = await verify_and_repair(session, run_pi, final)
        except Exception as exc:  # a broken review must never lose the answer
          print(f"review: skipped ({exc!r})", flush=True)
  # Post-run audit: the four gates the narrative names. Costs one container
  # round trip of shell and cannot fail the task - a missing deliverable is
  # reported in the log, never raised, so the answer is always returned.
  try:
    loop = asyncio.get_running_loop()
    spec, defects = await asyncio.to_thread(_audit, session, loop, final)
    _log_audit(spec, defects)
  except Exception as exc:  # an audit problem must never lose the answer
    print(f"selfcheck: skipped ({exc!r})", flush=True)
  return final
