"""Deterministic Site Sandbox geometry and evidence evaluation."""
from __future__ import annotations

import math
from typing import Any

from shapely.geometry import Polygon, shape
from shapely.ops import transform

from app.sandbox import EARTH_RADIUS_M, SCENE_SCHEMA_VERSION


FRAME_VERSION = "local_tangent_plane_v1"
GEOMETRY_EVIDENCE_IDS = ("parcel_id", "parcel_boundary_geojson", "parcel_match_type", "parcel_match_distance_m")


class SceneValidationError(ValueError):
    """Raised when mutable scene state cannot describe a valid proposed object."""


def _result(
    constraint_id: str,
    outcome: str,
    *,
    basis: str = "DERIVED",
    evidence_ids: list[str] | tuple[str, ...] = (),
    calculation: str,
    inputs: dict | None = None,
    result: Any = None,
    units: str | None = None,
    explanation: str,
) -> dict:
    return {
        "constraint_id": constraint_id,
        "basis": basis,
        "outcome": outcome,
        "evidence_ids": list(evidence_ids),
        "calculation": calculation,
        "inputs": inputs or {},
        "result": result,
        "units": units,
        "explanation": explanation,
    }


def _number(value: Any, name: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SceneValidationError(f"{name} must be numeric.") from exc
    if not math.isfinite(number) or (positive and number <= 0) or (nonnegative and number < 0):
        qualifier = "a positive" if positive else "a non-negative" if nonnegative else "finite"
        raise SceneValidationError(f"{name} must be {qualifier} finite number.")
    return number


def _frame(scene_state: dict, snapshot: dict) -> dict:
    if scene_state.get("schema_version") != SCENE_SCHEMA_VERSION:
        raise SceneValidationError("Unsupported scene state schema version.")
    if scene_state.get("site_snapshot_id") != snapshot.get("snapshot_id"):
        raise SceneValidationError("Scene state does not belong to this SiteSnapshot.")
    frame = scene_state.get("frame")
    if not isinstance(frame, dict) or frame.get("coordinate_frame_version") != FRAME_VERSION:
        raise SceneValidationError("Scene state must use local_tangent_plane_v1.")
    origin = frame.get("origin")
    if not isinstance(origin, dict):
        raise SceneValidationError("Scene state has no local frame origin.")
    lat = _number(origin.get("lat"), "frame.origin.lat")
    lng = _number(origin.get("lng"), "frame.origin.lng")
    selected = snapshot.get("parcel_identity", {}).get("selected_point", {})
    if not math.isclose(lat, _number(selected.get("lat"), "snapshot selected latitude"), abs_tol=1e-9) or not math.isclose(
        lng, _number(selected.get("lng"), "snapshot selected longitude"), abs_tol=1e-9
    ):
        raise SceneValidationError("Scene frame origin must match the SiteSnapshot selected point.")
    return {"lat": lat, "lng": lng}


def _to_local_geometry(geometry: dict, origin: dict) -> Any:
    source = shape(geometry)
    if source.is_empty or not source.is_valid:
        raise SceneValidationError("Observed geometry is invalid.")
    latitude_cosine = math.cos(math.radians(origin["lat"]))
    if abs(latitude_cosine) < 1e-12:
        raise SceneValidationError("Local tangent plane is unsupported at this latitude.")

    def project(lng: float, lat: float, z: float | None = None):
        x = EARTH_RADIUS_M * latitude_cosine * math.radians(lng - origin["lng"])
        y = EARTH_RADIUS_M * math.radians(lat - origin["lat"])
        return (x, y) if z is None else (x, y, z)

    return transform(project, source)


def build_oriented_footprint(proposed_object: dict) -> Polygon:
    """Build a local-meter rectangle from the declared scene transform."""
    geometry = proposed_object.get("geometry_local")
    if not isinstance(geometry, dict) or geometry.get("shape") != "oriented_rectangle":
        raise SceneValidationError("Proposed objects must use an oriented_rectangle footprint.")
    center = geometry.get("center_xy_m")
    if not isinstance(center, list) or len(center) != 2:
        raise SceneValidationError("geometry_local.center_xy_m must contain two coordinates.")
    center_x = _number(center[0], "geometry_local.center_xy_m[0]")
    center_y = _number(center[1], "geometry_local.center_xy_m[1]")
    width = _number(geometry.get("width_m"), "geometry_local.width_m", positive=True)
    length = _number(geometry.get("length_m"), "geometry_local.length_m", positive=True)
    _number(geometry.get("height_m"), "geometry_local.height_m", positive=True)
    _number(proposed_object.get("attributes", {}).get("capacity_mw"), "attributes.capacity_mw", positive=True)
    radians = math.radians(_number(geometry.get("rotation_deg"), "geometry_local.rotation_deg"))
    cosine, sine = math.cos(radians), math.sin(radians)
    corners = []
    for x, y in ((-width / 2, -length / 2), (width / 2, -length / 2), (width / 2, length / 2), (-width / 2, length / 2)):
        corners.append((center_x + x * cosine - y * sine, center_y + x * sine + y * cosine))
    footprint = Polygon(corners)
    if footprint.is_empty or not footprint.is_valid or footprint.area <= 0:
        raise SceneValidationError("Proposed footprint is degenerate.")
    return footprint


def _geometry_evidence_issue(snapshot: dict) -> str | None:
    if snapshot.get("is_expired"):
        return "The SiteSnapshot is stale; parcel geometry cannot be treated as current."
    identity = snapshot.get("parcel_identity", {})
    if identity.get("parcel_match_type") != "exact_intersect" or identity.get("parcel_match_distance_m") != 0:
        return "The SiteSnapshot does not prove an exact parcel identity."
    evidence = snapshot.get("evidence")
    if not isinstance(evidence, dict):
        return "Required parcel evidence is missing."
    for evidence_id in GEOMETRY_EVIDENCE_IDS:
        record = evidence.get(evidence_id)
        if not isinstance(record, dict) or record.get("status") not in {"ok", None} or record.get("value") is None:
            return f"Required parcel evidence is missing or unavailable: {evidence_id}."
    return None


def _objects(scene_state: dict) -> list[tuple[dict, Polygon]]:
    proposed = scene_state.get("proposed")
    if not isinstance(proposed, list) or not proposed:
        raise SceneValidationError("Scene state must contain at least one proposed object.")
    objects = []
    seen = set()
    for object_state in proposed:
        object_id = object_state.get("id")
        if not isinstance(object_id, str) or not object_id or object_id in seen:
            raise SceneValidationError("Each proposed object requires a unique id.")
        seen.add(object_id)
        objects.append((object_state, build_oriented_footprint(object_state)))
    return objects


def _selected_objects(objects: list[tuple[dict, Polygon]], spec: dict) -> list[tuple[dict, Polygon]]:
    object_id = spec.get("object_id")
    selected = [(item, footprint) for item, footprint in objects if object_id in {None, item["id"]}]
    if not selected:
        raise SceneValidationError(f"Requested object was not found: {object_id}.")
    return selected


def _metrics(parcel: Polygon, objects: list[tuple[dict, Polygon]]) -> dict:
    parcel_area = parcel.area
    values = {}
    for item, footprint in objects:
        values[item["id"]] = {
            "footprint_area_m2": round(footprint.area, 6),
            "parcel_area_m2": round(parcel_area, 6),
            "parcel_coverage_percent": round(100 * footprint.area / parcel_area, 6),
            "minimum_boundary_distance_m": round(footprint.boundary.distance(parcel.boundary), 6),
        }
    return values


def _geometry_unresolved(spec: dict, explanation: str) -> dict:
    return _result(
        spec["constraint_id"], "UNRESOLVED", evidence_ids=GEOMETRY_EVIDENCE_IDS,
        calculation="sandbox_geometry.evidence_gate.v1", explanation=explanation,
    )


def _containment(spec: dict, parcel: Polygon, objects: list[tuple[dict, Polygon]], metrics: dict) -> dict:
    selected = _selected_objects(objects, spec)
    failing = [item["id"] for item, footprint in selected if not parcel.covers(footprint)]
    return _result(
        "footprint_inside_parcel", "FAIL" if failing else "PASS", evidence_ids=GEOMETRY_EVIDENCE_IDS,
        calculation="sandbox_geometry.parcel_containment.v1", inputs={"object_ids": [item["id"] for item, _ in selected]},
        result={item["id"]: metrics[item["id"]]["minimum_boundary_distance_m"] for item, _ in selected}, units="m",
        explanation=(f"Footprint crosses the authoritative parcel boundary: {', '.join(failing)}." if failing else "All requested footprints are contained by the authoritative parcel geometry."),
    )


def _setback(spec: dict, parcel: Polygon, objects: list[tuple[dict, Polygon]], metrics: dict) -> dict:
    minimum = _number(spec.get("minimum_m"), "minimum_m", nonnegative=True)
    selected = _selected_objects(objects, spec)
    failing = [item["id"] for item, footprint in selected if not parcel.covers(footprint) or metrics[item["id"]]["minimum_boundary_distance_m"] < minimum]
    return _result(
        "minimum_setback", "FAIL" if failing else "PASS", evidence_ids=GEOMETRY_EVIDENCE_IDS,
        calculation="sandbox_geometry.boundary_distance.v1", inputs={"minimum_m": minimum, "object_ids": [item["id"] for item, _ in selected]},
        result={item["id"]: metrics[item["id"]]["minimum_boundary_distance_m"] for item, _ in selected}, units="m",
        explanation=(f"The required setback is not met: {', '.join(failing)}." if failing else "All requested footprints meet the minimum parcel-boundary setback."),
    )


def _area(spec: dict, _parcel: Polygon, objects: list[tuple[dict, Polygon]], metrics: dict) -> dict:
    selected = _selected_objects(objects, spec)
    minimum = spec.get("min_m2")
    maximum = spec.get("max_m2")
    minimum = _number(minimum, "min_m2", nonnegative=True) if minimum is not None else None
    maximum = _number(maximum, "max_m2", nonnegative=True) if maximum is not None else None
    if minimum is not None and maximum is not None and minimum > maximum:
        raise SceneValidationError("min_m2 cannot exceed max_m2.")
    failing = [item["id"] for item, _ in selected if (minimum is not None and metrics[item["id"]]["footprint_area_m2"] < minimum) or (maximum is not None and metrics[item["id"]]["footprint_area_m2"] > maximum)]
    return _result(
        "footprint_area", "FAIL" if failing else "PASS", calculation="sandbox_geometry.area.v1",
        inputs={"min_m2": minimum, "max_m2": maximum, "object_ids": [item["id"] for item, _ in selected]},
        result={item["id"]: metrics[item["id"]]["footprint_area_m2"] for item, _ in selected}, units="m2",
        explanation=(f"Footprint area predicate is not met: {', '.join(failing)}." if failing else "Footprint area is deterministically calculated from the proposed rectangle."),
    )


def _coverage(spec: dict, _parcel: Polygon, objects: list[tuple[dict, Polygon]], metrics: dict) -> dict:
    selected = _selected_objects(objects, spec)
    maximum = spec.get("max_percent")
    maximum = _number(maximum, "max_percent", nonnegative=True) if maximum is not None else None
    failing = [item["id"] for item, _ in selected if maximum is not None and metrics[item["id"]]["parcel_coverage_percent"] > maximum]
    return _result(
        "parcel_coverage", "FAIL" if failing else "PASS", evidence_ids=GEOMETRY_EVIDENCE_IDS,
        calculation="sandbox_geometry.coverage.v1", inputs={"max_percent": maximum, "object_ids": [item["id"] for item, _ in selected]},
        result={item["id"]: metrics[item["id"]]["parcel_coverage_percent"] for item, _ in selected}, units="percent",
        explanation=(f"Parcel coverage predicate is not met: {', '.join(failing)}." if failing else "Parcel coverage is derived from the authoritative parcel geometry and proposed footprint."),
    )


def _collision(spec: dict, _parcel: Polygon, objects: list[tuple[dict, Polygon]], _metrics: dict, scene_state: dict, origin: dict) -> dict:
    blocked = []
    for observed in scene_state.get("observed", []):
        if observed.get("kind") in {"blocked_geometry", "prohibited_geometry"}:
            blocked.append((observed.get("id", "blocked_geometry"), _to_local_geometry(observed["geometry"], origin)))
    collisions = []
    checked_pairs = set()
    selected = _selected_objects(objects, spec)
    for item, footprint in selected:
        for blocked_id, blocked_geometry in blocked:
            if footprint.intersects(blocked_geometry):
                collisions.append({"object_id": item["id"], "blocked_geometry_id": blocked_id})
        for other, other_footprint in objects:
            pair = tuple(sorted((item["id"], other["id"])))
            if item["id"] != other["id"] and pair not in checked_pairs and footprint.intersects(other_footprint):
                collisions.append({"object_id": item["id"], "blocked_geometry_id": other["id"]})
            checked_pairs.add(pair)
    return _result(
        "object_collision", "FAIL" if collisions else "PASS", evidence_ids=GEOMETRY_EVIDENCE_IDS,
        calculation="sandbox_geometry.collision.v1", inputs={"object_ids": [item["id"] for item, _ in selected], "blocked_geometry_count": len(blocked)},
        result=collisions, units=None,
        explanation=("A proposed footprint overlaps a declared blocked geometry or another proposed object." if collisions else "No requested footprint overlaps a declared blocked geometry or another proposed object."),
    )


def _unsupported(spec: dict, reason: str) -> dict:
    return _result(
        spec["constraint_id"], "UNRESOLVED", basis="OBSERVED", calculation="sandbox_evidence.scope_check.v1",
        explanation=reason,
    )


def evaluate_site(snapshot: dict, scene_state: dict, requested_constraints: list[dict]) -> dict:
    """Evaluate only predicates that the snapshot evidence and scene geometry can prove."""
    if not isinstance(requested_constraints, list) or not requested_constraints:
        raise SceneValidationError("At least one requested constraint is required.")
    origin = _frame(scene_state, snapshot)
    objects = _objects(scene_state)
    parcel = _to_local_geometry(snapshot.get("geometry", {}), origin)
    if parcel.geom_type not in {"Polygon", "MultiPolygon"} or parcel.area <= 0:
        raise SceneValidationError("SiteSnapshot does not contain a valid parcel polygon.")
    metrics = _metrics(parcel, objects)
    geometry_issue = _geometry_evidence_issue(snapshot)
    results = []
    geometry_handlers = {
        "footprint_inside_parcel": _containment,
        "minimum_setback": _setback,
        "footprint_area": _area,
        "parcel_coverage": _coverage,
        "object_collision": _collision,
    }
    for spec in requested_constraints:
        if not isinstance(spec, dict) or not isinstance(spec.get("constraint_id"), str):
            raise SceneValidationError("Each requested constraint needs a string constraint_id.")
        constraint_id = spec["constraint_id"]
        if constraint_id in geometry_handlers:
            if geometry_issue:
                results.append(_geometry_unresolved(spec, geometry_issue))
            elif constraint_id == "object_collision":
                results.append(_collision(spec, parcel, objects, metrics, scene_state, origin))
            else:
                results.append(geometry_handlers[constraint_id](spec, parcel, objects, metrics))
        elif constraint_id == "max_slope_degrees":
            results.append(_unsupported(spec, "slope_degrees is point-scoped evidence and cannot prove a parcel-wide or footprint-wide slope predicate."))
        elif constraint_id == "industrial_zoning":
            results.append(_unsupported(spec, "parcel_zoning has no jurisdiction-aware industrial-zoning mapping in this evaluator."))
        else:
            results.append(_unsupported(spec, "This constraint is not supported by the deterministic evaluator."))
    outcomes = {item["outcome"] for item in results}
    overall = "FAIL" if "FAIL" in outcomes else "UNRESOLVED" if "UNRESOLVED" in outcomes else "PASS"
    return {
        "evaluator_version": "site_sandbox_geometry_v1",
        "site_snapshot_id": snapshot["snapshot_id"],
        "overall_status": overall,
        "constraint_results": results,
        "derived_geometry_metrics": metrics,
    }
