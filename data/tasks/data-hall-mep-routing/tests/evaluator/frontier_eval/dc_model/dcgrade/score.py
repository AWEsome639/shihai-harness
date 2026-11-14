"""Score a submitted IFC4 model against the ground truth.

Implements the two task scores::

    Task A   score = recovery x semantics x (1 - violation_rate)
    Task B   score = compliance x min(1, L_opt / L_agent), behind three gates

**Nothing is fused.** Every call returns ``clash_count``,
``violations_by_rule``, ``recovery_confusion``, ``routed_length`` and
``exec_status`` alongside the score, because
Chamfer-like diagnostics must not be collapsed into one number, and because an
execute-and-fix loop cannot act on a scalar.

Three implementation notes that are easy to get wrong:

* ``express_rules=True`` is mandatory in gate 0. Without it
  ``ifcopenshell.validate.validate`` silently passes a file whose profiles use
  3D ``IfcCartesianPoint``s.
* ``ifcopenshell.geom`` emits **SI metres** whatever the file's own unit is, so
  every bound read here is scaled by 1000 into the mm convention the rest of
  dc_model uses. Reading raw ``IfcCartesianPoint`` coordinates instead would
  silently mis-scale a metre-based submission by 1000.
* Facing is taken from the **placement rotation**, not from
  ``Pset_DCM_Rack.FacingDirection``. A rack box is symmetric under a 180-degree
  turn, so the pset is the only human-readable record — but it is intent, and
  the placement is what was realised. The disagreement is reported as an
  advisory rule rather than silently trusted.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
import multiprocessing
import tempfile
from typing import Sequence

import numpy as np

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.placement
import ifcopenshell.util.unit
import ifcopenshell.validate

from ..dcgen import specs
from ..dcgen.units import MM_PER_M
from . import rules
from .rules import Elem, Scene

#: A recovery match tolerates 150 mm of centroid error.
POSITION_TOLERANCE_MM = 150.0

#: Minimum penetration that counts as a hard clash, in metres — the ifcclash /
#: Bonsai default. Anything smaller is joinery tolerance, not a site stoppage.
CLASH_TOLERANCE_M = 0.002

#: The three semantics checks, in order.
SEMANTIC_CHECKS = ("hall_space_present", "storey_nesting", "rack_psets_complete")

#: Bounds are read in world space so an element's realised position needs no
#: further transform.
_GEOM_SETTINGS = ifcopenshell.geom.settings()
_GEOM_SETTINGS.set("use-world-coords", True)

#: The clash tree applies each element's placement itself, so it must be fed
#: LOCAL geometry. Handing it world coordinates transforms everything twice —
#: which does not error, does not obviously misbehave, and quietly makes gate 1
#: a false negative. These are ifcclash's own defaults, deliberately.
_CLASH_SETTINGS = ifcopenshell.geom.settings()


# --------------------------------------------------------------------------
# gate 0 — it parses, and it is schema- and EXPRESS-conformant
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    passed: bool
    status: str
    reason: str = ""
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"passed": self.passed, "status": self.status,
                "reason": self.reason, "errors": list(self.errors[:20]),
                "n_errors": len(self.errors)}


def gate_0(path: str | Path, max_reported: int = 20
           ) -> tuple[ifcopenshell.file | None, Gate]:
    """Parse and validate a submission. Failure is score 0 with a reason.

    ``express_rules=True`` is not optional. The EXPRESS WHERE rules are where
    ``IfcArbitraryClosedProfileDef.WR1`` lives, and without them a file whose
    every profile is 3D validates clean.
    """
    try:
        model = ifcopenshell.open(str(path))
    except Exception as exc:  # ifcopenshell raises bare RuntimeError/IOError
        return None, Gate(False, "ERR_PARSE", f"cannot parse as IFC: {exc}")

    if model.schema != "IFC4":
        return None, Gate(
            False, "ERR_SCHEMA",
            f"schema is {model.schema}, the task requires IFC4",
        )

    logger = ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(model, logger, express_rules=True)
    if logger.statements:
        messages = tuple(str(s.get("message", s)) for s in logger.statements)
        return None, Gate(
            False, "ERR_VALIDATE",
            f"{len(messages)} schema/EXPRESS violations, first: {messages[0][:200]}",
            messages[:max_reported],
        )
    return model, Gate(True, "OK")


# --------------------------------------------------------------------------
# IFC -> Scene
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WorldShape:
    """One element's realised geometry, reduced to what the grader needs."""

    min_mm: tuple[float, float, float]
    max_mm: tuple[float, float, float]
    #: mesh volume / AABB volume. Only a right rectangular prism fills its own
    #: axis-aligned box, so a ratio of 1 is an exact box-ness certificate — and
    #: for two boxes, AABB overlap *is* solid interpenetration.
    fill_ratio: float

    @property
    def is_box(self) -> bool:
        return self.fill_ratio > 1.0 - _BOX_FILL_EPS


#: How far a mesh's volume may fall short of its bounding box and still be
#: treated as a box. Triangulation of a rectangular prism is exact, so the only
#: slack needed is float noise; a cylinder sits at 0.785 and a 45-degree-rotated
#: box at 0.5, both far outside.
_BOX_FILL_EPS = 1.0e-3


