#!/usr/bin/env python3
"""Weighted-rubric judge for `landscape-sheet-revision`.

This file holds the grading contract (artifact text extraction, grader instructions, response schema) and an
OpenAI-compatible judge transport; it reads the task's rubric (verifiers[]) from criteria.json (see its `_source`).
Writes grade.json in the current directory unless --out says otherwise.

In the task's verifier container, where tests/verify.py runs it, the judge is any OpenAI-compatible endpoint:
JUDGE_BASE_URL with JUDGE_API_KEY, and JUDGE_MODEL (--backend is not read). pypdf comes from _vendor/.

Usage, in the verifier: python3 grade.py --candidate <dir|file> --backend openai [--out grade.json]
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
import http.client
import threading
import urllib.error
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parent / "_vendor" / "pypdf-6.19.0-py3-none-any.whl"))  # pypdf

HERE = Path(__file__).resolve().parent
JUDGE_MAX_TOKENS = 32000

# ------------------------------------------------ the grading contract
MAX_TEXT_CHARS = 350_000

def office_text(path: Path) -> str:
    texts = []
    with zipfile.ZipFile(path) as bundle:
        names = sorted(n for n in bundle.namelist() if n.endswith(".xml"))
        for name in names:
            if not any(key in name for key in ("document.xml", "sharedStrings.xml", "sheet", "slide", "notesSlide")):
                continue
            try:
                root = ET.fromstring(bundle.read(name))
            except ET.ParseError:
                continue
            value = " ".join(t.strip() for t in root.itertext() if t.strip())
            if value:
                texts.append(f"\n--- {name} ---\n{value}")
    return "".join(texts)

def extract(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".py", ".html", ".xml"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}:
        return office_text(path)
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return "[PDF text unavailable: install pypdf]"
        return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    return f"[binary artifact: {path.name}, {path.stat().st_size} bytes]"

def candidate_text(candidate: Path) -> str:
    files = [candidate] if candidate.is_file() else sorted(p for p in candidate.rglob("*") if p.is_file())
    blocks = []
    used = 0
    for path in files:
        text = extract(path)
        block = f"\n===== {path.relative_to(candidate) if candidate.is_dir() else path.name} =====\n{text}"
        remaining = MAX_TEXT_CHARS - used
        if remaining <= 0:
            break
        blocks.append(block[:remaining])
        used += len(block[:remaining])
    return "".join(blocks)

schema = {
        "type": "object",
        "properties": {"grades": {"type": "array", "items": {"type": "object", "properties": {
            "index": {"type": "integer"}, "score": {"type": "number", "minimum": 0, "maximum": 1},
            "passed": {"type": "boolean"}, "evidence": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["index", "score", "passed", "evidence", "reason"], "additionalProperties": False}}},
        "required": ["grades"], "additionalProperties": False}

instructions = (
        "Grade only the supplied candidate artifacts against every criterion. Scores are 0..1. "
        "A criterion passes only when the artifact contains direct, specific evidence. Do not infer "
        "missing work, award credit for promises, or use outside knowledge to fill gaps. Evidence "
        "must name the file and quote or precisely locate the supporting content. Return one grade "
        "for every criterion index and no extra indices."
    )
# ---------------------------------------------------------------------------------- end of the grading contract



# ---------------------------------------------------------------------------------- reading a deliverable, bounded
# Bounds far above any real deliverable. A text file is read only as far as MAX_TEXT_CHARS reaches (a rendering's
# criteria: MAX_RENDERING_CHARS), so what the judge is shown is unchanged; a file over MAX_FILE_BYTES, an office file that unpacks past MAX_UNPACKED_BYTES, a PDF past
# MAX_PDF_PAGES pages, and whatever lies past MAX_CANDIDATE_BYTES in all are not read, and a note in their place says
# so: the deliverable is graded on what fits, and the judge knows the rest exists.
MAX_FILE_BYTES = 256 << 20
MAX_CANDIDATE_BYTES = 1 << 30
MAX_UNPACKED_BYTES = 256 << 20
MAX_PDF_PAGES = 2000
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".py", ".html", ".xml"}   # extract()'s text kinds
OFFICE_SUFFIXES = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}
TEXT_HEAD_BYTES = 4 * MAX_TEXT_CHARS + 4096      # UTF-8 enough for MAX_TEXT_CHARS characters, whatever they are
_not_read: dict = {}                             # a placeholder regular_files() left -> what extract() says for it
_lengths: dict = {}                              # extract()'s result length per path, so nothing is read twice

_base_extract = extract


def _too_large(path: Path, size: int):
    """Why the artifact at `path` is not read at all, or None."""
    if size > MAX_FILE_BYTES:
        return f"{size} bytes, over the grader's {MAX_FILE_BYTES}-byte limit for one file"
    suffix = path.suffix.lower()
    if suffix in OFFICE_SUFFIXES:
        try:
            with zipfile.ZipFile(path) as bundle:
                unpacked = sum(info.file_size for info in bundle.infolist())
        except (zipfile.BadZipFile, OSError, ValueError):
            return None                          # the base reader says it cannot be read
        if unpacked > MAX_UNPACKED_BYTES:
            return f"it unpacks to {unpacked} bytes, over the grader's {MAX_UNPACKED_BYTES}-byte limit"
    elif suffix == ".pdf":
        try:
            from pypdf import PdfReader
            pages = len(PdfReader(str(path)).pages)
        except Exception:  # noqa: BLE001 - the base reader says what is wrong with it
            return None
        if pages > MAX_PDF_PAGES:
            return f"{pages} pages, over the grader's {MAX_PDF_PAGES}-page limit"
    return None


def _extract(path: Path) -> str:
    if path in _not_read:
        return _not_read[path]
    try:
        size = path.stat().st_size
        if path.suffix.lower() in TEXT_SUFFIXES and size > TEXT_HEAD_BYTES:
            # the base read_text(), stopped where MAX_TEXT_CHARS stops reading it anyway
            with path.open(encoding="utf-8", errors="replace") as fh:
                head = fh.read(MAX_TEXT_CHARS + 1)
            return head + f"\n[the rest of {path.name}, {size} bytes in all, is past what the judge reads]"
        problem = _too_large(path, size)
        if problem:
            return f"[artifact not read: {path.name} ({problem})]"
        return _base_extract(path)
    except Exception as exc:  # noqa: BLE001
        return f"[unreadable artifact: {path.name} ({type(exc).__name__}: {str(exc)[:200]})]"


def extract(path: Path) -> str:
    """The base extract(), bounded as above, except that an artifact it cannot read (a corrupt docx or pdf)
    becomes a marker like extract()'s own, instead of ending the grade: the candidate's other files still count."""
    text = _extract(path)
    _lengths[path] = len(text)
    return text


def regular_files(candidate: Path, scratch: Path, text_bytes: int = TEXT_HEAD_BYTES) -> Path:
    """A copy of the candidate holding only its regular files, at the same relative paths, for
    candidate_text() to read: never a symlink, which could point at a file on this host (the verifier drops them,
    but the judge reads whatever it is given), nor anything that resolves outside the candidate. Bounded: a text
    file is copied as far as the judge reads it (text_bytes: TEXT_HEAD_BYTES for candidate_text(), more for a
    rendering's criteria), ending in a marker where it is cut, and a file over MAX_FILE_BYTES, or past
    MAX_CANDIDATE_BYTES in all (taken in the order the judge reads: the answer, then changed/), leaves an empty
    placeholder whose note extract() returns."""
    if candidate.is_symlink():
        sys.exit(f"candidate is a symlink: {candidate}")
    left = [MAX_CANDIDATE_BYTES]
    if candidate.is_file():
        _copy_bounded(candidate, scratch / candidate.name, left, text_bytes)
        return scratch / candidate.name
    root, copy = candidate.resolve(), scratch / "candidate"
    copy.mkdir()
    found = []
    for d, _, files in os.walk(candidate, followlinks=False):
        for name in files:
            p = Path(d) / name
            if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(root):
                continue
            found.append(p.relative_to(candidate))
    for rel in sorted(found, key=lambda r: (r.parts[0] == "changed", r)):
        target = copy / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_bounded(candidate / rel, target, left, text_bytes)
    return copy


def _copy_bounded(src: Path, target: Path, left: list, text_bytes: int) -> None:
    size = src.stat().st_size
    take = min(size, text_bytes) if src.suffix.lower() in TEXT_SUFFIXES else size
    if take > MAX_FILE_BYTES:
        why = f"{size} bytes, over the grader's {MAX_FILE_BYTES}-byte limit for one file"
    elif take > left[0]:
        why = f"the deliverable's files past its first {MAX_CANDIDATE_BYTES} bytes are not read"
    else:
        if take == size:
            shutil.copyfile(src, target)
        else:
            # cut: the copy ends in a marker (within take), never in clipped text a reader could take for the whole
            marker = f"\n[the rest of {src.name}, {size} bytes in all, is past what the judge reads]".encode()
            cut = max(take - len(marker), 0)
            with src.open("rb") as fh:
                head = fh.read(cut + 1)
            for _ in range(3):                   # nor inside a UTF-8 character
                if cut and head[cut] & 0xC0 == 0x80:
                    cut -= 1
            target.write_bytes(head[:cut] + marker)
        left[0] -= take
        return
    target.touch()
    _not_read[target] = f"[artifact not read: {src.name} ({why})]"


def _extract_json(text: str) -> dict:
    """The reply's JSON object: the whole text, else the one object in it that carries `grades` (else the first
    object that parses), so prose with braces before or after it (a note on "criterion {1}") does not hide it. Two
    different `grades` objects (a draft and a revision) make the reply ambiguous: it is asked again."""
    try:
        whole = json.loads(text)
        if isinstance(whole, dict):
            return whole
    except json.JSONDecodeError:
        pass
    decoder, found, graded, pos = json.JSONDecoder(), None, [], 0
    for m in re.finditer(r"\{", text):
        if m.start() < pos:
            continue            # inside an object already read
        try:
            obj, end = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        pos = end
        if isinstance(obj, dict) and "grades" in obj:
            if obj not in graded:
                graded.append(obj)
        elif found is None and isinstance(obj, dict):
            found = obj
    if len(graded) > 1:
        raise ValueError(f"judge returned {len(graded)} different grades objects")
    if graded:
        return graded[0]
    if found is None:
        raise ValueError(f"judge returned no JSON object: {text[:300]}")
    return found


def _score(row: dict) -> float:
    """A reply row's score: a number from 0 to 1 (the schema's `score: number, minimum 0, maximum 1`).
    Anything else -- missing, null, text, NaN, 7 on a 0-10 scale -- makes the reply malformed, so it is asked again
    rather than credited."""
    value = row.get("score")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"criterion {row.get('index')}: score {value!r} is not a number")
    s = float(value)
    if not math.isfinite(s) or not 0.0 <= s <= 1.0:
        raise ValueError(f"criterion {row.get('index')}: score {value!r} is outside 0..1")
    return s


def _passed(row: dict) -> bool:
    """A reply row's `passed`: a JSON boolean, as the schema requires it. Missing or anything else makes
    the reply malformed (asked again), never a pass inferred from the score."""
    value = row.get("passed")
    if not isinstance(value, bool):
        raise ValueError(f"criterion {row.get('index')}: passed {value!r} is not a boolean")
    return value


def _rows_by_index(rows, expected: set[int]) -> dict:
    """The reply's rows by criterion index: one row per criterion, as the judge requires (it refuses a
    reply whose row count or index set differs). A row that is not an object, an index that is not an integer, a
    duplicate, a missing or an unexpected index makes the reply malformed."""
    if not isinstance(rows, list):
        raise ValueError(f"grades is {type(rows).__name__}, not a list")
    by_index: dict = {}
    for r in rows:
        if not isinstance(r, dict) or isinstance(r.get("index"), bool) or not isinstance(r.get("index"), int):
            raise ValueError(f"a grades row has no integer index: {str(r)[:120]}")
        if r["index"] in by_index:
            raise ValueError(f"criterion {r['index']} is graded twice")
        by_index[r["index"]] = r
    missing, extra = expected - set(by_index), set(by_index) - expected
    if missing or extra:
        raise ValueError(f"criterion index mismatch: missing {sorted(missing)}, unexpected {sorted(extra)}")
    return by_index


# A judge call that fails for want of capacity (a rate limit, an overloaded or unreachable endpoint) is sent again
# after each of these waits; a reply that arrives malformed is a different matter, asked again by the caller.
RETRY_WAITS = (15, 30, 60, 120)
RETRYABLE_TYPES = ("RateLimit", "ServiceUnavailable", "InternalServerError", "Timeout", "APIConnectionError",
                   "Overloaded")
RETRYABLE_STATUS = (429, 500, 502, 503, 504, 529)
# a status code standing alone, not the digits of a token count ("215000 tokens")
RETRYABLE_TEXT = re.compile(r"(?<![0-9])(429|500|502|503|504|529)(?![0-9])|RESOURCE_EXHAUSTED|[Oo]verloaded|timed out")


def retryable(exc: Exception) -> bool:
    """A call that failed for want of capacity: by the exception's type, else its HTTP status, else its text."""
    if any(k in type(exc).__name__ for k in RETRYABLE_TYPES):
        return True
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code in RETRYABLE_STATUS
    return bool(RETRYABLE_TEXT.search(str(exc)))


