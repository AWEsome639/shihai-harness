#!/usr/bin/env python3
"""Grade a task's collected deliverable in its verifier container: stage what the judge reads, then run grader/grade.py,
the task's judge.

    python3 verify.py --submission /logs/verifier/submission [--out grade.json]

test.sh collected the deliverable into /logs/verifier/submission/ (the vendor renderings, programmatic scores and the
files from outside the workspace beside it). A task whose criteria read vendor renderings is judged on them and the
agent's final message, any other on the agent's own files. grade.json gets the judge's primary score as its reward, or
0 when nothing was delivered. When no grade can be made (a rendering failed on our side, a programmatic score is
missing, the judge does not answer) verify.py writes <out stem>.failed.json and exits non-zero: not measured.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # tests/, at /tests in the verifier
TASK = "fire-test-validation-memo"
# The staging below reads a task folder (grader/criteria.json) and a trial (verifier/submission, renderings/,
# programmatic/): in the verifier those are /tests and /logs, where Harbor mounts the trial's verifier/ folder.
TASK_DIR = HERE
GRADER = HERE / "grader"
DEFAULT_JUDGE_MODEL = "claude-opus-5-5"
# one judge run: test.sh runs verify.py twice at most, both inside the verifier's timeout_sec (1800 s), so a
# judge that hangs ends in GRADER_FAILED.txt (not measured), never in Harbor's timeout
JUDGE_TIMEOUT_SEC = 840


def output_contract(task_dir: Path) -> bool:
    """Whether the task collects under the output/ contract (output/ flattened into submission/, the world files the
    agent changed diffed into submission/changed/), read from tests/test.sh."""
    sh = task_dir / "test.sh"
    text = sh.read_text(encoding="utf-8", errors="replace") if sh.is_file() else ""
    return "/app/files/output" in text and "/logs/verifier/submission/changed" in text


# ------------------------------------------------ the candidate's staging
def world_root(task_dir: Path) -> Path | None:
    """The pristine world this task ships, so we can tell the agent's work from the scenery."""
    for name in ("files", "filesystem"):
        d = task_dir / "environment" / name
        if d.is_dir():
            return d
    return None


NOTHING_CHANGED = "NOTHING CHANGED"


NOT_DELIVERED = "NOT DELIVERED"


RENDERING_FAILED = "RENDERING FAILED"


PROGRAMMATIC_MISSING = "PROGRAMMATIC SCORES MISSING"


def _searchable(d: Path) -> None:
    """Give a collected directory this host owns its owner read and search bits back. cp -a in the container keeps
    the mode the agent gave it (`chmod -R 644 output`) and Harbor's chown -R changes only the owner, so it is ours
    yet cannot be listed or entered, and every file under it would read as absent."""
    try:
        st = d.lstat()
        if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and st.st_mode & 0o500 != 0o500:
            d.chmod(stat.S_IMODE(st.st_mode) | 0o500)
    except OSError:
        pass


def own_files(root: Path) -> list[Path]:
    """The regular files of a tree the container collected, as the host may read them: never a symlink (to
    a file or a directory) and never a path that resolves outside `root`. The collection comes from a
    container the agent ran in, and a link left there (output/notes.txt -> /proc/self/environ) would
    resolve on THIS host, handing the judge, and whatever it writes, the judge's own environment.
    A directory that still cannot be listed or entered raises CollectionUnreadable: what it holds is unknown,
    which is that trial's judge failing, never an empty deliverable."""
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        return []
    _searchable(root)
    base = root.resolve()
    out, errors = [], []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=errors.append):
        here = Path(dirpath)
        kept = []
        for n in sorted(dirnames):
            try:            # lstat, not Path.is_symlink(): that one reads a permission error as "no"
                if stat.S_ISLNK(os.lstat(here / n).st_mode):
                    continue
            except OSError as exc:
                errors.append(exc)
                continue
            _searchable(here / n)
            kept.append(n)
        dirnames[:] = kept
        for name in sorted(filenames):
            f = here / name
            try:
                if not stat.S_ISREG(os.lstat(f).st_mode):       # a link, a FIFO, a device: never the agent's file
                    continue
                f.resolve().relative_to(base)
            except ValueError:
                continue
            except OSError as exc:
                errors.append(exc)
                continue
            out.append(f)
    if errors:
        e = errors[0]
        raise CollectionUnreadable(f"cannot read the collected tree under {root}: "
                                   f"{getattr(e, 'filename', None) or ''} {getattr(e, 'strerror', None) or e}")
    return out


