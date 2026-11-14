"""The oracle overhead route — Task B's answer key and its ``L_opt``.

Task B is underdetermined: many routes are valid. What the generator owes it is
therefore not *the* answer but a **feasible, short, fully compliant** one, so
that ``min(1, L_opt / L_agent)`` has a defensible denominator and so that gate 2
and the clash gate have something real to be exercised against. A seed whose
oracle is infeasible is rejected at build time rather than shipped ungradeable.

**The topology, and why it is the one real coordination uses.** Three services
have to reach every rack row:

    tier 2   spines, running in +Y, one per service, side by side in X
    tier 1   branches, running in +X, one per service per served aisle

Mains perpendicular to and above the branches is not an aesthetic choice: put
spines and branches on the *same* tier and any two services must cross, because
a spine spans every Y and a branch spans every X. There is no single-tier
comb, serpentine or nesting that avoids it for three services. Two tiers make
every crossing a vertical miss.

**What the route has to dodge.**

* **Structural columns.** They span the full clear height, so a run that crosses
  one clashes. This is the constraint agents miss.
* **Sprinkler heads,** but only for a tier whose top rises above
  ``deflector - 460 mm`` (TIA-942 5.3.4.3). With the tier heights below that is
  tier 2 only, and only when the hall is 3600 mm or lower — which is exactly
  what the ``tight_headroom`` knob buys.
* **The hall polygon,** which the ``l_shaped`` knob notches.
* **The connectivity band.** A run dodged too far stops serving its rows, so
  every candidate offset is checked against the same band ``dcgrade`` uses.

Lateral order within a corridor is ``chw | tray | busway`` with the
TIA-942 7.3.1 Table 2 300 mm power-telecom separation sitting between the last
two, so the separation is satisfied by construction rather than by luck.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from . import specs
from .elements import Column, Crah, Rack, RouteSegment, Sprinkler
from .program import HallProgram, aisle_bands, row_extents

#: Access-floor panel thickness, restated here rather than imported: build.py
#: imports this module, so the dependency cannot run the other way.
ACCESS_FLOOR_PANEL_MM = 35.0
from .units import box_in_polygon

#: Clearance held above the TIA-942 room-height minimum before the first tier.
#: Small on purpose: the tier stack is what has to fit under the sprinklers, and
#: every millimetre spent here is one the upper tier does not have.
TIER_BASE_MARGIN_MM = 20.0

#: Vertical gap between the top of tier 1 and the bottom of tier 2.
#:
#: It has to clear the *graded* TIA-942 300 mm power-telecom separation, not
#: merely the 200 mm stacked-tray recommendation, because the tray on one tier
#: sits directly under the busway on the other. Sizing it from
#: :data:`specs.STACKED_TRAY_CLEARANCE` alone would leave 241 mm between a tray branch
#: and the busway spine crossing above it — the tier heights are quoted for the
#: tallest service, and the tray is 41 mm shorter than that.
TIER_GAP_MM = max(float(specs.STACKED_TRAY_CLEARANCE),
                  float(specs.POWER_TELECOM_SEPARATION))

#: Lateral order of the three services in a corridor, low coordinate first.
SERVICE_ORDER: tuple[str, ...] = ("chw_pipe", "cable_tray", "busway")

SERVICE_WIDTH_MM: dict[str, float] = {
    "chw_pipe": float(specs.CHW_PIPE_INSULATED_OD),
    "cable_tray": float(specs.TRAY_OUTSIDE_WIDTH),
    "busway": float(specs.BUSWAY_WIDTH),
}
SERVICE_HEIGHT_MM: dict[str, float] = {
    "chw_pipe": float(specs.CHW_PIPE_INSULATED_OD),
    "cable_tray": float(specs.TRAY_SIDE_RAIL_HEIGHT),
    "busway": float(specs.BUSWAY_HEIGHT),
}
SERVICE_LABEL: dict[str, str] = {
    "chw_pipe": "chilled_water",
    "cable_tray": "telecom",
    "busway": "power",
}

#: Order lanes are placed in: widest first, because it is the hardest to fit,
#: and busway straight after the tray so the graded 300 mm separation is
#: resolved while there is still room to move.
_PLACEMENT_ORDER: tuple[str, ...] = ("cable_tray", "busway", "chw_pipe")

#: Clear gap held between two lanes on the same tier that have no graded
#: separation between them. Maintenance access, not a code number.
LANE_GAP_MM = 100.0

#: Extra slack added to every keep-out when searching for a clear lane, so an
#: oracle run never merely grazes an obstruction it was supposed to dodge.
KEEPOUT_SLACK_MM = 25.0

#: How far the search may slide a lane off its nominal centre, and in what
#: increment.
_SEARCH_STEP_MM = 50.0
_SEARCH_LIMIT_MM = 6000.0


#: Clear space held between the west wall and the first trunk lane.
WEST_STRIP_MARGIN_MM = 200.0


def trunk_lanes(min_x: float) -> dict[str, tuple[float, float]]:
    """The three trunk lanes' x extents, side by side in the west strip.

    Side by side, not stacked. Stacking does not work: separating the busway
    from the tray in z puts it 629 mm down, its riser then crosses the tray
    trunk on the way up, and that is a hard clash in the answer key. The strip
    is :data:`sampler.ROW_START_X_MM` wide, which holds all three lanes
    abreast with the graded TIA-942 separations between them and leaves every
    riser standing clear of both the cabinets and the other trunks.
    """
    out: dict[str, tuple[float, float]] = {}
    cursor = min_x + WEST_STRIP_MARGIN_MM
    for service in ("cable_tray", "busway", "chw_pipe"):
        if out:
            cursor += max(required_gap_mm(service, other) for other in out)
        out[service] = (cursor, cursor + SERVICE_WIDTH_MM[service])
        cursor += SERVICE_WIDTH_MM[service]
    return out


def trunk_top_mm() -> float:
    """Top of every trunk lane: TIA-942-B 8.6.2's clearance below the tile."""
    return -(ACCESS_FLOOR_PANEL_MM + float(specs.TRAY_BELOW_TILE))


