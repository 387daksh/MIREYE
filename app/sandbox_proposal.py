"""Deterministic parcel-derived placement for conceptual sandbox proposals.

Strategy ``parcel_inset_bess_v1``:
1. Project the authoritative parcel into the scene's local-meter frame.
2. Inset it by the requested minimum setback.
3. Try the requested rotation, or deterministic parcel-aligned orientations.
4. Test central points followed by a stable 21x21 grid.
5. If dimensions were omitted, uniformly scale the conceptual BESS planning
   envelope from 100% to 25%; explicit dimensions are never reduced.
6. Accept a candidate only after the unchanged deterministic evaluator passes
   containment and setback.
"""
from __future__ import annotations

import copy
import math
from typing import Any

from shapely.geometry import MultiPolygon, Polygon

from app.sandbox import SandboxError, conceptual_bess_facility
from app.sandbox_evaluator import (
    SceneValidationError,
    _geometry_evidence_issue,
    _to_local_geometry,
    build_oriented_footprint,
    evaluate_site,
)


PLACEMENT_STRATEGY = "parcel_inset_bess_v1"
DEFAULT_PHASE_POWER_MW = 100.0
DEFAULT_PHASE_ENERGY_MWH = 400.0
DEFAULT_DURATION_HOURS = 4.0
DEFAULT_EXPANSION_POWER_MW = 300.0
DEFAULT_EXPANSION_ENERGY_MWH = 1200.0
DEFAULT_WIDTH_M = 410.792
DEFAULT_LENGTH_M = 547.723
DEFAULT_HEIGHT_M = 4.0
DEFAULT_MINIMUM_SETBACK_M = 10.0
MINIMUM_DEFAULT_SCALE = 0.25
GRID_STEPS = 20


