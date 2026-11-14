"""Unit convention and access-floor tile arithmetic for dcgen.

**The convention, and why it exists.**

dcgen works in *millimetres* everywhere. Drawings are dimensioned in mm and every
TIA-942 threshold is quoted in mm, so mm is the only representation that never
needs rounding at a graded boundary.

IfcOpenShell is not internally consistent about this, and the inconsistency is
silent:

- Raw geometry entities (``IfcCartesianPoint``, ``IfcExtrudedAreaSolid.Depth``)
  are written in **project file units** — mm, given our unit assignment.
- ``ifcopenshell.api geometry.edit_object_placement`` takes its matrix in
  **metres** and converts to file units internally.

Passing mm to a placement therefore puts an element 1000x too far away, and
nothing errors: the file is valid, the geometry is valid, and the hall is a
kilometre wide. Verified empirically — passing ``(1.2, 3.0, 0)`` yields file
coordinates ``[1200, 3000, 0]``.

So: never call ``edit_object_placement`` directly. Use
:func:`mm_matrix_to_ifc`, or better, ``IfcBuilder.place``.
"""
from __future__ import annotations

import string
from dataclasses import dataclass

import numpy as np

#: IfcOpenShell placement matrices are metres; everything else here is mm.
MM_PER_M = 1000.0


def mm_to_m(value_mm: float) -> float:
    """Convert a millimetre scalar to metres (for IFC placement matrices)."""
    return value_mm / MM_PER_M


def mm_matrix_to_ifc(matrix_mm: np.ndarray) -> np.ndarray:
    """Convert a 4x4 transform whose translation is in mm into IFC placement form.

    Only the translation column is scaled; the rotation block is dimensionless.
    """
    out = np.array(matrix_mm, dtype=float, copy=True)
    out[:3, 3] = out[:3, 3] / MM_PER_M
    return out


def translation_mm(x_mm: float, y_mm: float, z_mm: float = 0.0) -> np.ndarray:
    """A pure-translation 4x4 in mm."""
    m = np.eye(4)
    m[:3, 3] = (float(x_mm), float(y_mm), float(z_mm))
    return m


def rotation_z(degrees: float) -> np.ndarray:
    """Rotation about +Z. Racks face one of four cardinal directions."""
    r = np.radians(float(degrees))
    c, s = np.cos(r), np.sin(r)
    m = np.eye(4)
    m[0, 0], m[0, 1] = c, -s
    m[1, 0], m[1, 1] = s, c
    return m


#: Cardinal facing directions, as stored in Pset_DCM_Rack.FacingDirection.
#: The angle is the rotation about +Z that points the cabinet FRONT that way.
#:
#: The quarter turns are 270/90, not 90/270, and that is not a typo.
#: :func:`rotation_z` is the standard right-handed rotation, so it carries an
#: element's local +Y — the face that points NORTH unrotated, i.e. the front —
#: to ``(-sin, cos)``. At +90 degrees that is **-X, which is WEST**. Reading
#: EAST as +90 would put this table in direct contradiction with every consumer of a
#: facing: :func:`facing_normal` below, ``rules._FRONT_AXIS``,
#: ``program.clearance_zones`` and ``build.working_space`` all place an
#: EAST-facing unit's front at +X, and they are the ones that are right. Only
#: N/S is generated today, so the disagreement would stay hidden — but
#: the grader recovers facing from the placement matrix alone, so the first
#: east-west unit would have its service clearance checked on its back.
FACING_ANGLES = {"NORTH": 0.0, "EAST": 270.0, "SOUTH": 180.0, "WEST": 90.0}
OPPOSITE_FACING = {"NORTH": "SOUTH", "SOUTH": "NORTH", "EAST": "WEST", "WEST": "EAST"}


def facing_normal(facing: str) -> np.ndarray:
    """Unit vector (mm-space, XY) that the cabinet front points along."""
    return {
        "NORTH": np.array([0.0, 1.0]),
        "SOUTH": np.array([0.0, -1.0]),
        "EAST": np.array([1.0, 0.0]),
        "WEST": np.array([-1.0, 0.0]),
    }[facing]


