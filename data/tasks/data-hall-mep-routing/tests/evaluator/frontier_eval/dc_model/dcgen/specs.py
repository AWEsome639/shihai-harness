"""Sourced constants for the hall generator.

Every value carries its provenance. The distinction that matters:

``REQUIREMENT``
    A published standard mandates it ("shall"). These become **graded
    thresholds** in dcgrade.

``RECOMMENDATION``
    A standard recommends it ("should"). Generator honours it; the grader may
    report it, but must not fail an agent on it.

``PRACTICE``
    Industry/vendor typical. Used **only** for generator geometry — never as a
    graded threshold. Mislabelling practice as requirement is the worst failure
    mode here, because it would fail agents for violating a rule that does not
    exist.

Values sourced during design are recorded below. Anything still marked
``PENDING`` has not been sourced and must not be used as a graded threshold.

**Revision discipline.** Clause numbers differ across TIA-942 revisions. The
original text uses ``5.11.x`` / ``5.3.4.3``; **TIA-942-B-2017 renumbers to
``6.14.x`` / ``6.4.2.4``**. Both are cited where known. One clause also changed
*meaning* between revisions — see :data:`TRAY_BELOW_TILE`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Kind(str, Enum):
    REQUIREMENT = "requirement"
    RECOMMENDATION = "recommendation"
    PRACTICE = "practice"
    PENDING = "pending"


@dataclass(frozen=True)
class Sourced:
    """A constant with its provenance attached."""

    value: float
    unit: str
    kind: Kind
    ref: str
    note: str = ""

    def __float__(self) -> float:
        return float(self.value)

    @property
    def gradeable(self) -> bool:
        """Only codified requirements may become graded thresholds."""
        return self.kind is Kind.REQUIREMENT


# --------------------------------------------------------------------------
# TIA-942 — verified against the standard text during design
# --------------------------------------------------------------------------

FRONT_CLEARANCE = Sourced(
    1000.0, "mm", Kind.REQUIREMENT, "TIA-942 5.11.7.1",
    "'A minimum of 1 m (3 ft) of front clearance shall be provided'; 1.2 m preferable.",
)
REAR_CLEARANCE = Sourced(
    600.0, "mm", Kind.REQUIREMENT, "TIA-942 5.11.7.1",
    "'A minimum of 0.6 m (2 ft) of rear clearance shall be provided'; 1 m preferable.",
)
CLEAR_HEIGHT = Sourced(
    2600.0, "mm", Kind.REQUIREMENT, "TIA-942 5.3.4.3 / TIA-942-B-2017 6.4.2.4",
    "'The minimum height of the room shall be 2.6 m (8.5 ft) from the finished "
    "floor to any obstruction'.",
)
SPRINKLER_CLEARANCE = Sourced(
    460.0, "mm", Kind.REQUIREMENT,
    "TIA-942 5.3.4.3 / TIA-942-B-2017 6.4.2.4 + Annex C",
    "'A minimum of 460 mm (18 in) clearance shall be maintained from water "
    "sprinkler heads'. Sets the top of the usable overhead "
    "containment / tray / busway zone.",
)
CABINET_DOOR_OPEN_AREA = Sourced(
    50.0, "percent", Kind.RECOMMENDATION, "TIA-942 5.11.7.2",
    "'Ventilation through slots or perforations ... to provide a minimum of 50% "
    "open space' — stated as a practice cabinets *can* use, hence recommendation.",
)
RAIL_RECESS = Sourced(
    100.0, "mm", Kind.RECOMMENDATION, "TIA-942 5.11.7.5",
    "'the front rails should be recessed at least 100 mm (4 in)' — 'should'.",
)
TRAY_BELOW_TILE = Sourced(
    20.0, "mm", Kind.REQUIREMENT, "TIA-942-B-2017 8.6.2",
    "MEANING CHANGED BETWEEN REVISIONS. The original text (7.3.2) says "
    "'PREFERABLY with the top of the cable tray or basket 20 mm below the bottom "
    "of the access floor tile' — a recommendation. TIA-942-B-2017 8.6.2 states it "
    "as a 20 mm MINIMUM from the underside of the floor tile to the top of a tray "
    "loaded to 100 % capacity. The B-2017 reading governs, so it is "
    "graded against the current revision. Cite the revision when quoting.",
)
TRAY_MAX_CABLE_DEPTH = Sourced(
    150.0, "mm", Kind.RECOMMENDATION, "TIA-942-B-2017 8.6.1",
    "Maximum recommended cable depth in any tray, under-floor or overhead.",
)
STACKED_TRAY_CLEARANCE = Sourced(
    200.0, "mm", Kind.RECOMMENDATION, "TIA-569-D",
    "Minimum between the top of a tray and the obstruction or tray above "
    "(reduced from 300 mm in the previous revision). Sourced second-hand from a "
    "TIA editor's conference deck, not from TIA-569-D directly — medium "
    "confidence, hence not graded.",
)

# Floor loading — governs suspended busway / tray / pipe-rack loads
FLOOR_LOADING_DISTRIBUTED = Sourced(
    7.2, "kPa", Kind.REQUIREMENT, "TIA-942-B-2017 6.4.2.7",
    "150 lbf/ft2 minimum distributed; 12 kPa (250 lbf/ft2) recommended.",
)
FLOOR_LOADING_HANGING = Sourced(
    1.2, "kPa", Kind.REQUIREMENT, "TIA-942-B-2017 6.4.2.7",
    "25 lbf/ft2 minimum hanging capacity; 2.4 kPa recommended. This is the "
    "figure that governs overhead busway/tray/pipe loads hung from the structure.",
)

#: TIA-942-B-2017 6.14.3 says cabinets SHOULD align with a tile edge. The
#: clause is a "should", not a "shall",
#: and no TIA clause mandates the 600 mm tile
#: module at all. The generator honours alignment; the grader must not fail an
#: agent on it.
TILE_ALIGNMENT = Sourced(
    0.0, "mm", Kind.RECOMMENDATION, "TIA-942-B-2017 6.14.3",
    "'should be aligned with a tile edge' — the modality is "
    "'should'. Generator-only.",
)
RAILS_MIN_RU = Sourced(
    42.0, "U", Kind.RECOMMENDATION, "TIA-942 5.11.7.5",
    "'The rails should provide 42 or more rack units (RUs) of mounting space'.",
)
CABINET_EXTRA_DEPTH = Sourced(
    150.0, "mm", Kind.RECOMMENDATION, "TIA-942 5.11.7.4",
    "'consider using cabinets that are at least 150 mm (6 in) deeper or wider "
    "than the deepest [equipment]'. NOTE: 5.11.7.4 gives NO absolute footprint.",
)

# --------------------------------------------------------------------------
# EIA/ECA-310-E — rack geometry
# --------------------------------------------------------------------------

RACK_UNIT = Sourced(
    44.45, "mm", Kind.REQUIREMENT, "EIA/ECA-310-E",
    "1U = 1.75 in = 44.45 mm.",
)
PANEL_WIDTH = Sourced(
    482.6, "mm", Kind.REQUIREMENT, "EIA/ECA-310-E",
    "19 in front panel width.",
)
HOLE_ROW_SPACING = Sourced(
    465.1, "mm", Kind.REQUIREMENT, "EIA/ECA-310-E",
    "18.312 in between vertical rows of mounting holes.",
)
#: Hole centres within one U, measured from the U boundary (EIA/ECA-310-E).
HOLE_OFFSETS_MM = (6.4, 22.2, 38.1)

# --------------------------------------------------------------------------
# NFPA 13 (2022) — sprinkler layout
# --------------------------------------------------------------------------

SPRINKLER_MAX_PROTECTION_AREA = Sourced(
    20.9032, "m2", Kind.REQUIREMENT, "NFPA 13 (2022) 10.2",
    "225 ft2 exactly = 20.9032 m2. Do NOT round this to 20 m2: that is 4.3 % "
    "STRICTER than the code, and as a graded threshold it would fail an agent "
    "for a legal layout (it caps square pitch at 4472 mm where NFPA allows "
    "4572 mm). A graded threshold must never be tighter than the standard it "
    "cites.",
)
SPRINKLER_DEFLECTOR_MIN_BELOW_CEILING = Sourced(
    25.4, "mm", Kind.REQUIREMENT, "NFPA 13 (2022) 10.2.6.1",
    "Unobstructed construction: deflector 1 in to 12 in below ceiling.",
)
SPRINKLER_DEFLECTOR_MAX_BELOW_CEILING = Sourced(
    304.8, "mm", Kind.REQUIREMENT, "NFPA 13 (2022) 10.2.6.1",
    "Unobstructed construction: deflector 1 in to 12 in below ceiling.",
)

# --------------------------------------------------------------------------
# Floor grid
# --------------------------------------------------------------------------

TILE_PITCH = Sourced(
    600.0, "mm", Kind.PRACTICE, "practice; APC WP 144",
    "No TIA-942 clause mandates "
    "a 600 mm tile module — 'No such requirement exists in ANSI/TIA-942'. "
    "6.14.3 governs placement RELATIVE to whatever grid exists. 600 mm is the "
    "near-universal metric tile and equals one standard rack width.",
)

# --------------------------------------------------------------------------
# Aisle widths — practice, not code. TIA-942 fixes only the per-cabinet
# clearances above; the aisle module comes from vendor/design convention.
# --------------------------------------------------------------------------

COLD_AISLE_MIN_PRACTICE = Sourced(
    1219.0, "mm", Kind.PRACTICE, "APC WP 144; NVIDIA DGX SuperPOD DC design guide; Oracle OCI",
    "4 ft minimum cold aisle. Three independent sources converge.",
)
HOT_AISLE_MIN_PRACTICE = Sourced(
    914.0, "mm", Kind.PRACTICE, "APC WP 144; NVIDIA DGX SuperPOD DC design guide",
    "3 ft minimum hot aisle.",
)
AISLE_NOMINAL_WIDTHS_MM = (900.0, 1200.0, 1500.0, 1800.0)
"""APC nominal containment aisle widths (3/4/5/6 ft). 1200 mm is the typical
2-tile cold aisle; 1800 mm (3 tiles) is used for high density."""

CONTINUOUS_CABINET_RUN_MAX = Sourced(
    16000.0, "mm", Kind.RECOMMENDATION, "ANSI/BICSI 002-2019 6.6.4.3",
    "'The length of adjacent cabinets forming a continuous aisle should not "
    "exceed 16 m (53 ft)', dropping to 6 m (20 ft) where one end of the aisle "
    "is closed off or has no personnel exit. A 'should', so it is honoured by "
    "the generator and NEVER graded — mislabelling it as a requirement is the "
    "failure mode this module exists to prevent. It is the single structural "
    "reason real hall plans show rows broken by transverse cross-aisles rather "
    "than one unbroken run, and APC WP-144 states the same rule at a lower "
    "threshold: 'for long rows of over 10 racks, local regulations may require "
    "that breaks be placed in rows to allow personnel to pass through'.",
)

CROSS_AISLE_WIDTH = Sourced(
    1200.0, "mm", Kind.PRACTICE, "ANSI/BICSI 002 7.4.12.1; IBC 2021 1020.2",
    "Two tiles. BICSI puts general circulation pathways at 1200 mm minimum "
    "with 2400 mm clear overhead; IBC's minimum corridor width is 1118 mm "
    "(44 in). Whole tiles so the rows east of the break stay on the module.",
)

ROW_PITCH_A = Sourced(
    4267.0, "mm", Kind.PRACTICE, "APC WP 144 Figures 6-7",
    "14 ft = 7 tiles (4 ft cold / 3 ft hot). Described as 'the most commonly "
    "used building block for data center row layouts'.",
)
ROW_PITCH_B = Sourced(
    4877.0, "mm", Kind.PRACTICE, "APC WP 144 Figures 6-7",
    "16 ft (6 ft cold / 3 ft hot).",
)

# --------------------------------------------------------------------------
# Other codified requirements (NEC, ASHRAE) — these ARE gradeable, and are kept
# out of the vendor section below so the "never graded" header stays true.
# --------------------------------------------------------------------------

BUSWAY_SUPPORT_SPACING = Sourced(
    1500.0, "mm", Kind.REQUIREMENT, "NFPA 70 (NEC) Article 368",
    "5 ft maximum support spacing for busway. Note the "
    "vendor hanger maximum (Starline, 3000 mm) is LESS restrictive than code — "
    "code governs.",
)

CHW_INSULATION_THICKNESS = Sourced(
    25.4, "mm", Kind.REQUIREMENT, "ASHRAE 90.1 Table 6.8.3",
    "1.0 in minimum for chilled water at 40-60 F, NPS 1.5 to <4 in. "
    "Insulated OD is what clashes, not bare pipe OD.",
)

POWER_TELECOM_SEPARATION = Sourced(
    300.0, "mm", Kind.REQUIREMENT, "ANSI/TIA-942 7.3.1 Table 2",
    "Clear separation between telecommunications cabling and a power feeder of "
    "91+ circuits, or ANY 100 A 415 V three-phase feeder — which is what a "
    "data-hall busway is. The table scales 50/100/150/300 mm with circuit "
    "count and doubles for unshielded power; 300 mm is the value that applies "
    "to busway-vs-tray and is the one graded. Horizontal OR vertical clear "
    "distance satisfies it.",
)

DEDICATED_ELECTRICAL_SPACE = Sourced(
    1800.0, "mm", Kind.REQUIREMENT, "NFPA 70 (NEC) 110.26(E)(1)(a)",
    "The zone equal to the width and depth of a switchboard/panelboard/PDU, "
    "from the floor to 1800 mm above the equipment (or to the structural "
    "ceiling, whichever is lower), is dedicated to the electrical installation: "
    "no piping, ducts or other equipment foreign to it. GRADED ONLY AGAINST "
    "CHILLED-WATER PIPE. Busway and cable tray are part of, or arguable as part "
    "of, the electrical installation, and 110.26(E)(1)(c) Exception explicitly "
    "permits sprinkler protection in the space — grading either would be "
    "stricter than the code.",
)

NEC_WORKING_SPACE_DEPTH = Sourced(
    900.0, "mm", Kind.REQUIREMENT, "NFPA 70 (NEC) 110.26(A)(1) Table",
    "3 ft clear working-space depth in front of energised electrical equipment "
    "likely to require examination while energised, for 0-150 V to ground in "
    "all three conditions. Governs the front of a PDU exactly as TIA-942 "
    "5.11.7.1 governs the front of a cabinet.",
)

NEC_WORKING_SPACE_HEIGHT = Sourced(
    2000.0, "mm", Kind.REQUIREMENT, "NFPA 70 (NEC) 110.26(A)(3)",
    "6.5 ft, or the height of the equipment, whichever is greater. The height "
    "is what stops the working-space rule from reading an overhead tray at "
    "2600 mm as an obstruction to a floor-level clearance.",
)

SERVICE_REACH = Sourced(
    2500.0, "mm", Kind.PRACTICE, "NFPA 70 (NEC) 368.56(B)(2), applied as design basis",
    "How far a run may sit from the row it serves, measured across the row. "
    "NEC caps a busway plug-in cord at 2500 mm; the same figure is used for "
    "tray and pipe as a STATED DESIGN BASIS, not as a code citation for those "
    "two. It lives here rather than in dcgrade because the oracle router and "
    "the connectivity gate have to agree on it, and dcgrade already imports "
    "dcgen while the reverse would be a cycle.",
)

SERVICE_COVERAGE_FRACTION = Sourced(
    0.8, "fraction", Kind.PRACTICE, "dc_model design decision",
    "How much of a row's length must lie within SERVICE_REACH of a run before "
    "the row counts as served. Not a code number and never graded as one — it "
    "is the anti-degenerate guard of the connectivity gate, and it is quoted "
    "to the agent in the task statement because a gate the submission cannot "
    "see is a trap rather than a requirement.",
)

SPRINKLER_OBSTRUCTION_ZONE = Sourced(
    450.0, "mm", Kind.REQUIREMENT, "NFPA 13 (2019, 2022) 9.5.5.2.1 / 10.2.7.2.1.1",
    "18 in. below the deflector: the pattern-development zone. An obstruction "
    "*outside* it is not an obstruction for this rule, which is what lets the "
    "lower of two overhead tiers pass beneath a head while the upper cannot.",
)

SPRINKLER_OBSTRUCTION_OFFSET_MAX = Sourced(
    610.0, "mm", Kind.REQUIREMENT, "NFPA 13 (2019, 2022) 10.2.7.2.1.3",
    "The 'three times rule': minimum horizontal offset from a continuous "
    "obstruction is three times the obstruction's dimension, capped at 24 in. "
    "GRADED IN ITS LENIENT READING. The obstruction dimension is taken as the "
    "run's SMALLEST horizontal extent — its cross-section, not the length of "
    "the run — which gives the smallest offset the clause can demand, and it "
    "applies only inside SPRINKLER_OBSTRUCTION_ZONE. The lenient "
    "reading is deliberate.",
)

OBSTRUCTION_WIDTH_NEEDING_SPRINKLER = Sourced(
    1200.0, "mm", Kind.REQUIREMENT, "NFPA 13 (2019, 2022) 9.5.5.3.1 / 10.2.7.4.2",
    "Sprinklers shall be installed under fixed obstructions over 4 ft wide. "
    "Graded per element, not per bank: whether three runs side by side are one "
    "obstruction is a judgement call, and reading them as one would be "
    "stricter than the clause. A single tray bank wider than this needs a head "
    "beneath it — which is the rule that stops 'one enormous tray' being the "
    "easy answer to a congested plenum.",
)

SPRINKLER_MIN_SPACING = Sourced(
    1800.0, "mm", Kind.REQUIREMENT, "NFPA 13 (2019, 2022) 10.2.5.4.1",
    "6 ft minimum on centre between sprinklers, to stop adjacent heads cold-"
    "soldering each other. Binds against the generator's own relocation of a "
    "head off a column.",
)

SPRINKLER_MAX_FROM_WALL = Sourced(
    2300.0, "mm", Kind.REQUIREMENT, "NFPA 13 (2019, 2022) 10.2.5.2.1",
    "Half the allowable spacing, i.e. 7 ft 6 in where the spacing limit is "
    "15 ft. A grid that satisfies the area cap can still leave a wall strip "
    "unprotected.",
)

TRAY_STACK_HEADROOM = Sourced(
    300.0, "mm", Kind.REQUIREMENT, "ANSI/TIA-569-B, cited in ANSI/TIA-942 7.3.2",
    "12 in minimum access headroom between the top of one tray or runway and "
    "the bottom of the one above. Distinct from STACKED_TRAY_CLEARANCE, which "
    "is the 200 mm TIA-569-D figure sourced second-hand from a conference deck "
    "and therefore not graded; this one is the codified clause TIA-942 itself "
    "points at.",
)

SPRINKLER_MAX_SPACING = Sourced(
    4600.0, "mm", Kind.REQUIREMENT,
    "NFPA 13 Table 10.2.4.2.1(a) (2019, 2022)",
    "15 ft maximum spacing between sprinklers, light hazard, noncombustible "
    "obstructed and unobstructed, hydraulically calculated. Independent of the "
    "225 ft2 area cap: a layout can satisfy the area and still exceed spacing "
    "on one axis.",
)


# --------------------------------------------------------------------------
# Vendor / practice geometry — generator-only, NEVER graded
# --------------------------------------------------------------------------

CRAH_INROW_WIDTH_NARROW = Sourced(
    300.0, "mm", Kind.PRACTICE, "Vertiv CRV; Schneider InRow",
    "300 mm nominal frame width. Vertiv CRV 300 x 1132 x 2000; "
    "Schneider 300 x 1095 x 1991.",
)
CRAH_INROW_WIDTH_WIDE = Sourced(
    600.0, "mm", Kind.PRACTICE, "Vertiv CRV; Schneider InRow",
    "Vertiv CRV 600 x 1175 x 2000 (chilled water); Schneider 600 x 1070 x 1991.",
)
CRAH_INROW_DEPTH = Sourced(
    1175.0, "mm", Kind.PRACTICE, "Vertiv CRV chilled-water datasheet")
CRAH_INROW_HEIGHT = Sourced(
    2000.0, "mm", Kind.PRACTICE, "Vertiv CRV datasheet")
CRAH_SERVICE_CLEARANCE = Sourced(
    914.4, "mm", Kind.PRACTICE, "Vertiv / Schneider in-row installation manuals",
    "36 in clear floor space required in FRONT and BEHIND the unit. Vendor "
    "installation requirement, not a code clearance — generator honours it.",
)

BUSWAY_WIDTH = Sourced(
    128.0, "mm", Kind.PRACTICE, "Starline Track Busway T5 datasheet",
    "T5 width is constant at 128 mm (5.04 in). T3 is 60.33 mm wide.",
)
BUSWAY_HEIGHT = Sourced(
    147.0, "mm", Kind.PRACTICE, "Starline Track Busway T5 datasheet",
    "111 mm at 250/400 A metric; 147 mm at 400 A global and 630 A.",
)

TRAY_MAX_DEPTH = Sourced(
    150.0, "mm", Kind.PRACTICE, "TIA-942-B-2017 8.6.1 (recommended)",
    "Maximum recommended tray depth for data-centre use.",
)
CHW_PIPE_OD = Sourced(
    114.3, "mm", Kind.PRACTICE, "NPS 4 (DN100) steel pipe OD",
    "Representative hall-scale main. ASHRAE 90.1 Table 6.5.4.6 caps flow per "
    "segment; at 2.4 gpm/ton and 10 degF delta-T, NPS 4 suits a mid-size hall.",
)

# --------------------------------------------------------------------------
# Vendor / practice geometry — generator-only, NEVER graded
# --------------------------------------------------------------------------

CABINET_WIDTH = Sourced(
    600.0, "mm", Kind.PRACTICE, "Vertiv VR; APC NetShelter SX AR3300; CPI ZetaFrame",
    "600 mm is the standard narrow cabinet; 800 mm is the wide variant.",
)
CABINET_DEPTH_STD = Sourced(
    1200.0, "mm", Kind.PRACTICE, "APC AR3300 (1991 x 600 x 1200); Rittal VX IT",
    "TIA-942-B 6.14.1 RECOMMENDS 1200 mm depth for new installations.",
)
CABINET_DEPTH_ALT = Sourced(
    1070.0, "mm", Kind.PRACTICE, "Schneider InRow 600 x 1070; Vertiv VR 1115",
)
CABINET_HEIGHT_42U = Sourced(
    1998.0, "mm", Kind.PRACTICE, "Vertiv VR 42U external height",
    "APC AR3300 42U is 1991 mm; CPI ZetaFrame 42U is 2016 mm. All ~2000 mm.",
)
CABINET_STATIC_LOAD = Sourced(
    1363.0, "kg", Kind.PRACTICE, "Vertiv VR (3000 lb)",
    "APC AR3300 1701 kg; CPI ZetaFrame 2268 kg per UL 2416.",
)
CABINET_DOOR_PERFORATION_TYPICAL = Sourced(
    77.0, "percent", Kind.PRACTICE, "Vertiv VR front/rear doors",
    "CPI ZetaFrame 78 %, Rittal VX IT ~85 %. All comfortably exceed the "
    "TIA-942 50 % figure, which is itself only a recommendation.",
)
RACK_POWER_DENSITY_TYPICAL = Sourced(
    8.0, "kW", Kind.PRACTICE, "Uptime Institute Global Data Center Survey 2024",
    "Modal rack density. AI racks are an order of magnitude higher: NVIDIA "
    "GB200 NVL72 is ~120 kW in a 600 x 1068 x 2261 mm enclosure.",
)

# --- floor-mount PDU ------------------------------------------------------

PDU_WIDTH = Sourced(
    762.0, "mm", Kind.PRACTICE, "Vertiv Liebert PPC User Manual, Figures 1-3",
    "762 x 813 x 1959 mm (30 x 32 x 77 in) for 15-125 kVA with one "
    "panelboard; 1118 x 1067 mm for 150-225 kVA with two.",
)
PDU_DEPTH = Sourced(
    813.0, "mm", Kind.PRACTICE, "Vertiv Liebert PPC User Manual, Figures 1-3")
PDU_HEIGHT = Sourced(
    1959.0, "mm", Kind.PRACTICE, "Vertiv Liebert PPC User Manual, Figures 1-3")
PDU_RATING_KVA = Sourced(
    225.0, "kVA", Kind.PRACTICE, "Vertiv Liebert PPC range 15-225 kVA",
    "Hall-scale unit. At the Uptime modal 8 kW/rack a 225 kVA PDU carries "
    "roughly 20 cabinets at 0.9 pf with N+1 headroom.",
)

# --- cold-aisle containment ----------------------------------------------

CONTAINMENT_ROOF_OVERHANG = Sourced(
    200.0, "mm", Kind.PRACTICE,
    "Legrand/Minkels Nexpand Aisle Containment, W1/W2/W3 dimension table",
    "Roof structure total width = aisle width + 200 mm (interior clear width "
    "= aisle width + 91 mm). For a 1200 mm aisle: 1400 mm total.",
)
CONTAINMENT_ROOF_THICKNESS = Sourced(
    55.5, "mm", Kind.PRACTICE,
    "Legrand/Minkels Nexpand 'Corridor - Standard' table, Hr = 55.5 mm",
    "Bare roof beam adds 55.5 mm above the row height; a beam carrying "
    "sensors/cameras/LED adds 135.5 mm.",
)
CONTAINMENT_END_PANEL_THICKNESS = Sourced(
    60.0, "mm", Kind.PRACTICE, "Legrand/Minkels Nexpand sliding-door frame",
    "End-of-aisle door assembly depth.",
)
ESCAPE_ROUTE_MIN_WIDTH = Sourced(
    875.0, "mm", Kind.REQUIREMENT, "IEC TS 62966-3:2021 Table 1 / 4.2.1",
    "Minimum clear escape width inside an aisle containment for up to 5 "
    "persons; 1000 mm for up to 20. Generator honours it; not graded because "
    "occupancy is not modelled.",
)

# --- overhead pathway cross-sections -------------------------------------

TRAY_NOMINAL_WIDTH = Sourced(
    610.0, "mm", Kind.PRACTICE, "NEMA VE 1-2017 cl. 4.3.3 (Widths)",
    "24 in, mid-range of the standard 152/229/305/457/610/762/914 series.",
)
TRAY_OUTSIDE_WIDTH = Sourced(
    683.0, "mm", Kind.PRACTICE, "NEMA VE 1-2017; aluminium I-beam ladder",
    "Outside width = nominal + ~73 mm. 610 nominal -> 683 outside.",
)
TRAY_SIDE_RAIL_HEIGHT = Sourced(
    127.0, "mm", Kind.PRACTICE, "NEMA VE 1-2017 cl. 4.3.4 / side-rail series",
    "5 in rail. TIA-942 7.5.2/7.6.1 caps data-centre tray depth at 150 mm.",
)
CHW_PIPE_INSULATED_OD = Sourced(
    168.3, "mm", Kind.PRACTICE, "ASTM C585 for NPS 4 with 1 in insulation",
    "The insulated envelope is what clashes: 114.3 mm bare OD + 25.4 mm "
    "insulation per ASHRAE 90.1 Table 6.8.3-2 gives a 168.3 mm outside "
    "diameter, which ASTM C585 confirms as the standard 6-5/8 in jacket.",
)


def all_constants() -> dict[str, Sourced]:
    """Every :class:`Sourced` defined in this module, by name."""
    return {
        name: obj
        for name, obj in globals().items()
        if isinstance(obj, Sourced)
    }


def gradeable_thresholds() -> dict[str, Sourced]:
    """The subset dcgrade is permitted to fail an agent on."""
    return {n: s for n, s in all_constants().items() if s.gradeable}


def pending() -> dict[str, Sourced]:
    """Constants not yet sourced. Must be empty before generating real instances."""
    return {n: s for n, s in all_constants().items() if s.kind is Kind.PENDING}