def _number(value: Any, name: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SceneValidationError(f"{name} must be numeric.") from exc
    if not math.isfinite(number) or (positive and number <= 0) or (nonnegative and number < 0):
        qualifier = "positive" if positive else "non-negative" if nonnegative else "finite"
        raise SceneValidationError(f"{name} must be a {qualifier} finite number.")
    return number


def _components(geometry: Polygon | MultiPolygon) -> list[Polygon]:
    polygons = [geometry] if geometry.geom_type == "Polygon" else list(geometry.geoms)
    return sorted(polygons, key=lambda item: (-item.area, tuple(round(value, 6) for value in item.bounds)))


def _orientations(region: Polygon | MultiPolygon, requested_rotation: float | None) -> list[float]:
    if requested_rotation is not None:
        return [requested_rotation % 360]
    values = []
    for component in _components(region):
        coordinates = list(component.minimum_rotated_rectangle.exterior.coords)
        for first, second in zip(coordinates, coordinates[1:]):
            angle = math.degrees(math.atan2(second[1] - first[1], second[0] - first[0])) % 180
            values.extend((angle, (angle + 90) % 180))
    values.extend((0.0, 90.0))
    unique = []
    for value in values:
        normalized = round(value % 180, 6)
        if normalized not in unique:
            unique.append(normalized)
    return unique


def _candidate_centers(region: Polygon | MultiPolygon, explicit_position: list[float] | None) -> list[list[float]]:
    if explicit_position is not None:
        return [explicit_position]
    candidates = []
    for component in _components(region):
        center = component.centroid
        representative = component.representative_point()
        points = [(center.x, center.y), (representative.x, representative.y)]
        min_x, min_y, max_x, max_y = component.bounds
        for row in range(GRID_STEPS + 1):
            y = min_y + (max_y - min_y) * row / GRID_STEPS
            for column in range(GRID_STEPS + 1):
                x = min_x + (max_x - min_x) * column / GRID_STEPS
                points.append((x, y))
        points.sort(key=lambda point: (round((point[0] - center.x) ** 2 + (point[1] - center.y) ** 2, 6), round(point[1], 6), round(point[0], 6)))
        for x, y in points:
            candidate = [round(x, 3), round(y, 3)]
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _scales(dimensions_explicit: bool) -> list[float]:
    if dimensions_explicit:
        return [1.0]
    steps = round((1.0 - MINIMUM_DEFAULT_SCALE) / 0.05)
    return [round(1.0 - index * 0.05, 2) for index in range(steps + 1)]


def generate_bess_proposal(
    snapshot: dict,
    scene_state: dict,
    *,
    power_mw: Any,
    energy_mwh: Any,
    duration_hours: Any,
    expansion_power_mw: Any = DEFAULT_EXPANSION_POWER_MW,
    expansion_energy_mwh: Any = DEFAULT_EXPANSION_ENERGY_MWH,
    width_m: Any = None,
    length_m: Any = None,
    height_m: Any = None,
    position: dict | None = None,
    rotation_deg: Any = None,
    minimum_setback_m: Any = None,
    elements: list[str] | None = None,
) -> dict:
    """Return a validated candidate scene or an explicit unresolved/impossible result."""
    evidence_issue = _geometry_evidence_issue(snapshot)
    if evidence_issue:
        return {"status": "UNRESOLVED", "strategy": PLACEMENT_STRATEGY, "reason": evidence_issue}

    power = _number(power_mw, "power_mw", positive=True)
    energy = _number(energy_mwh, "energy_mwh", positive=True)
    duration = _number(duration_hours, "duration_hours", positive=True)
    expansion_power = _number(expansion_power_mw, "expansion_power_mw", positive=True)
    expansion_energy = _number(expansion_energy_mwh, "expansion_energy_mwh", positive=True)
    if not math.isclose(energy, power * duration) or not math.isclose(expansion_energy, expansion_power * duration):
        raise SceneValidationError("BESS energy_mwh must equal power_mw multiplied by duration_hours for both phases.")
    width = _number(width_m if width_m is not None else DEFAULT_WIDTH_M, "width_m", positive=True)
    length = _number(length_m if length_m is not None else DEFAULT_LENGTH_M, "length_m", positive=True)
    height = _number(height_m if height_m is not None else DEFAULT_HEIGHT_M, "height_m", positive=True)
    setback = _number(minimum_setback_m if minimum_setback_m is not None else DEFAULT_MINIMUM_SETBACK_M, "minimum_setback_m", nonnegative=True)
    rotation = _number(rotation_deg, "rotation_deg") if rotation_deg is not None else None
    explicit_position = None
    if position is not None:
        if not isinstance(position, dict) or set(position) != {"x_m", "y_m"}:
            raise SceneValidationError("position must contain exactly x_m and y_m.")
        explicit_position = [_number(position["x_m"], "position.x_m"), _number(position["y_m"], "position.y_m")]

    origin = scene_state.get("frame", {}).get("origin")
    if not isinstance(origin, dict):
        raise SceneValidationError("Scene state has no local frame origin.")
    parcel = _to_local_geometry(snapshot.get("geometry", {}), origin)
    feasible_region = parcel.buffer(-setback, join_style=2)
    if feasible_region.is_empty or feasible_region.area <= 0:
        return {
            "status": "IMPOSSIBLE",
            "strategy": PLACEMENT_STRATEGY,
            "reason": "The parcel has no usable area after applying the minimum setback.",
            "minimum_setback_m": setback,
        }

    dimensions_explicit = width_m is not None or length_m is not None
    centers = _candidate_centers(feasible_region, explicit_position)
    for scale in _scales(dimensions_explicit):
        placed_width, placed_length = round(width * scale, 6), round(length * scale, 6)
        for angle in _orientations(feasible_region, rotation):
            for center in centers:
                try:
                    candidate = conceptual_bess_facility(
                        center_xy_m=center,
                        width_m=placed_width,
                        length_m=placed_length,
                        height_m=height,
                        rotation_deg=angle,
                        power_mw=power,
                        energy_mwh=energy,
                        duration_hours=duration,
                        expansion_power_mw=expansion_power,
                        expansion_energy_mwh=expansion_energy,
                        elements=elements,
                    )
                except SandboxError as exc:
                    raise SceneValidationError(str(exc)) from exc
                if not feasible_region.covers(build_oriented_footprint(candidate)):
                    continue
                candidate_scene = copy.deepcopy(scene_state)
                candidate_scene["proposed"] = [item for item in candidate_scene.get("proposed", []) if item.get("id") != candidate["id"]]
                candidate_scene["proposed"].append(candidate)
                evaluation = evaluate_site(snapshot, candidate_scene, [
                    {"constraint_id": "footprint_inside_parcel", "object_id": candidate["id"]},
                    {"constraint_id": "minimum_setback", "object_id": candidate["id"], "minimum_m": setback},
                ])
                if evaluation["overall_status"] != "PASS":
                    return {
                        "status": "UNRESOLVED" if evaluation["overall_status"] == "UNRESOLVED" else "IMPOSSIBLE",
                        "strategy": PLACEMENT_STRATEGY,
                        "reason": "The deterministic evaluator did not validate the generated candidate.",
                        "evaluation": evaluation,
                    }
                return {
                    "status": "PLACED" if scale == 1.0 else "ADJUSTED",
                    "strategy": PLACEMENT_STRATEGY,
                    "object_id": candidate["id"],
                    "minimum_setback_m": setback,
                    "requested_dimensions_m": {"width": width, "length": length, "height": height},
                    "placed_dimensions_m": {"width": placed_width, "length": placed_length, "height": height},
                    "scale_factor": scale,
                    "scene_state": candidate_scene,
                    "evaluation": evaluation,
                }

    return {
        "status": "IMPOSSIBLE",
        "strategy": PLACEMENT_STRATEGY,
        "reason": "No validated placement exists for the requested dimensions and minimum setback.",
        "minimum_setback_m": setback,
        "requested_dimensions_m": {"width": width, "length": length, "height": height},
    }
