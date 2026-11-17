"""diaphragm.py - DIAPH-MT 1.0, the reference semi-rigid roof-diaphragm model.

The model is the one the project's structural design criteria (EOR-NDDC-A2-S01 section 9)
prescribes, and it is implemented here exactly as written there:

  * a structured mesh of square elements of side H over the roof polygon; an element belongs to
    the diaphragm when its centroid lies inside the roof outline and outside a roof opening;
  * each element is a 4-node orthotropic membrane carrying stress RESULTANTS
        Nx = EAx * eps_x,  Ny = EAy * eps_y,  Nxy = Ghat * gamma            [kip/ft]
    with Ghat = 12 * G' (G' the deck shear stiffness in kip/in) and EAx, EAy the smeared axial
    rigidity of the roof framing of criteria 9.2 (girders east-west, joists north-south);
  * chord and collector members are two-node bars along the lines of the criteria, area A,
    E = 29,000 ksi, connected to the mesh nodes they pass through;
  * each precast pier is a spring attached at the perimeter node nearest its centroid, acting in
    the plane of its wall, with the cantilever stiffness of criteria 5.5;
  * the roof-level wall reactions of criteria 6.4 are applied to the perimeter nodes of each
    wall line as consistent nodal loads, acting along the wall's outward normal.

Everything is in FEET and KIPS.  Displacements come out in feet; the reported quantities are
converted at the boundary (unit shear in plf, deflection in inches).
"""
from __future__ import annotations

import math

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import splu

# 2 x 2 Gauss points on the reference square
_GP = [(-1 / math.sqrt(3), -1 / math.sqrt(3)), (1 / math.sqrt(3), -1 / math.sqrt(3)),
       (1 / math.sqrt(3), 1 / math.sqrt(3)), (-1 / math.sqrt(3), 1 / math.sqrt(3))]


def _point_in_poly(x: float, y: float, poly) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xin:
                inside = not inside
    return inside


class Mesh:
    """The structured diaphragm mesh of criteria 9.1 at one refinement level."""

    def __init__(self, poly, openings, H: float):
        self.H = float(H)
        self.poly = [tuple(p) for p in poly]
        self.openings = [dict(o) for o in openings]
        xs = [p[0] for p in self.poly]
        ys = [p[1] for p in self.poly]
        self.x0, self.x1 = min(xs), max(xs)
        self.y0, self.y1 = min(ys), max(ys)
        self.nx = int(round((self.x1 - self.x0) / H))
        self.ny = int(round((self.y1 - self.y0) / H))
        cells = []
        for j in range(self.ny):
            for i in range(self.nx):
                cx = self.x0 + (i + 0.5) * H
                cy = self.y0 + (j + 0.5) * H
                if not _point_in_poly(cx, cy, self.poly):
                    continue
                if any(o["x0"] < cx < o["x1"] and o["y0"] < cy < o["y1"] for o in self.openings):
                    continue
                cells.append((i, j))
        self.cells = cells
        nodes = {}
        for (i, j) in cells:
            for (a, b) in ((i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1)):
                if (a, b) not in nodes:
                    nodes[(a, b)] = len(nodes)
        self.node_id = nodes
        self.nnode = len(nodes)
        self.xy = np.zeros((self.nnode, 2))
        for (a, b), k in nodes.items():
            self.xy[k] = (self.x0 + a * H, self.y0 + b * H)
        self.elem = np.array([[nodes[(i, j)], nodes[(i + 1, j)],
                               nodes[(i + 1, j + 1)], nodes[(i, j + 1)]] for (i, j) in cells],
                             dtype=int)
        self.area = self.H * self.H

    def node_at(self, x: float, y: float):
        """The mesh node at (x, y), or the nearest one when the point is off-grid."""
        i = int(round((x - self.x0) / self.H))
        j = int(round((y - self.y0) / self.H))
        if (i, j) in self.node_id:
            return self.node_id[(i, j)]
        d = np.hypot(self.xy[:, 0] - x, self.xy[:, 1] - y)
        return int(np.argmin(d))

    def elem_centroids(self):
        return np.array([[self.x0 + (i + 0.5) * self.H, self.y0 + (j + 0.5) * self.H]
                         for (i, j) in self.cells])


