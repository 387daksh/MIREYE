import copy

import pytest

from app.sandbox_evaluator import SceneValidationError, build_oriented_footprint, evaluate_site


def snapshot():
    return {
        "snapshot_id": "site-test",
        "is_expired": False,
        "parcel_identity": {
            "parcel_match_type": "exact_intersect",
            "parcel_match_distance_m": 0.0,
            "selected_point": {"lat": 0.0, "lng": 0.0},
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-0.005, -0.005], [0.005, -0.005], [0.005, 0.005], [-0.005, 0.005], [-0.005, -0.005]]],
        },
        "evidence": {
            name: {"value": value, "status": "ok"}
            for name, value in {
                "parcel_id": "parcel-1",
                "parcel_boundary_geojson": "authoritative-boundary",
                "parcel_match_type": "exact_intersect",
                "parcel_match_distance_m": 0.0,
                "slope_degrees": 2.0,
            }.items()
        },
    }


def scene(*, center=(0.0, 0.0), width=100.0, length=200.0, rotation=0.0):
    return {
        "schema_version": "1",
        "site_snapshot_id": "site-test",
        "frame": {"origin": {"lat": 0.0, "lng": 0.0}, "coordinate_frame_version": "local_tangent_plane_v1"},
        "observed": [],
        "proposed": [{
            "id": "data_center_1",
            "geometry_local": {"shape": "oriented_rectangle", "center_xy_m": list(center), "width_m": width, "length_m": length, "height_m": 28.0, "rotation_deg": rotation},
            "attributes": {"capacity_mw": 100.0},
        }],
        "camera": {},
    }


def result(evaluation, constraint_id):
    return next(item for item in evaluation["constraint_results"] if item["constraint_id"] == constraint_id)


def test_valid_footprint_passes_containment_and_metrics_are_deterministic():
    evaluation = evaluate_site(snapshot(), scene(), [
        {"constraint_id": "footprint_inside_parcel"},
        {"constraint_id": "minimum_setback", "minimum_m": 100},
        {"constraint_id": "footprint_area"},
        {"constraint_id": "parcel_coverage", "max_percent": 20},
    ])

    metrics = evaluation["derived_geometry_metrics"]["data_center_1"]
    assert evaluation["overall_status"] == "PASS"
    assert metrics["footprint_area_m2"] == 20000.0
    assert 0 < metrics["parcel_coverage_percent"] < 20
    assert result(evaluation, "footprint_inside_parcel")["outcome"] == "PASS"
    assert result(evaluation, "minimum_setback")["outcome"] == "PASS"


def test_crossing_boundary_and_setback_fail():
    crossing = evaluate_site(snapshot(), scene(center=(540, 0), width=100), [{"constraint_id": "footprint_inside_parcel"}])
    setback = evaluate_site(snapshot(), scene(center=(480, 0), width=100), [{"constraint_id": "minimum_setback", "minimum_m": 50}])

    assert crossing["overall_status"] == "FAIL"
    assert setback["overall_status"] == "FAIL"


def test_rotation_and_resize_change_footprint_deterministically():
    base = build_oriented_footprint(scene()["proposed"][0])
    rotated = build_oriented_footprint(scene(rotation=90)["proposed"][0])
    resized = build_oriented_footprint(scene(width=200)["proposed"][0])

    assert list(base.exterior.coords) != list(rotated.exterior.coords)
    assert rotated.area == base.area
    assert resized.area == base.area * 2


def test_collision_with_declared_blocked_geometry_fails():
    state = scene()
    state["observed"].append({
        "id": "wetland-1",
        "kind": "blocked_geometry",
        "origin": "OBSERVED",
        "geometry": {"type": "Polygon", "coordinates": [[[-0.0001, -0.0001], [0.0001, -0.0001], [0.0001, 0.0001], [-0.0001, 0.0001], [-0.0001, -0.0001]]]},
    })

    evaluation = evaluate_site(snapshot(), state, [{"constraint_id": "object_collision"}])
    assert evaluation["overall_status"] == "FAIL"


def test_degenerate_geometry_is_rejected():
    with pytest.raises(SceneValidationError, match="width_m"):
        evaluate_site(snapshot(), scene(width=0), [{"constraint_id": "footprint_inside_parcel"}])


def test_stale_or_missing_parcel_evidence_is_unresolved():
    stale = snapshot()
    stale["is_expired"] = True
    missing = snapshot()
    del missing["evidence"]["parcel_boundary_geojson"]

    assert result(evaluate_site(stale, scene(), [{"constraint_id": "footprint_inside_parcel"}]), "footprint_inside_parcel")["outcome"] == "UNRESOLVED"
    assert result(evaluate_site(missing, scene(), [{"constraint_id": "footprint_inside_parcel"}]), "footprint_inside_parcel")["outcome"] == "UNRESOLVED"


def test_point_scoped_slope_cannot_pass_a_footprint_constraint():
    evaluation = evaluate_site(snapshot(), scene(), [{"constraint_id": "max_slope_degrees", "max_degrees": 5}])

    assert evaluation["overall_status"] == "UNRESOLVED"
    assert "point-scoped" in result(evaluation, "max_slope_degrees")["explanation"]


def test_identical_inputs_produce_identical_evaluation_output():
    constraints = [{"constraint_id": "footprint_area"}, {"constraint_id": "parcel_coverage", "max_percent": 20}]
    first = evaluate_site(snapshot(), scene(), constraints)
    second = evaluate_site(copy.deepcopy(snapshot()), copy.deepcopy(scene()), copy.deepcopy(constraints))

    assert first == second