class OracleInfeasible(RuntimeError):
    """No compliant oracle route exists for this program. Reject the seed."""


def required_gap_mm(a: str, b: str) -> float:
    """Clear lateral distance two co-planar services must hold.

    Only one pair has a sourced number: TIA-942 7.3.1 Table 2 puts 300 mm
    between telecommunications cabling and a power feeder of the size a
    data-hall busway is. Everything else is a maintenance allowance.
    """
    if {a, b} == {"cable_tray", "busway"}:
        return float(specs.POWER_TELECOM_SEPARATION)
    return LANE_GAP_MM


@dataclass(frozen=True)
class Tiers:
    """The two horizontal planes the route occupies."""

    branch_z_mm: float
    spine_z_mm: float
    height_mm: float
    #: does a run on this tier have to stay out from under a sprinkler head?
    branch_dodges_heads: bool
    spine_dodges_heads: bool
    deflector_z_mm: float
    #: the mains run in the access-floor plenum instead of overhead, because
    #: the overhead zone cannot hold two tiers
    spine_underfloor: bool = False

    @property
    def branch_top_mm(self) -> float:
        return self.branch_z_mm + self.height_mm

    @property
    def spine_top_mm(self) -> float:
        return self.spine_z_mm + self.height_mm


def tiers_for(program: HallProgram) -> Tiers:
    """Where the branches sit, and whether the sprinklers constrain them.

    One topology for every hall: **branches overhead, trunk under the floor,
    risers joining them.** Two overhead tiers are worse in both
    directions. Where they fit they are unnecessary, and where they do not
    — a 3050 mm hall — there is no single-tier arrangement that keeps three
    services connected, because a trunk spans every Y, a branch spans every X,
    and any two of them must cross. TIA-942-B 8.6.2 puts mains in the
    access-floor plenum for exactly this reason.

    Raises :class:`OracleInfeasible` when the hall cannot hold the arrangement.
    """
    height = max(SERVICE_HEIGHT_MM.values())
    branch_z = float(specs.CLEAR_HEIGHT) + TIER_BASE_MARGIN_MM
    deflector = (program.clear_height_mm
                 - float(specs.SPRINKLER_DEFLECTOR_MIN_BELOW_CEILING))
    if branch_z + height > deflector:
        raise OracleInfeasible(
            f"clear height {program.clear_height_mm:.0f}mm leaves "
            f"{deflector - branch_z:.0f}mm above the TIA-942 room minimum, "
            f"which cannot hold even one {height:.0f}mm tier"
        )
    top = trunk_top_mm()
    deepest = -(top - max(SERVICE_HEIGHT_MM.values()))
    if deepest > program.raised_floor_mm - ACCESS_FLOOR_PANEL_MM:
        raise OracleInfeasible(
            f"the trunk reaches {deepest:.0f}mm below finished floor and the "
            f"plenum is only "
            f"{program.raised_floor_mm - ACCESS_FLOOR_PANEL_MM:.0f}mm deep"
        )
    lanes = trunk_lanes(program.bounds_mm()[0])
    if max(hi for _, hi in lanes.values()) > program.row_start_x_mm:
        raise OracleInfeasible(
            "the three trunk lanes do not fit in the west service strip, so a "
            "riser would have to stand inside the cabinet line")
    # TIA-942 keeps 460 mm clear directly below a head; NFPA 13 keeps the
    # 450 mm pattern-development zone clear of obstructions in plan by up to
    # 610 mm. A branch whose top rises into either has to dodge, and the NFPA
    # offset is the larger keep-out, so it is the one the search uses.
    ceiling = deflector - max(float(specs.SPRINKLER_CLEARANCE),
                              float(specs.SPRINKLER_OBSTRUCTION_ZONE))
    return Tiers(
        # z of the trunk's UNDERSIDE, so spine_top_mm means what it says
        branch_z_mm=branch_z, spine_z_mm=top - max(SERVICE_HEIGHT_MM.values()),
        height_mm=height,
        branch_dodges_heads=(branch_z + height) > ceiling,
        spine_dodges_heads=False, deflector_z_mm=deflector,
        spine_underfloor=True,
    )


