"""Typed element builders: hall program objects -> :class:`ElementSpec`.

Every IFC class and PredefinedType below was verified present in the IFC4 schema
via ``ifcopenshell.ifcopenshell_wrapper.schema_by_name("IFC4")``. The mapping is
also what ``dcgrade`` keys on, so it is defined once, here.

The one judgement call is racks. IFC4 has no ``IfcRack``; the industry convention
is ``IfcFurniture`` with an ``ObjectType`` discriminator, and that is what both
the generator and the grader use. It is a convention, not a standard, and is
pinned in the task statement so it cannot drift.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ifc_writer import ElementSpec
from .units import FACING_ANGLES

#: element kind -> (IfcClass, PredefinedType, ObjectType)
IFC_TYPES: dict[str, tuple[str, str | None, str | None]] = {
    "rack":         ("IfcFurniture",                "USERDEFINED",         "Rack"),
    "crah":         ("IfcUnitaryEquipment",         "AIRCONDITIONINGUNIT", None),
    "pdu":          ("IfcElectricDistributionBoard", "DISTRIBUTIONBOARD",  None),
    "cable_tray":   ("IfcCableCarrierSegment",      "CABLETRAYSEGMENT",    None),
    "busway":       ("IfcCableSegment",             "BUSBARSEGMENT",       None),
    "chw_pipe":     ("IfcPipeSegment",              "RIGIDSEGMENT",        None),
    "sprinkler":    ("IfcFireSuppressionTerminal",  "SPRINKLER",           None),
    "raised_floor": ("IfcCovering",                 "FLOORING",            None),
    "containment":  ("IfcCovering",                 "CLADDING",            None),
    "column":       ("IfcColumn",                   None,                  None),
}

#: Property sets the grader requires. Missing keys fail the semantics check.
REQUIRED_PSETS: dict[str, str] = {
    "rack": "Pset_DCM_Rack",
    "crah": "Pset_DCM_Cooling",
    "pdu": "Pset_DCM_Power",
    "cable_tray": "Pset_DCM_Route",
    "busway": "Pset_DCM_Route",
    "chw_pipe": "Pset_DCM_Route",
}


def _spec(kind: str, name: str, w: float, d: float, h: float,
          x: float, y: float, z: float = 0.0, rotation_deg: float = 0.0,
          psets: dict[str, dict[str, Any]] | None = None) -> ElementSpec:
    ifc_class, predefined, object_type = IFC_TYPES[kind]
    return ElementSpec(
        ifc_class=ifc_class, name=name,
        width_mm=w, depth_mm=d, height_mm=h,
        x_mm=x, y_mm=y, z_mm=z, rotation_deg=rotation_deg,
        predefined_type=predefined, object_type=object_type,
        psets=psets or {},
    )


#: Enclosure footprint -> the type mark the drawings label it with. The mark
#: is a property on the cabinet as well as a column in A-601's type schedule,
#: because a row of mixed widths is only readable if the plan symbol resolves
#: to a dimension somewhere, and TIA-606-C asset naming is keyed to the type.
CABINET_TYPE_MARKS: dict[tuple[float, float], str] = {
    (600.0, 1200.0): "C1",
    (600.0, 1070.0): "C2",
    (800.0, 1200.0): "N1",
    (800.0, 1070.0): "N2",
}


def cabinet_type_mark(width_mm: float, depth_mm: float) -> str:
    """Type mark for one enclosure footprint; generated if unlisted."""
    return CABINET_TYPE_MARKS.get((round(width_mm, 1), round(depth_mm, 1)),
                                  f"C{int(round(width_mm))}")


@dataclass(frozen=True)
class Rack:
    """One cabinet, positioned by the min corner of its footprint (mm)."""

    name: str
    x_mm: float
    y_mm: float
    width_mm: float
    depth_mm: float
    height_mm: float
    rack_units: int
    row_id: str
    facing: str
    tile: str
    z_mm: float = 0.0
    #: ``"it"`` or ``"network"``. Generator-side only — what reaches the model
    #: is the type mark, which is what the drawings state.
    bay_kind: str = "it"

    @property
    def type_mark(self) -> str:
        return cabinet_type_mark(self.width_mm, self.depth_mm)

    def to_spec(self) -> ElementSpec:
        return _spec(
            "rack", self.name, self.width_mm, self.depth_mm, self.height_mm,
            self.x_mm, self.y_mm, self.z_mm,
            rotation_deg=FACING_ANGLES[self.facing],
            psets={"Pset_DCM_Rack": {
                "RackUnits": int(self.rack_units),
                "WidthMM": float(self.width_mm),
                "DepthMM": float(self.depth_mm),
                "TileCoordinate": self.tile,
                "RowId": self.row_id,
                "FacingDirection": self.facing,
                "CabinetType": self.type_mark,
            }},
        )


@dataclass(frozen=True)
class Crah:
    """A chilled-water air handler, at the perimeter or standing in a row.

    ``placement`` is the only thing that distinguishes the two, and it exists
    because chilled water has to *reach* these and nothing else. In-row units
    put a consumer inside every row, which is what makes gate 2's chilled-water
    rule true rather than merely enforced.
    """

    name: str
    x_mm: float
    y_mm: float
    width_mm: float
    depth_mm: float
    height_mm: float
    capacity_kw: float
    airflow_m3h: float
    facing: str
    z_mm: float = 0.0
    #: ``"perimeter"`` or ``"in_row"``
    placement: str = "perimeter"
    #: for an in-row unit, the row it stands in; ``""`` at the perimeter
    row_id: str = ""

    def to_spec(self) -> ElementSpec:
        return _spec(
            "crah", self.name, self.width_mm, self.depth_mm, self.height_mm,
            self.x_mm, self.y_mm, self.z_mm,
            rotation_deg=FACING_ANGLES[self.facing],
            psets={"Pset_DCM_Cooling": {
                "CapacityKW": float(self.capacity_kw),
                "AirflowM3H": float(self.airflow_m3h),
                "FacingDirection": self.facing,
                "Placement": self.placement,
            }},
        )


@dataclass(frozen=True)
class Pdu:
    """Floor-mount power distribution unit, serving one rack row.

    Two things make a PDU more than another box. NEC 110.26(A)(1) demands
    900 mm of clear working space at its front, and 110.26(E)(1)(a) reserves
    the 1800 mm *above* it for the electrical installation — so a chilled-water
    main cannot cross over one. Both are graded.
    """

    name: str
    x_mm: float
    y_mm: float
    width_mm: float
    depth_mm: float
    height_mm: float
    rating_kva: float = 225.0
    serves_row: str = ""
    facing: str = "NORTH"
    z_mm: float = 0.0

    def to_spec(self) -> ElementSpec:
        return _spec("pdu", self.name, self.width_mm, self.depth_mm,
                     self.height_mm, self.x_mm, self.y_mm, self.z_mm,
                     rotation_deg=FACING_ANGLES[self.facing],
                     psets={"Pset_DCM_Power": {
                         "RatingKVA": float(self.rating_kva),
                         "ServesRow": self.serves_row,
                         "FacingDirection": self.facing,
                     }})


@dataclass(frozen=True)
class Containment:
    """One panel of a cold-aisle containment assembly.

    Modelled as plain rectangular panels — a roof over the aisle and a door
    assembly at each end — because that is what a plan and a section show. The
    roof deliberately overhangs the aisle onto the cabinet tops
    (:data:`specs.CONTAINMENT_ROOF_OVERHANG`), which is why its plan extent is
    wider than the aisle it closes.
    """

    name: str
    x_mm: float
    y_mm: float
    z_mm: float
    width_mm: float
    depth_mm: float
    height_mm: float
    aisle_id: str
    part: str            # "roof" | "door"

    def to_spec(self) -> ElementSpec:
        return _spec("containment", self.name, self.width_mm, self.depth_mm,
                     self.height_mm, self.x_mm, self.y_mm, self.z_mm)


@dataclass(frozen=True)
class Column:
    name: str
    x_mm: float
    y_mm: float
    size_mm: float
    height_mm: float

    def to_spec(self) -> ElementSpec:
        return _spec("column", self.name, self.size_mm, self.size_mm,
                     self.height_mm, self.x_mm, self.y_mm)


@dataclass(frozen=True)
class Sprinkler:
    """Pendent head. ``z_mm`` is the DEFLECTOR elevation above finished floor.

    NFPA 13 constrains the deflector's distance below the ceiling, and TIA-942
    constrains clearance from the head to anything below it, so the deflector
    plane is the graded datum — not the head's nominal centre.
    """

    name: str
    x_mm: float
    y_mm: float
    z_mm: float
    body_mm: float = 50.0

    def to_spec(self) -> ElementSpec:
        # x/y are the protection-cell CENTRE, but box geometry is placed from
        # its min corner, so offset by half the body or the head sits half a
        # body-width north-east of the point every clearance check uses.
        half = self.body_mm / 2.0
        return _spec("sprinkler", self.name, self.body_mm, self.body_mm,
                     self.body_mm, self.x_mm - half, self.y_mm - half, self.z_mm)


@dataclass(frozen=True)
class RouteSegment:
    """A straight run of tray, busway or pipe.

    Routes are axis-aligned runs; ``kind`` selects the IFC typing and drives the
    separation rules (power vs telecom cannot share a tier without separation).
    """

    kind: str            # cable_tray | busway | chw_pipe
    name: str
    x_mm: float
    y_mm: float
    z_mm: float
    length_mm: float
    width_mm: float
    height_mm: float
    #: "X" or "Y" for a horizontal run, "Z" for a riser. A riser is what joins
    #: an under-floor main to an overhead branch, and without one the two are
    #: separate objects that happen to be in the same building.
    axis: str
    service: str         # "telecom" | "power" | "chilled_water"

    def to_spec(self) -> ElementSpec:
        if self.axis == "X":
            w, d, h = self.length_mm, self.width_mm, self.height_mm
        elif self.axis == "Y":
            w, d, h = self.width_mm, self.length_mm, self.height_mm
        else:
            w, d, h = self.width_mm, self.height_mm, self.length_mm
        return _spec(
            self.kind, self.name, w, d, h,
            self.x_mm, self.y_mm, self.z_mm,
            psets={"Pset_DCM_Route": {
                "Service": self.service,
                "Axis": self.axis,
                "LengthMM": float(self.length_mm),
            }},
        )

    @property
    def centreline_length_mm(self) -> float:
        return self.length_mm
