import asyncio
import copy
import time

import pytest

from app.sandbox import ConfirmationRequired, REFRESH_IDENTITY_FIELDS, SiteSnapshotService, scene_state_from_snapshot
from app.sandbox_agent import InMemorySandboxSessions, ModelReply, SandboxAgent
from app.sandbox_scenarios import ScenarioService
from app.workspace.store import WorkspaceStore
from tests.test_sandbox import FakeMireyeClient


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def lifecycle(tmp_path):
    store = WorkspaceStore(db_path=tmp_path / "lifecycle.db")
    client = FakeMireyeClient()
    scenarios = ScenarioService(store)
    service = SiteSnapshotService(store=store, client=client, scenarios=scenarios)
    return store, client, scenarios, service


def _stale_copy(snapshot, *, field):
    stale = copy.deepcopy(snapshot)
    stale["snapshot_id"] = "site_stale_snapshot"
    stale["created_at"] += 1
    stale["observed_at"] += 1
    stale["evidence"][field]["expires_at"] = time.time() - 1
    stale["expires_at"] = time.time() - 1
    return stale


def test_site_identity_is_stable_and_freshness_is_field_level(lifecycle):
    store, _client, _scenarios, service = lifecycle
    first = _run(service.create_snapshot(workspace_id="ws-life", lat=32.0, lng=-97.0, confirmed=True))
    stale = _stale_copy(first, field="wetland_fraction_of_parcel")
    stale["evidence"]["slope_degrees"] = {"value": None, "status": "ok"}
    stale["evidence"]["parcel_zoning"]["status"] = "error"
    stale["evidence"]["fema_flood_zone"]["lifecycle"] = "deprecated"
    store.create_site_snapshot(stale)

    loaded = store.get_site_snapshot(stale["snapshot_id"])
    freshness = service.freshness_status(stale["snapshot_id"])
    states = {item["field"]: item["classification"] for item in freshness["fields"]}

    assert first["site_id"] == loaded["site_id"]
    assert states["wetland_fraction_of_parcel"] == "stale"
    assert states["slope_degrees"] == "missing"
    assert states["parcel_zoning"] == "incompatible"
    assert states["fema_flood_zone"] == "deprecated"
    assert "nearest_major_road_distance_m" in freshness["fresh_fields"]


def test_test_expiry_override_is_in_memory_and_does_not_change_provider_ttl(lifecycle):
    _store, _client, _scenarios, service = lifecycle
    snapshot = _run(service.create_snapshot(workspace_id="ws-life", lat=32.0, lng=-97.0, confirmed=True))
    original_expiry = snapshot["evidence"]["wetland_fraction_of_parcel"]["expires_at"]

    freshness = service.freshness_status(
        snapshot["snapshot_id"], test_expiry_overrides={"wetland_fraction_of_parcel": time.time() - 1},
    )
    states = {item["field"]: item for item in freshness["fields"]}

    assert states["wetland_fraction_of_parcel"]["classification"] == "stale"
    assert "Test-time local expiration override" in states["wetland_fraction_of_parcel"]["reason"]
    assert service.get_snapshot(snapshot["snapshot_id"])["evidence"]["wetland_fraction_of_parcel"]["expires_at"] == original_expiry


def test_default_freshness_checks_only_fields_present_in_partial_snapshot(lifecycle):
    store, _client, _scenarios, service = lifecycle
    snapshot = _run(service.create_snapshot(workspace_id="ws-life", lat=32.0, lng=-97.0, confirmed=True))
    partial = copy.deepcopy(snapshot)
    partial["snapshot_id"] = "site-partial-fields"
    partial["created_at"] += 1
    partial["evidence"] = {name: partial["evidence"][name] for name in ("parcel_id", "parcel_boundary_geojson")}
    store.create_site_snapshot(partial)

    freshness = service.freshness_status(partial["snapshot_id"])

    assert {item["field"] for item in freshness["fields"]} == {"parcel_id", "parcel_boundary_geojson"}
    assert freshness["missing_fields"] == []


