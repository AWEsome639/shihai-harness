"""submission.py - parse a candidate package.

The package is `output/submission.zip` and must contain exactly these five members (any
directory prefix inside the zip is tolerated, the basename is what matters):

    lateral_schedule.xlsx   DeckZones, ChordSchedule, Piers, ShearWalls, Diaphragm, Chords,
                            Drift, Convergence, Cost
    diaphragm_plan.dxf      the plan drawings
    constraint_register.xlsx  sheet `Register`
    calculations.pdf        the 8-1/2 x 11 calculation package
    design_basis.md         the design basis narrative and its data table

Nothing here raises: a malformed package produces empty tables and a parse error string, and
the grader scores it as unparseable.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

PLACEHOLDER = re.compile(r"^\s*(tbd|tba|n/?a|xx+|\?+|-+|none|todo)\s*$", re.I)


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v).strip().replace(",", "").replace("$", "")
    if not s or PLACEHOLDER.match(s):
        return None
    m = re.match(r"^[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", s)
    return float(m.group(0)) if m else None


SCEN = re.compile(r"\bW\s*0*(\d{1,2})\b", re.I)
AXIS = {"X": "X", "EW": "X", "E-W": "X", "EAST-WEST": "X", "LONGITUDINAL": "X",
        "Y": "Y", "NS": "Y", "N-S": "Y", "NORTH-SOUTH": "Y", "TRANSVERSE": "Y"}
LINES = ("N1", "N2", "E2", "S", "E", "W")


def _scen(v):
    """`w1`, `W 01`, `case W01` and `W01` are the same scenario."""
    m = SCEN.search(str(v or ""))
    return f"W{int(m.group(1)):02d}" if m else _txt(v).upper()


def _line(v):
    """`Line S`, `line s`, `S` are the same wall line; the two-character ids win over the one."""
    t = re.sub(r"[^A-Z0-9]", "", str(v or "").upper())
    t = re.sub(r"^(WALL)?(LINE|GRID|AXIS)", "", t)
    for lid in LINES:
        if t == lid:
            return lid
    for lid in LINES:
        if t.endswith(lid):
            return lid
    return t


def _axis(v):
    """`X`, `EW`, `east-west`, `Longitudinal`, `Transverse (Y)` - the criteria's own words."""
    u = str(v or "").upper()
    t = re.sub(r"[^A-Z-]", "", u)
    if t in AXIS:
        return AXIS[t]
    for word, ax in (("TRANSVERSE", "Y"), ("LONGITUDINAL", "X"),
                     ("NORTH-SOUTH", "Y"), ("NORTHSOUTH", "Y"),
                     ("EAST-WEST", "X"), ("EASTWEST", "X")):
        if word in u.replace(" ", ""):
            return ax
    m = re.search(r"\b([XY])\b", u)
    return m.group(1) if m else t[:1]


def _txt(v):
    if v is None:
        return ""
    s = str(v).strip()
    return "" if PLACEHOLDER.match(s) else s


class Sheet:
    """A worksheet reduced to a list of dicts keyed by normalised column name."""

    def __init__(self, rows: list[dict], headers: list[str]):
        self.rows = rows
        self.headers = headers

    def __len__(self):
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)


def _pick(row, *keys):
    """The first key that is PRESENT, not the first that is truthy.

    `row.get("s0_ft") or row.get("s0")` would read a station of 0.0 as absent and drop
    the first pier of every wall line from the schedule."""
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


TOTALS = re.compile(r"^\s*(grand\s+)?(total|totals|sum|subtotal|sub-total|all)\b", re.I)


def _is_total(row: dict) -> bool:
    """A summary line at the foot of a schedule is not a zone, a pier or a chord line."""
    for v in list(row.values())[:2]:
        if isinstance(v, str) and TOTALS.match(v):
            return True
    return False


def _norm(h) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(h or "").strip().lower()).strip("_")


def _read_xlsx(data: bytes, errors: list[str], label: str) -> dict[str, Sheet]:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{label}: unreadable workbook ({exc})")
        return {}
    out = {}
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            out[_norm(ws.title)] = Sheet([], [])
            continue
        head_i = 0
        for i, r in enumerate(rows[:5]):
            if sum(1 for c in r if c not in (None, "")) >= 2:
                head_i = i
                break
        headers = [_norm(c) for c in rows[head_i]]
        body = []
        for r in rows[head_i + 1:]:
            if all(c in (None, "") for c in r):
                continue
            body.append({h: c for h, c in zip(headers, r) if h})
        out[_norm(ws.title)] = Sheet(body, [h for h in headers if h])
    try:
        wb.close()
    except Exception:  # noqa: BLE001
        pass
    return out