# --------------------------------------------------------------------------
# keep-outs
# --------------------------------------------------------------------------


def _blocked_intervals(centres: Iterable[float], half_extent_mm: float
                       ) -> list[tuple[float, float]]:
    return [(c - half_extent_mm, c + half_extent_mm) for c in centres]


def _clear_of(intervals: Sequence[tuple[float, float]],
              low: float, high: float) -> bool:
    return all(high <= lo or hi <= low for lo, hi in intervals)


def nfpa_offset_mm(service: str) -> float:
    """NFPA 13 10.2.7.2.1.3 clearance a run owes a head, for its cross-section.

    Three times the obstruction's dimension, capped at 610 mm. Per service,
    because the cap only binds for the wide ones: the 683 mm tray needs the
    full 610, the 128 mm busway only 384.
    """
    return min(3.0 * SERVICE_WIDTH_MM[service],
               float(specs.SPRINKLER_OBSTRUCTION_OFFSET_MAX))


def _column_keepouts(axis: int, columns: Sequence[Column]
                     ) -> list[tuple[float, float]]:
    """A run along one axis is stopped by anything sharing its other-axis band."""
    return [((column.x_mm if axis == 0 else column.y_mm) - KEEPOUT_SLACK_MM,
             (column.x_mm if axis == 0 else column.y_mm) + column.size_mm
             + KEEPOUT_SLACK_MM)
            for column in columns]


def _head_keepouts(axis: int, sprinklers: Sequence[Sprinkler],
                   offset_mm: float) -> list[tuple[float, float]]:
    """Where a run may not sit relative to each head, for one service.

    ``offset_mm`` is the NFPA horizontal clearance, not the head's own width.
    Dodging a head by its 25 mm body satisfies the clash gate and violates the
    obstruction clause by 585 mm.
    """
    out: list[tuple[float, float]] = []
    for head in sprinklers:
        base = head.x_mm if axis == 0 else head.y_mm
        half = head.body_mm / 2.0 + offset_mm + KEEPOUT_SLACK_MM
        out.append((base - half, base + half))
    return out


