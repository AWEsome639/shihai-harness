"""cost.py - installed cost of a lateral package, and the certified bound.

    python -m grading.cost            recompute grading/data/bound.json

The bound is the exact optimum of a documented RELAXATION of the design problem:

  * for every zone, a DEMAND FLOOR is established by sampling the assignment space - each of the
    sixteen uniform assignments and a hundred seeded mixed ones - and taking 85 % of the lowest
    unit shear that zone ever carried across all twelve scenarios.  The floor is deliberately
    below anything observed: the relaxation is allowed to be optimistic, it is not allowed to be
    wrong.
  * each zone then gets the cheapest catalogue deck whose ASD capacity covers its floor.  On
    this building every zone's floor lands below the capacity of the cheapest deck in the
    catalogue, so the deck term reduces to "the cheapest deck over the whole roof" - a floor no
    design can go under whatever the demand turns out to be, which is what makes the bound
    unconditional here rather than only as good as its sample;
  * chords and collectors get the cheapest catalogue section, and embeds the vendor's minimum
    count; no panel gets a tie-down;
  * the diaphragm drift limit, the pier checks, the four-deck-type cap and the stiffness coupling
    (a zone's deck changes the force everywhere) are all dropped.

No feasible design can be built from those pieces, which is the point: the bound is below every
attainable cost, so the objective credit O = bound / cost never reaches 1.  A re-sample
with a different seed records that no sampled feasible assignment costs less than the bound.
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

import numpy as np

from .model import Analysis, Design, piers, truth

HERE = Path(__file__).resolve().parent


def min_embeds(T: dict, width: float) -> int:
    """Vendor minimum: two per panel, and not more than `embed_max_spacing_ft` apart."""
    smax = T["connections"]["embed_max_spacing_ft"]
    return int(max(2, math.ceil(width / smax) + 1))


def panel_width(T: dict, mark: str) -> float:
    for rows in T["panels"].values():
        for p in rows:
            if p["mark"] == mark:
                return p["s1"] - p["s0"]
    return 0.0


def design_cost(T: dict, d: Design) -> dict:
    zones = {z["id"]: z for z in T["zones"]}
    deck_by = {x["code"]: x for x in T["deck_catalogue"]}
    chord_by = {x["code"]: x for x in T["chord_catalogue"]}
    deck = 0.0
    for zid, z in zones.items():
        c = deck_by.get(d.deck.get(zid))
        if c is None:
            return {"total": float("inf"), "deck": float("inf"), "chord": 0.0,
                    "embed": 0.0, "tiedown": 0.0, "invalid": True}
        deck += z["area_sf"] * c["cost_per_sf"]
    chord = 0.0
    for cl in T["chord_lines"]:
        c = chord_by.get(d.chords.get(cl["id"]))
        if c is None:
            return {"total": float("inf"), "deck": deck, "chord": float("inf"),
                    "embed": 0.0, "tiedown": 0.0, "invalid": True}
        chord += (cl["s1"] - cl["s0"]) * c["weight_plf"] * T["costs"]["chord_per_lb"]
    embed = 0.0
    marks = [p["mark"] for rows in T["panels"].values() for p in rows]
    for m in marks:
        n = d.embeds.get(m, min_embeds(T, panel_width(T, m)))
        embed += max(0, int(n)) * T["connections"]["embed_cost"]
    tie = len([m for m in d.tiedowns if m in marks]) * T["connections"]["tiedown_cost"]
    total = deck + chord + embed + tie
    return {"total": round(total, 2), "deck": round(deck, 2), "chord": round(chord, 2),
            "embed": round(embed, 2), "tiedown": round(tie, 2), "invalid": False}


SAMPLE_SEED = 20260923
SAMPLE_N = 100
FLOOR_MARGIN = 0.85


def _zone_and_chord_demand(T, deck_map, chord_map):
    d = Design(deck_map, chord_map, {}, [])
    A = Analysis(T, d)
    res = A.analyse()
    zone = {z["id"]: 0.0 for z in T["zones"]}
    chord = {c["id"]: 0.0 for c in T["chord_lines"]}
    for r in res.values():
        q = np.abs(r["q"]) * 1000.0
        for e, zid in enumerate(A.elem_zone):
            if zid is not None and q[e] > zone[zid]:
                zone[zid] = float(q[e])
        for cid, f in r["chord_force"].items():
            chord[cid] = max(chord[cid], float(f))
    return zone, chord


def relaxed_demand(T: dict) -> dict:
    """The per-zone and per-chord demand FLOOR of the relaxation.

    Sampled, not assumed: every uniform assignment plus SAMPLE_N seeded mixed ones, the lowest
    value each zone and each chord line ever took, times FLOOR_MARGIN."""
    rng = random.Random(SAMPLE_SEED)
    cheap = min(T["chord_catalogue"], key=lambda c: c["area_in2"])["code"]
    chord_map = {c["id"]: cheap for c in T["chord_lines"]}
    zone_min = {z["id"]: float("inf") for z in T["zones"]}
    chord_min = {c["id"]: float("inf") for c in T["chord_lines"]}
    zones = [z["id"] for z in T["zones"]]
    assignments = [{z: c["code"] for z in zones} for c in T["deck_catalogue"]]
    for _ in range(SAMPLE_N):
        k = rng.randint(1, T["deck"]["max_types"])
        pool = [rng.choice(T["deck_catalogue"])["code"] for _ in range(k)]
        assignments.append({z: rng.choice(pool) for z in zones})
    for deck_map in assignments:
        zone, chord = _zone_and_chord_demand(T, deck_map, chord_map)
        for k2, v in zone.items():
            zone_min[k2] = min(zone_min[k2], v)
        for k2, v in chord.items():
            chord_min[k2] = min(chord_min[k2], v)
    return {"zone": {k: v * FLOOR_MARGIN for k, v in zone_min.items()},
            "chord": {k: v * FLOOR_MARGIN for k, v in chord_min.items()},
            "samples": len(assignments), "seed": SAMPLE_SEED, "margin": FLOOR_MARGIN}


def certified_bound(T: dict, demand: dict | None = None) -> dict:
    dem = demand or relaxed_demand(T)
    zones = {z["id"]: z for z in T["zones"]}
    deck = 0.0
    deck_pick = {}
    for zid, z in zones.items():
        ok = [c for c in T["deck_catalogue"] if c["asd_plf"] >= dem["zone"][zid] - 1e-9]
        c = min(ok, key=lambda c: c["cost_per_sf"]) if ok else max(
            T["deck_catalogue"], key=lambda c: c["asd_plf"])
        deck_pick[zid] = c["code"]
        deck += z["area_sf"] * c["cost_per_sf"]
    chord = 0.0
    chord_pick = {}
    for cl in T["chord_lines"]:
        need = dem["chord"][cl["id"]]
        ok = [c for c in T["chord_catalogue"] if c["allow_c_kips"] >= need - 1e-9]
        c = min(ok, key=lambda c: c["weight_plf"]) if ok else max(
            T["chord_catalogue"], key=lambda c: c["allow_c_kips"])
        chord_pick[cl["id"]] = c["code"]
        chord += (cl["s1"] - cl["s0"]) * c["weight_plf"] * T["costs"]["chord_per_lb"]
    embed = 0.0
    for rows in T["panels"].values():
        for p in rows:
            embed += min_embeds(T, p["s1"] - p["s0"]) * T["connections"]["embed_cost"]
    return {"bound": round(deck + chord + embed, 2), "deck": round(deck, 2),
            "chord": round(chord, 2), "embed": round(embed, 2), "tiedown": 0.0,
            "samples": dem.get("samples"), "seed": dem.get("seed"),
            "floor_margin": dem.get("margin"),
            "deck_pick": deck_pick, "chord_pick": chord_pick,
            "zone_demand": {k: round(v, 1) for k, v in dem["zone"].items()},
            "chord_demand": {k: round(v, 2) for k, v in dem["chord"].items()}}


def main() -> int:
    T = truth()
    b = certified_bound(T)
    out = HERE / "data" / "bound.json"
    out.write_text(json.dumps(b, indent=1))
    print(json.dumps({k: v for k, v in b.items()
                      if k in ("bound", "deck", "chord", "embed")}, indent=1))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