def _b_matrix(xi, eta, H):
    """Strain-displacement matrix of the square Q4 element (side H), rows (eps_x, eps_y, gamma)."""
    dN_dxi = np.array([-(1 - eta), (1 - eta), (1 + eta), -(1 + eta)]) / 4.0
    dN_deta = np.array([-(1 - xi), -(1 + xi), (1 + xi), (1 - xi)]) / 4.0
    J = H / 2.0                       # constant Jacobian for a square element
    dN_dx = dN_dxi / J
    dN_dy = dN_deta / J
    B = np.zeros((3, 8))
    for a in range(4):
        B[0, 2 * a] = dN_dx[a]
        B[1, 2 * a + 1] = dN_dy[a]
        B[2, 2 * a] = dN_dy[a]
        B[2, 2 * a + 1] = dN_dx[a]
    return B


class Model:
    """Assembles and solves DIAPH-MT for one design (deck stiffness per element, chord areas)."""

    def __init__(self, mesh: Mesh, ghat_elem, ea_x: float, ea_y: float):
        self.mesh = mesh
        self.ghat = np.asarray(ghat_elem, dtype=float)      # kip/ft, per element
        self.ea_x = float(ea_x)
        self.ea_y = float(ea_y)
        self.ndof = 2 * mesh.nnode
        self.rows: list[int] = []
        self.cols: list[int] = []
        self.vals: list[float] = []
        self.bars: list[tuple[int, int, int, float]] = []   # (na, nb, dir, EA/L)
        self.springs: list[tuple[int, int, float]] = []      # (node, dir, k)
        self._assembled = False
        self._assemble_membrane()

    # ---------------------------------------------------------------- assembly
    def _add(self, r, c, v):
        self.rows.append(r)
        self.cols.append(c)
        self.vals.append(v)

    def _assemble_membrane(self):
        H = self.mesh.H
        for e, nodes in enumerate(self.mesh.elem):
            D = np.diag([self.ea_x, self.ea_y, self.ghat[e]])
            Ke = np.zeros((8, 8))
            for (xi, eta) in _GP:
                B = _b_matrix(xi, eta, H)
                Ke += B.T @ D @ B * (H * H / 4.0)
            dofs = np.empty(8, dtype=int)
            for a in range(4):
                dofs[2 * a] = 2 * nodes[a]
                dofs[2 * a + 1] = 2 * nodes[a] + 1
            for a in range(8):
                for b in range(8):
                    if Ke[a, b] != 0.0:
                        self._add(int(dofs[a]), int(dofs[b]), float(Ke[a, b]))

    def add_bar(self, na: int, nb: int, direction: int, ea_over_l: float):
        """A chord/collector bar between two adjacent nodes; direction 0 = X, 1 = Y."""
        da, db = 2 * na + direction, 2 * nb + direction
        self._add(da, da, ea_over_l)
        self._add(db, db, ea_over_l)
        self._add(da, db, -ea_over_l)
        self._add(db, da, -ea_over_l)
        self.bars.append((na, nb, direction, ea_over_l))

    def add_spring(self, node: int, direction: int, k: float):
        d = 2 * node + direction
        self._add(d, d, k)
        self.springs.append((node, direction, k))

    def factorize(self):
        K = coo_matrix((self.vals, (self.rows, self.cols)),
                       shape=(self.ndof, self.ndof)).tocsc()
        self.K = K
        self.lu = splu(K)
        self._assembled = True

    # ---------------------------------------------------------------- solve
    def solve(self, f: np.ndarray) -> np.ndarray:
        if not self._assembled:
            self.factorize()
        return self.lu.solve(f)

    # ---------------------------------------------------------------- recovery
    def element_shear(self, u: np.ndarray) -> np.ndarray:
        """Shear flow at each element centre, kip/ft (positive = +gamma)."""
        H = self.mesh.H
        B = _b_matrix(0.0, 0.0, H)
        out = np.zeros(len(self.mesh.elem))
        for e, nodes in enumerate(self.mesh.elem):
            ue = np.empty(8)
            for a in range(4):
                ue[2 * a] = u[2 * nodes[a]]
                ue[2 * a + 1] = u[2 * nodes[a] + 1]
            out[e] = self.ghat[e] * float(B[2] @ ue)
        return out

    def bar_forces(self, u: np.ndarray) -> np.ndarray:
        """Axial force in every bar, kips (tension positive)."""
        out = np.zeros(len(self.bars))
        for b, (na, nb, d, eal) in enumerate(self.bars):
            out[b] = eal * (u[2 * nb + d] - u[2 * na + d])
        return out

    def spring_reactions(self, u: np.ndarray) -> np.ndarray:
        return np.array([k * u[2 * n + d] for (n, d, k) in self.springs])
