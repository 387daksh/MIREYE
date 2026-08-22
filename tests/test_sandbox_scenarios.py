import asyncio
import copy
import json
import math

from app.sandbox import EARTH_RADIUS_M, scene_state_from_snapshot
from app.sandbox_agent import InMemorySandboxSessions, ModelReply, SandboxAgent
from app.sandbox_scenarios import ScenarioService
from app.workspace.store import WorkspaceStore


def _run(coro):
    return asyncio.run(coro)


def snapshot():
    half = math.degrees(500 / EARTH_RADIUS_M)
    return {
        "snapshot_id": "site-scenario-test", "workspace_id": "ws-scenario", "is_expired": False,
        "parcel_identity": {"parcel_id": "parcel-1", "parcel_data_source": "TEST", "parcel_match_type": "exact_intersect", "parcel_match_distance_m": 0.0, "selected_point": {"lat": 0.0, "lng": 0.0}},
        "geometry": {"type": "Polygon", "coordinates": [[[-half, -half], [half, -half], [half, half], [-half, half], [-half, -half]]]},
        "evidence": {name: {"value": value, "status": "ok", "source": "TEST", "ttl_seconds": 3600, "observed_at": 1.0, "expires_at": 9999999999.0} for name, value in {
            "parcel_id": "parcel-1", "parcel_boundary_geojson": "authoritative-boundary", "parcel_match_type": "exact_intersect", "parcel_match_distance_m": 0.0,
        }.items()},
        "raw_response": {"fields": {}}, "raw_response_hash": "raw-hash", "request": {"lat": 0.0, "lng": 0.0}, "request_hash": "request-hash",
        "field_catalog_version": "test-v1", "provider_metadata": {"provider": "test"}, "observed_at": 1.0, "expires_at": 9999999999.0, "created_at": 1.0,
    }


def scene(site):
    return scene_state_from_snapshot(site)


def make_service(tmp_path):
    store = WorkspaceStore(db_path=tmp_path / "workspace.db")
    site = snapshot()
    store.create_site_snapshot(site)
    return store, site, ScenarioService(store)


def test_scenario_creation_hash_and_restart_safe_persistence(tmp_path):
    store, site, service = make_service(tmp_path)
    first = service.create(site, workspace_id="ws-scenario", user_intent="Place 100 MW", scene_state=scene(site), model_id="test-model")
    second = service.create(site, workspace_id="ws-scenario", user_intent="Place 100 MW", scene_state=scene(site), model_id="test-model")

    restarted = ScenarioService(WorkspaceStore(db_path=store.db_path))
    loaded = restarted.get(first["scenario_id"])

    assert first["revision"] == 1
    assert first["state_hash"] == second["state_hash"]
    assert loaded["state_hash"] == first["state_hash"]
    assert loaded["site_snapshot_id"] == site["snapshot_id"]
    assert store.get_site_snapshot(site["snapshot_id"])["geometry"] == site["geometry"]


def test_accepted_chat_mutation_creates_revision_and_rejected_call_does_not(tmp_path):
    _store, site, service = make_service(tmp_path)

    class Model:
        def __init__(self, replies): self.replies = replies
        async def respond(self, *_args): return self.replies.pop(0)

    def reply(name, arguments):
        return ModelReply("", [{"id": name, "name": name, "arguments": json.dumps(arguments)}], [])

    create = reply("propose_data_center", {"capacity_mw": 100, "width_m": None, "length_m": None, "height_m": None, "position": None, "rotation_deg": None, "minimum_setback_m": 10})
    final = ModelReply("done", [], [])
    agent = SandboxAgent(model=Model([create, final]), sessions=InMemorySandboxSessions(), scenarios=service)
    response = _run(agent.chat(site, "session", "Place a 100 MW data center."))
    scenario_id = response["scenario"]["scenario_id"]
    assert len(service.list_revisions(scenario_id)) == 1

    malformed = ModelReply("", [{"id": "bad", "name": "transform_object", "arguments": "{"}], [])
    rejected = _run(SandboxAgent(model=Model([malformed]), sessions=agent.sessions, scenarios=service).chat(site, "session", "Break it."))
    assert rejected["tool_trace"][0]["status"] == "rejected"
    assert len(service.list_revisions(scenario_id)) == 1

    move = reply("transform_object", {"object_id": "data_center_1", "operation": "move", "delta_x_m": 50, "delta_y_m": 0, "width_m": None, "length_m": None, "height_m": None, "rotation_deg": None, "capacity_mw": None})
    moved = _run(SandboxAgent(model=Model([move, final]), sessions=agent.sessions, scenarios=service).chat(site, "session", "Move it east."))
    assert moved["scenario"]["revision"] == 2
    assert len(service.list_revisions(scenario_id)) == 2


def test_branch_and_comparison_detect_geometry_metrics_and_constraints(tmp_path):
    _store, site, service = make_service(tmp_path)
    base = service.create(site, workspace_id="ws-scenario", user_intent="Scenario A", scene_state=scene(site))
    branch = service.branch(base["scenario_id"], user_intent="Scenario B")
    moved_scene = copy.deepcopy(branch["scene_state"])
    moved_scene["proposed"][0]["geometry_local"]["center_xy_m"] = [600.0, 0.0]
    moved = service.append(site, scenario_id=branch["scenario_id"], user_intent="Move B north", scene_state=moved_scene)

    comparison = service.compare(base["scenario_id"], branch["scenario_id"], right_revision=moved["revision"])

    assert branch["parent_scenario_id"] == base["scenario_id"]
    assert moved["revision"] == 2
    assert comparison["object_changes"]["data_center_1"]["change"] == "modified"
    assert comparison["metric_changes"]
    assert comparison["constraint_changes"]["footprint_inside_parcel"]["after"]["outcome"] == "FAIL"
    assert comparison["evaluation_versions"]["left"]["evaluator_version"] == "site_sandbox_evidence_v1"


def test_comparison_reflects_phase6_evidence_constraint_results(tmp_path):
    _store, site, service = make_service(tmp_path)
    site["evidence"].update({
        "wetland_acres_on_parcel": {"value": 1.0, "status": "ok", "scope": "PARCEL", "expires_at": 9999999999.0},
        "wetland_fraction_of_parcel": {"value": 0.1, "status": "ok", "scope": "PARCEL", "expires_at": 9999999999.0},
    })
    passing = service.create(site, workspace_id="ws-scenario", user_intent="Wetland pass", scene_state=scene(site), requested_constraints=[{"constraint_id": "max_nwi_wetland_fraction_of_parcel", "max_fraction": 0.2}])
    failing = service.create(site, workspace_id="ws-scenario", user_intent="Wetland fail", scene_state=scene(site), requested_constraints=[{"constraint_id": "max_nwi_wetland_fraction_of_parcel", "max_fraction": 0.05}])

    comparison = service.compare(passing["scenario_id"], failing["scenario_id"])

    assert comparison["constraint_changes"]["max_nwi_wetland_fraction_of_parcel"]["before"]["outcome"] == "PASS"
    assert comparison["constraint_changes"]["max_nwi_wetland_fraction_of_parcel"]["after"]["outcome"] == "FAIL"
