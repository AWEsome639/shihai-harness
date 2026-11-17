"""Deck catalogue lookups, cell fields and the installed cost of a deck design.

Everything here reads the tables in `data/truth.json`, which are the same tables the NORDECK
catalogue in `world_files/project/70_procurement/` prints.
"""
from __future__ import annotations

import numpy as np

from . import loads as LD


def key_diaphragm(gage, pattern, sidelap, ns):
    return f"{int(gage)}|{pattern}|{sidelap}|{int(ns)}"


def lookup(truth, gage, pattern, sidelap, ns):
    d = truth["deck"]["diaphragm"].get(key_diaphragm(gage, pattern, sidelap, ns))
    return d


def shear_capacity_plf(truth, gage, pattern, sidelap, ns):
    d = lookup(truth, gage, pattern, sidelap, ns)
    return 0.0 if d is None else truth["deck"]["phi_d"] * d["Sn_plf"]


def stiffness_kip_in(truth, gage, pattern, sidelap, ns):
    d = lookup(truth, gage, pattern, sidelap, ns)
    return 0.0 if d is None else d["Gp_kip_in"]


def uplift_capacity_psf(truth, gage, pattern):
    return truth["deck"]["uplift_capacity_psf"].get(f"{int(gage)}|{pattern}", 0.0)


def gravity_capacity_psf(truth, gage):
    return truth["deck"]["gages"][str(int(gage))]["gravity_capacity_psf"]


def deck_weight_psf(truth, gage):
    return truth["deck"]["gages"][str(int(gage))]["wt"]


def deflection_in(truth, gage, w_psf):
    return truth["deck"]["gages"][str(int(gage))]["deflection_per_psf_in"] * w_psf


def valid_combination(truth, gage, pattern, sidelap, ns) -> str | None:
    """Return None if the combination is a catalogue combination, else why it is not."""
    dk = truth["deck"]
    if str(int(gage)) not in dk["gages"]:
        return f"gage {gage} is not in the catalogue"
    if pattern not in dk["support_patterns"]:
        return f"support pattern {pattern!r} is not in the catalogue"
    if sidelap not in dk["sidelap_types"]:
        return f"sidelap type {sidelap!r} is not in the catalogue"
    if int(ns) not in dk["sidelap_counts"]:
        return f"{ns} sidelap connectors per span is not a tabulated value"
    if int(gage) < dk["sidelap_types"][sidelap]["min_gage"]:
        return f"{sidelap} may not be used on {gage} gage"
    return None


def cell_fields(truth, zones, assign):
    """Build the per-cell fields implied by a zone assignment.

    `assign` is an integer array of cell -> zone index (-1 where no deck is required).
    Returns dict of arrays over the deck-cell grid.
    """
    nx, ny = LD.cell_shape(truth)
    gt = np.zeros((nx, ny))
    cap = np.zeros((nx, ny))
    upl = np.zeros((nx, ny))
    grav = np.zeros((nx, ny))
    wt = np.zeros((nx, ny))
    dfl = np.zeros((nx, ny))
    for zi, z in enumerate(zones):
        m = assign == zi
        if not m.any():
            continue
        gt[m] = stiffness_kip_in(truth, z["gage"], z["pattern"], z["sidelap"], z["ns"])
        cap[m] = shear_capacity_plf(truth, z["gage"], z["pattern"], z["sidelap"], z["ns"])
        upl[m] = uplift_capacity_psf(truth, z["gage"], z["pattern"])
        grav[m] = gravity_capacity_psf(truth, z["gage"])
        wt[m] = deck_weight_psf(truth, z["gage"])
        dfl[m] = truth["deck"]["gages"][str(int(z["gage"]))]["deflection_per_psf_in"]
    return dict(gt=gt, shear_cap=cap, uplift_cap=upl, gravity_cap=grav, weight=wt, defl_per_psf=dfl)


def installed_cost(truth, zones, assign):
    """Installed cost of the design, USD (cost plan CP-RD-2026 rates)."""
    c = truth["cost"]
    g = truth["geometry"]
    area_cell = g["zone_grid_x_ft"] * g["zone_grid_y_ft"]
    total = 0.0
    gages, patterns = set(), set()
    n_zones = 0
    n_rects = 0
    for zi, z in enumerate(zones):
        n = int((assign == zi).sum())
        if n == 0:
            continue
        n_zones += 1
        n_rects += len(z.get("rects", [])) or 1
        gages.add(int(z["gage"]))
        patterns.add((z["pattern"], z["sidelap"], int(z["ns"])))
        area = n * area_cell
        total += c["deck_psf"][str(int(z["gage"]))] * area
        # support fasteners: n per 36 in panel width at every joist crossing
        n_supports = area / (g["panel_width_ft"] * g["joist_spacing_ft"])
        total += c["support_fastener"] * n_supports * truth["deck"]["support_patterns"][z["pattern"]]
        # sidelap connectors: ns per 6 ft span along every sidelap line
        n_laps = area / (g["panel_width_ft"] * g["joist_spacing_ft"])
        total += c["sidelap"][z["sidelap"]] * n_laps * int(z["ns"])
    total += c["zone_setup"] * n_zones
    total += c["rect_setup"] * n_rects
    total += c["gage_setup"] * len(gages)
    total += c["pattern_setup"] * len(patterns)
    return total
