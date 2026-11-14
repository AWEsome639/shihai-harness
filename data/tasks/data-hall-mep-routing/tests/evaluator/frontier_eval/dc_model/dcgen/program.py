"""The hall program and the rack-row layout engine.

A :class:`HallProgram` is the seeded description of one instance. :func:`layout_racks`
turns it into placed cabinets that satisfy the TIA-942 layout rules *by
construction* — the ground truth must pass the grader perfectly, so correctness
here is not best-effort, it is the reference-round-trip gate.

**Hot/cold aisle geometry** (TIA-942 5.11.2: cabinets arranged in an alternating
pattern, fronts facing each other). The repeating unit along +Y is::

    cold aisle          <- fronts of the row above and the row below face in
    row  (faces SOUTH)
    hot aisle           <- rears face each other
    row  (faces NORTH)
    cold aisle
    ...

so a row's *front* clearance is the cold aisle width and its *rear* clearance is
the hot aisle width. TIA-942 5.11.7.1 requires >= 1000 mm front and >= 600 mm
rear; both aisles are additionally constrained to whole floor tiles by 5.11.4.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Sequence

from . import specs
from .elements import Crah, Rack
from .units import TileGrid, box_in_polygon, occupied_aabb, point_in_polygon

#: TIA-942 5.11.7.1 minima. Re-exported from :mod:`specs` rather than restated,
#: so there is exactly one source of truth for a graded threshold.
MIN_FRONT_CLEARANCE_MM = float(specs.FRONT_CLEARANCE)
MIN_REAR_CLEARANCE_MM = float(specs.REAR_CLEARANCE)


@dataclass(frozen=True)
class BaySpec:
    """One position in a row: what stands there, and how wide it is.

    ``kind`` is one of:

    ``it``
        A 600 mm server cabinet, the ordinary case.
    ``network``
        A distributor cabinet. ANSI/TIA-942-C requires cabinets in the MDA,
        IDA and HDA to be **at least 800 mm wide**, and 800 does not divide
        600 — so a row containing one walks off the tile joints at that bay
        and never re-registers. That is the whole point of it being here.
    ``spare``
        A position left deliberately empty. UFC 3-580-01 asks for one spare
        cabinet per four utilised, and Schneider RD110's rows are literally
        "eight IT racks bookended by an empty rack". It advances the cursor
        and places nothing, so counting cabinets does not recover the pitch.
    ``cross``
        The gap a transverse cross-aisle cuts through the row. ANSI/BICSI
        002-2019 6.6.4.3 caps a continuous run of adjacent cabinets at 16 m,
        and a row that fills a 24-30 m hall exceeds it — which is why real
        plans show a circulation spine through the rows.
    """

    kind: str
    width_mm: float = 600.0
    depth_mm: float = 1200.0
    height_mm: float = 2000.0
    rack_units: int = 42

    @property
    def placed(self) -> bool:
        """Does this bay put a cabinet in the model?"""
        return self.kind not in ("spare", "cross")


@dataclass(frozen=True)
class RowSpec:
    """One rack row.

    ``bays`` is the row's enclosure sequence, and it is what
    :func:`layout_racks` walks. Leaving it ``None`` means ``n_racks``
    identical bays, which is what a uniform row is — the field exists
    so a heterogeneous row does not need a different class.
    """

    row_id: str
    n_racks: int
    rack_width_mm: float = 600.0
    rack_depth_mm: float = 1200.0
    rack_height_mm: float = 2000.0
    rack_units: int = 42
    bays: tuple[BaySpec, ...] | None = None

    def bay_sequence(self) -> tuple[BaySpec, ...]:
        if self.bays is not None:
            return self.bays
        return tuple(
            BaySpec("it", self.rack_width_mm, self.rack_depth_mm,
                    self.rack_height_mm, self.rack_units)
            for _ in range(self.n_racks)
        )

    @property
    def length_mm(self) -> float:
        """Nominal west-to-east extent, before any bay is dropped."""
        return sum(b.width_mm for b in self.bay_sequence())


@dataclass
class HallProgram:
    """A single generated instance.

    ``footprint_mm`` is a closed-ish CCW polygon in mm (first point not repeated).
    A rectangle gives a simple hall; adding a notch gives the L-shaped variant.
    """

    seed: int
    footprint_mm: list[tuple[float, float]]
    clear_height_mm: float
    raised_floor_mm: float
    tile: TileGrid
    rows: list[RowSpec]
    #: 2 tiles = 1200 mm. APC's nominal containment aisle widths are
    #: 900/1200/1500/1800 mm, and a 2-tile (1200 mm) cold aisle is the typical
    #: case, 3-tile (1800 mm) for high density. Whole tiles keep successive rows
    #: aligned to the grid. 1200 mm clears the TIA-942 1000 mm front minimum.
    cold_aisle_tiles: int = 2
    #: 1200 mm also clears the TIA-942 600 mm rear minimum with margin, and
    #: matches an APC nominal width. Practice sometimes uses 900 mm (1.5 tiles),
    #: but a half-tile pitch offsets every following row from the grid.
    hot_aisle_tiles: int = 2
    #: West wall to the west face of the cabinet line. Three tiles, not two:
    #: the three service trunks run north-south in this strip and their risers
    #: stand in it, and three lanes abreast with the graded TIA-942 separations
    #: need 1379 mm.
    row_start_x_mm: float = 1800.0
    first_aisle_y_mm: float = 1200.0
    column_grid_mm: float = 7200.0
    column_size_mm: float = 600.0
    column_offset_mm: float = 0.0
    sprinkler_pitch_mm: float = 3000.0
    #: Which of the catalogued row pitches this hall is set out on. Recorded
    #: because it is printed on A-101 and it is the one fact that explains why
    #: the two aisles are the widths they are.
    pitch_class: str = ""
    #: ``(x0, x1)`` of the transverse cross-aisle, or ``None`` where the rows
    #: are short enough not to need one. Its EAST edge is common to every row;
    #: its west edge steps, because a row of mixed-width bays cannot end
    #: flush on both sides of a break and a real plan does not pretend it can.
    cross_aisle_mm: tuple[float, float] | None = None
    knobs: dict[str, float] = field(default_factory=dict)

    # ------------------------------------------------------------------ derived

    @property
    def cold_aisle_mm(self) -> float:
        return self.cold_aisle_tiles * self.tile.pitch_mm

    @property
    def hot_aisle_mm(self) -> float:
        return self.hot_aisle_tiles * self.tile.pitch_mm

    @property
    def ceiling_z_mm(self) -> float:
        """Underside of the ceiling/obstruction plane above finished floor."""
        return self.clear_height_mm

    def bounds_mm(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.footprint_mm]
        ys = [p[1] for p in self.footprint_mm]
        return min(xs), min(ys), max(xs), max(ys)

    def validate(self) -> list[str]:
        """Structural self-check. A non-empty result means the seed is unusable."""
        problems: list[str] = []
        if self.cold_aisle_mm < MIN_FRONT_CLEARANCE_MM:
            problems.append(
                f"cold aisle {self.cold_aisle_mm:.0f}mm < TIA-942 front minimum "
                f"{MIN_FRONT_CLEARANCE_MM:.0f}mm"
            )
        if self.hot_aisle_mm < MIN_REAR_CLEARANCE_MM:
            problems.append(
                f"hot aisle {self.hot_aisle_mm:.0f}mm < TIA-942 rear minimum "
                f"{MIN_REAR_CLEARANCE_MM:.0f}mm"
            )
        if len(self.rows) % 2 != 0:
            problems.append(
                "odd row count: hot/cold aisle alternation requires rows in pairs"
            )
        if not self.rows:
            problems.append("no rows defined")
        for r in self.rows:
            if r.n_racks <= 0:
                problems.append(f"row {r.row_id} has no racks")
        return problems


def _aabb_overlap(a: tuple[float, float, float, float],
                  b: tuple[float, float, float, float]) -> bool:
    """Do two (x0, y0, x1, y1) boxes overlap in plan?"""
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def clearance_zones(x_mm: float, y_mm: float, width_mm: float, depth_mm: float,
                    facing: str) -> tuple[tuple[float, float, float, float],
                                          tuple[float, float, float, float]]:
    """The (front, rear) service-clearance prisms swept off a cabinet's faces.

    TIA-942 5.11.7.1 requires clear space in FRONT of and BEHIND a cabinet, not
    merely that the cabinet itself is unobstructed. A column standing in that
    space makes the cabinet unusable even though nothing overlaps the cabinet
    footprint -- you cannot get a server into it.
    """
    front = float(specs.FRONT_CLEARANCE)
    rear = float(specs.REAR_CLEARANCE)
    if facing == "SOUTH":          # front faces -Y
        return ((x_mm, y_mm - front, x_mm + width_mm, y_mm),
                (x_mm, y_mm + depth_mm, x_mm + width_mm, y_mm + depth_mm + rear))
    if facing == "NORTH":          # front faces +Y
        return ((x_mm, y_mm + depth_mm, x_mm + width_mm, y_mm + depth_mm + front),
                (x_mm, y_mm - rear, x_mm + width_mm, y_mm))
    if facing == "EAST":           # front faces +X
        return ((x_mm + width_mm, y_mm, x_mm + width_mm + front, y_mm + depth_mm),
                (x_mm - rear, y_mm, x_mm, y_mm + depth_mm))
    return ((x_mm - front, y_mm, x_mm, y_mm + depth_mm),
            (x_mm + width_mm, y_mm, x_mm + width_mm + rear, y_mm + depth_mm))


def bay_origins(program: HallProgram, row: RowSpec
                ) -> list[tuple[float, BaySpec]]:
    """``(x of the bay's west face, bay)`` for every bay in a row.

    The one place the cursor is computed. :func:`layout_racks` walks it to
    place cabinets and :func:`build.self_check` walks it to verify them, so
    the placement and the check cannot disagree about where bay ``k`` is.
    """
    out: list[tuple[float, BaySpec]] = []
    cursor = program.row_start_x_mm
    for bay in row.bay_sequence():
        out.append((cursor, bay))
        cursor += bay.width_mm
    return out


def layout_racks(program: HallProgram) -> list[Rack]:
    """Place every cabinet of every row on the tile grid.

    Rows alternate facing so that fronts meet across cold aisles and rears meet
    across hot aisles. Row pitch therefore alternates between the hot and cold
    aisle width rather than being constant.

    Cabinet positions that would collide with a structural column are **omitted**,
    which is what real layouts do — the plan simply shows a gap in the row where
    the column lands. Without this the ``column_offset_mm`` knob generates ground
    truth with cabinets inside columns, which is physically impossible and fails
    the grader's own clash gate.
    """
    racks: list[Rack] = []
    obstacles = column_footprints(program)
    y = program.first_aisle_y_mm + program.cold_aisle_mm

    for index, row in enumerate(program.rows):
        # even rows face SOUTH (front at low y, onto the cold aisle below);
        # odd rows face NORTH (front at high y, onto the cold aisle above)
        facing = "SOUTH" if index % 2 == 0 else "NORTH"

        # snap the ROW ORIGIN once and advance from the snapped value, so the
        # realised rows and aisle_bands() agree. Snapping per-rack while
        # accumulating an unsnapped y would make the two drift apart under mixed
        # rack depths.
        _, y = program.tile.snap(0.0, y)

        # A CURSOR, not an index. The row is a sequence of bays of differing
        # widths, so bay k starts at the accumulated sum of the widths before
        # it — "row_start + k x 600" is the wrong answer as soon as one 800 mm
        # distributor cabinet appears, and every cabinet east of it moves by
        # 200 mm, which is 1.33 x the recovery tolerance.
        #
        # Only the row ORIGIN is snapped to the tile grid. Snapping each bay
        # would quantise the accumulation away and put the wide bays back on
        # the module, which is exactly the coordination problem TIA-942-C's
        # 800 mm minimum creates and BICSI Figure 6-5 draws.
        placed = 0
        for x, bay in bay_origins(program, row):
            if not bay.placed:
                continue          # drawn as a future position, never modelled
            gx, gy = x, y
            box = occupied_aabb(gx, gy, bay.width_mm, bay.depth_mm, facing)
            if any(_aabb_overlap(box, c) for c in obstacles):
                continue  # column occupies this bay
            if not box_in_polygon(program.footprint_mm, *box):
                continue  # bay falls in the L-shaped notch, outside the room
            # A column standing in the service clearance makes the bay unusable
            # even when it misses the cabinet itself -- e.g. leaving only
            # 450mm of front clearance against the TIA-942 1000mm minimum.
            front_zone, rear_zone = clearance_zones(
                gx, gy, bay.width_mm, bay.depth_mm, facing)
            if any(_aabb_overlap(front_zone, c) or _aabb_overlap(rear_zone, c)
                   for c in obstacles):
                continue
            # the clearance has to be inside the building, not merely clear of
            # other equipment. A row far
            # enough north can have its front clearance run out through the
            # l_shaped notch: a cabinet you cannot stand in front of, which the
            # grader reports as "blocked by hall boundary" and is right to.
            if not (box_in_polygon(program.footprint_mm, *front_zone)
                    and box_in_polygon(program.footprint_mm, *rear_zone)):
                continue
            placed += 1
            racks.append(
                Rack(
                    name=f"RACK-{row.row_id}{placed:02d}",
                    x_mm=gx, y_mm=gy,
                    width_mm=bay.width_mm,
                    depth_mm=bay.depth_mm,
                    height_mm=bay.height_mm,
                    rack_units=bay.rack_units,
                    row_id=row.row_id,
                    facing=facing,
                    tile=program.tile.label(gx, gy),
                    z_mm=0.0,
                    bay_kind=bay.kind,
                )
            )

        # advance past this row, then past whichever aisle comes next
        y += row.rack_depth_mm
        y += program.hot_aisle_mm if index % 2 == 0 else program.cold_aisle_mm

    return racks


#: Nominal sensible capacity of one in-row unit. Vendor range for a 600 mm
#: chilled-water CRV/InRow frame is 30-70 kW depending on water temperature and
#: entering air; 50 kW is mid-band. PRACTICE, generator-only — it sizes how many
#: units a hall gets and is never graded.
IN_ROW_CAPACITY_KW = 50.0
IN_ROW_AIRFLOW_M3H = 9000.0


def substitute_in_row_units(racks: Sequence[Rack]
                            ) -> tuple[list[Rack], list[Crah]]:
    """Swap cabinet bays for in-row cooling units, and renumber what is left.

    **Substitution, not extension.** ``CRAH_INROW_WIDTH_WIDE`` is 600 mm — the
    cabinet pitch exactly — so a unit drops into a bay and leaves the row's
    lattice, the tile grid, the containment span, the PDU setback and every
    vertical dimension on A-301 untouched. Appending units to the east end
    instead would push the longest row past the wall on the narrow seeds.

    The unit takes the row's own depth and height rather than the vendor's
    1175 mm, because 1175 matches neither the 1200 nor the 1070 mm cabinet and
    a unit that protrudes 105 mm into a hot aisle is a coordination problem the
    generator invented rather than one the drawing states. The corpus supports
    both flush values — Schneider InRow 600 x 1070 x 1991, Vertiv CRV 600 x
    1200 x 2000 — so the flush choice is sourced, not a convenience.

    Facing is the row's facing: the unit takes hot air in at its rear, out of
    the hot aisle, and discharges cold at its front into the cold aisle, which
    is what every cabinet beside it does.

    Interior bays only. The end bays carry the row tag, the containment end
    door and the PDU setback, and a unit there collides with all three for no
    gain.
    """
    by_row = rows_of(racks)
    per_row = in_row_units_per_row(racks)
    if per_row <= 0:
        return list(racks), []

    kept: list[Rack] = []
    units: list[Crah] = []
    for row_id in sorted(by_row):
        members = sorted(by_row[row_id], key=lambda r: r.x_mm)
        # Bay 0 and bay n-1 are the ends; the interior is bays 1..n-2, so the
        # bay index and the position within the interior are the same number.
        # Taking the midpoint of each of ``take`` equal slices spaces the units
        # along the row rather than bunching them at one end.
        interior = max(0, len(members) - 2)
        take = min(per_row, interior)
        chosen = {round((k + 0.5) * interior / take) for k in range(take)}
        # Never a distributor cabinet. The unit frame is 600 mm and an 800 mm
        # bay is 800 mm; swapping one would leave a 200 mm hole in the row and
        # put a cooling unit where TIA-942-C requires patching. Move the
        # choice to the NEAREST eligible server bay, which is as often west as
        # east — a strict eastward slide loses a unit on the rows where the
        # last eligible bay is behind the wanted one, and the per-row count
        # the manifest reports would then be wrong.
        #
        # Never the second or second-last bay either, once the row is long
        # enough to have one to spare. The end bays carry the row tag, the
        # containment door and the PDU setback, and a unit one bay in prints
        # CRAH-C01 through RACK-C01 — on rows short enough that the
        # unit density lets the spread reach
        # index 1.
        margin = 2 if len(members) >= 8 else 1
        eligible = [i for i, r in enumerate(members)
                    if margin <= i < len(members) - margin
                    and r.bay_kind == "it"]
        if eligible:
            picked: list[int] = []
            for want in sorted(chosen):
                if want in eligible and want not in picked:
                    picked.append(want)
                    continue
                # ``eligible`` ascends, so min() already returns the western
                # candidate on a tie
                alt = min((i for i in eligible if i not in picked),
                          key=lambda i: abs(i - want), default=None)
                if alt is not None:
                    picked.append(alt)
            chosen = set(picked)
        # ``members`` is already west to east, so both counters number their
        # elements in the order the plan tags them and the schedule reads them
        placed = cooled = 0
        for index, rack in enumerate(members):
            if index in chosen:
                cooled += 1
                units.append(Crah(
                    name=f"CRAH-{row_id}{cooled:02d}",
                    x_mm=rack.x_mm, y_mm=rack.y_mm,
                    width_mm=float(specs.CRAH_INROW_WIDTH_WIDE),
                    depth_mm=rack.depth_mm, height_mm=rack.height_mm,
                    capacity_kw=IN_ROW_CAPACITY_KW,
                    airflow_m3h=IN_ROW_AIRFLOW_M3H,
                    facing=rack.facing, z_mm=rack.z_mm,
                    placement="in_row", row_id=row_id))
                continue
            placed += 1
            # renumber: RACK-A01..A21 has to stay contiguous after the swap,
            # the same way it stays contiguous after a column drops a bay
            kept.append(replace(rack, name=f"RACK-{row_id}{placed:02d}"))
    return kept, units


def rows_of(racks: Sequence[Rack]) -> dict[str, list[Rack]]:
    """Cabinets grouped by ``row_id``. Insertion order within a row is kept."""
    out: dict[str, list[Rack]] = {}
    for rack in racks:
        out.setdefault(rack.row_id, []).append(rack)
    return out


#: What the perimeter gallery carries: four units at their schedule rating.
#: Only used to size how many in-row units a hall needs, never graded.
PERIMETER_GALLERY_KW = 4.0 * 100.0


def in_row_units_per_row(racks: Sequence[Rack]) -> int:
    """How many in-row units each row gets.

    Sized from the load the perimeter gallery cannot carry, so the number has a
    reason rather than being a dial: four 100 kW perimeter units against a hall
    of 8 kW cabinets leaves most of the load unserved, and a schedule that says
    so is a schedule an engineer would believe. Clamped to at least one per row
    whenever there is a row to put one in — a CHW consumer in every row is the
    point, not a side effect of the arithmetic.

    Keyed on the placed cabinets rather than on ``program.rows``, because a row
    the L-notch wiped out has no load and needs no cooling.
    """
    n_rows = len({r.row_id for r in racks})
    if not n_rows:
        return 0
    hall_kw = len(racks) * float(specs.RACK_POWER_DENSITY_TYPICAL)
    shortfall = max(0.0, hall_kw - PERIMETER_GALLERY_KW)
    return max(1, math.ceil(math.ceil(shortfall / IN_ROW_CAPACITY_KW) / n_rows))


def row_extents(program: HallProgram) -> list[tuple[float, float]]:
    """The (y_start, y_end) each row actually occupies, snapped as placed."""
    out: list[tuple[float, float]] = []
    y = program.first_aisle_y_mm + program.cold_aisle_mm
    for index, row in enumerate(program.rows):
        _, y = program.tile.snap(0.0, y)
        out.append((y, y + row.rack_depth_mm))
        y += row.rack_depth_mm
        y += program.hot_aisle_mm if index % 2 == 0 else program.cold_aisle_mm
    return out


def aisle_bands(program: HallProgram) -> list[tuple[str, float, float]]:
    """The (kind, y_start, y_end) bands of every aisle, in order.

    Derived from the rows' REALISED extents, so a band is literally the gap
    between two rows. Computing a band as ``start + nominal_aisle_width``
    instead drifts away from the racks whenever a row depth is not a whole
    number of tiles — under mixed 1070/1200 depths the error reaches 130 mm per
    row, which would put perforated tiles (TIA-942 5.11.3) and hot-aisle tray
    routing underneath a cabinet.
    """
    extents = row_extents(program)
    if not extents:
        return []

    bands: list[tuple[str, float, float]] = [
        ("cold", program.first_aisle_y_mm, extents[0][0])
    ]
    for index, ((_, prev_end), (next_start, _)) in enumerate(
        zip(extents, extents[1:])
    ):
        # the gap after an even row is the hot aisle, after an odd row the cold
        bands.append(("hot" if index % 2 == 0 else "cold", prev_end, next_start))

    # trailing aisle after the last row
    last_end = extents[-1][1]
    trailing = (program.hot_aisle_mm if (len(extents) - 1) % 2 == 0
                else program.cold_aisle_mm)
    bands.append(("hot" if (len(extents) - 1) % 2 == 0 else "cold",
                  last_end, last_end + trailing))
    return bands


def hall_depth_required_mm(program: HallProgram) -> float:
    """Total Y extent the row stack consumes, including the trailing aisle."""
    y = program.first_aisle_y_mm + program.cold_aisle_mm
    for index, row in enumerate(program.rows):
        _, y = program.tile.snap(0.0, y)
        y += row.rack_depth_mm
        y += program.hot_aisle_mm if index % 2 == 0 else program.cold_aisle_mm
    return y


def fits_in_hall(program: HallProgram) -> tuple[bool, str]:
    """Whether the laid-out rows actually fit the footprint."""
    if not program.rows:
        return False, "no rows defined"
    min_x, min_y, max_x, max_y = program.bounds_mm()
    need_y = hall_depth_required_mm(program)
    if need_y > (max_y - min_y):
        return False, f"rows need {need_y:.0f}mm of depth, hall has {max_y - min_y:.0f}mm"
    widest = max(
        program.row_start_x_mm + r.n_racks * r.rack_width_mm for r in program.rows
    )
    if widest > (max_x - min_x):
        return False, f"rows need {widest:.0f}mm of width, hall has {max_x - min_x:.0f}mm"
    return True, ""


def column_footprints(program: HallProgram) -> list[tuple[float, float, float, float]]:
    """Column plan AABBs as ``(x0, y0, x1, y1)``, derived from their CENTRES."""
    half = program.column_size_mm / 2.0
    return [
        (cx - half, cy - half, cx + half, cy + half)
        for cx, cy in column_positions(program)
    ]


def column_positions(program: HallProgram) -> list[tuple[float, float]]:
    """Structural column CENTRES on the column grid.

    Note these are centres, not min-corners: callers building box geometry must
    subtract half the column size. :func:`column_footprints` does this for you.

    ``column_offset_mm`` deliberately shifts the column grid off the tile grid.
    That offset is a difficulty knob: with it non-zero, cabinets cannot be
    simultaneously on-tile and clear of columns everywhere, so the agent has to
    choose which constraint to relax and where.
    """
    min_x, min_y, max_x, max_y = program.bounds_mm()
    pitch = program.column_grid_mm
    half = program.column_size_mm / 2.0
    out: list[tuple[float, float]] = []
    nx = int(math.floor((max_x - min_x) / pitch))
    ny = int(math.floor((max_y - min_y) / pitch))
    for i in range(1, nx + 1):
        for j in range(1, ny + 1):
            cx = min_x + i * pitch + program.column_offset_mm
            cy = min_y + j * pitch + program.column_offset_mm
            # the l_shaped knob notches the footprint; a column placed from the
            # bounding box alone lands in the removed corner, outside the room
            if box_in_polygon(program.footprint_mm,
                              cx - half, cy - half, cx + half, cy + half):
                out.append((cx, cy))
    return out
