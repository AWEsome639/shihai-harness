"""Assemble a :class:`HallProgram` into a complete ground-truth IFC4 model.

This is the generator's top level. Whatever it emits *is* the answer key, so it
must satisfy every graded threshold by construction — :func:`self_check` asserts
that before the file is written, and a failure means the seed is unusable rather
than that the agent is wrong.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import specs
from .elements import Column, Containment, Crah, Pdu, Rack, RouteSegment, Sprinkler
from .ifc_writer import ElementSpec, IfcBuilder
from .program import (
    HallProgram,
    aisle_bands,
    bay_origins,
    clearance_zones,
    _aabb_overlap,
    column_footprints,
    column_positions,
    fits_in_hall,
    layout_racks,
    rows_of,
    substitute_in_row_units,
)
from .route import OracleInfeasible, RoutePlan, plan_route
from .units import box_in_polygon, occupied_aabb, point_in_polygon


#: Access-floor panel thickness. Panels sit on pedestals with their top face at
#: finished floor level; the plenum below stays open for under-floor routing.
ACCESS_FLOOR_PANEL_MM = 35.0

#: Clear gap between the last cabinet of a row and the PDU that feeds it.
PDU_SETBACK_MM = 1800.0

#: Millimetre-scale slack for comparing two positions the generator itself
#: computed. Anything larger is a real disagreement, not float noise.
EPS_MM = 1.0


@dataclass
class BuildResult:
    path: str
    racks: list[Rack]
    crahs: list[Crah]
    columns: list[Column]
    sprinklers: list[Sprinkler]
    problems: list[str]
    pdus: list[Pdu] = field(default_factory=list)
    containment: list[Containment] = field(default_factory=list)
    routes: list[RouteSegment] = field(default_factory=list)
    l_opt_mm: float = 0.0

    @property
    def element_count(self) -> int:
        return (len(self.racks) + len(self.crahs) + len(self.columns)
                + len(self.sprinklers) + len(self.pdus)
                + len(self.containment) + len(self.routes))

    #: The two CRAH classes live in one list because they are one IFC kind and
    #: one recovery group; only the sheets and the CHW gate need to tell them
    #: apart. Filtering properties rather than a second field, so
    #: ``element_count`` and every caller that iterates ``crahs`` keep working.
    @property
    def perimeter_crahs(self) -> list[Crah]:
        return [c for c in self.crahs if c.placement == "perimeter"]

    @property
    def in_row_crahs(self) -> list[Crah]:
        return [c for c in self.crahs if c.placement == "in_row"]


def _blocked_by_column(columns, x: float, y: float, body_mm: float) -> bool:
    """Does the head's BODY overlap a column?

    Testing the centre point alone is wrong in both directions. An inclusive
    point test deletes heads that merely touch a corner; a strict point test
    keeps heads whose body still overlaps the column by half its width -- a
    real clash the grader catches. The head is a solid object, so test its
    actual extent.
    """
    half = body_mm / 2.0
    lo_x, lo_y, hi_x, hi_y = x - half, y - half, x + half, y + half
    return any(not (hi_x <= c[0] or c[2] <= lo_x or hi_y <= c[1] or c[3] <= lo_y)
               for c in columns)


#: Clearance held between a relocated head's body and the column it dodged.
HEAD_RELOCATION_CLEARANCE_MM = 50.0


def _place_head(program: HallProgram, columns, x: float, y: float, body_mm: float):
    """Resolve a grid point to a usable head position, or None.

    A genuinely obstructed head is RELOCATED rather than deleted -- deleting it
    leaves a gap in the coverage grid, which is the opposite of what NFPA 13
    requires. Real design nudges the head off the obstruction; it does not omit
    it.

    The nudge is the **minimum** that clears the column, not a whole floor tile.
    A tile-sized jump is fine at a 3000 mm pitch but not at 2500: it leaves
    one head 3100 mm from its neighbour, which multiplies out to 22.05 m2 of
    protection area against NFPA's 20.9032 m2 cap. Moving only as far as the
    obstruction demands keeps the realised spacing inside the pitch's own
    budget.
    """
    if not point_in_polygon(program.footprint_mm, x, y):
        return None                      # outside the room: genuinely no head here
    if not _blocked_by_column(columns, x, y, body_mm):
        return (x, y)

    half = body_mm / 2.0 + HEAD_RELOCATION_CLEARANCE_MM
    candidates: list[tuple[float, float, float]] = []
    for c in columns:
        for nx, ny in ((c[0] - half, y), (c[2] + half, y),
                       (x, c[1] - half), (x, c[3] + half)):
            candidates.append((abs(nx - x) + abs(ny - y), nx, ny))
    step = program.tile.pitch_mm
    for radius in (1, 2):
        for dx, dy in ((step, 0), (-step, 0), (0, step), (0, -step),
                       (step, step), (-step, -step), (step, -step), (-step, step)):
            nx, ny = x + dx * radius, y + dy * radius
            candidates.append((abs(nx - x) + abs(ny - y), nx, ny))

    for _, nx, ny in sorted(candidates):
        if (point_in_polygon(program.footprint_mm, nx, ny)
                and not _blocked_by_column(columns, nx, ny, body_mm)):
            return (nx, ny)
    return None                          # boxed in; nothing sensible to place


def sprinkler_grid(program: HallProgram) -> list[Sprinkler]:
    """Pendent heads on a regular grid at the ceiling.

    ``z`` is the DEFLECTOR elevation. NFPA 13 10.2.6.1 puts the deflector 1-12 in
    below the ceiling in unobstructed construction; we sit it at the shallow end
    of that window so the head intrudes as little as possible into the plenum,
    which is what makes the TIA-942 460 mm standoff bind against tray routing.
    """
    lo = float(specs.SPRINKLER_DEFLECTOR_MIN_BELOW_CEILING)
    deflector_z = program.ceiling_z_mm - lo
    # the head hangs from the ceiling: its body fills deflector -> ceiling, so
    # it never protrudes above the slab or outside the hall solid
    body = program.ceiling_z_mm - deflector_z

    min_x, min_y, max_x, max_y = program.bounds_mm()
    pitch = program.sprinkler_pitch_mm
    columns = column_footprints(program)
    heads: list[Sprinkler] = []
    n = 0
    y = min_y + pitch / 2.0
    while y < max_y:
        x = min_x + pitch / 2.0
        while x < max_x:
            # A head is a real object: it cannot be inside a column, and it
            # cannot hang in the notch the l_shaped knob removed from the room.
            # Bounds are STRICT -- an inclusive test would delete heads that merely
            # touch a column corner, punching holes in the coverage grid for
            # no reason.
            placed = _place_head(program, columns, x, y, body)
            if placed is not None:
                n += 1
                heads.append(Sprinkler(name=f"SP-{n:03d}", x_mm=placed[0],
                                       y_mm=placed[1], z_mm=deflector_z,
                                       body_mm=body))
            x += pitch
        y += pitch
    return _pull_end_heads_to_walls(program, columns, heads, body)


#: Deflector drop below the containment roof it hangs from. NFPA 13 10.2.6.1's
#: window is 25.4-304.8 mm below the surface above; 110 mm sits mid-window and
#: leaves the head's own body 10 mm clear of the panel, which the clash
#: detector needs — two boxes that merely share a face are fine, but the margin
#: costs nothing and the panel is a kit part with real fixings.
CONTAINMENT_HEAD_DROP_MM = 110.0
CONTAINMENT_HEAD_BODY_MM = 100.0

#: How far in from the end of a roof run the first head sits. The containment
#: doors stand at those ends, full height, and a head has to be clear of one in
#: plan or it is inside the door leaf.
CONTAINMENT_HEAD_INSET_MM = 500.0


def containment_head_line(program: HallProgram,
                          containment: list[Containment],
                          start_index: int) -> list[Sprinkler]:
    """The heads NFPA 13 requires beneath the containment lid.

    NFPA 13 (2022) 10.2.7.4.2: "sprinklers shall be installed under fixed
    obstructions over 4 ft (1.2 m) wide". A cold-aisle containment roof is
    1400 mm over a two-tile aisle and 2000 mm over a three-tile one, so every
    contained aisle needs a head line under it.

    NFPA 75 (2024) 6.7.8.1 does offer a way out: drop-away lid panels released
    by automatic smoke detection, which is what many real halls buy. That is a
    note, not a geometry, and a benchmark whose answer key is a note has not
    modelled anything. The heads are drawn instead.

    Spacing is the hall's own pitch, capped at NFPA's 4600 mm maximum and
    floored at its 1800 mm minimum, and the line is centred on the run so the
    two end gaps are equal.
    """
    pitch = min(program.sprinkler_pitch_mm,
                float(specs.SPRINKLER_MAX_SPACING))
    floor_pitch = float(specs.SPRINKLER_MIN_SPACING)
    out: list[Sprinkler] = []
    n = start_index
    for panel in sorted((c for c in containment if c.part == "roof"),
                        key=lambda c: (c.aisle_id, c.x_mm)):
        x0 = panel.x_mm + CONTAINMENT_HEAD_INSET_MM
        x1 = panel.x_mm + panel.width_mm - CONTAINMENT_HEAD_INSET_MM
        if x1 <= x0:
            continue
        # CEIL, not floor. `int(d // pitch) + 1` counts how many whole
        # pitches fit and adds one, which on a 4700 mm run at a 3100 mm pitch
        # gives two heads 4700 apart — past NFPA 13's 4600 mm cap. What is wanted
        # is enough INTERVALS that none exceeds the pitch.
        
        count = max(1, int(math.ceil((x1 - x0) / pitch)) + 1)
        while count > 1 and (x1 - x0) / (count - 1) < floor_pitch:
            count -= 1
        step = (x1 - x0) / (count - 1) if count > 1 else 0.0
        cy = panel.y_mm + panel.depth_mm / 2.0
        for k in range(count):
            n += 1
            out.append(Sprinkler(
                name=f"SP-{n:03d}",
                x_mm=x0 + k * step if count > 1 else (x0 + x1) / 2.0,
                y_mm=cy,
                z_mm=panel.z_mm - CONTAINMENT_HEAD_DROP_MM,
                body_mm=CONTAINMENT_HEAD_BODY_MM,
            ))
    return out


def _pull_end_heads_to_walls(program: HallProgram, columns,
                             heads: list[Sprinkler], body_mm: float
                             ) -> list[Sprinkler]:
    """Move the last head on each line out to half a pitch from the wall.

    NFPA 13 10.2.5.2.1 caps the distance from a sprinkler to a wall at half the
    allowable spacing, and a grid anchored at ``min + pitch/2`` only satisfies
    that on the walls its phase happens to suit. The ``l_shaped`` knob's
    re-entrant corner is where it fails: a head 2450 mm from the notch wall is
    charged 4900 x 4500 mm = 22.05 m2 by NFPA's own doubling rule, against a
    20.9032 m2 cap.

    Moving a head outward only ever increases the gap to its neighbour, so this
    cannot breach the 1800 mm minimum spacing of 10.2.5.4.1.
    """
    half = program.sprinkler_pitch_mm / 2.0
    band = program.sprinkler_pitch_mm / 2.0
    positions = {h.name: [h.x_mm, h.y_mm] for h in heads}

    for axis in (0, 1):
        other = 1 - axis
        lines: dict[int, list[str]] = {}
        for name, pos in positions.items():
            lines.setdefault(int(round(pos[other] / band)), []).append(name)
        for members in lines.values():
            for sign in (1.0, -1.0):
                end = max(members, key=lambda n: positions[n][axis] * sign)
                pos = positions[end]
                wall = _ray_to_wall(program.footprint_mm, pos[0], pos[1],
                                    axis, sign)
                # only ever a correction, never a relocation. Bounding the move
                # at half a pitch means a gap can grow by at most half a pitch,
                # which keeps it inside the NFPA 4600 mm spacing limit. Without
                # the bound, a head at the head of a line under the L-notch would be
                # dragged the length of the room, leaving an 8700 mm gap behind
                # it — a cure worse than the disease.
                if wall is None or not (half < wall <= 2.0 * half):
                    continue
                moved = list(pos)
                moved[axis] += sign * (wall - half)
                if (point_in_polygon(program.footprint_mm, moved[0], moved[1])
                        and not _blocked_by_column(columns, moved[0], moved[1],
                                                   body_mm)):
                    positions[end] = moved

    return [Sprinkler(name=h.name, x_mm=positions[h.name][0],
                      y_mm=positions[h.name][1], z_mm=h.z_mm, body_mm=h.body_mm)
            for h in heads]


def _ray_to_wall(polygon, x: float, y: float, axis: int,
                 sign: float) -> float | None:
    """Distance from a point to the room boundary along +/- one axis."""
    here = (x, y)
    other = 1 - axis
    best: float | None = None
    n = len(polygon)
    for i in range(n):
        p, q = polygon[i], polygon[(i + 1) % n]
        lo, hi = min(p[other], q[other]), max(p[other], q[other])
        if not (lo - 1.0 <= here[other] <= hi + 1.0):
            continue
        if abs(q[other] - p[other]) < 1.0:
            continue
        t = (here[other] - p[other]) / (q[other] - p[other])
        travel = (p[axis] + t * (q[axis] - p[axis]) - here[axis]) * sign
        if travel > 1.0 and (best is None or travel < best):
            best = travel
    return best


def perimeter_crahs(program: HallProgram, count: int,
                    width_mm: float, depth_mm: float, height_mm: float,
                    capacity_kw: float, airflow_m3h: float) -> list[Crah]:
    """CRAH units along the low-Y wall, facing into the hall.

    Dimensions are injected rather than read from :mod:`specs` because the CRAH
    constants are vendor data; the caller owns the choice of unit.
    """
    min_x, min_y, max_x, _ = program.bounds_mm()
    units: list[Crah] = []
    # spread across the wall rather than bunching in the first six tiles: a
    # perimeter CRAH gallery that stops a third of the way along the room reads
    # as a generator artefact, and it leaves the far end of every row unserved
    usable = (max_x - min_x) - 2 * program.tile.pitch_mm - width_mm
    spacing = max(program.tile.pitch_mm * 4, usable / max(1, count - 1))
    for i in range(count):
        x = min_x + program.tile.pitch_mm + i * spacing
        gx, gy = program.tile.snap(x, min_y)
        if not box_in_polygon(program.footprint_mm,
                              gx, gy, gx + width_mm, gy + depth_mm):
            continue
        units.append(Crah(
            name=f"CRAH-{len(units) + 1:02d}", x_mm=gx, y_mm=gy,
            width_mm=width_mm, depth_mm=depth_mm, height_mm=height_mm,
            capacity_kw=capacity_kw, airflow_m3h=airflow_m3h, facing="NORTH",
        ))
    return units


def working_space(x_mm: float, y_mm: float, width_mm: float, depth_mm: float,
                  facing: str, distance_mm: float
                  ) -> tuple[float, float, float, float]:
    """NEC 110.26(A)(1) working space swept off the front of electrical gear.

    Same geometry as a cabinet's service clearance, different clause and a
    different number, so it is written out rather than reusing
    :func:`program.clearance_zones` — which is pinned to the TIA-942 cabinet
    figures and would silently change meaning if either standard moved.
    """
    if facing == "SOUTH":
        return (x_mm, y_mm - distance_mm, x_mm + width_mm, y_mm)
    if facing == "NORTH":
        return (x_mm, y_mm + depth_mm, x_mm + width_mm, y_mm + depth_mm + distance_mm)
    if facing == "EAST":
        return (x_mm + width_mm, y_mm, x_mm + width_mm + distance_mm, y_mm + depth_mm)
    return (x_mm - distance_mm, y_mm, x_mm, y_mm + depth_mm)


def row_end_pdus(program: HallProgram, racks: list[Rack],
                 blockers: list[Containment] | None = None) -> list[Pdu]:
    """One floor-mount PDU per rack row, east of the row it feeds.

    Its front face is flush with the row's front face, so the NEC 110.26(A)(1)
    900 mm working space opens onto the same cold aisle the cabinets do — which
    is both how it is built and what makes the working space checkable against
    the aisle the drawing dimensions.

    Placement walks east one tile at a time until the unit, its working space
    and the room boundary all agree. A row whose PDU cannot be placed simply
    does not get one, exactly as a bay blocked by a column does not get a
    cabinet.

    ``blockers`` is the containment, which has to be built first. Its end doors
    stand at the east end of the *pair* of rows they close, so when one of the
    two rows is short a column the door lands exactly where that row's PDU
    would otherwise go — a hard clash in the answer key.
    """
    width = float(specs.PDU_WIDTH)
    depth = float(specs.PDU_DEPTH)
    height = float(specs.PDU_HEIGHT)
    reach = float(specs.NEC_WORKING_SPACE_DEPTH)
    obstacles = column_footprints(program)
    obstacles += [occupied_aabb(r.x_mm, r.y_mm, r.width_mm, r.depth_mm, r.facing)
                  for r in racks]
    obstacles += [(c.x_mm, c.y_mm, c.x_mm + c.width_mm, c.y_mm + c.depth_mm)
                  for c in (blockers or [])]
    placed: list[Pdu] = []
    grouped = rows_of(racks)

    for row in program.rows:
        members = grouped.get(row.row_id)
        if not members:
            continue
        facing = members[0].facing
        x_end = max(r.x_mm + r.width_mm for r in members)
        y_lo = min(r.y_mm for r in members)
        y_hi = max(r.y_mm + r.depth_mm for r in members)
        py = y_lo if facing == "SOUTH" else y_hi - depth

        for step in range(8):
            px = x_end + PDU_SETBACK_MM + step * program.tile.pitch_mm
            box = occupied_aabb(px, py, width, depth, facing)
            zone = working_space(px, py, width, depth, facing, reach)
            if not box_in_polygon(program.footprint_mm, *box):
                continue
            if not box_in_polygon(program.footprint_mm, *zone):
                continue
            if any(_aabb_overlap(box, o) or _aabb_overlap(zone, o)
                   for o in obstacles):
                continue
            if any(_aabb_overlap(box, occupied_aabb(
                    p.x_mm, p.y_mm, p.width_mm, p.depth_mm, p.facing))
                    for p in placed):
                continue
            placed.append(Pdu(
                name=f"PDU-{len(placed) + 1:02d}", x_mm=px, y_mm=py,
                width_mm=width, depth_mm=depth, height_mm=height,
                rating_kva=float(specs.PDU_RATING_KVA),
                serves_row=row.row_id, facing=facing,
            ))
            obstacles.append(box)
            break
    return placed


def cold_aisle_containment(program: HallProgram, racks: list[Rack]
                           ) -> list[Containment]:
    """Roof and end doors over every cold aisle with a cabinet row on both sides.

    Only interior cold aisles are contained. A containment assembly needs
    cabinets to seal against on both long sides, so the leading and trailing
    aisles — which face a wall — are left open, and hot aisles are left open
    because this is cold-aisle containment.

    The roof overhangs the aisle onto the cabinet tops by
    :data:`specs.CONTAINMENT_ROOF_OVERHANG`, which is why its plan extent is
    wider than the aisle it closes, and it starts at cabinet height so it
    touches the cabinets without interpenetrating them.
    """
    bands = aisle_bands(program)
    grouped = rows_of(racks)
    overhang = float(specs.CONTAINMENT_ROOF_OVERHANG) / 2.0
    roof_thickness = float(specs.CONTAINMENT_ROOF_THICKNESS)
    door_thickness = float(specs.CONTAINMENT_END_PANEL_THICKNESS)
    out: list[Containment] = []

    for k in range(1, len(program.rows)):
        if bands[k][0] != "cold":
            continue
        below = grouped.get(program.rows[k - 1].row_id, [])
        above = grouped.get(program.rows[k].row_id, [])
        if not below or not above:
            continue
        members = below + above
        x0 = min(r.x_mm for r in members)
        x1 = max(r.x_mm + r.width_mm for r in members)
        top = max(r.z_mm + r.height_mm for r in members)
        y0 = bands[k][1] - overhang
        y1 = bands[k][2] + overhang
        aisle = f"{program.rows[k - 1].row_id}{program.rows[k].row_id}"

        # Columns run the full clear height, so a roof spanning the aisle in one
        # piece is driven straight through any column that lands in it. Real
        # containment roofs are panelised per bay and are simply omitted (or
        # flashed) where the structure comes through, so the roof is emitted as
        # the maximal runs of aisle that no column interrupts.
        # The cross-aisle is a circulation path, not a gap under a lid: the
        # containment has to stop at it and start again on the other side,
        # with a door on each new end. BICSI notes containment doors at row
        # ends double as egress points, which is the whole reason the break
        # is there.
        blocked = _column_x_bands(program, y0, y1)
        if program.cross_aisle_mm is not None:
            blocked = blocked + [program.cross_aisle_mm]

        candidates: list[Containment] = []
        runs = [r for r in _clear_runs(x0, x1, blocked)
                if r[1] - r[0] >= program.tile.pitch_mm]
        for index, (rx0, rx1) in enumerate(runs, start=1):
            candidates.append(Containment(
                name=f"CONT-{aisle}-ROOF-{index:02d}", x_mm=rx0, y_mm=y0,
                z_mm=top, width_mm=rx1 - rx0, depth_mm=y1 - y0,
                height_mm=roof_thickness, aisle_id=aisle, part="roof"))
        if not candidates:
            continue

        # A door at every end a person can walk to: the two outer ends, and
        # both faces of the cross-aisle where one cuts through.
        ends: list[tuple[str, float]] = [("W", x0 - door_thickness), ("E", x1)]
        if program.cross_aisle_mm is not None:
            cx0, cx1 = program.cross_aisle_mm
            if any(r[1] <= cx0 + EPS_MM for r in runs):
                ends.append(("CW", cx0))
            if any(r[0] >= cx1 - EPS_MM for r in runs):
                ends.append(("CE", cx1 - door_thickness))
        for side, dx in ends:
            door = Containment(
                name=f"CONT-{aisle}-DOOR-{side}", x_mm=dx, y_mm=y0, z_mm=0.0,
                width_mm=door_thickness, depth_mm=y1 - y0,
                height_mm=top + roof_thickness, aisle_id=aisle, part="door")
            if _clear_runs(dx, dx + door_thickness,
                           _column_x_bands(program, y0, y1)):
                candidates.append(door)

        out.extend(
            c for c in candidates
            if box_in_polygon(program.footprint_mm, c.x_mm, c.y_mm,
                              c.x_mm + c.width_mm, c.y_mm + c.depth_mm)
        )
    return out


def _column_x_bands(program: HallProgram, y0: float, y1: float
                    ) -> list[tuple[float, float]]:
    """X extents of the columns that intrude into the band ``y0..y1``."""
    return [(cx0, cx1) for cx0, cy0, cx1, cy1 in column_footprints(program)
            if cy1 > y0 and cy0 < y1]


def _clear_runs(x0: float, x1: float, blocked: list[tuple[float, float]]
                ) -> list[tuple[float, float]]:
    """``x0..x1`` minus the blocked intervals, as a list of maximal runs."""
    runs = [(x0, x1)]
    for bx0, bx1 in sorted(blocked):
        nxt: list[tuple[float, float]] = []
        for rx0, rx1 in runs:
            if bx1 <= rx0 or rx1 <= bx0:
                nxt.append((rx0, rx1))
                continue
            if rx0 < bx0:
                nxt.append((rx0, bx0))
            if bx1 < rx1:
                nxt.append((bx1, rx1))
        runs = nxt
    return runs


#: One element's realised solid: ``(name, kind, (x0, y0, z0, x1, y1, z1))``.
Solid = "tuple[str, str, tuple[float, float, float, float, float, float]]"


def _solid_boxes(program: HallProgram, racks: list[Rack], crahs: list[Crah],
                 columns: list[Column], sprinklers: list[Sprinkler],
                 pdus: list[Pdu], containment: list[Containment],
                 routes: list[RouteSegment]) -> list:
    """Every emitted element as a 3D world box, for the clash and zone checks."""
    out: list = []

    def add(name: str, kind: str, x: float, y: float, z: float,
            w: float, d: float, h: float, facing: str = "NORTH") -> None:
        x0, y0, x1, y1 = occupied_aabb(x, y, w, d, facing)
        out.append((name, kind, (x0, y0, z, x1, y1, z + h)))

    for r in racks:
        add(r.name, "rack", r.x_mm, r.y_mm, r.z_mm,
            r.width_mm, r.depth_mm, r.height_mm, r.facing)
    for c in crahs:
        add(c.name, "crah", c.x_mm, c.y_mm, c.z_mm,
            c.width_mm, c.depth_mm, c.height_mm, c.facing)
    for p in pdus:
        add(p.name, "pdu", p.x_mm, p.y_mm, p.z_mm,
            p.width_mm, p.depth_mm, p.height_mm, p.facing)
    for k in containment:
        add(k.name, "containment", k.x_mm, k.y_mm, k.z_mm,
            k.width_mm, k.depth_mm, k.height_mm)
    for col in columns:
        add(col.name, "column", col.x_mm, col.y_mm, 0.0,
            col.size_mm, col.size_mm, col.height_mm)
    for s in sprinklers:
        half = s.body_mm / 2.0
        add(s.name, "sprinkler", s.x_mm - half, s.y_mm - half, s.z_mm,
            s.body_mm, s.body_mm, s.body_mm)
    for seg in routes:
        # straight from the spec the IFC writer uses, not from a second
        # transcription of the axis convention. A duplicate that did not
        # know about the "Z" axis would read every riser as a 2675 mm deep
        # horizontal slab, inventing clashes and clear-height violations
        # that are not in the model at all.
        spec = seg.to_spec()
        add(spec.name, seg.kind, spec.x_mm, spec.y_mm, spec.z_mm,
            spec.width_mm, spec.depth_mm, spec.height_mm)
    return out


def _boxes_overlap(a, b, eps_mm: float = 1.0) -> bool:
    """3D interpenetration by more than ``eps_mm`` on every axis.

    3D, not plan. A plan-only test is correct only while every
    element stands on the floor; with routes at 2600 mm it reads a cable tray
    crossing over a cabinet as a hard clash.
    """
    return all(min(a[i + 3], b[i + 3]) - max(a[i], b[i]) > eps_mm
               for i in range(3))


def _zone_overlaps(zone: tuple[float, float, float, float],
                   box, eps_mm: float = 1.0) -> bool:
    """Plan overlap between a 2D clearance zone and a 3D solid."""
    return (min(zone[2], box[3]) - max(zone[0], box[0]) > eps_mm
            and min(zone[3], box[4]) - max(zone[1], box[1]) > eps_mm)


def self_check(program: HallProgram, racks: list[Rack],
               sprinklers: list[Sprinkler],
               crahs: list[Crah] | None = None,
               columns: list[Column] | None = None,
               pdus: list[Pdu] | None = None,
               containment: list[Containment] | None = None,
               routes: list[RouteSegment] | None = None) -> list[str]:
    """Assert the ground truth satisfies every graded threshold.

    Only ``REQUIREMENT``-kind constants are checked: the generated model is
    allowed to depart from a "should".

    Every element list is optional, and the gate is blind to whatever it is not
    shown: seeing only racks would miss sprinkler heads embedded in columns;
    seeing only floor-standing equipment would let the oracle route hang
    through one.
    :func:`build` always passes everything.
    """
    crahs = crahs or []
    columns = columns or []
    pdus = pdus or []
    containment = containment or []
    routes = routes or []
    problems: list[str] = []

    problems.extend(program.validate())
    ok, why = fits_in_hall(program)
    if not ok:
        problems.append(why)

    # TIA-942: front/rear clearance, realised as the cold/hot aisle widths
    if program.cold_aisle_mm < float(specs.FRONT_CLEARANCE):
        problems.append(
            f"cold aisle {program.cold_aisle_mm:.0f} < FRONT_CLEARANCE "
            f"{float(specs.FRONT_CLEARANCE):.0f} ({specs.FRONT_CLEARANCE.ref})"
        )
    if program.hot_aisle_mm < float(specs.REAR_CLEARANCE):
        problems.append(
            f"hot aisle {program.hot_aisle_mm:.0f} < REAR_CLEARANCE "
            f"{float(specs.REAR_CLEARANCE):.0f} ({specs.REAR_CLEARANCE.ref})"
        )

    # TIA-942: clear height to any obstruction
    if program.clear_height_mm < float(specs.CLEAR_HEIGHT):
        problems.append(
            f"clear height {program.clear_height_mm:.0f} < CLEAR_HEIGHT "
            f"{float(specs.CLEAR_HEIGHT):.0f} ({specs.CLEAR_HEIGHT.ref})"
        )

    solids = _solid_boxes(program, racks, crahs, columns, sprinklers,
                          pdus, containment, routes)
    by_kind: dict[str, list] = {}
    for name, kind, box in solids:
        by_kind.setdefault(kind, []).append((name, box))

    # TIA-942 5.3.4.3: 460mm kept clear below every head, measured against
    # whatever is actually underneath it -- not against the tallest thing in the
    # hall. A global "tallest" test cannot tell a cabinet standing under a head
    # from one three aisles away, and the whole point of the tier plan is that a
    # run may sit above the standoff plane *provided it dodges the heads*.
    standoff = float(specs.SPRINKLER_CLEARANCE)
    intrusions: list[str] = []
    for head in sprinklers:
        half = head.body_mm / 2.0
        head_box = (head.x_mm - half, head.y_mm - half, head.z_mm,
                    head.x_mm + half, head.y_mm + half, head.z_mm + head.body_mm)
        for name, kind, box in solids:
            if kind == "sprinkler" or box[5] > head.z_mm + 1.0:
                continue
            if _zone_overlaps((head_box[0], head_box[1], head_box[3], head_box[4]),
                              box) and (head.z_mm - box[5]) < standoff:
                intrusions.append(f"{head.name}~{name}")
    if intrusions:
        problems.append(
            f"{len(intrusions)} elements inside the TIA-942 {standoff:.0f}mm "
            f"sprinkler standoff, e.g. {intrusions[:3]} "
            f"({specs.SPRINKLER_CLEARANCE.ref})"
        )

    # TIA-942 5.3.4.3 again, from the other side: nothing OVERHEAD may hang
    # below the room minimum. Anything standing on or passing through the
    # finished floor is not overhead — that covers under-floor mains and the
    # vertical risers that join them to the branches, on the same reasoning
    # that already excludes racks, CRAHs and columns. Mirrors
    # ``dcgrade.rules.clear_height``; the two tests have to agree or a seed
    # passes the build gate and fails the grader.
    #
    # A head hanging under a containment roof is exempt, on the same reasoning
    # that exempts a connection landing on top of a CRAH: the lid above it
    # already caps that column of air at its own underside. NFPA 13 10.2.7.4.2
    # REQUIRES that head, so grading it against the room's 2600 mm would make
    # the code-required element the violation. ``dcgrade.rules.clear_height``
    # carves out exactly the same case via ``protecting_panel``.
    lids = [box for _, kind, box in solids if kind == "containment"]

    def _under_a_lid(box) -> bool:
        return any(lid[0] - EPS_MM <= box[0] and box[3] <= lid[3] + EPS_MM
                   and lid[1] - EPS_MM <= box[1] and box[4] <= lid[4] + EPS_MM
                   and box[5] <= lid[2] + EPS_MM
                   for lid in lids)

    low = [name for name, kind, box in solids
           if kind in ("cable_tray", "busway", "chw_pipe", "sprinkler")
           and box[2] > 0.0 and box[2] < float(specs.CLEAR_HEIGHT) - 1.0
           and not (kind == "sprinkler" and _under_a_lid(box))]
    if low:
        problems.append(
            f"{len(low)} overhead elements below the {float(specs.CLEAR_HEIGHT):.0f}mm "
            f"clear height, e.g. {low[:3]} ({specs.CLEAR_HEIGHT.ref})"
        )

    # NEC 110.26(A)(1)+(A)(3): 900mm deep and 2000mm tall of working space in
    # front of every PDU. The height matters -- a cable tray at 2600mm is over
    # the space, not in it, and calling that an obstruction would be stricter
    # than the code.
    reach = float(specs.NEC_WORKING_SPACE_DEPTH)
    head_room = float(specs.NEC_WORKING_SPACE_HEIGHT)
    blocked: list[str] = []
    for pdu in pdus:
        zone = working_space(pdu.x_mm, pdu.y_mm, pdu.width_mm, pdu.depth_mm,
                             pdu.facing, reach)
        for name, kind, box in solids:
            if name == pdu.name or box[2] >= head_room or box[5] <= 0.0:
                continue
            if _zone_overlaps(zone, box):
                blocked.append(f"{pdu.name}~{name}")
    if blocked:
        problems.append(
            f"{len(blocked)} obstructions in a PDU's NEC working space, e.g. "
            f"{blocked[:3]} ({specs.NEC_WORKING_SPACE_DEPTH.ref})"
        )

    # NEC 110.26(E)(1)(a): the 1800mm above a PDU is dedicated to the
    # electrical installation. Graded against chilled water only -- see the
    # constant's note for why busway and tray are not foreign to it.
    dedicated = float(specs.DEDICATED_ELECTRICAL_SPACE)
    foreign: list[str] = []
    for pdu in pdus:
        top = pdu.z_mm + pdu.height_mm
        zone = (pdu.x_mm, pdu.y_mm, pdu.x_mm + pdu.width_mm, pdu.y_mm + pdu.depth_mm)
        for name, box in by_kind.get("chw_pipe", []):
            if _zone_overlaps(zone, box) and box[2] < top + dedicated:
                foreign.append(f"{pdu.name}~{name}")
    if foreign:
        problems.append(
            f"{len(foreign)} chilled-water runs inside a PDU's NEC dedicated "
            f"space, e.g. {foreign[:3]} ({specs.DEDICATED_ELECTRICAL_SPACE.ref})"
        )

    # TIA-942 7.3.1 Table 2: 300mm clear between telecom tray and power busway,
    # satisfied by separation on any single axis.
    separation = float(specs.POWER_TELECOM_SEPARATION)
    too_close: list[str] = []
    for tray_name, tray in by_kind.get("cable_tray", []):
        for bus_name, bus in by_kind.get("busway", []):
            gap = max(max(tray[i] - bus[i + 3], bus[i] - tray[i + 3])
                      for i in range(3))
            if gap < separation:
                too_close.append(f"{tray_name}~{bus_name} ({gap:.0f}mm)")
    if too_close:
        problems.append(
            f"{len(too_close)} tray/busway pairs closer than {separation:.0f}mm, "
            f"e.g. {too_close[:3]} ({specs.POWER_TELECOM_SEPARATION.ref})"
        )

    # NFPA 13: protection area per head
    max_area_m2 = float(specs.SPRINKLER_MAX_PROTECTION_AREA)
    area_m2 = (program.sprinkler_pitch_mm / 1000.0) ** 2
    if area_m2 > max_area_m2:
        problems.append(
            f"sprinkler pitch {program.sprinkler_pitch_mm:.0f}mm gives "
            f"{area_m2:.1f} m2/head > {max_area_m2:.0f} ({specs.SPRINKLER_MAX_PROTECTION_AREA.ref})"
        )

    # Tile-grid placement. The row ORIGIN and every row's y are on the grid;
    # x within a row is not, and must not be.
    #
    # TIA-942-B 6.14.3's "should be aligned with a tile edge" cannot hold for
    # every bay once a row contains an 800 mm distributor cabinet, because
    # TIA-942-C separately requires distributor cabinets to be at least 800 mm
    # wide and 800 does not divide 600. Choosing which of its own two clauses
    # to break is what BICSI Figure 6-5 shows a real layout doing. The
    # invariant that survives — and the one an agent has to reproduce — is
    # that a row registers at one end and every bay after that is the running
    # sum of the widths before it.
    off_grid = [r.name for r in racks
                if not program.tile.is_on_grid(program.row_start_x_mm, r.y_mm)]
    if off_grid:
        problems.append(f"{len(off_grid)} rows off the tile grid, e.g. {off_grid[:3]}")
    by_row = rows_of(racks)
    for row in program.rows:
        origins = {round(x, 3): bay for x, bay in bay_origins(program, row)}
        for rack in by_row.get(row.row_id, ()):
            bay = origins.get(round(rack.x_mm, 3))
            if bay is None or abs(bay.width_mm - rack.width_mm) > EPS_MM:
                problems.append(
                    f"row {row.row_id} bay {rack.name} at x={rack.x_mm:.0f} "
                    f"w={rack.width_mm:.0f} is not on the bay cursor")
                break

    # Hard clash, EVERY emitted pair -- not just racks, and in 3D. Gating only
    # racks would miss sprinkler-in-column geometry; gating
    # only the plan would read every overhead run crossing a cabinet as a clash.
    clashes: list[str] = []
    for i, (n1, _, b1) in enumerate(solids):
        for n2, _, b2 in solids[i + 1:]:
            if _boxes_overlap(b1, b2):
                clashes.append(f"{n1}~{n2}")
    if clashes:
        problems.append(
            f"{len(clashes)} hard clashes in the ground truth, e.g. {clashes[:3]}"
        )

    col_boxes = [(c.name, (c.x_mm, c.y_mm, c.x_mm + c.size_mm, c.y_mm + c.size_mm))
                 for c in columns] or [
        (f"COL-{i}", b) for i, b in enumerate(column_footprints(program))]

    # TIA-942 5.11.7.1 PER CABINET, not just per aisle. The aisle-width check
    # above is necessary but not sufficient: a column can stand inside one
    # cabinet's service clearance while every aisle is nominally wide enough.
    
    obstruction_boxes = [b for _, b in col_boxes] + [
        occupied_aabb(c.x_mm, c.y_mm, c.width_mm, c.depth_mm, c.facing) for c in crahs
    ]
    starved = []
    for r in racks:
        front_zone, rear_zone = clearance_zones(
            r.x_mm, r.y_mm, r.width_mm, r.depth_mm, r.facing)
        for ob in obstruction_boxes:
            if _aabb_overlap(front_zone, ob) or _aabb_overlap(rear_zone, ob):
                starved.append(r.name)
                break
    if starved:
        problems.append(
            f"{len(starved)} cabinets have an obstruction inside their TIA-942 "
            f"service clearance, e.g. {starved[:3]} ({specs.FRONT_CLEARANCE.ref})"
        )

    # NFPA 13 PER HEAD, not just per nominal pitch. The pitch check above uses
    # the DESIGN pitch; if a head is dropped or shifted, the realised spacing
    # between surviving heads is what actually determines coverage.
    # grouped within half a pitch, not on exact equality. A head nudged off a
    # column no longer shares its neighbours' y to the millimetre, and exact
    # grouping would read it as a one-head row and its neighbours as a row with
    # a hall-wide gap in it.
    band = max(1.0, program.sprinkler_pitch_mm / 2.0)
    by_row: dict[int, list[float]] = {}
    for head in sprinklers:
        by_row.setdefault(int(round(head.y_mm / band)), []).append(head.x_mm)
    worst = 0.0
    for _, xs in by_row.items():
        xs.sort()
        for a, b in zip(xs, xs[1:]):
            worst = max(worst, b - a)
    if worst > 0:
        realised_area_m2 = (worst / 1000.0) * (program.sprinkler_pitch_mm / 1000.0)
        if realised_area_m2 > max_area_m2:
            problems.append(
                f"realised sprinkler spacing {worst:.0f}mm gives "
                f"{realised_area_m2:.1f} m2/head > {max_area_m2:.4f} "
                f"({specs.SPRINKLER_MAX_PROTECTION_AREA.ref})"
            )

    # Room containment: nothing may sit outside the hall polygon. bounds_mm() is
    # the bounding BOX, so anything placed from it lands in the l_shaped notch.
    outside: list[str] = [
        name for name, _, box in solids
        if not box_in_polygon(program.footprint_mm, box[0], box[1], box[3], box[4])
    ]
    if outside:
        problems.append(
            f"{len(outside)} elements outside the hall polygon, e.g. {outside[:3]}"
        )

    return problems


def build(program: HallProgram, out_path: str,
          crah_count: int = 4,
          crah_width_mm: float = 1200.0, crah_depth_mm: float = 900.0,
          crah_height_mm: float = 2000.0, crah_capacity_kw: float = 100.0,
          crah_airflow_m3h: float = 20000.0,
          strict: bool = True) -> BuildResult:
    """Generate one hall. With ``strict``, refuses to write a non-compliant model."""
    racks = layout_racks(program)
    # In-row units take cabinet bays, so the swap happens before anything that
    # reads the rack list: containment spans the aisle between rows, the PDU
    # sets back from the row's east end, and both would be computed against
    # cabinets that are no longer there.
    racks, in_row = substitute_in_row_units(racks)
    crahs = perimeter_crahs(program, crah_count, crah_width_mm, crah_depth_mm,
                            crah_height_mm, crah_capacity_kw, crah_airflow_m3h)
    crahs = crahs + in_row
    sprinklers = sprinkler_grid(program)
    # column_positions returns CENTRES; box geometry is placed from its min corner
    half = program.column_size_mm / 2.0
    columns = [
        Column(name=f"COL-{i + 1:02d}", x_mm=cx - half, y_mm=cy - half,
               size_mm=program.column_size_mm, height_mm=program.clear_height_mm)
        for i, (cx, cy) in enumerate(column_positions(program))
    ]

    containment = cold_aisle_containment(program, racks)
    # The lid is a fixed obstruction wider than 1200 mm, so NFPA 13 10.2.7.4.2
    # puts a head under it. It has to come after the containment and before
    # the route, which dodges heads.
    sprinklers = sprinklers + containment_head_line(program, containment,
                                                    len(sprinklers))
    pdus = row_end_pdus(program, racks, containment)

    # A seed whose oracle route is infeasible is rejected
    # at build time, not shipped ungradeable. Under ``strict`` that means an
    # exception; otherwise it becomes a recorded problem and Task B is simply
    # not available for the instance.
    route: RoutePlan | None = None
    route_problem: str | None = None
    try:
        route = plan_route(program, racks, columns, sprinklers, crahs)
    except OracleInfeasible as exc:
        route_problem = f"no feasible oracle route: {exc}"

    routes = list(route.segments) if route else []
    problems = self_check(program, racks, sprinklers, crahs, columns,
                          pdus, containment, routes)
    if route_problem:
        problems.append(route_problem)
    if problems and strict:
        raise ValueError(
            "ground truth violates its own graded thresholds; seed "
            f"{program.seed} is unusable:\n  - " + "\n  - ".join(problems)
        )

    b = IfcBuilder(hall_name="DATA HALL 1")
    b.set_hall_geometry(program.footprint_mm, program.clear_height_mm)

    # Access-floor PANELS, not a solid block of plenum. Modelling the covering
    # as the full raised_floor_mm depth would fill the entire under-floor void with
    # solid geometry, so every Task B under-floor cable-tray route would hard
    # clash with the floor itself -- while TRAY_BELOW_TILE is a graded threshold
    # that assumes the plenum is open. Panels are ~35 mm thick on pedestals,
    # with their top face at finished-floor level (z = 0).
    b.add(ElementSpec(
        ifc_class="IfcCovering", name="ACCESS FLOOR PANELS",
        width_mm=0.0, depth_mm=0.0, height_mm=ACCESS_FLOOR_PANEL_MM,
        x_mm=0.0, y_mm=0.0, z_mm=-ACCESS_FLOOR_PANEL_MM,
        predefined_type="FLOORING",
    ), footprint_mm=program.footprint_mm)

    b.add_all(r.to_spec() for r in racks)
    b.add_all(c.to_spec() for c in crahs)
    b.add_all(p.to_spec() for p in pdus)
    b.add_all(k.to_spec() for k in containment)
    b.add_all(c.to_spec() for c in columns)
    b.add_all(s.to_spec() for s in sprinklers)
    b.add_all(seg.to_spec() for seg in routes)
    b.write(out_path)

    return BuildResult(path=out_path, racks=racks, crahs=crahs,
                       columns=columns, sprinklers=sprinklers, problems=problems,
                       pdus=pdus, containment=containment, routes=routes,
                       l_opt_mm=route.l_opt_mm if route else 0.0)