def test_refresh_creates_t2_and_re_evaluates_only_affected_scenario(lifecycle):
    store, client, scenarios, service = lifecycle
    first = _run(service.create_snapshot(workspace_id="ws-life", lat=32.0, lng=-97.0, confirmed=True))
    scenario = scenarios.create(
        first,
        workspace_id="ws-life",
        user_intent="Avoid wetlands",
        scene_state=scene_state_from_snapshot(first),
        requested_constraints=[{"constraint_id": "max_nwi_wetland_fraction_of_parcel", "max_fraction": 0.1}],
    )
    assert scenario["evaluation"]["overall_status"] == "PASS"

    stale = _stale_copy(first, field="wetland_fraction_of_parcel")
    store.create_site_snapshot(stale)
    client.fields["wetland_fraction_of_parcel"]["value"] = 0.4

    plan = _run(service.quote_refresh(stale["snapshot_id"]))
    assert plan["status"] == "QUOTED"
    assert set(REFRESH_IDENTITY_FIELDS).issubset(plan["requested_fields"])
    assert "wetland_fraction_of_parcel" in plan["requested_fields"]
    assert plan["affected_scenarios"] == [{
        "scenario_id": scenario["scenario_id"], "revision": 1,
        "status": "STALE_EVIDENCE", "affected_constraint_ids": ["max_nwi_wetland_fraction_of_parcel"],
    }]
    assert client.fetch_calls[-1]["fields"] != plan["requested_fields"]
    with pytest.raises(ConfirmationRequired):
        _run(service.confirm_and_refresh(plan["spend_plan_id"], confirmed_by_application=False))
    assert client.fetch_calls[-1]["fields"] != plan["requested_fields"]

    refreshed = _run(service.confirm_and_refresh(plan["spend_plan_id"], confirmed_by_application=True))
    second = refreshed["snapshot"]
    runs = refreshed["evaluation_runs"]

    assert second["snapshot_id"] != stale["snapshot_id"]
    assert second["site_id"] == first["site_id"]
    assert store.get_site_snapshot(first["snapshot_id"])["evidence"]["wetland_fraction_of_parcel"]["value"] == 0.0
    assert second["evidence"]["nearest_major_road_distance_m"]["carried_from_snapshot_id"] == stale["snapshot_id"]
    assert runs[0]["status"] == "INVALIDATED_BY_REFRESH"
    assert runs[0]["affected_constraint_ids"] == ["max_nwi_wetland_fraction_of_parcel"]
    assert runs[0]["evaluation"]["constraint_results"][0]["outcome"] == "FAIL"
    assert scenarios.get(scenario["scenario_id"])["site_snapshot_id"] == first["snapshot_id"]
    assert store.get_mireye_spend_plan(plan["spend_plan_id"])["status"] == "COMPLETED"


def test_geometry_change_never_mutates_proposal_and_requires_rebase(lifecycle):
    store, client, scenarios, service = lifecycle
    first = _run(service.create_snapshot(workspace_id="ws-life", lat=32.0, lng=-97.0, confirmed=True))
    scenario = scenarios.create(
        first,
        workspace_id="ws-life",
        user_intent="Parcel fit",
        scene_state=scene_state_from_snapshot(first),
        requested_constraints=[{"constraint_id": "footprint_inside_parcel"}],
    )
    stale = _stale_copy(first, field="parcel_boundary_geojson")
    store.create_site_snapshot(stale)
    client.fields["parcel_boundary_geojson"]["value"] = "{\"type\":\"Polygon\",\"coordinates\":[[[-97.005,31.995],[-96.995,31.995],[-96.995,32.005],[-97.005,32.005],[-97.005,31.995]]]}"

    plan = _run(service.quote_refresh(stale["snapshot_id"]))
    refreshed = _run(service.confirm_and_refresh(plan["spend_plan_id"], confirmed_by_application=True))
    run = refreshed["evaluation_runs"][0]

    assert refreshed["snapshot_diff"]["geometry_changed"] is True
    assert run["status"] == "NEEDS_GEOMETRY_REBASE"
    assert scenarios.get(scenario["scenario_id"])["scene_state"] == scenario["scene_state"]


def test_agent_refresh_tools_require_application_confirmation(lifecycle):
    _store, _client, _scenarios, service = lifecycle
    snapshot = _run(service.create_snapshot(workspace_id="ws-life", lat=32.0, lng=-97.0, confirmed=True))
    stale = _stale_copy(snapshot, field="wetland_fraction_of_parcel")
    service.store.create_site_snapshot(stale)

    class Model:
        def __init__(self, replies):
            self.replies = replies

        async def respond(self, *_args):
            return self.replies.pop(0)

    quote_call = ModelReply("", [{"id": "quote", "name": "quote_mireye_refresh", "arguments": '{"snapshot_id":"site_stale_snapshot"}'}], [])
    response = _run(SandboxAgent(model=Model([quote_call, ModelReply("quoted", [], [])]), sessions=InMemorySandboxSessions(), intelligence=service).chat(stale, "refresh-agent", "Check refresh."))
    plan_id = response["tool_trace"][0]["result"]["spend_plan_id"]
    confirm_call = ModelReply("", [{"id": "confirm", "name": "confirm_and_refresh_evidence", "arguments": '{"spend_plan_id":"' + plan_id + '"}'}], [])
    rejected = _run(SandboxAgent(model=Model([confirm_call]), sessions=InMemorySandboxSessions(), intelligence=service).chat(stale, "refresh-agent", "Refresh now."))

    assert rejected["tool_trace"][0]["status"] == "rejected"
    assert "application has not confirmed" in rejected["message"]

    confirmed = _run(SandboxAgent(model=Model([confirm_call, ModelReply("refreshed", [], [])]), sessions=InMemorySandboxSessions(), intelligence=service).chat(
        stale, "refresh-agent-confirmed", "Refresh now.", confirmed_refresh_plan_id=plan_id,
    ))
    assert confirmed["tool_trace"][0]["result"]["status"] == "REFRESHED"
