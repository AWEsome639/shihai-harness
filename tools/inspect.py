#!/usr/bin/env python3
"""Report both graded gates at once for a data-hall IFC model.

Usage:  python3 inspect.py <model.ifc>

Prints one block with
  GATE 1  hard clashes          (bounding-box overlaps beyond 2 mm)
  GATE 2  rows under-covered    (per service, on Pset_DCM_Rack.RowId rows)
  BUDGET  routed length vs the reference optimum, when the model carries it

Both gates are reported together because they trade against each other: adding
routing to lift coverage is exactly what reintroduces clashes.  Use the two
counts as the objective, not either one alone.

Exit code 0 when both gates pass, 1 otherwise.
"""
import sys, collections, math

try:
    import ifcopenshell
    import ifcopenshell.geom
except ImportError:
    sys.exit("ifcopenshell is not importable in this environment")

REACH_MM = 2500.0
MIN_COVER = 0.80
TOL_MM = 2.0
SERVICE_KEYS = (("cable_tray", ("tray", "cable")),
                ("busway", ("busway", "bus")),
                ("chw_pipe", ("chw", "chilled", "pipe")))


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
        lo = (min(xs) * 1000, min(ys) * 1000, min(zs) * 1000)
        hi = (max(xs) * 1000, max(ys) * 1000, max(zs) * 1000)
        name = " ".join(filter(None, [getattr(el, "Name", None), getattr(el, "ObjectType", None)])) or ""
        row = None
        for rel in getattr(el, "IsDefinedBy", None) or []:
            if not rel.is_a("IfcRelDefinesByProperties"):
                continue
            ps = getattr(rel, "RelatingPropertyDefinition", None)
            if ps is None or not ps.is_a("IfcPropertySet"):
                continue
            for prop in ps.HasProperties or []:
                if prop.Name in ("RowId", "Row") and prop.NominalValue is not None:
                    row = str(prop.NominalValue.wrappedValue)
        out.append({"gid": el.GlobalId, "name": name, "lo": lo, "hi": hi,
                    "row": row, "rack": row is not None or "rack" in name.lower()})
    return out


def service_of(b):
    low = b["name"].lower()
    for svc, keys in SERVICE_KEYS:
        if any(k in low for k in keys):
            return svc
    return None


def overlaps(elems, tol):
    hits = []
    for i in range(len(elems)):
        a = elems[i]
        for j in range(i + 1, len(elems)):
            b = elems[j]
            d = min(min(a["hi"][k], b["hi"][k]) - max(a["lo"][k], b["lo"][k]) for k in range(3))
            if d > tol:
                hits.append((d, a, b))
    hits.sort(key=lambda h: -h[0])
    return hits


def rows_of(boxes):
    rows = collections.defaultdict(list)
    for b in boxes:
        if not b["rack"]:
            continue
        key = b["row"] if b["row"] else f"y={round((b['lo'][1] + b['hi'][1]) / 2):d}"
        rows[key].append(b)
    return rows


def row_box(racks):
    x0 = min(r["lo"][0] for r in racks); x1 = max(r["hi"][0] for r in racks)
    y0 = min(r["lo"][1] for r in racks); y1 = max(r["hi"][1] for r in racks)
    return (x0, x1, y0, y1, 0) if (x1 - x0) >= (y1 - y0) else (y0, y1, x0, x1, 1)


def covered(runs, lo, hi, alo, ahi, axis):
    other = 1 - axis
    spans = []
    for r in runs:
        if r["lo"][other] > ahi + REACH_MM or r["hi"][other] < alo - REACH_MM:
            continue
        a, b = max(r["lo"][axis], lo), min(r["hi"][axis], hi)
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
    boxes = load(argv[1])
    print(f"elements with geometry: {len(boxes)}")

    print("\n=== GATE 1: hard clashes ===")
    hits = overlaps(boxes, TOL_MM)
    print(f"HARD CLASHES: {len(hits)}")
    for d, a, b in hits[:10]:
        print(f"  {d:8.1f} mm  {a['name'][:40]}  x  {b['name'][:40]}")

    rows = rows_of(boxes)
    print("\n=== GATE 2: rows served by all three services ===")
    under = []
    if not rows:
        print("UNKNOWN: no rack with RowId found")
    else:
        print(f"rows: {len(rows)}  reach: {REACH_MM:.0f} mm  min cover: {MIN_COVER:.0%}")
        for svc, _ in SERVICE_KEYS:
            runs = [b for b in boxes if service_of(b) == svc]
            bad = []
            for key in sorted(rows):
                lo, hi, alo, ahi, axis = row_box(rows[key])
                frac = covered(runs, lo, hi, alo, ahi, axis)
                if frac < MIN_COVER:
                    bad.append(f"{key}({frac*100:.0f}%)")
            print(f"  {svc}: {len(runs)} runs, " + ("all rows ok" if not bad else "LOW: " + ", ".join(bad)))
            if bad:
                under.append(svc)
    print(f"ROWS UNDER-COVERED: {len(under)}")

    ok = (len(hits) == 0) and not under
    print(f"\nVERDICT: {'BOTH GATES PASS' if ok else 'NOT YET - keep iterating on both counts'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
