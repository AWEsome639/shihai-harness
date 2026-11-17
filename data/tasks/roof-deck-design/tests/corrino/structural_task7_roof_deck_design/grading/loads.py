"""Gravity, snow-drift and wind-uplift load fields over the roof.

The roof is discretised into the framing bays the deck actually spans: 6 ft in x (joist to joist) by
30 ft in y (girder to girder).  Every field is evaluated on that grid and reduced to the maximum over
each cell, because the deck check is a point check and the deck cannot be varied inside a cell.

All of it is a pure function of `truth`, so the grader, the bound and the gold answer see the same
numbers, and a solver that reads the site snow study, the roof model and the design criteria can
reproduce them.
"""
from __future__ import annotations

import math

import numpy as np


# ---------------------------------------------------------------------------
# the grid
# ---------------------------------------------------------------------------

def grid(truth):
    """Boundaries of the deck-design cells: joist lines in x, sidelap lines in y."""
    g = truth["geometry"]
    dx, dy = g["zone_grid_x_ft"], g["zone_grid_y_ft"]
    xs = np.arange(0.0, g["lx_ft"] + 0.5 * dx, dx)
    ys = np.arange(0.0, g["ly_ft"] + 0.5 * dy, dy)
    return xs, ys


def fe_grid(truth):
    """Boundaries of the diaphragm finite elements: joist lines in x, girder lines in y."""
    g = truth["geometry"]
    return np.array(g["joist_x"], dtype=float), np.array(g["girder_y"], dtype=float)


def cell_centres(truth):
    xs, ys = grid(truth)
    cx = 0.5 * (xs[:-1] + xs[1:])
    cy = 0.5 * (ys[:-1] + ys[1:])
    return cx, cy


def cell_shape(truth):
    xs, ys = grid(truth)
    return len(xs) - 1, len(ys) - 1


def cell_area_sf(truth):
    g = truth["geometry"]
    return g["zone_grid_x_ft"] * g["zone_grid_y_ft"]


FINE_FT = 1.0          # the load fields are evaluated on a 1 ft grid and reduced to the framing
                       # cells by MAXIMUM: the deck must carry the peak anywhere inside its cell.


def fine_centres(truth):
    g = truth["geometry"]
    nfx = int(round(g["zone_grid_x_ft"] / FINE_FT))
    nfy = int(round(g["zone_grid_y_ft"] / FINE_FT))
    nx, ny = cell_shape(truth)
    fx = (np.arange(nx * nfx) + 0.5) * FINE_FT
    fy = (np.arange(ny * nfy) + 0.5) * FINE_FT
    return fx, fy, nfx, nfy


def fine_mesh(truth):
    fx, fy, _, _ = fine_centres(truth)
    return np.meshgrid(fx, fy, indexing="ij")


def reduce_max(truth, fine):
    """Reduce a fine-grid field to the framing cells by taking the maximum over each cell."""
    nx, ny = cell_shape(truth)
    _, _, nfx, nfy = fine_centres(truth)
    return fine.reshape(nx, nfx, ny, nfy).max(axis=(1, 3))