@dataclass(frozen=True)
class TileGrid:
    """The access-floor tile grid.

    TIA-942 5.11.4 requires equipment to be placed relative to the tile grid.
    The grid origin is deliberately allowed to be offset from the structural
    column grid — that offset is one of the generator's difficulty knobs, since
    racks then cannot align to both.
    """

    pitch_mm: float = 600.0
    origin_x_mm: float = 0.0
    origin_y_mm: float = 0.0

    def snap(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        """Snap a point to the nearest tile corner."""
        gx = round((x_mm - self.origin_x_mm) / self.pitch_mm) * self.pitch_mm + self.origin_x_mm
        gy = round((y_mm - self.origin_y_mm) / self.pitch_mm) * self.pitch_mm + self.origin_y_mm
        return gx, gy

    def is_on_grid(self, x_mm: float, y_mm: float, tol_mm: float = 1.0) -> bool:
        gx, gy = self.snap(x_mm, y_mm)
        return abs(gx - x_mm) <= tol_mm and abs(gy - y_mm) <= tol_mm

    def indices(self, x_mm: float, y_mm: float) -> tuple[int, int]:
        """Integer tile indices (col, row) containing a point."""
        col = int(np.floor((x_mm - self.origin_x_mm) / self.pitch_mm))
        row = int(np.floor((y_mm - self.origin_y_mm) / self.pitch_mm))
        return col, row

    def label(self, x_mm: float, y_mm: float) -> str:
        """Data-center tile coordinate, e.g. ``AA07``.

        Columns are lettered A..Z then AA..AZ (the convention used on real
        floor-tile plans); rows are 1-based integers.
        """
        col, row = self.indices(x_mm, y_mm)
        return f"{_col_letters(col)}{row + 1:02d}"

    def origin_of(self, col: int, row: int) -> tuple[float, float]:
        """Min-corner of tile (col, row)."""
        return (
            self.origin_x_mm + col * self.pitch_mm,
            self.origin_y_mm + row * self.pitch_mm,
        )


def occupied_aabb(x_mm: float, y_mm: float, width_mm: float, depth_mm: float,
                  facing: str = "NORTH") -> tuple[float, float, float, float]:
    """Plan AABB an element actually occupies, accounting for its facing.

    Elements are rotated about their footprint CENTRE, so a 90/270-degree
    facing swaps width and depth about that centre. Every collision, clearance
    and containment check must use this rather than the naive
    ``(x, y, x+w, y+d)``, which is only correct for NORTH/SOUTH.

    Today the generator emits N/S rows only, so the naive form happened to be
    right — but ``FACING_ANGLES`` supports E/W and the first east-west row or
    side-mounted in-row CRAH would silently defeat every gate.
    """
    cx = x_mm + width_mm / 2.0
    cy = y_mm + depth_mm / 2.0
    if facing in ("EAST", "WEST"):
        half_w, half_d = depth_mm / 2.0, width_mm / 2.0
    else:
        half_w, half_d = width_mm / 2.0, depth_mm / 2.0
    return (cx - half_w, cy - half_d, cx + half_w, cy + half_d)


def point_in_polygon(polygon: list[tuple[float, float]], x: float, y: float) -> bool:
    """Ray-casting containment test, points on the boundary counted as inside.

    Needed because a hall footprint is not always its bounding box: the
    ``l_shaped`` difficulty knob notches a corner out. Anything that places
    elements from ``bounds_mm()`` alone will happily fill the notch with
    equipment that is outside the room.
    """
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def box_in_polygon(polygon: list[tuple[float, float]],
                   x0: float, y0: float, x1: float, y1: float) -> bool:
    """True when all four corners of an axis-aligned box lie inside the polygon.

    Corner testing is sufficient for the rectilinear footprints dcgen produces
    (rectangles and single-notch L shapes); it would not be for a polygon with
    a concave spike narrower than the box.
    """
    return all(
        point_in_polygon(polygon, px, py)
        for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    )


def _col_letters(col: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA. Negative indices are clamped to A."""
    if col < 0:
        return "A"
    letters = ""
    col += 1
    while col > 0:
        col, rem = divmod(col - 1, 26)
        letters = string.ascii_uppercase[rem] + letters
    return letters
