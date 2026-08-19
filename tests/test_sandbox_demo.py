import asyncio
import json

import pytest

from app.sandbox import SITE_SNAPSHOT_FIELDS, SiteSnapshotService
from app.sandbox_agent import ModelReply, SandboxAgent
from app.sandbox_demo import DemoSetupError, provision_demo
from app.sandbox_scenarios import ScenarioService
from app.workspace.store import WorkspaceStore


class DemoMireyeClient:
    mode = "live"
    base_url = "https://api.mireye.com"

    def __init__(self):
        self.fetch_count = 0

    async def lookup(self, **_kwargs):
        return {"disposition": "resolved", "candidates": [{"lat": 30.0, "lng": -97.0}]}

    async def meta_fields(self):
        return {"version": "demo-test", "fields": [{"name": name, "source": "TEST", "ttl_seconds": 3600} for name in SITE_SNAPSHOT_FIELDS]}

    async def fetch_quote(self, **_kwargs):
        return {"estimated_credits": 322}

    async def fetch(self, **_kwargs):
        self.fetch_count += 1
        values = {name: None for name in SITE_SNAPSHOT_FIELDS}
        values.update({
            "parcel_id": "real-parcel-test",
            "parcel_apn": "APN-1",
            "parcel_address": "1 Demo Parcel Way",
            "parcel_area_m2": 1_000_000.0,
            "parcel_boundary_geojson": json.dumps({
                "type": "Polygon",
                "coordinates": [[[-97.005, 29.995], [-96.995, 29.995], [-96.995, 30.005], [-97.005, 30.005], [-97.005, 29.995]]],
            }),
            "parcel_data_source": "regrid_paid",
            "parcel_match_type": "exact_intersect",
            "parcel_match_distance_m": 0.0,
            "parcel_match_radius_m": 0.0,
        })
        return {"ok": True, "fields": {name: {"value": value, "status": "ok"} for name, value in values.items()}}


class ScriptedModel:
    model = "scripted-demo"

    def __init__(self, replies):
        self.replies = iter(replies)

    async def respond(self, _input_items, _tools):
        return next(self.replies)


def call(name, arguments, call_id):
    return ModelReply("", [{"id": call_id, "name": name, "arguments": arguments}], [])


def test_demo_provisions_and_exercises_persisted_flow(tmp_path):
    client = DemoMireyeClient()
    store = WorkspaceStore(tmp_path / "demo.db")
    snapshots = SiteSnapshotService(store, client)
    scenarios = ScenarioService(store)

    result = asyncio.run(provision_demo(snapshots, scenarios, address="1 Demo Parcel Way", confirmed=True))
    snapshot, first = result["snapshot"], result["scenario"]

    assert client.fetch_count == 1
    assert snapshots.get_snapshot(snapshot["snapshot_id"])["raw_response_hash"] == snapshot["raw_response_hash"]
    assert first["scene_state"]["proposed"][0]["attributes"]["capacity_mw"] == 100
    assert first["evaluation"]["overall_status"] == "PASS"

    move = call("transform_object", {
        "object_id": "data_center_1", "operation": "move", "delta_x_m": 10, "delta_y_m": 0,
        "width_m": None, "length_m": None, "height_m": None, "rotation_deg": None, "capacity_mw": None,
    }, "move-1")
    evaluate = call("evaluate_scenario", {"requested_constraints": [{
        "constraint_id": "footprint_inside_parcel", "object_id": None, "minimum_m": None,
        "min_m2": None, "max_m2": None, "max_percent": None, "max_degrees": None,
    }]}, "evaluate-1")
    agent = SandboxAgent(model=ScriptedModel([move, evaluate, ModelReply("Deterministic evaluation complete.", [], [])]), scenarios=scenarios)
    chat = asyncio.run(agent.chat(snapshot, "demo-session", "Move it and evaluate it.", workspace_id=snapshot["workspace_id"], scenario_id=first["scenario_id"]))

    assert chat["evaluation"]["overall_status"] == "PASS"
    assert scenarios.get(first["scenario_id"])["revision"] == 3
    branch = scenarios.branch(first["scenario_id"], user_intent="Alternative")
    comparison = scenarios.compare(first["scenario_id"], branch["scenario_id"])
    assert comparison["comparison_version"] == "sandbox_scenario_compare_v1"
    assert store.get_site_snapshot(snapshot["snapshot_id"])["geometry"] == snapshot["geometry"]


def test_demo_requires_explicit_ambiguous_selection(tmp_path):
    client = DemoMireyeClient()

    async def ambiguous(**_kwargs):
        return {"disposition": "clarify", "candidates": [{"lat": 30.0, "lng": -97.0}, {"lat": 31.0, "lng": -98.0}]}

    client.lookup = ambiguous
    store = WorkspaceStore(tmp_path / "demo.db")
    with pytest.raises(DemoSetupError, match="ambiguous"):
        asyncio.run(provision_demo(SiteSnapshotService(store, client), ScenarioService(store), address="Main Street", confirmed=True))
    assert client.fetch_count == 0
