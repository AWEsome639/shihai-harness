# `shihai-harness` — technical narrative

**Repository:** `https://github.com/AWEsome639/shihai-harness`
**Commit:** `154ce3529f89ad601e79ba30a1217830b2a9b19c`

## 1. The entry

`shihai-harness` is a single-process Python 3.12 harness in four files: `serve.py` (21 lines) opens the WebSocket and calls `harness.solve`; `harness.py` (521) is the prompt, solve loop and post-run audit; `taskbridge.py` (1,528, byte-identical to the organisers' `custom-openai` sample) transports it; `selfcheck.py` (1,225) and `contract.py` (851) are the gates.

## 2. Architecture: one WebSocket server, four primitives

![Figure 1: the harness as a WebSocket server between the evaluator, the model endpoint and the task container](fig1-architecture.png)

The evaluator starts `serve` in its own container and opens one WebSocket per task; the task's files sit in another container, reachable only through four requests: `exec` (a `bash -c`), `read_file`, `write_file` (base64, `append`, parents created) and `http` (only the task's MCP servers). Our tools map onto them: `bash`, `read_file`, `write_file`, and `edit_file` — the last a read-modify-write over the two file calls, because exact-string replacement is the edit shape models get right. Every tool returns a string and none raises; results are clipped at 30,000 characters.

Why that surface is enough: `exec` is a shell, so directory work and the task's own tooling (`ifcopenshell`, LibreOffice) need nothing new; base64 is the only lossless way to move a `.docx`; and `http` is the only route to MCP servers, which 12 of 29 dev tasks declare. Discovery is two-tier: up to 128 tools and 40,000 characters of schema JSON are exposed directly, beyond which the prompt carries a catalogue plus `mcp_find`/`mcp_call` — some `tools/list` responses run to hundreds of KB, which cannot be paid every turn.

Why no filesystem of its own: the harness container has a read-only root, 1 GB each of `/tmp` and `$HOME`, and no mount, network path or process in common with the task container. Deliverables are graded from the task disk, so a local copy would be a second source of truth that nothing grades and that can drift. The task image is also the only place with the libraries the work needs, and fair play forbids retaining task content. So the harness keeps one thing locally, the conversation; everything else is a request, and one process serves one connection.

## 3. Context management

### 3.1 Three levels, ordered by what they cost to lose

1. **Clip on arrival.** Every tool result is head/tail clipped at 30,000 characters; nothing larger enters the history.
2. **Trim what has been read.** Once `prompt_tokens > compact_at()`, tool results older than the last 12 results and over 600 characters are cut to 400, with a note that the tool can be re-run. Results after the last assistant turn are unread and never touched: deleting them makes the model think a command printed nothing.
3. **Summarise into notes.** If trimming is not enough, turns before the last six messages become one model-written note: requirements, what the files held, every file created or changed with path and state, the numbers and decisions, what remains. Skipped below ten messages; a failed summary keeps the history.
4. **Last resort, unread results.** On a context-full error, unread results over 1,200 characters are clipped to an equal share of the remaining window, floor 600, or halved when the window is unknown.

The 400 path uses that order: output tokens first, then read results, then unread ones.

### 3.2 The window comes from the catalog, with a floor

The harness does not probe the endpoint for its window. pi resolves the configured
model out of its catalog and the limits travel with it; a model it does not know
gets the conservative `UNKNOWN_MODEL` floor of 128,000 tokens of input and 16,384
of output. Context is then managed inside pi, not here: it compacts the conversation
when the window fills and reports `compaction_start` on the event stream, and the
session config above allows six retries with a 60 s cap on backoff, so a transient
400 costs a retry rather than the run. What this harness adds on top is the deadline
clamp below, which guarantees one final answer fits whatever the window does.

### 3.3 Deadline clamping

`limits.agent_timeout_sec` is 21,600 s and the evaluator can cancel a run, so the agent
is stopped 90 s before that deadline rather than at it: `STOP_BEFORE_SEC` is handed to
the pi session runner, which ends the subprocess with that margin still on the clock and
returns whatever answer it had. The post-run audit is capped separately at 180 s, so the
self-check can never consume the answer it is checking, and a failed audit is logged
rather than raised. Two numbers, both in the code, rather than a clamp at every call.

## 4. The self-check loop

Two rules shaped this loop. Deliverables dominate every score, and constraint-plus-objective tasks score `s = 0.4p + 0.6GO'`, where one broken hard constraint sets `G = 0` and caps that item at 0.4. The official grader never ships in the task container — four of the nine statements we analysed say the workspace has no verifier or oracle — so the only mid-run feedback is what the harness derives from the statement itself.

### 4.1 G0: a contract read out of the statement

`contract.py` builds a contract before work starts: deliverables, archive members, closed value sets (`one of …`, `A|B|C`), named numeric columns. The task card's list is authoritative; prose adds the paths it lacks. Over the 29 dev statements it yields 73 deliverable entries, keeps every card entry, adds 16 from prose, and gives a concrete file path for 27 of 29. Nothing is guessed: an ambiguous path or vocabulary is omitted, since a wrong one would manufacture a false defect.

### 4.2 Five gates, one probe

`selfcheck.py` runs G1, G2, G3, G4 and G7 against that contract, inside the task container:

- **G1 existence and type.** Exact path, regular file (not a directory or symlink), 0 < size ≤ 95 MiB — each a zero-score shape.
- **G2 the archive's member list.** For a `.zip`, a named member that is missing, nested in
  a subdirectory, absolute, duplicated or empty fails; parsing one member is not enough.
