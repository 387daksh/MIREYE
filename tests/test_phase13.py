import asyncio
import copy
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.diligence import DiligenceService
from app.project_changes import changes_from_refresh, changes_from_world_refresh, world_snapshot_diff
from app.sandbox import ParcelIdentityError, SiteSnapshotService
from app.sandbox_agent import ModelReply, SandboxAgent
from app.workspace.store import WorkspaceStore
from tests.test_diligence import FakeMireye, FakeWorlds, ScriptedModel, _phase10_enrich
from tests.test_sandbox import FakeMireyeClient


def run(coro):
    return asyncio.run(coro)


def _snapshot(snapshot_id="t1"):
    return {
        "snapshot_id": snapshot_id, "site_id": "site-1",
        "parcel_identity": {"parcel_id": "parcel-1", "parcel_address": "1 Test Road"},
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
        "evidence": {
            "nearest_transmission_line_distance_m": {
                "value": 989.0, "status": "ok", "scope": "NEAREST_FEATURE", "provider": "MIREYE",
                "source": "SOURCE_A", "source_url": "https://example.test/a", "observed_at": 10.0, "expires_at": 20.0,
            }
        },
    }


def _intelligence(status="UNRESOLVED", readiness="CRITICAL", *, action=True):
    requirement = {
        "requirement_id": "max_resolution_point_transmission_distance_m", "title": "Transmission proximity",
        "status": status, "impact": "HIGH", "decision_provable": status in {"PASS", "FAIL"},
    }
    return {
        "evidence_dependencies": [{"requirement_id": requirement["requirement_id"], "evidence_id": "nearest_transmission_line_distance_m"}],
        "evidence_coverage": [requirement],
        "readiness": {"Power": {"status": readiness, "requirements": [requirement["requirement_id"]]}},
        "recommended_actions": ([{"action_id": "action-1", "requirement_id": requirement["requirement_id"], "required_evidence": ["utility_confirmation"]}] if action else []),
    }


def test_deterministic_evidence_diff_and_change_propagation():
    before, after = _snapshot(), _snapshot("t2")
    after["evidence"]["nearest_transmission_line_distance_m"].update(
        value=730.0, status="reviewed", scope="POINT", source="SOURCE_B", source_url="https://example.test/b", expires_at=40.0,
    )
    diff = SiteSnapshotService.snapshot_diff(before, after)
    runs = [{"scenario_id": "scenario-a", "revision": 2, "status": "UNRESOLVED", "affected_constraint_ids": ["max_resolution_point_transmission_distance_m"]}]
    changes = changes_from_refresh(
        project_id="project-1", site_id="site-1", before_snapshot=before, after_snapshot=after,
        snapshot_diff=diff, before_intelligence=_intelligence(), after_intelligence=_intelligence("PASS", "READY", action=False),
        evaluation_runs=runs, scenario_dependencies={"nearest_transmission_line_distance_m": [{"scenario_id": "scenario-a", "revision": 2, "constraint_id": "max_resolution_point_transmission_distance_m"}]}, detected_at=50.0,
    )
    by_type = {item["semantic_change_type"]: item for item in changes}
    assert {"VALUE_CHANGED", "FRESHNESS_CHANGED", "STATUS_CHANGED", "SCOPE_CHANGED", "SOURCE_CHANGED"} <= set(by_type)
    assert by_type["VALUE_CHANGED"]["old_value"] == 989.0 and by_type["VALUE_CHANGED"]["new_value"] == 730.0
    assert by_type["VALUE_CHANGED"]["significance"] == "HIGH"
    assert by_type["VALUE_CHANGED"]["affected_scenarios"] == [{"scenario_id": "scenario-a", "revision": 2, "state": "STALE"}]
    assert by_type["VALUE_CHANGED"]["affected_readiness"] == [{"domain": "Power", "before": "CRITICAL", "after": "READY"}]
    assert changes == changes_from_refresh(
        project_id="project-1", site_id="site-1", before_snapshot=before, after_snapshot=after,
        snapshot_diff=diff, before_intelligence=_intelligence(), after_intelligence=_intelligence("PASS", "READY", action=False),
        evaluation_runs=runs, scenario_dependencies={"nearest_transmission_line_distance_m": [{"scenario_id": "scenario-a", "revision": 2, "constraint_id": "max_resolution_point_transmission_distance_m"}]}, detected_at=50.0,
    )


