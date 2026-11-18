"""G0 — read the deliverable contract out of a task instruction.

`selfcheck.py` checks whatever a `contract.json` says the grader will look at,
so the contract has to be complete before any gate means anything
(`TASK-REQUIREMENTS.md` §5.3, G0). This module derives that contract from the
two legal sources the harness has before the run starts:

    contract = contract.extract(instruction, task_card)

* `instruction` is the task statement text (the `instruction` field of
  `task_cards.jsonl`);
* `task_card` is that same row, or `None`. Its `deliverables`, `workdir`,
  `final_message` and `input_files` are authoritative — the instruction is only
  used to *add* what the card does not carry.

Nothing here reads a task directory, a test, a reference answer or a grading
script: the input is prose plus public task-card metadata, and the output is a
`contract.json` body (`TASK-REQUIREMENTS.md` §5.2) with these keys:

    task_id, workdir      copied from the task card when it is given
    deliverables          [{"path", "kind", "required"}]
    archives              [{"path", "members", "allow_extra"}] for every .zip
    secondary             [{"kind": "final_message"}] when the card grades it
    vocab                 {"<column>": [allowed values]}
    numeric_cols          ["<column>"] — only where the instruction says so
    banned_tokens         the frozen placeholder list below
    units                 {"project"/"placement"/...: "<unit>"}
    runtime_limit_s       when the instruction states a wall-clock limit

Three rules shape every extractor:

1. **The card wins.** A path is never invented to disagree with a declared
   deliverable: when the instruction says `output/submission.zip` and the card
   says `/filesystem/<task>/output/submission.zip`, the card path is kept and
   the relative spelling is not added as a second file. The card's
   `input_files` are subtracted, so a document that is read never becomes a
   document that is written.
2. **A zip member is not a deliverable.** Files the instruction lists *inside*
   `submission.zip` are archive members (the card grades the zip); they are
   recorded under `archives`, not as files beside the archive.
3. **Silence beats a guess.** A key is omitted unless a pattern is unambiguous,
   because a false deliverable or a wrong vocabulary turns into a false defect
   in `selfcheck.py`, which costs the model a round for nothing. So member
   lists must match the count the instruction states, `numeric_cols` is only
   filled from an explicit "the numeric columns ... reads" enumeration, and a
   path the instruction calls *optional* is carried with `required: False`.

Every extractor is a pure function of the text, so the same instruction always
yields the same contract.
"""

from __future__ import annotations

import posixpath
import re

__all__ = [
    "BANNED_TOKENS",
    "KINDS",
    "extract",
    "kind_of",
]

# Placeholder cells (`TASK-REQUIREMENTS.md` §4, "占位符"). Fixed rather than
# extracted: a task statement never has to say that `TBD` is not a number.
BANNED_TOKENS = (
    "TBD",
    "TBA",
    "to be determined",
    "to be advised",
    "NaN",
    "Inf",
    "XXX",
    "<fill in>",
    "N/A",
)

# Extension -> kind. The probe in `selfcheck.py` dispatches on the extension, so
# `kind` is for humans and for the archive/`secondary` rules, not for dispatch.
KINDS = {
    ".py": "py",
    ".csv": "csv",
    ".json": "json",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".docx": "docx",
    ".zip": "zip",
    ".ifc": "ifc",
    ".md": "md",
    ".txt": "txt",
    ".pdf": "pdf",
    ".dxf": "dxf",
}

_EXT = "|".join(ext.lstrip(".") for ext in KINDS)

# A path as written in prose: word-ish segments, a known extension. A file name
# may contain spaces (`Memo to Callaway.docx`), so up to four space-separated
# words are allowed at the tail; the sentence shapes below bound where a path
# can start, so prose cannot be swallowed whole.
_SEGMENT = r"[A-Za-z0-9_.\-]+"
_TAIL = r"%s(?:\s%s){0,3}" % (_SEGMENT, _SEGMENT)
_FULL_PATH = re.compile(
    r"^(?:/|\./)?(?:%s/)*%s\.(?:%s)$" % (_SEGMENT, _TAIL, _EXT)
)
_ABS_PATH = re.compile(
    r"(?<![\w./\-])/(?:%s/)+%s\.(?:%s)\b" % (_SEGMENT, _TAIL, _EXT)
)
_TICK = re.compile(r"`")
_TICK_PATH = re.compile(r"^(?:/|\./)?(?:%s/)*%s\.(?:%s)$" % (_SEGMENT, _TAIL, _EXT))

