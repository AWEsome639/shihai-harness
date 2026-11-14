"""Seeded instance sampling with dialable difficulty.

Difficulty is *engineered*, not hoped for. Each knob below makes two constraints
that are individually easy become jointly hard, which is the Solihin Class-3
seam where Text2BIM measured frontier agents degrading into deleting geometry.

Determinism: one ``random.Random(seed)`` drives every choice, so a seed fully
reproduces an instance. Unlike Infinigen (which needs two seeds because
parameter sampling and geometry construction happen in separate scoped contexts)
dcgen samples the whole program up front, so a single seed is sufficient — and
the manifest records every resolved value anyway, so an instance can be rebuilt
even if the sampler changes.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field

from . import specs
from .program import BaySpec, HallProgram, RowSpec, fits_in_hall
from .units import TileGrid

TILE = 600.0

#: West wall to the west face of the cabinet line, on the tile module.
#: Three tiles, not two. The service trunks run north-south in this strip and
#: their risers stand in it, and three lanes abreast with the graded TIA-942
#: separations between them need 1379 mm. At two tiles the busway would have to drop
#: 629 mm into the plenum to clear the tray, and its riser would then cross the
#: tray trunk on the way back up — a hard clash in the answer key.
ROW_START_X_MM = 1800.0


@dataclass
class Knobs:
    """The difficulty dials. All default to the benign setting."""

    #: shift the structural column grid off the tile grid, so cabinets cannot
    #: be simultaneously on-tile and clear of columns everywhere
    column_offset_mm: float = 0.0
    #: sprinkler pitch chosen so it shares no common factor with the tile pitch,
    #: leaving no containment position that clears every head
    sprinkler_coprime: bool = False
    #: mix 1070 and 1200 deep cabinets within one row, so a single row pitch
    #: cannot hold both front and rear clearance
    mixed_rack_depths: bool = False
    #: notch the footprint into an L, breaking row continuity
    l_shaped: bool = False
    #: drive clear height toward the 2600 mm floor so the plenum binds
    tight_headroom: bool = False
    #: tighten the sprinkler grid, so the NFPA 13 obstruction keep-outs leave
    #: less clear plenum between head lines than one service corridor is wide
    dense_sprinkler: bool = False

    def level(self) -> int:
        """How many knobs are engaged."""
        return sum([
            self.column_offset_mm != 0.0,
            self.sprinkler_coprime,
            self.mixed_rack_depths,
            self.l_shaped,
            self.tight_headroom,
            self.dense_sprinkler,
        ])


def knobs_for(difficulty: float, rng: random.Random) -> Knobs:
    """Engage knobs in increasing order of nastiness.

    ``difficulty`` is 0.0 (clean rectangular hall) to 1.0 (all knobs engaged).
    """
    d = max(0.0, min(1.0, float(difficulty)))
    order = [
        lambda k: setattr(k, "column_offset_mm", rng.choice([150.0, 300.0, -150.0])),
        lambda k: setattr(k, "sprinkler_coprime", True),
        lambda k: setattr(k, "tight_headroom", True),
        lambda k: setattr(k, "mixed_rack_depths", True),
        lambda k: setattr(k, "l_shaped", True),
        lambda k: setattr(k, "dense_sprinkler", True),
    ]
    n = round(d * len(order))
    k = Knobs()
    for apply in order[:n]:
        apply(k)
    return k


def _footprint(rng: random.Random, knobs: Knobs) -> list[tuple[float, float]]:
    """Rectangular hall, optionally notched into an L."""
    w = rng.choice([24000.0, 27000.0, 30000.0])
    h = rng.choice([18000.0, 21000.0, 24000.0])
    if not knobs.l_shaped:
        return [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)]
    # notch the top-right corner, on the tile module
    nx = round(w * rng.uniform(0.25, 0.4) / TILE) * TILE
    ny = round(h * rng.uniform(0.25, 0.4) / TILE) * TILE
    return [
        (0.0, 0.0), (w, 0.0), (w, h - ny),
        (w - nx, h - ny), (w - nx, h), (0.0, h),
    ]


#: ANSI/TIA-942-C: cabinets in the MDA, IDA and HDA shall be at least 800 mm
#: wide. BICSI 002 adds that 600 mm is adequate for servers only. 800 does not
#: divide the 600 mm tile, which is the coordination problem BICSI Figure 6-5
#: draws and the reason a distributor cabinet is not just a wider box.
DISTRIBUTOR_WIDTH_MM = 800.0
IT_WIDTH_MM = 600.0


def row_bays(index: int, n_it: int, depth_mm: float,
             usable_width_mm: float) -> tuple[BaySpec, ...]:
    """One row's enclosure sequence, west to east.

    Three things stop this being ``n`` identical cabinets, and all three are
    from issued practice rather than invented:

    * A **horizontal distribution area** of one or two 800 mm distributor
      cabinets. ANSI/TIA-942-C sets the 800 mm minimum; TIA-942 Annex H puts
      the HDA "in the centre of the space it serves", so it goes INSIDE the
      row rather than on its end — which is what makes it displace every
      cabinet east of it instead of merely lengthening the row.
    * A **spare position**, empty by design. UFC 3-580-01 asks for one spare
      cabinet per four utilised; Schneider RD110's rows are "eight IT racks
      bookended by an empty rack". It is drawn and it is not modelled.
    * The HDA's position and size **vary by row**, so recovering one row's
      accumulation does not give you the next one's.

    The sequence is trimmed from the east until it fits ``usable_width_mm``,
    so a wider enclosure costs a bay rather than pushing the row through the
    wall.
    """
    bays: list[BaySpec] = [BaySpec("it", IT_WIDTH_MM, depth_mm)
                           for _ in range(max(1, n_it))]
    n_dist = 1 + (index % 2)
    at = max(1, int(len(bays) * (0.34 + 0.11 * (index % 3))))
    for k in range(n_dist):
        bays.insert(at + k, BaySpec("network", DISTRIBUTOR_WIDTH_MM, depth_mm))
    bays.insert(min(len(bays), int(len(bays) * 0.78)),
                BaySpec("spare", IT_WIDTH_MM, depth_mm))
    while len(bays) > 2 and sum(b.width_mm for b in bays) > usable_width_mm:
        # drop from the east end, and never the spare or a distributor: those
        # are the row's programme, the plain server bays are its fill
        drop = next((i for i in range(len(bays) - 1, -1, -1)
                     if bays[i].kind == "it"), len(bays) - 1)
        bays.pop(drop)
    return tuple(bays)


def cross_aisle_band(row_start_mm: float, longest_run_mm: float
                     ) -> tuple[float, float] | None:
    """Where the circulation spine goes, or ``None`` if the rows are short.

    ANSI/BICSI 002-2019 6.6.4.3 caps a continuous run of adjacent cabinets at
    16 m. A row filling a 24-30 m hall is 14-19 m of cabinets, so a long
    row would be a run the standard says not to build — and every real
    hall plan of that size shows a transverse aisle through the rows for
    exactly this reason.

    The break goes at the midpoint, tile-aligned, which leaves both halves
    under the cap for every footprint the sampler can draw.
    """
    cap = float(specs.CONTINUOUS_CABINET_RUN_MAX)
    if longest_run_mm <= cap:
        return None
    width = float(specs.CROSS_AISLE_WIDTH)
    x0 = row_start_mm + round(longest_run_mm / 2.0 / TILE) * TILE
    return (x0, x0 + width)


def split_at_cross_aisle(bays: tuple[BaySpec, ...], row_start_mm: float,
                         band: tuple[float, float] | None
                         ) -> tuple[BaySpec, ...]:
    """Cut a bay sequence with the cross-aisle band, keeping its EAST edge true.

    Every row resumes at exactly ``band[1]``, so the aisle's east side is a
    straight line across the hall. Its west side steps by up to one bay: a row
    assembled from 600 and 800 mm enclosures cannot end flush on both sides of
    a break, and a plan that drew it flush would be lying about where the
    cabinets are. The gap is therefore never narrower than the nominal aisle,
    which is the direction a clearance may err in.
    """
    if band is None:
        return bays
    x0, x1 = band
    out: list[BaySpec] = []
    cursor = row_start_mm
    inserted = False
    for bay in bays:
        if not inserted and cursor + bay.width_mm > x0:
            depth = bay.depth_mm
            out.append(BaySpec("cross", x1 - cursor, depth, bay.height_mm))
            cursor = x1
            inserted = True
        out.append(bay)
        cursor += bay.width_mm
    return tuple(out)


#: Aisle pairs, in whole tiles, as ``(cold, hot)``.
#:
#: Every one of APC WP-144's four catalogued row pitches makes the two aisles
#: DIFFERENT widths, and so does every reference design in the corpus — RD99
#: puts 1.2 m, 1.8 m and 2.4 m hot aisles in one building, chosen by the pod's
#: kW/rack. Equal 1200/1200 aisles are
#: not a pitch anyone draws, and they make the two aisles indistinguishable by
#: measurement: a submission that swapped hot for cold would be geometrically
#: identical to one that did not.
#:
#: Both pairs here are whole tiles, so successive rows stay on the module and
#: the cold aisle keeps the two full tile rows BICSI 6.6.4.4.1 asks for.
#:
#: ``WIDE_HOT`` is not a mistake. RD99 widens the HOT aisle to 2.4 m for its
#: 40 kW pods "to allow proper airflow", so the wider aisle is not always the
#: cold one — which is why the containment, and not the aisle width, is what
#: identifies a cold aisle on the plan.
PITCH_CLASSES: dict[str, tuple[int, int]] = {
    "B — WIDE COLD": (3, 2),
    "C — WIDE HOT": (2, 3),
}


def sample_program(seed: int, difficulty: float = 0.0) -> HallProgram:
    """Produce one reproducible :class:`HallProgram`.

    The row count is reduced until the stack fits the footprint, so the sampler
    never emits a geometrically impossible instance — that is a sampler bug, not
    a legitimate difficulty.
    """
    rng = random.Random(seed)
    knobs = knobs_for(difficulty, rng)

    footprint = _footprint(rng, knobs)
    xs = [p[0] for p in footprint]
    ys = [p[1] for p in footprint]
    width, height = max(xs) - min(xs), max(ys) - min(ys)

    # 3050 mm, not 2700 or 3400. TIA-942 puts the room minimum at 2600 mm and NFPA 13
    # puts the sprinkler deflector 25.4 mm below the ceiling, so a 2700 mm hall
    # leaves 55 mm of legal overhead plenum — no route fits at all, and the knob
    # stops making the task harder and starts making it a different task.
    # 3400 mm does not bite: it leaves room for two
    # overhead tiers, and a router could simply put everything on the lower one at
    # 2610-2870 mm, below the NFPA pattern-development zone, where none of the
    # obstruction clauses can reach it. 3050 mm puts the *whole* legal overhead
    # band inside that zone — nothing may pass under a head at all, and two
    # tiers no longer fit, so the mains have to go into the access-floor plenum
    # and the hall becomes the two-zone coordination problem a shallow real one
    # is. Still legal: TIA-942 asks for 2600 mm.
    clear_height = 3050.0 if knobs.tight_headroom else rng.choice([3600.0, 4000.0, 4200.0])

    # NFPA 13 caps protection area at 20.9 m2, so pitch must stay <= 4572 mm,
    # and 10.2.5.4.1 puts a 1800 mm FLOOR under head-to-head spacing. The
    # coprime variant shares no factor with the 600 mm tile. The dense variant
    # goes to 2500 mm: 6.25 m2 per head, comfortably legal, and it leaves only
    # 2500 - 2 x 610 - 25 = 1255 mm of plenum clear of the NFPA 13 obstruction
    # keep-outs between head lines — narrower than the 1379 mm a full service
    # corridor needs, so the three services can no longer share one gap. A head
    # relocated a tile off a column still lands 1900 mm from its neighbour,
    # clear of the 1800 mm minimum.
    if knobs.dense_sprinkler:
        sprinkler_pitch = 2500.0
    elif knobs.sprinkler_coprime:
        sprinkler_pitch = 3100.0
    else:
        sprinkler_pitch = 3000.0

    # Fill the hall. Rows of 10-16 cabinets leave a 24-30 m hall two-thirds
    # empty, which reads as a synthetic drawing rather than a data hall and
    # crowds every PDU into one narrow band. The east strip is reserved for the
    # PDU line and circulation.
    east_service_zone = 6 * TILE
    fits = int((width - ROW_START_X_MM - east_service_zone) // TILE)
    n_racks = max(8, min(fits, rng.choice([20, 24, 28])))

    depth_std, depth_alt = 1200.0, 1070.0
    n_rows = rng.choice([4, 6])
    # Drawn BEFORE the rows, because the pitch decides how many of them fit.
    pitch_class = rng.choice(sorted(PITCH_CLASSES))
    cold_tiles, hot_tiles = PITCH_CLASSES[pitch_class]
    usable_width = width - ROW_START_X_MM - east_service_zone

    # The break is sized off the longest UNBROKEN row, before the gap goes in,
    # so every row gets the same spine even though their bay counts differ.
    longest = max(sum(b.width_mm for b in row_bays(i, n_racks, depth_std,
                                                   usable_width))
                  for i in range(n_rows))
    cross = cross_aisle_band(ROW_START_X_MM, longest)

    def build_rows(count: int) -> list[RowSpec]:
        rows: list[RowSpec] = []
        for i in range(count):
            depth = depth_std
            if knobs.mixed_rack_depths and i % 2 == 1:
                depth = depth_alt
            bays = split_at_cross_aisle(
                row_bays(i, n_racks, depth,
                         usable_width - (float(specs.CROSS_AISLE_WIDTH)
                                         if cross else 0.0)),
                ROW_START_X_MM, cross)
            rows.append(RowSpec(
                row_id=chr(ord("A") + i),
                n_racks=sum(1 for b in bays if b.placed),
                rack_depth_mm=depth, bays=bays,
            ))
        return rows

    program = HallProgram(
        seed=seed,
        footprint_mm=footprint,
        clear_height_mm=clear_height,
        raised_floor_mm=rng.choice([600.0, 900.0]),
        tile=TileGrid(pitch_mm=TILE),
        rows=build_rows(n_rows),
        # Both classes are five whole tiles of aisle (3+2 or 2+3), so cold +
        # hot is always 3000 mm and both clear the TIA-942 1000 mm front and
        # 600 mm rear minima. The realised row-to-row repeat is 5400 mm on
        # every seed because layout_racks snaps each row ORIGIN to the tile
        # grid before advancing — NOT because it is cold + hot + two rack
        # depths, which comes to 5270 on the mixed-depth seeds.
        row_start_x_mm=ROW_START_X_MM,
        cold_aisle_tiles=cold_tiles,
        hot_aisle_tiles=hot_tiles,
        pitch_class=pitch_class,
        cross_aisle_mm=cross,
        column_grid_mm=rng.choice([7200.0, 8400.0]),
        column_offset_mm=knobs.column_offset_mm,
        sprinkler_pitch_mm=sprinkler_pitch,
        knobs=asdict(knobs),
    )

    # shrink the row stack until it fits; rows come in pairs to preserve
    # hot/cold alternation
    while n_rows >= 2:
        program.rows = build_rows(n_rows)
        ok, _ = fits_in_hall(program)
        if ok:
            break
        n_rows -= 2

    return program


def manifest(program: HallProgram) -> dict:
    """Everything needed to rebuild and to report this instance.

    ``racks_placed`` is authoritative. ``racks_nominal`` is the per-row sum
    BEFORE bays occupied by columns or falling in an L-shaped notch are
    dropped, so the two differ whenever those knobs engage. Reporting only the
    nominal figure would make the answer-key metadata overstate what is
    actually in the IFC, which would score a perfect agent short.

    In-row cooling units take bays too, and for the same reason they are
    subtracted here rather than being left for a reader to notice: the count
    has to be the count in the file.
    """
    from .program import layout_racks, substitute_in_row_units

    min_x, min_y, max_x, max_y = program.bounds_mm()
    racks, in_row = substitute_in_row_units(layout_racks(program))
    placed = len(racks)
    return {
        "seed": program.seed,
        "knobs": program.knobs,
        "difficulty_level": sum(
            1 for v in program.knobs.values()
            if not (v is False or v is None or v == 0.0)
        ),
        "footprint_mm": program.footprint_mm,
        "hall_width_mm": max_x - min_x,
        "hall_depth_mm": max_y - min_y,
        "clear_height_mm": program.clear_height_mm,
        "raised_floor_mm": program.raised_floor_mm,
        "tile_pitch_mm": program.tile.pitch_mm,
        "pitch_class": program.pitch_class,
        "cross_aisle_mm": list(program.cross_aisle_mm)
                          if program.cross_aisle_mm else None,
        "cold_aisle_mm": program.cold_aisle_mm,
        "hot_aisle_mm": program.hot_aisle_mm,
        "row_pitch_mm": (program.cold_aisle_mm + program.hot_aisle_mm
                         + 2.0 * max(r.rack_depth_mm for r in program.rows)),
        "column_grid_mm": program.column_grid_mm,
        "column_offset_mm": program.column_offset_mm,
        "sprinkler_pitch_mm": program.sprinkler_pitch_mm,
        "rows": [
            {"row_id": r.row_id, "n_racks": r.n_racks,
             "rack_depth_mm": r.rack_depth_mm, "rack_units": r.rack_units,
             # the bay sequence is the row; recording only a count would
             # lose the thing that makes the row non-uniform
             "bays": [{"kind": b.kind, "width_mm": b.width_mm}
                      for b in r.bay_sequence()],
             "row_length_mm": r.length_mm}
            for r in program.rows
        ],
        "racks_nominal": sum(r.n_racks for r in program.rows),
        "racks_placed": placed,
        "in_row_crahs": len(in_row),
        #: kept as an alias of racks_placed so downstream graders that key on
        #: "total_racks" get the count that is actually in the file
        "total_racks": placed,
    }
