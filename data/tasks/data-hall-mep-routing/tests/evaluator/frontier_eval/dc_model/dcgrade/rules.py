"""The graded rule battery.

**Provenance is the whole design.** A rule here may fail an agent only on a
constant whose :attr:`specs.Sourced.gradeable` is true — i.e. a published
standard says *shall*. Everything else is advisory: reported so a human can see
it, never folded into the score. :func:`compliance` enforces the split.

Two consequences that look like omissions but are not:

* **Tile alignment is not graded.** TIA-942-B-2017 6.14.3 says cabinets *should*
  align with a tile edge, so :data:`specs.TILE_ALIGNMENT` is a RECOMMENDATION.
  A rack one tile off is still caught — by ``recovery``, which is where a
  determinate task belongs.
* **Hot/cold aisle alternation is not a rule here.** It is a pattern, not a
  number, and no ``Sourced`` constant backs it. It is graded through recovery
  instead: a rack facing the wrong way fails the facing test and counts as
  mistyped.

Rules operate on :class:`Scene` — plain geometry, no ifcopenshell — so they can
be unit-tested against hand-built element lists and so the IFC reading lives in
exactly one place (:mod:`.score`).

Every rule reports the *specific violating element ids*, never just a count:
diagnostics are never fused into a single number, and a bare
violation count cannot be acted on by an execute-and-fix loop.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..dcgen import specs
from ..dcgen.elements import IFC_TYPES
from ..dcgen.units import box_in_polygon

#: Geometric slop. Faces that merely touch (a rack against its neighbour, a
#: cabinet on the floor) must not read as overlap, and mesh coordinates arrive
#: as floats via ``ifcopenshell.geom``. 1 mm is far below any graded threshold
#: and far above float noise at hall scale.
EPS_MM = 1.0

#: Floor on the lateral offset within which two sprinkler heads are treated as
#: being on the same branch line. A grouping heuristic for reading a layout, NOT
#: a graded threshold — the graded number is
#: :data:`specs.SPRINKLER_MAX_PROTECTION_AREA`.
#:
#: The band actually used is :func:`branch_line_band`, which scales with the
#: layout's own spacing. A fixed 150 mm is wrong in a way that produces
#: physically impossible readings: real design nudges an obstructed head a whole
#: tile off its grid column, and at 150 mm every peer on the perpendicular axis
#: then drops out, leaving the wall fallback to report ``L = 33000 mm`` in a
#: hall only 21000 mm deep.
#: That is stricter than the standard it cites, which the rule battery
#: forbids.
BRANCH_LINE_TOL_MM = 150.0

#: Kinds that have a defined front, so "facing" is meaningful. A sprinkler or a
#: square column has no front; demanding a facing match on those would fail an
#: agent for a rotation nobody can observe.
FRONTED_KINDS = frozenset({"rack", "crah", "pdu"})

#: Kinds that hang in the plenum. TIA-942's 2.6 m is floor-to-lowest-overhead-
#: obstruction, so floor-standing equipment (racks, CRAHs, columns) and the
#: access floor are not obstructions to room height and are not checked.
#: Aisle containment is excluded deliberately: a containment roof sits at
#: cabinet height by definition, and reading the room-height clause as banning
#: it would fail every legal contained-aisle layout.
OVERHEAD_KINDS = frozenset({"sprinkler", "cable_tray", "busway", "chw_pipe"})

#: What a service run may terminate on top of without becoming an obstruction
#: to room height. CRAH only, and that narrowness is the whole point.
#:
#: The exemption exists for one real case: a chilled-water connection landing
#: on top of a 2000 mm in-row unit, which takes no headroom the unit had not
#: already taken. Written as "any floor-standing element" it would be an exploit —
#: cabinets in a row are contiguous at the 600 mm pitch, so an agent could chop
#: a 14 m horizontal main into 24 per-bay segments, each one wholly inside the
#: cabinet beneath it, and carry it across the hall at 2100 mm with zero
#: violations.
#: In-row units are three or four bays apart, so the same
#: trick over CRAHs leaves every intervening segment exposed.
CONNECTABLE_KINDS = frozenset({"crah"})

#: kind -> (IfcClass, PredefinedType, ObjectType), upper-cased for lookup.
_SIGNATURE_TO_KIND: dict[tuple[str, str | None, str | None], str] = {
    (cls.upper(), pt.upper() if pt else None, ot.upper() if ot else None): kind
    for kind, (cls, pt, ot) in IFC_TYPES.items()
}


def classify(ifc_class: str, predefined_type: str | None,
             object_type: str | None) -> str:
    """Map an IFC signature onto a dcgen element kind, or ``"unknown"``.

    Falls back from the full triple to (class, predefined type) to class alone,
    so an agent that omits ``ObjectType`` on a CRAH still classifies, while a
    rack — whose whole identity is the ``ObjectType`` discriminator, IFC4 having
    no ``IfcRack`` — only classifies when it carries one.
    """
    cls = ifc_class.upper()
    pt = predefined_type.upper() if predefined_type else None
    ot = object_type.upper() if object_type else None
    for key in ((cls, pt, ot), (cls, pt, None), (cls, None, None)):
        if key in _SIGNATURE_TO_KIND:
            return _SIGNATURE_TO_KIND[key]
    return "unknown"


#: The task statement pins only the rack signature, so an element
#: :func:`classify` leaves unknown gets one more, deliberately narrow reading
#: (:func:`classify_fallback`); an element it recognises keeps its kind. These
#: are the drawing set's service and unit names, looked up case-insensitively in
#: the element's Name, ObjectType, Description and group names, whole words
#: only (:func:`_spells`) -- these tokens only, nothing fuzzier.
_FALLBACK_TOKENS: dict[str, tuple[str, ...]] = {
    "cable_tray": ("CABLETRAY", "TRAY", "LADDER", "TELECOM", "CT1"),
    "busway": ("BUSWAY", "BUSBAR", "BUSDUCT", "BUS", "BW1"),
    "chw_pipe": ("CHILLEDWATER", "CHW", "DN100", "PIPE"),
    "crah": ("CRAH",),
    "pdu": ("PDU",),
    "sprinkler": ("SPRINKLER",),
}
#: The task's words for its optional supports and hangers: never a service or
#: a unit.
_FALLBACK_VETO = ("SUPPORT", "HANGER")
#: Fire-protection pipework is not the chilled-water service.
_FALLBACK_NOT_CHW = ("SPRINKLER", "FIRE")
#: Cabinets are not units.
_FALLBACK_NOT_UNIT = ("RACK", "CABINET")
_ROUTE_KINDS = frozenset({"cable_tray", "busway", "chw_pipe"})
_UNIT_KINDS = frozenset({"crah", "pdu", "sprinkler"})
#: IFC class -> its family's kind, whatever its PredefinedType: the family
#: says whether the element is a run or a unit, and this is the kind it is when
#: its names give no other of the same category.
_FALLBACK_FAMILY: dict[str, str] = {
    "IFCCABLECARRIERSEGMENT": "cable_tray",
    "IFCPIPESEGMENT": "chw_pipe",
    "IFCUNITARYEQUIPMENT": "crah",
    "IFCELECTRICDISTRIBUTIONBOARD": "pdu",
    "IFCFIRESUPPRESSIONTERMINAL": "sprinkler",
}
#: Classes with no service meaning of their own -> the kinds their tokens may
#: give them.
_FALLBACK_GENERIC: dict[str, frozenset[str]] = {
    "IFCBUILDINGELEMENTPROXY": _ROUTE_KINDS | _UNIT_KINDS,
    "IFCDISTRIBUTIONELEMENT": _ROUTE_KINDS | _UNIT_KINDS,
    "IFCFLOWSEGMENT": _ROUTE_KINDS,
    "IFCDISTRIBUTIONFLOWELEMENT": _ROUTE_KINDS,
    "IFCFLOWTERMINAL": _UNIT_KINDS,
    "IFCFLOWCONTROLLER": _UNIT_KINDS,
    "IFCENERGYCONVERSIONDEVICE": _UNIT_KINDS,
    "IFCFURNITURE": frozenset({"crah", "pdu"}),
}


def _words(text: str) -> list[str]:
    """``text``'s words, upper-cased: split at every character that is not an
    ASCII letter or digit, where a lower-case letter meets a capital, before
    the capital that starts a word after a run of capitals ("CHWPipe") and
    between letters and digits."""
    return [w.upper() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+",
                                          text)]


def _spells(words: Sequence[str], token: str) -> bool:
    """Whether consecutive whole words spell ``token``: "TRAY" is in "Tray-01"
    and not in "Ashtray"; "CABLETRAY" is in "Cable Tray", "CableTray" and
    "CABLETRAY"; "BW1" is in "BW-1"."""
    for i in range(len(words)):
        spelt = ""
        for word in words[i:]:
            spelt += word
            if not token.startswith(spelt):
                break
            if spelt == token:
                return True
    return False


def classify_fallback(ifc_class: str, texts: Sequence[str | None]) -> str:
    """The kind of an element :func:`classify` leaves unknown, or "unknown".

    A class family decides whether the element is a run (cable carrier,
    pipe) or a unit (unitary equipment, distribution board, fire-suppression
    terminal); its names decide which: the one kind of that category they name,
    else the family's own kind (named none of that category, or the family's
    kind among others), else -- two other kinds named -- nothing. A pipe named
    as fire-protection pipework is nothing. A cable segment is the busway when
    named so. A generic class takes the kind its names give, when they name
    exactly one kind it may be. Supports, hangers and any other class stay
    unknown.
    """
    cls = ifc_class.upper()
    words = [_words(str(text)) for text in texts if text]

    def named(tokens: Sequence[str]) -> bool:
        return any(_spells(split, token) for split in words for token in tokens)

    if named(_FALLBACK_VETO):
        return "unknown"
    kinds = {kind for kind, tokens in _FALLBACK_TOKENS.items()
             if named(tokens)}
    if named(_FALLBACK_NOT_CHW):
        kinds.discard("chw_pipe")
        if cls == "IFCPIPESEGMENT":
            return "unknown"
    if cls in _FALLBACK_FAMILY:
        own = _FALLBACK_FAMILY[cls]
        given = kinds & (_ROUTE_KINDS if own in _ROUTE_KINDS else _UNIT_KINDS)
        if not given or own in given:
            return own
        return next(iter(given)) if len(given) == 1 else "unknown"
    if cls == "IFCCABLESEGMENT":
        return "busway" if "busway" in kinds else "unknown"
    allowed = _FALLBACK_GENERIC.get(cls, frozenset())
    if named(_FALLBACK_NOT_UNIT):
        allowed = allowed - _UNIT_KINDS
    if len(kinds) == 1 and kinds <= allowed:
        return next(iter(kinds))
    return "unknown"


def classify_product(product) -> str:
    """:func:`classify` of an IFC product, else :func:`classify_fallback` of
    its Name, ObjectType, Description and the names of its groups."""
    object_type = getattr(product, "ObjectType", None)
    kind = classify(product.is_a(), getattr(product, "PredefinedType", None),
                    object_type)
    if kind != "unknown":
        return kind
    texts = [product.Name, object_type, getattr(product, "Description", None)]
    for rel in getattr(product, "HasAssignments", None) or ():
        if rel.is_a("IfcRelAssignsToGroup") and rel.RelatingGroup is not None:
            group = rel.RelatingGroup
            texts += [group.Name, getattr(group, "ObjectType", None)]
    return classify_fallback(product.is_a(), texts)


@dataclass(frozen=True)
class Elem:
    """One physical element, reduced to what the rules need.

    ``min_mm`` / ``max_mm`` are the **realised** world-space axis-aligned bounds
    in millimetres, read back from the model's geometry. They are not the
    element's nominal size and placement.
    """

    eid: str
    name: str
    kind: str
    ifc_class: str
    predefined_type: str | None
    object_type: str | None
    min_mm: tuple[float, float, float]
    max_mm: tuple[float, float, float]
    #: cardinal direction the front points, derived from the placement rotation;
    #: ``None`` for kinds without a front
    facing: str | None = None
    psets: dict[str, dict[str, object]] = field(default_factory=dict)

    @property
    def centroid_mm(self) -> tuple[float, float, float]:
        return tuple((a + b) / 2.0 for a, b in zip(self.min_mm, self.max_mm))  # type: ignore[return-value]

    @property
    def plan_box_mm(self) -> tuple[float, float, float, float]:
        return (self.min_mm[0], self.min_mm[1], self.max_mm[0], self.max_mm[1])

    @property
    def top_mm(self) -> float:
        return self.max_mm[2]

    @property
    def bottom_mm(self) -> float:
        return self.min_mm[2]

    def label(self) -> str:
        return f"{self.name or self.ifc_class} ({self.eid})"


@dataclass(frozen=True)
class Scene:
    """Everything a rule may look at, extracted from one IFC model.

    The hall is held apart from ``elements`` because it is an ``IfcSpace`` — a
    spatial structure element, not an ``IfcElement``. It is a checkable subject
    for the room-height rule but never a recovery candidate or a clash partner.
    """

    elements: tuple[Elem, ...]
    hall: Elem | None = None
    hall_polygon_mm: tuple[tuple[float, float], ...] | None = None

    def of_kind(self, *kinds: str) -> list[Elem]:
        want = set(kinds)
        return [e for e in self.elements if e.kind in want]

    @property
    def floor_z_mm(self) -> float:
        """Finished floor level. TIA-942 measures room height from here."""
        return self.hall.min_mm[2] if self.hall else 0.0

    @property
    def ceiling_z_mm(self) -> float | None:
        return self.hall.max_mm[2] if self.hall else None


@dataclass(frozen=True)
class Violation:
    eid: str
    name: str
    detail: str


@dataclass(frozen=True)
class RuleResult:
    """Outcome of one rule over one scene.

    ``checkable`` is the denominator contribution and ``violations`` the
    numerator, because compliance is counted per **element-rule
    pair** — a hall with 80 racks and one bad one must not score the same as a
    hall with 2 racks and one bad one.
    """

    rule: str
    source: str
    threshold: str
    gradeable: bool
    checkable: tuple[str, ...]
    violations: tuple[Violation, ...]

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "source": self.source,
            "threshold": self.threshold,
            "gradeable": self.gradeable,
            "n_checkable": len(self.checkable),
            "n_violations": len(self.violations),
            "violations": [
                {"id": v.eid, "name": v.name, "detail": v.detail}
                for v in self.violations
            ],
        }


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------


def _overlaps(a: Elem, box_min: Sequence[float], box_max: Sequence[float]) -> bool:
    """Does element ``a`` intersect the given box by more than :data:`EPS_MM`?"""
    return all(
        min(a.max_mm[i], box_max[i]) - max(a.min_mm[i], box_min[i]) > EPS_MM
        for i in range(3)
    )


def _plan_overlaps(a: Elem, b: Elem) -> bool:
    return all(
        min(a.max_mm[i], b.max_mm[i]) - max(a.min_mm[i], b.min_mm[i]) > EPS_MM
        for i in range(2)
    )


def _within_plan(inner: Elem, outer: Elem) -> bool:
    """Is ``inner``'s plan footprint wholly inside ``outer``'s?

    Wholly, not merely overlapping: a run that clips the corner of a cabinet on
    its way across the room is still crossing the room.
    """
    return (inner.min_mm[0] >= outer.min_mm[0] - EPS_MM
            and inner.min_mm[1] >= outer.min_mm[1] - EPS_MM
            and inner.max_mm[0] <= outer.max_mm[0] + EPS_MM
            and inner.max_mm[1] <= outer.max_mm[1] + EPS_MM)


#: facing -> (axis index, sign) the FRONT points along
_FRONT_AXIS = {"NORTH": (1, 1.0), "SOUTH": (1, -1.0), "EAST": (0, 1.0), "WEST": (0, -1.0)}


def _clearance_band(elem: Elem, distance_mm: float, front: bool
                    ) -> tuple[list[float], list[float]] | None:
    """The prism that must stay clear in front of (or behind) an element."""
    if elem.facing not in _FRONT_AXIS:
        return None
    axis, sign = _FRONT_AXIS[elem.facing]
    if not front:
        sign = -sign
    lo = list(elem.min_mm)
    hi = list(elem.max_mm)
    if sign > 0:
        lo[axis] = elem.max_mm[axis]
        hi[axis] = elem.max_mm[axis] + distance_mm
    else:
        hi[axis] = elem.min_mm[axis]
        lo[axis] = elem.min_mm[axis] - distance_mm
    return lo, hi


def _ray_to_boundary(polygon: Sequence[tuple[float, float]],
                     x: float, y: float, axis: int, sign: float) -> float | None:
    """Distance from ``(x, y)`` to the polygon boundary along +/- one axis.

    Needed for the NFPA protection-area calculation, where an end sprinkler's
    span is twice its distance to the wall. The hall is a polygon, not its
    bounding box — the ``l_shaped`` knob notches a corner — so this walks edges
    rather than differencing against ``bounds``.
    """
    here = (x, y)
    other = 1 - axis
    best: float | None = None
    n = len(polygon)
    for i in range(n):
        p = polygon[i]
        q = polygon[(i + 1) % n]
        span_lo, span_hi = min(p[other], q[other]), max(p[other], q[other])
        if not (span_lo - EPS_MM <= here[other] <= span_hi + EPS_MM):
            continue
        if abs(q[other] - p[other]) < EPS_MM:
            continue  # edge parallel to the ray; it cannot bound it
        t = (here[other] - p[other]) / (q[other] - p[other])
        cross = p[axis] + t * (q[axis] - p[axis])
        travel = (cross - here[axis]) * sign
        if travel > EPS_MM and (best is None or travel < best):
            best = travel
    return best


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------


def _clearance_rule(scene: Scene, sourced: specs.Sourced, name: str,
                    front: bool) -> RuleResult:
    """TIA-942 5.11.7.1 working clearance in front of / behind every cabinet.

    A cabinet needs its stated clearance to get equipment in and out, so the
    check is obstruction of the prism swept off the cabinet face — by any other
    solid, and by the room boundary. Column-in-the-aisle is a real coordination
    failure and is reported as one.
    """
    distance = float(sourced)
    racks = scene.of_kind("rack")
    violations: list[Violation] = []
    for rack in racks:
        band = _clearance_band(rack, distance, front)
        if band is None:
            continue
        lo, hi = band
        axis, sign = _FRONT_AXIS[rack.facing]  # type: ignore[index]
        if not front:
            sign = -sign
        blockers: list[tuple[float, str]] = []
        for other in scene.elements:
            if other is rack or not _overlaps(other, lo, hi):
                continue
            reach = (other.min_mm[axis] - rack.max_mm[axis] if sign > 0
                     else rack.min_mm[axis] - other.max_mm[axis])
            blockers.append((max(0.0, reach), other.label()))
        if scene.hall_polygon_mm is not None and not box_in_polygon(
            list(scene.hall_polygon_mm), lo[0], lo[1], hi[0], hi[1]
        ):
            # no usable clearance at all: the band leaves the room, so sort it
            # below any partial obstruction
            blockers.append((-1.0, "hall boundary"))
        if blockers:
            reach, culprit = min(blockers)
            got = "no" if reach < 0 else f"{reach:.0f}mm"
            violations.append(Violation(
                rack.eid, rack.name,
                f"{got} clearance {'in front of' if front else 'behind'} a "
                f"{rack.facing}-facing cabinet, needs {distance:.0f}mm "
                f"({sourced.ref}); blocked by {culprit}",
            ))
    return RuleResult(
        rule=name, source=sourced.ref, gradeable=sourced.gradeable,
        threshold=f">= {distance:.0f} {sourced.unit}",
        checkable=tuple(r.eid for r in racks), violations=tuple(violations),
    )


def front_clearance(scene: Scene) -> RuleResult:
    return _clearance_rule(scene, specs.FRONT_CLEARANCE, "front_clearance", front=True)


def rear_clearance(scene: Scene) -> RuleResult:
    return _clearance_rule(scene, specs.REAR_CLEARANCE, "rear_clearance", front=False)


def clear_height(scene: Scene) -> RuleResult:
    """TIA-942 5.3.4.3 — 2.6 m from finished floor to the lowest OVERHEAD
    obstruction.

    Two things are therefore not obstructions and both carve-outs are below:
    anything at or under floor level, which is where TIA-942-B 8.6.2 expressly
    permits mains to run; and a connection landing on top of a CRAH, which
    takes no headroom the unit had not already taken.
    """
    sourced = specs.CLEAR_HEIGHT
    minimum = float(sourced)
    floor = scene.floor_z_mm
    checkable: list[str] = []
    violations: list[Violation] = []

    if scene.hall is not None:
        checkable.append(scene.hall.eid)
        height = scene.hall.max_mm[2] - scene.hall.min_mm[2]
        if height < minimum - EPS_MM:
            violations.append(Violation(
                scene.hall.eid, scene.hall.name,
                f"room height {height:.0f}mm < {minimum:.0f}mm ({sourced.ref})",
            ))

    standing = [e for e in scene.elements if e.kind in CONNECTABLE_KINDS]
    for elem in scene.elements:
        if elem.kind not in OVERHEAD_KINDS:
            continue
        if elem.bottom_mm <= floor + EPS_MM:
            # Not overhead. TIA-942's 2.6 m is floor-to-lowest-OVERHEAD-
            # obstruction, which is why racks, CRAHs and columns are already
            # excluded, and the same reasoning covers anything else that stands
            # on or passes through the floor: an under-floor tray (the standard
            # devotes clause 8.6.2 to permitting them) and the vertical riser
            # that joins one to an overhead branch. A tray *hung* at 1000 mm
            # still has its bottom above the floor and is still caught.
            continue
        if elem.kind == "sprinkler" and protecting_panel(scene, elem) is not None:
            # Nor is a sprinkler hanging under a containment roof. NFPA 13
            # 10.2.7.4.2 requires one there, and the lid above it already caps
            # that column of air at its own underside — exactly the reasoning
            # that exempts a connection landing on top of a CRAH below. Grading
            # it against the room's 2600 mm would make the code-required head
            # the violation.
            continue
        if any(_within_plan(elem, unit) and elem.bottom_mm >= unit.top_mm - EPS_MM
               for unit in standing):
            # Nor is a connection landing on top of a CRAH. The unit already
            # occupies that column of air to 2000 mm, so a drop onto it takes
            # no headroom from the room that the unit had not already taken.
            # Without this the only legal way to pipe a 2000 mm in-row unit is
            # from the plenum, which makes the feed direction a trap rather
            # than the agent's decision — and top-connected in-row units are
            # ordinary. ON TOP OF, not merely above: a run threaded through the
            # column of air *beside* a unit at 1000 mm is an obstruction, and
            # so is one that clips a corner on its way across the room, which
            # is why :func:`_within_plan` tests containment and not overlap.
            continue
        checkable.append(elem.eid)
        headroom = elem.bottom_mm - floor
        if headroom < minimum - EPS_MM:
            violations.append(Violation(
                elem.eid, elem.name,
                f"underside at {headroom:.0f}mm above finished floor, "
                f"needs {minimum:.0f}mm ({sourced.ref})",
            ))

    return RuleResult(
        rule="clear_height", source=sourced.ref, gradeable=sourced.gradeable,
        threshold=f">= {minimum:.0f} {sourced.unit}",
        checkable=tuple(checkable), violations=tuple(violations),
    )


def sprinkler_clearance(scene: Scene) -> RuleResult:
    """TIA-942 5.3.4.3 — 460 mm clearance maintained below every head.

    Scoped to what sits *directly beneath* a head, which is the least strict
    reading of the clause and therefore the only safe one: NFPA 13's horizontal
    obstruction geometry (the "three times" rule) is not graded because no
    sourced constant in :mod:`specs` backs it.
    """
    sourced = specs.SPRINKLER_CLEARANCE
    standoff = float(sourced)
    heads = scene.of_kind("sprinkler")
    violations: list[Violation] = []
    for head in heads:
        deflector = head.bottom_mm
        worst: tuple[float, str] | None = None
        for other in scene.elements:
            if other is head or other.kind == "sprinkler":
                continue
            if other.top_mm > deflector + EPS_MM or not _plan_overlaps(head, other):
                continue
            gap = deflector - other.top_mm
            if gap < standoff - EPS_MM and (worst is None or gap < worst[0]):
                worst = (gap, other.label())
        if worst is not None:
            violations.append(Violation(
                head.eid, head.name,
                f"only {worst[0]:.0f}mm below the deflector before {worst[1]}, "
                f"needs {standoff:.0f}mm ({sourced.ref})",
            ))
    return RuleResult(
        rule="sprinkler_clearance", source=sourced.ref, gradeable=sourced.gradeable,
        threshold=f">= {standoff:.0f} {sourced.unit}",
        checkable=tuple(h.eid for h in heads), violations=tuple(violations),
    )


def branch_line_band(centres: Sequence[tuple[float, float, float]]) -> float:
    """Lateral offset within which two heads count as sharing a branch line.

    Half the layout's own median nearest-neighbour distance, floored at
    :data:`BRANCH_LINE_TOL_MM`. There is exactly one length scale in a sprinkler
    layout — its spacing — so deriving the band from it is the only choice that
    is not arbitrary, and it is the choice that keeps the rule from being
    stricter than NFPA:

    * a head nudged off an obstruction by one 600 mm tile stays on its line,
      because 600 < pitch / 2;
    * the *next* branch line never joins, because it sits a full pitch away.

    The median (not the mean) so a couple of heads stacked on top of each other
    cannot collapse the band for the whole layout.
    """
    if len(centres) < 2:
        return BRANCH_LINE_TOL_MM
    nearest: list[float] = []
    for i, a in enumerate(centres):
        best = min(
            (math.hypot(b[0] - a[0], b[1] - a[1])
             for j, b in enumerate(centres) if j != i),
            default=0.0,
        )
        if best > EPS_MM:
            nearest.append(best)
    if not nearest:
        return BRANCH_LINE_TOL_MM
    nearest.sort()
    median = nearest[len(nearest) // 2]
    return max(BRANCH_LINE_TOL_MM, median / 2.0)


def sprinkler_protection_area(scene: Scene) -> RuleResult:
    """NFPA 13 (2022) 10.2 — protection area per head.

    ``As = S x L``, where each span is the greater of the distance to the
    adjacent head on that line and twice the distance to the wall, per NFPA's
    own method. Note the threshold is 20.9032 m2 (225 ft2 exactly) and not the
    rounded 20 m2 in the design table: rounding down would be 4.3 % stricter
    than the code and would fail a legal 4572 mm square layout.

    Two departures from a naive transcription of the method, both in the
    lenient direction because a graded threshold must never be stricter than
    the standard it cites:

    * peers are grouped onto a branch line with :func:`branch_line_band` rather
      than a fixed tolerance, so a head relocated off an obstruction keeps its
      neighbours instead of falling through to the wall fallback;
    * each span is capped at the room's own extent through the head. NFPA's
      "twice the distance to the wall" is a design convenience for an end
      sprinkler; applied at both ends of a lone head it reports a span longer
      than the building, and a head cannot be responsible for floor area that
      does not exist.
    """
    sourced = specs.SPRINKLER_MAX_PROTECTION_AREA
    max_area = float(sourced)
    heads = scene.of_kind("sprinkler")
    violations: list[Violation] = []
    centres = [h.centroid_mm for h in heads]
    # Heads on different planes are not peers, and a head under a containment
    # roof protects the aisle rather than the room. Without the split, one
    # in-aisle line 2400 mm below the ceiling grid halves the layout's median
    # nearest-neighbour distance and drags every ceiling head's branch band
    # with it; and an in-aisle head with no peer across the aisle falls
    # through to the wall fallback and is charged for the whole hall.
    panels = [protecting_panel(scene, h) for h in heads]
    band_by_plane = {
        key: branch_line_band([c for c, p in zip(centres, panels)
                               if (p.eid if p is not None else None) == key])
        for key in {(p.eid if p is not None else None) for p in panels}
    }

    for i, head in enumerate(heads):
        panel = panels[i]
        plane = panel.eid if panel is not None else None
        band = band_by_plane[plane]
        spans: list[float] = []
        for axis in (0, 1):
            other = 1 - axis
            peers = [
                c[axis] for j, c in enumerate(centres)
                if j != i and abs(c[other] - centres[i][other]) <= band
                and (panels[j].eid if panels[j] is not None else None) == plane
            ]
            here = centres[i][axis]
            span = 0.0
            room = 0.0
            for sign in (1.0, -1.0):
                if panel is not None:
                    # the obstruction it hangs under IS its room
                    wall = (panel.max_mm[axis] - centres[i][axis] if sign > 0
                            else centres[i][axis] - panel.min_mm[axis])
                else:
                    wall = (
                        _ray_to_boundary(scene.hall_polygon_mm, centres[i][0],
                                         centres[i][1], axis, sign)
                        if scene.hall_polygon_mm is not None else None
                    )
                room += wall if wall is not None else 0.0
                gaps = [(p - here) * sign for p in peers if (p - here) * sign > EPS_MM]
                if gaps:
                    reach = min(gaps)
                elif wall is not None:
                    reach = 2.0 * wall
                else:
                    reach = 0.0
                span = max(span, reach)
            if room > EPS_MM:
                span = min(span, room)
            spans.append(span)
        area_m2 = (spans[0] * spans[1]) / 1.0e6
        if area_m2 > max_area + 1e-6:
            violations.append(Violation(
                head.eid, head.name,
                f"covers {area_m2:.2f} m2 (S={spans[0]:.0f}mm x L={spans[1]:.0f}mm), "
                f"max {max_area:.4f} m2 ({sourced.ref})",
            ))
    return RuleResult(
        rule="sprinkler_protection_area", source=sourced.ref,
        gradeable=sourced.gradeable,
        threshold=f"<= {max_area:.4f} {sourced.unit}",
        checkable=tuple(h.eid for h in heads), violations=tuple(violations),
    )


def sprinkler_deflector_position(scene: Scene) -> RuleResult:
    """NFPA 13 (2022) 10.2.6.1 — deflector 25.4-304.8 mm below the ceiling."""
    lo_src = specs.SPRINKLER_DEFLECTOR_MIN_BELOW_CEILING
    hi_src = specs.SPRINKLER_DEFLECTOR_MAX_BELOW_CEILING
    lo, hi = float(lo_src), float(hi_src)
    heads = scene.of_kind("sprinkler")
    ceiling = scene.ceiling_z_mm
    if ceiling is None:
        # without a modelled room there is no ceiling datum, so nothing is
        # checkable; the missing space is already caught by the semantics gate
        heads = []
    violations: list[Violation] = []
    for head in heads:
        panel = protecting_panel(scene, head)
        datum = panel.bottom_mm if panel is not None else ceiling
        drop = datum - head.bottom_mm  # type: ignore[operator]
        if drop < lo - EPS_MM or drop > hi + EPS_MM:
            where = "the containment roof" if panel is not None else "the ceiling"
            violations.append(Violation(
                head.eid, head.name,
                f"deflector {drop:.0f}mm below {where}, must be "
                f"{lo:.0f}-{hi:.0f}mm ({lo_src.ref})",
            ))
    return RuleResult(
        rule="sprinkler_deflector_position", source=lo_src.ref,
        gradeable=lo_src.gradeable and hi_src.gradeable,
        threshold=f"{lo:.1f}-{hi:.1f} {lo_src.unit} below ceiling",
        checkable=tuple(h.eid for h in heads), violations=tuple(violations),
    )


def tray_below_tile(scene: Scene) -> RuleResult:
    """TIA-942-B-2017 8.6.2 — 20 mm minimum, tile underside to loaded tray top.

    Under-floor trays only: the clause governs the access-floor plenum. The
    datum is the underside of the ``FLOORING`` covering as actually modelled,
    not an assumed panel thickness.
    """
    sourced = specs.TRAY_BELOW_TILE
    minimum = float(sourced)
    floors = scene.of_kind("raised_floor")
    tile_underside = min((f.bottom_mm for f in floors), default=None)
    if tile_underside is None:
        # no access floor modelled, so no plenum and no datum to measure from
        trays: list[Elem] = []
    else:
        trays = [t for t in scene.of_kind("cable_tray")
                 if t.top_mm <= tile_underside + EPS_MM]
    violations: list[Violation] = []
    for tray in trays:
        gap = tile_underside - tray.top_mm  # type: ignore[operator]
        if gap < minimum - EPS_MM:
            violations.append(Violation(
                tray.eid, tray.name,
                f"tray top {gap:.0f}mm below the tile underside, needs "
                f"{minimum:.0f}mm ({sourced.ref})",
            ))
    return RuleResult(
        rule="tray_below_tile", source=sourced.ref, gradeable=sourced.gradeable,
        threshold=f">= {minimum:.0f} {sourced.unit}",
        checkable=tuple(t.eid for t in trays), violations=tuple(violations),
    )


def pdu_working_space(scene: Scene) -> RuleResult:
    """NEC 110.26(A) — clear working space at the front of every PDU.

    900 mm deep by 110.26(A)(1) and 2000 mm tall by 110.26(A)(3). The height
    is load-bearing: without it an overhead tray at 2600 mm reads as an
    obstruction to a floor-level clearance, which is stricter than the code.
    """
    depth_src = specs.NEC_WORKING_SPACE_DEPTH
    height_src = specs.NEC_WORKING_SPACE_HEIGHT
    depth, head_room = float(depth_src), float(height_src)
    pdus = scene.of_kind("pdu")
    violations: list[Violation] = []
    for pdu in pdus:
        band = _clearance_band(pdu, depth, front=True)
        if band is None:
            continue
        lo, hi = band
        hi[2] = min(hi[2], lo[2] + head_room)
        blockers = [other.label() for other in scene.elements
                    if other is not pdu and _overlaps(other, lo, hi)]
        if scene.hall_polygon_mm is not None and not box_in_polygon(
            list(scene.hall_polygon_mm), lo[0], lo[1], hi[0], hi[1]
        ):
            blockers.append("hall boundary")
        if blockers:
            violations.append(Violation(
                pdu.eid, pdu.name,
                f"{depth:.0f}mm working space in front of a {pdu.facing}-facing "
                f"PDU is obstructed by {blockers[0]} ({depth_src.ref})",
            ))
    return RuleResult(
        rule="pdu_working_space", source=depth_src.ref,
        gradeable=depth_src.gradeable and height_src.gradeable,
        threshold=f">= {depth:.0f} mm deep x {head_room:.0f} mm tall, clear",
        checkable=tuple(p.eid for p in pdus), violations=tuple(violations),
    )


def pdu_dedicated_space(scene: Scene) -> RuleResult:
    """NEC 110.26(E)(1)(a) — the 1800 mm above a PDU belongs to the electrics.

    Graded **only against chilled-water pipe**. Busway and cable tray are part
    of (or arguable as part of) the electrical installation rather than foreign
    to it, and 110.26(E)(1)(c) Exception explicitly permits sprinkler
    protection in the space, so failing an agent for any of those would be
    stricter than the code.
    """
    sourced = specs.DEDICATED_ELECTRICAL_SPACE
    reserve = float(sourced)
    pdus = scene.of_kind("pdu")
    pipes = scene.of_kind("chw_pipe")
    violations: list[Violation] = []
    for pdu in pdus:
        lo = [pdu.min_mm[0], pdu.min_mm[1], pdu.max_mm[2]]
        hi = [pdu.max_mm[0], pdu.max_mm[1], pdu.max_mm[2] + reserve]
        intruders = [p.label() for p in pipes if _overlaps(p, lo, hi)]
        if intruders:
            violations.append(Violation(
                pdu.eid, pdu.name,
                f"{intruders[0]} crosses the {reserve:.0f}mm dedicated "
                f"electrical space above this PDU ({sourced.ref})",
            ))
    return RuleResult(
        rule="pdu_dedicated_space", source=sourced.ref,
        gradeable=sourced.gradeable,
        threshold=f"no foreign piping within {reserve:.0f} mm above a PDU",
        checkable=tuple(p.eid for p in pdus), violations=tuple(violations),
    )


def power_telecom_separation(scene: Scene) -> RuleResult:
    """ANSI/TIA-942 7.3.1 Table 2 — 300 mm between telecom tray and busway.

    The table scales 50/100/150/300 mm with circuit count and doubles for
    unshielded power; 300 mm is the row for 91+ circuits or any 100 A 415 V
    three-phase feeder, which is what a data-hall busway is. Separation on any
    single axis satisfies it, so the measure is the largest axis gap — the
    task statement's "separated both horizontally and vertically" would be
    stricter than the clause and is not what is graded.
    """
    sourced = specs.POWER_TELECOM_SEPARATION
    required = float(sourced)
    trays = scene.of_kind("cable_tray")
    busways = scene.of_kind("busway")
    violations: list[Violation] = []
    for tray in trays:
        worst: tuple[float, str] | None = None
        for bus in busways:
            gap = max(max(tray.min_mm[i] - bus.max_mm[i],
                          bus.min_mm[i] - tray.max_mm[i]) for i in range(3))
            if gap < required - EPS_MM and (worst is None or gap < worst[0]):
                worst = (gap, bus.label())
        if worst is not None:
            violations.append(Violation(
                tray.eid, tray.name,
                f"{max(0.0, worst[0]):.0f}mm clear of {worst[1]}, needs "
                f"{required:.0f}mm on at least one axis ({sourced.ref})",
            ))
    return RuleResult(
        rule="power_telecom_separation", source=sourced.ref,
        gradeable=sourced.gradeable,
        threshold=f">= {required:.0f} {sourced.unit} on at least one axis",
        checkable=tuple(t.eid for t in trays), violations=tuple(violations),
    )


def sprinkler_max_spacing(scene: Scene) -> RuleResult:
    """NFPA 13 Table 10.2.4.2.1(a) — 4600 mm maximum spacing between heads.

    Independent of the 225 ft2 area cap and not implied by it: a layout can
    satisfy the area and still run one axis past the spacing limit.
    """
    sourced = specs.SPRINKLER_MAX_SPACING
    limit = float(sourced)
    heads = scene.of_kind("sprinkler")
    centres = [h.centroid_mm for h in heads]
    # per plane, for the reason given in sprinkler_protection_area
    panels = [protecting_panel(scene, h) for h in heads]
    planes = [(p.eid if p is not None else None) for p in panels]
    band_by_plane = {
        key: branch_line_band([c for c, k in zip(centres, planes) if k == key])
        for key in set(planes)
    }
    violations: list[Violation] = []
    for i, head in enumerate(heads):
        band = band_by_plane[planes[i]]
        worst: tuple[float, int] | None = None
        for axis in (0, 1):
            other = 1 - axis
            peers = [c[axis] for j, c in enumerate(centres)
                     if j != i and abs(c[other] - centres[i][other]) <= band
                     and planes[j] == planes[i]]
            for sign in (1.0, -1.0):
                gaps = [(p - centres[i][axis]) * sign for p in peers
                        if (p - centres[i][axis]) * sign > EPS_MM]
                if gaps and min(gaps) > limit + EPS_MM:
                    if worst is None or min(gaps) > worst[0]:
                        worst = (min(gaps), axis)
        if worst is not None:
            violations.append(Violation(
                head.eid, head.name,
                f"{worst[0]:.0f}mm to the next head along "
                f"{'X' if worst[1] == 0 else 'Y'}, max {limit:.0f}mm "
                f"({sourced.ref})",
            ))
    return RuleResult(
        rule="sprinkler_max_spacing", source=sourced.ref,
        gradeable=sourced.gradeable,
        threshold=f"<= {limit:.0f} {sourced.unit} between adjacent heads",
        checkable=tuple(h.eid for h in heads), violations=tuple(violations),
    )


#: Kinds that are "fixed obstructions" to a sprinkler discharge pattern.
OBSTRUCTION_KINDS = frozenset({"cable_tray", "busway", "chw_pipe"})

#: What NFPA 13 10.2.7.4.2's "fixed obstruction" means for the WIDTH rule, as
#: opposed to the three-times offset rule. A cold-aisle containment roof is a
#: fixed horizontal obstruction 1400-2000 mm wide spanning the length of an
#: aisle, and the clause does not care that it is furniture rather than
#: pathway. It is NOT in :data:`OBSTRUCTION_KINDS` because the other two rules
#: that set reads are about a run passing under a head: the lid sits at
#: 2055 mm and every ceiling deflector in these halls is at least 460 mm above
#: that, so it can never enter the pattern-development zone, and grading
#: `tray_stack_headroom` between a lid and a tray would read a clause about
#: access between stacked TRAYS onto something it does not cover.
FIXED_OBSTRUCTION_KINDS = OBSTRUCTION_KINDS | frozenset({"containment"})


def protecting_panel(scene: Scene, head: Elem):
    """The containment roof a head hangs under, or ``None`` for a ceiling head.

    NFPA 13 requires a sprinkler beneath a fixed obstruction wider than
    1200 mm, and once one is there its datum is the obstruction and not the
    room. Three rules would otherwise fail a head that is exactly where the
    code puts it: :func:`clear_height` reads 1890 mm against a 2600 mm room
    minimum, :func:`sprinkler_deflector_position` reads 1160 mm below a
    ceiling it does not hang from, and the branch-line grouping in
    :func:`sprinkler_protection_area` would take the ceiling grid as its
    peers. The lid is what it hangs from, so the lid is its datum.
    """
    for panel in scene.of_kind("containment"):
        if (_within_plan(head, panel)
                and head.top_mm <= panel.bottom_mm + EPS_MM):
            return panel
    return None


def sprinkler_obstruction_offset(scene: Scene) -> RuleResult:
    """NFPA 13 10.2.7.2.1.3 — the three-times rule, in its lenient reading.

    A run inside the 450 mm pattern-development zone below a deflector has to
    stand ``min(3 x its cross-section, 610 mm)`` clear of the head in plan.
    A run *below* that zone is not an obstruction for this clause at all, which
    is exactly why a two-tier overhead layout works and a single high tier does
    not: the lower tier may cross beneath a head, the upper one may not.

    Every judgement call here is taken the lenient way. The obstruction
    dimension is the run's smallest horizontal extent, not its length; the zone
    test is inclusive; and the rule is keyed on the head, so a head with
    nothing near it is checkable and clean rather than absent.
    """
    zone_src = specs.SPRINKLER_OBSTRUCTION_ZONE
    cap_src = specs.SPRINKLER_OBSTRUCTION_OFFSET_MAX
    zone, cap = float(zone_src), float(cap_src)
    heads = scene.of_kind("sprinkler")
    runs = scene.of_kind(*OBSTRUCTION_KINDS)
    violations: list[Violation] = []
    for head in heads:
        deflector = head.bottom_mm
        worst: tuple[float, float, str] | None = None
        for run in runs:
            if run.top_mm < deflector - zone - EPS_MM or run.bottom_mm > deflector:
                continue                      # outside the pattern-development zone
            width = min(run.max_mm[i] - run.min_mm[i] for i in (0, 1))
            required = min(3.0 * width, cap)
            gap = max(max(head.min_mm[i] - run.max_mm[i],
                          run.min_mm[i] - head.max_mm[i]) for i in (0, 1))
            if gap < required - EPS_MM and (worst is None or gap < worst[0]):
                worst = (gap, required, run.label())
        if worst is not None:
            gap, required, culprit = worst
            violations.append(Violation(
                head.eid, head.name,
                f"{max(0.0, gap):.0f}mm horizontal clear of {culprit} inside the "
                f"{zone:.0f}mm pattern-development zone, needs {required:.0f}mm "
                f"({cap_src.ref})",
            ))
    return RuleResult(
        rule="sprinkler_obstruction_offset", source=cap_src.ref,
        gradeable=cap_src.gradeable and zone_src.gradeable,
        threshold=f"3x the obstruction, capped at {cap:.0f} mm, within "
                  f"{zone:.0f} mm below the deflector",
        checkable=tuple(h.eid for h in heads), violations=tuple(violations),
    )


def wide_obstruction_needs_sprinkler(scene: Scene) -> RuleResult:
    """NFPA 13 9.5.5.3.1 — a head is required under a fixed obstruction >1200 mm.

    Per element, never per bank: whether three runs side by side count as one
    obstruction is a judgement call, and reading them as one would be stricter
    than the clause. This is the rule that stops "one enormous tray" being the
    easy answer to a congested plenum.
    """
    sourced = specs.OBSTRUCTION_WIDTH_NEEDING_SPRINKLER
    limit = float(sourced)
    runs = [r for r in scene.of_kind(*FIXED_OBSTRUCTION_KINDS)
            if min(r.max_mm[i] - r.min_mm[i] for i in (0, 1)) > limit]
    heads = scene.of_kind("sprinkler")
    violations: list[Violation] = []
    for run in runs:
        beneath = any(
            head.top_mm <= run.bottom_mm + EPS_MM and _plan_overlaps(head, run)
            for head in heads)
        if not beneath:
            width = min(run.max_mm[i] - run.min_mm[i] for i in (0, 1))
            violations.append(Violation(
                run.eid, run.name,
                f"{width:.0f}mm wide with no sprinkler beneath it, and anything "
                f"over {limit:.0f}mm needs one ({sourced.ref})",
            ))
    return RuleResult(
        rule="wide_obstruction_needs_sprinkler", source=sourced.ref,
        gradeable=sourced.gradeable,
        threshold=f"a head beneath any obstruction wider than {limit:.0f} mm",
        checkable=tuple(r.eid for r in runs), violations=tuple(violations),
    )


def tray_stack_headroom(scene: Scene) -> RuleResult:
    """ANSI/TIA-569-B via TIA-942 7.3.2 — 300 mm access between stacked tiers.

    Only between runs that actually sit one above the other in plan; two trays
    at the same elevation in different aisles are not stacked, and neither is a
    tray that merely shares a bounding box with one three metres away.
    """
    sourced = specs.TRAY_STACK_HEADROOM
    minimum = float(sourced)
    runs = scene.of_kind(*OBSTRUCTION_KINDS)
    violations: list[Violation] = []
    for lower in runs:
        worst: tuple[float, str] | None = None
        for upper in runs:
            if upper is lower or not _plan_overlaps(lower, upper):
                continue
            gap = upper.bottom_mm - lower.top_mm
            if upper.kind == lower.kind and gap <= EPS_MM:
                # a riser and the branch it feeds touch by definition; they are
                # one pathway with a bend in it, not two stacked trays needing
                # access between them
                continue
            if 0.0 <= gap < minimum - EPS_MM and (worst is None or gap < worst[0]):
                worst = (gap, upper.label())
        if worst is not None:
            violations.append(Violation(
                lower.eid, lower.name,
                f"{worst[0]:.0f}mm of access below {worst[1]}, needs "
                f"{minimum:.0f}mm ({sourced.ref})",
            ))
    return RuleResult(
        rule="tray_stack_headroom", source=sourced.ref,
        gradeable=sourced.gradeable,
        threshold=f">= {minimum:.0f} {sourced.unit} between stacked runs",
        checkable=tuple(r.eid for r in runs), violations=tuple(violations),
    )


def tile_alignment(scene: Scene) -> RuleResult:
    """ADVISORY. TIA-942-B-2017 6.14.3 says cabinets *should* align to a tile.

    Reported, never scored: :data:`specs.TILE_ALIGNMENT` is a RECOMMENDATION,
    and no TIA clause mandates the 600 mm module at all. An off-grid rack is
    still penalised — through ``recovery``, where a determinate task belongs.
    """
    sourced = specs.TILE_ALIGNMENT
    pitch = float(specs.TILE_PITCH)
    racks = scene.of_kind("rack")
    # the grid origin is not recorded in IFC; the hall's min corner is the
    # convention dcgen builds to and the only datum a drawing would imply
    if scene.hall is not None:
        origin = (scene.hall.min_mm[0], scene.hall.min_mm[1])
    elif scene.hall_polygon_mm:
        origin = (min(p[0] for p in scene.hall_polygon_mm),
                  min(p[1] for p in scene.hall_polygon_mm))
    else:
        origin = (0.0, 0.0)
    violations: list[Violation] = []
    for rack in racks:
        off = [
            abs(((rack.min_mm[i] - origin[i]) + pitch / 2.0) % pitch - pitch / 2.0)
            for i in (0, 1)
        ]
        if max(off) > EPS_MM:
            violations.append(Violation(
                rack.eid, rack.name,
                f"min corner {off[0]:.0f}/{off[1]:.0f}mm off the {pitch:.0f}mm "
                f"tile grid ({sourced.ref}, advisory)",
            ))
    return RuleResult(
        rule="tile_alignment", source=sourced.ref, gradeable=sourced.gradeable,
        threshold=f"on the {pitch:.0f}mm tile grid (recommendation)",
        checkable=tuple(r.eid for r in racks), violations=tuple(violations),
    )


def facing_metadata_consistent(scene: Scene) -> RuleResult:
    """ADVISORY. Does ``Pset_DCM_Rack.FacingDirection`` agree with the placement?

    Geometry is the authority everywhere else in this grader, so a disagreement
    cannot change the score — but it is invisible otherwise, and it is exactly
    the failure an agent makes when it writes the schedule from the drawing and
    then places the cabinet from the plan.
    """
    racks = scene.of_kind("rack")
    violations: list[Violation] = []
    for rack in racks:
        declared = rack.psets.get("Pset_DCM_Rack", {}).get("FacingDirection")
        if declared is None or rack.facing is None:
            continue
        if str(declared).upper() != rack.facing:
            violations.append(Violation(
                rack.eid, rack.name,
                f"Pset says {declared}, placement is rotated to {rack.facing}",
            ))
    return RuleResult(
        rule="facing_metadata_consistent", source="dcgrade (advisory)",
        gradeable=False, threshold="pset matches realised rotation",
        checkable=tuple(r.eid for r in racks), violations=tuple(violations),
    )


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

RuleFn = Callable[[Scene], RuleResult]

#: Task A is the determinate fit-out: cabinets, cooling, power distribution and
#: containment. The sprinkler rules stay in the battery because an agent that
#: models heads is held to them, but they contribute nothing when it does not —
#: ``checkable`` is empty and ``compliance`` divides by the pairs that exist.
TASK_A_RULES: tuple[RuleFn, ...] = (
    front_clearance,
    rear_clearance,
    clear_height,
    pdu_working_space,
    sprinkler_clearance,
    sprinkler_protection_area,
    sprinkler_max_spacing,
    sprinkler_deflector_position,
    tile_alignment,
    facing_metadata_consistent,
)

#: Task B is overhead MEP coordination: the racks are given, the plenum is not.
#: Cabinet clearances are excluded — the agent does not choose where cabinets
#: go — but everything the routing can breach is in, including the two rules
#: that make the plenum a coordination problem rather than a drawing exercise:
#: the NEC dedicated space over a PDU and the TIA-942 power/telecom separation.
TASK_B_RULES: tuple[RuleFn, ...] = (
    clear_height,
    sprinkler_clearance,
    sprinkler_obstruction_offset,
    wide_obstruction_needs_sprinkler,
    tray_stack_headroom,
    tray_below_tile,
    pdu_dedicated_space,
    power_telecom_separation,
)

RULES_BY_TASK: dict[str, tuple[RuleFn, ...]] = {"A": TASK_A_RULES, "B": TASK_B_RULES}


def run_rules(scene: Scene, task: str = "A") -> dict[str, RuleResult]:
    """Every rule in the task's battery, graded and advisory alike."""
    try:
        battery = RULES_BY_TASK[task.upper()]
    except KeyError:
        raise ValueError(f"unknown task {task!r}; expected one of {sorted(RULES_BY_TASK)}")
    return {r.__name__: r(scene) for r in battery}


def compliance(results: dict[str, RuleResult]) -> dict:
    """``violation_rate`` over element-rule pairs, graded rules only.

    Advisory results are carried through in ``advisory`` so a reviewer sees
    them, but they cannot move the number — that separation is the point of
    :attr:`specs.Sourced.gradeable`.
    """
    graded = {k: r for k, r in results.items() if r.gradeable}
    advisory = {k: r for k, r in results.items() if not r.gradeable}
    checkable = sum(len(r.checkable) for r in graded.values())
    violating = sum(len(r.violations) for r in graded.values())
    # no checkable pairs means nothing was asserted, not that everything failed
    rate = (violating / checkable) if checkable else 0.0
    return {
        "violation_rate": rate,
        "checkable_pairs": checkable,
        "violating_pairs": violating,
        "violations_by_rule": {k: r.to_dict() for k, r in graded.items()},
        "advisory": {k: r.to_dict() for k, r in advisory.items()},
    }
