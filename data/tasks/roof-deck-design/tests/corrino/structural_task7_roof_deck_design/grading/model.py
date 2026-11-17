"""Parse a candidate submission.zip into the objects the checks work on.

Nothing here executes candidate code and nothing here trusts a candidate value: every number the
candidate reports is kept only so that a check can compare it with the grader's own re-derivation.
"""
from __future__ import annotations

import io
import math
import re
import zipfile
from dataclasses import dataclass, field

import numpy as np

MAX_MEMBER_BYTES = 80 * 1024 * 1024
SHEETS = ["Zones", "DesignBasis", "Drifts", "CalcSummary", "Diaphragm", "BOM"]


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return None if (isinstance(v, float) and not math.isfinite(v)) else float(v)
    s = str(v).strip().replace(",", "")
    m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", s)
    if not m:
        return None
    try:
        x = float(m.group(0))
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def _s(v):
    return "" if v is None else str(v).strip()


@dataclass
class Submission:
    parse_ok: bool = False
    errors: list = field(default_factory=list)
    zone_rects: list = field(default_factory=list)      # dicts: zone_id,x0,y0,x1,y1,gage,pattern,sidelap,ns
    zones: dict = field(default_factory=dict)           # zone_id -> properties
    basis: dict = field(default_factory=dict)
    calc: dict = field(default_factory=dict)            # zone_id -> dict of reported values
    diaphragm: dict = field(default_factory=dict)       # scenario -> dict of reported values
    drifts: dict = field(default_factory=dict)          # (tag, face) -> dict of reported values
    bom: dict = field(default_factory=dict)
    register_rows: list = field(default_factory=list)
    dxf_polys: list = field(default_factory=list)
    dxf_labels: list = field(default_factory=list)
    dxf_lines: list = field(default_factory=list)
    dxf_error: str = ""
    basis_md_len: int = 0
    basis_md: str = ""


def _safe_members(z):
    out = {}
    for info in z.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        base = name.split("/")[-1]
        if not base or info.file_size > MAX_MEMBER_BYTES:
            continue
        # a submission is read by basename at the zip root; a path-traversal name reduces to its base
        if base not in out:
            out[base] = info
    return out


def _read_xlsx(data):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = {}
    for ws in wb.worksheets:
        rows = []
        for r in ws.iter_rows(values_only=True):
            rows.append(list(r))
            if len(rows) > 20000:
                break
        out[ws.title.strip()] = rows
    wb.close()
    return out


def _table(rows):
    """Header row -> list of dicts keyed by the lower-cased header."""
    if not rows:
        return []
    hdr = None
    for i, r in enumerate(rows[:10]):
        cells = [_s(c).lower() for c in r]
        if sum(1 for c in cells if c) >= 2:
            hdr = i
            break
    if hdr is None:
        return []
    keys = [_s(c).lower() for c in rows[hdr]]
    out = []
    for r in rows[hdr + 1:]:
        if all(c is None or _s(c) == "" for c in r):
            continue
        out.append({k: r[i] if i < len(r) else None for i, k in enumerate(keys) if k})
    return out


def _parse_dxf(data):
    """Minimal DXF reader: closed LWPOLYLINE vertices and TEXT/MTEXT labels, by layer."""
    txt = data.decode("utf-8", "replace").splitlines()
    polys, labels, lines = [], [], []
    i = 0
    n = len(txt)
    while i < n - 1:
        code, val = txt[i].strip(), txt[i + 1].strip()
        if code == "0" and val in ("LWPOLYLINE", "POLYLINE", "TEXT", "MTEXT", "LINE"):
            kind = val
            layer, xs, ys, s = "", [], [], ""
            j = i + 2
            while j < n - 1:
                c, v = txt[j].strip(), txt[j + 1].strip()
                if c == "0":
                    break
                if c == "8":
                    layer = v
                elif c == "10":
                    try:
                        xs.append(float(v))
                    except ValueError:
                        pass
                elif c == "20":
                    try:
                        ys.append(float(v))
                    except ValueError:
                        pass
                elif c in ("1", "3"):
                    s += v
                j += 2
            if kind in ("LWPOLYLINE", "POLYLINE") and len(xs) >= 3 and len(ys) >= 3:
                polys.append(dict(layer=layer, x=xs[:len(ys)], y=ys[:len(xs)]))
            elif kind == "LINE" and xs and ys:
                lines.append(dict(layer=layer, x=xs, y=ys))
            elif kind in ("TEXT", "MTEXT") and xs and ys:
                labels.append(dict(layer=layer, x=xs[0], y=ys[0], text=s))
            i = j
            continue
        i += 2
    return polys, labels, lines