def clear_stage(stage: Path) -> None:
    """Remove a staging dir. It sits under verifier/, which is mounted while the agent runs, so it (or
    anything in it) may be a link the agent left: removed, never followed. One that cannot be removed
    stops the pass rather than have a copy write through what is left."""
    if stage.is_symlink() or stage.is_file():
        stage.unlink()
    shutil.rmtree(stage, ignore_errors=True)
    if stage.exists() or stage.is_symlink():
        sys.exit(f"cannot clear the staging dir {stage}: remove it by hand")


class CollectionUnreadable(OSError):
    """A collected file this host cannot read: that trial's judge fails."""


def readable(f: Path) -> Path:
    """`f`, made readable when this host owns it. cp -a in the container keeps the mode the agent gave a file
    and Harbor's chown -R changes only the owner, so a file the agent left without its read bit is ours yet
    unreadable: it gets u+r back (the content is what it delivered). One that still cannot be read raises
    CollectionUnreadable."""
    try:
        if not os.access(f, os.R_OK) and f.stat().st_uid == os.getuid():
            f.chmod(f.stat().st_mode | 0o400)
        if not os.access(f, os.R_OK):
            raise PermissionError(f"permission denied: {f}")
    except OSError as exc:
        raise CollectionUnreadable(f"cannot read the collected file {f}: {exc}") from None
    return f


MAX_STAGED_FILE_BYTES = 256 << 20


MAX_STAGED_BYTES = 1 << 30


