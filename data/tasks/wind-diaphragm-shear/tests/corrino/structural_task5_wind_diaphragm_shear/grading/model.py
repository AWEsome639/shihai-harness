"""model.py - the forward model: wind pressures -> wall reactions -> diaphragm -> design checks.

`truth()` returns the frozen task data (grading/data/truth.json).  `Analysis` takes a `Design`
(what the candidate decided: a deck type per zone, a chord size per chord line, embed counts and
tie-downs per panel) and returns, for each of the twelve wind scenarios, the ASD diaphragm unit
shears, wall-line reactions, chord/collector forces, deflections and pier demands.

All internal arithmetic is strength level (1.0W); every quantity that leaves this module is
multiplied by the ASD factor of criteria 3.3 (0.6W), because that is the basis the submittal
must be reported and checked on.
"""
from __future__ import annotations

import functools
import json
import math
from pathlib import Path

import numpy as np

from .diaphragm import Mesh, Model

HERE = Path(__file__).resolve().parent


@functools.lru_cache(maxsize=1)
def truth() -> dict:
    return json.loads((HERE / "data" / "truth.json").read_text())


# --------------------------------------------------------------------------- piers
def piers(T: dict) -> list[dict]:
    """Every pier (vertical leg) of every precast panel, with its cantilever stiffness.

    Criteria 5.4: a panel's openings divide it into legs; a leg narrower than MIN_LEG is
    neglected.  Criteria 5.5: each leg is a cantilever of height h over the full panel height.
    """
    out = []
    h_in = T["geometry"]["roof_z"] * 12.0
    E = T["materials"]["Ec_ksi"]
    G = E * T["materials"]["G_over_E"]
    for line_id, panels in T["panels"].items():
        for p in panels:
            edges = [p["s0"], p["s1"]]
            for o in p["openings"]:
                edges += [o[0], o[1]]
            edges = sorted(edges)
            segs = []
            for a, b in zip(edges, edges[1:]):
                mid = 0.5 * (a + b)
                if any(o[0] < mid < o[1] for o in p["openings"]):
                    continue
                segs.append((a, b))
            for k, (a, b) in enumerate(segs):
                w = b - a
                if w < T["limits"]["min_leg_ft"] - 1e-9:
                    continue
                L_in = w * 12.0
                t_in = float(p["t_in"])
                I = t_in * L_in ** 3 / 12.0
                A = t_in * L_in
                flex = h_in ** 3 / (3.0 * E * I)
                shear = 1.2 * h_in / (G * A)
                k_in = 1.0 / (flex + shear)              # kip/in
                top = p.get("top_z", T["geometry"]["roof_z"] + T["geometry"]["parapet_typ"])
                weight = (t_in / 12.0) * w * top * T["materials"]["panel_density_pcf"] / 1000.0
                out.append({"line": line_id, "panel": p["mark"],
                            "pier": f"{p['mark']}-{k + 1}" if len(segs) > 1 else p["mark"],
                            "s0": a, "s1": b, "width": w, "t_in": t_in,
                            "k_kip_per_in": k_in, "weight_kips": weight, "top_z": top})
    return out


# --------------------------------------------------------------------------- wind
def q_at(T: dict, z: float) -> float:
    W = T["wind"]
    zz = max(z, 15.0)
    Kz = 2.01 * (zz / W["zg_ft"]) ** (2.0 / W["alpha"])
    return W["q_coeff"] * Kz * W["Kzt"] * W["Kd"] * W["Ke"] * W["V_mph"] ** 2


def line_loads(T: dict, az: int) -> dict:
    """Roof-level line load w(s) for every wall line under a Case 1 wind from `az`.

    Criteria 6.4: the panels span vertically as simple beams between the foundation and the roof
    deck, so a patch pressure p acting over an area A whose centroid is at z delivers
    p*A*z/h to the roof deck.  Patches above the deck (parapet and screen wall) therefore
    deliver MORE than their own force - the lever action of the cantilever above the deck.

    Returns {line_id: [(s0, s1, w_kip_per_ft), ...]} at strength level, positive INWARD.
    """
    h = T["geometry"]["roof_z"]
    qh = T["wind"]["q_h"]
    out: dict[str, list] = {}
    for line in T["wall_lines"]:
        segs: dict[tuple, float] = {}
        for patch in T["patches"][line["id"]]:
            cp = T["cp"][str(az)][patch["id"]]
            zc = 0.5 * (patch["z0"] + patch["z1"])
            dz = patch["z1"] - patch["z0"]
            q = qh if patch["kind"] == "wall" else q_at(T, patch["z1"])
            p = q * cp / 1000.0                      # kips per square foot
            key = (patch["s0"], patch["s1"])
            segs[key] = segs.get(key, 0.0) + p * dz * zc / h
        out[line["id"]] = [(a, b, w) for (a, b), w in sorted(segs.items())]
    return out


