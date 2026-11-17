#!/usr/bin/env python3
# The final score for this task (0.4 p + 0.6 [h=1] O', see below), from its grader's own output: tests/test.sh runs this after the grader.


from __future__ import annotations

import json
from pathlib import Path

FILES = ("verifier/metrics.json", "verifier/reward.txt")
NEEDS_SUBMISSION = False

HARD = ("G1_FOOTPRINT", "G2_PARAPET_HEIGHT", "G3_PIERS", "G4_ZONE_ASSIGN", "G5_TYPE_CAP", "G6_CATALOGUE",
        "G7_CHORD_ASSIGN", "G8_SPAN_FACTOR", "G9_ASD_CAPACITY", "G10_CAP", "G11_EMBED_SPACING",
        "F1_DECK_CAPACITY", "F2_CHORD_CAPACITY", "F3_COLLECTOR_CAPACITY", "F4_PIER_BASE", "F5_EMBED_CAPACITY",
        "F6_OVERTURNING", "F7_DIAPH_DRIFT", "F8_PIER_DRIFT", "F9_SHEAR_AGREE", "F10_REACT_AGREE",
        "F11_LINELOAD_AGREE")
SOFT = ("G12_DXF", "G13_PDF", "G14_SCHEDULE", "G15_GCI", "F12_CHORD_AGREE", "F13_DRIFT_AGREE",
        "D1_GOVERNING", "D2_KZT", "D3_KE", "D4_QH", "D5_SCREEN", "D6_ASBUILT_DOOR", "D7_ASD", "D8_MODEL")
KEYS = set(HARD) | set(SOFT)
N_ALL, N_HARD = 36, 22
HELDOUT = ("W02", "W03", "W05", "W06", "W08", "W10", "W11")
assert len(KEYS) == N_ALL and len(HARD) == N_HARD



# ------------------------------------------------ final scoring rule
# Each item scores  s = 0.4 p + 0.6 [h = 1] O',  O' = clip((O - O_naive) / (1 - O_naive), 0, 1),  and the task the
# mean over its items. p is the item's satisfied-constraint fraction, h its hard-constraint fraction, O its objective
# (lower bound / cost: 1 = the bound, which is the only ceiling), O_naive the O of the task's naive design.
# O_naive: no naive design usable for this task: O_naive = 0.
FINAL_W_P, FINAL_W_GO = 0.4, 0.6
O_NAIVE = 0.0


def _final_item(p: float, h: float, O: float, o_naive: float = O_NAIVE) -> float:
    gate = 1.0 if h >= 1.0 - 1e-9 else 0.0
    o = min(1.0, max(0.0, ((O or 0.0) - o_naive) / (1.0 - o_naive)))
    return FINAL_W_P * p + FINAL_W_GO * gate * o

class NotRecomputable(Exception):
    pass


