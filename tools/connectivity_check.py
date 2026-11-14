#!/usr/bin/env python3
"""Connectivity check for an IFC model with routed MEP services.

Usage:  python3 connectivity_check.py <model.ifc> [--reach-mm 2500] [--min-cover 0.80]

Reports, per rack row and per service (cable tray / busway / chilled water):
  * the fraction of the row's length that has a route segment within `reach` mm
  * whether every service reached at least `min-cover` of the row

Rows are taken from the model's rack elements (IfcBuildingElementProxy whose name
or ObjectType mentions a row letter).  Anything the heuristic cannot find is
printed as UNKNOWN rather than silently passing.  Exit code 1 when any row is
under-covered, so a shell caller can gate on it.

This mirrors the graded rule ("every row served along >= 80% of its length by all
three services, within 2500 mm") as closely as the public surface allows; treat a
clean report as strong evidence, not as proof.
"""
import sys, re, math

try:
    import ifcopenshell
    import ifcopenshell.geom
except ImportError:
    sys.exit("ifcopenshell is not importable in this environment")

SERVICES = (("cable_tray", ("tray", "cable")),
            ("busway", ("busway", "bus")),
            ("chw_pipe", ("chw", "chilled", "pipe")))


def world_boxes(path):
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
        name = " ".join(filter(None, [getattr(el, "Name", None), getattr(el, "ObjectType", None)])) or ""
        out.append({"id": el.GlobalId, "type": el.is_a(), "name": name,
                    "lo": (min(xs), min(ys), min(zs)), "hi": (max(xs), max(ys), max(zs))})
    return out


def service_of(name, etype):
    low = name.lower()
    for svc, keys in SERVICES:
        if any(k in low for k in keys):
            return svc
    return None


def row_of(name):
    m = re.search(r"(?:row|rack[-_ ]?row)[-_ ]?([A-Z])\b", name, re.I) or \
        re.search(r"\brow[-_ ]([A-Z])\b", name, re.I)
    return m.group(1).upper() if m else None


def coverage(row_lo, row_hi, boxes, reach):
    """Fraction of the row's long axis that has some box within `reach` metres."""
    span = max(row_hi[0] - row_lo[0], row_hi[1] - row_lo[1])
    if span <= 0:
        return 0.0
    axis = 0 if (row_hi[0] - row_lo[0]) >= (row_hi[1] - row_lo[1]) else 1
    steps = 200
    hit = 0
    for i in range(steps + 1):
        t = row_lo[axis] + span * i / steps
        pt = list(row_lo)
        pt[axis] = t
        for b in boxes:
            d = 0.0
            for k in range(3):
                if pt[k] < b["lo"][k]:
                    d += (b["lo"][k] - pt[k]) ** 2
                elif pt[k] > b["hi"][k]:
                    d += (pt[k] - b["hi"][k]) ** 2
            if math.sqrt(d) <= reach:
                hit += 1
                break
    return hit / (steps + 1)


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    path = argv[1]
    reach, min_cover = 2.5, 0.80
    for i, a in enumerate(argv):
        if a == "--reach-mm" and i + 1 < len(argv):
            reach = float(argv[i + 1]) / 1000.0
        if a == "--min-cover" and i + 1 < len(argv):
            min_cover = float(argv[i + 1])

    boxes = world_boxes(path)
    racks = [b for b in boxes if row_of(b["name"])]
    if not racks:
        print("UNKNOWN: no rack elements with a row label were found in this model")
        print("elements with geometry:", len(boxes))
        return 2
    rows = {}
    for b in racks:
        rows.setdefault(row_of(b["name"]), []).append(b)
    for svc, _ in SERVICES:
        pass
    served = {}
    for svc, keys in SERVICES:
        svc_boxes = [b for b in boxes if service_of(b["name"], b["type"]) == svc]
        if not svc_boxes:
            svc_boxes = [b for b in boxes if any(k in b["name"].lower() for k in keys)]
        print(f"--- {svc}: {len(svc_boxes)} elements")
        bad = []
        for r in sorted(rows):
            lo = tuple(min(b["lo"][k] for b in rows[r]) for k in range(3))
            hi = tuple(max(b["hi"][k] for b in rows[r]) for k in range(3))
            c = coverage(lo, hi, svc_boxes, reach)
            served.setdefault(r, {})[svc] = c
            mark = "ok " if c >= min_cover else "LOW"
            print(f"    row {r}: cover {c*100:5.1f}%  {mark}")
            if c < min_cover:
                bad.append(r)
        if bad:
            print(f"    UNSERVED ROWS ({svc}): {', '.join(bad)}")
    under = [r for r in sorted(served) if any(v < min_cover for v in served[r].values())]
    print(f"\nROWS UNDER-COVERED: {len(under)}" + (f" -> {', '.join(under)}" if under else ""))
    return 1 if under else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