# The judge's transport in the verifier container, one for every grader: a chat completion from any OpenAI-compatible
# endpoint, JUDGE_BASE_URL with JUDGE_API_KEY. Each fallback is decided on what THIS request sent, so the calls a
# grader makes at once each read the refusal of their own request: the output limit goes as max_completion_tokens, or
# as max_tokens once the endpoint refuses that name; a size it refuses (SIZE_REFUSED) is halved, down to
# MIN_OUTPUT_TOKENS, for this call and every later one (never grown back), unless the prompt alone is too long
# (input_overflow), which no smaller limit fixes; temperature is left out once the endpoint refuses it (some models
# take only their default). A reply cut off while it is read (a reset, an IncompleteRead, a TLS record cut short), an
# error's as well as an answer's, is the connection's failure, which the caller's retryable() sends again. No body
# reaches an exception or a log with the judge's key in it, should the endpoint echo it (_redacted).
MIN_OUTPUT_TOKENS = 1024                    # the judge answers with a JSON object
# A 400 that refuses the output limit's size, not its name: more than the model writes in one reply, or more than its
# context window leaves after the prompt (as OpenAI, vLLM, Anthropic, Gemini and the gateways in front of them say it)
SIZE_REFUSED = re.compile(r"too large|exceed|maximum context length|context[_ ](?:length|window)|supported range"
                          r"|at most \d|must be (?:<=|less than|at most)|\d\s*>\s*\d", re.IGNORECASE)