# --------------------------------------------------------------------------- design
class Design:
    """What the candidate decided."""

    def __init__(self, deck: dict, chords: dict, embeds: dict, tiedowns):
        self.deck = dict(deck)                 # zone_id -> deck code
        self.chords = dict(chords)             # chord line id -> chord code
        self.embeds = dict(embeds)             # panel mark -> number of embed connections
        self.tiedowns = set(tiedowns)          # panel marks with a base tie-down assembly

    def to_json(self) -> dict:
        return {"deck": self.deck, "chords": self.chords, "embeds": self.embeds,
                "tiedowns": sorted(self.tiedowns)}


def deck_by_code(T: dict) -> dict:
    return {d["code"]: d for d in T["deck_catalogue"]}


def chord_by_code(T: dict) -> dict:
    return {c["code"]: c for c in T["chord_catalogue"]}


# --------------------------------------------------------------------------- analysis
class Analysis:
    def __init__(self, T: dict, design: Design, level: str | None = None):
        self.T = T
        self.d = design
        self.level = level or T["fe"]["grade_level"]
        self.H = T["fe"]["levels"][self.level]
        g = T["geometry"]
        self.mesh = Mesh(g["footprint"], [g["roof_opening"]], self.H)
        self.piers = piers(T)
        self._build()

    # ------------------------------------------------------------------ build
    def _zone_of(self, x, y):
        for z in self.T["zones"]:
            if z["x0"] <= x <= z["x1"] and z["y0"] <= y <= z["y1"]:
                return z["id"]
        return None

    def _build(self):
        T, dk = self.T, deck_by_code(self.T)
        cents = self.mesh.elem_centroids()
        ghat = np.zeros(len(cents))
        self.cap_plf = np.zeros(len(cents))
        self.elem_zone = []
        mech = T["geometry"]["mech_zone"]
        for e, (x, y) in enumerate(cents):
            zid = self._zone_of(x, y)
            self.elem_zone.append(zid)
            code = self.d.deck.get(zid)
            d = dk.get(code)
            if d is None:                       # unassigned or invalid zone -> no deck
                ghat[e] = 1e-3
                self.cap_plf[e] = 0.0
                continue
            f = T["deck"]["span_factor_4ft"] if (mech["x0"] <= x <= mech["x1"]
                                                 and mech["y0"] <= y <= mech["y1"]) else 1.0
            ghat[e] = 12.0 * d["G_kip_per_in"] * f
            self.cap_plf[e] = d["Sn_plf"] / T["deck"]["omega_d"]
        self.model = Model(self.mesh, ghat, T["fe"]["EA_x_kip_per_ft"], T["fe"]["EA_y_kip_per_ft"])

        # chord and collector bars
        ch = chord_by_code(T)
        self.bar_line = []
        self.bar_allow = []
        for cl in T["chord_lines"]:
            code = self.d.chords.get(cl["id"])
            c = ch.get(code)
            A = c["area_in2"] if c else 1e-4
            eal = T["materials"]["Es_ksi"] * A / self.H
            direction = 0 if cl["axis"] == "X" else 1
            s = cl["s0"]
            while s < cl["s1"] - 1e-9:
                if cl["axis"] == "X":
                    na = self.mesh.node_at(s, cl["const"])
                    nb = self.mesh.node_at(s + self.H, cl["const"])
                else:
                    na = self.mesh.node_at(cl["const"], s)
                    nb = self.mesh.node_at(cl["const"], s + self.H)
                self.model.add_bar(na, nb, direction, eal)
                self.bar_line.append(cl["id"])
                self.bar_allow.append((c["allow_t_kips"], c["allow_c_kips"]) if c else (0.0, 0.0))
                s += self.H
        self.bar_line = np.array(self.bar_line)

        # pier springs
        lines = {l["id"]: l for l in T["wall_lines"]}
        self.spring_pier = []
        for p in self.piers:
            line = lines[p["line"]]
            sc = 0.5 * (p["s0"] + p["s1"])
            if line["axis"] == "X":
                node = self.mesh.node_at(sc, line["const"])
                direction = 0
            else:
                node = self.mesh.node_at(line["const"], sc)
                direction = 1
            self.model.add_spring(node, direction, 12.0 * p["k_kip_per_in"])
            self.spring_pier.append(p)
        self.model.factorize()

        # nodal tributary areas (for the torsional load case)
        self.node_area = np.zeros(self.mesh.nnode)
        for nodes in self.mesh.elem:
            for n in nodes:
                self.node_area[n] += self.mesh.area / 4.0
        tot = self.node_area.sum()
        self.centroid = (float((self.mesh.xy[:, 0] * self.node_area).sum() / tot),
                         float((self.mesh.xy[:, 1] * self.node_area).sum() / tot))

    # ------------------------------------------------------------------ loads
    def _nodal_from_line(self, line, segs, f):
        """Consistent nodal loads on the perimeter nodes of a wall line (criteria 9.3)."""
        H = self.H
        s0, s1 = line["s0"], line["s1"]
        nodes = []
        s = s0
        while s <= s1 + 1e-9:
            if line["axis"] == "X":
                nodes.append((s, self.mesh.node_at(s, line["const"])))
            else:
                nodes.append((s, self.mesh.node_at(line["const"], s)))
            s += H
        inward = (-line["normal"][0], -line["normal"][1])
        for k, (sk, nk) in enumerate(nodes):
            lo = nodes[k - 1][0] if k > 0 else sk
            hi = nodes[k + 1][0] if k + 1 < len(nodes) else sk
            total = 0.0
            for (a, b, w) in segs:
                # integral of w * hat_k over [a, b]
                for (p0, p1, peak_at) in ((lo, sk, sk), (sk, hi, sk)):
                    if p1 <= p0:
                        continue
                    aa, bb = max(a, p0), min(b, p1)
                    if bb <= aa:
                        continue
                    # hat is linear from 0 at the far end to 1 at sk
                    far = p0 if p1 == sk else p1
                    span = abs(sk - far)
                    if span < 1e-12:
                        continue
                    h0 = 1.0 - abs(aa - sk) / span
                    h1 = 1.0 - abs(bb - sk) / span
                    total += w * 0.5 * (h0 + h1) * (bb - aa)
            f[2 * nk] += total * inward[0]
            f[2 * nk + 1] += total * inward[1]

    def load_vector(self, scenario: dict) -> np.ndarray:
        T = self.T
        f = np.zeros(self.model.ndof)
        segs = line_loads(T, scenario["az"])
        lines = {l["id"]: l for l in T["wall_lines"]}
        for lid, sg in segs.items():
            self._nodal_from_line(lines[lid], sg, f)
        if scenario["case"] == 2:
            factor = T["wind"]["case2_factor"]
            f *= factor
            fx = float(f[0::2].sum())
            fy = float(f[1::2].sum())
            if abs(scenario["az"] % 180) < 1e-9:          # wind on the N/S faces: load in Y
                F, B = abs(fy), T["geometry"]["B_for_y_load"]
            else:
                F, B = abs(fx), T["geometry"]["B_for_x_load"]
            M_T = scenario["ecc"] * B * F
            cx, cy = self.centroid
            dx = self.mesh.xy[:, 0] - cx
            dy = self.mesh.xy[:, 1] - cy
            tx = -dy * self.node_area
            ty = dx * self.node_area
            m = float((dx * ty - dy * tx).sum())
            alpha = M_T / m if abs(m) > 1e-9 else 0.0
            f[0::2] += alpha * tx
            f[1::2] += alpha * ty
        return f

    # ------------------------------------------------------------------ solve
    def solve_all(self) -> dict:
        out = {}
        for sc in self.T["scenarios"]:
            f = self.load_vector(sc)
            u = self.model.solve(f)
            out[sc["id"]] = {"u": u, "f": f}
        return out

    # ------------------------------------------------------------------ recovery (ASD)
    def results(self, sc_id: str, u: np.ndarray) -> dict:
        T = self.T
        asd = T["wind"]["asd_factor"]
        q = self.model.element_shear(u) * asd                     # kip/ft
        bars = self.model.bar_forces(u) * asd                     # kips
        reac = self.model.spring_reactions(u) * asd               # kips
        cents = self.mesh.elem_centroids()

        # unit shear at the report stations of each wall line
        stations = {}
        for line in T["wall_lines"]:
            for frac in T["report_fractions"]:
                s = line["s0"] + frac * (line["s1"] - line["s0"])
                best, bq = None, 0.0
                for e, (x, y) in enumerate(cents):
                    if line["axis"] == "X":
                        if abs(abs(y - line["const"]) - self.H / 2.0) > 1e-6:
                            continue
                        d = abs(x - s)
                    else:
                        if abs(abs(x - line["const"]) - self.H / 2.0) > 1e-6:
                            continue
                        d = abs(y - s)
                    if best is None or d < best:
                        best, bq = d, q[e]
                stations[f"{line['id']}@{frac:.2f}"] = abs(bq) * 1000.0    # plf

        # wall line reactions and pier demands
        line_reac: dict[str, float] = {}
        pier_demand: dict[str, float] = {}
        pier_drift: dict[str, float] = {}
        for i, p in enumerate(self.piers):
            line_reac[p["line"]] = line_reac.get(p["line"], 0.0) + reac[i]
            pier_demand[p["pier"]] = reac[i]
            node, direction, k = self.model.springs[i]
            pier_drift[p["pier"]] = abs(u[2 * node + direction]) * 12.0 * asd   # inches

        # chord / collector forces
        chord_force = {}
        for cl in T["chord_lines"]:
            m = self.bar_line == cl["id"]
            chord_force[cl["id"]] = float(np.max(np.abs(bars[m]))) if m.any() else 0.0
        chord_signed = {}
        for cl in T["chord_lines"]:
            m = self.bar_line == cl["id"]
            if m.any():
                bb = bars[m]
                chord_signed[cl["id"]] = (float(bb.max()), float(bb.min()))
            else:
                chord_signed[cl["id"]] = (0.0, 0.0)

        # diaphragm deflection relative to the supports, inches, per global direction
        defl = {}
        for direction, key in ((0, "X"), (1, "Y")):
            ks = np.array([k for (n, d, k) in self.model.springs if d == direction])
            us = np.array([u[2 * n + d] for (n, d, k) in self.model.springs if d == direction])
            if len(ks) == 0:
                defl[key] = 0.0
                continue
            ubar = float((ks * us).sum() / ks.sum())
            defl[key] = float(np.max(np.abs(u[direction::2] - ubar))) * 12.0 * asd
        # roof-level ASD line load per wall line, plf (criteria 6.4), with the case factor
        sc = next(s for s in T["scenarios"] if s["id"] == sc_id)
        case_f = T["wind"]["case2_factor"] if sc["case"] == 2 else 1.0
        segs = line_loads(T, sc["az"])
        line_load = {}
        for lid, sg in segs.items():
            L = sum(b - a for a, b, _ in sg)
            tot = sum(w * (b - a) for a, b, w in sg)
            line_load[lid] = abs(tot / L) * 1000.0 * asd * case_f if L > 0 else 0.0

        return {"q": q, "bars": bars, "reac": reac, "stations": stations,
                "line_load": line_load,
                "line_reac": {k: abs(v) for k, v in line_reac.items()},
                "line_reac_signed": line_reac,
                "pier_demand": {k: abs(v) for k, v in pier_demand.items()},
                "pier_drift": pier_drift, "chord_force": chord_force,
                "chord_signed": chord_signed, "deflection": defl,
                "max_unit_shear_plf": float(np.max(np.abs(q))) * 1000.0}

    def analyse(self) -> dict:
        U = self.solve_all()
        return {sid: self.results(sid, U[sid]["u"]) for sid in U}

    # ------------------------------------------------------------------ line load report
    def roof_line_load(self, az: int) -> dict:
        """ASD roof-level line load per wall line, plf, averaged over the line (criteria 6.4)."""
        asd = self.T["wind"]["asd_factor"]
        segs = line_loads(self.T, az)
        out = {}
        for lid, sg in segs.items():
            L = sum(b - a for a, b, _ in sg)
            tot = sum(w * (b - a) for a, b, w in sg)
            out[lid] = tot / L * 1000.0 * asd if L > 0 else 0.0
        return out
