You are working in `/filesystem/final_batch4__structural_task5_wind_diaphragm_shear__blind`. The project documents are under `/filesystem/final_batch4__structural_task5_wind_diaphragm_shear__blind/` (read `task.md` there first); write your deliverable to `/filesystem/final_batch4__structural_task5_wind_diaphragm_shear__blind/output/submission.zip`.

Write a first COMPLETE version of `output/submission.zip` within your first ~100 steps, then iterate on it in place: the last version on disk when the run ends is what gets graded, and a run that exhausts its step or token budget with no file scores 0. An imperfect early submission earns partial credit.

# Building A2 — roof diaphragm shears and shear wall loads for wind

You are the delegated engineer for the **lateral package** of Building A2 at the Northern Divide
Data Campus, in Lewis and Clark County, Montana: a single-storey data hall with a steel roof deck
on joists and girders, carried laterally by precast concrete wall panels acting as shear walls on
every exterior building line, with a parapet cantilevering above the roof deck.

Your job is to work out **how the wind gets from the wall panels into the roof deck diaphragm and
back out into the shear walls**, to design the deck, the chords and collectors and the panel
connections that carry it, and to hand over the package the Engineer of Record will check: plan
drawings of the diaphragm unit shears and the shear wall loads, the schedules behind them, and a
calculation package.

The building is not the clean rectangle the 2024 drawings show. The as-built geometry is in the
2026 federated model and the roof line scan.

## What you are given

The task folder (project/, library/ and templates/) is the document set issued for this package: the structural design criteria, the
boundary-layer wind study and its coefficient data, the topographic study, the AHJ letter, the
survey and the roof line scan, the 2026 federated model, the architectural and structural
drawings, the precast panel drawings and schedule, the deck, chord and connection catalogues, the
owner's basis of design, the submittal specification, and the public standards library. It also
holds superseded revisions and a study of a different building on the same campus; part of the
job is knowing which document governs.

Nothing has been pre-digested for you. Every parameter you need is in those files — including the
analysis procedure the criteria requires you to use, and the verification example it publishes so
you can check your implementation before you trust it.

## What you must deliver

Write **`output/submission.zip`** containing exactly these five files (blank templates for the two
workbooks and the drawing are in `templates/`):

**1. `lateral_schedule.xlsx`** — nine worksheets.

* `DeckZones`: one row per deck zone —
  `zone_id, deck_code, G_prime_used_kip_per_in, capacity_asd_plf, area_sf, cost_usd`.
  `G_prime_used_kip_per_in` is the shear stiffness you actually used for that zone and
  `capacity_asd_plf` the shear capacity you checked against.
* `ChordSchedule`: one row per chord and collector line — `chord_id, section_code, length_ft,
  cost_usd`.
* `Piers`: one row per pier — `pier_id, panel, line, s0_ft, s1_ft, width_ft, thickness_in,
  top_z_ft, k_kip_per_in, embeds, tiedown`. `s0_ft`/`s1_ft` are the pier's extent along its wall
  line, `top_z_ft` the top of that panel, `k_kip_per_in` the stiffness you assigned it, `embeds`
  the number of deck-to-panel embeds on that panel and `tiedown` `yes` or `no`.
* `ShearWalls`: one row per wind scenario per wall line — `scenario, line, line_load_plf,
  reaction_kips`. `line_load_plf` is the roof-level line load on that wall line and
  `reaction_kips` the load that wall line takes out of the diaphragm. Report unit shears, line
  loads, reactions, chord forces and deflections as **magnitudes**; the scenario already says
  which way the wind blows, and either sign convention is read the same way.
* `Diaphragm`: one row per wind scenario, wall line and report station — `scenario, line,
  station_frac, unit_shear_plf`. The criteria fixes which stations are reported; use those.
* `Chords`: one row per wind scenario per chord and collector line — `scenario, chord_id,
  force_kips`.
* `Drift`: one row per wind scenario per global axis — `scenario, direction, deflection_in`.
* `Convergence`: one row per mesh refinement level — `level, mesh_size_ft, elements,
  max_unit_shear_plf, key_reaction_kips`, with `key_reaction_kips` the reaction of wall line W in
  scenario W01.
* `Cost`: `item, quantity, unit_cost_usd, total_usd`, one row per cost item, summing to the
  installed cost of the package.

**2. `diaphragm_plan.dxf`** — the plan view drawings, on the building grid in feet, with the
layers and the content the submittal specification requires.

**3. `constraint_register.xlsx`** — sheet `Register`, columns
`document, clause, requirement, value, units, items`: the requirements you applied, where each
one comes from, the value it sets, and the items it binds.

**4. `calculations.pdf`** — the calculation package, in the format and with the sections the
criteria and the submittal specification require.

**5. `design_basis.md`** — the design basis narrative, carrying the two tables the submittal
specification defines and, in prose, every conflict between issued documents that you resolved
and how.

## How it is graded

Per wind scenario: the package must be present and parseable; every constraint family is checked;
and the objective credit is the ratio of a certified cost bound to your installed cost, paid only
when you miss no hard family.

    score = 0.4 * p + 0.6 * [h = 1] * O

where `p` is the fraction of the 36 constraint families you both satisfied **and** registered, `h`
is the fraction of the 22 hard families you both satisfied and registered (`[h = 1]` is 1 when all
22 are, else 0), and `O` is the objective credit (at most 1). A hard miss zeroes the `O` term. The
score is the mean over a held-out subset of the twelve wind scenarios. Constraint families cover
the set-out and take-off from the as-built geometry, the deck, chord, collector and connection
design, the acceptance limits, the agreement of every reported quantity with the Engineer's
re-analysis of **your** design, and the design basis.

**The register is scored.** A family counts only when the requirements that govern it appear in
your register, matched on document and clause. So the rules, in full:

* one document per row, at most three clauses and at most eight numbers in a row;
* a row citing a document that is not in the corpus, or a clause that does not appear in the
  document it cites, or a number that contradicts that clause, is a **wrong row**;
* a row citing a **superseded** revision or another building's study is a wrong row;
* a row that cites a genuine clause this register does not happen to index is **neutral** — it
  neither helps nor hurts, so documenting more of the corpus than is asked for is never punished;
* precision is `1 - wrong / claimed`, and **a register whose precision falls below 0.60 is
  voided entirely** — every family then counts as unregistered. At most 150 rows are judged.

## Working notes

Write a first COMPLETE version of `output/submission.zip` early and improve it in place: the last
version on disk when the run ends is what gets graded, and an imperfect early package earns
partial credit while no package earns none.

Your workspace has Python 3 with numpy, scipy, openpyxl, ezdxf, reportlab, pdfplumber and pymupdf already installed. There is no network access, so pip cannot install anything else.


## Verifier access (blind variant)

No verifier, oracle, or evaluation data of any kind is available in this workspace — the
only checks you can run are the ones you build yourself. Work from the brief and the
documents alone and submit your best final deliverable; it will be graded offline by the
frozen verifier on held-out instances.