def test_identity_geometry_and_evidence_add_remove_are_explicit():
    before, after = _snapshot(), _snapshot("t2")
    after["parcel_identity"]["parcel_id"] = "parcel-2"
    after["geometry"] = {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]}
    after["evidence"]["new_field"] = {"value": 1, "status": "ok", "expires_at": 40.0}
    diff = SiteSnapshotService.snapshot_diff(before, after)
    changes = changes_from_refresh(
        project_id="project-1", site_id="site-1", before_snapshot=before, after_snapshot=after,
        snapshot_diff=diff, before_intelligence=None, after_intelligence=None, detected_at=50.0,
    )
    types = {item["semantic_change_type"] for item in changes}
    assert {"IDENTITY_CHANGED", "GEOMETRY_CHANGED", "EVIDENCE_ADDED"} <= types
    assert all(item["significance"] == "CRITICAL" for item in changes if item["semantic_change_type"] in {"IDENTITY_CHANGED", "GEOMETRY_CHANGED"})


def test_live_refresh_identity_mismatch_is_a_hard_stop(tmp_path):
    store = WorkspaceStore(tmp_path / "identity.db")
    client = FakeMireyeClient()
    service = SiteSnapshotService(store, client)
    first = run(service.create_snapshot(workspace_id="workspace-1", lat=32.0, lng=-97.0, confirmed=True))
    stale = copy.deepcopy(first)
    stale["snapshot_id"] = "site-identity-stale"
    stale["evidence"]["parcel_id"]["expires_at"] = 1.0
    stale["expires_at"] = 1.0
    store.create_site_snapshot(stale)
    plan = run(service.quote_refresh(stale["snapshot_id"]))
    client.fields["parcel_id"]["value"] = "different-parcel"

    with pytest.raises(ParcelIdentityError):
        run(service.confirm_and_refresh(plan["spend_plan_id"], confirmed_by_application=True))

    assert len(store.list_site_snapshots(first["site_id"])) == 2
    assert store.get_mireye_spend_plan(plan["spend_plan_id"])["status"] == "IDENTITY_MISMATCH"


def test_world_snapshot_diff_uses_source_and_artifact_hashes():
    before = {"layers": [{"layer": "roads", "availability": "AVAILABLE", "source": {"release": "2026-01"}, "artifacts": {"render": {"sha256": "a"}}}]}
    after = {"layers": [{"layer": "roads", "availability": "AVAILABLE", "source": {"release": "2026-02"}, "artifacts": {"render": {"sha256": "b"}}}]}
    assert world_snapshot_diff(before, after) == [{"layer": "roads", "before": {"availability": "AVAILABLE", "source": {"release": "2026-01"}, "artifacts": {"render": "a"}}, "after": {"availability": "AVAILABLE", "source": {"release": "2026-02"}, "artifacts": {"render": "b"}}}]
    before.update(world_snapshot_id="world-1", created_at=10.0)
    after.update(world_snapshot_id="world-2", created_at=20.0)
    changes = changes_from_world_refresh(
        project_id="project-1", site_id="site-1", site_snapshot=_snapshot(), before_world=before, after_world=after, detected_at=30.0,
    )
    assert changes[0]["significance"] == "INFO"
    assert changes[0]["affected_scenarios"] == []
    assert changes[0]["world_snapshot_before"] == "world-1" and changes[0]["world_snapshot_after"] == "world-2"


def test_project_change_persistence_filters_and_http_summary(monkeypatch, tmp_path):
    store = WorkspaceStore(tmp_path / "phase13.db")
    service = DiligenceService(store, SiteSnapshotService(store, FakeMireye()), FakeWorlds())
    project = service.create_project(workspace_id="workspace-1", message="Evaluate one site.", candidates=["First Site"])
    change = changes_from_refresh(
        project_id=project["project_id"], site_id="site-1", before_snapshot=_snapshot(), after_snapshot=_snapshot("t2"),
        snapshot_diff={"identity_changed": False, "geometry_changed": False, "field_changes": {
            "nearest_transmission_line_distance_m": {"existence": {"before": True, "after": True}, "value": {"before": 989, "after": 730}}
        }}, before_intelligence=_intelligence(), after_intelligence=_intelligence(), detected_at=50.0,
    )
    store.save_project_changes(change)
    store.save_project_changes(change)
    assert len(store.list_project_changes(project["project_id"], significance="HIGH")) == 1
    monkeypatch.setattr(main, "diligence_service", service)
    response = TestClient(main.app).get(f"/v1/diligence/projects/{project['project_id']}/changes?severity=HIGH")
    assert response.status_code == 200
    assert response.json()["material_change_count"] == 1
    check = TestClient(main.app).post(f"/v1/diligence/projects/{project['project_id']}/check-now")
    assert check.status_code == 200 and check.json()["status"] == "CURRENT"


