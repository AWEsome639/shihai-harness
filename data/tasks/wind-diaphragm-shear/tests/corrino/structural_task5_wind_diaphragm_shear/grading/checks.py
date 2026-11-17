"""checks.py - one function per constraint family.

Model-scope families are evaluated once for the package; scenario-scope families are evaluated
for each of the twelve wind scenarios against a re-analysis of the SUBMITTED design.  Every
check returns {"ok": bool, ...evidence}, never raises, and never reads anything the candidate
could have written except through `Submission`.
"""
from __future__ import annotations

import math
import re

import numpy as np

from .cost import design_cost, min_embeds
from .model import chord_by_code, deck_by_code

LINE_IDS = ("S", "E", "N2", "E2", "N1", "W")

# What each required section of the calculation package has to be ABOUT.  Each entry is a list
# of alternatives; an alternative is satisfied when every word in it appears somewhere in the
# package.
SECTION_WORDS = {
    "wind pressures": [("wind", "pressure"), ("velocity pressure",), ("q_h",), ("qh",)],
    "parapet cantilever": [("parapet",), ("screen wall",)],
    "diaphragm reactions": [("reaction",), ("line load",)],
    "diaphragm shears": [("unit shear",), ("diaphragm", "shear")],
    "chord forces": [("chord",), ("collector",)],
    "shear wall loads": [("shear wall",), ("wall", "load"), ("pier",)],
    "design basis": [("design basis",), ("basis",), ("governing",)],
}


def _close(a, b, rel, abs_):
    if a is None or b is None:
        return False
    return abs(a - b) <= max(rel * abs(b), abs_)


def _close_mag(a, b, rel, abs_):
    """Compare MAGNITUDES.

    Unit shears, wall reactions, chord forces and deflections have a sign only by convention -
    which way is positive depends on how the engineer set out the model, and the scenario already
    says which way the wind blows. The instruction says magnitudes;
    the grader reads them that way whichever sign the solver chose."""
    if a is None or b is None:
        return False
    return abs(abs(a) - abs(b)) <= max(rel * abs(b), abs_)


def _basis_table(text: str) -> dict:
    """The `| item | value | source |` rows of the design basis, keyed by item.

    A key maps to the LIST of values stated for it, in order. A basis may record the superseded parameters in a table of the same shape -
    which is exactly what "record every conflict you resolved" invites. Which order two tables sit in is
    not something the submittal specification asks of anyone, so a value counts as stated when
    the basis states it anywhere in its data tables. The cap keeps that from becoming a list of
    candidate answers.
    """
    out: dict[str, list] = {}
    for line in text.splitlines():
        if line.count("|") < 2:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or set("".join(cells)) <= set("-: "):
            continue
        k = re.sub(r"[^a-z0-9]+", "_", cells[0].lower()).strip("_")
        if k in ("item", "parameter", "key") or not k:
            continue
        vals = out.setdefault(k, [])
        if len(vals) < MAX_STATED and cells[1] not in vals:
            vals.append(cells[1])
    return out


# Two: a basis may record the superseded value alongside the governing one, which is exactly
# what "record every conflict you resolved" asks for. Three starts to be a list of candidates.
MAX_STATED = 2


def _basis_nums(tbl: dict, *keys):
    """Every number stated for the first key that appears."""
    for k in keys:
        if k in tbl:
            out = []
            for cell in tbl[k]:
                m = re.search(r"[-+]?\d*\.?\d+", str(cell).replace(",", ""))
                if m:
                    out.append(float(m.group(0)))
            return out
    return []


def _basis_num(tbl: dict, *keys):
    n = _basis_nums(tbl, *keys)
    return n[0] if n else None


def _basis_txts(tbl: dict, *keys):
    for k in keys:
        if k in tbl:
            return list(tbl[k])
    return []


def _basis_txt(tbl: dict, *keys):
    t = _basis_txts(tbl, *keys)
    return t[0] if t else ""


def _stated(tbl, keys, ok):
    """True when ANY value the basis states for this item satisfies `ok`."""
    return any(ok(v) for v in _basis_nums(tbl, *keys))