def load_submission(zip_path, truth) -> Submission:
    sub = Submission()
    try:
        z = zipfile.ZipFile(zip_path)
    except Exception as e:  # noqa: BLE001
        sub.errors.append(f"not a readable zip: {type(e).__name__}")
        return sub
    with z:
        members = _safe_members(z)
        want = {"deck_design.xlsx", "roof_deck_plan.dxf", "constraint_register.xlsx", "design_basis.md"}
        missing = [w for w in want if w not in members]
        if "deck_design.xlsx" in missing:
            sub.errors.append("deck_design.xlsx missing")
            return sub
        try:
            book = _read_xlsx(z.read(members["deck_design.xlsx"]))
        except Exception as e:  # noqa: BLE001
            sub.errors.append(f"deck_design.xlsx unreadable: {type(e).__name__}")
            return sub
        names = {k.lower(): k for k in book}
        for need in ("zones",):
            if need not in names:
                sub.errors.append(f"sheet {need!r} missing")
                return sub

        # --- Zones -------------------------------------------------------------
        for r in _table(book[names["zones"]]):
            zid = _s(r.get("zone_id"))
            vals = [_num(r.get(k)) for k in ("x0_ft", "y0_ft", "x1_ft", "y1_ft")]
            if not zid or any(v is None for v in vals):
                continue
            gage = _num(r.get("gage"))
            ns = _num(r.get("sidelap_per_span"))
            rect = dict(zone_id=zid, x0=min(vals[0], vals[2]), y0=min(vals[1], vals[3]),
                        x1=max(vals[0], vals[2]), y1=max(vals[1], vals[3]),
                        gage=int(gage) if gage else None,
                        pattern=_s(r.get("support_pattern")).upper().replace(" ", ""),
                        sidelap=_s(r.get("sidelap_type")).upper().replace(" ", ""),
                        ns=int(ns) if ns else None)
            sub.zone_rects.append(rect)
        if not sub.zone_rects:
            sub.errors.append("no usable rows in sheet Zones")
            return sub
        for r in sub.zone_rects:
            zid = r["zone_id"]
            p = sub.zones.setdefault(zid, dict(gage=r["gage"], pattern=r["pattern"],
                                               sidelap=r["sidelap"], ns=r["ns"], rects=[]))
            p["rects"].append(r)
            for k in ("gage", "pattern", "sidelap", "ns"):
                if p[k] != r[k]:
                    p[k + "_inconsistent"] = True

        # --- other sheets ------------------------------------------------------
        if "designbasis" in names:
            for r in _table(book[names["designbasis"]]):
                k = _s(r.get("key")).upper()
                if k:
                    sub.basis[k] = r.get("value")
        if "drifts" in names:
            for r in _table(book[names["drifts"]]):
                tag = _s(r.get("location")).upper()
                face = _s(r.get("face")).upper()[:1]
                if tag and face:
                    sub.drifts[(tag, face)] = {k: r.get(k) for k in r}
        if "calcsummary" in names:
            for r in _table(book[names["calcsummary"]]):
                zid = _s(r.get("zone_id"))
                if zid:
                    sub.calc[zid] = {k: r.get(k) for k in r}
        if "diaphragm" in names:
            for r in _table(book[names["diaphragm"]]):
                sc = _s(r.get("scenario")).upper()
                if sc:
                    sub.diaphragm[sc] = {k: r.get(k) for k in r}
        if "bom" in names:
            for r in _table(book[names["bom"]]):
                it = _s(r.get("item"))
                if it:
                    sub.bom[it.lower()] = _num(r.get("quantity"))

        # --- constraint register ----------------------------------------------
        if "constraint_register.xlsx" in members:
            try:
                rb = _read_xlsx(z.read(members["constraint_register.xlsx"]))
                key = next((k for k in rb if k.strip().lower() == "register"), None)
                if key is None and rb:
                    key = list(rb)[0]
                if key:
                    for r in _table(rb[key]):
                        sub.register_rows.append(dict(document=_s(r.get("document")),
                                                      clause=_s(r.get("clause")),
                                                      value=_s(r.get("value")),
                                                      applies_to=_s(r.get("applies_to"))))
            except Exception as e:  # noqa: BLE001
                sub.errors.append(f"constraint_register.xlsx unreadable: {type(e).__name__}")

        # --- drawing ------------------------------------------------------------
        if "roof_deck_plan.dxf" in members:
            try:
                sub.dxf_polys, sub.dxf_labels, sub.dxf_lines = _parse_dxf(z.read(members["roof_deck_plan.dxf"]))
            except Exception as e:  # noqa: BLE001
                sub.dxf_error = f"{type(e).__name__}"
        else:
            sub.dxf_error = "missing"

        # --- design basis -------------------------------------------------------
        if "design_basis.md" in members:
            try:
                sub.basis_md = z.read(members["design_basis.md"]).decode("utf-8", "replace")
                sub.basis_md_len = len(sub.basis_md)
            except Exception:  # noqa: BLE001
                pass
    sub.parse_ok = True
    return sub


def zone_assignment(sub, truth):
    """Map every deck cell to the index of the zone covering it.

    Returns (assign, problems): assign[i, j] is the zone index, -1 where no zone covers the cell.
    A cell covered by more than one zone rectangle is recorded as an overlap.
    """
    from . import loads as LD
    xs, ys = LD.grid(truth)
    nx, ny = LD.cell_shape(truth)
    zone_ids = list(sub.zones)
    assign = np.full((nx, ny), -1, dtype=int)
    overlap = np.zeros((nx, ny), dtype=int)
    cx = 0.5 * (xs[:-1] + xs[1:])
    cy = 0.5 * (ys[:-1] + ys[1:])
    X, Y = np.meshgrid(cx, cy, indexing="ij")
    for r in sub.zone_rects:
        m = (X > r["x0"]) & (X < r["x1"]) & (Y > r["y0"]) & (Y < r["y1"])
        zi = zone_ids.index(r["zone_id"])
        assign = np.where(m & (assign < 0), zi, assign)
        overlap += m.astype(int)
    return assign, overlap, zone_ids