def _candidates(nominal_mm: float) -> list[float]:
    """Corridor centres to try, nearest the nominal first."""
    out = [nominal_mm]
    step = _SEARCH_STEP_MM
    while step <= _SEARCH_LIMIT_MM:
        out.extend((nominal_mm - step, nominal_mm + step))
        step += _SEARCH_STEP_MM
    return out


# --------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------


@dataclass
class RoutePlan:
    segments: list[RouteSegment] = field(default_factory=list)
    #: per branch: ``{service: (low, high)}`` of the lane it occupies in Y
    branch_lanes: list[dict[str, tuple[float, float]]] = field(default_factory=list)
    #: ``{service: (low, high)}`` of the spine lanes in X
    spine_lanes: dict[str, tuple[float, float]] = field(default_factory=dict)
    tiers: Tiers | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def l_opt_mm(self) -> float:
        """Total routed centreline length, the same figure ``dcgrade`` sums."""
        return sum(s.centreline_length_mm for s in self.segments)


#: Lattice step for enumerating branch positions across the hall.
BRANCH_SCAN_STEP_MM = 50.0


def feasible_centres(program: HallProgram,
                     keepouts: dict[str, list[tuple[float, float]]],
                     along: tuple[float, float], axis: int
                     ) -> list[tuple[float, dict[str, tuple[float, float]]]]:
    """Every corridor position that fits, scanned across the hall.

    A lattice, not a shortlist of aisle and row centres. Sliding each
    shortlisted candidate to the nearest clear position and then asking what it
    covers is the wrong way round: the positions that cover the most
    rows are the ones aligned to the *sprinkler* bays, and those have nothing
    to do with where the aisles are. Scanning finds two-branch solutions where
    the shortlist needs three.
    """
    offsets, width = corridor_offsets()
    lo_bound, hi_bound = (program.bounds_mm()[1], program.bounds_mm()[3])
    if axis == 0:
        lo_bound, hi_bound = program.bounds_mm()[0], program.bounds_mm()[2]
    out: list[tuple[float, dict[str, tuple[float, float]]]] = []
    centre = lo_bound + width / 2.0
    while centre <= hi_bound - width / 2.0:
        lanes = {s: (centre + offsets[s] - SERVICE_WIDTH_MM[s] / 2.0,
                     centre + offsets[s] + SERVICE_WIDTH_MM[s] / 2.0)
                 for s in SERVICE_ORDER}
        if all(_clear_of(keepouts[s], *lanes[s]) for s in SERVICE_ORDER):
            box = ((along[0], centre - width / 2.0, along[1], centre + width / 2.0)
                   if axis == 1 else
                   (centre - width / 2.0, along[0], centre + width / 2.0, along[1]))
            if box_in_polygon(list(program.footprint_mm), *box):
                out.append((centre, lanes))
        centre += BRANCH_SCAN_STEP_MM
    return out


def _reaches(lanes: dict[str, tuple[float, float]],
             row: tuple[float, float]) -> bool:
    """Is every service of this branch within reach across the row?

    The same test :func:`dcgrade.score.connectivity` applies, from the same
    constant. Branches run the length of the row, so the coverage-along term is
    satisfied by construction and only the across-reach can fail.
    """
    reach = float(specs.SERVICE_REACH)
    return all(lo <= row[1] + reach and hi >= row[0] - reach
               for lo, hi in lanes.values())


def _cover_rows(placements: list[tuple[float, dict[str, tuple[float, float]]]],
                extents: Sequence[tuple[float, float]]) -> list[int]:
    """The fewest branches that reach every row. Exact, not greedy-by-size.

    A corridor's reach is an interval in y, and the rows are ordered in y, so
    the set of rows one branch covers is always *contiguous*. That makes this
    interval covering, where the textbook greedy — take the interval that
    covers the first uncovered row and extends furthest — is provably optimal.
    Maximum-coverage greedy is not: on a six-row hall it happily takes a branch
    covering rows 1-4, is then left with rows 0 and 5 at opposite ends of the
    room, and spends three branches where {0,1,2} + {3,4,5} needs two.
    """
    _, width = corridor_offsets()
    covers = [sorted(i for i, row in enumerate(extents) if _reaches(lanes, row))
              for _, lanes in placements]
    chosen: list[int] = []
    target = 0
    while target < len(extents):
        # two branches closer together than one corridor width would put their
        # risers in the same place, which is a hard clash in the answer key
        reaching = [
            j for j, c in enumerate(covers)
            if c and c[0] <= target <= c[-1]
            and all(abs(placements[j][0] - placements[k][0]) >= width
                    for k in chosen)
        ]
        if not reaching:
            return []
        best = max(reaching, key=lambda j: covers[j][-1])
        chosen.append(best)
        target = covers[best][-1] + 1
    return chosen