class Submission:
    def __init__(self):
        self.errors: list[str] = []
        self.parse_ok = False
        self.deck_zones: list[dict] = []
        self.chord_schedule: list[dict] = []
        self.piers: list[dict] = []
        self.wall_loads: list[dict] = []
        self.diaphragm: list[dict] = []
        self.chords: list[dict] = []
        self.drift: list[dict] = []
        self.convergence: list[dict] = []
        self.cost_rows: list[dict] = []
        self.register: list[dict] = []
        self.dxf_text: str = ""
        self.pdf_bytes: bytes = b""
        self.basis_text: str = ""
        self.placeholders = 0
        self._raw_placeholders = 0

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, zip_path: Path) -> "Submission":
        s = cls()
        try:
            z = zipfile.ZipFile(zip_path)
        except Exception as exc:  # noqa: BLE001
            s.errors.append(f"submission.zip not a readable zip ({exc})")
            return s
        members = {}
        for n in z.namelist():
            base = n.rsplit("/", 1)[-1].lower()
            if base and base not in members:
                members[base] = n
        need = ["lateral_schedule.xlsx", "diaphragm_plan.dxf", "constraint_register.xlsx",
                "calculations.pdf", "design_basis.md"]
        missing = [n for n in need if n not in members]
        for n in missing:
            s.errors.append(f"missing {n}")

        if "lateral_schedule.xlsx" in members:
            sheets = _read_xlsx(z.read(members["lateral_schedule.xlsx"]), s.errors,
                                "lateral_schedule.xlsx")
            s._take_schedule(sheets)
        if "constraint_register.xlsx" in members:
            sheets = _read_xlsx(z.read(members["constraint_register.xlsx"]), s.errors,
                                "constraint_register.xlsx")
            reg = sheets.get("register") or (list(sheets.values())[0] if sheets else Sheet([], []))
            for r in reg:
                s.register.append({"document": _txt(_pick(r, "document")),
                                   "clause": _txt(_pick(r, "clause")),
                                   "requirement": _txt(_pick(r, "requirement")),
                                   "value": _txt(_pick(r, "value")),
                                   "units": _txt(_pick(r, "units")),
                                   "items": _txt(_pick(r, "items"))})
        if "diaphragm_plan.dxf" in members:
            try:
                s.dxf_text = z.read(members["diaphragm_plan.dxf"]).decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                s.errors.append(f"diaphragm_plan.dxf unreadable ({exc})")
        if "calculations.pdf" in members:
            try:
                s.pdf_bytes = z.read(members["calculations.pdf"])
            except Exception as exc:  # noqa: BLE001
                s.errors.append(f"calculations.pdf unreadable ({exc})")
        if "design_basis.md" in members:
            try:
                s.basis_text = z.read(members["design_basis.md"]).decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                s.errors.append(f"design_basis.md unreadable ({exc})")
        z.close()
        s.parse_ok = (not missing and bool(s.deck_zones) and bool(s.piers)
                      and bool(s.diaphragm) and bool(s.wall_loads))
        if not s.parse_ok and not s.errors:
            s.errors.append("required schedule sheets are empty")
        return s

    # ------------------------------------------------------------------ sheets
    # Only the columns the submittal specification names carry a required value, so an extra column
    # of the solver's own notes is not charged, and "-" or "none" in
    # a yes/no column is an answer, not an omission.
    REQUIRED = {
        "deckzones": ("zone_id", "deck_code", "capacity_asd_plf", "area_sf"),
        "chordschedule": ("chord_id", "section_code", "length_ft"),
        "piers": ("panel", "line", "width_ft", "k_kip_per_in", "embeds"),
        "shearwalls": ("scenario", "line", "line_load_plf", "reaction_kips"),
        "diaphragm": ("scenario", "line", "station_frac", "unit_shear_plf"),
        "chords": ("scenario", "chord_id", "force_kips"),
        "drift": ("scenario", "direction", "deflection_in"),
        "convergence": ("level", "mesh_size_ft", "elements"),
    }

    def _count_placeholders(self, sheets: dict[str, Sheet]):
        n = 0
        for name, cols in self.REQUIRED.items():
            sheet = sheets.get(name)
            if sheet is None:
                continue
            for row in sheet:
                if _is_total(row):          # a summary line owes no values
                    continue
                for c in cols:
                    if c not in row:
                        continue
                    v = row[c]
                    if v is None or (isinstance(v, str)
                                     and (not v.strip() or PLACEHOLDER.match(v))):
                        n += 1
        return n

    def _take_schedule(self, sheets: dict[str, Sheet]):
        self._raw_placeholders = self._count_placeholders(sheets)
        def sheet(*names):
            for n in names:
                if n in sheets:
                    return sheets[n]
            return Sheet([], [])

        for r in sheet("deckzones", "deck_zones", "zones"):
            if _is_total(r):
                continue
            self.deck_zones.append({
                "zone": _txt(_pick(r, "zone_id", "zone")),
                "code": _txt(_pick(r, "deck_code", "code", "deck")),
                "g_prime": _num(_pick(r, "g_prime_used_kip_per_in", "g_prime", "g_kip_per_in")),
                "capacity": _num(_pick(r, "capacity_asd_plf", "capacity_plf", "capacity")),
                "area": _num(_pick(r, "area_sf", "area")),
                "cost": _num(_pick(r, "cost_usd", "cost")),
            })
        for r in sheet("chordschedule", "chord_schedule", "chordsizes"):
            if _is_total(r):
                continue
            self.chord_schedule.append({
                "chord": _txt(_pick(r, "chord_id", "chord", "id")),
                "code": _txt(_pick(r, "section_code", "section", "code")),
                "length": _num(_pick(r, "length_ft", "length")),
                "cost": _num(_pick(r, "cost_usd", "cost")),
            })
        for r in sheet("piers", "pier", "panels"):
            if _is_total(r):
                continue
            self.piers.append({
                "pier": _txt(_pick(r, "pier_id", "pier")),
                "panel": _txt(_pick(r, "panel", "panel_mark", "mark")),
                "line": _txt(_pick(r, "line", "wall_line")),
                "s0": _num(_pick(r, "s0_ft", "s0", "start_ft")),
                "s1": _num(_pick(r, "s1_ft", "s1", "end_ft")),
                "width": _num(_pick(r, "width_ft", "width")),
                "t_in": _num(_pick(r, "thickness_in", "t_in", "thickness")),
                "top_z": _num(_pick(r, "top_z_ft", "top_z", "top_elevation_ft")),
                "k": _num(_pick(r, "k_kip_per_in", "k", "stiffness")),
                "embeds": _num(_pick(r, "embeds", "n_embeds", "embed_count")),
                "tiedown": _txt(_pick(r, "tiedown", "tie_down")),
            })
        for r in sheet("shearwalls", "shear_walls", "walllines"):
            self.wall_loads.append({
                "scenario": _scen(_pick(r, "scenario", "case", "load_case")),
                "line": _line(_pick(r, "line", "wall_line")),
                "line_load": _num(_pick(r, "line_load_plf", "w_plf", "line_load")),
                "reaction": _num(_pick(r, "reaction_kips", "v_kips", "reaction")),
            })
        for r in sheet("diaphragm", "diaphragmshear", "shears"):
            self.diaphragm.append({
                "scenario": _scen(_pick(r, "scenario", "case", "load_case")),
                "line": _line(_pick(r, "line", "wall_line")),
                "frac": _num(_pick(r, "station_frac", "frac", "station")),
                "v": _num(_pick(r, "unit_shear_plf", "v_plf", "unit_shear")),
            })
        for r in sheet("chords", "chordforces"):
            self.chords.append({
                "scenario": _scen(_pick(r, "scenario", "case", "load_case")),
                "chord": _txt(_pick(r, "chord_id", "chord", "id")),
                "force": _num(_pick(r, "force_kips", "force", "axial_kips")),
            })
        for r in sheet("drift", "deflection", "drifts"):
            self.drift.append({
                "scenario": _scen(_pick(r, "scenario", "case", "load_case")),
                "direction": _axis(_pick(r, "direction", "dir", "axis")),
                "defl": _num(_pick(r, "deflection_in", "deflection", "delta_in")),
            })
        for r in sheet("convergence", "meshconvergence", "gci"):
            self.convergence.append({
                "level": _txt(_pick(r, "level", "mesh_level")),
                "h": _num(_pick(r, "mesh_size_ft", "h_ft", "size_ft")),
                "elements": _num(_pick(r, "elements", "n_elements")),
                "max_v": _num(_pick(r, "max_unit_shear_plf", "max_v_plf")),
                "key_reaction": _num(_pick(r, "key_reaction_kips", "reaction_kips")),
            })
        for r in sheet("cost", "costs", "summary"):
            self.cost_rows.append({"item": _txt(_pick(r, "item")),
                                   "total": _num(_pick(r, "total_usd", "total", "cost_usd"))})
        # counted on the RAW cells: `_txt` blanks a placeholder before it is stored, so counting
        # the stored values could never find one.  A blank where a value is required is what the
        # submittal specification calls incomplete, so that is what is counted.
        self.placeholders = self._raw_placeholders