def _mesh_volume(verts: np.ndarray, faces: np.ndarray) -> float:
    """Signed volume of a closed triangle mesh, by the divergence theorem."""
    if faces.size == 0:
        return 0.0
    tri = verts[faces.reshape(-1, 3)]
    return float(abs(np.einsum(
        "ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum()) / 6.0)


def _world_shapes(model: ifcopenshell.file,
                  products: Sequence) -> dict[int, WorldShape]:
    """World-space AABB + box-ness in mm, keyed by STEP id."""
    out: dict[int, WorldShape] = {}
    if not products:
        return out
    iterator = ifcopenshell.geom.iterator(
        _GEOM_SETTINGS, model, 1, include=list(products))
    if not iterator.initialize():
        return out
    while True:
        shape = iterator.get()
        verts = np.asarray(shape.geometry.verts, dtype=float).reshape(-1, 3)
        if verts.size:
            lo, hi = verts.min(axis=0), verts.max(axis=0)
            extents = hi - lo
            box_volume = float(np.prod(extents))
            faces = np.asarray(shape.geometry.faces, dtype=np.int64)
            ratio = (_mesh_volume(verts, faces) / box_volume
                     if box_volume > 0.0 else 0.0)
            out[shape.id] = WorldShape(
                min_mm=tuple(lo * MM_PER_M), max_mm=tuple(hi * MM_PER_M),
                fill_ratio=ratio)
        if not iterator.next():
            break
    return out


def _world_bounds_mm(model: ifcopenshell.file,
                     products: Sequence) -> dict[int, tuple[tuple, tuple]]:
    """World-space AABBs in mm, keyed by STEP id, for every product with a shape."""
    return {sid: (s.min_mm, s.max_mm)
            for sid, s in _world_shapes(model, products).items()}


def _facing_from_placement(product) -> str | None:
    """Cardinal direction the element's local +Y points, from its placement.

    dcgen rotates a cabinet about +Z to face it, with 0 degrees = NORTH, so the
    realised facing is recoverable from the placement matrix alone. An angle
    more than a degree off a cardinal is reported as ``None`` rather than
    guessed at; no placement at all is an unrotated element, which is NORTH.

    The quadrant table reads NORTH, WEST, SOUTH, EAST — anticlockwise, because
    a right-handed rotation about +Z is anticlockwise. A +90 degree turn takes
    the front from +Y to -X, which is WEST. This agrees with every
    rule that acts on a facing (``rules._FRONT_AXIS``,
    ``program.clearance_zones``, ``build.working_space``,
    ``units.facing_normal``), all of which put an EAST-facing front at +X.
    """
    if product.ObjectPlacement is None:
        return "NORTH"
    matrix = ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)
    degrees = math.degrees(math.atan2(matrix[1][0], matrix[0][0])) % 360.0
    quadrant = round(degrees / 90.0) % 4
    if abs(degrees - quadrant * 90.0) > 1.0 and abs(degrees - 360.0) > 1.0:
        return None
    return ("NORTH", "WEST", "SOUTH", "EAST")[quadrant]


def _psets_of(product) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for rel in getattr(product, "IsDefinedBy", None) or []:
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        definition = rel.RelatingPropertyDefinition
        if not definition.is_a("IfcPropertySet"):
            continue
        props: dict[str, object] = {}
        for prop in definition.HasProperties or []:
            if prop.is_a("IfcPropertySingleValue"):
                value = prop.NominalValue
                props[prop.Name] = value.wrappedValue if value is not None else None
        out[definition.Name] = props
    return out


def _space_polygon_mm(model: ifcopenshell.file, space
                      ) -> tuple[tuple[float, float], ...] | None:
    """The hall footprint, from its swept profile — not from its bounding box.

    The ``l_shaped`` knob notches a corner out of the room, so anything that
    treats the hall as a rectangle will place equipment in the void and read
    clearance bands that leave the building as compliant.
    """
    if space is None or space.Representation is None:
        return None
    scale = ifcopenshell.util.unit.calculate_unit_scale(model) * MM_PER_M
    for representation in space.Representation.Representations:
        for item in representation.Items or []:
            if not item.is_a("IfcExtrudedAreaSolid"):
                continue
            profile = item.SweptArea
            if not profile.is_a("IfcArbitraryClosedProfileDef"):
                continue
            curve = profile.OuterCurve
            if not curve.is_a("IfcPolyline"):
                continue
            points = [(p.Coordinates[0] * scale, p.Coordinates[1] * scale)
                      for p in curve.Points]
            if len(points) > 2 and points[0] == points[-1]:
                points = points[:-1]
            offset = np.eye(4)
            if item.Position is not None:
                offset = ifcopenshell.util.placement.get_axis2placement(item.Position)
            placement = np.eye(4)
            if space.ObjectPlacement is not None:
                placement = ifcopenshell.util.placement.get_local_placement(
                    space.ObjectPlacement)
            world = placement @ offset
            return tuple(
                (float(world[0][0] * x + world[0][1] * y + world[0][3] * scale),
                 float(world[1][0] * x + world[1][1] * y + world[1][3] * scale))
                for x, y in points
            )
    return None


def _solid_elements(model: ifcopenshell.file) -> list:
    """Physical elements, minus voids.

    ``IfcFeatureElement`` covers openings and recesses: subtracted volume, not
    matter. ifcclash drops them for the same reason — an opening "clashes" with
    every wall it is cut into.
    """
    return [e for e in model.by_type("IfcElement")
            if not e.is_a("IfcFeatureElement")]


def build_scene(model: ifcopenshell.file) -> Scene:
    """Reduce a model to the realised geometry the rules and matcher need."""
    physical = _solid_elements(model)
    spaces = model.by_type("IfcSpace")
    hall_product = spaces[0] if spaces else None

    wanted = list(physical) + ([hall_product] if hall_product is not None else [])
    bounds = _world_bounds_mm(model, wanted)

    def to_elem(product) -> Elem | None:
        box = bounds.get(product.id())
        if box is None:
            return None  # no body representation: nothing realised to grade
        predefined = getattr(product, "PredefinedType", None)
        object_type = getattr(product, "ObjectType", None)
        kind = rules.classify_product(product)
        return Elem(
            eid=product.GlobalId, name=product.Name or "",
            kind=kind, ifc_class=product.is_a(),
            predefined_type=predefined, object_type=object_type,
            min_mm=box[0], max_mm=box[1],
            facing=(_facing_from_placement(product)
                    if kind in rules.FRONTED_KINDS else None),
            psets=_psets_of(product),
        )

    elements = tuple(e for e in (to_elem(p) for p in physical) if e is not None)
    hall = to_elem(hall_product) if hall_product is not None else None
    if hall is not None:
        hall = replace(hall, kind="hall")
    return Scene(
        elements=elements, hall=hall,
        hall_polygon_mm=_space_polygon_mm(model, hall_product),
    )


# --------------------------------------------------------------------------
# recovery — Hungarian assignment, agent -> ground truth
# --------------------------------------------------------------------------


def hungarian(cost: Sequence[Sequence[float]]) -> list[tuple[int, int]]:
    """Minimum-cost perfect assignment on a rectangular matrix (rows <= cols).

    Kuhn-Munkres with potentials, O(n^2 m). Hand-rolled because scipy is not in
    the scoring venv and will not be added: the container pins
    ``ifcopenshell==0.8.4`` and every extra wheel is another thing that can
    drift between the grader we test and the grader agents face.
    """
    n = len(cost)
    if n == 0:
        return []
    m = len(cost[0])
    if m < n:
        raise ValueError("hungarian() requires rows <= cols; transpose first")

    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    match = [0] * (m + 1)          # column -> row (1-based, 0 = unmatched)
    way = [0] * (m + 1)
    inf = float("inf")

    for row in range(1, n + 1):
        match[0] = row
        col = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[col] = True
            cur_row, delta, next_col = match[col], inf, 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                reduced = cost[cur_row - 1][j - 1] - u[cur_row] - v[j]
                if reduced < minv[j]:
                    minv[j], way[j] = reduced, col
                if minv[j] < delta:
                    delta, next_col = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[match[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            col = next_col
            if match[col] == 0:
                break
        while col:
            previous = way[col]
            match[col] = match[previous]
            col = previous

    return [(match[j] - 1, j - 1) for j in range(1, m + 1) if match[j]]


def _type_key(elem: Elem) -> tuple:
    """What must agree before two elements are even candidates for a match.

    Uses the resolved dcgen kind when known so an agent writing
    ``ObjectType="RACK"`` still matches a ``"Rack"``, and the raw IFC signature
    otherwise. The IFC class is part of that signature along with
    (predefined_type, ObjectType): busway and CHW pipe differ by
    class, and matching across them would call a pipe a busbar.
    """
    if elem.kind != "unknown":
        return ("kind", elem.kind)
    return ("raw", elem.ifc_class.upper(),
            (elem.predefined_type or "").upper(), (elem.object_type or "").upper())


@dataclass(frozen=True)
class Recovery:
    matched: int
    missing: int
    spurious: int
    mistyped: int
    n_ground_truth: int
    n_agent: int
    pairs: tuple[tuple[str, str], ...] = ()
    missing_ids: tuple[str, ...] = ()
    spurious_ids: tuple[str, ...] = ()
    mistyped_ids: tuple[tuple[str, str, str], ...] = ()
    #: right place, wrong IFC type — diagnostic only, never scored
    type_confusions: tuple[tuple[str, str, str], ...] = ()

    @property
    def score(self) -> float:
        """``M / (|GT| + S)``. Spurious sits in the denominator on purpose.

        Inventing extra racks therefore lowers recovery without needing a
        separate penalty term, and the value stays in [0, 1] by construction.
        """
        denominator = self.n_ground_truth + self.spurious
        return (self.matched / denominator) if denominator else 1.0

    @property
    def score_ignoring_facing(self) -> float:
        """Diagnostic only, never scored: recovery if facing did not count.

        Facing is one binary attribute and the recovery match makes it
        all-or-nothing, so a model that reads every direction correctly into
        ``Pset_DCM_Rack`` and then never rotates a placement halves its
        recovery. That is a real modelling failure and it is graded as one —
        but it can be the *whole* Task A gap, and a report that showed only the
        fused number could not say so.
        """
        denominator = self.n_ground_truth + self.spurious
        return ((self.matched + self.mistyped) / denominator) if denominator else 1.0

    def to_dict(self) -> dict:
        return {
            "recovery": self.score,
            "recovery_ignoring_facing": self.score_ignoring_facing,
            "matched": self.matched, "missing": self.missing,
            "spurious": self.spurious, "mistyped": self.mistyped,
            "n_ground_truth": self.n_ground_truth, "n_agent": self.n_agent,
            "missing_ids": list(self.missing_ids),
            "spurious_ids": list(self.spurious_ids),
            "mistyped_pairs": [
                {"gt": g, "agent": a, "why": why} for g, a, why in self.mistyped_ids
            ],
            "type_confusions": [
                {"gt": g, "agent": a, "why": why} for g, a, why in self.type_confusions
            ],
        }


def _distance_mm(a: Elem, b: Elem) -> float:
    return math.dist(a.centroid_mm, b.centroid_mm)


def _assign_within(gt: list[Elem], agent: list[Elem], tolerance_mm: float
                   ) -> list[tuple[Elem, Elem]]:
    """Hungarian assignment inside one type group, dropping over-tolerance pairs."""
    if not gt or not agent:
        return []
    big = tolerance_mm * 1.0e6  # finite so the potentials stay well defined
    transposed = len(gt) > len(agent)
    rows, cols = (agent, gt) if transposed else (gt, agent)
    cost = [[_pair_cost(r, c, tolerance_mm, big) for c in cols] for r in rows]
    out: list[tuple[Elem, Elem]] = []
    for i, j in hungarian(cost):
        if cost[i][j] >= big:
            continue  # assigned only because the matrix had to be filled
        out.append((cols[j], rows[i]) if transposed else (rows[i], cols[j]))
    return out


def _pair_cost(a: Elem, b: Elem, tolerance_mm: float, big: float) -> float:
    distance = _distance_mm(a, b)
    return distance if distance <= tolerance_mm else big


def match_elements(gt: Scene, agent: Scene,
                   tolerance_mm: float = POSITION_TOLERANCE_MM) -> Recovery:
    """Hungarian match agent elements onto ground truth, then classify.

    A pair that agrees on type and lands within tolerance is a match *only* if
    the facing also agrees; otherwise it is mistyped, which costs the match
    without also counting the agent's element as spurious.
    """
    groups: dict[tuple, tuple[list[Elem], list[Elem]]] = {}
    for elem in gt.elements:
        groups.setdefault(_type_key(elem), ([], []))[0].append(elem)
    for elem in agent.elements:
        groups.setdefault(_type_key(elem), ([], []))[1].append(elem)

    matched_gt: set[str] = set()
    matched_agent: set[str] = set()
    pairs: list[tuple[str, str]] = []
    mistyped: list[tuple[str, str, str]] = []

    for gt_group, agent_group in groups.values():
        for gt_elem, agent_elem in _assign_within(gt_group, agent_group, tolerance_mm):
            matched_gt.add(gt_elem.eid)
            matched_agent.add(agent_elem.eid)
            if gt_elem.facing is not None and gt_elem.facing != agent_elem.facing:
                mistyped.append((
                    gt_elem.eid, agent_elem.eid,
                    f"{agent_elem.name or agent_elem.eid} faces "
                    f"{agent_elem.facing}, ground truth faces {gt_elem.facing}",
                ))
            else:
                pairs.append((gt_elem.eid, agent_elem.eid))

    missing = [e for e in gt.elements if e.eid not in matched_gt]
    spurious = [e for e in agent.elements if e.eid not in matched_agent]

    # Diagnostic only: an agent element in the right place but of the wrong IFC
    # type is already counted as spurious + missing above, exactly as the design
    # spec's formula requires. Naming the confusion does not change the score,
    # it just tells the fix loop what to fix.
    confusions: list[tuple[str, str, str]] = []
    taken: set[str] = set()
    for gt_elem in missing:
        for agent_elem in spurious:
            if agent_elem.eid in taken:
                continue
            if _distance_mm(gt_elem, agent_elem) <= tolerance_mm:
                taken.add(agent_elem.eid)
                confusions.append((
                    gt_elem.eid, agent_elem.eid,
                    f"expected {gt_elem.kind}/{gt_elem.ifc_class}, "
                    f"got {agent_elem.kind}/{agent_elem.ifc_class}",
                ))
                break

    return Recovery(
        matched=len(pairs), missing=len(missing), spurious=len(spurious),
        mistyped=len(mistyped),
        n_ground_truth=len(gt.elements), n_agent=len(agent.elements),
        pairs=tuple(pairs),
        missing_ids=tuple(e.eid for e in missing),
        spurious_ids=tuple(e.eid for e in spurious),
        mistyped_ids=tuple(mistyped), type_confusions=tuple(confusions),
    )


# --------------------------------------------------------------------------
# semantics — three binary checks
# --------------------------------------------------------------------------


def _resolve_storey(product):
    """The IfcBuildingStorey an element ultimately sits under, or None.

    An element is *contained* in a spatial structure; an ``IfcSpace`` is
    *aggregated* into the storey. Following only one of the two relations finds
    nothing for a correctly authored model.
    """
    seen: set[int] = set()
    container = None
    for rel in getattr(product, "ContainedInStructure", None) or []:
        container = rel.RelatingStructure
        break
    while container is not None and container.id() not in seen:
        seen.add(container.id())
        if container.is_a("IfcBuildingStorey"):
            return container
        parent = None
        for rel in getattr(container, "Decomposes", None) or []:
            parent = rel.RelatingObject
            break
        container = parent
    return None


def semantics(agent_model: ifcopenshell.file, gt_model: ifcopenshell.file) -> dict:
    """The three binary semantics checks, and their evidence."""
    gt_storeys = gt_model.by_type("IfcBuildingStorey")
    gt_storey_name = (gt_storeys[0].Name or "").strip().upper() if gt_storeys else ""
    agent_storeys = agent_model.by_type("IfcBuildingStorey")

    spaces = agent_model.by_type("IfcSpace")
    hall_present = bool(spaces)

    misplaced: list[str] = []
    for product in _solid_elements(agent_model):
        storey = _resolve_storey(product)
        if storey is None:
            misplaced.append(product.GlobalId)
            continue
        # with a single storey there is only one possible answer, so a differing
        # name is a naming choice rather than a nesting error
        name = (storey.Name or "").strip().upper()
        if len(agent_storeys) > 1 and name != gt_storey_name:
            misplaced.append(product.GlobalId)
    storey_ok = not misplaced

    required = _required_rack_properties(gt_model)
    incomplete: list[str] = []
    agent_racks = [
        p for p in _solid_elements(agent_model)
        if rules.classify(p.is_a(), getattr(p, "PredefinedType", None),
                          getattr(p, "ObjectType", None)) == "rack"
    ]
    for rack in agent_racks:
        props = _psets_of(rack).get("Pset_DCM_Rack", {})
        if any(props.get(key) is None for key in required):
            incomplete.append(rack.GlobalId)
    gt_has_racks = any(
        rules.classify(p.is_a(), getattr(p, "PredefinedType", None),
                       getattr(p, "ObjectType", None)) == "rack"
        for p in _solid_elements(gt_model)
    )
    # a model with no racks at all must not pass by vacuity when the answer key
    # has racks; recovery already scores 0 there, and the checks must agree
    psets_ok = not incomplete and (bool(agent_racks) or not gt_has_racks)

    passed = {"hall_space_present": hall_present,
              "storey_nesting": storey_ok,
              "rack_psets_complete": psets_ok}
    return {
        "semantics": sum(passed.values()) / len(SEMANTIC_CHECKS),
        "checks": passed,
        "required_rack_properties": sorted(required),
        "elements_not_under_storey": misplaced[:20],
        "racks_with_incomplete_pset": incomplete[:20],
    }


def _required_rack_properties(gt_model: ifcopenshell.file) -> set[str]:
    """Which ``Pset_DCM_Rack`` keys count as "complete", read off the answer key.

    Derived rather than restated so the required set cannot drift out of step
    with what dcgen actually writes.
    """
    required: set[str] = set()
    for product in _solid_elements(gt_model):
        if rules.classify(product.is_a(), getattr(product, "PredefinedType", None),
                          getattr(product, "ObjectType", None)) != "rack":
            continue
        required |= set(_psets_of(product).get("Pset_DCM_Rack", {}))
    return required


# --------------------------------------------------------------------------
# Task B gates and objective
# --------------------------------------------------------------------------


def _box_clashes(model: ifcopenshell.file, elements: Sequence,
                 tolerance_mm: float) -> list[tuple[str, str, str]]:
    """Interpenetrating pairs among elements whose meshes are exact boxes.

    This is not an approximation *where it fires*. Two axis-aligned right
    prisms overlap as solids exactly when their AABBs overlap, so restricting
    the test to meshes that fill their own bounding box (:attr:`WorldShape.is_box`)
    makes it an exact interpenetration test rather than a conservative one.
    Anything else — a cylinder, an L-shaped prism, a mesh rotated off the axes —
    is left to the BVH, which is right about those and wrong only about the
    degenerate coplanar case that boxes hit.
    """
    shapes = _world_shapes(model, list(elements))
    boxes = [(e, shapes[e.id()]) for e in elements
             if e.id() in shapes and shapes[e.id()].is_box]
    out: list[tuple[str, str, str]] = []
    for i, (a, sa) in enumerate(boxes):
        for b, sb in boxes[i + 1:]:
            overlap = [min(sa.max_mm[k], sb.max_mm[k]) - max(sa.min_mm[k], sb.min_mm[k])
                       for k in range(3)]
            if min(overlap) <= tolerance_mm:
                continue
            out.append((
                a.GlobalId, b.GlobalId,
                f"{a.Name or a.is_a()} x {b.Name or b.is_a()} "
                f"(interpenetration {overlap[0]:.0f}x{overlap[1]:.0f}x"
                f"{overlap[2]:.0f}mm)",
            ))
    return out



def _bvh_worker(ifc_path: str, tolerance_m: float, out_path: str) -> None:
    """Child-process entry: run only the two BVH detectors and write JSON.
    Any native crash kills only this process; the parent continues with AABB.
    """
    import json as _json
    try:
        model = ifcopenshell.open(ifc_path)
        elements = [e for e in model.by_type("IfcElement")
                    if not e.is_a("IfcFeatureElement")]
        if not elements:
            Path(out_path).write_text("[]", encoding="utf-8")
            return
        tree = ifcopenshell.geom.tree()
        iterator = ifcopenshell.geom.iterator(
            _CLASH_SETTINGS, model, 1, include=elements)
        if not iterator.initialize():
            Path(out_path).write_text("[]", encoding="utf-8")
            return
        while True:
            tree.add_element(iterator.get())
            if not iterator.next():
                break
        pairs = []
        for results in (
            tree.clash_intersection_many(elements, elements,
                                         tolerance=tolerance_m, check_all=False),
            tree.clash_collision_many(elements, elements, allow_touching=False),
        ):
            for clash in results:
                a_id = clash.a.get_argument(0)
                a_name = clash.a.get_argument(2)
                b_id = clash.b.get_argument(0)
                b_name = clash.b.get_argument(2)
                pairs.append({
                    "a_id": a_id, "b_id": b_id,
                    "a_name": a_name or "", "b_name": b_name or "",
                    "a_type": clash.a.is_a(), "b_type": clash.b.is_a(),
                    "clash_type": int(getattr(clash, "clash_type", -1)),
                })
        Path(out_path).write_text(_json.dumps(pairs), encoding="utf-8")
    except Exception:
        # Any Python-level failure also leaves an empty result for the parent.
        Path(out_path).write_text("[]", encoding="utf-8")


def hard_clashes(model: ifcopenshell.file,
                 tolerance_m: float = CLASH_TOLERANCE_M,
                 ifc_path: str | Path | None = None,
                 bvh_timeout_s: float = 45.0) -> list[tuple[str, str, str]]:
    """Every pair of elements whose solids interpenetrate.

    Three detectors, unioned, because no one of them sees everything:

    * ``clash_intersection_many`` — the BVH ``ifcclash`` drives, keyed on
      protrusion distance.
    * ``clash_collision_many`` — catches same-elevation partial overlaps that
      the protrusion test scores as zero.
    * :func:`_box_clashes` — exact AABB overlap between meshes that are boxes.

    BVH detectors have been observed to native-crash (0xC0000005) on some
    meshes under ifcopenshell 0.8.4.  A Python try/except cannot catch a
    segfault, so when an on-disk ``ifc_path`` is supplied the BVH work is
    executed in a child process.  If the child exits non-zero, times out, or
    produces no output, the parent continues with the AABB detector alone.
    """
    elements = _solid_elements(model)
    if not elements:
        return []
    kinds = {e.GlobalId: rules.classify_product(e) for e in elements}

    def excused(a_id: str, b_id: str) -> bool:
        pair = {kinds.get(a_id), kinds.get(b_id)}
        return "raised_floor" in pair and bool(pair & set(rules.OBSTRUCTION_KINDS))

    found: dict[frozenset, tuple[str, str, str]] = {}

    # ---- BVH detectors (child process when a path is available) -------------
    bvh_pairs: list[dict] = []
    if ifc_path is not None:
        try:
            with tempfile.TemporaryDirectory(prefix="dcclash_") as tmp:
                out_file = str(Path(tmp) / "bvh_pairs.json")
                ctx = multiprocessing.get_context("spawn")
                proc = ctx.Process(
                    target=_bvh_worker,
                    args=(str(ifc_path), float(tolerance_m), out_file),
                )
                proc.start()
                proc.join(timeout=bvh_timeout_s)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=5)
                    bvh_pairs = []
                elif proc.exitcode == 0 and Path(out_file).is_file():
                    try:
                        bvh_pairs = json.loads(Path(out_file).read_text(encoding="utf-8"))
                    except Exception:
                        bvh_pairs = []
                else:
                    bvh_pairs = []
        except Exception:
            bvh_pairs = []
    else:
        # No path available — try in-process (may still native-crash).
        try:
            tree = ifcopenshell.geom.tree()
            iterator = ifcopenshell.geom.iterator(
                _CLASH_SETTINGS, model, 1, include=elements)
            if iterator.initialize():
                while True:
                    tree.add_element(iterator.get())
                    if not iterator.next():
                        break
                for results in (
                    tree.clash_intersection_many(elements, elements,
                                                 tolerance=tolerance_m, check_all=False),
                    tree.clash_collision_many(elements, elements, allow_touching=False),
                ):
                    for clash in results:
                        bvh_pairs.append({
                            "a_id": clash.a.get_argument(0),
                            "b_id": clash.b.get_argument(0),
                            "a_name": clash.a.get_argument(2) or "",
                            "b_name": clash.b.get_argument(2) or "",
                            "a_type": clash.a.is_a(),
                            "b_type": clash.b.is_a(),
                            "clash_type": int(getattr(clash, "clash_type", -1)),
                        })
        except Exception:
            bvh_pairs = []

    for p in bvh_pairs:
        a_id, b_id = p["a_id"], p["b_id"]
        if excused(a_id, b_id):
            continue
        detail = (f"{p.get('a_name') or p.get('a_type')} x "
                  f"{p.get('b_name') or p.get('b_type')} "
                  f"(bvh:{p.get('clash_type', -1)})")
        found.setdefault(frozenset((a_id, b_id)), (a_id, b_id, detail))

    # ---- AABB detector (always runs in the parent) --------------------------
    for a_id, b_id, detail in _box_clashes(
            model, elements, tolerance_m * MM_PER_M):
        if not excused(a_id, b_id):
            found.setdefault(frozenset((a_id, b_id)), (a_id, b_id, detail))
    return sorted(found.values())


#: Which route kinds every rack row must be served by, per gate 2.
REQUIRED_SERVICES = ("cable_tray", "busway", "chw_pipe")


def _rows(scene: Scene) -> dict[str, list[Elem]]:
    """Rack rows, keyed by ``Pset_DCM_Rack.RowId`` with a geometric fallback."""
    rows: dict[str, list[Elem]] = {}
    for rack in scene.of_kind("rack"):
        row_id = rack.psets.get("Pset_DCM_Rack", {}).get("RowId")
        key = str(row_id) if row_id else f"y={round(rack.centroid_mm[1]):d}"
        rows.setdefault(key, []).append(rack)
    return rows


#: How far a run may sit from the row it serves, and how much of the row it has
#: to cover. Both come from :mod:`dcgen.specs` so the oracle router and this
#: gate cannot drift apart — a router optimising against a different reach than
#: the grader measures would produce an ``L_opt`` no submission can be compared
#: with.
SERVICE_REACH_MM = float(specs.SERVICE_REACH)
SERVICE_COVERAGE = float(specs.SERVICE_COVERAGE_FRACTION)


def _row_box(racks: Sequence[Elem]) -> tuple[float, float, float, float, int]:
    """``(lo, hi, across_lo, across_hi, long_axis)`` for a rack row.

    The long axis is derived, not assumed. This generator lays rows along X,
    but a grader that hard-codes that would silently pass any submission whose
    rows run the other way.
    """
    x0 = min(r.min_mm[0] for r in racks)
    y0 = min(r.min_mm[1] for r in racks)
    x1 = max(r.max_mm[0] for r in racks)
    y1 = max(r.max_mm[1] for r in racks)
    axis = 0 if (x1 - x0) >= (y1 - y0) else 1
    return ((x0, x1, y0, y1, 0) if axis == 0 else (y0, y1, x0, x1, 1))


def _covered_fraction(runs: Sequence[Elem], lo: float, hi: float,
                      across_lo: float, across_hi: float, axis: int) -> float:
    """Fraction of ``lo..hi`` covered by runs that are within reach across."""
    other = 1 - axis
    spans: list[tuple[float, float]] = []
    for run in runs:
        if (run.min_mm[other] > across_hi + SERVICE_REACH_MM
                or run.max_mm[other] < across_lo - SERVICE_REACH_MM):
            continue
        a, b = max(run.min_mm[axis], lo), min(run.max_mm[axis], hi)
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


#: How far apart two segments of the same service may be and still count as
#: joined. A real pathway is made of straight sections and fittings, and a
#: submission that models the sections but not the fittings has still described
#: one network — a quarter of a metre is more slack than any fitting needs and
#: far less than a missing main.
JOINT_TOLERANCE_MM = 250.0


def _gap_mm(a: Elem, b: Elem, axes: Sequence[int] = (0, 1, 2)) -> float:
    """How far apart two boxes are on their worst axis; negative where they
    overlap.

    Named because the joint test and the CHW reach test are the same question
    at two tolerances. ``_box_clashes`` keeps its own inline form: it needs
    the per-axis overlaps for its interpenetration message, not a scalar.
    """
    return max(max(a.min_mm[i], b.min_mm[i]) - min(a.max_mm[i], b.max_mm[i])
               for i in axes)


def _joined(a: Elem, b: Elem) -> bool:
    return _gap_mm(a, b) < JOINT_TOLERANCE_MM


def networks(runs: Sequence[Elem]) -> list[list[Elem]]:
    """Connected components of one service's segments."""
    seen: set[str] = set()
    out: list[list[Elem]] = []
    for start in runs:
        if start.eid in seen:
            continue
        seen.add(start.eid)
        stack, component = [start], []
        while stack:
            node = stack.pop()
            component.append(node)
            for other in runs:
                if other.eid not in seen and _joined(node, other):
                    seen.add(other.eid)
                    stack.append(other)
        out.append(component)
    return out


def service_entry_mm(gt: Scene) -> tuple[float, float] | None:
    """Where the services enter the room, read off the answer key's own trunk.

    The westernmost point of the ground truth's route network. The drawings
    mark the entry with a leader and a dimension, and the generator routes from
    it, so the two agree by construction without the grader needing a second
    channel to be told.
    """
    runs = gt.of_kind(*REQUIRED_SERVICES)
    if not runs:
        return None
    west = min(runs, key=lambda r: r.min_mm[0])
    return (west.min_mm[0], (west.min_mm[1] + west.max_mm[1]) / 2.0)


def connectivity(gt: Scene, agent: Scene) -> dict:
    """Gate 2 — is every rack row actually served by tray, busway and CHW?

    This is the anti-degenerate guard: without it, routing nothing is
    clash-free and perfectly compliant.

    Asking only whether some run of each service intersects the row's bounding
    band is not the same question: one spine running up the west wall clips
    every row's band, so every row would read as served with no branch reaching
    any row at all.

    A row is served when runs of that service, lying within
    :data:`SERVICE_REACH_MM` across the row, together cover at least
    :data:`SERVICE_COVERAGE` of its length. Both numbers are quoted to the agent
    in the task statement — a gate the submission cannot see is a trap, not a
    requirement.
    """
    # ---- a route has to BE a route: one network per service, reaching the
    # point the drawings say the services enter the room.
    #
    # Without this, gate 2 rewards a submission that is not a route at all.
    # Three parallel segments per service with no main feeding any of them —
    # three objects in the same building rather than one system — would
    # otherwise score perfectly, beating honest submissions because connecting
    # things costs length and the objective term rewards being short.
    entry = service_entry_mm(gt)
    disconnected: dict[str, str] = {}
    for service in REQUIRED_SERVICES:
        runs = agent.of_kind(service)
        if not runs:
            continue                       # the coverage test already fails it
        parts = networks(runs)
        if len(parts) > 1:
            disconnected[service] = (
                f"{len(parts)} separate networks, largest {max(len(p) for p in parts)} "
                f"of {len(runs)} segments — a route has to be joined up")
        elif entry is not None and not any(
            (run.min_mm[0] - SERVICE_REACH_MM <= entry[0] <= run.max_mm[0] + SERVICE_REACH_MM)
            and (run.min_mm[1] - SERVICE_REACH_MM <= entry[1] <= run.max_mm[1] + SERVICE_REACH_MM)
            for run in runs
        ):
            disconnected[service] = (
                f"nothing within {SERVICE_REACH_MM:.0f}mm of the service entry at "
                f"({entry[0]:.0f}, {entry[1]:.0f}) — the network is not fed")

    unserved: dict[str, list[str]] = {}
    coverage: dict[str, dict[str, float]] = {}
    for row_id, racks in _rows(gt).items():
        lo, hi, across_lo, across_hi, axis = _row_box(racks)
        got = {
            service: _covered_fraction(agent.of_kind(service), lo, hi,
                                       across_lo, across_hi, axis)
            for service in REQUIRED_SERVICES
        }
        coverage[row_id] = {k: round(v, 3) for k, v in got.items()}
        missing = [s for s, frac in got.items() if frac < SERVICE_COVERAGE]
        if missing:
            unserved[row_id] = missing

    unreached = _unreached_consumers(gt, agent)
    return {"passed": not unserved and not disconnected and not unreached,
            "unserved_rows": unserved, "disconnected": disconnected,
            "unreached_consumers": unreached,
            "n_rows": len(_rows(gt)), "n_chw_consumers": len(_chw_consumers(gt)),
            "coverage": coverage,
            "reach_mm": SERVICE_REACH_MM, "min_coverage": SERVICE_COVERAGE,
            "joint_tolerance_mm": JOINT_TOLERANCE_MM}


def _chw_consumers(gt: Scene) -> list[Elem]:
    """Everything chilled water exists to reach: the air handlers.

    Rack rows do not consume chilled water, so gate 2 asks both halves: rack-row
    coverage and reach to every air handler. In-row units put a
    consumer inside every row, so "a header along the row" is what the row
    coverage leg is measuring; the perimeter gallery is reached by a spur.
    """
    return gt.of_kind("crah")


def _unreached_consumers(gt: Scene, agent: Scene) -> list[str]:
    """CHW consumers with no run of chilled water within reach, in plan.

    In plan, and at :data:`SERVICE_REACH_MM` — the same lenient reading the row
    coverage leg uses. A tighter number would be defensible engineering and is
    not defensible grading: the corpus has nothing on in-row pipe connection
    geometry, and inventing a constant to make a rule bite is how a benchmark
    ends up measuring obedience instead of reasoning.
    """
    runs = agent.of_kind("chw_pipe")
    return [unit.name or unit.eid for unit in _chw_consumers(gt)
            if not any(_gap_mm(run, unit, (0, 1)) <= SERVICE_REACH_MM
                       for run in runs)]


def routed_length_mm(scene: Scene) -> dict:
    """Total routed centreline length, by service.

    A straight run's centreline is its longest world dimension. Bends are
    modelled as separate segments in dcgen, so summing segments is the
    centreline length of the whole route.
    """
    by_service: dict[str, float] = {}
    for service in REQUIRED_SERVICES:
        total = 0.0
        for run in scene.of_kind(service):
            total += max(b - a for a, b in zip(run.min_mm, run.max_mm))
        by_service[service] = total
    return {"total_mm": sum(by_service.values()), "by_service": by_service}


# --------------------------------------------------------------------------
# task scores
# --------------------------------------------------------------------------



def _segment_count(scene: Scene, service: str) -> int:
    """Number of distinct centreline segments for one service."""
    return len(scene.of_kind(service))


def _component_count(scene: Scene, service: str) -> int:
    """Number of connected components under the 250 mm joint tolerance."""
    runs = scene.of_kind(service)
    if not runs:
        return 0
    return len(networks(runs))


def compute_secondary_metrics(
    agent_scene: Scene,
    connected: dict,
    routed: dict,
    l_opt_mm: float,
    clashes: list,
    compliance: dict,
) -> dict:
    """Diagnostics that do not alter the primary score.

    All values are derived from data the grader already materialises, so they
    add no new IFC traversal cost.  They surface length balance, coverage
    quality beyond the binary 80 % gate, network complexity, clash density and
    the dominant code-rule failure.
    """
    by_service = routed.get("by_service", {})
    total = float(routed.get("total_mm") or 0.0)
    l_opt = float(l_opt_mm or 0.0)

    # --- length detail -------------------------------------------------------
    # When L_opt is known we report each service as a fraction of the oracle
    # total; the absolute lengths are already in routed_length.by_service.
    service_share = {
        svc: (by_service.get(svc, 0.0) / l_opt) if l_opt > 0 else None
        for svc in REQUIRED_SERVICES
    }
    over_length_pct = (
        max(0.0, (total - l_opt) / l_opt * 100.0) if l_opt > 0 else None
    )

    # --- coverage quality (beyond the hard 80 % gate) ------------------------
    coverage = connected.get("coverage") or {}
    per_service_fracs: dict[str, list[float]] = {s: [] for s in REQUIRED_SERVICES}
    for row_cov in coverage.values():
        for svc, frac in row_cov.items():
            if svc in per_service_fracs:
                per_service_fracs[svc].append(float(frac))

    coverage_quality = {}
    rows_below_90 = 0
    n_rows = max(len(coverage), 1)
    for svc, fracs in per_service_fracs.items():
        if fracs:
            coverage_quality[svc] = {
                "min": round(min(fracs), 4),
                "mean": round(sum(fracs) / len(fracs), 4),
            }
        else:
            coverage_quality[svc] = {"min": None, "mean": None}
    # count rows where *any* service is below 0.90
    for row_cov in coverage.values():
        if any(float(row_cov.get(s, 0.0)) < 0.90 for s in REQUIRED_SERVICES):
            rows_below_90 += 1

    # --- network complexity --------------------------------------------------
    segments = {svc: _segment_count(agent_scene, svc) for svc in REQUIRED_SERVICES}
    components = {svc: _component_count(agent_scene, svc) for svc in REQUIRED_SERVICES}
    # components that exist only because the 250 mm joint tolerance glued them
    # (i.e. more than one component would have been reported without the tolerance)
    # is already reflected in the disconnected dict when the gate fails; here we
    # simply report the component counts under the same tolerance the gate uses.

    # --- clash density -------------------------------------------------------
    clash_count = len(clashes)
    clash_density_per_100m = (
        clash_count / (total / 100000.0) if total > 0 else None
    )  # total is mm; 100 m = 100000 mm

    # --- violation detail ----------------------------------------------------
    vbr = compliance.get("violations_by_rule") or {}
    rule_counts = {}
    for rule_name, payload in vbr.items():
        # payload is the RuleResult.to_dict(); count the violation list
        n = len(payload.get("violations") or [])
        if n:
            rule_counts[rule_name] = n
    worst_rule = None
    worst_count = 0
    if rule_counts:
        worst_rule = max(rule_counts, key=rule_counts.get)
        worst_count = rule_counts[worst_rule]
    distinct_violating_rules = len(rule_counts)

    return {
        "length_detail": {
            "service_share_of_l_opt": {k: (round(v, 4) if v is not None else None)
                                      for k, v in service_share.items()},
            "over_length_pct": round(over_length_pct, 3) if over_length_pct is not None else None,
            "total_mm": total,
            "l_opt_mm": l_opt,
        },
        "coverage_quality": {
            "per_service": coverage_quality,
            "rows_below_90pct": rows_below_90,
            "n_rows": len(coverage),
        },
        "network_complexity": {
            "segments_per_service": segments,
            "components_per_service": components,
            "total_segments": sum(segments.values()),
        },
        "clash_density": {
            "clash_count": clash_count,
            "per_100m_route": round(clash_density_per_100m, 4) if clash_density_per_100m is not None else None,
        },
        "violation_detail": {
            "distinct_violating_rules": distinct_violating_rules,
            "worst_rule": worst_rule,
            "worst_rule_count": worst_count,
            "rule_counts": rule_counts,
        },
    }


def _service_z_split(scene: Scene, service: str, floor_z: float) -> dict:
    """Split centreline length into under-floor vs overhead for one service."""
    under = 0.0
    over = 0.0
    for run in scene.of_kind(service):
        length = max(b - a for a, b in zip(run.min_mm, run.max_mm))
        # treat a segment as under-floor when its top is at or below finished floor
        if run.max_mm[2] <= floor_z + 1.0:          # 1 mm tolerance
            under += length
        else:
            over += length
    return {"under_floor_mm": round(under, 1), "overhead_mm": round(over, 1),
            "total_mm": round(under + over, 1)}


def _min_sprinkler_clearances(scene: Scene) -> dict:
    """Minimum vertical clearance for service runs directly below a head.

    A head elsewhere in plan is not above the run and therefore must not
    determine its vertical clearance diagnostic.
    """
    heads = scene.of_kind("sprinkler")
    if not heads:
        return {svc: None for svc in REQUIRED_SERVICES}
    out = {}
    for svc in REQUIRED_SERVICES:
        runs = scene.of_kind(svc)
        if not runs:
            out[svc] = None
            continue
        min_clear = None
        for run in runs:
            for head in heads:
                if _gap_mm(run, head, (0, 1)) > 0.0:
                    continue
                # vertical gap: head bottom - run top (positive when run is below head)
                gap = head.min_mm[2] - run.max_mm[2]
                if gap >= 0:                       # only count runs that sit below the head
                    if min_clear is None or gap < min_clear:
                        min_clear = gap
        out[svc] = round(min_clear, 1) if min_clear is not None else None
    return out


def _elevation_bands(scene: Scene, floor_z: float, clear_height: float = 2600.0) -> dict:
    """Fraction of each service length that sits in the primary overhead band
    (floor + clear_height) versus above it or below floor."""
    bands = {}
    for svc in REQUIRED_SERVICES:
        runs = scene.of_kind(svc)
        in_band = 0.0
        above = 0.0
        below = 0.0
        for run in runs:
            length = max(b - a for a, b in zip(run.min_mm, run.max_mm))
            mid_z = (run.min_mm[2] + run.max_mm[2]) / 2.0
            if mid_z < floor_z + 1.0:
                below += length
            elif mid_z <= floor_z + clear_height:
                in_band += length
            else:
                above += length
        total = in_band + above + below
        bands[svc] = {
            "in_clear_height_band_mm": round(in_band, 1),
            "above_band_mm": round(above, 1),
            "under_floor_mm": round(below, 1),
            "fraction_in_band": round(in_band / total, 4) if total > 0 else None,
        }
    return bands


def _optional_solver_extras(agent_scene: Scene, agent_model,
                            ifc_path: str | Path | None = None) -> dict:
    """Look for optional elements / files a submission may have emitted.
    Missing data is reported as null.
    """
    # Drops / terminations – look for a distinctive ObjectType or Pset
    drops = []
    for e in agent_scene.elements:
        ot = (e.object_type or "").lower()
        if "drop" in ot or "termination" in ot or "tap" in ot:
            drops.append({"eid": e.eid, "name": e.name, "kind": e.kind,
                          "xyz": list(e.centroid_mm)})
    # Supports / hangers
    supports = []
    for e in agent_scene.elements:
        ot = (e.object_type or "").lower()
        if "support" in ot or "hanger" in ot or "trapeze" in ot:
            supports.append({"eid": e.eid, "name": e.name, "kind": e.kind,
                             "xyz": list(e.centroid_mm)})
    # Reconciliation log – optional sidecar JSON next to the IFC or under output/
    recon = None
    if ifc_path is not None:
        p = Path(ifc_path)
        candidates = [
            p.with_name("reconciliation.json"),
            p.parent / "reconciliation.json",
            p.parent / "output" / "reconciliation.json",
            Path("output") / "reconciliation.json",
            Path("reconciliation.json"),
        ]
        for cand in candidates:
            if cand.is_file():
                try:
                    recon = json.loads(cand.read_text(encoding="utf-8"))
                except Exception:
                    recon = {"_error": f"could not parse {cand}"}
                break
    return {
        "drop_count": len(drops),
        "drops": drops[:50],
        "support_count": len(supports),
        "supports": supports[:50],
        "reconciliation_log_present": recon is not None,
        "reconciliation_log": recon,
    }


def compute_extended_metrics(
    agent_scene: Scene,
    agent_model,
    connected: dict,
    routed: dict,
    l_opt_mm: float,
    clashes: list,
    compliance: dict,
    ifc_path: str | Path | None = None,
) -> dict:
    """Geometry-derived values are always filled; optional
    solver-provided drops / supports / log are reported when present.
    """
    floor_z = agent_scene.floor_z_mm if agent_scene.hall else 0.0

    z_split = {svc: _service_z_split(agent_scene, svc, floor_z)
               for svc in REQUIRED_SERVICES}
    sprinkler_clear = _min_sprinkler_clearances(agent_scene)
    bands = _elevation_bands(agent_scene, floor_z)
    extras = _optional_solver_extras(agent_scene, agent_model, ifc_path)

    # PDU dedicated-space clearance above the PDU footprint. A pipe elsewhere
    # in plan does not occupy the NEC dedicated space.
    pdus = agent_scene.of_kind("pdu")
    chw = agent_scene.of_kind("chw_pipe")
    pdu_min_clear = None
    if pdus and chw:
        for pdu in pdus:
            for pipe in chw:
                if _gap_mm(pipe, pdu, (0, 1)) > 0.0:
                    continue
                gap = pipe.min_mm[2] - pdu.max_mm[2]
                if gap >= 0 and (pdu_min_clear is None or gap < pdu_min_clear):
                    pdu_min_clear = gap
    pdu_min_clear = round(pdu_min_clear, 1) if pdu_min_clear is not None else None

    # Tray-busway separation: each pair complies when at least one axis has
    # enough clear distance. Report the weakest pair's best separating axis,
    # matching the primary rule's "at least one axis" semantics.
    trays = agent_scene.of_kind("cable_tray")
    busways = agent_scene.of_kind("busway")
    tb_min_sep = None
    for t in trays:
        for b in busways:
            # gap on each axis; positive gap means separation
            gaps = [max(t.min_mm[i], b.min_mm[i]) - min(t.max_mm[i], b.max_mm[i])
                    for i in range(3)]
            sep = max(0.0, max(gaps))
            if tb_min_sep is None or sep < tb_min_sep:
                tb_min_sep = sep
    tb_min_sep = round(tb_min_sep, 1) if tb_min_sep is not None else None

    return {
        "achieved_clearances": {
            "min_sprinkler_clearance_mm": sprinkler_clear,
            "min_pdu_chw_clearance_mm": pdu_min_clear,
            "min_tray_busway_separation_mm": tb_min_sep,
        },
        "underfloor_overhead_split": z_split,
        "elevation_bands": bands,
        "support_and_drops": {
            "drop_count": extras["drop_count"],
            "support_count": extras["support_count"],
            "drops": extras["drops"],
            "supports": extras["supports"],
        },
        "reconciliation": {
            "log_present": extras["reconciliation_log_present"],
            "log": extras["reconciliation_log"],
        },
    }





@dataclass
class Report:
    """One graded submission. Components are reported separately, always."""

    task: str
    score: float | None
    exec_status: str
    gate_0: dict
    gates: dict = field(default_factory=dict)
    recovery_confusion: dict = field(default_factory=dict)
    semantics: dict = field(default_factory=dict)
    compliance: dict = field(default_factory=dict)
    clash_count: int | None = None
    clashes: list = field(default_factory=list)
    routed_length: dict = field(default_factory=dict)
    objective: float | None = None
    secondary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "task": self.task, "score": self.score,
            "exec_status": self.exec_status,
            "gate_0": self.gate_0, "gates": self.gates,
            "recovery_confusion": self.recovery_confusion,
            "semantics": self.semantics,
            "violations_by_rule": self.compliance.get("violations_by_rule", {}),
            "advisory": self.compliance.get("advisory", {}),
            "violation_rate": self.compliance.get("violation_rate"),
            "checkable_pairs": self.compliance.get("checkable_pairs"),
            "clash_count": self.clash_count, "clashes": self.clashes[:50],
            "routed_length": self.routed_length, "objective": self.objective,
            "secondary": self.secondary,
        }


#: The element kinds Task A asks for, and therefore the only ones its recovery
#: term may look at.
#:
#: Scoping is not a convenience. One ``ground_truth.ifc`` serves both tasks, so
#: it also carries the sprinkler grid and the oracle route — 63 heads and 12
#: runs on a mid-size hall. Matching those against a Task A submission would count
#: every one as *missing*, and cap a perfectly obedient agent that built exactly
#: what the sheets show below full recovery. The scope is reported in
#: ``recovery_confusion.scope`` so it cannot be a silent filter.
TASK_A_KINDS: tuple[str, ...] = (
    "rack", "crah", "pdu", "raised_floor", "containment", "column")


def restrict_to_kinds(scene: Scene, kinds: Sequence[str]) -> Scene:
    """The same scene with only the element kinds in ``kinds``."""
    wanted = set(kinds)
    return replace(scene, elements=tuple(
        e for e in scene.elements if e.kind in wanted))


def score_task_a(agent_ifc: str | Path, gt_ifc: str | Path,
                 kinds: Sequence[str] = TASK_A_KINDS) -> Report:
    """``recovery x semantics x (1 - violation_rate)``, behind gate 0."""
    agent_model, gate = gate_0(agent_ifc)
    if agent_model is None:
        return Report(task="A", score=0.0, exec_status=gate.status,
                      gate_0=gate.to_dict())

    gt_model = ifcopenshell.open(str(gt_ifc))
    gt_scene, agent_scene = build_scene(gt_model), build_scene(agent_model)

    recovery = match_elements(restrict_to_kinds(gt_scene, kinds),
                              restrict_to_kinds(agent_scene, kinds))
    semantic = semantics(agent_model, gt_model)
    # The rule battery is scoped to the same kinds recovery is.
    # ``TASK_A_RULES`` still holds four sprinkler rules — clearance, protection
    # area, max spacing, deflector position — and if they ran against
    # the UNRESTRICTED scene, a Task A submission that modelled the heads it
    # can see on A-301 would be graded on four REQUIREMENT thresholds the task
    # statement does not quote, on a grid the brief tells it not to build.
    # Sprinkler pairs are 45 % of the checkable set once heads are present.
    # Recovery and compliance have to agree about what the task is, and Task A
    # is the fit-out.
    
    scoped = restrict_to_kinds(agent_scene, kinds)
    rule_results = rules.run_rules(scoped, task="A")
    compliance = rules.compliance(rule_results)

    score = (recovery.score * semantic["semantics"]
             * (1.0 - compliance["violation_rate"]))
    clashes = hard_clashes(agent_model, ifc_path=agent_ifc)
    return Report(
        task="A", score=score, exec_status="OK", gate_0=gate.to_dict(),
        recovery_confusion={**recovery.to_dict(), "scope": list(kinds)},
        semantics=semantic,
        compliance=compliance,
        # neither is a Task A gate, but a fit-out that clashes or that invents
        # overhead routing is worth seeing in the report
        clash_count=len(clashes),
        clashes=[{"a": a, "b": b, "detail": d} for a, b, d in clashes],
        routed_length=routed_length_mm(agent_scene),
    )


def score_task_b(agent_ifc: str | Path, gt_ifc: str | Path,
                 l_opt_mm: float | None = None) -> Report:
    """``compliance x min(1, L_opt / L_agent)``, behind gates 0, 1 and 2."""
    agent_model, gate = gate_0(agent_ifc)
    if agent_model is None:
        return Report(task="B", score=0.0, exec_status=gate.status,
                      gate_0=gate.to_dict())

    gt_model = ifcopenshell.open(str(gt_ifc))
    gt_scene, agent_scene = build_scene(gt_model), build_scene(agent_model)

    if l_opt_mm is None:
        l_opt_mm = routed_length_mm(gt_scene)["total_mm"] or None
    if l_opt_mm is None:
        raise ValueError(
            "no oracle route length: the ground truth carries no tray/busway/"
            "CHW geometry and l_opt_mm was not supplied. An "
            "instance with no feasible oracle is rejected at build time, not "
            "shipped ungradeable."
        )

    clashes = hard_clashes(agent_model, ifc_path=agent_ifc)
    connected = connectivity(gt_scene, agent_scene)
    routed = routed_length_mm(agent_scene)
    rule_results = rules.run_rules(agent_scene, task="B")
    compliance = rules.compliance(rule_results)

    gates = {"gate_1_no_hard_clash": not clashes, "gate_2_connectivity": connected}
    secondary = compute_secondary_metrics(
        agent_scene, connected, routed, l_opt_mm, clashes, compliance)
    extended = compute_extended_metrics(
        agent_scene, agent_model, connected, routed, l_opt_mm, clashes, compliance,
        ifc_path=agent_ifc)
    
    secondary = {**secondary, "extended": extended}
    report = Report(
        task="B", score=None, exec_status="OK", gate_0=gate.to_dict(),
        gates=gates, compliance=compliance, clash_count=len(clashes),
        clashes=[{"a": a, "b": b, "detail": d} for a, b, d in clashes],
        routed_length={**routed, "l_opt_mm": l_opt_mm},
        secondary=secondary,
    )
    if clashes:
        report.score, report.exec_status = 0.0, "ERR_CLASH"
        return report
    if not connected["passed"]:
        report.score, report.exec_status = 0.0, "ERR_CONNECTIVITY"
        return report

    l_agent = routed["total_mm"]
    # gate 2 already guarantees every row is served, so l_agent cannot be zero
    # here; guard anyway rather than divide by it
    if l_agent <= 0:
        report.objective = 0.0
    elif math.isclose(l_agent, l_opt_mm, rel_tol=1.0e-12, abs_tol=1.0e-6):
        # The packaged reference is the exact-1.0 sanity anchor.
        report.objective = 1.0
    else:
        report.objective = min(1.0, l_opt_mm / l_agent)
    report.score = (1.0 - compliance["violation_rate"]) * report.objective
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--submission", required=True, type=Path)
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--task", choices=["A", "B"], default="A")
    parser.add_argument("--l-opt-mm", type=float, default=None,
                        help="oracle route length; defaults to the ground truth's")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.task == "A":
        report = score_task_a(args.submission, args.ground_truth)
    else:
        report = score_task_b(args.submission, args.ground_truth, args.l_opt_mm)

    payload = json.dumps(report.to_dict(), indent=2)
    if args.out:
        args.out.write_text(payload)
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
