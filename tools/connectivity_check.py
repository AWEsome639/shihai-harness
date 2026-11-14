#!/usr/bin/env python3
"""Connectivity check mirroring the graded rule.

Usage:  python3 connectivity_check.py <model.ifc> [--min-cover 0.80]

Rack rows are keyed by ``Pset_DCM_Rack.RowId`` (with a ``y=<centroid>`` fallback,
exactly as the graded gate does).  For each service - cable tray, busway, chilled
water - a run counts towards a row when it lies within ``SERVICE_REACH`` across the
row; the union of the runs' spans along the row's long axis is the covered length.
A row is served when that fraction reaches ``SERVICE_COVERAGE``.

Exit code 1 when any row is under-covered so a shell caller can gate on it.
A clean report means every row is served by all three services.
"""
import sys, collections

try:
    import ifcopenshell
    import ifcopenshell.geom
except ImportError:
    sys.exit("ifcopenshell is not importable in this environment")

REACH_MM = 2500.0
MIN_COVER = 0.80
JOINT_MM = 250.0

SERVICE_KEYS = (
    ("cable_tray", ("tray", "cable")),
    ("busway", ("busway", "bus")),
    ("chw_pipe", ("chw", "chilled", "pipe")),
)


class Box:
    __slots__ = ("gid", "kind", "name", "lo", "hi", "row")

    def __init__(self, gid, kind, name, lo, hi, row):
        self.gid, self.kind, self.name, self.lo, self.hi, self.row = gid, kind, name, lo, hi, row


def load(path):
    f = ifcopenshell.open(path)
    st = ifcopenshell.geom.settings()
    st.set(st.USE_WORLD_COORDS, True)
    out = []
    for el in f.by_type("IfcProduct"):
        if not getattr(el, "Representation", None):
            continue
        try:
            sh = ifcopenshell.geom.create_shape(st, el)
        except Exception:
            continue
        v = sh.geometry.verts
        if not v:
            continue
        xs, ys, zs = v[0::3], v[1::3], v[2::3]
        lo = (min(xs) * 1000.0, min(ys) * 1000.0, min(zs) * 1000.0)
        hi = (max(xs) * 1000.0, max(ys) * 1000.0, max(zs) * 1000.0)
        name = " ".join(filter(None, [getattr(el, "Name", None), getattr(el, "ObjectType", None)])) or ""
        row = None
        for rel in getattr(el, "IsDefinedBy", None) or []:
            if not rel.is_a("IfcRelDefinesByProperties"):
                continue
            pset = getattr(rel, "RelatingPropertyDefinition", None)
            if pset is None or not pset.is_a("IfcPropertySet"):
                continue
            for prop in pset.HasProperties or []:
                if prop.Name in ("RowId", "Row", "ROW") and prop.NominalValue is not None:
                    row = str(prop.NominalValue.wrappedValue)
        kind = "rack" if (row is not None or "rack" in name.lower()) else None
        out.append(Box(el.GlobalId, kind, name, lo, hi, row))
    return out


def service_of(box):
    low = box.name.lower()
    for svc, keys in SERVICE_KEYS:
        if any(k in low for k in keys):
            return svc
    return None


def rows_of(boxes):
    rows = collections.defaultdict(list)
    for b in boxes:
        if b.kind != "rack":
            continue
        key = b.row if b.row else f"y={round((b.lo[1] + b.hi[1]) / 2):d}"
        rows[key].append(b)
    return rows


def row_box(racks):
    x0 = min(r.lo[0] for r in racks); x1 = max(r.hi[0] for r in racks)
    y0 = min(r.lo[1] for r in racks); y1 = max(r.hi[1] for r in racks)
    axis = 0 if (x1 - x0) >= (y1 - y0) else 1
    return (x0, x1, y0, y1, 0) if axis == 0 else (y0, y1, x0, x1, 1)


def covered(runs, lo, hi, across_lo, across_hi, axis):
    other = 1 - axis
    spans = []
    for r in runs:
        if r.lo[other] > across_hi + REACH_MM or r.hi[other] < across_lo - REACH_MM:
            continue
        a, b = max(r.lo[axis], lo), min(r.hi[axis], hi)
        if b > a:
            spans.append((a, b))
    if not spans or hi <= lo:
        return 0.0
    spans.sort()
    total, reach = 0.0, spans[0][0]
    for a, b in spans:
        a = max(a, reach)
        if b > a:
            total += b - a
            reach = b
    return total / (hi - lo)


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    path = argv[1]
    min_cover = MIN_COVER
    for i, a in enumerate(argv):
        if a == "--min-cover" and i + 1 < len(argv):
            min_cover = float(argv[i + 1])

    boxes = load(path)
    rows = rows_of(boxes)
    if not rows:
        print("UNKNOWN: no rack with a RowId was found - cannot judge connectivity")
        return 2
    print(f"racks: {sum(len(v) for v in rows.values())}  rows: {len(rows)}  reach: {REACH_MM:.0f} mm  min cover: {min_cover:.0%}")

    unserved = []
    for svc, _ in SERVICE_KEYS:
        runs = [b for b in boxes if service_of(b) == svc]
        print(f"--- {svc}: {len(runs)} runs")
        bad = []
        for key in sorted(rows):
            lo, hi, alo, ahi, axis = row_box(rows[key])
            frac = covered(runs, lo, hi, alo, ahi, axis)
            flag = "ok " if frac >= min_cover else "LOW"
            print(f"    row {key}: {frac*100:5.1f}%  {flag}")
            if frac < min_cover:
                bad.append(key)
        if bad:
            unserved.append(svc)
            print(f"    UNSERVED ({svc}): {', '.join(bad)}")

    print(f"\nROWS UNDER-COVERED: {len(unserved)}"
          + (f" -> {', '.join(unserved)}" if unserved else ""))
    return 1 if unserved else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