TEXT_SUFFIXES = frozenset({".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".py", ".html", ".xml"})


TEXT_HEAD_BYTES = 16 << 20


NOT_READ = ".NOT_READ.txt"


class StageBudget:
    """The bytes a trial's stage may still take."""

    def __init__(self, total: int | None = None):
        self.left = MAX_STAGED_BYTES if total is None else total


def copy_collected(f: Path, dst: Path, budget: StageBudget) -> None:
    """Copy one collected file into the stage within `budget` (MAX_STAGED_BYTES), or leave a note in its place."""
    try:
        src = readable(f)
        size = src.stat().st_size
        take = size if size <= MAX_STAGED_FILE_BYTES else TEXT_HEAD_BYTES if f.suffix.lower() in TEXT_SUFFIXES else None
        if take is None:
            why = f"it is {size} bytes, over the grader's {MAX_STAGED_FILE_BYTES}-byte limit for one file"
        elif take > budget.left:
            why = f"it lies past the grader's {MAX_STAGED_BYTES}-byte limit for one deliverable"
        else:
            if take == size:
                shutil.copy2(src, dst)
            else:
                with src.open("rb") as fh, dst.open("xb") as out:
                    out.write(fh.read(take))
            budget.left -= take
            return
        dst.with_name(dst.name + NOT_READ).write_text(f"[the grader did not read {dst.name}: {why}]\n",
                                                      encoding="utf-8")
    except CollectionUnreadable:
        raise
    except OSError as exc:
        raise CollectionUnreadable(f"cannot copy the collected file {f}: {exc}") from None


def stage_copy(collected: Path, stage: Path) -> int:
    """Copy a collection's own files (own_files) into a fresh stage, keeping their relative paths, the answer before
    changed/ (the order the judge reads them in, so the budget goes to the answer first); the count of files
    copied. CollectionUnreadable when one cannot be read."""
    clear_stage(stage)
    files = sorted(own_files(collected), key=lambda f: (f.relative_to(collected).parts[0] == "changed", f))
    budget = StageBudget()
    for f in files:
        dst = stage / f.relative_to(collected)
        dst.parent.mkdir(parents=True, exist_ok=True)
        copy_collected(f, dst, budget)
    stage.mkdir(parents=True, exist_ok=True)
    return len(files)


def narrow_candidate(task_dir: Path, collected: Path, stage: Path) -> tuple[Path, str]:
    """Reduce a collected workspace to what the agent actually produced.

    The judge concatenates the candidate and hard-truncates at MAX_TEXT_CHARS = 350_000 (the
    judge's own constant). A world task's workspace is about a
    million characters of pre-existing scenery, so whether the deliverable is inside the window
    depends on where its path sorts, and a deliverable that falls outside it scores a confident
    0.000 that looks exactly like a failed agent.

    Narrowing to created/modified files follows the contract the world itself
    ships: its snapshot_diff.py helper defines the candidate as the diff of
    /app/files and /.apps_data against the pre-trajectory base. We diff against the pristine
    world instead of a container-side base, and keep binary content (the vendor's diff drops it)
    because the judge's own extract() reads xlsx/docx/pdf.

    Returns (candidate path, one-line description). Falls back to the whole collection when
    there is no world to diff against. Under the output/ contract nothing is filtered: output/ is the
    answer whatever it holds (a copy of a world file there is the agent's choice), and the rest was diffed
    in the container already.
    """
    if output_contract(task_dir):
        total = stage_copy(collected, stage)
        return stage, f"output/ contract collection ({total} file(s), world diffed in the container)"
    world = world_root(task_dir)
    if world is None:
        total = stage_copy(collected, stage)
        return stage, f"whole collection ({total} file(s); no pristine world to diff against)"
    orig = {p.relative_to(world).as_posix(): p.stat().st_size for p in world.rglob("*") if p.is_file()}

    def digest(p: Path) -> str:
        h = hashlib.sha256()
        with readable(p).open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    orig_hash = {}
    changed: list[tuple[str, Path]] = []
    collected_files = own_files(collected)
    for p in collected_files:
        rel = p.relative_to(collected).as_posix()
        src = world / rel
        if rel in orig and orig[rel] == p.stat().st_size:
            if rel not in orig_hash:
                orig_hash[rel] = digest(src)
            if orig_hash[rel] == digest(p):
                continue                      # untouched scenery
        changed.append((rel, p))

    # Anything the container collected from outside the world (/app/output, /.apps_data) is by
    # definition the agent's, so it is never scenery.
    outside = collected.parent / "outside"
    for p in own_files(outside):
        changed.append(("outside/" + p.relative_to(outside).as_posix(), p))

    if not changed:
        return collected, f"{NOTHING_CHANGED} -- agent left the world untouched"

    stage.mkdir(parents=True, exist_ok=True)
    budget = StageBudget()
    for rel, p in changed:
        dst = stage / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        copy_collected(p, dst, budget)
    return stage, f"{len(changed)} agent artifact(s) of {len(collected_files)} collected"


def declared_renderings(task_dir: Path) -> list[str]:
    """The rendering text files the task's criteria read, in the names the verifier writes them
    under: a PDF rendering gets a `.txt` twin, a text rendering is itself."""
    path = task_dir / "grader" / "criteria.json"
    if not path.is_file():
        return []                 # no rubric of its own (a task's own grader): it reads no renderings
    crit = json.loads(path.read_text(encoding="utf-8"))
    names = {r for c in crit.get("criteria", []) for r in (c.get("renderings") or [])}
    return sorted(n + ".txt" if n.lower().endswith(".pdf") else n for n in names if n.lower().endswith((".pdf", ".txt")))


HOST_FAILURE_LINE = re.compile(r"^(?:ERROR: LibreOffice conversion timed out|ERROR: LibreOffice not found)\s*$"
                               r"|^(?:MemoryError\b|OSError: \[Errno (?:12|28)\] )", re.M)


STATUS = re.compile(r"^== status (\d+) for (.+)$")


SOURCE = re.compile(r"^== source ([A-Za-z0-9+/=]+) for (.+)$")


HOST_EXIT = 124


MALFORMED_SIGNS = ("BadZipFile", "is not a zip file", "PackageNotFoundError", "InvalidFileException",
                   "PdfReadError", "PdfStreamError", "EmptyFileError", "There is no item named")


CONVERSION = re.compile(r"^== [^:\n]+: (.+) -> (.+)$")


OPC_ROOTS = {"[Content_Types].xml": "{http://schemas.openxmlformats.org/package/2006/content-types}Types",
             "_rels/.rels": "{http://schemas.openxmlformats.org/package/2006/relationships}Relationships"}


OFFICE_DOCUMENT_REL = ("http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
                       "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument")


OOXML_MAIN_NS = {suffix: ns for suffixes, ns in (
    ((".docx", ".docm"), ("http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                          "http://purl.oclc.org/ooxml/wordprocessingml/main")),
    ((".xlsx", ".xlsm"), ("http://schemas.openxmlformats.org/spreadsheetml/2006/main",
                          "http://purl.oclc.org/ooxml/spreadsheetml/main")),
    ((".pptx",), ("http://schemas.openxmlformats.org/presentationml/2006/main",
                  "http://purl.oclc.org/ooxml/presentationml/main"))) for suffix in suffixes}


OOXML_MAIN_TYPE = {".docx": ("wordprocessingml", "ms-word"), ".docm": ("wordprocessingml", "ms-word"),
                   ".xlsx": ("spreadsheetml", "ms-excel"), ".xlsm": ("spreadsheetml", "ms-excel"),
                   ".pptx": ("presentationml", "ms-powerpoint")}


MAX_MAIN_PART_BYTES = 64 << 20


MAX_CRC_TEST_BYTES = 256 << 20


WORKSPACE = "/app/files/"


def _conversion_logs(text: str) -> dict[str, tuple[list[str | bytes], str, list[int]]]:
    """transform.log split into what each attempted conversion printed, keyed by the rendering it was making:
    (the source paths it tried, in order -- an attempt per candidate path, so a rendering can have several; each the
    exact path below /app/files the collect step recorded (bytes), or, where it recorded none, the path as it
    was shown --, everything they printed, and the exit status of each attempt where the collect step recorded one)."""
    out: dict[str, tuple[list[str | bytes], str, list[int]]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("== "):
            status = STATUS.match(line)
            if status:
                name = status.group(2).strip()
                if name in out:
                    out[name][2].append(int(status.group(1)))
                current = None
                continue
            source = SOURCE.match(line)
            if source:
                if current == source.group(2).strip():
                    out[current][0][-1] = base64.b64decode(source.group(1))
                continue
            m = CONVERSION.match(line)
            current = m.group(2).strip() if m else None
            if current is not None:
                srcs, printed, codes = out.setdefault(current, ([], "", []))
                srcs.append(m.group(1).strip())
        elif current is not None:
            srcs, printed, codes = out[current]
            out[current] = (srcs, printed + line + "\n", codes)
    return out


def malformed_office_file(path: Path) -> str | None:
    """Why a delivered Office Open XML file cannot be the format its name claims, or None when it can (or when
    nothing cheap can tell: another format, an unreadable or oversized file). Never extracts more than the
    package's three small parts (content types, package relationships, main part), so a zip bomb costs nothing."""

    def part_name(target: str) -> str:
        """A package relationship's target resolved against the package root, as part names compare."""
        out: list[str] = []
        for seg in target.split("/"):
            if seg == "..":
                out = out[:-1]
            elif seg not in ("", "."):
                out.append(seg)
        return "/".join(out).lower()

    suffix = path.suffix.lower()
    if suffix not in OOXML_MAIN_NS or path.is_symlink() or not path.is_file():
        return None
    part = "[Content_Types].xml"
    try:
        if path.stat().st_size == 0:
            return "an empty file"
        with zipfile.ZipFile(path) as z:
            names: dict[str, str] = {}           # each part's name as part names compare -> as the zip has it
            for name in z.namelist():
                if name.endswith("/"):
                    continue
                if name.lower() in names:
                    return f"a zip naming the part {name} more than once"
                names[name.lower()] = name
            if sum(i.file_size for i in z.infolist()) <= MAX_CRC_TEST_BYTES:
                try:
                    bad = z.testzip()             # every part read through: its bytes must match its stored CRC
                except Exception as exc:          # a corrupt deflate stream (zlib.error), ...
                    return f"a zip with a part that cannot be decompressed ({type(exc).__name__})"
                if bad is not None:
                    return f"a zip whose part {bad} fails its CRC check: the file is corrupt"

            def read(name: str) -> tuple[bytes | None, str | None]:
                """(the part's bytes, None when it is too large to parse here; why it cannot be an Office part)"""
                info = z.getinfo(names[name.lower()])
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    return None, f"{name} compressed with zip method {info.compress_type}, which Office files never use"
                return (None if info.file_size > MAX_MAIN_PART_BYTES else z.read(info)), None

            if missing := [p for p in OPC_ROOTS if p.lower() not in names]:
                return f"a zip without {', '.join(missing)}"
            raw, trees = {}, {}
            for part in OPC_ROOTS:
                raw[part], why = read(part)
                if why or raw[part] is None:
                    return why
                try:
                    trees[part] = ET.fromstring(raw[part])
                except ET.ParseError as exc:
                    return f"{part} is not well-formed XML ({exc})"
                if trees[part].tag != OPC_ROOTS[part]:
                    return f"{part} has the root {trees[part].tag}, not {OPC_ROOTS[part]}"
            prefixed = re.search(rb"<([A-Za-z_][\w.-]*):[A-Za-z_]", raw["[Content_Types].xml"])
            if prefixed:
                return (f"[Content_Types].xml names its elements with the prefix {prefixed.group(1).decode()}: "
                        "Office reads the content types only unprefixed")
            relationship = OPC_ROOTS["_rels/.rels"][:-1]          # {...relationships}Relationship
            documents = sorted({part_name(r.get("Target") or "") for r in trees["_rels/.rels"].iter(relationship)
                                if r.get("Type") in OFFICE_DOCUMENT_REL and r.get("TargetMode") != "External"}
                               & set(names))
            if not documents:
                return ("_rels/.rels names no officeDocument relationship to a part of the zip: Office cannot find "
                        "the document")
            types = OPC_ROOTS["[Content_Types].xml"][:-len("Types")]    # {...content-types}
            overrides = {(o.get("PartName") or "").lstrip("/").lower(): o.get("ContentType") or ""
                         for o in trees["[Content_Types].xml"].iter(types + "Override")}
            defaults = {(d.get("Extension") or "").lower(): d.get("ContentType") or ""
                        for d in trees["[Content_Types].xml"].iter(types + "Default")}
            given = {doc: overrides.get(doc, defaults.get(doc.rsplit(".", 1)[-1], "")) for doc in documents}
            mains = [doc for doc, ct in given.items()
                     if ct.lower().endswith(".main+xml") and any(f in ct.lower() for f in OOXML_MAIN_TYPE[suffix])]
            if not mains:
                return (f"[Content_Types].xml gives {names[documents[0]]} the content type "
                        f"{given[documents[0]] or 'none'}, not its format's main document type: Office cannot tell "
                        "what the document is")
            part = names[mains[0]]
            data, why = read(part)
            if why or data is None:
                return why
            try:
                main = ET.fromstring(data)
            except ET.ParseError as exc:
                return f"{part} is not well-formed XML ({exc})"
    except zipfile.BadZipFile:
        return "not a zip file"
    except (OSError, MemoryError):
        return None
    except Exception as exc:        # a corrupt deflate stream (zlib.error), an XML encoding no parser knows, ...
        return f"{part} cannot be read ({type(exc).__name__})"
    if not any(main.tag.startswith("{" + ns + "}") for ns in OOXML_MAIN_NS[suffix]):
        return f"{part} has the root {main.tag}, in no namespace of its format"
    return None


def _delivered_copy(trial: Path, src: str | bytes) -> Path | None:
    """The collected copy of a file the collect step converted in the container, when it lies in the workspace
    (collected whole into verifier/submission/), made readable: the one file of the collection (own_files) at the
    exact path the step recorded (bytes, below /app/files) or, where the step recorded none, whose path reads as the log shows
    `src` -- control characters logged as '?', the log decoded with errors='replace'. None when no single file
    matches or it cannot be read."""
    if isinstance(src, str) and not src.startswith(WORKSPACE):
        return None
    sub = trial / "verifier" / "submission"

    def matches(rel: bytes) -> bool:
        if isinstance(src, bytes):
            return rel == src
        return re.sub(rb"[\x00-\x1f\x7f]", b"?", rel).decode("utf-8", "replace") == src[len(WORKSPACE):]
    try:
        hits = [f for f in own_files(sub) if matches(os.fsencode(f.relative_to(sub)))]
        return readable(hits[0]) if len(hits) == 1 else None
    except CollectionUnreadable:
        return None


def rendering_failures(task_dir: Path, trial: Path) -> tuple[list[str], list[str]]:
    """(failed on our side, unreadable deliverable) among the declared renderings the verifier set out to make
    from a deliverable it found and did not make. The collect step logs `== <transformation>: <source> ->
    <rendering>` before each conversion and `deliverable <name> not found` when there was nothing to
    convert, so a missing rendering with a logged conversion (or a PDF with no text twin) is a conversion
    that failed. It failed on our side -- LibreOffice timing out on a loaded host -- unless what the converter
    printed shows it could not open the file (a CSV saved as .xlsx: BadZipFile), which is the deliverable's
    own fault: the judge then fails the criteria that read that rendering, as the vendor's flow would. An
    error that names neither cause is the deliverable's when every file the converter tried is a malformed Office
    document (malformed_office_file), and ours otherwise."""
    rend = trial / "verifier" / "renderings"
    log = rend / "transform.log"
    logs = _conversion_logs(log.read_text(encoding="utf-8", errors="replace") if log.is_file() else "")
    ours, unreadable = [], []
    for name in declared_renderings(task_dir):
        if (rend / name).is_file():
            continue
        made = name[:-len(".txt")] if name.lower().endswith(".pdf.txt") else name
        if made not in logs and not (made != name and (rend / made).is_file()):
            continue                                  # never attempted: not delivered
        srcs, printed, codes = logs.get(made, ([], "", []))
        host = HOST_FAILURE_LINE.search(printed) is not None or any(c >= HOST_EXIT for c in codes)
        failed_conversion = made in logs and not (rend / made).is_file() and not host
        if failed_conversion and (any(s in printed for s in MALFORMED_SIGNS) or (
                srcs and all((f := _delivered_copy(trial, src)) is not None and malformed_office_file(f)
                             for src in srcs))):
            # the converter said the file is not its format, or (LibreOffice saying only that it failed) every
            # file it tried is a broken Office document: the deliverable's own fault
            unreadable.append(name)
        else:
            ours.append(name)
    return ours, unreadable


def failed_renderings(task_dir: Path, trial: Path) -> list[str]:
    """The declared renderings that failed on our side (rendering_failures): a trial with any is not measured."""
    return rendering_failures(task_dir, trial)[0]


def missing_programmatic(task_dir: Path, trial: Path) -> list[str]:
    """The declared programmatic items whose score the verifier did not leave in programmatic/<verifier id>.json.
    The verifier runs every item whether or not the document was delivered, and the item's
    code_verifier writes the score only when it ran to the end, so a missing one is the grader failing."""
    crit = json.loads((task_dir / "grader" / "criteria.json").read_text(encoding="utf-8"))
    prog = trial / "verifier" / "programmatic"
    out = []
    for c in crit.get("criteria", []):
        vid = (c.get("programmatic") or {}).get("verifier_id") if isinstance(c.get("programmatic"), dict) else None
        score = prog / f"{vid}.json"
        if vid and (score.is_symlink() or not score.is_file()):     # a link is the agent's, never followed
            out.append(vid)
    return out


def programmatic_declared(task_dir: Path) -> bool:
    """Whether the task's rubric has programmatic items, whose scores the verifier leaves in programmatic/."""
    crit = json.loads((task_dir / "grader" / "criteria.json").read_text(encoding="utf-8"))
    return any(c.get("programmatic") for c in crit.get("criteria", []))


def candidate_for_trial(task_dir: Path, trial: Path, stage: Path) -> tuple[Path, str]:
    """What the judge reads. World verifiers render the deliverable the way the vendor's shutdown
    step does (PDF -> text twin, live-formula dump); for a task whose criteria declare renderings,
    those ARE the candidate, plus the agent's final message, and when none was made the deliverable
    was not delivered: the collected workspace holds the task's own inputs too, so it is no stand-in.
    Other tasks never read renderings/ -- the agent can write there -- and are narrowed to the
    agent's own files."""
    # /logs/verifier is mounted during the agent phase, so the staging dir may hold what the agent put there
    clear_stage(stage)
    wanted = declared_renderings(task_dir)
    if not wanted:
        return narrow_candidate(task_dir, trial / "verifier" / "submission", stage)
    failed, unreadable = rendering_failures(task_dir, trial)
    if failed:
        # the judge would read a partial candidate and fail the criteria tied to it: our fault, not a grade
        return stage, f"{RENDERING_FAILED} -- {', '.join(failed)} not made from the delivered file " \
                      f"(verifier/renderings/transform.log)"
    if programmatic_declared(task_dir) and (missing := missing_programmatic(task_dir, trial)):
        # without them the judge would fail those items (or, with nothing else staged, the trial would read
        # NOT DELIVERED): the grader's failure, not the agent's 0
        return stage, f"{PROGRAMMATIC_MISSING} -- no score for {', '.join(missing)} (verifier/programmatic/)"
    rend = trial / "verifier" / "renderings"
    texts = [rend / n for n in wanted if (rend / n).is_file() and not (rend / n).is_symlink()]
    # programmatic items are scored whether or not the document was delivered (their judge fails the
    # unrendered criteria on its own), so their scores are staged either way
    prog = [p for p in own_files(trial / "verifier" / "programmatic") if p.suffix == ".json" and p.parent.name == "programmatic"] \
        if programmatic_declared(task_dir) else []
    if not texts and not prog:
        why = f"{', '.join(unreadable)} could not be read from the delivered file" if unreadable \
            else f"no rendering of {', '.join(wanted)}"
        return stage, f"{NOT_DELIVERED} -- {why}"
    stage.mkdir(parents=True, exist_ok=True)
    budget = StageBudget()
    final = trial / "verifier" / "submission" / "FINAL_MESSAGE.md"
    has_final = final.is_file() and not final.is_symlink()
    if has_final:                       # the message first: a bound criterion reads it before its renderings
        copy_collected(final, stage / final.name, budget)
    for p in texts:
        copy_collected(p, stage / p.name, budget)
    for p in prog:
        (stage / "programmatic").mkdir(exist_ok=True)
        copy_collected(p, stage / "programmatic" / p.name, budget)
    return stage, (f"{len(texts)} vendor rendering text(s)" + (" + final message" if has_final else "")
                   + (f" + {len(prog)} programmatic score(s)" if prog else "")
                   + (f"; {', '.join(unreadable)} unreadable (its criteria fail)" if unreadable else ""))


# ------------------------------------------------ the verdict
def write_failure(out: Path, result: dict, error: str) -> int:
    """No grade: the evidence goes to <out stem>.failed.json, never to --out, which only a grade writes."""
    path = out.with_name(out.stem + ".failed.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(result, grader_failed=error), indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"GRADER FAILED: {error}", file=sys.stderr)
    return 1


def write_grade(out: Path, result: dict) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "grades"}, indent=2))
    return 0


def judge(candidate: Path, out: Path, model: str) -> dict:
    """grader/grade.py over the staged candidate: its grade, or {"error": ...}.
    grade.py reads PDFs with the pypdf wheel in grader/_vendor, whatever the image holds."""
    tmp = out.with_name(f".{out.name}.partial")
    tmp.unlink(missing_ok=True)
    argv = [sys.executable, str(GRADER / "grade.py"), "--candidate", str(candidate), "--out", str(tmp),
            "--backend", "openai", "--model", model]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, cwd=str(HERE), timeout=JUDGE_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return {"error": f"judge timed out after {JUDGE_TIMEOUT_SEC} s"}
    sys.stderr.write(p.stderr[-20000:])
    if p.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        return {"error": (p.stderr or p.stdout or "judge failed").strip()[-300:]}
    try:
        grade = json.loads(tmp.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:        # a write cut short: no grade, the failure's evidence still goes out
        return {"error": f"grade.py wrote no readable grade ({type(exc).__name__}: {exc})"[:300]}
    finally:
        tmp.unlink(missing_ok=True)
    if not isinstance(grade, dict):
        return {"error": f"grade.py wrote {type(grade).__name__}, not a grade"}
    return grade


def main() -> int:
    """The verdict over one trial: our failures are not measured, a deliverable that is not there
    scores 0, anything else is judged."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--submission", required=True, type=Path, help="the collected deliverable: <trial>/verifier/submission")
    ap.add_argument("--out", type=Path, default=Path("grade.json"))
    ap.add_argument("--model", default=os.environ.get("JUDGE_MODEL") or DEFAULT_JUDGE_MODEL)
    a = ap.parse_args()
    sub = a.submission
    trial = sub.parent.parent
    result = {"task_id": TASK, "judge_model": a.model}
    try:
        if declared_renderings(TASK_DIR) and (unmade := failed_renderings(TASK_DIR, trial)):
            return write_failure(a.out, result, f"{RENDERING_FAILED}: {', '.join(unmade)} (renderings/transform.log)")
        if programmatic_declared(TASK_DIR) and (missing := missing_programmatic(TASK_DIR, trial)):
            return write_failure(a.out, result, f"{PROGRAMMATIC_MISSING}: {', '.join(missing)} (programmatic/)")
        if not (sub.is_dir() and not sub.is_symlink() and own_files(sub)):
            return write_grade(a.out, {**result, "reward": 0.0, "candidate": "no deliverable collected"})
        cand, how = candidate_for_trial(TASK_DIR, trial, trial / "verifier" / "candidate")
    except CollectionUnreadable as exc:
        return write_failure(a.out, result, str(exc))
    if how.startswith((RENDERING_FAILED, PROGRAMMATIC_MISSING)):
        return write_failure(a.out, result, how)
    if how.startswith((NOTHING_CHANGED, NOT_DELIVERED)):
        return write_grade(a.out, {**result, "reward": 0.0, "candidate": how})
    grade = judge(cand, a.out, a.model)
    if "error" in grade:
        return write_failure(a.out, {**result, "candidate": how}, f"judge failed: {grade['error']}")
    return write_grade(a.out, {**grade, "reward": grade.get("primary_score", grade.get("score")), "candidate": how})


if __name__ == "__main__":
    raise SystemExit(main())
