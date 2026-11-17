You are working in `/filesystem/final_batch4__structural_task7_roof_deck_design__blind`. The project documents are under `/filesystem/final_batch4__structural_task7_roof_deck_design__blind/` (read `task.md` there first); write your deliverable to `/filesystem/final_batch4__structural_task7_roof_deck_design__blind/output/submission.zip`.

Write a first COMPLETE version of `output/submission.zip` within your first ~100 steps, then iterate on it in place: the last version on disk when the run ends is what gets graded, and a run that exhausts its step or token budget with no file scores 0. An imperfect early submission earns partial credit.

# DC-2 Helena data centre — roof deck design for gravity and lateral loads

You are the structural engineer for Building A of the DC-2 data centre in Lewis and Clark County, Montana.
The roof framing is fixed and is not yours to change: open-web steel joists at 6 ft-0 in on centre spanning
30 ft between joist girders, over a 240 ft by 180 ft footprint, with the deck spanning across the joists in
three-span continuous conditions. Your job is the **roof deck**: its gage, its support-fastener pattern, its
sidelap connectors, and where each of those changes across the roof.

The project documents are in `project/`; the published standards the project cites are in `library/`; blank
deliverable templates are in `templates/`. The roof carries a rooftop equipment enclosure, equipment
screens, rooftop units, parapets and framed openings — the three-dimensional record of all of it is the
structural model in `project/15_model/`, and the two-dimensional drawings do not all agree with it or with
each other. Work out which documents govern and what they require.

The deck must carry the gravity loads, including the snow drifts the roof's own obstructions create; it must
carry the in-plane diaphragm shears from wind and seismic to the braced frames; its attachment must hold the
roof down under wind uplift; and among designs that do all of that, the lowest **installed cost** at the cost
plan rates is preferred.

The deck's shear stiffness is a result of your design, and the distribution of the roof force between the
eight braced frames depends on it — a stiffer deck does not simply have more capacity in reserve.

## Deliverable

Write one file, `output/submission.zip`, containing at its root:

### 1. `deck_design.xlsx` — the design (sheet names and column headers exactly as below)

All plan coordinates are **project grid coordinates in feet**: x along grids 1→9, y along grids A→G, origin
at grid 1-A.

* **Zones**: `zone_id`, `x0_ft`, `y0_ft`, `x1_ft`, `y1_ft`, `gage`, `support_pattern`, `sidelap_type`,
  `sidelap_per_span`. One row per **rectangle**. A zone may be made of several rectangles — repeat the
  `zone_id`, and every rectangle of a zone must carry the same gage, pattern, sidelap type and count. The
  rectangles must not overlap and together must cover every part of the roof that receives deck.
  `support_pattern` is written `36/n` for n fasteners across the 36 in cover width at each support.
* **DesignBasis**: `key`, `value` — one row per key, all of:
  `PG_PSF`, `CE`, `CT`, `IS`, `PF_PSF`, `GAMMA_PCF`, `LR_PSF`, `QH_PSF`, `A_ZONE_FT`, `SDS`, `IE`, `R`,
  `W_SEISMIC_PSF`, `FPX_KIP`, `WIND_X_KIP`, `WIND_Y_KIP`, `DIAPHRAGM_CLASS`.
  `W_SEISMIC_PSF` is the effective seismic weight per square foot of roof, `FPX_KIP` the roof diaphragm
  design force, `WIND_X_KIP` and `WIND_Y_KIP` the roof-level wind forces, `DIAPHRAGM_CLASS` the
  classification the design criteria ask for.
* **Drifts**: `location`, `face`, `peak_surcharge_psf`, `band_from_ft`, `band_to_ft` — one row for each
  of `MEP-PH-1` face `W`, `SCR-N` face `S`, `PARAPET` face `N`, `RTU-1` face `E`. `peak_surcharge_psf` is
  the drift surcharge at the face, over and above the balanced snow load; `band_from_ft` and `band_to_ft`
  are the plan coordinates bounding the drift, on the axis normal to that face — x for an east or west
  face, y for a north or south face — running from the face outwards. For `PARAPET` face `N`, the face is
  the inside of the north parapet.
* **CalcSummary**: `zone_id`, `governing_scenario`, `shear_demand_plf`, `shear_capacity_plf`,
  `gravity_demand_psf`, `gravity_capacity_psf`, `uplift_demand_psf`, `uplift_capacity_psf` — one row per
  zone, each demand being the largest anywhere in that zone over all scenarios, and `governing_scenario`
  the scenario id (`S01`…`S10`) whose demand/capacity ratio is the largest for that zone.
* **Diaphragm**: `scenario`, `direction`, `max_shear_plf`, `max_deflection_in`, `chord_force_kip`,
  `collector_force_kip`, `classification` — one row per lateral scenario.
