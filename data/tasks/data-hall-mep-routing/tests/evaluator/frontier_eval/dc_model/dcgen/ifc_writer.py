"""IFC4 emission layer for dcgen.

Wraps the parts of the IfcOpenShell API that dcgen needs, with the mm/metre
convention from :mod:`units` enforced at a single choke point so it cannot be
got wrong per-call site.

Spatial structure follows IFC semantics rather than convenience:

    IfcProject -> IfcSite -> IfcBuilding -> IfcBuildingStorey -> IfcSpace
                                                                    |
                                                          elements contained here

``IfcSpace`` is a *spatial structure element*, so it is **aggregated** into the
storey (``IfcRelAggregates``), not contained in it. Physical elements are
**contained** (``IfcRelContainedInSpatialStructure``) in the hall space, which is
the most precise legal container and lets the grader ask "is this rack actually
inside the hall".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np

import ifcopenshell
import ifcopenshell.api

from .units import mm_matrix_to_ifc, rotation_z, translation_mm

run = ifcopenshell.api.run


@dataclass
class ElementSpec:
    """A physical element to emit.

    Geometry is an axis-aligned box in mm, with its origin at the **min corner**
    before rotation, so that a rack's footprint maps directly onto tile corners.
    """

    ifc_class: str
    name: str
    width_mm: float
    depth_mm: float
    height_mm: float
    x_mm: float
    y_mm: float
    z_mm: float = 0.0
    rotation_deg: float = 0.0
    predefined_type: str | None = None
    object_type: str | None = None
    psets: dict[str, dict[str, Any]] = field(default_factory=dict)


class IfcBuilder:
    """Builds one hall model."""

    def __init__(self, project_name: str = "DC Model", hall_name: str = "DATA HALL 1",
                 storey_name: str = "LEVEL 1", building_name: str = "DC-01") -> None:
        self.model = run("project.create_file", version="IFC4")
        self.project = run("root.create_entity", self.model,
                           ifc_class="IfcProject", name=project_name)
        run("unit.assign_unit", self.model,
            length={"is_metric": True, "raw": "MILLIMETERS"})

        ctx = run("context.add_context", self.model, context_type="Model")
        self.body = run("context.add_context", self.model,
                        context_type="Model", context_identifier="Body",
                        target_view="MODEL_VIEW", parent=ctx)

        self.site = run("root.create_entity", self.model, ifc_class="IfcSite", name="SITE")
        self.building = run("root.create_entity", self.model,
                            ifc_class="IfcBuilding", name=building_name)
        self.storey = run("root.create_entity", self.model,
                          ifc_class="IfcBuildingStorey", name=storey_name)
        run("aggregate.assign_object", self.model,
            products=[self.site], relating_object=self.project)
        run("aggregate.assign_object", self.model,
            products=[self.building], relating_object=self.site)
        run("aggregate.assign_object", self.model,
            products=[self.storey], relating_object=self.building)

        self.hall = run("root.create_entity", self.model,
                        ifc_class="IfcSpace", name=hall_name)
        # spatial element -> aggregated, NOT contained
        run("aggregate.assign_object", self.model,
            products=[self.hall], relating_object=self.storey)
        self._hall_placed = False

    # ------------------------------------------------------------------ geometry

    def _polyline_profile(self, points_mm: Sequence[tuple[float, float]]):
        """Closed profile from an mm point list (raw file units).

        Points MUST be 2D. IFC4 EXPRESS rule ``IfcArbitraryClosedProfileDef.WR1``
        requires ``OuterCurve.Dim == 2``; emitting 3D ``IfcCartesianPoint``s here
        produces a file that parses and renders fine but fails every
        express-rule validation, one violation per element. Note that
        ``ifcopenshell.validate.validate(model, logger)`` does **not** catch this
        unless you pass ``express_rules=True``.
        """
        pts = list(points_mm)
        if pts[0] != pts[-1]:
            pts = pts + [pts[0]]
        poly = self.model.create_entity(
            "IfcPolyline",
            Points=[
                self.model.create_entity("IfcCartesianPoint", Coordinates=(float(x), float(y)))
                for x, y in pts
            ],
        )
        return self.model.create_entity(
            "IfcArbitraryClosedProfileDef", ProfileType="AREA", OuterCurve=poly
        )

    def _extrude(self, profile, height_mm: float):
        solid = self.model.create_entity(
            "IfcExtrudedAreaSolid",
            SweptArea=profile,
            Position=self.model.create_entity(
                "IfcAxis2Placement3D",
                Location=self.model.create_entity(
                    "IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
            ),
            ExtrudedDirection=self.model.create_entity(
                "IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
            Depth=float(height_mm),
        )
        return self.model.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=self.body,
            RepresentationIdentifier="Body",
            RepresentationType="SweptSolid",
            Items=[solid],
        )

    def box_representation(self, width_mm: float, depth_mm: float, height_mm: float):
        return self._extrude(
            self._polyline_profile(
                [(0.0, 0.0), (width_mm, 0.0), (width_mm, depth_mm), (0.0, depth_mm)]
            ),
            height_mm,
        )

    def prism_representation(self, footprint_mm: Sequence[tuple[float, float]], height_mm: float):
        """Extruded arbitrary footprint — used for L-shaped halls."""
        return self._extrude(self._polyline_profile(footprint_mm), height_mm)

    # ------------------------------------------------------------------ placement

    def place(self, product, x_mm: float, y_mm: float, z_mm: float = 0.0,
              rotation_deg: float = 0.0,
              pivot_mm: tuple[float, float] | None = None) -> None:
        """Place a product from mm coordinates.

        The single choke point for the mm -> metre conversion. Nothing else in
        dcgen may call ``geometry.edit_object_placement``.

        ``pivot_mm`` is the local point to rotate about. Box geometry has its
        origin at the MIN CORNER, so rotating about the default origin swings the
        box out of its own footprint — a 180-degree rotation moves it wholly into
        the -X/-Y quadrant, one full footprint away from where it was placed.
        Passing the footprint centre keeps the occupied volume put, which is what
        "this cabinet faces south" should mean.
        """
        matrix = translation_mm(x_mm, y_mm, z_mm)
        if rotation_deg:
            if pivot_mm is not None:
                px, py = pivot_mm
                matrix = (matrix
                          @ translation_mm(px, py, 0.0)
                          @ rotation_z(rotation_deg)
                          @ translation_mm(-px, -py, 0.0))
            else:
                matrix = matrix @ rotation_z(rotation_deg)
        run("geometry.edit_object_placement", self.model,
            product=product, matrix=mm_matrix_to_ifc(matrix))

    # ------------------------------------------------------------------ elements

    def set_hall_geometry(self, footprint_mm: Sequence[tuple[float, float]],
                          height_mm: float, z_mm: float = 0.0) -> None:
        run("geometry.assign_representation", self.model, product=self.hall,
            representation=self.prism_representation(footprint_mm, height_mm))
        self.place(self.hall, 0.0, 0.0, z_mm)
        self._hall_placed = True

    def add(self, spec: ElementSpec, footprint_mm: Sequence[tuple[float, float]] | None = None):
        """Emit one element, contained in the hall space.

        ``footprint_mm`` overrides the box profile with an arbitrary polygon —
        used for the access floor, which must follow the hall outline rather
        than its bounding box so it does not extend into an L-shaped notch.
        """
        product = run("root.create_entity", self.model,
                      ifc_class=spec.ifc_class, name=spec.name)

        if spec.predefined_type is not None:
            product.PredefinedType = spec.predefined_type
        if spec.object_type is not None:
            product.ObjectType = spec.object_type

        run("spatial.assign_container", self.model,
            products=[product], relating_structure=self.hall)
        representation = (
            self.prism_representation(footprint_mm, spec.height_mm)
            if footprint_mm is not None
            else self.box_representation(spec.width_mm, spec.depth_mm, spec.height_mm)
        )
        run("geometry.assign_representation", self.model, product=product,
            representation=representation)
        # rotate about the footprint centre so facing does not move the element
        self.place(product, spec.x_mm, spec.y_mm, spec.z_mm, spec.rotation_deg,
                   pivot_mm=(spec.width_mm / 2.0, spec.depth_mm / 2.0))

        for pset_name, props in spec.psets.items():
            pset = run("pset.add_pset", self.model, product=product, name=pset_name)
            run("pset.edit_pset", self.model, pset=pset, properties=props)
        return product

    def add_all(self, specs: Iterable[ElementSpec]) -> list:
        return [self.add(s) for s in specs]

    # ------------------------------------------------------------------ output

    def write(self, path: str) -> str:
        if not self._hall_placed:
            raise RuntimeError(
                "hall geometry never set — call set_hall_geometry() before write()"
            )
        self.model.write(path)
        return path