def _f(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def load(trial_dir: Path, extracted: dict | None = None) -> dict:
    t = Path(trial_dir)
    v = t / "verifier" if (t / "verifier").is_dir() else t
    try:
        reward = float((v / "reward.txt").read_text().strip())
    except (OSError, ValueError):
        reward = None
    return {"metrics": _json(v / "metrics.json"), "reward": reward, "trial": str(t)}


def rescore(bundle: dict) -> dict:
    m = bundle.get("metrics")
    stored = bundle.get("reward")
    if stored is None and isinstance(m, dict):
        stored = _f(m.get("harbor_reward"))
    if not isinstance(m, dict):
        if not stored:
            return {"stored": 0.0, "current": 0.0, "eq1": 0.0, "items": 0, "note": "ungraded"}
        raise NotRecomputable(f"stored reward {stored} without verifier metrics")
    stored = float(stored or 0.0)
    status = str(m.get("exec_status", m.get("status", "")))
    valid = _f(m.get("valid")) or 0.0
    if status.lower() != "ok" or valid <= 0.0 or not m.get("parse_ok"):
        if stored != 0.0:
            raise NotRecomputable(f"stored reward {stored} but status={status} valid={valid}")
        return {"stored": stored, "current": 0.0, "eq1": 0.0, "items": 0,
                "note": f"nothing gradable (exec_status={status}, valid={valid}, parse_ok={m.get('parse_ok')})"}
    scn = m.get("scenarios")
    if not isinstance(scn, dict) or not scn:
        raise NotRecomputable("parse_ok but no per-scenario record")
    if m.get("split", "heldout") == "heldout" and set(scn) != set(HELDOUT):
        raise NotRecomputable(f"unexpected scenario set {sorted(scn)}")
    O_paid, O_raw = _f(m.get("objective_paid")), _f(m.get("objective_raw"))
    if O_paid is None or O_raw is None:
        raise NotRecomputable("objective_paid / objective_raw missing")
    cur = eq = 0.0
    hs = []
    for sid, s in scn.items():
        failed, unreg = set(s.get("failed") or []), set(s.get("unregistered") or [])
        if not (failed | unreg) <= KEYS:
            raise NotRecomputable(f"{sid}: unknown family {sorted((failed | unreg) - KEYS)}")
        miss = failed | unreg
        p = (N_ALL - len(miss)) / N_ALL
        if abs(p - float(s["p"])) > 1e-9:
            raise NotRecomputable(f"{sid}: p from lists {p} != stored p {s['p']}")
        G = 1.0 if not (miss & set(HARD)) else 0.0
        A = 1.0 if not miss else 0.0
        h = (N_HARD - len(miss & set(HARD))) / N_HARD
        hs.append(h)
        cur += 0.40 * p * p + 0.40 * G * O_paid + 0.20 * A * O_paid
        eq += _final_item(p, h, O_raw)
    n = len(scn)
    note = (f"O_raw={O_raw:.4f} paid={O_paid:.4f} feasible_all={m.get('feasible_all_scenarios')} "
            f"mean_h={sum(hs) / n:.4f} min_h={min(hs):.4f}")
    if (m.get("register") or {}).get("voided"):
        note += " register voided (precision<0.6): no family registered"
    return {"stored": stored, "current": cur / n, "eq1": eq / n, "items": n, "note": note}


# ------------------------------------------------ in the verifier: the grader's output -> the paper's score
# The re-score above reads a trial folder (verifier/ with the grader's outputs, verifier/submission/submission.zip);
# this builds one from the verifier's own files and applies the result. tests/test.sh runs it twice, once per role:
#
#   paper_score.py score --verifier /logs/verifier --submission <the collected submission.zip> --grader-dir <dir>
#       as the grader's user where the grader runs as one: re-scores and prints one JSON object; exits non-zero,
#       printing nothing, when the grade cannot be re-scored (the trial is then not measured, never scored by the
#       grader's own rule)
#   paper_score.py apply --result <that JSON> --grade /logs/verifier/grade.json
#       as root: grade.json's reward becomes the paper's score, its own reward kept as grader_reward
#   paper_score.py apply-native --result <that JSON> --verifier /logs/verifier
#       as root, for a grader that writes reward.txt: it becomes the paper's score, the grader's own kept as
#       reward.delivered.txt, the record written to paper_score.json (metrics.json stays as the grader wrote it)
import argparse
import json
import math
import os
import sys
import tempfile
from pathlib import Path

PAPER_SCORING = "final scoring rule: 0.4 p + 0.6 [h=1] O'"
# rescore() also rebuilds the grader's own rule from the stored components ("current"): a rebuild that differs from
# the grader's reward means the grade was not read the way the re-score expects, so no reward is written
CHECK_TOL = 1e-5


class PaperScoreError(Exception):
    """The grade could not be re-scored: no reward is written."""


def _number(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _grader_folder(grader_dir: Path, into: Path) -> Path:
    """The folder EQ1_GRADER_DIR names: for a verifier image that installs the grading folder itself (its
    verification/ at the top, no grading/ inside), a folder whose grading/ links to it; else grader_dir itself (the
    folder holding the grader, as the re-score looks for it)."""
    if not grader_dir.is_dir():
        raise PaperScoreError(f"no grader folder at {grader_dir}")
    if (grader_dir / "verification").is_dir() and not (grader_dir / "grading").exists():
        root = into / "grader"
        root.mkdir()
        (root / "grading").symlink_to(grader_dir.resolve(), target_is_directory=True)
        return root
    return grader_dir


def build_trial(verifier: Path, submission: Path | None, into: Path) -> Path:
    """A trial folder in `into`: verifier/ links each regular file at the top of `verifier`, adds reward.json from
    grade.json's reward when the grader wrote no reward file, and links submission/submission.zip to `submission`
    when that is a regular file."""
    trial = into / "trial"
    v = trial / "verifier"
    v.mkdir(parents=True)
    for p in sorted(verifier.iterdir()):
        if p.name != "submission" and p.is_file() and not p.is_symlink():
            (v / p.name).symlink_to(p.resolve())
    if not (v / "reward.json").exists() and not (v / "reward.txt").exists() and (v / "grade.json").is_file():
        try:
            reward = json.loads((v / "grade.json").read_text(encoding="utf-8")).get("reward")
        except (OSError, ValueError, AttributeError):
            reward = None
        if _number(reward) is None:
            raise PaperScoreError("grade.json holds no numeric reward")
        (v / "reward.json").write_text(json.dumps({"reward": reward}), encoding="utf-8")
    if submission is not None and submission.is_file() and not submission.is_symlink():
        (v / "submission").mkdir()
        (v / "submission" / "submission.zip").symlink_to(submission.resolve())
    return trial


def paper_score(verifier: Path, submission: Path | None, grader_dir: Path) -> dict:
    """The paper's score of the grade in `verifier` (the record apply writes); PaperScoreError when there is none."""
    with tempfile.TemporaryDirectory(prefix="paper_score_") as tmp:
        tmp = Path(tmp)
        os.environ["EQ1_GRADER_DIR"] = str(_grader_folder(grader_dir, tmp))
        trial = build_trial(verifier, submission, tmp)
        try:
            extracted = extract(trial) if NEEDS_SUBMISSION else None
            r = rescore(load(trial, extracted))
        except NotRecomputable as e:
            raise PaperScoreError(f"not recomputable: {e}") from None
    eq1, stored, current = _number(r.get("eq1")), _number(r.get("stored")), _number(r.get("current"))
    if eq1 is None or not 0.0 <= eq1 <= 1.0:
        raise PaperScoreError(f"the re-score gives {r.get('eq1')!r}, not a number from 0 to 1")
    if stored is None or current is None:
        raise PaperScoreError(f"the re-score read no grader reward ({r.get('stored')!r}) or rebuilt none "
                              f"({r.get('current')!r})")
    if abs(current - stored) > CHECK_TOL:
        raise PaperScoreError(f"the re-score rebuilds the grader's rule as {current!r}, the grader gave {stored!r}")
    return {"scoring": PAPER_SCORING, "reward": eq1, "grader_reward": stored, "grader_rule_rebuilt": current,
            "items": r.get("items"), "note": r.get("note")}


def read_result(path: Path) -> dict:
    """The record `score` printed, checked again by the process that writes the reward."""
    try:
        res = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PaperScoreError(f"no paper score record: {e}") from None
    if not isinstance(res, dict) or res.get("scoring") != PAPER_SCORING:
        raise PaperScoreError("the paper score record is not one")
    reward, grader = _number(res.get("reward")), _number(res.get("grader_reward"))
    if reward is None or not 0.0 <= reward <= 1.0 or grader is None:
        raise PaperScoreError(f"the paper score record holds reward {res.get('reward')!r}, grader reward "
                              f"{res.get('grader_reward')!r}")
    return res


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.paper")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o644)
    os.replace(tmp, path)


def apply_grade(result: Path, grade: Path) -> float:
    """grade.json with the paper's score as its reward and the grader's own as grader_reward."""
    res = read_result(result)
    g = json.loads(grade.read_text(encoding="utf-8"))
    if not isinstance(g, dict) or "grader_reward" in g:
        raise PaperScoreError("grade.json is not a grader's grade, or was re-scored already")
    own = _number(g.get("reward"))
    if own is None or abs(own - res["grader_reward"]) > CHECK_TOL:
        raise PaperScoreError(f"grade.json's reward {g.get('reward')!r} is not the one re-scored "
                              f"({res['grader_reward']!r})")
    out = {**g, "reward": res["reward"], "grader_reward": g["reward"], "scoring": PAPER_SCORING,
           "paper_score": {k: res.get(k) for k in ("grader_rule_rebuilt", "items", "note")}}
    _write(grade, json.dumps(out, indent=2, ensure_ascii=False, default=str) + "\n")
    return res["reward"]


def apply_native(result: Path, verifier: Path) -> float:
    """reward.txt as the paper's score; the grader's reward kept as reward.delivered.txt, the record as
    paper_score.json."""
    res = read_result(result)
    delivered = (verifier / "reward.txt").read_text(encoding="utf-8")
    own = _number(float(delivered.strip()))
    if own is None or abs(own - res["grader_reward"]) > CHECK_TOL:
        raise PaperScoreError(f"reward.txt holds {delivered.strip()!r}, not the reward re-scored "
                              f"({res['grader_reward']!r})")
    _write(verifier / "reward.delivered.txt", delivered)
    _write(verifier / "paper_score.json", json.dumps(res, indent=2, ensure_ascii=False, default=str) + "\n")
    (verifier / "reward.json").unlink(missing_ok=True)
    _write(verifier / "reward.txt", f"{res['reward']:.17g}\n")
    return res["reward"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="the paper's score of this task's grade")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score")
    s.add_argument("--verifier", type=Path, required=True)
    s.add_argument("--submission", type=Path)
    s.add_argument("--grader-dir", type=Path, required=True)
    a_ = sub.add_parser("apply")
    a_.add_argument("--result", type=Path, required=True)
    a_.add_argument("--grade", type=Path, required=True)
    n = sub.add_parser("apply-native")
    n.add_argument("--result", type=Path, required=True)
    n.add_argument("--verifier", type=Path, required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "score":
            # the grader's code the re-score runs may print, itself or in a child process: everything but the record
            # goes to stderr
            out = os.fdopen(os.dup(1), "w")
            sys.stdout.flush()
            os.dup2(2, 1)
            out.write(json.dumps(paper_score(a.verifier, a.submission, a.grader_dir)) + "\n")
            out.close()
        elif a.cmd == "apply":
            print(f"paper score {apply_grade(a.result, a.grade)!r} -> {a.grade}", file=sys.stderr)
        else:
            print(f"paper score {apply_native(a.result, a.verifier)!r} -> {a.verifier / 'reward.txt'}",
                  file=sys.stderr)
    except PaperScoreError as e:
        print(f"PAPER SCORE FAILED: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 -- any failure leaves the trial unmeasured, never scored another way
        print(f"PAPER SCORE FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