# "Save it as X", "Produce X", "Hand back X", "write ... to X", "named X".
# A deliverable root named in plain prose: "One script at output/solution.py".
_ROOT_PATH = re.compile(
    r"(?<![\w/])((?:output|submission)/%s\.(?:%s))\b" % (_TAIL, _EXT)
)
_VERB = re.compile(
    r"(?i)\b(save[sd]?|saving|write|writes|written|produce[sd]?|producing"
    r"|hand\s+back|return|submit(?:s|ted)?|named)\b"
)
_VERBISH = re.compile(
    r"(?i)\b(save[sd]?|saving|write|writes|written|produce[sd]?|produced"
    r"|deliver(?:s|ed)?|hand\s+back|submit(?:s|ted)?|return|named"
    r"|contain(?:s|ing)?)\b"
)
# A file the statement asks for but does not grade ("optional side file").
_OPTIONAL = re.compile(
    r"(?i)\boptional\b|\bnot a gate\b|\bgate failure\b|\babsence of\b"
    r"|\byou may\b"
)

# Directories whose contents are inputs, not deliverables.
_INPUT_DIRS = frozenset(
    """
    input inputs templates template docs doc handover reference references
    library project site standards drawings schedules correspondence criteria
    geotech vendor_data climate economics water civil cost owner design
    commissioning procurement review contract index source sources assets
    """.split()
)

# Instruction prose that names a file without delivering one.
_NOT_DELIVERABLES = frozenset(
    """
    task.md readme.md agent_view.json oracle_client.py example.ifc
    """.split()
)

_NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10,
}
_MEMBER_COUNT = re.compile(
    r"(?i)(?:exactly\s+these|these|the)\s+(%s|\d{1,2})\s+files?\b"
    % "|".join(_NUMBER_WORDS)
)
_HEADING = re.compile(r"\n#{1,3} ")
# Words that glue a multi-word file name together: "Memo to Callaway.docx".
_CONNECTOR = re.compile(r"^(?:to|of|and|for|the|in|a|an|at|by|on|with|from)$")
_UNIT_WORDS = {
    "millimetre": "mm", "millimetres": "mm", "millimeter": "mm",
    "millimeters": "mm", "mm": "mm",
    "centimetre": "cm", "centimetres": "cm", "centimeter": "cm",
    "centimeters": "cm", "cm": "cm",
    "metre": "m", "metres": "m", "meter": "m", "meters": "m", "m": "m",
    "inch": "in", "inches": "in", "in": "in",
    "foot": "ft", "feet": "ft", "ft": "ft",
}


def kind_of(path: str) -> str:
  """Returns the contract `kind` for a deliverable path.

  Args:
    path: Deliverable path, absolute or relative.

  Returns:
    One of `KINDS.values()`, or "" when the path has no extension this module
    knows (a bare directory such as `/app/files`).
  """
  return KINDS.get(posixpath.splitext(str(path or "").strip())[1].lower(), "")



def _drop_duplicate_paths(deliverables):
  """Drops a path that repeats the one before it under a doubled first segment.

  One dev card lists the same file twice, once as `/filesystem/<task>/output/x.zip`
  and once as `/filesystem/filesystem/<task>/output/x.zip`. Keeping both would make
  G1 demand a path the container never creates, which is a false defect, so the
  collapsible duplicate goes.
  """
  kept = []
  seen = set()
  for item in deliverables or []:
    path = str(item.get("path") or "")
    parts = [p for p in path.split("/") if p]
    while len(parts) > 1 and parts[0] == parts[1]:
      parts.pop(0)
    key = "/" + "/".join(parts)
    if key in seen:
      continue
    seen.add(key)
    kept.append(item)
  return kept