class Checks:
    def __init__(self, T, sub, design, A, ref):
        self.T = T
        self.sub = sub
        self.d = design
        self.A = A
        self.ref = ref                     # {scenario: results}
        self.deck_by = deck_by_code(T)
        self.chord_by = chord_by_code(T)
        self.piers = A.piers
        self.basis = _basis_table(sub.basis_text)
        self.cost = design_cost(T, design)
        self._pier_index = {}
        for p in self.piers:
            self._pier_index[(p["panel"], round(p["s0"], 1))] = p

    # ------------------------------------------------------------------ helpers
    def _sub_piers(self):
        """Submitted piers matched to reference piers by panel mark and start station."""
        matched, unmatched = {}, []
        for r in self.sub.piers:
            key = None
            if r["panel"] and r["s0"] is not None:
                for (pm, s0), p in self._pier_index.items():
                    if pm.upper() == r["panel"].upper() and abs(s0 - r["s0"]) <= 0.25:
                        key = (pm, s0)
                        break
            if key is None:
                unmatched.append(r)
            else:
                matched[key] = r
        return matched, unmatched

    def _panel_shear(self, res):
        """Reference ASD shear collected by each panel (sum over its piers)."""
        out = {}
        for i, p in enumerate(self.piers):
            out[p["panel"]] = out.get(p["panel"], 0.0) + abs(res["reac"][i])
        return out

    # ------------------------------------------------------------------ model scope
    def model_checks(self) -> dict:
        T, sub = self.T, self.sub
        out = {}

        # G1 - footprint and take-off
        zones = {z["id"]: z for z in T["zones"]}
        rows = {r["zone"].upper(): r for r in sub.deck_zones if r["zone"]}
        bad = []
        for zid, z in zones.items():
            r = rows.get(zid.upper())
            if r is None:
                bad.append(f"{zid}: missing")
            elif not _close(r["area"], z["area_sf"], 0.015, 1.0):
                bad.append(f"{zid}: area {r['area']} vs {z['area_sf']}")
        extra = [k for k in rows if k not in {z.upper() for z in zones}]
        tot = sum(r["area"] or 0.0 for r in rows.values())
        true_tot = sum(z["area_sf"] for z in zones.values())
        if not _close(tot, true_tot, 0.01, 10.0):
            bad.append(f"total area {tot:.0f} vs {true_tot:.0f}")
        out["G1_FOOTPRINT"] = {"ok": not bad and not extra, "bad": bad[:6], "extra": extra[:6]}

        # G2 - parapet / screen wall heights from the as-built model
        matched, unmatched = self._sub_piers()
        bad = []
        for key, p in self._pier_index.items():
            r = matched.get(key)
            if r is None:
                continue
            if not _close(r.get("top_z"), p["top_z"], 0.0, 0.5):
                bad.append(f"{p['pier']}: top {r.get('top_z')} vs {p['top_z']}")
        out["G2_PARAPET_HEIGHT"] = {"ok": len(bad) == 0 and len(matched) == len(self.piers),
                                    "bad": bad[:6], "matched": len(matched)}

        # G3 - pier set and stiffness
        bad = []
        for key, p in self._pier_index.items():
            r = matched.get(key)
            if r is None:
                bad.append(f"{p['pier']}: missing")
                continue
            if not _close(r["width"], p["width"], 0.0, 0.25):
                bad.append(f"{p['pier']}: width {r['width']} vs {p['width']}")
            elif not _close(r["k"], p["k_kip_per_in"], 0.05, 1.0):
                bad.append(f"{p['pier']}: k {r['k']} vs {p['k_kip_per_in']:.0f}")
        out["G3_PIERS"] = {"ok": not bad and not unmatched,
                           "bad": bad[:6], "unmatched": len(unmatched),
                           "n_sub": len(sub.piers), "n_ref": len(self.piers)}

        # G4 / G5 / G6 - deck assignment, type cap, catalogue
        codes = [r["code"] for r in sub.deck_zones
                 if r["zone"].upper() in {z.upper() for z in zones}]
        good_codes = [c for c in codes if c in self.deck_by]
        out["G4_ZONE_ASSIGN"] = {"ok": len(good_codes) == len(zones) and len(codes) == len(zones),
                                 "n_assigned": len(good_codes), "n_zones": len(zones)}
        distinct = sorted(set(good_codes))
        out["G5_TYPE_CAP"] = {"ok": len(distinct) <= T["deck"]["max_types"] and len(distinct) > 0,
                              "distinct": distinct, "cap": T["deck"]["max_types"]}
        ch_codes = [r["code"] for r in sub.chord_schedule]
        out["G6_CATALOGUE"] = {
            "ok": all(c in self.deck_by for c in codes) and bool(codes)
                  and all(c in self.chord_by for c in ch_codes) and bool(ch_codes),
            "bad_deck": [c for c in codes if c not in self.deck_by][:4],
            "bad_chord": [c for c in ch_codes if c not in self.chord_by][:4]}

        # G7 - every chord line assigned, with the right length
        cl = {c["id"]: c for c in T["chord_lines"]}
        crows = {r["chord"].upper(): r for r in sub.chord_schedule if r["chord"]}
        bad = []
        for cid, c in cl.items():
            r = crows.get(cid.upper())
            if r is None or r["code"] not in self.chord_by:
                bad.append(f"{cid}: missing or not in catalogue")
            elif not _close(r["length"], c["s1"] - c["s0"], 0.02, 1.0):
                bad.append(f"{cid}: length {r['length']} vs {c['s1'] - c['s0']}")
        out["G7_CHORD_ASSIGN"] = {"ok": not bad, "bad": bad[:6]}

        # G8 - deck span factor in the 4 ft zone
        bad = []
        for zid, z in zones.items():
            r = rows.get(zid.upper())
            c = self.deck_by.get(r["code"]) if r else None
            if c is None:
                bad.append(f"{zid}: no deck")
                continue
            want = c["G_kip_per_in"] * (T["deck"]["span_factor_4ft"]
                                        if z["deck_span_ft"] == 4.0 else 1.0)
            if not _close(r["g_prime"], want, 0.02, 0.2):
                bad.append(f"{zid}: G' {r['g_prime']} vs {want:.1f}")
        out["G8_SPAN_FACTOR"] = {"ok": not bad, "bad": bad[:6]}

        # G9 - ASD capacities
        bad = []
        for zid in zones:
            r = rows.get(zid.upper())
            c = self.deck_by.get(r["code"]) if r else None
            if c is None:
                bad.append(f"{zid}: no deck")
                continue
            want = c["Sn_plf"] / T["deck"]["omega_d"]
            if not _close(r["capacity"], want, 0.02, 5.0):
                bad.append(f"{zid}: capacity {r['capacity']} vs {want:.0f}")
        out["G9_ASD_CAPACITY"] = {"ok": not bad, "bad": bad[:6]}

        # G10 - capital cap
        cap = T["costs"]["capital_cap"]
        out["G10_CAP"] = {"ok": self.cost["total"] <= cap + 1e-6,
                          "cost": self.cost["total"], "cap": cap}

        # G11 - embed spacing / minimum count
        bad = []
        for lid, prows in T["panels"].items():
            for p in prows:
                n = self.d.embeds.get(p["mark"])
                need = min_embeds(T, p["s1"] - p["s0"])
                if n is None or int(n) < need:
                    bad.append(f"{p['mark']}: {n} < {need}")
        out["G11_EMBED_SPACING"] = {"ok": not bad, "bad": bad[:6]}

        # G12 - drawing.  Normalise line endings first, so a drawing written with CRLF
        # counts its entities.
        txt = sub.dxf_text.replace("\r\n", "\n").replace("\r", "\n").upper()
        layers = [l for l in T["dxf_layers"] if l.upper() in txt]
        has_dirs = ("TRANSVERSE" in txt and "LONGITUDINAL" in txt)
        has_asd = "ASD" in txt
        n_ent = sum(txt.count(f"\n{e}\n") for e in
                    ("LINE", "LWPOLYLINE", "POLYLINE", "TEXT", "MTEXT", "CIRCLE", "ARC",
                     "INSERT", "HATCH", "DIMENSION", "SOLID", "POINT", "ELLIPSE", "SPLINE"))
        # The layers that carry the drawing's content have to carry something.
        
        
        
        # Entities are counted as entities, and the
        # requirement is simply that each of the three carries at least one - the specification
        # says what goes on them, it does not say how many.
        populated, layers_used = {}, False
        try:
            import ezdxf
            import io as _io
            doc = ezdxf.read(_io.StringIO(sub.dxf_text))
            for e in doc.modelspace():
                lay = str(getattr(e.dxf, "layer", "")).upper()
                populated[lay] = populated.get(lay, 0) + 1
            layers_used = all(populated.get(l, 0) >= 1
                              for l in ("S-DIAP-SHEAR", "S-WALL-REAC", "S-CHRD"))
            n_ent = sum(populated.values())
        except Exception as exc:  # noqa: BLE001
            populated = {"error": str(exc)[:60]}
        populated = {k: v for k, v in populated.items()
                     if k in ("S-DIAP-SHEAR", "S-WALL-REAC", "S-CHRD", "error")}
        # No entity-count threshold: it appears in no document.
        # What
        # specification 1.7 actually requires is the seven layers, the two directions, the ASD
        # declaration, and content on the layers that carry the design.
        out["G12_DXF"] = {"ok": len(layers) == len(T["dxf_layers"]) and has_dirs and has_asd
                                and layers_used,
                          "layers_found": len(layers), "dirs": has_dirs, "asd": has_asd,
                          "entities": n_ent, "on_layer": populated}

        # G13 - calculation package
        pdf_ok, pages, size, found = False, 0, None, []
        if sub.pdf_bytes:
            try:
                import pymupdf
                doc = pymupdf.open(stream=sub.pdf_bytes, filetype="pdf")
                pages = doc.page_count
                r = doc[0].rect
                size = (round(r.width, 1), round(r.height, 1))
                # G13 is a FORMAT and COVERAGE check, and nothing more.
                #
                
                
                
                
                
                
                # Whether the calculations are RIGHT is measured where it belongs and
                # where it is hard-gated: F9-F13 re-run the analysis on the submitted design and
                # compare every reported quantity. So this family checks what specification
                # 01 33 00 par 1.6 and criteria 11.1 actually require of the file - letter
                # portrait, a real package rather than a cover sheet, and every subject the
                # criteria lists covered somewhere in it.
                pagetexts = [doc[i].get_text().lower() for i in range(min(pages, 60))]
                text = " ".join(pagetexts)
                found = [k for k, alts in SECTION_WORDS.items()
                         if any(all(w in text for w in alt) for alt in alts)]
                letter = abs(r.width - 612) <= 3 and abs(r.height - 792) <= 3
                pdf_ok = letter and pages >= 4 and len(found) == len(T["pdf_sections"])
                doc.close()
            except Exception as exc:  # noqa: BLE001
                found = [f"error {exc}"]
        out["G13_PDF"] = {"ok": pdf_ok, "pages": pages, "page_size_pt": size,
                          "sections_found": len(found), "missing": sorted(
                              set(T["pdf_sections"]) - set(found)),
                          "sections_required": len(T["pdf_sections"])}

        # G14 - schedule completeness
        n_expect_diaph = len(T["scenarios"]) * len(T["wall_lines"]) * len(T["report_fractions"])
        n_expect_wall = len(T["scenarios"]) * len(T["wall_lines"])
        cost_rows = sum(r["total"] or 0.0 for r in sub.cost_rows)
        out["G14_SCHEDULE"] = {
            "ok": (len(sub.diaphragm) >= n_expect_diaph and len(sub.wall_loads) >= n_expect_wall
                   and len(sub.chords) >= len(T["scenarios"]) * len(T["chord_lines"])
                   and len(sub.drift) >= 2 * len(T["scenarios"])
                   and sub.placeholders == 0
                   and _close(cost_rows, self.cost["total"], 0.02, 500.0)),
            "n_diaph": len(sub.diaphragm), "n_wall": len(sub.wall_loads),
            "n_chord": len(sub.chords), "n_drift": len(sub.drift),
            "placeholders": sub.placeholders, "cost_rows": round(cost_rows, 0)}

        # G15 - mesh convergence
        # the same normalisation D8 applies: "L2", "Level 2" and "L2 (H = 5.0 ft)" are one level
        conv = {}
        for r in sub.convergence:
            t = re.sub(r"[^A-Z0-9]", "", str(r["level"] or "").upper())
            m = re.search(r"L(?:EVEL)?(\d)", t)
            if m:
                conv.setdefault(f"L{m.group(1)}", r)
        ok = False
        detail = {}
        if all(k in conv for k in ("L1", "L2", "L3")):
            hs = {k: conv[k]["h"] for k in ("L1", "L2", "L3")}
            el = {k: conv[k]["elements"] for k in ("L1", "L2", "L3")}
            v = {k: conv[k]["key_reaction"] for k in ("L1", "L2", "L3")}
            h_ok = all(_close(hs[k], T["fe"]["levels"][k], 0.0, 0.01) for k in hs)
            e_ok = all(_close(el[k], T["mesh_elements"][k], 0.02, 1.0) for k in el)
            gci = (abs(v["L2"] - v["L3"]) / abs(v["L3"])
                   if v.get("L2") and v.get("L3") else 1.0)
            ok = bool(h_ok and e_ok and gci <= T["limits"]["gci_max"])
            detail = {"h_ok": h_ok, "elements_ok": e_ok, "gci": round(gci, 4)}
        out["G15_GCI"] = {"ok": ok, **detail}

        # ---------------------------------------------------------- design basis
        tbl = self.basis
        out["D2_KZT"] = {"ok": _stated(tbl, ("kzt", "topographic_factor"), lambda v: _close(v, self.T["wind"]["Kzt"], 0.0, 0.01)),
                        "stated": _basis_nums(tbl, "kzt", "topographic_factor")}
        out["D3_KE"] = {"ok": _stated(tbl, ("ke", "ground_elevation_factor"), lambda v: _close(v, self.T["wind"]["Ke"], 0.0, 0.01)),
                        "stated": _basis_nums(tbl, "ke", "ground_elevation_factor")}
        out["D4_QH"] = {"ok": _stated(tbl, ("q_h_psf", "qh_psf", "q_h"), lambda v: _close(v, self.T["wind"]["q_h"], 0.03, 0.0)),
                        "stated": _basis_nums(tbl, "q_h_psf", "qh_psf", "q_h")}
        out["D5_SCREEN"] = {"ok": _stated(tbl, ("screen_wall_top_ft", "screen_wall_top"), lambda v: _close(v, self.T["geometry"]["screen_wall"]["top_z"], 0.0, 0.5)),
                        "stated": _basis_nums(tbl, "screen_wall_top_ft", "screen_wall_top")}
        ab = " ".join(_basis_txts(tbl, "asbuilt_opening_panel",
                                  "as_built_opening_panel")).upper()
        want_ab = sorted(p["mark"] for rows in T["panels"].values() for p in rows
                         if p.get("asbuilt_only"))
        # Naming every panel in the building is not finding the one that was cut in the field:
        # the answer may name a couple of
        # candidates, not a catalogue.
        all_marks = {p["mark"].upper() for rows in T["panels"].values() for p in rows}
        # strip document ids first: "cf. PC-S01, PC-S02" is a citation, not a panel list
        
        ab_marks = re.sub(r"\b(?:PC|A|S|SJ|E|M|P)-S?\d{2,3}[A-Z]?\b", " ", ab)
        named = {m for m in all_marks
                 if re.search(rf"(?<![A-Z0-9]){m}(?![A-Z0-9])", ab_marks)}
        out["D6_ASBUILT_DOOR"] = {"ok": bool(want_ab) and all(m in named for m in want_ab)
                                        and len(named) <= len(want_ab) + 2,
                                  "value": ab[:80], "named": sorted(named)[:6]}
        comb = " ".join(_basis_txts(tbl, "asd_combination",
                                    "load_combination")).replace(" ", "").upper()
        om_ok = _stated(tbl, ("omega_d", "deck_safety_factor"),
                        lambda v: _close(v, T["deck"]["omega_d"], 0.0, 0.02))
        om = _basis_num(tbl, "omega_d", "deck_safety_factor")
        # "0.6W", "0.60W" and "0.6 W" are one answer
        # "0.6W", "0.60W", "0.6*W", "0.6(D+W)", "0.6D+0.6W" are one answer
        asd_ok = re.search(r"0\.6\d*\s*[*x(]?\s*(?:D\s*\+\s*)?W", comb) is not None
        out["D7_ASD"] = {"ok": asd_ok and om_ok,
                         "combination": comb, "omega": om}
        meth = " ".join(_basis_txts(tbl, "analysis_method", "diaphragm_idealisation",
                                    "diaphragm_idealization")).lower()
        # "L2" and "Level 2" name the same refinement level
        lvl = re.sub(r"[^A-Z0-9]", "", " ".join(
            _basis_txts(tbl, "mesh_level", "grading_mesh_level")).upper())
        out["D8_MODEL"] = {"ok": ("semi" in meth and "rigid" in meth)
                                 and T["fe"]["grade_level"] in lvl,
                           "method": meth[:40], "level": lvl}

        # D1 - governing scenario per wall line
        gov, near = self.governing()
        # Specification 01 33 00 par 1.6 defines the table: a header naming the governing
        # scenario, then one row per wall line. So that is what is read - the rows under that
        # header, and only those.
        #
        
        
        # Reading one labelled table kills
        # the ordering trap and the union of every line-keyed row. A basis that never labels its table falls back to the
        # first contiguous block of line-keyed rows, which is the only block an unlabelled
        # document has.
        claimed = _governing_table(sub.basis_text)
        bad = [f"{l}: {sorted(claimed.get(l, []))} misses {near[l]}" for l in gov
               if not (claimed.get(l, set()) & set(near[l]))]
        out["D1_GOVERNING"] = {"ok": not bad, "bad": bad[:6],
                               "claimed": {k: sorted(v) for k, v in claimed.items()}}
        return out

    def governing(self):
        """Governing scenario per wall line (largest reaction), with the near-ties accepted."""
        gov, near = {}, {}
        for line in self.T["wall_lines"]:
            lid = line["id"]
            xs = sorted(((self.ref[s["id"]]["line_reac"].get(lid, 0.0), s["id"])
                         for s in self.T["scenarios"]), reverse=True)
            best = xs[0][0]
            gov[lid] = xs[0][1]
            near[lid] = [sid for v, sid in xs if best - v <= 0.05 * max(best, 1e-9)]
        return gov, near

    # ------------------------------------------------------------------ scenario scope
    def scenario_checks(self, sc: dict, res: dict) -> dict:
        T, sub = self.T, self.sub
        sid = sc["id"]
        out = {}

        # F1 - deck capacity
        q = np.abs(res["q"]) * 1000.0
        over = [(self.A.elem_zone[e], round(float(q[e]), 0), round(float(self.A.cap_plf[e]), 0))
                for e in range(len(q)) if q[e] > self.A.cap_plf[e] + 1e-6]
        out["F1_DECK_CAPACITY"] = {"ok": not over, "n_over": len(over), "worst": over[:3]}

        # F2 / F3 - chord and collector capacity
        for key, roles in (("F2_CHORD_CAPACITY", ("chord",)),
                           ("F3_COLLECTOR_CAPACITY", ("collector", "opening"))):
            bad = []
            for cl in T["chord_lines"]:
                if cl["role"] not in roles:
                    continue
                code = self.d.chords.get(cl["id"])
                c = self.chord_by.get(code)
                t, cc = res["chord_signed"][cl["id"]]
                if c is None:
                    bad.append(f"{cl['id']}: no section")
                elif t > c["allow_t_kips"] + 1e-6 or -cc > c["allow_c_kips"] + 1e-6:
                    bad.append(f"{cl['id']}: T {t:.1f}/C {-cc:.1f} vs "
                               f"{c['allow_t_kips']}/{c['allow_c_kips']}")
            out[key] = {"ok": not bad, "bad": bad[:4]}

        # F4 - panel base connection.  Criteria 5.7 states the capacity per foot of PANEL length
        # against the PANEL's in-plane shear (5.6: the sum over its legs).
        
        
        panel_v0 = self._panel_shear(res)
        widths0 = {p_["mark"]: p_["s1"] - p_["s0"]
                   for rows in T["panels"].values() for p_ in rows}
        bad = []
        for mark, v in panel_v0.items():
            cap = T["connections"]["base_asd_kips_per_ft"] * widths0[mark]
            if v > cap + 1e-6:
                bad.append(f"{mark}: {v:.1f} > {cap:.1f}")
        out["F4_PIER_BASE"] = {"ok": not bad, "bad": bad[:4]}

        # F5 - embeds
        panel_v = self._panel_shear(res)
        bad = []
        for mark, v in panel_v.items():
            n = int(self.d.embeds.get(mark, 0) or 0)
            cap = n * T["connections"]["embed_asd_kips"]
            if v > cap + 1e-6:
                bad.append(f"{mark}: {v:.1f} > {cap:.1f} ({n} embeds)")
        out["F5_EMBED_CAPACITY"] = {"ok": not bad, "bad": bad[:4]}

        # F6 - overturning
        weights, widths = {}, {}
        for rows in T["panels"].values():
            for p in rows:
                weights[p["mark"]] = ((p["t_in"] / 12.0) * (p["s1"] - p["s0"]) * p["top_z"]
                                      * T["materials"]["panel_density_pcf"] / 1000.0)
                widths[p["mark"]] = p["s1"] - p["s0"]
        bad = []
        for mark, v in panel_v.items():
            M_ot = v * T["geometry"]["roof_z"]
            M_r = 0.6 * weights[mark] * widths[mark] / 2.0
            if mark in self.d.tiedowns:
                M_r += T["connections"]["tiedown_asd_kips"] * widths[mark]
            if M_ot > M_r + 1e-6:
                bad.append(f"{mark}: Mot {M_ot:.0f} > Mr {M_r:.0f}")
        out["F6_OVERTURNING"] = {"ok": not bad, "bad": bad[:4]}

        # F7 / F8 - drift
        dmax = max(res["deflection"].values())
        out["F7_DIAPH_DRIFT"] = {"ok": dmax <= T["limits"]["diaphragm_drift_in"] + 1e-9,
                                 "max_in": round(dmax, 3),
                                 "limit": T["limits"]["diaphragm_drift_in"]}
        pmax = max(res["pier_drift"].values()) if res["pier_drift"] else 0.0
        plim = T["geometry"]["roof_z"] * 12.0 * T["limits"]["pier_drift_ratio"]
        out["F8_PIER_DRIFT"] = {"ok": pmax <= plim + 1e-9, "max_in": round(pmax, 3),
                                "limit": round(plim, 3)}

        # F9 - reported unit shears
        rel, ab_plf = T["limits"]["agree_rel"], T["limits"]["agree_abs_plf"]
        rows = {(r["line"], round(r["frac"], 2)): r["v"] for r in sub.diaphragm
                if r["scenario"] == sid and r["frac"] is not None}
        # The criteria fixes the quarter points but not which end of a line they are measured
        # from, so a station reported from the other end is the same station mirrored: 0.25 and
        # 0.75 swap, 0.50 is itself. Accept either orientation rather than grade a convention
        # the documents do not state.
        bad, n = [], 0
        for key, ref_v in res["stations"].items():
            lid, frac = key.split("@")
            f = round(float(frac), 2)
            got = rows.get((lid, f))
            mirrored = rows.get((lid, round(1.0 - f, 2)))
            ref_mirror = res["stations"].get(f"{lid}@{1.0 - f:.2f}")
            n += 1
            ok = _close_mag(got, ref_v, rel, ab_plf)
            if not ok and mirrored is not None and ref_mirror is not None:
                ok = _close_mag(mirrored, ref_v, rel, ab_plf)
            if not ok:
                bad.append(f"{key}: {got} vs {ref_v:.0f}")
        out["F9_SHEAR_AGREE"] = {"ok": not bad, "n": n, "bad": bad[:4]}

        # F10 - reported wall line reactions
        rows = {r["line"]: r for r in sub.wall_loads if r["scenario"] == sid}
        bad = []
        for lid, ref_v in res["line_reac"].items():
            got = rows.get(lid, {}).get("reaction")
            if not _close_mag(got, ref_v, rel, T["limits"]["agree_abs_kips"]):
                bad.append(f"{lid}: {got} vs {ref_v:.1f}")
        out["F10_REACT_AGREE"] = {"ok": not bad, "bad": bad[:4]}

        # F11 - reported roof-level line load (the parapet lever)
        bad = []
        for lid, ref_v in res["line_load"].items():
            got = rows.get(lid, {}).get("line_load")
            if not _close_mag(got, ref_v, rel, ab_plf):
                bad.append(f"{lid}: {got} vs {ref_v:.0f}")
        out["F11_LINELOAD_AGREE"] = {"ok": not bad, "bad": bad[:4]}

        # F12 - reported chord forces
        crows = {r["chord"].upper(): r["force"] for r in sub.chords if r["scenario"] == sid}
        bad = []
        for cid, ref_v in res["chord_force"].items():
            if not _close_mag(crows.get(cid.upper()), ref_v, rel, T["limits"]["agree_abs_kips"]):
                bad.append(f"{cid}: {crows.get(cid)} vs {ref_v:.1f}")
        out["F12_CHORD_AGREE"] = {"ok": not bad, "bad": bad[:4]}

        # F13 - reported deflections
        drows = {r["direction"]: r["defl"] for r in sub.drift if r["scenario"] == sid}
        bad = []
        for k, ref_v in res["deflection"].items():
            if not _close_mag(drows.get(k), ref_v, rel, T["limits"]["agree_abs_in"]):
                bad.append(f"{k}: {drows.get(k)} vs {ref_v:.3f}")
        out["F13_DRIFT_AGREE"] = {"ok": not bad, "bad": bad[:4]}
        return out