def _rects_overlap(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
    return not (ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0)


def opening_mask(truth):
    """True where the cell is (partly) inside a framed roof opening: no deck there."""
    nx, ny = cell_shape(truth)
    xs, ys = grid(truth)
    m = np.zeros((nx, ny), dtype=bool)
    for op in truth["geometry"]["openings"]:
        for i in range(nx):
            for j in range(ny):
                if _rects_overlap(xs[i], ys[j], xs[i + 1], ys[j + 1],
                                  op["x0"], op["y0"], op["x1"], op["y1"]):
                    m[i, j] = True
    return m


def penthouse_mask(truth):
    """True where the cell is under the penthouse footprint: that roof is not this deck."""
    nx, ny = cell_shape(truth)
    xs, ys = grid(truth)
    ph = truth["geometry"]["penthouse"]
    m = np.zeros((nx, ny), dtype=bool)
    for i in range(nx):
        for j in range(ny):
            if _rects_overlap(xs[i], ys[j], xs[i + 1], ys[j + 1], ph["x0"], ph["y0"], ph["x1"], ph["y1"]):
                m[i, j] = True
    return m


def deck_mask(truth):
    """Cells that must carry deck.  The equipment enclosure stands on the roof, so the deck runs
    beneath it; only the framed roof openings have no deck."""
    return ~opening_mask(truth)


# ---------------------------------------------------------------------------
# snow
# ---------------------------------------------------------------------------

def leeward_drift_height_ft(truth, lu_ft):
    L = truth["loads"]
    lu = max(float(lu_ft), L.get("drift_min_fetch_ft", 20.0))
    return max(L["drift_c"] * lu ** (1.0 / 3.0) * (L["pg_psf"] + 10.0) ** 0.25 - 1.5, 0.0)


def _drift_profile(truth, hd, hc, dist):
    """Surcharge psf at distance `dist` (ft, array) from the obstruction, triangular over width w."""
    L = truth["loads"]
    gamma = L["gamma_pcf"]
    if hc <= 0.0 or hd <= 0.0:
        return np.zeros_like(dist)
    if hd <= hc:
        peak = hd * gamma
        w = L["drift_width_factor"] * hd
    else:
        peak = hc * gamma
        w = min(L["drift_width_factor"] * hd * hd / hc, 8.0 * hc)
    if w <= 0.0:
        return np.zeros_like(dist)
    return np.clip(peak * (1.0 - dist / w), 0.0, None)


def _obstruction_drift(truth, field, X, Y, x0, y0, x1, y1, height_ft):
    """Add the drift surcharges formed against a rectangular obstruction standing on the roof.

    Design criteria EOR-DC-RD-01 clause 5.4, applied identically here:
      * on the face the wind arrives at, a windward drift of height 0.75 hd(lu) with lu the roof
        fetch from the upwind roof edge to that face;
      * on the sheltered face, a leeward drift of height hd(lu) with lu the along-wind length of the
        obstruction itself (its own roof is the snow source);
      * wind from each of the four directions; the surcharge at a point is the largest of them.
    """
    L = truth["loads"]
    g = truth["geometry"]
    hb = L["hb_ft"]
    pf = L["pf_psf"]
    hc = height_ft - hb
    # criteria clause 5.5: an obstruction is a drift source when its clear height above the balanced
    # snow exceeds the threshold - the test is on hc, not on the bare height
    if hc <= L["obstruction_min_height_ft"]:
        return
    if hc <= 0.0:
        return
    lx, ly = g["lx_ft"], g["ly_ft"]
    wf = L["drift_windward_factor"]
    len_x, len_y = (x1 - x0), (y1 - y0)
    # (distance array measured away from the face, windward fetch, leeward fetch)
    faces = [
        (x0 - X, x0, len_x, (Y >= min(y0, y1) - 1e-9) & (Y <= max(y0, y1) + 1e-9)),          # west face
        (X - x1, lx - x1, len_x, (Y >= min(y0, y1) - 1e-9) & (Y <= max(y0, y1) + 1e-9)),      # east face
        (y0 - Y, y0, len_y, (X >= min(x0, x1) - 1e-9) & (X <= max(x0, x1) + 1e-9)),          # south face
        (Y - y1, ly - y1, len_y, (X >= min(x0, x1) - 1e-9) & (X <= max(x0, x1) + 1e-9)),      # north face
    ]
    for dist, lu_w, lu_l, band in faces:
        hd = max(wf * leeward_drift_height_ft(truth, lu_w), leeward_drift_height_ft(truth, lu_l))
        d = np.where(band & (dist > 0), dist, 1e9)
        np.maximum(field, pf + _drift_profile(truth, hd, hc, d), out=field)


def snow_field(truth, case):
    """Snow load field (psf) on each cell.

    case 'balanced'  - pf everywhere
    case 'penthouse' - pf + drifts against the rooftop penthouse walls
    case 'parapet'   - pf + windward drifts against the four parapets
    case 'equipment' - pf + drifts against the rooftop units and the equipment screen wall
    """
    L = truth["loads"]
    g = truth["geometry"]
    X, Y = fine_mesh(truth)
    pf = L["pf_psf"]
    hb = L["hb_ft"]
    field = np.full(X.shape, pf, dtype=float)
    if case == "balanced":
        return reduce_max(truth, field)

    if case == "penthouse":
        ph = g["penthouse"]
        _obstruction_drift(truth, field, X, Y, ph["x0"], ph["y0"], ph["x1"], ph["y1"], ph["height_ft"])
        return reduce_max(truth, field)

    if case == "parapet":
        hc = g["parapet_ht_ft"] - hb
        if hc > 0.0:
            wf = L["drift_windward_factor"]
            for dist, lu in [(Y - 0.0, g["ly_ft"]), (g["ly_ft"] - Y, g["ly_ft"]),
                             (X - 0.0, g["lx_ft"]), (g["lx_ft"] - X, g["lx_ft"])]:
                hd = wf * leeward_drift_height_ft(truth, lu)
                d = np.where(dist > 0, dist, 1e9)
                np.maximum(field, pf + _drift_profile(truth, hd, hc, d), out=field)
        return reduce_max(truth, field)

    if case == "equipment":
        for r in g["rtus"]:
            _obstruction_drift(truth, field, X, Y, r["x0"], r["y0"], r["x1"], r["y1"], r["height_ft"])
        for w in g["screen_walls"]:
            x0, x1 = min(w["x0"], w["x1"]), max(w["x0"], w["x1"])
            y0, y1 = min(w["y0"], w["y1"]), max(w["y0"], w["y1"])
            if abs(y1 - y0) < 1e-9:
                y0, y1 = y0 - 0.25, y1 + 0.25     # the wall has a nominal thickness
            if abs(x1 - x0) < 1e-9:
                x0, x1 = x0 - 0.25, x1 + 0.25
            _obstruction_drift(truth, field, X, Y, x0, y0, x1, y1, w["height_ft"])
        return reduce_max(truth, field)

    raise ValueError(case)


# The drift report the deliverable asks for: one named face per obstruction, so that the answer is a
# single unambiguous band rather than whichever of several equal faces the reporter happened to pick.
DRIFT_FACES = [("MEP-PH-1", "W"), ("SCR-N", "S"), ("PARAPET", "N"), ("RTU-1", "E")]


def _face_rect(truth, tag):
    """(x0, y0, x1, y1, height_ft) of the obstruction the tag names."""
    g = truth["geometry"]
    if tag == g["penthouse"]["tag"]:
        ph = g["penthouse"]
        return ph["x0"], ph["y0"], ph["x1"], ph["y1"], ph["height_ft"]
    for w in g["screen_walls"]:
        if w["tag"] == tag:
            x0, x1 = min(w["x0"], w["x1"]), max(w["x0"], w["x1"])
            y0, y1 = min(w["y0"], w["y1"]), max(w["y0"], w["y1"])
            if abs(y1 - y0) < 1e-9:
                y0, y1 = y0 - 0.25, y1 + 0.25
            if abs(x1 - x0) < 1e-9:
                x0, x1 = x0 - 0.25, x1 + 0.25
            return x0, y0, x1, y1, w["height_ft"]
    for r in g["rtus"]:
        if r["tag"] == tag:
            return r["x0"], r["y0"], r["x1"], r["y1"], r["height_ft"]
    raise ValueError(tag)


def face_drift(truth, tag, face):
    """The drift on one named face: (peak surcharge psf, band from ft, band to ft).

    The band bounds are plan coordinates on the axis normal to the face - x for an east or west
    face, y for a north or south face - running from the face outwards.
    """
    L = truth["loads"]
    g = truth["geometry"]
    hb = L["hb_ft"]
    wf = L["drift_windward_factor"]
    if tag == "PARAPET":
        h = g["parapet_ht_ft"]
        # the parapet drift forms on the inside face; the fetch is the roof dimension upwind
        coord = {"N": g["ly_ft"], "S": 0.0, "E": g["lx_ft"], "W": 0.0}[face]
        lu_w = g["ly_ft"] if face in "NS" else g["lx_ft"]
        lu_l = 0.0
        outward = -1 if face in ("N", "E") else +1
    else:
        x0, y0, x1, y1, h = _face_rect(truth, tag)
        len_x, len_y = x1 - x0, y1 - y0
        if face == "W":
            coord, lu_w, lu_l, outward = x0, x0, len_x, -1
        elif face == "E":
            coord, lu_w, lu_l, outward = x1, g["lx_ft"] - x1, len_x, +1
        elif face == "S":
            coord, lu_w, lu_l, outward = y0, y0, len_y, -1
        else:
            coord, lu_w, lu_l, outward = y1, g["ly_ft"] - y1, len_y, +1
    hc = h - hb
    # Criteria clause 5.5's height threshold is a test on rooftop EQUIPMENT: a low unit does not
    # create a drift. A parapet is not equipment - clause 5.4.6 gives the windward parapet drift with
    # no height threshold - so the threshold must not be applied to it.
    floor = 0.0 if tag == "PARAPET" else L["obstruction_min_height_ft"]
    if hc <= floor:
        return 0.0, coord, coord
    hd = max(wf * leeward_drift_height_ft(truth, lu_w), leeward_drift_height_ft(truth, lu_l))
    gamma = L["gamma_pcf"]
    if hd <= hc:
        peak, w = hd * gamma, L["drift_width_factor"] * hd
    else:
        peak, w = hc * gamma, min(L["drift_width_factor"] * hd * hd / hc, 8.0 * hc)
    far = coord + outward * w
    return float(peak), float(min(coord, far)), float(max(coord, far))


def gravity_demand_psf(truth, scenario, deck_weight_psf):
    """Total service gravity load on each cell for a gravity scenario (psf), including dead load.

    `deck_weight_psf` may be a scalar or a per-cell array: criteria section 3 makes the deck self
    weight part of the dead load of the cell it is in, and it varies with the gage."""
    L = truth["loads"]
    dl = L["dl_collateral_psf"] + np.asarray(deck_weight_psf)
    if scenario == "S01":
        live = np.full(cell_shape(truth), L["lr_deck_psf"], dtype=float)
    elif scenario == "S02":
        live = snow_field(truth, "penthouse")
    elif scenario == "S03":
        live = snow_field(truth, "parapet")
    elif scenario == "S04":
        live = snow_field(truth, "equipment")
    else:
        raise ValueError(scenario)
    live = np.maximum(live, L["lr_deck_psf"])
    return dl + live


def live_component_psf(truth, scenario):
    """The non-dead part of the gravity demand, used for the live-load deflection check."""
    L = truth["loads"]
    if scenario == "S01":
        return np.full(cell_shape(truth), L["lr_deck_psf"], dtype=float)
    key = {"S02": "penthouse", "S03": "parapet", "S04": "equipment"}[scenario]
    return np.maximum(snow_field(truth, key), L["lr_deck_psf"])


# ---------------------------------------------------------------------------
# wind uplift (components and cladding)
# ---------------------------------------------------------------------------

def cc_zone_field(truth):
    """Zone label ('3', '2', '1', '1p') for every cell, from the flat-roof C&C zone geometry."""
    g = truth["geometry"]
    L = truth["loads"]
    a = L["a_zone_ft"]
    X, Y = fine_mesh(truth)
    dx = np.minimum(X, g["lx_ft"] - X)
    dy = np.minimum(Y, g["ly_ft"] - Y)
    d = np.minimum(dx, dy)
    zone = np.full(X.shape, "1p", dtype=object)
    zone[d <= L["zone1p_inset_ft"]] = "1"
    zone[d <= a] = "2"
    corner = (dx <= a) & (dy <= a)
    zone[corner] = "3"
    return zone


def uplift_demand_psf(truth, deck_weight_psf):
    """Net factored uplift on the deck attachment, psf (positive upward)."""
    L = truth["loads"]
    zone = cc_zone_field(truth)
    gcp = np.vectorize(lambda z: L["cc_zones"][z])(zone).astype(float)
    p = L["qh_psf"] * (gcp - L["gcpi"])                    # negative = uplift
    # 0.9 D resists, LRFD; the suspended MEP allowance does not hold the deck down (criteria 6.2.3)
    resist = 0.9 * (np.asarray(deck_weight_psf) + L["dl_roofing_psf"])
    dem = reduce_max(truth, np.maximum(-p, 0.0))
    return np.maximum(dem - resist, 0.0)


# ---------------------------------------------------------------------------
# seismic weight and diaphragm design forces
# ---------------------------------------------------------------------------

def seismic_weight_psf(truth):
    L = truth["loads"]
    w = L["w_roof_psf"]
    if L["pf_psf"] > L["snow_in_seismic_threshold_psf"]:
        w += L["snow_seismic_fraction"] * L["pf_psf"]
    return w


def seismic_diaphragm_force_kip(truth):
    """Fpx delivered to the roof diaphragm, kips (single-storey: Fpx = V, within the 12.10.1 bounds)."""
    L = truth["loads"]
    g = truth["geometry"]
    area = g["lx_ft"] * g["ly_ft"] - (g["penthouse"]["x1"] - g["penthouse"]["x0"]) * 0.0
    w_kip = seismic_weight_psf(truth) * area / 1000.0
    cs = L["sds"] * L["ie"] / L["r"]
    v = cs * w_kip
    lo = 0.2 * L["sds"] * L["ie"] * w_kip
    hi = 0.4 * L["sds"] * L["ie"] * w_kip
    return float(min(max(v, lo), hi)), float(w_kip)


def lateral_case(truth, scenario):
    """Return (direction, total force kip, eccentricity ft, load pattern, omega applied)."""
    L = truth["loads"]
    g = truth["geometry"]
    fp, _ = seismic_diaphragm_force_kip(truth)
    # the eccentricity is measured on the plan dimension PERPENDICULAR to the direction of the
    # force, as criteria clause 7.3 and lateral report clause 4.3 both state
    if scenario == "S05":
        return "X", L["wind_roof_force_x_kip"], L["wind_ecc_factor"] * g["ly_ft"], "edge", 1.0
    if scenario == "S06":
        return "Y", L["wind_roof_force_y_kip"], L["wind_ecc_factor"] * g["lx_ft"], "edge", 1.0
    if scenario == "S07":
        return "X", fp, L["accidental_ecc"] * g["ly_ft"], "mass", 1.0
    if scenario == "S08":
        return "Y", fp, L["accidental_ecc"] * g["lx_ft"], "mass", 1.0
    if scenario == "S10":
        return "X", fp, L["accidental_ecc"] * g["ly_ft"], "mass", L["omega0"]
    raise ValueError(scenario)
