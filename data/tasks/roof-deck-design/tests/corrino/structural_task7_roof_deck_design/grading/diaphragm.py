"""Semi-rigid roof-diaphragm analysis.

The roof is a plane assembly of shear panels (the steel deck), axial bars (the joists, the joist
girders and the perimeter edge member) and discrete springs (the braced frames).  The deck's shear
rigidity G' is a *design* quantity: it comes from the gage, the support-fastener pattern and the
sidelap count the candidate chose for the zone the panel lies in.  The distribution of the roof force
between the eight braced frames therefore depends on the deck design, and the deck design has to be
found against the shear field it itself produces.  That is the coupling the task is built on.

Units inside: kips and inches.

Element
-------
Each finite element spans one joist bay in x (6 ft) and one girder bay in y (30 ft) and carries pure
shear with rigidity Gt = G' (kip/in):

    gamma = B u,   q = Gt gamma,   K = Gt A B^T B

with B the centroidal shear-strain operator of the bilinear quadrilateral.  Inside the element the
shear flow q is constant, so the five 6 ft deck strips stacked across its depth act in series: the
element's effective G' is the height-weighted harmonic mean of the strips, and its shear capacity is
the smallest strip capacity.
"""
from __future__ import annotations

import numpy as np

from . import loads as LD

REG_SPRING = 1.0e-3        # kip/in on every free degree of freedom (numerical regularisation)
REG_SHARE_LIMIT = 0.02     # a scenario has no load path if the springs carry more than this


def _node_index(nx_nodes, ny_nodes):
    def idx(i, j):
        return j * nx_nodes + i
    return idx


