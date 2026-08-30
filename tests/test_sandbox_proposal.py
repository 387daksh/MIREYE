import copy
import math

from app.sandbox import EARTH_RADIUS_M, scene_state_from_snapshot
from app.sandbox_evaluator import evaluate_site
from app.sandbox_proposal import PLACEMENT_STRATEGY, generate_bess_proposal


def snapshot(width_m, length_m):
    half_lng = math.degrees((width_m / 2) / EARTH_RADIUS_M)
    half_lat = math.degrees((length_m / 2) / EARTH_RADIUS_M)
    return {
        "snapshot_id": f"site-{width_m}-{length_m}",
        "is_expired": False,
        "parcel_identity": {
            "parcel_id": "parcel-1",
            "parcel_match_type": "exact_intersect",
            "parcel_match_distance_m": 0.0,
            "selected_point": {"lat": 0.0, "lng": 0.0},
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-half_lng, -half_lat], [half_lng, -half_lat],
                [half_lng, half_lat], [-half_lng, half_lat],
                [-half_lng, -half_lat],
            ]],
        },
        "evidence": {
            name: {"value": value, "status": "ok"}
            for name, value in {
                "parcel_id": "parcel-1",
                "parcel_boundary_geojson": "authoritative-boundary",
                "parcel_match_type": "exact_intersect",
                "parcel_match_distance_m": 0.0,
            }.items()
        },
    }


def empty_scene(site):
    scene = scene_state_from_snapshot(site)
    scene["proposed"] = []
    return scene


def propose(site, **overrides):
    arguments = {
        "power_mw": 100,
        "energy_mwh": 400,
        "duration_hours": 4,
        "expansion_power_mw": 300,
        "expansion_energy_mwh": 1200,
        "width_m": None,
        "length_m": None,
        "height_m": None,
        "position": None,
        "rotation_deg": None,
        "minimum_setback_m": 10,
    }
    arguments.update(overrides)
    return generate_bess_proposal(site, empty_scene(site), **arguments)


def test_default_footprint_fits_with_parcel_derived_placement():
    result = propose(snapshot(800, 800))

    assert result["status"] == "PLACED"
    assert result["strategy"] == PLACEMENT_STRATEGY
    assert result["scale_factor"] == 1.0
    assert result["evaluation"]["overall_status"] == "PASS"
    assert result["scene_state"]["proposed"][0]["attributes"]["capacity_mw"] == 100


def test_default_proposal_is_a_deterministic_semantic_bess_facility():
    result = propose(snapshot(800, 800))
    facility = result["scene_state"]["proposed"][0]
    components = {item["id"]: item for item in facility["components"]}

    assert facility["kind"] == "bess_facility"
    assert facility["attributes"]["power_mw"] == 100
    assert facility["attributes"]["energy_mwh"] == 400
    assert facility["attributes"]["duration_hours"] == 4
    assert facility["attributes"]["expansion_power_mw"] == 300
    assert facility["attributes"]["expansion_energy_mwh"] == 1200
    assert facility["assumption_profile"] == "conceptual_bess_100mw_400mwh_v1"
    assert set(components) == {
        "battery_enclosure_a", "battery_enclosure_b", "inverter_pcs_a", "inverter_pcs_b", "point_of_interconnection",
        "service_area", "internal_access", "expansion_reserve",
    }
    assert sum(components[item]["attributes"]["power_mw"] for item in ("battery_enclosure_a", "battery_enclosure_b")) == 100
    assert sum(components[item]["attributes"]["energy_mwh"] for item in ("battery_enclosure_a", "battery_enclosure_b")) == 400
    assert sum(components[item]["attributes"]["power_mw"] for item in ("inverter_pcs_a", "inverter_pcs_b")) == 100
    assert components["expansion_reserve"]["attributes"]["power_mw"] == 200
    assert components["expansion_reserve"]["attributes"]["energy_mwh"] == 800
    assert result == propose(snapshot(800, 800))


def test_default_footprint_is_uniformly_reduced_when_needed():
    result = propose(snapshot(260, 340))

    assert result["status"] == "ADJUSTED"
    assert 0.25 <= result["scale_factor"] < 1.0
    assert result["placed_dimensions_m"]["width"] < result["requested_dimensions_m"]["width"]
    assert result["evaluation"]["overall_status"] == "PASS"
    assert result["scene_state"]["proposed"][0]["attributes"]["capacity_mw"] == 100


def test_parcel_too_small_for_explicit_dimensions_is_impossible():
    site = snapshot(200, 200)
    scene = empty_scene(site)
    result = generate_bess_proposal(
        site,
        scene,
        power_mw=100,
        energy_mwh=400,
        duration_hours=4,
        width_m=250,
        length_m=350,
        minimum_setback_m=10,
    )

    assert result["status"] == "IMPOSSIBLE"
    assert "scene_state" not in result
    assert scene["proposed"] == []


def test_identical_parcel_and_request_produce_identical_placement():
    site = snapshot(500, 600)
    first = propose(site)
    second = generate_bess_proposal(
        copy.deepcopy(site), empty_scene(copy.deepcopy(site)),
        power_mw=100, energy_mwh=400, duration_hours=4, minimum_setback_m=10,
    )

    assert first == second


def test_existing_evaluator_still_rejects_crossing_footprint():
    site = snapshot(500, 600)
    result = propose(site)
    scene = result["scene_state"]
    scene["proposed"][0]["geometry_local"]["center_xy_m"] = [400, 0]

    evaluation = evaluate_site(site, scene, [{"constraint_id": "footprint_inside_parcel"}])
    assert evaluation["overall_status"] == "FAIL"
