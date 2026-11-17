"""A certified lower bound on the installed cost of ANY compliant roof-deck design.

The bound is a relaxation, and every constraint it drops can only make a design cheaper, so no
compliant design can cost less than the bound.

Kept (all of them design-independent, so they hold for every compliant design):
  * catalogue validity - gage, support pattern, sidelap type and sidelap count must be a tabulated
    combination, and the sidelap spacing rule must hold;
  * gravity - the worst gravity demand of any scenario at the cell must not exceed the allowable
    uniform load of the gage, and the live-load deflection and the deck end reaction must be within
    their limits.  The gravity field is fixed by the snow, the drifts and the roof live load, none of
    which depend on the deck design;
  * uplift - the net components-and-cladding uplift at the cell must not exceed the attachment's
    factored uplift capacity.  The uplift field is fixed by the wind pressures;
  * boundary attachment - a cell on the diaphragm boundary or on the edge of a framed opening must
    carry at least the minimum perimeter pattern and sidelap count;
  * the deck must exist on every cell that is not inside a framed opening;
  * the minimum possible set-up costs: one gage, one attachment pattern, one zone, one rectangle.

Relaxed (dropped):
  * the diaphragm shear, deflection, chord and collector requirements - these depend on the design
    through G', so no per-cell demand can be stated independently of the design;
  * the cardinality limits (at most two gages, three patterns, twelve zones) - each cell is free to
    take its own cheapest combination;
  * the requirement that a zone be a rectangle on the framing grid;
  * every reporting and documentation family.

The resulting programme separates by cell, so the bound is the sum of per-cell minima: an exact
solution of the relaxation, not a heuristic.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import deck as DK
from . import loads as LD

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


def cell_cost(truth, gage, pattern, sidelap, ns):
    """Installed cost of one deck cell with this combination, excluding set-up costs."""
    c = truth["cost"]
    g = truth["geometry"]
    area = g["zone_grid_x_ft"] * g["zone_grid_y_ft"]
    lines = area / (g["panel_width_ft"] * g["joist_spacing_ft"])
    return (c["deck_psf"][str(int(gage))] * area
            + c["support_fastener"] * lines * truth["deck"]["support_patterns"][pattern]
            + c["sidelap"][sidelap] * lines * int(ns))


def combinations(truth):
    dk = truth["deck"]
    out = []
    for gs in dk["gages"]:
        gage = int(gs)
        if gage not in dk["gages_stocked"]:
            continue
        for pattern in dk["support_patterns"]:
            for sl in dk["sidelap_types"]:
                for ns in dk["sidelap_counts"]:
                    if DK.valid_combination(truth, gage, pattern, sl, ns):
                        continue
                    span_in = truth["geometry"]["joist_spacing_ft"] * 12.0
                    if span_in / ns > dk["sidelap_max_spacing_in"] + 1e-6:
                        continue
                    out.append(dict(gage=gage, pattern=pattern, sidelap=sl, ns=ns,
                                    cost=cell_cost(truth, gage, pattern, sl, ns),
                                    grav=DK.gravity_capacity_psf(truth, gage),
                                    upl=DK.uplift_capacity_psf(truth, gage, pattern),
                                    defl=dk["gages"][str(gage)]["deflection_per_psf_in"],
                                    reac=dk["gages"][str(gage)]["Rn_end"],
                                    npat=dk["support_patterns"][pattern]))
    return out


def compute(truth):
    g = truth["geometry"]
    dk = truth["deck"]
    need = LD.deck_mask(truth)
    nx, ny = LD.cell_shape(truth)

    # design-independent demand envelopes
    wbar_min = min(dk["gages"][str(int(x))]["wt"] for x in dk["gages_stocked"])
    grav = np.zeros((nx, ny))
    live = np.zeros((nx, ny))
    for sid in ("S01", "S02", "S03", "S04"):
        grav = np.maximum(grav, LD.gravity_demand_psf(truth, sid, wbar_min))
        live = np.maximum(live, LD.live_component_psf(truth, sid))
    # the heaviest gage adds weight, so the gravity envelope uses each candidate's own weight below
    upl = LD.uplift_demand_psf(truth, max(dk["gages"][str(int(x))]["wt"] for x in dk["gages_stocked"]))

    boundary = np.zeros((nx, ny), dtype=bool)
    boundary[0, :] = boundary[-1, :] = True
    boundary[:, 0] = boundary[:, -1] = True
    op = LD.opening_mask(truth)
    edge = np.zeros((nx, ny), dtype=bool)
    edge[1:, :] |= op[:-1, :]
    edge[:-1, :] |= op[1:, :]
    edge[:, 1:] |= op[:, :-1]
    edge[:, :-1] |= op[:, 1:]
    must_perimeter = (boundary | (edge & ~op)) & need
    pmin = dk["support_patterns"][dk["perimeter_min_pattern"]]
    defl_lim = g["joist_spacing_ft"] * 12.0 / dk["defl_limit_denom"]

    combos = combinations(truth)
    total = 0.0
    per_cell = np.zeros((nx, ny))
    infeasible = []
    for i in range(nx):
        for j in range(ny):
            if not need[i, j]:
                continue
            best = None
            for c in combos:
                if must_perimeter[i, j] and (c["npat"] < pmin or c["ns"] < dk["min_sidelap_at_boundary"]):
                    continue
                dl = grav[i, j] - wbar_min + dk["gages"][str(c["gage"])]["wt"]
                if dl > c["grav"] + 1e-9:
                    continue
                if c["defl"] * live[i, j] > defl_lim + 1e-9:
                    continue
                if 1.1 * dl * g["joist_spacing_ft"] > c["reac"] + 1e-9:
                    continue
                if upl[i, j] > c["upl"] + 1e-9:
                    continue
                if best is None or c["cost"] < best["cost"]:
                    best = c
            if best is None:
                infeasible.append((int(i), int(j)))
                continue
            per_cell[i, j] = best["cost"]
            total += best["cost"]
    c = truth["cost"]
    setup = c["gage_setup"] + c["pattern_setup"] + c["zone_setup"] + c["rect_setup"]
    return dict(cost_bound=total + setup, cell_cost_total=total, setup_min=setup,
                n_cells=int(need.sum()), infeasible_cells=infeasible,
                n_combinations=len(combos))


def main():
    truth = json.loads((DATA / "truth.json").read_text())
    b = compute(truth)
    (DATA / "bound.json").write_text(json.dumps(b, indent=1))
    print(json.dumps({k: v for k, v in b.items() if k != "infeasible_cells"}, indent=1))
    if b["infeasible_cells"]:
        print(f"WARNING: {len(b['infeasible_cells'])} cells have no feasible combination")


if __name__ == "__main__":
    main()
