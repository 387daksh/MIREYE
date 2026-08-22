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


def phase6_snapshot():
    site = snapshot()
    values = {
        "wetland_acres_on_parcel": (1.5, "PARCEL"),
        "wetland_fraction_of_parcel": (0.1, "PARCEL"),
        "within_floodplain_polygon": (False, "POINT"),
        "fema_flood_zone": ("X", "POINT"),
        "slope_degrees": (3.0, "POINT"),
        "nearest_substation_distance_m": (800.0, "NEAREST_FEATURE"),
        "nearest_substation_status": ("IN SERVICE", "NEAREST_FEATURE"),
        "nearest_substation_max_voltage_kv": (345.0, "NEAREST_FEATURE"),
        "nearest_transmission_line_distance_m": (1200.0, "NEAREST_FEATURE"),
        "nearest_transmission_line_status": ("ACTIVE", "NEAREST_FEATURE"),
        "nearest_transmission_line_voltage_kv": (230.0, "NEAREST_FEATURE"),
        "nearest_major_road_distance_m": (300.0, "NEAREST_FEATURE"),
        "parcel_zoning": ("I-2", "PARCEL"),
    }
    site["evidence"].update({
        name: {"value": value, "status": "ok", "scope": scope, "observed_at": 1.0, "expires_at": 200.0}
        for name, (value, scope) in values.items()
    })
    return site


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


def evaluate_one(site, constraint):
    return result(evaluate_site(site, scene(), [constraint], now=100), constraint["constraint_id"])


PHASE6_EVIDENCE_CASES = [
    ("within_floodplain_polygon", {"constraint_id": "resolution_point_outside_fema_sfha"}, "not-boolean"),
    ("wetland_fraction_of_parcel", {"constraint_id": "max_nwi_wetland_fraction_of_parcel", "max_fraction": 0.2}, "not-numeric"),
    ("wetland_acres_on_parcel", {"constraint_id": "max_nwi_wetland_acres_on_parcel", "max_acres": 2.0}, "not-numeric"),
    ("slope_degrees", {"constraint_id": "max_resolution_point_slope_degrees", "max_degrees": 5}, "not-numeric"),
    ("nearest_substation_distance_m", {"constraint_id": "max_resolution_point_substation_distance_m", "max_distance_m": 1000}, "not-numeric"),
    ("nearest_transmission_line_distance_m", {"constraint_id": "max_resolution_point_transmission_distance_m", "max_distance_m": 1500}, "not-numeric"),
    ("nearest_major_road_distance_m", {"constraint_id": "max_resolution_point_major_road_distance_m", "max_distance_m": 500}, "not-numeric"),
    ("parcel_zoning", {"constraint_id": "parcel_zoning_code_in", "allowed_codes": ["I-2"]}, ["I-2"]),
]


def test_fema_resolution_point_pass_fail_and_parcel_request_unresolved():
    outside = phase6_snapshot()
    inside = phase6_snapshot()
    inside["evidence"]["within_floodplain_polygon"]["value"] = True

    assert evaluate_one(outside, {"constraint_id": "resolution_point_outside_fema_sfha"})["outcome"] == "PASS"
    assert evaluate_one(inside, {"constraint_id": "resolution_point_outside_fema_sfha"})["outcome"] == "FAIL"
    parcel = evaluate_one(outside, {"constraint_id": "parcel_outside_fema_sfha"})
    assert parcel["outcome"] == "UNRESOLVED"
    assert "whole-parcel" in parcel["explanation"]


@pytest.mark.parametrize("constraint_id, threshold_key, passing, failing", [
    ("max_nwi_wetland_fraction_of_parcel", "max_fraction", 0.2, 0.05),
    ("max_nwi_wetland_acres_on_parcel", "max_acres", 2.0, 1.0),
])
def test_nwi_wetland_thresholds_pass_fail_and_state_limits(constraint_id, threshold_key, passing, failing):
    site = phase6_snapshot()
    passed = evaluate_one(site, {"constraint_id": constraint_id, threshold_key: passing})
    failed = evaluate_one(site, {"constraint_id": constraint_id, threshold_key: failing})

    assert passed["outcome"] == "PASS"
    assert failed["outcome"] == "FAIL"
    assert "USACE jurisdiction" in passed["explanation"]

    missing = phase6_snapshot()
    del missing["evidence"]["wetland_fraction_of_parcel" if "fraction" in constraint_id else "wetland_acres_on_parcel"]
    assert evaluate_one(missing, {"constraint_id": constraint_id, threshold_key: passing})["outcome"] == "UNRESOLVED"


