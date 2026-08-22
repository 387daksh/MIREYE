"""Deterministic Site Sandbox geometry and evidence evaluation."""
from __future__ import annotations

import math
import time
from typing import Any

from shapely.geometry import Polygon, shape
from shapely.ops import transform

from app.sandbox import EARTH_RADIUS_M, SCENE_SCHEMA_VERSION, SITE_SNAPSHOT_FIELD_SCOPES


FRAME_VERSION = "local_tangent_plane_v1"
GEOMETRY_EVIDENCE_IDS = ("parcel_id", "parcel_boundary_geojson", "parcel_match_type", "parcel_match_distance_m")
EVIDENCE_EVALUATOR_VERSION = "sandbox_evidence.field_predicate.v1"


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
    if isinstance(value, bool):
        raise SceneValidationError(f"{name} must be numeric.")
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


def _geometry_evidence_issue(snapshot: dict, *, now: float | None = None) -> str | None:
    now = time.time() if now is None else now
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
        expires_at = record.get("expires_at")
        if expires_at is not None:
            try:
                if now >= float(expires_at):
                    return f"Required parcel evidence is stale: {evidence_id}."
            except (TypeError, ValueError):
                return f"Required parcel evidence has invalid freshness metadata: {evidence_id}."
    if snapshot.get("is_expired") and not all(evidence.get(field, {}).get("expires_at") is not None for field in GEOMETRY_EVIDENCE_IDS):
        return "The SiteSnapshot is stale; parcel geometry cannot be treated as current."
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


def _evidence_gate(snapshot: dict, fields: tuple[str, ...], scope: str, *, now: float, require_exact_parcel: bool = False) -> dict:
    gate = {"accepted": False, "evidence_ids": list(fields), "expected_scope": scope, "reason_code": None, "reason": None, "values": {}, "records": {}}

    def reject(code: str, reason: str) -> dict:
        gate.update(reason_code=code, reason=reason)
        return gate

    if require_exact_parcel:
        identity = snapshot.get("parcel_identity", {})
        if identity.get("parcel_match_type") != "exact_intersect" or identity.get("parcel_match_distance_m") != 0:
            return reject("parcel_identity_not_exact", "The SiteSnapshot does not prove an exact parcel identity.")
    evidence = snapshot.get("evidence")
    if not isinstance(evidence, dict):
        return reject("evidence_missing", "Required MIREYE evidence is missing.")
    for field in fields:
        record = evidence.get(field)
        if not isinstance(record, dict):
            return reject("field_missing", f"Required MIREYE evidence is missing: {field}.")
        if record.get("status") != "ok":
            return reject("status_unusable", f"Required MIREYE evidence has unusable status: {field}.")
        if record.get("value") is None:
            return reject("value_null", f"Required MIREYE evidence is null: {field}.")
        try:
            expires_at = float(record.get("expires_at"))
        except (TypeError, ValueError):
            return reject("freshness_missing", f"Required MIREYE evidence has no valid freshness metadata: {field}.")
        if not math.isfinite(expires_at) or expires_at <= now:
            return reject("field_stale", f"Required MIREYE evidence is stale: {field}.")
        actual_scope = str(record.get("scope") or SITE_SNAPSHOT_FIELD_SCOPES.get(field, "")).upper().replace("-", "_")
        if actual_scope != scope:
            return reject("scope_incompatible", f"Required MIREYE evidence has {actual_scope or 'unknown'} scope, not {scope} scope: {field}.")
        gate["values"][field] = record["value"]
        gate["records"][field] = {"status": record.get("status"), "scope": actual_scope, "expires_at": expires_at, "source": record.get("source")}
    gate.update(accepted=True, reason_code="accepted", reason="All required evidence records are fresh, usable, and scope-compatible.")
    return gate


def _gate_summary(gate: dict) -> dict:
    return {key: gate[key] for key in ("accepted", "evidence_ids", "expected_scope", "reason_code", "reason")}