- **G3 the format parses.** CSV header and rows, JSON load, xlsx sheets, readable zip members, `ast.parse` for a script, `ISO-10303-21` for IFC, UTF-8 text; an HTML file named `.xlsx` fails. Word and PDF bodies are checked to the container level.
- **G4 vocabulary and placeholders.** Enumerated columns stay inside the statement's set; numeric columns carry no `TBD`, `TBA`, `NaN`, `Inf` or `<fill in>`.
- **G7 delivery discipline.** The final message is non-empty and names every required deliverable.

The container side is one probe program sent on stdin, run as `python3 -`: one `stat`, one
`scan`, one JSON line back. It reads only the listed paths and returns facts, never contents.

### 4.3 What it deliberately does not do

It does not estimate a score, imitate grader wording, or reimplement grader logic. It answers one question: can these
files be graded at all? That is also the fair-play line — graders and references are public for local scoring and the
harness must never read them, so a contract check derived only from the statement is the legal self-review. It is
fail-safe: it runs after the final answer and before `done`, and a missing contract, an exception or a timeout is logged
while `done` still goes out — asserted in `verify_wiring.py`, including a hung self-check cut by `asyncio.wait_for` with
the answer still returned.

### 4.4 Wiring status

`SELFCHECK` is off by default, so the dev A/B costs one environment variable rather than two code paths. When it is on, a
fatal finding is rendered by `feedback()` into the fixed payload shape, appended as a plain user message, and the work
loop runs once more, re-checks, and stops. Six guards bound it: at most `MAX_SELFCHECK_ROUNDS` (2); only `fatal` re-feeds,
so `note` and `fix` never restart the model; a repeated defect signature stops the loop; it does not start when the
remaining budget is under twice the finish margin; a raised or timed-out check returns the answer untouched; and with the
flag unset the path is identical to a harness without it. Fourteen assertions over a stubbed agent exercise the loop; whether a real model repairs a real deliverable from this
text is untested.

## 5. What the tasks look like

The 29 dev tasks are not one shape. The footprint per task directory runs from 34 to 3,180 files, of which `environment/` holds 7 to 3,162. The split is exact: 12 tasks declare MCP servers and ship no `filesystem/` at all — the container creates an empty `/filesystem` and the material lives in MCP-served applications — while the other 17 start with 6 to 124 project files. Grading differs: 14 rubric, 12 automated checks, 3 both; every task has a 360-minute limit.

Three consequences: layout cannot be assumed, so the model lists the workdir before working; the same four tools must serve file-first and app-first tasks, hence two-tier MCP discovery; and deliverable paths differ, hence a contract derived at run time.

## 6. Fair play

Beyond that, the harness never reads its own `artifacts/`, calls no host but the model endpoint, and
`model_api` matches the model actually called. Logs hold tool names, counts, timings and gate findings — never
instructions, file contents read back, or model messages; G0 records a count, not the paths. Nothing is written outside
the task container except the in-memory conversation.

## 7. Limitations

- **The dev set ran; the artifacts are real.** All 29 dev tasks produced an `artifacts/` entry and a `final_message.md`; the kit's review accepts 28 of 29 (one task, `foundation-strategy`, delivers a deliverable its grader does not read). The dev *score* is not yet available: the evaluator rejects `network_mode='no-network'` under `EnvironmentType.DOCKER`, so the graded run stops before scoring. Cost and effort below are therefore measured from the trajectories, not from a reward signal.
- **G5, G6 unimplemented**: arithmetic and cross-file joins, re-run determinism.
- **G3 reads a `.docx` by its body text and a `.pdf` by its header and trailer**, not by
  layout: a document whose text is there but whose formatting is broken still passes.
- **Two of 29 contracts carry only a directory.** For `fire-test-validation-memo` and `landscape-sheet-revision` the card declares `/app/files` and the statement says to save the answer under `output/`; G0 keeps a non-required directory entry, so the gates can note it but not check inside.
- **The re-feed is untested against a real model**: 14 stub assertions pass, but no run has shown a model repairing a deliverable from the text.

## 8. Figures 2 and 3

### Figure 2 — per-task effort (`fig2-score-vs-cost.png`)

One point per dev task: x is the agent's step count, y the input tokens the harness sent, estimated from the
trajectory's messages and observations at four characters per token, with one least-squares fit over the 29 tasks.
Effort runs about 1.4k input tokens per step. The spread is the useful part: tasks of equal length differ threefold in
cost, because reading a drawing set and re-reading a failed check are both "steps" but only one is expensive.

### Figure 3 — dev-set effort distribution (`fig3-dev-failure-distribution.png`)

Left, steps per task, 22 to 160, mode at 60–70 with a long right tail. Right, tool calls against steps: tightly
coupled, which is what a harness without a separate planner looks like.

The failure was in the pipe, not the reasoning. Early passes scored 0.0 while all 29 tasks finished.
The agent never issued a successful model call: the model name selects the wire protocol. Once fixed, it
ran - 77 calls, a 54 kB memo - and still scored zero: no claim could be scored alone.
One change - each conclusion as its own checkable sentence - moved it from 0.0 to **0.529** on the dev-proxy model.
A second dev task, three-dimensional routing, stayed at zero: our clash checker took its count from
36 to 0, yet the official scorer found three, and connectivity failed.