def extract(instruction: str, task_card: dict | None = None) -> dict:
  """Derives a `contract.json` body from a task instruction and its card.

  Args:
    instruction: The task statement text.
    task_card: The matching `task_cards.jsonl` row, when one exists. Its
      `deliverables`, `workdir`, `task_id`, `final_message` and `input_files`
      are trusted; the instruction only supplements them.

  Returns:
    A contract dictionary (`TASK-REQUIREMENTS.md` §5.2). Keys that could not be
    read with confidence are absent rather than guessed; `deliverables` and
    `banned_tokens` are always present.
  """
  text = instruction if isinstance(instruction, str) else ""
  card = task_card if isinstance(task_card, dict) else {}
  workdir = str(card.get("workdir") or "").strip()
  card_paths = _card_paths(card)
  candidates = _instruction_paths(text, card, card_paths)
  deliverables = _deliverables(text, card_paths, workdir, candidates)

  contract = {}
  if card.get("task_id"):
    contract["task_id"] = str(card["task_id"])
  if workdir:
    contract["workdir"] = workdir
  contract["deliverables"] = _drop_duplicate_paths(deliverables)

  archives = _archives(text, deliverables)
  if archives:
    contract["archives"] = archives
  if card.get("final_message"):
    contract["secondary"] = [{"kind": "final_message", "required": True}]
  vocab = _vocab(text)
  if vocab:
    contract["vocab"] = vocab
  numeric = _numeric_cols(text)
  if numeric:
    contract["numeric_cols"] = numeric
  contract["banned_tokens"] = list(BANNED_TOKENS)
  units = _units(text)
  if units:
    contract["units"] = units
  limit = _runtime_limit(text)
  if limit:
    contract["runtime_limit_s"] = limit
  return contract


def _card_paths(card: dict) -> list[str]:
  """Returns the card's deliverable paths, in card order, deduplicated.

  Args:
    card: The task card (may be empty).

  Returns:
    Path strings exactly as the card spells them.
  """
  out: list[str] = []
  for item in card.get("deliverables") or []:
    path = str(item or "").strip()
    if path and path not in out:
      out.append(path)
  return out


def _deliverables(
    text: str,
    card_paths: list[str],
    workdir: str,
    candidates: list[tuple[str, bool]],
) -> list[dict]:
  """Merges the card's deliverable list with the paths the instruction names.

  Args:
    text: The instruction.
    card_paths: `deliverables` from the task card, in card order.
    workdir: The task working directory, used to resolve relative paths.
    candidates: `(raw path, required)` from `_instruction_paths`.

  Returns:
    `[{"path", "kind", "required"}]`: the card entries verbatim first, then any
    instruction path not already covered. A card entry with no extension is a
    directory (rubric tasks list `/app/files`, `/filesystem`), so it is kept —
    the card is authoritative — but marked `required: False`, because a
    directory cannot satisfy G1 and reporting it as a missing file would be a
    false defect.
  """
  out: list[dict] = []
  seen_paths: set[str] = set()
  resolved: set[str] = set()
  for path in card_paths:
    if path in seen_paths:
      continue
    seen_paths.add(path)
    kind = kind_of(path)
    out.append({"path": path, "kind": kind, "required": bool(kind)})
    resolved.add(_resolve(workdir, path))

  for raw, required in candidates:
    rel = _relative(raw)
    if not rel:
      continue
    # The card wins: `output/submission.zip` in prose is the card's
    # `.../output/submission.zip`, not a second file beside it.
    if any(_relative(path) == rel for path in card_paths):
      continue
    if any(posixpath.normpath(path).endswith("/" + rel) for path in card_paths):
      continue
    target = _resolve(workdir, rel)
    if target in resolved or rel in seen_paths:
      continue
    seen_paths.add(rel)
    resolved.add(target)
    out.append({"path": rel, "kind": kind_of(rel), "required": required})
  return out


def _relative(path: str) -> str:
  """Returns a path with any leading `./` removed and normalization applied.

  Args:
    path: A deliverable path.

  Returns:
    The normalized path; the leading `/` of an absolute path is preserved.
  """
  raw = str(path or "").strip()
  while raw.startswith("./"):
    raw = raw[2:]
  return posixpath.normpath(raw) if raw else ""


def _ticked_tokens(text: str) -> list[tuple[int, str]]:
  """Pairs the backticks in a region and returns the tokens between them.

  A regex over `` `([^`]*)` `` mis-pairs as soon as one backticked token is
  too short to match (`` `+` `` in the flush plan), which silently shifted every
  later token; pairing the markers by position cannot shift.

  Args:
    text: Any region of the instruction.

  Returns:
    `(offset, token)` pairs for the single-line backticked runs.
  """
  marks = [match.start() for match in _TICK.finditer(text)]
  tokens: list[tuple[int, str]] = []
  for start, end in zip(marks[0::2], marks[1::2]):
    token = text[start + 1: end]
    if 1 <= len(token) <= 200 and "\n" not in token:
      tokens.append((start, token))
  return tokens