def _row_x_span(racks: Sequence[Rack], row_ids: Sequence[str]
                ) -> tuple[float, float] | None:
    members = [r for r in racks if r.row_id in row_ids]
    if not members:
        return None
    return (min(r.x_mm for r in members),
            max(r.x_mm + r.width_mm for r in members))


def _serves(run_lo: float, run_hi: float, rows: Sequence[tuple[float, float]],
            margin_mm: float) -> bool:
    """Does a run at this perpendicular position stay inside dcgrade's band?"""
    return all(run_hi > lo - margin_mm and run_lo < hi + margin_mm
               for lo, hi in rows)


def corridor_offsets() -> tuple[dict[str, float], float]:
    """Each service's centre relative to the corridor centre, and the width.

    Order matters and it is not arbitrary. ``chw | tray | busway`` puts the
    service with the LARGEST NFPA obstruction offset — the 683 mm tray, which
    owes the full 610 mm — in the middle, where the clear span between two
    sprinkler lines is widest, and the 128 mm busway that owes only 384 mm on
    the outside. Any other order needs a wider bay than exists.
    """
    cursor = 0.0
    edges: dict[str, float] = {}
    for service in SERVICE_ORDER:
        cursor += _lead_gap(service)
        edges[service] = cursor
        cursor += SERVICE_WIDTH_MM[service]
    half = cursor / 2.0
    return ({s: edges[s] + SERVICE_WIDTH_MM[s] / 2.0 - half for s in SERVICE_ORDER},
            cursor)


def _lead_gap(service: str) -> float:
    index = SERVICE_ORDER.index(service)
    if index == 0:
        return 0.0
    return required_gap_mm(SERVICE_ORDER[index - 1], service)


def _place_lanes(nominal_mm: float, keepouts: dict[str, list[tuple[float, float]]],
                 polygon: Sequence[tuple[float, float]],
                 along: tuple[float, float], axis: int,
                 rows: Sequence[tuple[float, float]] = (),
                 margin_mm: float = 0.0) -> dict[str, tuple[float, float]] | None:
    """Slide the whole three-service corridor to the nearest position that fits.

    Placed as a corridor, not service by service. Independent placement looks
    more flexible and is strictly worse: each service takes the first clear
    position walking out from the nominal, so the tray would land at the edge
    of a bay and shove the busway into the next one. Lane spread comes out at
    2200-3400 mm, and a branch that wide reaches two rack rows where a
    1379 mm corridor reaches three or four — which is the difference between a
    twelve-run oracle and a nine-run one, and the whole objective term rides on
    ``L_opt`` being the shorter of the two.

    ``axis`` is the axis the corridor is offset along: 1 for a branch running
    in X, 0 for a spine running in Y. ``rows`` is a soft preference — a
    position that serves its rows is chosen over one that does not.
    """
    offsets, width = corridor_offsets()

    def spans(centre: float) -> dict[str, tuple[float, float]]:
        return {s: (centre + offsets[s] - SERVICE_WIDTH_MM[s] / 2.0,
                    centre + offsets[s] + SERVICE_WIDTH_MM[s] / 2.0)
                for s in SERVICE_ORDER}

    for require_serves in (True, False) if rows else (False,):
        for centre in _candidates(nominal_mm):
            lanes = spans(centre)
            if not all(_clear_of(keepouts[s], *lanes[s]) for s in SERVICE_ORDER):
                continue
            if require_serves and not all(
                    _serves(lo, hi, rows, margin_mm) for lo, hi in lanes.values()):
                continue
            lo = centre - width / 2.0
            hi = centre + width / 2.0
            box = ((along[0], lo, along[1], hi) if axis == 1
                   else (lo, along[0], hi, along[1]))
            if not box_in_polygon(list(polygon), *box):
                continue
            return lanes
    return None


