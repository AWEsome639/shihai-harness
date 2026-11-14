# Route the overhead MEP of a data hall from its drawing set, clash-free (dc_model `hall_seed000_d000`)

## Situation
You are the MEP coordinator for a data hall (~27 × 21 m, 128 cabinets). The architect has issued
a seven-sheet drawing set and you must produce the coordinated 3D model: rebuild the room and its
fit-out exactly as drawn, then route the three overhead services through the plenum so that
nothing collides, every row is served, and the routes are as short as practical — the same job a
BIM coordinator does before construction, delivered as an IFC4 model built by a script.

## Inputs (under `input/` — seven PNG drawing sheets, nothing else)
- `A-101` equipment plan · `A-301` building section · `A-601` cabinet schedule ·
  `A-602` containment/pathway/construction data · `M-101` chilled water · `E-101` busway & PDU ·
  `F-101` sprinkler grid.
- They behave like a real drawing set: positions on the plan, sizes in the schedule, elevations
  only in the section callout. No JSON side-channel, no coordinate dump. Reconcile them yourself
  (read them as images).

## Goal
1. **Rebuild the hall as the coordination background**: racks on their floor tiles with correct
   facings, structural columns, CRAHs, PDUs, access floor, containment, and every sprinkler head
   — your routes are clash-checked against all of it.
2. **Route three overhead services** from the entries marked on M-101/E-101 — telecom cable
   tray, power busway, chilled-water pipe — as straight axis-aligned segments (every direction
   or tier change is a new segment). Tray and busway must serve every rack row; chilled water
   must serve every row **and reach every CRAH**, including the perimeter units no row header
   passes.

## Constraints (all checked mechanically)
- **Zero hard clashes** — one solid overlapping another by > 2 mm anywhere (a tray through a
  column, a pipe through a rack) fails the task. Columns run full clear height.
- **Connectivity** — each service must form ONE connected network reaching its service entry;
  every row served along ≥ 80 % of its length by all three services (within 2500 mm across the
  row); chilled water within 2500 mm of every CRAH.
- **Code compliance** — 2600 mm min headroom; 460 mm kept clear below every sprinkler deflector
  (plus the NFPA 13 obstruction rule within 450 mm below a deflector); heads required beneath
  runs wider than 1200 mm and beneath containment roofs; 300 mm separation between tray and
  busway on at least one axis; no chilled water in the 1800 mm dedicated space above a PDU;
  25.4 mm pipe insulation counts in clashes. The under-floor plenum is usable for routing.
- **Modelling contract** — IFC4 via `ifcopenshell==0.8.4` (+ numpy only), millimetre project
  units (but placement-matrix translations in metres!), 2D profile points only, the prescribed
  spatial structure, exact element classes (e.g. racks = `IfcFurniture`/`USERDEFINED`/`"Rack"`)
  and the required `Pset_DCM_*` property sets, correct facing rotations about the footprint
  centre. Validate with `express_rules=True` before submitting.
- The script must run headless in 300 s, deterministically, with no network and no reading the
  PNGs back — bake everything you read off the drawings into the script as literals.

## Expected output
One self-contained Python script at `output/solution.py` that builds the model with ifcopenshell
and writes one IFC4 file to `sys.argv[1]` (default `output/model.ifc`). Scoring: schema gate →
clash gate → connectivity gate (each 0 if failed), then
`score = compliance × min(1, L_opt / L_yours)` — material efficiency against the reference
route, discounted by code violations.

## Additional diagnostic data

In addition to the original modelling, routing, clash, connectivity, and code-compliance
obligations, make the following design-quality data available. These items do not change the
primary score; the grader reports them as diagnostics.

1. **Achieved clearances** — For every sprinkler head, make the vertical clearance from the top
   of each service passing below it measurable. Make the vertical clearance from each PDU top to
   the nearest CHW pipe measurable. Geometry must also make tray–busway separation measurable.
2. **Under-floor versus overhead length** — Place each service so the grader can measure its
   centreline length below finished floor and in the overhead plenum.
3. **Service elevation / tier assignment** — Give every route segment an unambiguous elevation
   relative to finished floor. The grader reports the length in the clear-height band, above it,
   and below finished floor.
4. **Supports and hangers (optional)** — If modelled, use `IfcElementAssembly` or
   `IfcBuildingElementProxy` with an `ObjectType` containing `Support` or `Hanger`, and respect any
   maximum support spacing shown in the drawings.
5. **Service drops / terminations (optional)** — If modelled, use short route segments or distinct
   elements whose `ObjectType` contains `Drop`, `Tap`, or `Termination`. The grader reports their
   count and locations.
6. **Drawing-note reconciliation log (optional side file)** — You may write
   `output/reconciliation.json` next to the IFC, listing numeric clearance, size, or spacing notes
   extracted from the seven sheets and how each was applied. Its absence is not a gate failure.

All gates, the modelling contract, the runtime limit, and the primary score formula remain exactly
as stated above. The additional data are diagnostics only.