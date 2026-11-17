"""grade.py - grade one candidate package.

    python -m grading.grade <submission.zip> [--split heldout|dev] [--metrics-out m.json]

Per graded wind scenario:
    P = 1 if the package is present and parseable, else 0
    p = families ENGINEERED / 36        (engineered = satisfied AND registered)
    G = P and every HARD family engineered           - the objective gate
    A = P and every family engineered                - the full-compliance flag
    O = clip(certified bound / installed cost, 0, 1), paid only when the design is hard-feasible
        in ALL twelve scenarios
    score = 0.40 p^2 + 0.40 G O + 0.20 A O
The task score is the mean over the split's scenarios.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from .checks import Checks
from .cost import certified_bound, design_cost, min_embeds
from .families import HARD, KEYS, SCOPE
from .model import Analysis, Design, truth
from .register import RegisterMap
from .submission import Submission

HERE = Path(__file__).resolve().parent


def design_from_submission(T, sub) -> Design:
    # identifiers are matched case-insensitively; the catalogue codes are matched as written,
    # because a deck code is a product code
    zone_id = {z["id"].upper(): z["id"] for z in T["zones"]}
    chord_id = {c["id"].upper(): c["id"] for c in T["chord_lines"]}
    panel_id = {p["mark"].upper(): p["mark"]
                for rows in T["panels"].values() for p in rows}
    deck = {zone_id.get(r["zone"].upper(), r["zone"]): r["code"]
            for r in sub.deck_zones if r["zone"]}
    chords = {chord_id.get(r["chord"].upper(), r["chord"]): r["code"]
              for r in sub.chord_schedule if r["chord"]}
    embeds, tiedowns = {}, []
    for r in sub.piers:
        if not r["panel"]:
            continue
        mark = panel_id.get(r["panel"].upper(), r["panel"])
        n = r["embeds"]
        if n is not None:
            embeds[mark] = max(int(embeds.get(mark, 0)), int(n))
        if str(r["tiedown"]).strip().lower() in ("y", "yes", "true", "1", "t1", "required",
                                                 "tiedown", "tie-down"):
            tiedowns.append(mark)
    for rows in T["panels"].values():
        for p in rows:
            embeds.setdefault(p["mark"], 0)
    return Design(deck, chords, embeds, sorted(set(tiedowns)))


def grade(zip_path, split="heldout"):
    T = truth()
    metrics = {"status": "ok", "parse_ok": False, "combined_score": 0.0, "split": split}
    sub = Submission.load(Path(zip_path))
    metrics["parse_errors"] = sub.errors[:10]
    bnd = json.loads((HERE / "data" / "bound.json").read_text())
    bound = bnd["bound"]
    metrics["bound"] = bound
    if not sub.parse_ok:
        metrics["scenarios"] = {}
        return metrics

    design = design_from_submission(T, sub)
    A = Analysis(T, design)
    res = A.analyse()
    ck = Checks(T, sub, design, A, res)
    model_res = ck.model_checks()

    rm = RegisterMap()
    reg = rm.score(sub.register)
    registered = set(reg["registered_families"])
    if reg["precision"] < reg["precision_floor"]:
        registered = set()
        reg["voided"] = True
    metrics["register"] = {k: v for k, v in reg.items() if k != "detail"}

    cost = design_cost(T, design)
    metrics["installed_cost"] = cost["total"]
    metrics["cost_parts"] = {k: v for k, v in cost.items() if k != "invalid"}

    per_scn = {}
    hard_ok_all = True
    for sc in T["scenarios"]:
        sid = sc["id"]
        sres = ck.scenario_checks(sc, res[sid])
        fam = {}
        for k in KEYS:
            r = model_res[k] if SCOPE[k] == "model" else sres[k]
            fam[k] = {"satisfied": bool(r["ok"]), "registered": k in registered,
                      "engineered": bool(r["ok"]) and k in registered,
                      "detail": {a: b for a, b in r.items() if a != "ok"}}
        hard_sat = all(fam[k]["satisfied"] for k in HARD)
        if not hard_sat:
            hard_ok_all = False
        per_scn[sid] = {"families": fam,
                        "p": sum(1 for k in KEYS if fam[k]["engineered"]) / len(KEYS),
                        "hard_engineered": all(fam[k]["engineered"] for k in HARD),
                        "all_engineered": all(fam[k]["engineered"] for k in KEYS),
                        "hard_satisfied": hard_sat}

    O_raw = min(max(bound / cost["total"], 0.0), 1.0) if cost["total"] > 0 else 0.0
    metrics["objective_raw"] = O_raw
    metrics["feasible_all_scenarios"] = hard_ok_all
    O = O_raw if hard_ok_all else 0.0
    metrics["objective_paid"] = O

    ids = T["split"][split]
    total = 0.0
    for sid in ids:
        s = per_scn[sid]
        G = 1.0 if s["hard_engineered"] else 0.0
        Aa = 1.0 if s["all_engineered"] else 0.0
        total += 0.40 * s["p"] ** 2 + 0.40 * G * O + 0.20 * Aa * O
    metrics["combined_score"] = total / len(ids)
    metrics["mean_p"] = sum(per_scn[s]["p"] for s in ids) / len(ids)
    metrics["hard_rate"] = sum(1.0 for s in ids if per_scn[s]["hard_engineered"]) / len(ids)
    metrics["full_compliance_rate"] = sum(1.0 for s in ids
                                          if per_scn[s]["all_engineered"]) / len(ids)
    metrics["mean_objective"] = O
    metrics["parse_ok"] = True
    metrics["max_deflection_in"] = max(max(r["deflection"].values()) for r in res.values())
    metrics["max_unit_shear_plf"] = max(r["max_unit_shear_plf"] for r in res.values())
    metrics["scenarios"] = {
        sid: {"p": per_scn[sid]["p"],
              "failed": [k for k in KEYS if not per_scn[sid]["families"][k]["satisfied"]],
              "unregistered": [k for k in KEYS if per_scn[sid]["families"][k]["satisfied"]
                               and not per_scn[sid]["families"][k]["registered"]]}
        for sid in ids}
    metrics["_detail"] = per_scn
    return metrics


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("submission")
    ap.add_argument("--split", default="heldout")
    ap.add_argument("--metrics-out", default=None)
    ap.add_argument("--full", action="store_true")
    a = ap.parse_args(argv)
    try:
        m = grade(a.submission, a.split)
    except Exception:  # noqa: BLE001
        m = {"status": "ERR_EXEC", "combined_score": 0.0, "parse_ok": False,
             "traceback": traceback.format_exc()[-4000:]}
    out = m if a.full else {k: v for k, v in m.items() if k != "_detail"}
    if a.metrics_out:
        Path(a.metrics_out).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items()
                      if k in ("status", "combined_score", "parse_ok", "mean_p", "hard_rate",
                               "objective_raw", "installed_cost", "feasible_all_scenarios")},
                     indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