def test_resolution_point_slope_pass_fail_and_parcel_semantic_unresolved():
    site = phase6_snapshot()
    assert evaluate_one(site, {"constraint_id": "max_resolution_point_slope_degrees", "max_degrees": 5})["outcome"] == "PASS"
    assert evaluate_one(site, {"constraint_id": "max_resolution_point_slope_degrees", "max_degrees": 2})["outcome"] == "FAIL"
    assert evaluate_one(site, {"constraint_id": "max_slope_degrees", "max_degrees": 5})["outcome"] == "UNRESOLVED"


def test_substation_distance_status_and_capacity_semantics():
    site = phase6_snapshot()
    passed = evaluate_one(site, {"constraint_id": "max_resolution_point_substation_distance_m", "max_distance_m": 1000, "require_operational": True, "required_statuses": ["in service"]})
    failed = evaluate_one(site, {"constraint_id": "max_resolution_point_substation_distance_m", "max_distance_m": 500})
    status_failed = evaluate_one(site, {"constraint_id": "max_resolution_point_substation_distance_m", "max_distance_m": 1000, "required_statuses": ["RETIRED"]})
    unresolved_status = evaluate_one(site, {"constraint_id": "max_resolution_point_substation_distance_m", "max_distance_m": 1000, "require_operational": True})
    capacity = evaluate_one(site, {"constraint_id": "substation_available_capacity_mw"})

    assert passed["outcome"] == "PASS"
    assert passed["result"]["status"] == "IN SERVICE"
    assert passed["result"]["voltage_kv"] == 345.0
    assert failed["outcome"] == "FAIL"
    assert status_failed["outcome"] == "FAIL"
    assert unresolved_status["outcome"] == "UNRESOLVED"
    assert capacity["outcome"] == "UNRESOLVED"

    site["evidence"]["nearest_substation_status"]["expires_at"] = 99.0
    stale_status = evaluate_one(site, {"constraint_id": "max_resolution_point_substation_distance_m", "max_distance_m": 1000, "required_statuses": ["IN SERVICE"]})
    assert stale_status["outcome"] == "UNRESOLVED"


def test_transmission_distance_status_and_capacity_semantics():
    site = phase6_snapshot()
    passed = evaluate_one(site, {"constraint_id": "max_resolution_point_transmission_distance_m", "max_distance_m": 1500, "required_statuses": ["active"]})
    failed = evaluate_one(site, {"constraint_id": "max_resolution_point_transmission_distance_m", "max_distance_m": 1000})
    capacity = evaluate_one(site, {"constraint_id": "transmission_available_capacity_mw"})

    assert passed["outcome"] == "PASS"
    assert passed["result"]["voltage_kv"] == 230.0
    assert failed["outcome"] == "FAIL"
    assert capacity["outcome"] == "UNRESOLVED"

    site["evidence"]["nearest_transmission_line_voltage_kv"]["expires_at"] = 99.0
    distance_only = evaluate_one(site, {"constraint_id": "max_resolution_point_transmission_distance_m", "max_distance_m": 1500})
    assert distance_only["outcome"] == "PASS"
    assert distance_only["result"]["voltage_kv"] is None


def test_major_road_distance_and_legal_access_semantics():
    site = phase6_snapshot()
    assert evaluate_one(site, {"constraint_id": "max_resolution_point_major_road_distance_m", "max_distance_m": 500})["outcome"] == "PASS"
    assert evaluate_one(site, {"constraint_id": "max_resolution_point_major_road_distance_m", "max_distance_m": 200})["outcome"] == "FAIL"
    legal = evaluate_one(site, {"constraint_id": "legal_access"})
    assert legal["outcome"] == "UNRESOLVED"
    assert "legal access" in legal["explanation"]