def _evidence_unresolved(spec: dict, gate: dict, explanation: str | None = None) -> dict:
    return _result(
        spec["constraint_id"], "UNRESOLVED", basis="OBSERVED", evidence_ids=gate["evidence_ids"],
        calculation="sandbox_evidence.field_gate.v1", inputs={"evidence_gate": _gate_summary(gate)},
        result=None, explanation=explanation or gate["reason"],
    )


def _source_number(spec: dict, gate: dict, field: str, *, maximum: float | None = None) -> float | dict:
    value = gate["values"][field]
    if isinstance(value, bool):
        return _evidence_unresolved(spec, gate, f"MIREYE evidence is not numeric: {field}.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _evidence_unresolved(spec, gate, f"MIREYE evidence is not numeric: {field}.")
    if not math.isfinite(number) or number < 0 or (maximum is not None and number > maximum):
        return _evidence_unresolved(spec, gate, f"MIREYE evidence is outside the valid range: {field}.")
    return number


def _wetland_threshold(spec: dict, snapshot: dict, *, now: float, field: str, threshold_key: str, constraint_id: str, units: str, maximum: float | None = None) -> dict:
    gate = _evidence_gate(snapshot, (field,), "PARCEL", now=now, require_exact_parcel=True)
    if not gate["accepted"]:
        return _evidence_unresolved(spec, gate)
    threshold = _number(spec.get(threshold_key), threshold_key, nonnegative=True)
    if maximum is not None and threshold > maximum:
        raise SceneValidationError(f"{threshold_key} cannot exceed {maximum}.")
    value = _source_number(spec, gate, field, maximum=maximum)
    if isinstance(value, dict):
        return value
    failed = value > threshold
    return _result(
        constraint_id, "FAIL" if failed else "PASS", basis="OBSERVED", evidence_ids=(field,), calculation=EVIDENCE_EVALUATOR_VERSION,
        inputs={threshold_key: threshold, "evidence_gate": _gate_summary(gate)}, result=value, units=units,
        explanation=("Mapped NWI parcel overlap exceeds the requested threshold." if failed else "Mapped NWI parcel overlap meets the requested threshold.") + " Zero mapped overlap does not establish survey-grade wetland absence or USACE jurisdiction.",
    )


def _parcel_acreage_range(spec: dict, snapshot: dict, *, now: float) -> dict:
    field = "parcel_area_m2"
    gate = _evidence_gate(snapshot, (field,), "PARCEL", now=now, require_exact_parcel=True)
    if not gate["accepted"]:
        return _evidence_unresolved(spec, gate)
    minimum = _number(spec.get("min_acres"), "min_acres", nonnegative=True)
    maximum = _number(spec.get("max_acres"), "max_acres", nonnegative=True)
    if minimum > maximum:
        raise SceneValidationError("min_acres cannot exceed max_acres.")
    area_m2 = _source_number(spec, gate, field)
    if isinstance(area_m2, dict):
        return area_m2
    acres = area_m2 / 4046.8564224
    passed = minimum <= acres <= maximum
    return _result(
        "parcel_acreage_range", "PASS" if passed else "FAIL", basis="OBSERVED", evidence_ids=(field,),
        calculation=EVIDENCE_EVALUATOR_VERSION,
        inputs={"min_acres": minimum, "max_acres": maximum, "evidence_gate": _gate_summary(gate)},
        result=round(acres, 6), units="acres",
        explanation="The observed parcel area is within the requested acreage range." if passed else "The observed parcel area is outside the requested acreage range.",
    )


def _resolution_point_flood(spec: dict, snapshot: dict, *, now: float) -> dict:
    field = "within_floodplain_polygon"
    gate = _evidence_gate(snapshot, (field,), "POINT", now=now)
    if not gate["accepted"]:
        return _evidence_unresolved(spec, gate)
    value = gate["values"][field]
    if not isinstance(value, bool):
        return _evidence_unresolved(spec, gate, "MIREYE within_floodplain_polygon evidence is not boolean.")
    return _result(
        "resolution_point_outside_fema_sfha", "FAIL" if value else "PASS", basis="OBSERVED", evidence_ids=(field,), calculation=EVIDENCE_EVALUATOR_VERSION,
        inputs={"evidence_gate": _gate_summary(gate)}, result=value, units=None,
        explanation=("The resolution point is inside the mapped FEMA floodplain polygon." if value else "The resolution point is outside the mapped FEMA floodplain polygon.") + " This does not prove parcel-wide or footprint-wide flood exclusion.",
    )


def _resolution_point_slope(spec: dict, snapshot: dict, *, now: float) -> dict:
    field = "slope_degrees"
    gate = _evidence_gate(snapshot, (field,), "POINT", now=now)
    if not gate["accepted"]:
        return _evidence_unresolved(spec, gate)
    threshold = _number(spec.get("max_degrees"), "max_degrees", nonnegative=True)
    value = _source_number(spec, gate, field, maximum=90)
    if isinstance(value, dict):
        return value
    failed = value > threshold
    return _result(
        "max_resolution_point_slope_degrees", "FAIL" if failed else "PASS", basis="OBSERVED", evidence_ids=(field,), calculation=EVIDENCE_EVALUATOR_VERSION,
        inputs={"max_degrees": threshold, "evidence_gate": _gate_summary(gate)}, result=value, units="degrees",
        explanation=("The resolution-point slope exceeds the requested threshold." if failed else "The resolution-point slope meets the requested threshold.") + " This does not prove parcel-wide or footprint-wide slope.",
    )


def _normalized_code(value: str) -> str:
    return value.strip().casefold()


def _optional_evidence(snapshot: dict, field: str, *, now: float) -> tuple[Any, dict]:
    gate = _evidence_gate(snapshot, (field,), "NEAREST_FEATURE", now=now)
    return (gate["values"].get(field) if gate["accepted"] else None), gate


def _resolution_point_distance(spec: dict, snapshot: dict, *, now: float, constraint_id: str, distance_field: str, label: str, status_field: str | None = None, voltage_field: str | None = None) -> dict:
    gate = _evidence_gate(snapshot, (distance_field,), "NEAREST_FEATURE", now=now)
    if not gate["accepted"]:
        return _evidence_unresolved(spec, gate)
    threshold = _number(spec.get("max_distance_m"), "max_distance_m", nonnegative=True)
    distance = _source_number(spec, gate, distance_field)
    if isinstance(distance, dict):
        return distance
    evidence_ids = [distance_field]
    result = {"distance_m": distance}
    for field, key in ((status_field, "status"), (voltage_field, "voltage_kv")):
        if field:
            value, optional_gate = _optional_evidence(snapshot, field, now=now)
            if key == "status" and value is not None and (not isinstance(value, str) or not value.strip()):
                optional_gate.update(accepted=False, reason_code="value_invalid", reason=f"MIREYE evidence is not a usable raw status: {field}.")
                value = None
            if key == "voltage_kv" and value is not None:
                try:
                    value = float(value)
                    if not math.isfinite(value) or value < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    optional_gate.update(accepted=False, reason_code="value_invalid", reason=f"MIREYE evidence is not a valid voltage: {field}.")
                    value = None
            result[key] = value
            result[f"{key}_evidence"] = _gate_summary(optional_gate)
            if optional_gate["accepted"]:
                evidence_ids.append(field)
    required_statuses = spec.get("required_statuses")
    require_operational = spec.get("require_operational")
    if require_operational is not None and not isinstance(require_operational, bool):
        raise SceneValidationError("require_operational must be boolean or null.")
    if require_operational and not required_statuses:
        return _evidence_unresolved(spec, gate, "Operational status cannot be inferred without an explicit caller-supplied required_statuses allow-list.")
    status_failed = False
    if required_statuses is not None:
        if not isinstance(required_statuses, list) or not required_statuses or not all(isinstance(value, str) and value.strip() for value in required_statuses):
            raise SceneValidationError("required_statuses must be a non-empty list of raw status values.")
        if not status_field:
            raise SceneValidationError("This constraint does not support a status requirement.")
        status_gate = _evidence_gate(snapshot, (status_field,), "NEAREST_FEATURE", now=now)
        if not status_gate["accepted"]:
            return _evidence_unresolved(spec, status_gate)
        status = status_gate["values"][status_field]
        if not isinstance(status, str) or not status.strip():
            return _evidence_unresolved(spec, status_gate, f"MIREYE evidence is not a usable raw status: {status_field}.")
        status_failed = _normalized_code(status) not in {_normalized_code(value) for value in required_statuses}
    failed = distance > threshold or status_failed
    caveat = " This does not establish grid capacity or available MW." if label in {"substation", "transmission line"} else " This does not establish legal access, frontage, right-of-way, or heavy-haul suitability."
    return _result(
        constraint_id, "FAIL" if failed else "PASS", basis="OBSERVED", evidence_ids=evidence_ids, calculation=EVIDENCE_EVALUATOR_VERSION,
        inputs={"max_distance_m": threshold, "required_statuses": required_statuses, "require_operational": require_operational, "evidence_gate": _gate_summary(gate)},
        result=result, units="m", explanation=(f"The resolution-point distance or raw status requirement for the nearest {label} is not met." if failed else f"The resolution-point distance and any raw status requirement for the nearest {label} are met.") + caveat,
    )


def _zoning_allow_list(spec: dict, snapshot: dict, *, now: float) -> dict:
    field = "parcel_zoning"
    gate = _evidence_gate(snapshot, (field,), "PARCEL", now=now, require_exact_parcel=True)
    if not gate["accepted"]:
        return _evidence_unresolved(spec, gate)
    allowed_codes = spec.get("allowed_codes")
    if not isinstance(allowed_codes, list) or not allowed_codes or not all(isinstance(code, str) and code.strip() for code in allowed_codes):
        raise SceneValidationError("allowed_codes must be a non-empty list of raw zoning codes.")
    zoning = gate["values"][field]
    if not isinstance(zoning, str) or not zoning.strip():
        return _evidence_unresolved(spec, gate, "MIREYE parcel_zoning evidence is not an unambiguous raw zoning code.")
    normalized = _normalized_code(zoning)
    normalized_allowed = [_normalized_code(code) for code in allowed_codes]
    passed = normalized in normalized_allowed
    return _result(
        "parcel_zoning_code_in", "PASS" if passed else "FAIL", basis="OBSERVED", evidence_ids=(field,), calculation=EVIDENCE_EVALUATOR_VERSION,
        inputs={"allowed_codes": allowed_codes, "normalized_allowed_codes": normalized_allowed, "evidence_gate": _gate_summary(gate)},
        result={"raw_code": zoning, "normalized_code": normalized}, units=None,
        explanation=("The normalized raw parcel zoning code is in the caller-supplied allow-list." if passed else "The normalized raw parcel zoning code is not in the caller-supplied allow-list.") + " No industrial-use meaning is inferred.",
    )


def evaluate_site(snapshot: dict, scene_state: dict, requested_constraints: list[dict], *, now: float | None = None) -> dict:
    """Evaluate only predicates that the snapshot evidence and scene geometry can prove."""
    if not isinstance(requested_constraints, list) or not requested_constraints:
        raise SceneValidationError("At least one requested constraint is required.")
    origin = _frame(scene_state, snapshot)
    objects = _objects(scene_state)
    parcel = _to_local_geometry(snapshot.get("geometry", {}), origin)
    if parcel.geom_type not in {"Polygon", "MultiPolygon"} or parcel.area <= 0:
        raise SceneValidationError("SiteSnapshot does not contain a valid parcel polygon.")
    evaluation_time = time.time() if now is None else now
    metrics = _metrics(parcel, objects)
    geometry_issue = _geometry_evidence_issue(snapshot, now=evaluation_time)
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
        elif constraint_id == "parcel_acreage_range":
            results.append(_parcel_acreage_range(spec, snapshot, now=evaluation_time))
        elif constraint_id == "max_nwi_wetland_fraction_of_parcel":
            results.append(_wetland_threshold(spec, snapshot, now=evaluation_time, field="wetland_fraction_of_parcel", threshold_key="max_fraction", constraint_id=constraint_id, units="fraction", maximum=1))
        elif constraint_id == "max_nwi_wetland_acres_on_parcel":
            results.append(_wetland_threshold(spec, snapshot, now=evaluation_time, field="wetland_acres_on_parcel", threshold_key="max_acres", constraint_id=constraint_id, units="acres"))
        elif constraint_id == "resolution_point_outside_fema_sfha":
            results.append(_resolution_point_flood(spec, snapshot, now=evaluation_time))
        elif constraint_id == "max_resolution_point_slope_degrees":
            results.append(_resolution_point_slope(spec, snapshot, now=evaluation_time))
        elif constraint_id == "max_resolution_point_substation_distance_m":
            results.append(_resolution_point_distance(spec, snapshot, now=evaluation_time, constraint_id=constraint_id, distance_field="nearest_substation_distance_m", status_field="nearest_substation_status", voltage_field="nearest_substation_max_voltage_kv", label="substation"))
        elif constraint_id == "max_resolution_point_transmission_distance_m":
            results.append(_resolution_point_distance(spec, snapshot, now=evaluation_time, constraint_id=constraint_id, distance_field="nearest_transmission_line_distance_m", status_field="nearest_transmission_line_status", voltage_field="nearest_transmission_line_voltage_kv", label="transmission line"))
        elif constraint_id == "max_resolution_point_major_road_distance_m":
            results.append(_resolution_point_distance(spec, snapshot, now=evaluation_time, constraint_id=constraint_id, distance_field="nearest_major_road_distance_m", label="major road"))
        elif constraint_id == "parcel_zoning_code_in":
            results.append(_zoning_allow_list(spec, snapshot, now=evaluation_time))
        elif constraint_id in {"parcel_outside_fema_sfha", "footprint_outside_fema_sfha"}:
            results.append(_unsupported(spec, "Point-scoped FEMA evidence cannot prove whole-parcel or whole-footprint flood exclusion."))
        elif constraint_id in {"legal_access", "heavy_haul_suitability"}:
            results.append(_unsupported(spec, "Mapped-road proximity cannot prove legal access, frontage, right-of-way, or heavy-haul suitability."))
        elif constraint_id in {"substation_available_capacity_mw", "transmission_available_capacity_mw", "sufficient_grid_capacity"}:
            results.append(_unsupported(spec, "Proximity, voltage, status, and queue counts cannot prove available grid capacity or deliverability."))
        elif constraint_id in {"utilities_available", "utility_capacity"}:
            results.append(_unsupported(spec, "The current evidence does not directly and authoritatively prove utility availability or capacity."))
        else:
            results.append(_unsupported(spec, "This constraint is not supported by the deterministic evaluator."))
    outcomes = {item["outcome"] for item in results}
    overall = "FAIL" if "FAIL" in outcomes else "UNRESOLVED" if "UNRESOLVED" in outcomes else "PASS"
    return {
        "evaluator_version": "site_sandbox_evidence_v1",
        "site_snapshot_id": snapshot["snapshot_id"],
        "overall_status": overall,
        "constraint_results": results,
        "derived_geometry_metrics": metrics,
    }