def plan_route(program: HallProgram, racks: Sequence[Rack],
               columns: Sequence[Column],
               sprinklers: Sequence[Sprinkler],
               crahs: Sequence[Crah] = ()) -> RoutePlan:
    """Build the oracle route, or raise :class:`OracleInfeasible`.

    ``crahs`` are the chilled-water consumers. They are not decoration: gate 2
    asks the CHW network to reach every air handler, and the in-row units
    are reached by the row branches for free while the perimeter gallery on the
    south wall is not — the nearest branch is 3400 mm from the nearest unit and
    9-12 m from the furthest. So the oracle owes them a spur.
    """
    if not racks:
        raise OracleInfeasible("no cabinets placed, so there is nothing to serve")

    tiers = tiers_for(program)
    plan = RoutePlan(tiers=tiers)
    extents = row_extents(program)
    min_x, _, _, _ = program.bounds_mm()

    # Only the heads a run can actually obstruct. NFPA 13's pattern-development
    # zone is the 450 mm BELOW a deflector, so a head hanging under a
    # containment lid at 1890 mm is not obstructed by a branch at 2620 mm — the
    # branch is above it, not under it. Passing every head into the keep-out
    # search would make those in-aisle heads block lanes they cannot see.
    
    overhead_heads = [h for h in sprinklers
                      if h.z_mm >= tiers.branch_z_mm + tiers.height_mm]

    # ---- branches: as few lane trios as reach every row, running +X on tier 1
    branch_keepouts = {
        service: (_column_keepouts(1, columns)
                  + (_head_keepouts(1, overhead_heads, nfpa_offset_mm(service))
                     if tiers.branch_dodges_heads else []))
        for service in SERVICE_ORDER
    }
    full_span = (min(r.x_mm for r in racks),
                 max(r.x_mm + r.width_mm for r in racks))

    placements = feasible_centres(program, branch_keepouts, full_span, axis=1)
    chosen = _cover_rows(placements, extents)
    if not chosen:
        raise OracleInfeasible(
            "no set of tier-1 branches reaches every rack row: columns and "
            "sprinkler keep-outs leave no clear lane within "
            f"{float(specs.SERVICE_REACH):.0f}mm of some row"
        )
    branch_spans: list[tuple[float, float]] = []
    for index in chosen:
        plan.branch_lanes.append(placements[index][1])
        branch_spans.append(full_span)

    # ---- trunk: one lane per service under the floor, in the west strip, each
    # directly beneath the riser that will feed its branches
    riser_x = trunk_lanes(min_x)
    top = trunk_top_mm()
    plan.spine_lanes = riser_x

    flat = [span for lanes in plan.branch_lanes for span in lanes.values()]
    y_lo, y_hi = min(lo for lo, _ in flat), max(hi for _, hi in flat)

    # ---- emit: branch (X, overhead) + riser (Z, west strip) + trunk (Y, plenum)
    for index, lanes in enumerate(plan.branch_lanes, start=1):
        for service, (lo, hi) in lanes.items():
            x0, x1 = riser_x[service]
            end_x = full_span[1]
            plan.segments.append(RouteSegment(
                kind=service, name=f"{_TAG[service]}-B{index:02d}",
                x_mm=x0, y_mm=lo, z_mm=tiers.branch_z_mm,
                length_mm=end_x - x0, width_mm=hi - lo,
                height_mm=SERVICE_HEIGHT_MM[service],
                axis="X", service=SERVICE_LABEL[service],
            ))
            plan.segments.append(RouteSegment(
                kind=service, name=f"{_TAG[service]}-R{index:02d}",
                x_mm=x0, y_mm=lo, z_mm=top,
                length_mm=tiers.branch_z_mm - top, width_mm=x1 - x0,
                height_mm=hi - lo, axis="Z", service=SERVICE_LABEL[service],
            ))

    # ---- the CHW spur to any consumer the branches do not already reach
    #
    # In practice that is the perimeter gallery: it stands on the south wall,
    # outside the rack field the branches comb, and no branch comes within the
    # graded reach of it. The spur runs in the access-floor plenum at the
    # trunk's own elevation, in the clear band between the gallery and the
    # first rack row — there is no rack, column or containment down there, and
    # the access floor itself is excused, so it dodges nothing and costs only
    # its length. The trunk is extended south to meet it rather than a second
    # network being started, because gate 2 wants ONE network per service.
    spur = _chw_spur(crahs, riser_x["chw_pipe"])
    # Only chilled water is extended south. Lengthening the shared ``y_lo``
    # would give the tray and the busway a 3.5-7.1 m stub apiece running to a
    # consumer neither of them has,
    # and L_opt is the denominator of every agent's objective term.
    # An answer key that carries route it does not need makes every submission
    # look shorter than it is.
    chw_y_lo = min(y_lo, spur[0]) if spur is not None else y_lo

    for service, (x0, x1) in riser_x.items():
        height = SERVICE_HEIGHT_MM[service]
        lo = chw_y_lo if service == "chw_pipe" else y_lo
        plan.segments.append(RouteSegment(
            kind=service, name=f"{_TAG[service]}-T01",
            x_mm=x0, y_mm=lo, z_mm=top - height,
            length_mm=y_hi - lo, width_mm=x1 - x0,
            height_mm=height, axis="Y", service=SERVICE_LABEL[service],
        ))

    if spur is not None:
        spur_y, spur_x1 = spur
        # start at the trunk's EAST face, not at its west one. Two boxes that
        # share a corner interpenetrate, and a hard clash in the answer key
        # fails the reference gate; two that abut are joined — gate 2's 250 mm
        # joint tolerance is satisfied by a zero-overlap touch, and the clash
        # detectors need more than a millimetre on all three axes.
        _, x0 = riser_x["chw_pipe"]
        height = SERVICE_HEIGHT_MM["chw_pipe"]
        plan.segments.append(RouteSegment(
            kind="chw_pipe", name="CHW-S01",
            x_mm=x0, y_mm=spur_y, z_mm=top - height,
            length_mm=spur_x1 - x0, width_mm=SERVICE_WIDTH_MM["chw_pipe"],
            height_mm=height, axis="X", service=SERVICE_LABEL["chw_pipe"],
        ))
        plan.notes.append(
            f"CHW spur in the plenum at y={spur_y:.0f}mm serving the perimeter "
            f"gallery, which no row branch reaches")

    plan.notes.append(
        f"{len(plan.branch_lanes)} branch corridors overhead at "
        f"z={tiers.branch_z_mm:.0f}mm, risers in the west strip, trunk in the "
        f"access-floor plenum (ANSI/TIA-942-B 8.6.2); the sprinkler standoff "
        f"{'does' if tiers.branch_dodges_heads else 'does not'} constrain the "
        f"branches"
    )
    return plan


