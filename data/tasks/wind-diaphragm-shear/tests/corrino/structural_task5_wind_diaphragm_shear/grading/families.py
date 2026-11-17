"""families.py - the constraint register: one row per family, its tier, and the documents that
govern it.  `grading/data/register_map.json` and the
grader read this list, so a family can never exist in one and not the others.

Tier rule: a family is HARD when violating it makes the package unsafe
or unbuildable (a member overstressed, a limit exceeded, the design mis-set on the building);
SOFT when it is documentation, drafting or reporting completeness.
"""

# (key, tier, scope, title, governing sources [(document, clause)])
FAMILIES = [
    # ------------------------------------------------------------ set-out and take-off
    ("G1_FOOTPRINT", "hard", "model",
     "Diaphragm set out on the as-built footprint, notch and roof opening deducted",
     [("A-101 Roof Plan Rev C", "key plan"), ("NDDC-A2 Federated Model 2026", "IfcSlab ROOF-A2")]),
    ("G2_PARAPET_HEIGHT", "hard", "model",
     "Parapet and screen-wall heights taken from the as-built model, not the 2024 panel schedule",
     [("NDDC-A2 Federated Model 2026", "IfcWall top elevation"),
      ("SR-2026-118 Roof Line Scan Report", "5.2")]),
    ("G3_PIERS", "hard", "model",
     "Pier schedule matches the as-built panel and opening geometry, narrow legs neglected",
     [("EOR-NDDC-A2-S01", "5.4"), ("NDDC-A2 Federated Model 2026", "IfcOpeningElement")]),
    ("G4_ZONE_ASSIGN", "hard", "model",
     "Every deck zone carries exactly one catalogue deck type over its full area",
     [("S-201 Roof Framing Plan Rev C", "deck zone schedule"), ("EOR-NDDC-A2-S01", "7.1")]),
    ("G5_TYPE_CAP", "hard", "model",
     "Not more than the permitted number of distinct deck types on the roof",
     [("OBD-NDDC-A2", "4.6")]),
    ("G6_CATALOGUE", "hard", "model",
     "Deck types, chord sections and connections taken from the issued catalogues",
     [("CDS-B36 Diaphragm Design Manual", "Table 3"),
      ("KRS-CH Chord and Collector Schedule", "Table 1")]),
    ("G7_CHORD_ASSIGN", "hard", "model",
     "Every chord and collector line of the criteria is assigned a section",
     [("EOR-NDDC-A2-S01", "10.1"), ("S-201 Roof Framing Plan Rev C", "chord and collector lines")]),
    ("G8_SPAN_FACTOR", "hard", "model",
     "Deck shear stiffness adjusted for the deck span actually framed in each zone",
     [("CDS-B36 Diaphragm Design Manual", "Table 3 note 4"),
      ("S-201 Roof Framing Plan Rev C", "joist spacing")]),
    ("G9_ASD_CAPACITY", "hard", "model",
     "Deck capacities reported on the ASD basis using the catalogue safety factor",
     [("CDS-B36 Diaphragm Design Manual", "Table 3 note 2"), ("EOR-NDDC-A2-S01", "3.3")]),
    ("G10_CAP", "hard", "model",
     "Installed cost of the lateral package within the owner's cap",
     [("OBD-NDDC-A2", "4.7")]),
    ("G11_EMBED_SPACING", "hard", "model",
     "Deck-to-panel embeds within the maximum spacing and minimum count",
     [("KRS-CN Connection Vendor Data", "2.3")]),
    ("G12_DXF", "soft", "model", "Deliverable drawing conventions and content",
     [("Spec 01 33 00", "1.7"), ("EOR-NDDC-A2-S01", "11.2")]),
    ("G13_PDF", "soft", "model", "Calculation package format and required sections",
     [("Spec 01 33 00", "1.6"), ("EOR-NDDC-A2-S01", "11.1")]),
    ("G14_SCHEDULE", "soft", "model", "Schedules complete, parseable and free of placeholders",
     [("Spec 01 33 00", "1.5")]),
    ("G15_GCI", "soft", "model", "Mesh convergence of the diaphragm model demonstrated",
     [("EOR-NDDC-A2-S01", "9.7")]),
    # ------------------------------------------------------------ simulation, per scenario
    ("F1_DECK_CAPACITY", "hard", "scenario", "Diaphragm unit shear within the deck ASD capacity",
     [("CDS-B36 Diaphragm Design Manual", "Table 3"), ("EOR-NDDC-A2-S01", "7.2")]),
    ("F2_CHORD_CAPACITY", "hard", "scenario", "Chord force within the section's allowable",
     [("KRS-CH Chord and Collector Schedule", "Table 1"), ("EOR-NDDC-A2-S01", "10.2")]),
    ("F3_COLLECTOR_CAPACITY", "hard", "scenario",
     "Collector force at the re-entrant corner and the roof opening within the allowable",
     [("EOR-NDDC-A2-S01", "10.3"), ("KRS-CH Chord and Collector Schedule", "Table 1")]),
    ("F4_PIER_BASE", "hard", "scenario", "Pier shear within the panel base connection capacity",
     [("KRS-CN Connection Vendor Data", "3.1"), ("EOR-NDDC-A2-S01", "5.7")]),
    ("F5_EMBED_CAPACITY", "hard", "scenario",
     "Deck-to-panel embeds adequate for the panel's roof-level shear",
     [("KRS-CN Connection Vendor Data", "2.1")]),
    ("F6_OVERTURNING", "hard", "scenario",
     "Panel overturning stability under 0.6D + 0.6W, or a tie-down provided",
     [("EOR-NDDC-A2-S01", "5.8"), ("KRS-CN Connection Vendor Data", "4.2")]),
    ("F7_DIAPH_DRIFT", "hard", "scenario", "Diaphragm deflection within the serviceability limit",
     [("EOR-NDDC-A2-S01", "8.2"), ("OBD-NDDC-A2", "4.4")]),
    ("F8_PIER_DRIFT", "hard", "scenario", "Pier drift within the limit",
     [("EOR-NDDC-A2-S01", "8.3")]),
    ("F9_SHEAR_AGREE", "hard", "scenario",
     "Reported diaphragm unit shears agree with re-analysis of the submitted design",
     [("EOR-NDDC-A2-S01", "11.3")]),
    ("F10_REACT_AGREE", "hard", "scenario",
     "Reported shear wall line loads agree with re-analysis of the submitted design",
     [("EOR-NDDC-A2-S01", "11.3")]),
    ("F11_LINELOAD_AGREE", "hard", "scenario",
     "Reported roof-level wall line load carries the parapet cantilever transfer",
     [("EOR-NDDC-A2-S01", "6.4"), ("WT-2026-07 Wind Study", "6.3")]),
    ("F12_CHORD_AGREE", "soft", "scenario", "Reported chord and collector forces agree with re-analysis",
     [("EOR-NDDC-A2-S01", "11.3")]),
    ("F13_DRIFT_AGREE", "soft", "scenario", "Reported deflections agree with re-analysis",
     [("EOR-NDDC-A2-S01", "11.3")]),
    # ------------------------------------------------------------ design basis
    ("D1_GOVERNING", "soft", "model", "Design basis names the governing scenario for each wall line",
     [("Spec 01 33 00", "1.6")]),
    ("D2_KZT", "soft", "model", "Topographic factor taken from the current site study",
     [("TS-2026-02 Topographic Wind Study", "4"), ("EOR-NDDC-A2-S01", "4.3")]),
    ("D3_KE", "soft", "model", "Ground elevation factor applied at the site elevation",
     [("EOR-NDDC-A2-S01", "4.4"), ("ALTA-2025-77 Site Survey", "benchmark")]),
    ("D4_QH", "soft", "model", "Velocity pressure at mean roof height stated and correct",
     [("EOR-NDDC-A2-S01", "4.2"), ("NDDC-AHJ-014 AHJ Letter", "2")]),
    ("D5_SCREEN", "soft", "model", "Screen wall identified with its top elevation",
     [("SR-2026-118 Roof Line Scan Report", "5.2"), ("A-301 Exterior Elevations Rev C", "north")]),
    ("D6_ASBUILT_DOOR", "soft", "model", "As-built opening not on the 2024 shop drawing identified",
     [("NDDC-A2 Federated Model 2026", "IfcOpeningElement"),
      ("PC-S02 Precast Panel Elevations Rev 2", "note 4")]),
    ("D7_ASD", "soft", "model", "Reporting basis and the deck safety factor stated",
     [("EOR-NDDC-A2-S01", "3.3"), ("CDS-B36 Diaphragm Design Manual", "Table 3 note 2")]),
    ("D8_MODEL", "soft", "model", "Diaphragm idealisation and mesh level stated",
     [("EOR-NDDC-A2-S01", "9.0"), ("EOR-NDDC-A2-S01", "9.7")]),
]

KEYS = [f[0] for f in FAMILIES]
HARD = [f[0] for f in FAMILIES if f[1] == "hard"]
SOFT = [f[0] for f in FAMILIES if f[1] == "soft"]
TIER = {f[0]: f[1] for f in FAMILIES}
SCOPE = {f[0]: f[2] for f in FAMILIES}
TITLE = {f[0]: f[3] for f in FAMILIES}
SOURCES = {f[0]: f[4] for f in FAMILIES}