def _instruction_paths(
    text: str, card: dict, card_paths: list[str]
) -> list[tuple[str, bool]]:
  """Returns the deliverable-looking paths an instruction names, in order.

  Four sentence shapes are read, each with its own evidence bar: an absolute
  path, a delivery verb followed by a path, a backticked path under `output/`
  or `submission/`, and a backticked path on a line whose verb promises a file.
  A path that is really a zip member, an input file or a bare document name is
  dropped.

  Args:
    text: The instruction.
    card: The task card (for `input_files`).
    card_paths: The card's deliverable paths.

  Returns:
    `(raw path, required)` pairs, deduplicated by path, in mention order.
  """
  members = _member_tokens(text, _zip_names(text, card_paths))
  inputs = {
      _resolve(str(card.get("workdir") or ""), str(f.get("path") or ""))
      for f in (card.get("input_files") or [])
      if isinstance(f, dict) and f.get("path")
  }

  found: list[tuple[str, bool]] = []
  order: dict[str, int] = {}

  def add(raw: str, required: bool = True, dirs_ok: bool = False) -> None:
    raw = raw.strip().strip("*_`").strip().rstrip(",;:.)").strip()
    raw = _relative(raw)
    if not raw or not _acceptable(raw, members, inputs, dirs_ok):
      return
    required = required and not _optional_here(text, raw)
    if raw in order:
      if not required:
        found[order[raw]] = (raw, False)
      return
    order[raw] = len(found)
    found.append((raw, required))

  for match in _ABS_PATH.finditer(text):
    add(match.group(0), dirs_ok=True)
  for match in _ROOT_PATH.finditer(text):
    add(match.group(1))
  for match in _VERB.finditer(text):
    window = re.split(
        r"\.\s|\n|;", text[match.end(): match.end() + 160], maxsplit=1
    )[0]
    token = _filename_in(window)
    if token:
      add(token, dirs_ok=True)
  for offset, token in _ticked_tokens(text):
    token = token.strip()
    if not _TICK_PATH.match(token):
      continue
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset + len(token))
    line = text[start: end if end > 0 else len(text)]
    if token.startswith(("output/", "submission/")) or _VERBISH.search(line):
      add(token)
  # One file, two spellings: keep the absolute one, which names the task
  # directory, and drop the relative twin that would resolve beside it.
  bases = {posixpath.basename(path) for path, _ in found if path.startswith("/")}
  return [
      (path, required)
      for path, required in found
      if path.startswith("/") or posixpath.basename(path) not in bases
  ]


def _filename_in(window: str) -> str:
  """Returns the first plausible file name in a short prose window.

  A delivery verb is followed by prose as often as by a path ("Return your
  work as a single Word document named `X.docx`"), so the window is walked
  word by word: a candidate starts at a word that looks like the head of a file
  name, and grows over capitalised or underscore-bearing words plus connectors
  ("Memo to Callaway.docx") until it matches a whole path.

  Args:
    window: Text following the verb, already cut at the sentence end.

  Returns:
    The file name, or "" when the window holds none.
  """
  words = window.split()
  for index, head in enumerate(words):
    head = head.strip("`*_(),.;:")
    if not head:
      continue
    if not (head[0].isupper() or head[0].isdigit() or "_" in head or "/" in head):
      continue
    parts = [head]
    next_index = index + 1
    while True:
      candidate = " ".join(parts).strip("`*_(),.;:")
      if _FULL_PATH.match(candidate):
        return candidate
      if next_index >= len(words) or len(parts) >= 4:
        break
      following = words[next_index].strip("`*_(),.;:")
      if not (
          _CONNECTOR.match(following)
          or following[:1].isupper()
          or "_" in following
          or any(char.isdigit() for char in following)
          or re.search(r"\.(?:%s)$" % _EXT, following)
      ):
        break
      parts.append(following)
      next_index += 1
  return ""


def _optional_here(text: str, raw: str) -> bool:
  """Says whether the instruction calls this path optional near its mention.

  Args:
    text: The instruction.
    raw: The path as the instruction spells it.

  Returns:
    True when the surrounding 200 characters call it optional (for example
    "optional side file", "not a gate failure", "default").
  """
  index = text.find(raw)
  if index < 0:
    return False
  return bool(_OPTIONAL.search(text[max(0, index - 200): index + len(raw) + 200]))