def text_lines(text: str):
    for line in text.splitlines():
        if line.count("|") >= 2:
            yield line


def _line_id(cell: str) -> str:
    """`Line S`, `line s` and `S` are the same wall line - as the workbook parser already reads
    them."""
    t = re.sub(r"[^A-Z0-9]", "", str(cell or "").upper())
    t = re.sub(r"^(WALL)?(LINE|GRID|AXIS)", "", t)
    for lid in ("N1", "N2", "E2", "S", "E", "W"):
        if t == lid:
            return lid
    return t


def _row_cells(line: str):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _scen_in(cells):
    for cell in cells:
        m = re.search(r"W\s*0*(\d{1,2})\b", cell.upper())
        if m:
            return f"W{int(m.group(1)):02d}"
    return None


def _governing_table(text: str) -> dict:
    """The line -> governing scenario rows of the table specification 1.6 defines."""
    labelled, current, out = {}, None, {}
    for line in text.splitlines():
        if line.count("|") < 2:
            current = None
            continue
        cells = _row_cells(line)
        joined = " ".join(cells).lower()
        if "governing" in joined and _line_id(cells[0]) not in LINE_IDS:
            current = labelled                       # this table is the labelled one
            continue
        if set(joined.replace("|", "").strip()) <= set("-: "):
            continue                                  # a markdown rule row
        lid = _line_id(cells[0])
        if lid in LINE_IDS:
            sc = _scen_in(cells[1:])
            if sc:
                if current is labelled:
                    labelled.setdefault(lid, set()).add(sc)
                else:
                    out.setdefault(lid, set()).add(sc)
        else:
            current = None
    # Specification 1.6: ONE row per wall line. A line listed against several different
    # scenarios has not been answered - it has been shotgunned. The rule holds wherever the
    # rows were found, under a labelled or an unlabelled header.
    
    if labelled:
        return {lid: sc for lid, sc in labelled.items() if len(sc) == 1}
    # Unlabelled: consider every contiguous block of line-keyed rows and take the one that
    # reads as a governing table - the most wall lines answered exactly once.
    
    
    blocks, cur = [], {}
    for line in text.splitlines():
        if line.count("|") < 2:
            if cur:
                blocks.append(cur)
                cur = {}
            continue
        cells = _row_cells(line)
        lid = _line_id(cells[0])
        if lid in LINE_IDS:
            sc = _scen_in(cells[1:])
            if sc:
                cur.setdefault(lid, set()).add(sc)
        elif cur:
            blocks.append(cur)
            cur = {}
    if cur:
        blocks.append(cur)
    scored = [{lid: sc for lid, sc in b.items() if len(sc) == 1} for b in blocks]
    return max(scored, key=len) if scored else {}