def test_check_now_skips_current_fetch_then_quotes_confirms_and_records_t2(tmp_path):
    store = WorkspaceStore(tmp_path / "phase13-check.db")
    client = FakeMireye()
    service = DiligenceService(store, SiteSnapshotService(store, client), FakeWorlds())
    project = _phase10_enrich(
        service,
        "Compare one site for a 100 MW / 400 MWh BESS, 20-50 acres, resolution point outside flood, within 2 km of transmission and within 1 km of road, with sufficient grid capacity.",
        "workspace-phase13",
    )
    snapshot = copy.deepcopy(store.get_site_snapshot(project["candidates"][0]["snapshot_id"]))
    snapshot["snapshot_id"] = "site-phase13-complete"
    for record in snapshot["evidence"].values():
        if record.get("value") is None:
            record["status"] = "absent"
    store.create_site_snapshot(snapshot)
    project["candidates"][0]["snapshot_id"] = snapshot["snapshot_id"]
    service._save(project)
    quote_count, fetch_count = len(client.quote_calls), len(client.fetch_calls)
    current = run(service.check_now_workflow(project["project_id"]))
    assert current["status"] == "CURRENT"
    assert len(client.quote_calls) == quote_count and len(client.fetch_calls) == fetch_count

    candidate = project["candidates"][0]
    stale = copy.deepcopy(store.get_site_snapshot(candidate["snapshot_id"]))
    stale["snapshot_id"] = "site-phase13-stale"
    stale["evidence"]["nearest_transmission_line_distance_m"].pop("evidence_hash", None)
    stale["evidence"]["nearest_transmission_line_distance_m"]["expires_at"] = 1.0
    stale["expires_at"] = 1.0
    store.create_site_snapshot(stale)
    persisted = service.get(project["project_id"])
    persisted["candidates"][0]["snapshot_id"] = stale["snapshot_id"]
    service._update_project_intelligence(persisted)
    service._save(persisted)

    planned = run(service.check_now_workflow(project["project_id"]))
    assert planned["status"] == "AWAITING_CONFIRMATION" and planned["confirmation_required"] is True
    assert len(client.fetch_calls) == fetch_count
    plan = planned["refresh_plans"][0]
    completed = run(service.check_now_workflow(
        project["project_id"], candidate_id=candidate["candidate_id"], spend_plan_id=plan["spend_plan_id"], confirmed=True,
    ))
    assert completed["status"] == "CHECK_COMPLETE"
    assert completed["refresh"]["previous_snapshot_id"] == stale["snapshot_id"]
    assert completed["refresh"]["snapshot"]["snapshot_id"] != stale["snapshot_id"]
    assert completed["impact_summary"]["change_count"] >= 1
    assert service.changes(project["project_id"], change_type="FRESHNESS_CHANGED")["change_count"] >= 1


def test_action_lifecycle_and_watch_are_persisted(tmp_path):
    project = {"action_transitions": []}
    DiligenceService._propagate_action_lifecycle(project, _intelligence(), _intelligence("PASS", "READY", action=False), "t1", "t2")
    assert project["action_transitions"] == [{"action_id": "action-1", "from": "CURRENT", "to": "COMPLETED", "snapshot_before": "t1", "snapshot_after": "t2"}]

    store = WorkspaceStore(tmp_path / "watch.db")
    service = DiligenceService(store, SiteSnapshotService(store, FakeMireye()), FakeWorlds())
    created = service.create_project(workspace_id="workspace-1", message="Evaluate one site.", candidates=["First Site"])
    watch = service.set_watch(created["project_id"], enabled=True)
    restarted = DiligenceService(WorkspaceStore(store.db_path), service.sandbox, FakeWorlds()).get(created["project_id"])
    assert watch["source"] == "MIREYE" and watch["cadence_policy"] == "MANUAL"
    assert restarted["watch"]["enabled"] is True and restarted["watch"]["cost_policy"]["metered_refresh"] == "EXPLICIT_CONFIRMATION_REQUIRED"


def test_agent_cannot_invent_change_without_structured_record(tmp_path):
    store = WorkspaceStore(tmp_path / "agent.db")
    service = DiligenceService(store, SiteSnapshotService(store, FakeMireye()), FakeWorlds())
    project = service.create_project(workspace_id="workspace-1", message="Evaluate one site.", candidates=["First Site"])
    model = ScriptedModel([ModelReply(message="Transmission changed and Scenario A became stale.", tool_calls=[], response_items=[])])
    response = run(SandboxAgent(model=model, diligence=service).chat_project(project["project_id"], "phase13", "What changed?"))
    assert response["message"] == "I cannot support a change claim because no structured ProjectChange record exists for this project."


def test_production_database_fingerprint_is_unchanged():
    database = Path(__file__).parents[1] / "app" / "data" / "workspaces.db"
    assert database.stat().st_size == 495616
    assert hashlib.sha256(database.read_bytes()).hexdigest().upper() == "07A6066F679C03F4FA90DB42644BF7C35BFEE375F35A8E2D088289B55BC9838F"