def _acceptable(
    raw: str, members: set[str], inputs: set[str], dirs_ok: bool = False
) -> bool:
  """Decides whether a candidate path can be a deliverable at all.

  Args:
    raw: The candidate path.
    members: Basenames the instruction puts inside an archive.
    inputs: Absolute paths the card lists as input files.
    dirs_ok: True for a path a delivery verb introduced; a verb is strong
      enough evidence to accept a directory name that is usually an input
      (`Correspondence/Client/Memo_...docx` is the deliverable in one task).

  Returns:
    True when the path can be a graded deliverable.
  """
  if not kind_of(raw):
    return False
  if ". " in raw or ":" in raw or ";" in raw or len(raw.split()) > 5:
    return False
  base = posixpath.basename(raw)
  if base.lower() in _NOT_DELIVERABLES:
    return False
  if base in members:
    return False
  segments = [seg for seg in raw.split("/") if seg not in ("", ".")]
  if (
      not dirs_ok
      and len(segments) > 1
      and any(seg.lower() in _INPUT_DIRS for seg in segments[:-1])
  ):
    return False
  if raw.startswith("/") and posixpath.normpath(raw) in inputs:
    return False
  return True


def _zip_names(text: str, card_paths: list[str]) -> list[str]:
  """Returns the archive basenames this task delivers.

  Args:
    text: The instruction.
    card_paths: The card's deliverable paths.

  Returns:
    Archive basenames, deduplicated.
  """
  names: list[str] = []
  for path in card_paths:
    if kind_of(path) == "zip":
      base = posixpath.basename(path)
      if base not in names:
        names.append(base)
  if not names:
    for match in _ABS_PATH.finditer(text):
      if kind_of(match.group(0)) == "zip":
        base = posixpath.basename(match.group(0))
        if base not in names:
          names.append(base)
  return names


def _member_windows(text: str, archive: str) -> list[str]:
  """Returns the text regions in which an archive's members are listed.

  An archive is usually named more than once — once in the "where to work"
  paragraph and again where the contents are listed — so every mention
  contributes a window, from the mention up to the next heading and capped at
  4000 characters.

  Args:
    text: The instruction.
    archive: Basename of the zip.

  Returns:
    One window per mention (at most six); empty when the archive is unnamed.
  """
  windows: list[str] = []
  for count in _MEMBER_COUNT.finditer(text):
    windows.append(text[count.start(): count.start() + 12000])
  start = 0
  while len(windows) < 6:
    index = text.find(archive, start)
    if index < 0:
      break
    window = text[text.rfind("\n", 0, index) + 1: index + 12000]
    heading = _HEADING.search(window)
    if heading and heading.start() > 0:
      window = window[: heading.start()]
    windows.append(window)
    start = index + len(archive)
  return windows


def _member_tokens(text: str, archives: list[str]) -> set[str]:
  """Returns every filename the instruction lists inside an archive.

  Args:
    text: The instruction.
    archives: Archive basenames.

  Returns:
    Basenames the instruction places inside one of the archives.
  """
  names: set[str] = set()
  for archive in archives:
    for window in _member_windows(text, archive):
      for _offset, token in _ticked_tokens(window):
        token = token.strip()
        if not re.match(r"^%s\.(?:%s)$" % (_TAIL, _EXT), token):
          continue
        base = posixpath.basename(token)
        if base in archives or base.lower() in _NOT_DELIVERABLES:
          continue
        names.add(base)
  return names


def _archives(text: str, deliverables: list[dict]) -> list[dict]:
  """Finds each `.zip` deliverable and the members the instruction requires.

  Args:
    text: The instruction.
    deliverables: The merged deliverable list.

  Returns:
    `[{"path", "required", "allow_extra"[, "members"]}]`, one per zip
    deliverable. `members` is only present when the instruction ties the list
    to a stated count and the count matches, so a half-read list never becomes
    a missing-member defect; `allow_extra` stays true because a grader that
    checks named files ignores the rest.
  """
  archives = []
  for item in deliverables:
    path = item["path"]
    if item["kind"] != "zip":
      continue
    entry = {"path": path, "required": item["required"], "allow_extra": True}
    members = _members(text, posixpath.basename(path))
    if members:
      entry["members"] = members
    archives.append(entry)
  return archives


