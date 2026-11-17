"""The constraint families: what the deck design has to satisfy, re-derived from the truth model.

Model-level families are geometric or documentary and are evaluated once on the submitted design.
Scenario-level families are evaluated in each design scenario and credited per scenario.
"""
from __future__ import annotations

import math
import re

import numpy as np

from . import deck as DK
from . import loads as LD
from .diaphragm import Diaphragm, REG_SHARE_LIMIT
from .model import zone_assignment

HARD = ["H01", "H02", "H03", "H04", "H05", "H06", "H07", "H08", "H09", "H10",
        "H11", "H12", "H13", "H14", "H15", "H16", "H17", "H18", "H19", "H20", "H21"]
SOFT = ["S01", "S02", "S03", "S04", "S05", "S06", "S07"]
FAMILIES = HARD + SOFT
SCENARIO_FAMILIES = {"H01", "H02", "H03", "H04", "H05", "H06", "H07", "H08", "H19", "S07"}

# An independent in-plane model cannot reproduce a boundary-member force to better than the band the
# reporting limbs already allow, so the capacity limbs carry the same allowance: without it H05/H06
# would be measuring implementation noise rather than the design.
CAP_TOL = 0.05

GRAVITY_SCEN = ["S01", "S02", "S03", "S04"]
LATERAL_SCEN = ["S05", "S06", "S07", "S08", "S10"]
UPLIFT_SCEN = ["S09"]

BASIS_KEYS = ["PG_PSF", "CE", "CT", "IS", "PF_PSF", "GAMMA_PCF", "LR_PSF", "QH_PSF", "A_ZONE_FT",
              "SDS", "IE", "R", "W_SEISMIC_PSF", "FPX_KIP", "WIND_X_KIP", "WIND_Y_KIP",
              "DIAPHRAGM_CLASS"]


def _f(v):
    from .model import _num
    return _num(v)


def _agree(got, want, rel=0.10, absol=0.0):
    g = _f(got)
    return g is not None and abs(g - want) <= max(absol, rel * abs(want))


