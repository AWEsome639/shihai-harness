"""Score a submission: families per scenario, register gating, objective against the certified bound.

    score_item = 0.40 p^2 + 0.40 G O + 0.20 A O

A family is ENGINEERED on an item when it is satisfied (on that item's scenario for the scenario
families, on the whole design for the rest) AND the requirements that govern it appear in the
submitted constraint register.  G = the package parses and every HARD family is engineered.
A = every family is engineered.  O = clip(certified cost bound / installed cost, 0, 1), paid only on
an item with G = 1 and only when the design is hard-feasible in all ten scenarios, because the bound
bounds fully compliant designs.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from . import checks as CH
from .model import load_submission
from .register import registered_families, score_register

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

HARD = CH.HARD
SOFT = CH.SOFT
FAMILIES = CH.FAMILIES


def _load(name):
    return json.loads((DATA / name).read_text())


def grade(zip_path: str, split: str = "heldout") -> dict:
    truth = _load("truth.json")
    rmap = _load("register_map.json")
    bound = _load("bound.json")
    items = truth["splits"][split]
    all_scen = [s["id"] for s in truth["scenarios"]]

    sub = load_submission(zip_path, truth)
    metrics = {"split": split, "items": items, "parse_ok": sub.parse_ok, "parse_errors": sub.errors[:10]}
    if not sub.parse_ok:
        metrics.update({"combined_score": 0.0, "exec_status": "ok", "mean_p": 0.0, "hard_pass_rate": 0.0,
                        "full_compliance_rate": 0.0, "mean_objective": 0.0,
                        "per_item": {i: 0.0 for i in items}})
        return metrics

    try:
        ana = CH.analyse(sub, truth)
    except Exception as e:  # noqa: BLE001
        metrics.update({"combined_score": 0.0, "exec_status": "ok", "mean_p": 0.0, "hard_pass_rate": 0.0,
                        "full_compliance_rate": 0.0, "mean_objective": 0.0,
                        "per_item": {i: 0.0 for i in items},
                        "analysis_error": f"{type(e).__name__}: {e}"[:300]})
        return metrics

    reg = score_register(sub.register_rows, rmap)
    registered = registered_families(rmap, reg, FAMILIES)

    def ok(f, sid):
        return CH.family_ok(ana, f, sid)

    feasible_all = all(ok(f, s) and registered[f] for s in all_scen for f in HARD)
    cost = ana["cost"]
    c_bound = bound["cost_bound"]
    O = float(min(1.0, c_bound / cost)) if (cost and math.isfinite(cost) and cost > 0) else 0.0

    per_item, per_fam = {}, {}
    ps, Gs, As, Os = [], [], [], []
    for sid in items:
        eng = {f: (ok(f, sid) and registered[f]) for f in FAMILIES}
        p = sum(eng.values()) / len(FAMILIES)
        Gi = all(eng[f] for f in HARD)
        Ai = all(eng.values())
        o = O if (Gi and feasible_all) else 0.0
        s = 0.40 * p ** 2 + 0.40 * (1.0 if Gi else 0.0) * o + 0.20 * (1.0 if Ai else 0.0) * o
        per_item[sid] = round(s, 6)
        per_fam[sid] = {f: int(v) for f, v in eng.items()}
        ps.append(p)
        Gs.append(Gi)
        As.append(Ai)
        Os.append(o)

    score = float(np.mean(list(per_item.values()))) if per_item else 0.0
    satisfied = {f: all(ok(f, s) for s in items) for f in FAMILIES}
    metrics.update({
        "combined_score": round(score, 6), "exec_status": "ok",
        "mean_p": float(np.mean(ps)), "hard_pass_rate": float(np.mean(Gs)),
        "full_compliance_rate": float(np.mean(As)), "mean_objective": float(np.mean(Os)),
        "objective_raw": O, "cost": cost, "cost_bound": c_bound,
        "feasible_all_scenarios": bool(feasible_all),
        "register_recall_hard": reg["recall_hard"], "register_recall_all": reg["recall_all"],
        "register_precision": reg["precision"], "register_active": reg["active"],
        "register_filed": reg["filed"], "register_wrong": reg["wrong"],
        "families_satisfied": {f: int(v) for f, v in satisfied.items()},
        "families_registered": {f: int(v) for f, v in registered.items()},
        "per_item": per_item, "per_item_families": per_fam,
        "n_zones": ana["n_zones"], "gages": ana["gages"], "patterns": ana["patterns"],
        "deck_area_sf": ana["deck_area_sf"], "lateral": ana["lateral"],
    })
    metrics["_fail_detail"] = {f: v[:6] for f, v in ana["fails"].items() if v}
    metrics["_scen_detail"] = {sid: {f: v[:4] for f, v in d.items() if v} for sid, d in ana["scen"].items()
                               if any(d.values())}
    return metrics