# An instruction that lists the archive contents as numbered items instead of
# stating a count: "containing at its root:\n### 1. `deck_design.xlsx`".
_ROOT_INTRO = re.compile(
    r"(?i)\bcontain(?:s|ing|ed)?\b|\bat its root\b|\bthese files\b"
)
_NUMBERED_ITEM = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?\d{1,2}\.[ \t]*(?:\*\*)?`?"
    r"([A-Za-z0-9_][A-Za-z0-9_.\-]*\.(?:%s))`?" % _EXT,
    re.M,
)


def _numbered_members(text: str, archive: str) -> list[str]:
  """Reads root members an instruction lists as numbered items.

  Some instructions say what the archive holds with a numbered list instead of
  a count ("containing at its root: 1. `deck_design.xlsx` ..."). The member
  window is truncated at the next heading, which would drop exactly that list,
  so the numbered items are read from the archive's own mention instead, and
  only where the mention introduces contents.

  Args:
    text: The instruction.
    archive: Basename of the zip, e.g. `submission.zip`.

  Returns:
    Member basenames in the order listed; empty when the instruction states
    none (a list that is one short would fail a grader on a file that was
    never asked for).
  """
  out: list[str] = []
  for match in re.finditer(re.escape(archive), text):
    window = text[match.start(): match.start() + 12000]
    if not _ROOT_INTRO.search(window[:800]):
      continue
    for name in _NUMBERED_ITEM.findall(window):
      if name == archive or name in out:
        continue
      if name.lower() in _NOT_DELIVERABLES:
        continue
      out.append(name)
  return out


def _members(text: str, archive: str) -> list[str]:
  """Reads the archive members the instruction requires, or nothing.

  Args:
    text: The instruction.
    archive: Basename of the zip, e.g. `submission.zip`.

  Returns:
    The member basenames when the instruction states a member count and the
    extraction matches it exactly; an empty list otherwise (a list that is one
    short would fail a grader on a file that was never asked for).
  """
  names: list[str] = []
  for window in _member_windows(text, archive):
    for _offset, token in _ticked_tokens(window):
      token = token.strip()
      if not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*\.(?:%s)$" % _EXT, token):
        continue
      if token == archive or token in names:
        continue
      if token.lower() in _NOT_DELIVERABLES:
        continue
      stem = re.escape(posixpath.splitext(token)[0])
      # `.md`, `.pdf` and `.docx` variants of one stem mean the instruction
      # accepts any of the three; naming one of them would be wrong.
      if len(set(re.findall(stem + r"\.(%s)\b" % _EXT, window))) > 1:
        continue
      names.append(token)

  count = _MEMBER_COUNT.search(text)
  if not count:
    listed = _numbered_members(text, archive)
    return listed if len(listed) >= 2 else []
  wanted = _NUMBER_WORDS.get(count.group(1).lower(), 0) or int(count.group(1))
  return names if len(names) == wanted else []


def _vocab(text: str) -> dict[str, list[str]]:
  """Reads closed value sets the instruction spells out.

  Four shapes occur in the real cards: "Use these `stage` tokens:" followed by
  a run of tokens, "`access_means` is one of `a`, `b`, `c`", a JSON snippet
  `"foundation_decision": "SPREAD|IMPROVED_SPREAD"`, and `x ∈ {A, B, C}`. A set
  needs at least two values to be a closed set at all.

  Args:
    text: The instruction.

  Returns:
    `{column: [values]}`; empty when no set reads cleanly.
  """
  vocab: dict[str, list[str]] = {}

  for match in re.finditer(
      r"(?i)use these\s+`?([a-z_][a-z0-9_]*)`?\s+tokens?:?\s*\n?\s*", text
  ):
    tail = text[match.end(): match.end() + 400]
    tokens = re.findall(r"\b([A-Z][A-Z0-9_]{1,29})\b", tail)
    if len(tokens) >= 2:
      vocab.setdefault(match.group(1), tokens)

  for match in re.finditer(
      r"`([a-z_][a-z0-9_]*)`\s+is one of\s+([^\n]{2,200})", text
  ):
    values = [
        value.strip("`.,;()")
        for value in re.findall(r"`([^`]{1,40})`", match.group(2))
    ]
    if len(values) >= 2:
      vocab.setdefault(match.group(1), values)

  for match in re.finditer(
      r'"([a-z_][a-z0-9_]*)"\s*:\s*"([A-Za-z_][A-Za-z0-9_]*'
      r'(?:\|[A-Za-z_][A-Za-z0-9_]*)+)"',
      text,
  ):
    values = [value for value in match.group(2).split("|") if value]
    if len(values) >= 2:
      vocab.setdefault(match.group(1), values)

  for match in re.finditer(
      r"`?([a-z_][a-z0-9_]*)`?\s*(?:∈|is in)\s*\{([A-Z0-9_, ]{3,120})\}", text
  ):
    values = [v.strip() for v in match.group(2).split(",") if v.strip()]
    if len(values) >= 2:
      vocab.setdefault(match.group(1), values)
  return vocab


