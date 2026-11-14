#!/usr/bin/env python3
"""Clash check for an IFC model: report axis-aligned bounding-box overlaps.

Usage:  python3 clash_check.py <model.ifc> [--tolerance-mm 2] [--limit 40]

A "hard clash" is two distinct elements whose axis-aligned bounding boxes overlap
by more than the tolerance on all three axes.  Bounding boxes are conservative -
a reported pair still needs a look - but a clean report is strong evidence that
there is no hard clash at all.

Runs with ifcopenshell only (no numpy, no network).  Exit code 1 when clashes are
found so a shell caller can gate on it.
"""
import sys

try:
    import ifcopenshell
    import ifcopenshell.geom
except ImportError:
    sys.exit("ifcopenshell is not importable in this environment")


def boxes(path, tolerance):
    f = ifcopenshell.open(path)
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    out = []
    for el in f.by_type("IfcProduct"):
        if not getattr(el, "Representation", None):
            continue
        try:
            shape = ifcopenshell.geom.create_shape(settings, el)
        except Exception:
            continue
        verts = shape.geometry.verts
        if not verts:
            continue
        xs, ys, zs = verts[0::3], verts[1::3], verts[2::3]
        lo = (min(xs), min(ys), min(zs))
        hi = (max(xs), max(ys), max(zs))
        if all(hi[i] - lo[i] >= 0 for i in range(3)):
            out.append((el.GlobalId, el.is_a(), lo, hi))
    return out


def overlap(a, b):
    """Returns the smallest per-axis overlap in metres (negative => disjoint)."""
    lo_a, hi_a, lo_b, hi_b = a[2], a[3], b[2], b[3]
    return min(min(hi_a[i], hi_b[i]) - max(lo_a[i], lo_b[i]) for i in range(3))


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    path = argv[1]
    tol = 0.002
    limit = 40
    for i, a in enumerate(argv):
        if a == "--tolerance-mm" and i + 1 < len(argv):
            tol = float(argv[i + 1]) / 1000.0
        if a == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1])

    elems = boxes(path, tol)
    print(f"elements with geometry: {len(elems)}  tolerance: {tol*1000:.1f} mm")
    hits = []
    for i in range(len(elems)):
        for j in range(i + 1, len(elems)):
            d = overlap(elems[i], elems[j])
            if d > tol:
                hits.append((d, elems[i], elems[j]))
    hits.sort(key=lambda h: -h[0])
    print(f"HARD CLASHES: {len(hits)}")
    for d, a, b in hits[:limit]:
        print(f"  {d*1000:8.1f} mm  {a[1]}({a[0]})  x  {b[1]}({b[0]})")
    if len(hits) > limit:
        print(f"  ... and {len(hits)-limit} more")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