# A 400 that says the prompt alone fills the context window (Anthropic, Gemini, OpenAI); vLLM's "you requested N
# tokens (P in the messages, C in the completion)" is weighed in input_overflow
INPUT_REFUSED = re.compile(r"prompt is too long|input token count|input is too long|too many input tokens"
                           r"|messages resulted in", re.IGNORECASE)
REFUSED = ("content_filter", "refusal")     # the finish_reason of a request the judge declined
# What later requests send, as refusals taught it: the limit's name, a cap on its size, whether temperature goes
SENT_AS = {"limit": "max_completion_tokens", "cap": None, "temperature": True}
SENT_AS_LOCK = threading.Lock()


class APIConnectionError(RuntimeError):
    """The endpoint could not be reached, did not answer in time, or cut its reply off (retryable() retries it by
    name)."""


class APIStatusError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        super().__init__(f"HTTP {status_code}: {body[:300]}")
        self.status_code, self.body = status_code, body


class NoCompletion(RuntimeError):
    """A 200 whose body is not a chat completion with a message, as some gateways answer an upstream failure: sent
    again, like the bad gateway's 502 it stands for."""
    status_code = 502           # what retryable() reads


class JudgeRefused(RuntimeError):
    """The judge declined the request: no text, finish_reason "content_filter" or "refusal" (an Anthropic model's
    stop_reason refusal through an OpenAI-compatible gateway). A refusal carries no grade, and asking again would get
    the same; never sent again (retryable() reads status_code)."""
    status_code = 400