class Diaphragm:
    """The finite-element mesh of the roof plane, independent of any deck design."""

    def __init__(self, truth):
        self.truth = truth
        xs, ys = LD.fe_grid(truth)
        self.xs_ft, self.ys_ft = xs, ys
        self.xs = xs * 12.0
        self.ys = ys * 12.0
        self.nxn, self.nyn = len(xs), len(ys)
        self.nel_x, self.nel_y = self.nxn - 1, self.nyn - 1
        self.ndof = 2 * self.nxn * self.nyn
        self.idx = _node_index(self.nxn, self.nyn)
        self._build_static()

    # -- mesh ------------------------------------------------------------------
    def _elem_dofs(self, i, j):
        n = [self.idx(i, j), self.idx(i + 1, j), self.idx(i + 1, j + 1), self.idx(i, j + 1)]
        return np.array([d for k in n for d in (2 * k, 2 * k + 1)])

    def _shear_B(self, i, j):
        a = self.xs[i + 1] - self.xs[i]
        b = self.ys[j + 1] - self.ys[j]
        return np.array([-1 / (2 * b), -1 / (2 * a),
                         -1 / (2 * b), 1 / (2 * a),
                         1 / (2 * b), 1 / (2 * a),
                         1 / (2 * b), -1 / (2 * a)]), a * b

    def _build_static(self):
        """Assemble everything that does not depend on the deck design: bars, frame springs."""
        f = self.truth["framing"]
        K = np.zeros((self.ndof, self.ndof))
        bars = []       # (dof_i, dof_j, k, tag) - axial bars, for chord/collector force recovery
        # joists: y-direction bars on every joist line
        for i in range(self.nxn):
            edge = i in (0, self.nxn - 1)
            EA = f["joist_EA_kip"] + (f["edge_EA_kip"] if edge else 0.0)
            for j in range(self.nyn - 1):
                L = self.ys[j + 1] - self.ys[j]
                k = EA / L
                di, dj = 2 * self.idx(i, j) + 1, 2 * self.idx(i, j + 1) + 1
                bars.append((di, dj, k, f"JY-{i}-{j}", "Y", i, j))
        # joist girders: x-direction bars on every girder line
        for j in range(self.nyn):
            edge = j in (0, self.nyn - 1)
            EA = f["girder_EA_kip"] + (f["edge_EA_kip"] if edge else 0.0)
            for i in range(self.nxn - 1):
                L = self.xs[i + 1] - self.xs[i]
                k = EA / L
                di, dj = 2 * self.idx(i, j), 2 * self.idx(i + 1, j)
                bars.append((di, dj, k, f"GX-{i}-{j}", "X", i, j))
        for di, dj, k, *_ in bars:
            K[di, di] += k
            K[dj, dj] += k
            K[di, dj] -= k
            K[dj, di] -= k
        self.bars = bars
        # braced frames: half the frame stiffness at each of its two column lines
        self.frame_dofs = {}
        for fr in self.truth["frames"]:
            if fr["dir"] == "X":
                j = int(np.argmin(np.abs(self.ys_ft - fr["line"])))
                nodes = [self.idx(int(np.argmin(np.abs(self.xs_ft - c))), j) for c in (fr["lo"], fr["hi"])]
                dofs = [2 * n for n in nodes]
            else:
                i = int(np.argmin(np.abs(self.xs_ft - fr["line"])))
                nodes = [self.idx(i, int(np.argmin(np.abs(self.ys_ft - c)))) for c in (fr["lo"], fr["hi"])]
                dofs = [2 * n + 1 for n in nodes]
            for d in dofs:
                K[d, d] += 0.5 * fr["k"]
            self.frame_dofs[fr["tag"]] = (dofs, fr["k"], fr["dir"])
        K[np.diag_indices_from(K)] += REG_SPRING
        self.K_static = K

    # -- deck contribution -----------------------------------------------------
    def element_deck(self, gt_cells, cap_cells):
        """Reduce the 40 x 30 deck-cell fields to the 40 x 6 element fields.

        gt_cells  - G' of each deck cell, kip/in (0 where there is no deck)
        cap_cells - factored shear capacity of each deck cell, plf
        """
        ncy = int(round((self.ys_ft[1] - self.ys_ft[0]) / self.truth["geometry"]["zone_grid_y_ft"]))
        g = gt_cells.reshape(self.nel_x, self.nel_y, ncy)
        c = cap_cells.reshape(self.nel_x, self.nel_y, ncy)
        with np.errstate(divide="ignore"):
            inv = np.where(g > 1e-9, 1.0 / np.maximum(g, 1e-9), np.inf)
        gt_el = np.where(np.isinf(inv).any(axis=2), 0.0, ncy / np.where(np.isinf(inv), 1.0, inv).sum(axis=2))
        cap_el = c.min(axis=2)
        return gt_el, cap_el

    def assemble(self, gt_el):
        K = self.K_static.copy()
        self._Bcache = {}
        for i in range(self.nel_x):
            for j in range(self.nel_y):
                gt = gt_el[i, j]
                if gt <= 1e-9:
                    continue
                B, A = self._shear_B(i, j)
                d = self._elem_dofs(i, j)
                ke = gt * A * np.outer(B, B)
                K[np.ix_(d, d)] += ke
        return K

    # -- loading ---------------------------------------------------------------
    def load_vector(self, direction, total_kip, ecc_ft, pattern):
        """Nodal forces for a roof force `total_kip` in `direction`, with plan eccentricity `ecc_ft`.

        pattern 'mass' spreads the force over the plan by tributary area (an inertial force);
        pattern 'edge' puts half on the windward wall line and half on the leeward wall line - the
        two walls whose plane is perpendicular to the direction of the force.
        """
        g = self.truth["geometry"]
        F = np.zeros(self.ndof)
        comp = 0 if direction == "X" else 1
        if pattern == "mass":
            w = np.zeros((self.nxn, self.nyn))
            for i in range(self.nxn):
                fx = 0.5 if i in (0, self.nxn - 1) else 1.0
                for j in range(self.nyn):
                    fy = 0.5 if j in (0, self.nyn - 1) else 1.0
                    w[i, j] = fx * fy
            w /= w.sum()
        else:
            # the wind pressure enters the roof along the two walls whose plane is perpendicular to
            # the load: the west and east walls (x = 0, x = Lx) for a force along x, the south and
            # north walls (y = 0, y = Ly) for a force along y.
            w = np.zeros((self.nxn, self.nyn))
            if direction == "X":
                for i in (0, self.nxn - 1):
                    for j in range(self.nyn):
                        w[i, j] = 0.5 if j in (0, self.nyn - 1) else 1.0
            else:
                for j in (0, self.nyn - 1):
                    for i in range(self.nxn):
                        w[i, j] = 0.5 if i in (0, self.nxn - 1) else 1.0
            w /= w.sum()
        for i in range(self.nxn):
            for j in range(self.nyn):
                F[2 * self.idx(i, j) + comp] += total_kip * w[i, j]
        # accidental / Case-2 torsion: a couple of magnitude total_kip * ecc about the plan centroid
        if abs(ecc_ft) > 1e-9:
            M = total_kip * ecc_ft * 12.0                      # kip-in
            cx, cy = 0.5 * g["lx_ft"] * 12.0, 0.5 * g["ly_ft"] * 12.0
            rx = np.array([self.xs[i] - cx for i in range(self.nxn)])
            ry = np.array([self.ys[j] - cy for j in range(self.nyn)])
            J = 0.0
            for i in range(self.nxn):
                for j in range(self.nyn):
                    J += w[i, j] * (rx[i] ** 2 + ry[j] ** 2)
            if J > 0:
                for i in range(self.nxn):
                    for j in range(self.nyn):
                        s = M * w[i, j] / J
                        F[2 * self.idx(i, j)] += -s * ry[j]
                        F[2 * self.idx(i, j) + 1] += s * rx[i]
        return F

    # -- solve -----------------------------------------------------------------
    def solve(self, gt_el, cap_el, direction, total_kip, ecc_ft, pattern, omega=1.0):
        K = self.assemble(gt_el)
        F = self.load_vector(direction, total_kip, ecc_ft, pattern)
        try:
            u = np.linalg.solve(K, F)
        except np.linalg.LinAlgError:
            return None
        if not np.all(np.isfinite(u)):
            return None
        # shear flow per element
        q = np.zeros((self.nel_x, self.nel_y))
        for i in range(self.nel_x):
            for j in range(self.nel_y):
                if gt_el[i, j] <= 1e-9:
                    continue
                B, _ = self._shear_B(i, j)
                d = self._elem_dofs(i, j)
                q[i, j] = gt_el[i, j] * float(B @ u[d])
        v_plf = np.abs(q) * 12000.0
        # bar axial forces (kips, tension positive)
        bar_force = {}
        for di, dj, k, tag, ax, i, j in self.bars:
            bar_force[tag] = k * (u[dj] - u[di])
        # frame reactions
        react = {}
        for tag, (dofs, k, dr) in self.frame_dofs.items():
            react[tag] = sum(0.5 * k * u[d] for d in dofs)
        # regularisation share
        reg = REG_SPRING * u
        comp = 0 if direction == "X" else 1
        applied = np.abs(F[comp::2]).sum()
        reg_share = np.abs(reg[comp::2]).sum() / applied if applied > 0 else 1.0
        # deflections: diaphragm displacement relative to the mean frame-line displacement
        disp = u[comp::2].reshape(self.nyn, self.nxn).T if comp == 0 else u[comp::2].reshape(self.nyn, self.nxn).T
        frame_disp = [abs(sum(u[d] for d in dofs) / len(dofs)) for tag, (dofs, k, dr) in self.frame_dofs.items()
                      if dr == direction]
        avg_frame = float(np.mean(frame_disp)) if frame_disp else 0.0
        max_disp = float(np.abs(u[comp::2]).max())
        return dict(
            v_plf=v_plf, util=np.zeros_like(v_plf), cap_plf=cap_el, bar_force=bar_force,
            reactions=react, reg_share=float(reg_share), max_disp_in=max_disp,
            avg_frame_disp_in=avg_frame, diaphragm_defl_in=max(max_disp - avg_frame, 0.0),
            omega=omega, u=u,
        )

    # -- post-processing --------------------------------------------------------
    def chord_force_kip(self, res, direction):
        """The largest axial force in the chord lines for this load direction."""
        if direction == "X":
            tags = [t for t in res["bar_force"] if t.startswith("JY-")
                    and int(t.split("-")[1]) in (0, self.nxn - 1)]
        else:
            tags = [t for t in res["bar_force"] if t.startswith("GX-")
                    and int(t.split("-")[2]) in (0, self.nyn - 1)]
        return max((abs(res["bar_force"][t]) for t in tags), default=0.0)

    def collector_force_kip(self, res, direction):
        """The largest axial force in a perimeter member inside a collector line of this direction:
        the drag force the frame's collector has to carry."""
        best = 0.0
        for fr in self.truth["frames"]:
            if fr["dir"] != direction:
                continue
            if direction == "X":
                j = int(np.argmin(np.abs(self.ys_ft - fr["line"])))
                for i in range(self.nel_x):
                    x0, x1 = self.xs_ft[i], self.xs_ft[i + 1]
                    if x1 <= fr["lo"] or x0 >= fr["hi"]:
                        continue
                    best = max(best, abs(res["bar_force"].get(f"GX-{i}-{j}", 0.0)))
                # the drag struts one bay each side
                for i in range(self.nel_x):
                    x0, x1 = self.xs_ft[i], self.xs_ft[i + 1]
                    if (fr["lo"] - self.truth["geometry"]["zone_grid_y_ft"] * 5 <= x1 <= fr["lo"]
                            or fr["hi"] <= x0 <= fr["hi"] + self.truth["geometry"]["zone_grid_y_ft"] * 5):
                        best = max(best, abs(res["bar_force"].get(f"GX-{i}-{j}", 0.0)))
            else:
                i = int(np.argmin(np.abs(self.xs_ft - fr["line"])))
                for j in range(self.nel_y):
                    y0, y1 = self.ys_ft[j], self.ys_ft[j + 1]
                    if y1 <= fr["lo"] - 30.0 or y0 >= fr["hi"] + 30.0:
                        continue
                    best = max(best, abs(res["bar_force"].get(f"JY-{i}-{j}", 0.0)))
        return best