def _numeric_cols(text: str) -> list[str]:
  """Reads the numeric columns from an explicit "the numeric columns" list.

  Only that phrase is trusted: a column list quoted for another purpose (a CSV
  schema table) mixes text and number columns, and a number-column scan over a
  text column reports `N/A` as a placeholder — a false fatal.

  Args:
    text: The instruction.

  Returns:
    Column names, in statement order; empty when the phrase is absent.
  """
  match = re.search(r"(?is)\bnumeric\s+columns?\b", text)
  if not match:
    return []
  tail = text[match.end(): match.end() + 900]
  dashes = [hit.start() for hit in re.finditer(r"—", tail)]
  if len(dashes) >= 2:
    tail = tail[dashes[0] + 1: dashes[1]]
  else:
    tail = tail.split("\n\n")[0]
  banned = {token.lower() for token in BANNED_TOKENS}
  cols: list[str] = []
  for token in re.findall(r"`([A-Za-z][A-Za-z0-9_]{2,40})`", tail):
    if token.lower() in banned or token in cols:
      continue
    cols.append(token)
  return cols


def _units(text: str) -> dict[str, str]:
  """Reads the unit conventions the instruction states outright.

  Args:
    text: The instruction.

  Returns:
    `{"project"/"placement"/"plan"/"coordinates"/"length": unit}`; empty when
    the statement says nothing about units.
  """
  units: dict[str, str] = {}
  patterns = (
      ("project", r"(?i)\b([a-z]+)\s+project\s+units?\b"),
      ("placement",
       r"(?i)placement(?:[-\s]matrix)?[^\n]{0,40}?\bin\s+([a-z]+)\b"),
      ("plan", r"(?i)\bplan\s+units?\s+(?:are|is)\s+([a-z]+)\b"),
      ("project", r"(?i)\bunits\s+are\s+([a-z]+)\b"),
      ("coordinates", r"(?i)\bcoordinates?\s+use\s+([a-z]+)\b"),
  )
  for key, pattern in patterns:
    hit = re.search(pattern, text)
    if not hit:
      continue
    unit = _UNIT_WORDS.get(hit.group(1).lower())
    if unit:
      units.setdefault(key, unit)
  if not units:
    hit = re.search(r"(?i)\bin\s+\**([a-z]+)\**(?=[\s,.)])", text)
    if hit:
      unit = _UNIT_WORDS.get(hit.group(1).lower())
      if unit:
        units["length"] = unit
  return units


def _runtime_limit(text: str) -> int:
  """Reads a stated wall-clock limit, e.g. "must run headless in 300 s".

  Args:
    text: The instruction.

  Returns:
    The limit in seconds, or 0 when the instruction states none.
  """
  hit = re.search(
      r"(?i)\b(?:run|runs|runtime|within|finish)[^\n.;]{0,40}?"
      r"\b(\d{2,5})\s*(?:s|secs?|seconds)\b",
      text,
  )
  return int(hit.group(1)) if hit else 0


def _resolve(workdir: str, path: str) -> str:
  """Resolves a contract path to an absolute path, as `selfcheck.py` does.

  Args:
    workdir: The task working directory.
    path: Absolute or workdir-relative path.

  Returns:
    The normalized absolute path, or the normalized relative one when no
    workdir is known.
  """
  path = str(path or "").strip()
  if not path:
    return ""
  if path.startswith("/"):
    return posixpath.normpath(path)
  if not workdir:
    return posixpath.normpath(path)
  return posixpath.normpath(posixpath.join(workdir, path))