def analyse(sub, truth):
    """Run every check.  Returns a dict with model-level failures, per-scenario failures, and the
    quantities the score needs (cost, register-independent feasibility)."""
    g = truth["geometry"]
    dk = truth["deck"]
    L = truth["loads"]
    fails = {f: [] for f in FAMILIES}
    scen = {s["id"]: {f: [] for f in FAMILIES} for s in truth["scenarios"]}

    assign, overlap, zone_ids = zone_assignment(sub, truth)
    need = LD.deck_mask(truth)
    zones = [dict(sub.zones[z], zone_id=z) for z in zone_ids]

    # ---------------- H13 catalogue combinations ----------------
    for zi, z in enumerate(zones):
        why = DK.valid_combination(truth, z["gage"] or 0, z["pattern"], z["sidelap"], z["ns"] or 0)
        if why:
            fails["H13"].append(f"{z['zone_id']}: {why}")
        elif int(z["gage"]) not in dk["gages_stocked"]:
            fails["H13"].append(f"{z['zone_id']}: {z['gage']} gage lead time "
                                f"{dk['lead_weeks_16']} weeks exceeds the {dk['project_lead_weeks']} week programme")
        for k in ("gage_inconsistent", "pattern_inconsistent", "sidelap_inconsistent", "ns_inconsistent"):
            if z.get(k):
                fails["H13"].append(f"{z['zone_id']}: {k.split('_')[0]} differs between its rectangles")
    catalogue_ok = not fails["H13"]

    # ---------------- H12 zone geometry ----------------
    gx, gy = g["zone_grid_x_ft"], g["zone_grid_y_ft"]
    for r in sub.zone_rects:
        for v, step, nm in ((r["x0"], gx, "x0"), (r["x1"], gx, "x1"),
                            (r["y0"], gy, "y0"), (r["y1"], gy, "y1")):
            if abs(v / step - round(v / step)) > 1e-6:
                fails["H12"].append(f"{r['zone_id']}: {nm} = {v} is not on the framing grid")
        if r["x0"] < -1e-9 or r["y0"] < -1e-9 or r["x1"] > g["lx_ft"] + 1e-9 or r["y1"] > g["ly_ft"] + 1e-9:
            fails["H12"].append(f"{r['zone_id']}: rectangle leaves the roof")
        if r["x1"] - r["x0"] < gx - 1e-9 or r["y1"] - r["y0"] < gy - 1e-9:
            fails["H12"].append(f"{r['zone_id']}: rectangle smaller than one framing cell")
    n_over = int((overlap > 1).sum())
    if n_over:
        fails["H12"].append(f"{n_over} cells covered by more than one zone rectangle")
    uncovered = int((need & (assign < 0)).sum())
    if uncovered:
        fails["H12"].append(f"{uncovered} deck cells covered by no zone")
    over_open = int((~need & (assign >= 0)).sum())
    if over_open:
        fails["H12"].append(f"{over_open} zone cells fall inside a framed roof opening")
    geometry_ok = not fails["H12"]

    # ---------------- H11 cardinality ----------------
    used = [zi for zi in range(len(zones)) if (assign == zi).any()]
    gages = {int(zones[zi]["gage"]) for zi in used if zones[zi]["gage"]}
    # an "attachment pattern" is the whole fastening specification of a zone: the support pattern,
    # the sidelap type and the number of sidelap connectors per span.
    patterns = {(zones[zi]["pattern"], zones[zi]["sidelap"], zones[zi]["ns"]) for zi in used}
    if len(gages) > dk["max_gages"]:
        fails["H11"].append(f"{len(gages)} deck gages used, at most {dk['max_gages']} permitted: {sorted(gages)}")
    if len(patterns) > dk["max_patterns"]:
        fails["H11"].append(f"{len(patterns)} attachment patterns used, at most {dk['max_patterns']} permitted: "
                            f"{sorted(str(p) for p in patterns)}")
    if len(used) > dk["max_zones"]:
        fails["H11"].append(f"{len(used)} zones, at most {dk['max_zones']} permitted")
    n_rect = len([r for r in sub.zone_rects if r["zone_id"] in {zones[zi]["zone_id"] for zi in used}])
    if n_rect > dk["max_rects"]:
        fails["H11"].append(f"{n_rect} separate rectangles, at most {dk['max_rects']} permitted")

    # ---------------- H09 sidelap rules ----------------
    span_in = g["joist_spacing_ft"] * 12.0
    for zi in used:
        z = zones[zi]
        ns = z["ns"] or 0
        if ns < 1 or span_in / ns > dk["sidelap_max_spacing_in"] + 1e-6:
            fails["H09"].append(f"{z['zone_id']}: {ns} sidelap connectors per {g['joist_spacing_ft']} ft span "
                                f"exceeds the {dk['sidelap_max_spacing_in']} in maximum spacing")

    # ---------------- H10 / H14 boundary and opening attachment ----------------
    xs, ys = LD.grid(truth)
    nx, ny = LD.cell_shape(truth)
    boundary = np.zeros((nx, ny), dtype=bool)
    boundary[0, :] = boundary[-1, :] = True
    boundary[:, 0] = boundary[:, -1] = True
    opening_edge = np.zeros((nx, ny), dtype=bool)
    op = LD.opening_mask(truth)
    opening_edge[1:, :] |= op[:-1, :]
    opening_edge[:-1, :] |= op[1:, :]
    opening_edge[:, 1:] |= op[:, :-1]
    opening_edge[:, :-1] |= op[:, 1:]
    opening_edge &= ~op
    pmin = dk["support_patterns"][dk["perimeter_min_pattern"]]
    for tag, mask, fam in (("diaphragm boundary", boundary & need, "H10"),
                           ("framed opening edge", opening_edge & need, "H14")):
        bad = set()
        for zi in used:
            if not (mask & (assign == zi)).any():
                continue
            z = zones[zi]
            n = dk["support_patterns"].get(z["pattern"], 0)
            if n < pmin or (z["ns"] or 0) < dk["min_sidelap_at_boundary"]:
                bad.add(f"{z['zone_id']} ({z['pattern']}, {z['ns']} sidelaps) on the {tag}")
        for b in sorted(bad):
            fails[fam].append(b)

    # ---------------- fields ----------------
    # The physical families are only meaningful on a design the grader can actually analyse: every
    # deck cell assigned exactly once, to a catalogue combination.  A design that fails either of
    # those does not get those families "for free" because the analysis could not run - each of them
    # is recorded as failed, on the model and in every scenario.
    physics_valid = catalogue_ok and uncovered == 0 and n_over == 0
    fields = DK.cell_fields(truth, zones, assign) if catalogue_ok else None
    cost = DK.installed_cost(truth, zones, assign) if catalogue_ok else float("inf")
    if not physics_valid:
        why = ("no catalogue combination" if not catalogue_ok
               else f"{uncovered} deck cells covered by no zone" if uncovered
               else f"{n_over} cells covered twice")
        msg = f"the design cannot be analysed: {why}"
        for f in ("H01", "H02", "H03", "H04", "H05", "H06", "H07", "H08", "H10", "H14",
                  "H19", "H20", "S01", "S07"):
            fails[f].append(msg)

    # the deck's own weight is part of D and belongs to the cell it is in (criteria section 3)
    if fields is not None:
        wcell = np.where(fields["weight"] > 0, fields["weight"],
                         float(fields["weight"][fields["weight"] > 0].mean())
                         if (fields["weight"] > 0).any() else 2.5)
    else:
        wcell = np.full(LD.cell_shape(truth), 2.5)

    # ---------------- gravity scenarios ----------------
    zone_worst = {zi: dict(shear=0.0, grav=0.0, uplift=0.0, gov="", gov_val=0.0) for zi in used}
    if fields is not None and physics_valid:
        for sid in GRAVITY_SCEN:
            dem = LD.gravity_demand_psf(truth, sid, wcell)
            live = LD.live_component_psf(truth, sid)
            cap = fields["gravity_cap"]
            m = need & (assign >= 0)
            over = m & (dem > cap + 1e-6)
            if over.any():
                i, j = np.argwhere(over)[0]
                scen[sid]["H01"].append(f"{int(over.sum())} cells overloaded, worst "
                                        f"{dem[over].max():.1f} psf on a {int(cap[over].min())} psf cell")
            d_in = fields["defl_per_psf"] * live
            lim = g["joist_spacing_ft"] * 12.0 / dk["defl_limit_denom"]
            bad = m & (d_in > lim + 1e-9)
            if bad.any():
                scen[sid]["H02"].append(f"{int(bad.sum())} cells deflect {d_in[bad].max():.3f} in > {lim:.3f} in")
            # end reaction at an interior support of a 3-span deck: 1.1 w L
            react = 1.1 * dem * g["joist_spacing_ft"]
            rcap = np.zeros_like(react)
            for zi in used:
                rcap[assign == zi] = truth["deck"]["gages"][str(int(zones[zi]["gage"]))]["Rn_end"]
            bad = m & (react > rcap + 1e-6)
            if bad.any():
                scen[sid]["H03"].append(f"{int(bad.sum())} cells exceed the deck end reaction, worst "
                                        f"{react[bad].max():.0f} plf")
            for zi in used:
                mm = (assign == zi) & need
                if mm.any():
                    v = float(dem[mm].max())
                    cmin = float(cap[mm].min())
                    if cmin > 0:
                        r = v / cmin
                        if r > zone_worst[zi]["gov_val"]:
                            zone_worst[zi].update(gov=sid, gov_val=r)
                    zone_worst[zi]["grav"] = max(zone_worst[zi]["grav"], v)

    # ---------------- uplift ----------------
    if fields is not None and physics_valid:
        dem = LD.uplift_demand_psf(truth, wcell)
        m = need & (assign >= 0)
        bad = m & (dem > fields["uplift_cap"] + 1e-6)
        if bad.any():
            scen["S09"]["H08"].append(f"{int(bad.sum())} cells uplift {dem[bad].max():.1f} psf > "
                                      f"{fields['uplift_cap'][bad].min():.1f} psf capacity")
        for zi in used:
            mm = (assign == zi) & need
            if mm.any():
                v = float(dem[mm].max())
                umin = float(fields["uplift_cap"][mm].min())
                if umin > 0:
                    r = v / umin
                    if r > zone_worst[zi]["gov_val"]:
                        zone_worst[zi].update(gov="S09", gov_val=r)
                zone_worst[zi]["uplift"] = v

    # ---------------- lateral scenarios ----------------
    lateral = {}
    if fields is not None and physics_valid:
        D = Diaphragm(truth)
        gt_el, cap_el = D.element_deck(fields["gt"], fields["shear_cap"])
        for sid in LATERAL_SCEN:
            dirn, F, ecc, pat, om = LD.lateral_case(truth, sid)
            res = D.solve(gt_el, cap_el, dirn, F, ecc, pat, om)
            if res is None:
                scen[sid]["H04"].append("the diaphragm model has no solution")
                continue
            if res["reg_share"] > REG_SHARE_LIMIT:
                scen[sid]["H04"].append(f"{100 * res['reg_share']:.1f} % of the load is carried by the "
                                        f"numerical regularisation: no load path")
            v = res["v_plf"]
            over = v > cap_el + truth["tol"]["abs_plf"]
            if over.any():
                scen[sid]["H04"].append(f"{int(over.sum())} panels over capacity, worst "
                                        f"{v[over].max():.0f} plf on a {cap_el[over].min():.0f} plf panel")
            if res["diaphragm_defl_in"] > dk["diaphragm_defl_limit_in"] + 1e-6:
                scen[sid]["H07"].append(f"in-plane deflection {res['diaphragm_defl_in']:.3f} in > "
                                        f"{dk['diaphragm_defl_limit_in']} in")
            chord = D.chord_force_kip(res, dirn)
            coll = D.collector_force_kip(res, dirn) * om
            chord_lim = min(truth["framing"]["chord_capacity_kip"],
                            truth["framing"].get("chord_splice_kip", 1e9))
            if chord > chord_lim * (1.0 + CAP_TOL):
                scen[sid]["H05"].append(f"chord force {chord:.1f} kip > {chord_lim} kip")
            coll_lim = min(truth["framing"]["collector_capacity_kip"],
                           truth["framing"].get("collector_splice_kip", 1e9))
            if coll > coll_lim * (1.0 + CAP_TOL):
                scen[sid]["H06"].append(f"collector force {coll:.1f} kip > {coll_lim} kip")
            rep = sub.diaphragm.get(sid, {})
            if not _agree(rep.get("chord_force_kip"), chord, 0.25, 3.0):
                scen[sid]["H05"].append(f"reported chord force {rep.get('chord_force_kip')!r} vs {chord:.1f} kip")
            if not _agree(rep.get("collector_force_kip"), coll, 0.25, 3.0):
                scen[sid]["H06"].append(f"reported collector force {rep.get('collector_force_kip')!r} vs {coll:.1f} kip")
            if not _agree(rep.get("max_shear_plf"), float(v.max()), 0.20, 30.0):
                scen[sid]["H19"].append(f"reported max shear {rep.get('max_shear_plf')!r} vs {v.max():.0f} plf")
            if not _agree(rep.get("max_deflection_in"), res["diaphragm_defl_in"], 0.30, 0.15):
                scen[sid]["H19"].append(f"reported deflection {rep.get('max_deflection_in')!r} vs "
                                        f"{res['diaphragm_defl_in']:.3f} in")
            lateral[sid] = dict(res=res, dirn=dirn, chord=chord, coll=coll, vmax=float(v.max()))
            for zi in used:
                # reduce the cell mask to elements: a zone touches an element if any of its cells do
                ncy = int(round((D.ys_ft[1] - D.ys_ft[0]) / g["zone_grid_y_ft"]))
                em = (assign == zi).reshape(D.nel_x, D.nel_y, ncy).any(axis=2)
                if em.any():
                    vv = float(v[em].max())
                    zone_worst[zi]["shear"] = max(zone_worst[zi]["shear"], vv)
                    # elements with no deck (a framed opening crosses them) carry no shear and have
                    # no capacity; including them would make the ratio meaningless
                    pos = cap_el[em] > 0
                    if pos.any():
                        r = vv / float(cap_el[em][pos].min())
                        if r > zone_worst[zi]["gov_val"]:
                            zone_worst[zi].update(gov=sid, gov_val=r)

    # ---------------- H20 flexibility classification ----------------
    if lateral:
        cls = "flexible"
        ratios = []
        for sid, d in lateral.items():
            a = d["res"]["avg_frame_disp_in"]
            ratios.append(d["res"]["diaphragm_defl_in"] / a if a > 1e-9 else 99.0)
        cls = "FLEXIBLE" if max(ratios) > dk["flexible_ratio"] else "SEMI-RIGID"
        got = str(sub.basis.get("DIAPHRAGM_CLASS", "")).strip().upper().replace(" ", "-")
        if got != cls:
            fails["H20"].append(f"DIAPHRAGM_CLASS reported {got!r}, re-derived {cls} "
                                f"(max deflection ratio {max(ratios):.2f})")
    else:
        fails["H20"].append("no lateral analysis was possible")

    # ---------------- H21 the drift report ----------------
    for tag, face in LD.DRIFT_FACES:
        peak, a, b = LD.face_drift(truth, tag, face)
        rep = sub.drifts.get((tag.upper(), face))
        if rep is None:
            fails["H21"].append(f"no Drifts row for {tag} face {face}")
            continue
        if not _agree(rep.get("peak_surcharge_psf"), peak, 0.10, 3.0):
            fails["H21"].append(f"{tag} {face}: peak surcharge {rep.get('peak_surcharge_psf')!r}, "
                                f"re-derived {peak:.1f} psf")
        # the band is an interval: its two bounds may be reported in either order (the instruction
        # describes it running from the face outwards, which reverses them on a west or south face)
        got = [_f(rep.get("band_from_ft")), _f(rep.get("band_to_ft"))]
        if any(g is None for g in got):
            fails["H21"].append(f"{tag} {face}: band bounds {rep.get('band_from_ft')!r}, "
                                f"{rep.get('band_to_ft')!r}, re-derived {a:.1f} to {b:.1f} ft")
        else:
            lo, hi = min(got), max(got)
            if abs(lo - a) > 2.0 or abs(hi - b) > 2.0:
                fails["H21"].append(f"{tag} {face}: band {lo:.1f} to {hi:.1f} ft, "
                                    f"re-derived {a:.1f} to {b:.1f} ft")

    # ---------------- H15..H18 design-basis values ----------------
    want = {
        "PG_PSF": (L["pg_psf"], 0.02, 0.0, "H15"),
        "PF_PSF": (L["pf_psf"], 0.03, 0.0, "H15"),
        "GAMMA_PCF": (L["gamma_pcf"], 0.03, 0.0, "H15"),
        "CE": (L["ce"], 0.02, 0.0, "H15"),
        "CT": (L["ct"], 0.02, 0.0, "H15"),
        "IS": (L["is_"], 0.02, 0.0, "H15"),
        "LR_PSF": (L["lr_deck_psf"], 0.02, 0.0, "H16"),
        "W_SEISMIC_PSF": (LD.seismic_weight_psf(truth), 0.03, 0.0, "H17"),
        "FPX_KIP": (LD.seismic_diaphragm_force_kip(truth)[0], 0.05, 0.0, "H17"),
        "SDS": (L["sds"], 0.02, 0.0, "H17"),
        "IE": (L["ie"], 0.02, 0.0, "H17"),
        "R": (L["r"], 0.02, 0.0, "H17"),
        "QH_PSF": (L["qh_psf"], 0.03, 0.0, "H18"),
        "A_ZONE_FT": (L["a_zone_ft"], 0.03, 0.0, "H18"),
        "WIND_X_KIP": (L["wind_roof_force_x_kip"], 0.05, 0.0, "H18"),
        "WIND_Y_KIP": (L["wind_roof_force_y_kip"], 0.05, 0.0, "H18"),
    }
    for k, (v, rel, ab, fam) in want.items():
        if not _agree(sub.basis.get(k), v, rel, ab):
            fails[fam].append(f"DesignBasis {k} = {sub.basis.get(k)!r}, re-derived {v:.4g}")

    # ---------------- H19 / S07 per-zone reporting ----------------
    for zi in (used if physics_valid else []):
        z = zones[zi]
        rep = sub.calc.get(z["zone_id"], {})
        w = zone_worst[zi]
        for key, val, rel, ab in (("shear_demand_plf", w["shear"], 0.20, 30.0),
                                  ("gravity_demand_psf", w["grav"], 0.08, 2.0),
                                  ("uplift_demand_psf", w["uplift"], 0.08, 2.0)):
            if not _agree(rep.get(key), val, rel, ab):
                fails["H19"].append(f"{z['zone_id']}: {key} reported {rep.get(key)!r}, re-derived {val:.4g}")
        got = str(rep.get("governing_scenario", "")).strip().upper()
        if got != w["gov"]:
            fails["S07"].append(f"{z['zone_id']}: governing scenario reported {got!r}, re-derived {w['gov']}")

    # ---------------- S01 completeness ----------------
    miss_calc = [z["zone_id"] for zi, z in enumerate(zones) if zi in used and z["zone_id"] not in sub.calc]
    miss_basis = [k for k in BASIS_KEYS if k not in sub.basis]
    miss_drift = [f"{t} {f}" for t, f in LD.DRIFT_FACES if (t.upper(), f) not in sub.drifts]
    if miss_calc or miss_basis or miss_drift:
        fails["S01"].append(f"CalcSummary missing {len(miss_calc)} zones; DesignBasis missing "
                            f"{miss_basis}; Drifts missing {miss_drift}")

    # ---------------- S02 / S03 drawing ----------------
    lay = truth["deliverable"]["layers"]
    polys = [p for p in sub.dxf_polys if p["layer"].upper().startswith(lay["zone"].upper())]
    if sub.dxf_error:
        fails["S02"].append(f"roof_deck_plan.dxf: {sub.dxf_error}")
    else:
        unmatched = 0
        for r in sub.zone_rects:
            hit = False
            for p in polys:
                if (abs(min(p["x"]) - r["x0"]) <= 0.5 and abs(max(p["x"]) - r["x1"]) <= 0.5
                        and abs(min(p["y"]) - r["y0"]) <= 0.5 and abs(max(p["y"]) - r["y1"]) <= 0.5):
                    hit = True
                    break
            if not hit:
                unmatched += 1
        if unmatched:
            fails["S02"].append(f"{unmatched} of {len(sub.zone_rects)} zone rectangles have no matching "
                                f"closed polyline on layer {lay['zone']}")
        for key, what in (("framing", "the roof framing"), ("opening", "the roof openings")):
            n = sum(1 for p in sub.dxf_polys if p["layer"].upper().startswith(lay[key].upper()))
            n += sum(1 for t in sub.dxf_labels if t["layer"].upper().startswith(lay[key].upper()))
            n += sum(1 for l in sub.dxf_lines if l["layer"].upper().startswith(lay[key].upper()))
            if n == 0:
                fails["S02"].append(f"nothing on layer {lay[key]} ({what})")
        labels = [t for t in sub.dxf_labels if t["layer"].upper().startswith(lay["text"].upper())]
        missing = []
        for zid, z in sub.zones.items():
            ok = any(zid.lower() in (t["text"] or "").lower()
                     and any(r["x0"] - 0.5 <= t["x"] <= r["x1"] + 0.5 and r["y0"] - 0.5 <= t["y"] <= r["y1"] + 0.5
                             for r in z["rects"])
                     for t in labels)
            if not ok:
                missing.append(zid)
        if missing:
            fails["S03"].append(f"{len(missing)} zones without a label on layer {lay['text']}: {missing[:5]}")

    # ---------------- S04 BOM ----------------
    if fields is not None:
        area_cell = g["zone_grid_x_ft"] * g["zone_grid_y_ft"]
        per_gage, n_sup, n_lap = {}, 0.0, 0.0
        for zi in used:
            z = zones[zi]
            n = int((assign == zi).sum())
            a = n * area_cell
            per_gage[int(z["gage"])] = per_gage.get(int(z["gage"]), 0.0) + a
            lines = a / (g["panel_width_ft"] * g["joist_spacing_ft"])
            n_sup += lines * dk["support_patterns"][z["pattern"]]
            n_lap += lines * int(z["ns"])
        bad = []
        for gg, a in per_gage.items():
            k = f"deck area {gg} gage"
            if not _agree(sub.bom.get(k.lower()), a, 0.02, 1.0):
                bad.append(f"{k}: {sub.bom.get(k.lower())!r} vs {a:.0f} sf")
        if not _agree(sub.bom.get("support fasteners"), n_sup, 0.03, 10.0):
            bad.append(f"support fasteners: {sub.bom.get('support fasteners')!r} vs {n_sup:.0f}")
        if not _agree(sub.bom.get("sidelap connectors"), n_lap, 0.03, 10.0):
            bad.append(f"sidelap connectors: {sub.bom.get('sidelap connectors')!r} vs {n_lap:.0f}")
        if bad:
            fails["S04"].append("; ".join(bad[:4]))

    # ---------------- S05 diaphragm report completeness ----------------
    miss = [s for s in LATERAL_SCEN if s not in sub.diaphragm]
    if miss:
        fails["S05"].append(f"Diaphragm sheet missing scenarios {miss}")

    # ---------------- S06 design basis narrative ----------------
    md = sub.basis_md
    prob = []
    if len(md) < 1500:
        prob.append(f"design_basis.md is {len(md)} characters")
    sw = truth["geometry"]["screen_walls"][0]
    if not re.search(rf"\b{int(sw['y0'])}\b", md):
        prob.append(f"the resolved position of {sw['tag']} (grid {int(sw['y0'])} ft) is not stated")
    if not re.search(r"\b63\b", md) or not re.search(r"(?i)risk\s*category\s*II\b|\bRC\s*II\b", md):
        prob.append("the ground snow load and the risk category its case study is based on are not stated")
    if prob:
        fails["S06"].append("; ".join(prob))

    return dict(fails=fails, scen=scen, cost=cost, zones=zones, used=used, assign=assign,
                lateral={k: dict(vmax=v["vmax"], chord=v["chord"], coll=v["coll"],
                                 defl=v["res"]["diaphragm_defl_in"]) for k, v in lateral.items()},
                n_zones=len(used), gages=sorted(gages), patterns=sorted(str(p) for p in patterns),
                deck_area_sf=float(need.sum()) * g["zone_grid_x_ft"] * g["zone_grid_y_ft"])


def family_ok(ana, fam, sid):
    if fam in SCENARIO_FAMILIES:
        return not ana["scen"][sid][fam] and not ana["fails"][fam]
    return not ana["fails"][fam]