def _chw_spur(crahs: Sequence[Crah], lane_x: tuple[float, float]
              ) -> tuple[float, float] | None:
    """``(y, x_east)`` of the plenum spur, or ``None`` if none is needed.

    ``y`` is placed so the spur lies within :data:`specs.SERVICE_REACH` of
    every unit it has to serve, measured the way ``dcgrade`` measures it, and
    clear of the units themselves in plan. The gallery is a single line of
    boxes on one wall, so one straight run reaches all of them; if a future
    layout scatters the units this returns ``None`` rather than emitting a run
    that only looks like it serves them.
    """
    unreached = [c for c in crahs if c.placement != "in_row"]
    if not unreached:
        return None
    reach = float(specs.SERVICE_REACH)
    north = max(c.y_mm + c.depth_mm for c in unreached)
    south = min(c.y_mm for c in unreached)
    width = SERVICE_WIDTH_MM["chw_pipe"]
    # just north of the gallery, which is the clear band between it and the
    # first rack row, and comfortably inside the reach of every unit
    y = north + LANE_GAP_MM
    if y + width > south + reach:
        return None
    x_east = max(c.x_mm + c.width_mm for c in unreached)
    if x_east <= lane_x[0]:
        return None
    return y, x_east


_TAG = {"cable_tray": "CT", "busway": "BW", "chw_pipe": "CHW"}