def _redacted(text: str) -> str:
    """`text` without the judge's key, should the endpoint echo it (a 401 that quotes the key it refused)."""
    key = os.environ.get("JUDGE_API_KEY") or ""
    return text.replace(key, "[the key]") if key else text


def _said(data: bytes) -> str:
    """What a reply that is no chat completion said, for the log: its error's message when it has one, else its start;
    never the judge's key."""
    text = data.decode("utf-8", errors="replace")
    try:
        error = json.loads(text).get("error")
        text = str(error.get("message") or error) if isinstance(error, dict) else str(error or text)
    except (ValueError, AttributeError):
        pass
    return _redacted(text)[:300]


def input_overflow(said: str) -> bool:
    """Whether a 400 refuses the prompt itself (more than the context window holds), not the output limit's size."""
    window = re.search(r"maximum context length is (\d+)", said)
    prompt = re.search(r"(\d+) (?:tokens )?in the messages", said)
    if window and prompt:
        return int(prompt.group(1)) >= int(window.group(1))
    return bool(INPUT_REFUSED.search(said))


def chat_completion(base: str, model: str, messages: list, max_tokens: int, temperature: float | None = None,
                    timeout: float = 1800) -> tuple[str, dict]:
    """(text, what the call sent and spent) of one chat completion at `base`, the output limit at most `max_tokens`,
    with `temperature` unless it is None or the endpoint refused it. APIStatusError, APIConnectionError, NoCompletion
    or JudgeRefused when it gives none."""
    while True:
        with SENT_AS_LOCK:
            limit, cap, with_temperature = SENT_AS["limit"], SENT_AS["cap"], SENT_AS["temperature"]
        tokens = max_tokens if cap is None else min(max_tokens, cap)
        body = {"model": model, "messages": messages, limit: tokens}
        if temperature is not None and with_temperature:
            body["temperature"] = temperature
        req = urllib.request.Request(f"{base}/chat/completions", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {os.environ.get('JUDGE_API_KEY', '')}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                said = _redacted(exc.read().decode("utf-8", errors="replace"))
            except (OSError, http.client.HTTPException) as cut:
                raise APIConnectionError(f"HTTP {exc.code} cut off: {type(cut).__name__}: {cut}") from None
            if exc.code == 400 and SIZE_REFUSED.search(said) and not input_overflow(said):
                if tokens > MIN_OUTPUT_TOKENS:
                    with SENT_AS_LOCK:
                        SENT_AS["cap"] = min(SENT_AS["cap"] or tokens, max(MIN_OUTPUT_TOKENS, tokens // 2))
                    print(f"the judge refused {tokens} output tokens ({said[:200]}); asking for fewer", file=sys.stderr)
                    continue
            elif exc.code == 400 and limit == "max_completion_tokens" and limit in said:
                with SENT_AS_LOCK:
                    SENT_AS["limit"] = "max_tokens"
                continue
            elif exc.code == 400 and "temperature" in body and "temperature" in said:
                with SENT_AS_LOCK:
                    SENT_AS["temperature"] = False
                continue
            raise APIStatusError(exc.code, said) from None
        except (OSError, http.client.HTTPException) as exc:
            # URLError, a timeout, a reset, a TLS record cut short (ssl.SSLError) are OSErrors, and a reply cut off
            # mid-body (IncompleteRead) an HTTPException: the connection's failure
            raise APIConnectionError(_redacted(f"{type(exc).__name__}: {exc}")) from None
        try:
            reply = json.loads(data)
            choice = reply["choices"][0]
            text = choice["message"].get("content") or ""
        except (ValueError, LookupError, TypeError, AttributeError):
            raise NoCompletion(f"HTTP 200 without a chat completion: {_said(data)}") from None
        if not text.strip() and choice.get("finish_reason") in REFUSED:
            raise JudgeRefused(f"the judge declined the request (finish_reason {choice['finish_reason']!r})")
        usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
        return text, {"max_tokens_sent": tokens, "finish_reason": choice.get("finish_reason"),
                      "completion_tokens": usage.get("completion_tokens")}


# One call's time limit: a full JUDGE_MAX_TOKENS reply at ~80 tokens/s. A call that stalls is cut there, and one more
# try (after RETRY_WAITS[0]) still ends inside verify.py's 840 s for the whole judge run, two of which, with the
# collect steps, fit the verifier's 1800 s.
CALL_TIMEOUT_SEC = 400


def call_model(backend: str, model: str, system: str, prompt: str) -> str:
    """One judge call through the verifier's transport (chat_completion; `backend` is not read): `system` and
    `prompt` as the messages, JUDGE_MAX_TOKENS as the output limit and no temperature; a failure retryable() accepts
    is sent again after each of RETRY_WAITS."""
    base = os.environ.get("JUDGE_BASE_URL", "").rstrip("/")
    if not base:
        sys.exit("JUDGE_BASE_URL is not set: the judge's OpenAI-compatible endpoint, e.g. http://localhost:4000/v1")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    for wait in (*RETRY_WAITS, None):
        try:
            return chat_completion(base, model, messages, JUDGE_MAX_TOKENS, timeout=CALL_TIMEOUT_SEC)[0]
        except Exception as exc:  # noqa: BLE001
            if wait is None or not retryable(exc):
                raise
            print(f"judge call failed ({type(exc).__name__}: {str(exc)[:200]}); sent again in {wait} s",
                  file=sys.stderr)
            time.sleep(wait)


MAX_ATTEMPTS = 3


def ask_rubric(criteria: list, task_prompt: str, body_text: str, call) -> dict:
    """One judge call over these criteria, retried up to
    MAX_ATTEMPTS times on a malformed or incomplete reply. Returns the reply rows by index."""
    payload = json.dumps({"task_prompt": task_prompt,
                          "criteria": [{k: v for k, v in c.items() if not k.startswith("_")}
                                       for c in criteria]}, ensure_ascii=False)
    payload += "\n\nReturn JSON matching this schema:\n" + json.dumps(schema) + "\n" + body_text
    expected = {int(c["index"]) for c in criteria}
    last = ""
    for attempt in range(MAX_ATTEMPTS):
        raw = call(instructions, payload)
        try:
            by_index = _rows_by_index(_extract_json(raw).get("grades", []), expected)
            for r in by_index.values():
                _score(r)
                _passed(r)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            continue
        return by_index
    raise RuntimeError(f"judge failed after {MAX_ATTEMPTS} attempts: {last}")


def weigh(criteria: list, by_index: dict) -> dict:
    """Weight the reply rows."""
    weighted = passed_weighted = total = 0.0
    graded = []
    for c in criteria:
        i = int(c["index"])
        w = float(c["numerical_weight"])
        row = by_index[i]
        s = _score(row)
        passed = _passed(row)
        weighted += s * w
        passed_weighted += w if passed else 0.0      # some rubrics report passed_score, not score
        total += w
        graded.append({**row, "passed": passed, "weight": w, "credited_fraction": round(s, 4)})
    score = (weighted / total) if total else 0.0
    passed_score = (passed_weighted / total) if total else 0.0
    return {"score": score, "passed_score": passed_score, "combined_score": score,
            "weighted_points": weighted, "possible_weighted_points": total,
            "grades": sorted(graded, key=lambda x: x["index"])}


def grade_rubric(criteria: list, task_prompt: str, body_text: str, call) -> dict:
    """One judge call over the whole rubric, weighted."""
    return weigh(criteria, ask_rubric(criteria, task_prompt, body_text, call))


def candidate_chars(candidate: Path) -> int:
    """What candidate_text() would have concatenated without its cap, counted until it passes MAX_TEXT_CHARS
    (all the truncated flags need), from the lengths extract() kept: a deliverable is not read twice."""
    files = [candidate] if candidate.is_file() else sorted(p for p in candidate.rglob("*") if p.is_file())
    total = 0
    for p in files:
        total += (len(f"\n===== {p.relative_to(candidate) if candidate.is_dir() else p.name} =====\n")
                  + (_lengths[p] if p in _lengths else len(extract(p))))
        if total > MAX_TEXT_CHARS:
            break
    return total


# The output/-contract verifier collects the world files the agent changed elsewhere under changed/, beside the answer
# (output/ and FINAL_MESSAGE.md). The reader takes files in path order under one budget, so the answer is
# read first and changed/ after it: working files left in the world never push the answer out of MAX_TEXT_CHARS.
CHANGED = "changed"


def answer_first(candidate: Path) -> list:
    """The candidate's copy in the order the judge reads it: the answer, then changed/ (moved aside for that)."""
    changed = candidate / CHANGED
    if not changed.is_dir():
        return [candidate]
    rest = candidate.parent / "changed_files"
    rest.mkdir()
    changed.rename(rest / CHANGED)
    return [candidate, rest]


def bound_text(candidate: Path, renderings: list, keep: float | None = None) -> tuple:
    """The text a criterion bound to these renderings gets: the agent's final message, then each
    rendering whole (a PDF through its text twin, a formula dump as is). No cap: each criterion
    gets its renderings entire.
    `keep` (fit_to_context) keeps that fraction of the largest rendering, its tail replaced by the cap's marker; the
    final message and every other rendering stay whole. Returns (text, file names, what was cut or None)."""
    files = []
    final = candidate / "FINAL_MESSAGE.md"
    if final.is_file():
        files.append(final)
    for name in renderings:
        for p in (candidate / f"{name}.txt", candidate / name):
            if p.is_file() and p.suffix == ".txt":
                files.append(p)
                break
    texts = {p: _rendering(p) for p in files}
    cut = None
    shown = [p for p in files if p != final]
    if keep is not None and shown:
        big = max(shown, key=lambda p: len(texts[p]))
        n = len(texts[big])
        k = int(n * keep)
        texts[big] = texts[big][:k] + f"\n[the rest of {big.name} is past what the judge reads]"
        cut = {"rendering": big.name, "chars_total": n, "chars_kept": k, "kept_fraction": keep}
    text = "".join(f"\n===== {p.name} =====\n{texts[p]}" for p in files)
    return text, [p.name for p in files], cut


# A rendering is handed to its criteria whole, up to this: past it no judge call could take it anyway.
MAX_RENDERING_CHARS = 4_000_000
# So the candidate's copy keeps that much of each text file for criteria bound to renderings (UTF-8 enough for
# MAX_RENDERING_CHARS characters); TEXT_HEAD_BYTES, which covers only candidate_text()'s window, would clip them.
RENDERING_HEAD_BYTES = 4 * MAX_RENDERING_CHARS + 4096
# fit_to_context: when the judge refuses a call's prompt as longer than its context ("prompt is too long: N tokens >
# M maximum"), the call is sent again with the largest rendering cut to FIT_MARGIN x M / N of the length it was just
# sent with (its tail replaced by the cap's marker), up to FIT_TRIES cuts: a dense rendering (a formula dump) holds
# more of the prompt's tokens than its share of the characters, so the cut one refusal sizes can still be too long.
# The instruction, the criteria and the final message are never cut.

FIT_MARGIN = 0.95
FIT_TRIES = 3
TOO_LONG = re.compile(r"prompt is too long: (\d+) tokens > (\d+) maximum")


def fit_to_context(exc: Exception) -> float | None:
    """The fraction of the largest rendering, as just sent, to keep after the judge refused the prompt as too long;
    None for any other error (or a refusal that names no counts)."""
    said = getattr(exc, "body", None) or str(exc)          # the whole reply, not the message's first 300 chars
    m = TOO_LONG.search(said) if getattr(exc, "status_code", None) == 400 else None
    if not m or int(m.group(1)) <= int(m.group(2)):
        return None
    return FIT_MARGIN * int(m.group(2)) / int(m.group(1))


def _rendering(p: Path) -> str:
    with p.open(encoding="utf-8", errors="replace") as fh:
        text = fh.read(MAX_RENDERING_CHARS + 1)
    if len(text) > MAX_RENDERING_CHARS:
        text = text[:MAX_RENDERING_CHARS] + f"\n[the rest of {p.name} is past what the judge reads]"
    return text


def grade_bound(criteria: list, task_prompt: str, candidate: Path, call) -> dict:
    """Criteria grouped by rendering set; one call per set over exactly its renderings. A criterion whose
    renderings were never produced fails: the judge reads only its dependency files, so there
    is nothing to judge (a trial with no rendering at all scores the same way)."""
    groups: dict = {}
    for c in criteria:
        groups.setdefault(tuple(c.get("renderings") or ()), []).append(c)
    by_index, binding, chars = {}, [], 0
    for names, group in groups.items():
        text, files, cut = bound_text(candidate, list(names))
        if not [f for f in files if f != "FINAL_MESSAGE.md"]:
            for c in group:
                by_index[int(c["index"])] = {"index": int(c["index"]), "score": 0.0, "passed": False,
                                             "source": "not_rendered", "evidence": "",
                                             "reason": f"no rendering of {list(names)}: the deliverable was not delivered"}
            binding.append({"renderings": list(names), "criteria": [int(c["index"]) for c in group],
                            "files": [], "chars": 0})
            continue
        if not text.strip():
            sys.exit(f"candidate yielded no readable content for renderings {list(names)}: {candidate}")
        keep, cuts = 1.0, 0
        while True:
            try:
                by_index.update(ask_rubric(group, task_prompt, text, call))
                break
            except Exception as exc:  # noqa: BLE001 (fit_to_context passes on anything but the judge's too-long refusal)
                fit = fit_to_context(exc)
                if fit is None or cuts == FIT_TRIES:
                    raise
                keep, cuts = keep * fit, cuts + 1
                text, files, cut = bound_text(candidate, list(names), keep=keep)
        binding.append({"renderings": list(names), "criteria": [int(c["index"]) for c in group],
                        "files": files, "chars": len(text), **({"fit_to_context": {**cut, "cuts": cuts}} if cut else {})})
        chars += len(text)
    out = weigh(criteria, by_index)
    out.update({"binding": binding, "candidate_chars": chars})
    return out

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True, type=Path, help="agent deliverable: file or dir")
    ap.add_argument("--criteria", type=Path, default=HERE / "criteria.json")
    ap.add_argument("--instruction", type=Path, default=HERE.parent / "instruction.md")
    ap.add_argument("--out", type=Path, default=Path("grade.json"))
    ap.add_argument("--backend", default=os.environ.get("JUDGE_BACKEND", "vertex"))
    ap.add_argument("--model", default=os.environ.get("JUDGE_MODEL", "claude-opus-5-5"))
    a = ap.parse_args()

    spec = json.loads(a.criteria.read_text(encoding="utf-8"))
    criteria = spec["criteria"]
    # A missing or empty candidate is an error, not a 0.0: grading nothing returns a plausible zero.
    if not a.candidate.exists():
        sys.exit(f"candidate does not exist: {a.candidate}")
    task_prompt = a.instruction.read_text(encoding="utf-8")
    call = lambda system, prompt: call_model(a.backend, a.model, system, prompt)
    with tempfile.TemporaryDirectory(prefix="judge_candidate_") as scratch:
        bound = any(c.get("renderings") for c in criteria)
        candidate = regular_files(a.candidate, Path(scratch), RENDERING_HEAD_BYTES if bound else TEXT_HEAD_BYTES)
        if bound:
            graded = grade_bound(criteria, task_prompt, candidate, call)
        else:
            parts = answer_first(candidate)
            body_text = "".join(candidate_text(p) for p in parts)[:MAX_TEXT_CHARS]
            if not body_text.strip():
                sys.exit(f"candidate yielded no readable content: {a.candidate}")
            graded = grade_rubric(criteria, task_prompt, body_text, call)
            answer, total = candidate_chars(parts[0]), sum(candidate_chars(p) for p in parts)
            graded.update({"truncated": total > MAX_TEXT_CHARS, "answer_truncated": answer > MAX_TEXT_CHARS,
                           "candidate_chars": total})
    primary = spec.get("score_key", "score")
    result = {"task_id": spec["task_id"], "judge_model": a.model, "judge_backend": a.backend,
              "grading_method": "portable_weighted_rubric_equivalent",
              "rubric_source": spec["_source"],
              "family": spec.get("family"), "score_key": primary,
              **graded,
              "primary_score": {"score": graded["score"], "passed_score": graded["passed_score"],
                                "combined_score": graded["score"]}[primary]}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "grades"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