* **BOM**: `item`, `quantity`, `unit` — at least `deck area <g> gage` in sf for every gage used,
  `support fasteners` and `sidelap connectors` in ea.

### 2. `roof_deck_plan.dxf` — the roof framing plan

In **feet**, in the same grid coordinates. One closed `LWPOLYLINE` per zone rectangle on layer
`S-DECK-ZONE`, and for each zone a `TEXT` label containing its `zone_id`, placed inside one of that zone's
rectangles, on layer `S-DECK-TEXT`. Show the framing on layer `S-FRAMING` and the roof openings on layer
`S-OPENING`.

### 3. `constraint_register.xlsx` — the requirements you designed to

Sheet `Register` with columns `document`, `clause`, `value`, `applies_to`: one row per requirement you
applied, naming the governing document (its title or document number), the clause, section or table, the
numerical value with units where there is one, and what it binds. Only the first 150 rows are read. A row
that cites a superseded or non-governing document, or whose value contradicts the clause it cites (it gives
a different number for a quantity that clause states), counts against the register; every number in a row
must be a value the cited clause states (or an equivalent of it), so one wrong number makes the row count
against the register even when another is right; rows without numbers do not count against it; a row that
cites requirements of more than one document or more than three clauses, or carries more than 8 numbers, is
not credited and counts against the register. If more than 40 % of the rows read count against the register,
none of it is credited. A requirement you satisfy but do not register earns less credit than one you satisfy
and register; a governing requirement missing from the register forfeits the credit of every family it governs.

### 4. `design_basis.md`

A narrative: which documents governed where they conflict and why, the snow and seismic bases, how the
diaphragm was analysed and classified, the zoning decisions and what drove each one.

## How the deliverable is judged

The design is re-analysed independently in every design scenario, with the loads, capacities and stiffnesses
derived from the project documents. Each scenario is credited for the constraints the design satisfies and
documents — hard constraints (gravity strength, deflection and bearing, diaphragm shear, in-plane
deflection, chord and collector forces, uplift, attachment rules, zoning rules, procurement, and the design
basis values) and softer documentation and consistency checks (schedule completeness, the drawing, the bill
of materials, the governing-scenario declaration). Cost credit is measured against a certified lower bound
on the installed cost of any compliant design: each scenario scores 0.4·p + 0.6·[h = 1]·O, where p is the
fraction of the 28 constraint families credited, h the fraction of the 21 hard families credited ([h = 1] is
1 when all 21 are, else 0), and O the bound divided by your installed cost, at most 1. A hard miss withholds
the cost credit.

**The design scenarios.** `S01` gravity with the roof live load; `S02` gravity with balanced snow and the
drifts at the equipment enclosure; `S03` gravity with balanced snow and the parapet drifts; `S04` gravity
with balanced snow and the drifts at the rooftop units and the equipment screen; `S05` wind along x;
`S06` wind along y; `S07` seismic along x; `S08` seismic along y; `S09` components-and-cladding uplift;
`S10` seismic along x with the collectors amplified by the overstrength factor.

**Checking conventions.** The roof is checked on a grid of cells one joist spacing by one sidelap module
(6 ft by 6 ft), and a cell must carry the worst demand occurring anywhere inside it; zone rectangle
boundaries fall on those same lines. A cell any part of which falls inside a framed roof opening carries no
deck, so a framed opening takes out every cell it touches, not only the cells it fills. The in-plane analysis the grader re-runs is the one the design criteria
and the lateral analysis report describe: shear panels one joist bay wide by one girder bay deep carrying
pure shear with the deck's G', axial bars along every joist line and every girder line with the axial
stiffnesses the lateral report gives, the perimeter edge member added to the boundary lines, and each braced
frame as a spring of half its stiffness at each of its two column lines; within a panel the shear flow is
constant, so deck strips stacked across its depth act in series. A small spring on every free degree of
freedom regularises the solution, and a scenario in which more than 2 % of the applied force is carried by
those springs rather than by the deck, the framing and the braced frames has no load path and fails. Wind enters along the two wall lines
perpendicular to the force, half at each; the seismic inertial force is spread over the plan by tributary
area; the stated eccentricity is applied as a plan couple. The reported chord force is the largest axial
force in the chord lines for that direction, and the reported collector force the largest axial force in a
perimeter member within one bay of a braced frame of that direction, amplified where the scenario says so.
Values you report are compared with the re-analysis within the tolerances an engineer would expect of an
independent model, not to the last digit.

Write a first complete version of the deliverable early and improve it; the version on disk at the end is
graded.


## Verifier access (blind variant)

No verifier, oracle, or evaluation data of any kind is available in this workspace — the
only checks you can run are the ones you build yourself. Work from the brief and the
documents alone and submit your best final deliverable; it will be graded offline by the
frozen verifier on held-out instances.