def test_raw_zoning_code_allow_list_pass_fail_and_generic_industrial_unresolved():
    site = phase6_snapshot()
    passed = evaluate_one(site, {"constraint_id": "parcel_zoning_code_in", "allowed_codes": [" i-2 ", "M-1"]})
    failed = evaluate_one(site, {"constraint_id": "parcel_zoning_code_in", "allowed_codes": ["I-1"]})

    assert passed["outcome"] == "PASS"
    assert passed["result"]["normalized_code"] == "i-2"
    assert failed["outcome"] == "FAIL"
    assert evaluate_one(site, {"constraint_id": "industrial_zoning"})["outcome"] == "UNRESOLVED"


@pytest.mark.parametrize("field, constraint, _invalid", PHASE6_EVIDENCE_CASES)
def test_each_phase6_predicate_rejects_stale_evidence(field, constraint, _invalid):
    site = phase6_snapshot()
    site["evidence"][field]["expires_at"] = 99.0
    assert evaluate_one(site, constraint)["outcome"] == "UNRESOLVED"


@pytest.mark.parametrize("field, constraint, _invalid", PHASE6_EVIDENCE_CASES)
def test_each_phase6_predicate_rejects_null_evidence(field, constraint, _invalid):
    site = phase6_snapshot()
    site["evidence"][field]["value"] = None
    assert evaluate_one(site, constraint)["outcome"] == "UNRESOLVED"


@pytest.mark.parametrize("field, constraint, _invalid", PHASE6_EVIDENCE_CASES)
def test_each_phase6_predicate_rejects_wrong_scope(field, constraint, _invalid):
    site = phase6_snapshot()
    site["evidence"][field]["scope"] = "REGION"
    assert evaluate_one(site, constraint)["outcome"] == "UNRESOLVED"


@pytest.mark.parametrize("field, constraint, invalid", PHASE6_EVIDENCE_CASES)
def test_each_phase6_predicate_handles_invalid_source_values_safely(field, constraint, invalid):
    site = phase6_snapshot()
    site["evidence"][field]["value"] = invalid
    item = evaluate_one(site, constraint)

    assert item["outcome"] == "UNRESOLVED"
    assert item["evidence_ids"]


def test_field_freshness_is_independent():
    site = phase6_snapshot()
    site["evidence"]["slope_degrees"]["expires_at"] = 99.0
    evaluation = evaluate_site(site, scene(), [
        {"constraint_id": "max_resolution_point_slope_degrees", "max_degrees": 5},
        {"constraint_id": "max_resolution_point_major_road_distance_m", "max_distance_m": 500},
    ], now=100)

    assert result(evaluation, "max_resolution_point_slope_degrees")["outcome"] == "UNRESOLVED"
    assert result(evaluation, "max_resolution_point_major_road_distance_m")["outcome"] == "PASS"


def test_parcel_scoped_constraints_require_exact_identity():
    site = phase6_snapshot()
    site["parcel_identity"]["parcel_match_type"] = "nearest_within_radius"
    site["parcel_identity"]["parcel_match_distance_m"] = 10.0

    assert evaluate_one(site, {"constraint_id": "max_nwi_wetland_fraction_of_parcel", "max_fraction": 0.2})["outcome"] == "UNRESOLVED"
    assert evaluate_one(site, {"constraint_id": "parcel_zoning_code_in", "allowed_codes": ["I-2"]})["outcome"] == "UNRESOLVED"


def test_unusable_status_and_invalid_typed_input_fail_safely():
    site = phase6_snapshot()
    site["evidence"]["nearest_substation_distance_m"]["status"] = "error"
    assert evaluate_one(site, {"constraint_id": "max_resolution_point_substation_distance_m", "max_distance_m": 1000})["outcome"] == "UNRESOLVED"

    with pytest.raises(SceneValidationError, match="max_distance_m must be numeric"):
        evaluate_one(phase6_snapshot(), {"constraint_id": "max_resolution_point_substation_distance_m", "max_distance_m": True})


def test_generic_utility_and_grid_claims_remain_unresolved():
    site = phase6_snapshot()
    assert evaluate_one(site, {"constraint_id": "utilities_available"})["outcome"] == "UNRESOLVED"
    assert evaluate_one(site, {"constraint_id": "sufficient_grid_capacity"})["outcome"] == "UNRESOLVED"
