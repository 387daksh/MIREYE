import asyncio
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app import main
from app.diligence import DiligenceService, compile_project_request
from app.project_readiness import AuthoritativeSourceService, build_entitlement_state, build_power_readiness
from app.sandbox import SiteSnapshotService
from app.sandbox_agent import ModelReply, SandboxAgent
from app.workspace.store import WorkspaceStore
from tests.test_diligence import FakeMireye, FakeWorlds, ScriptedModel, _phase10_enrich


def run(coro):
    return asyncio.run(coro)


def evidence(value, *, source="MIREYE_SOURCE", strength="SOURCE_BACKED_SIGNAL", scope="NEAREST_FEATURE", now=100.0):
    return {
        "value": value, "status": "ok", "source": source, "provider": "MIREYE",
        "semantic_strength": strength, "scope": scope, "observed_at": now,
        "expires_at": now + 3600, "evidence_hash": hashlib.sha256(repr(value).encode()).hexdigest(),
    }


def project_and_snapshot():
    project = {
        "project_id": "project-power", "request": {
            "capacity_mw": 100.0,
            "storage_requirements": {"phase_1_power_mw": 100.0, "phase_1_energy_mwh": 400.0, "duration_hours": 4.0, "expansion_power_mw": 300.0, "expansion_energy_mwh": 1200.0, "target_energization_date": None},
        }, "rfis": [],
    }
    candidate = {"candidate_id": "candidate-1", "site_id": "site-1", "snapshot_id": "snapshot-1"}
    snapshot = {
        "snapshot_id": "snapshot-1", "site_id": "site-1",
        "parcel_identity": {"parcel_id": "parcel-1", "parcel_address": "1032 ROBOTIC AVE", "selected_point": {"lat": 30.224762, "lng": -97.605674}},
        "evidence": {
            "electric_utility_service_territory": evidence("Austin Energy", scope="POINT"),
            "iso_rto": evidence("ERCOT", scope="REGION", strength="DIRECTLY_VERIFIED"),
            "nearest_transmission_line_distance_m": evidence(1000.0),
            "nearest_transmission_line_voltage_kv": evidence(345.0),
            "nearest_substation_distance_m": evidence(2500.0),
            "nearest_substation_max_voltage_kv": evidence(345.0),
            "nearest_power_plant_capacity_mw": evidence(500.0),
            "interconnection_queue_active_capacity_ercot_mw": evidence(10000.0, scope="REGION"),
            "parcel_zoning": evidence("LI", scope="PARCEL", strength="DIRECTLY_VERIFIED"),
            "political_county": evidence("Travis County", scope="REGION", strength="DIRECTLY_VERIFIED"),
        },
    }
    gap_power = {"gap_id": "gap-power", "requirement_id": "bess_export_interconnection", "domain": "Power", "missing_evidence": ["utility_or_iso_confirmed_export_injection_capacity_mw", "approved_bess_interconnection_path"], "affected_constraints": ["bess_export_interconnection"], "affected_scenarios": [], "stale_evidence": []}
    gap_entitlement = {"gap_id": "gap-entitlement", "requirement_id": "energy_storage_entitlement", "domain": "Entitlement", "missing_evidence": ["jurisdiction_aware_energy_storage_permitted_use_determination"], "affected_constraints": ["energy_storage_entitlement"], "affected_scenarios": [], "stale_evidence": []}
    actions = [
        {"action_id": "action-power", "requirement_id": "bess_export_interconnection", "type": "BESS_EXPORT_INTERCONNECTION_RFI"},
        {"action_id": "action-entitlement", "requirement_id": "energy_storage_entitlement", "type": "ENERGY_STORAGE_ENTITLEMENT_RFI"},
    ]
    intelligence = {"unresolved_issues": [gap_power, gap_entitlement], "recommended_actions": actions}
    return project, candidate, snapshot, intelligence


def external_state(now=100.0):
    records = [{
        "evidence_id": "external-jurisdiction", "field": "austin_jurisdiction", "value": {"JURISDICTION_TYPE_SPECIFICS": "FULL PURPOSE"},
        "status": "ok", "provider": "City of Austin", "dataset": "Jurisdiction boundaries", "source_url": "https://services.arcgis.com/example",
        "scope": "POINT_IN_POLYGON", "semantic_strength": "DIRECTLY_VERIFIED", "requirement_ids": ["energy_storage_entitlement"],
        "observed_at": now, "expires_at": now + 3600, "freshness": "CURRENT", "source_hash": "jurisdiction-hash", "human_review_required": True,
    }, {
        "evidence_id": "external-county", "field": "travis_county_development_permit_context", "value": {"development_permit_described": True, "site_applicability": "REQUIRES_CONFIRMATION"},
        "status": "ok", "provider": "Travis County", "dataset": "Environmental Review", "source_url": "https://www.traviscountytx.gov/example",
        "scope": "COUNTY_RULE_CONTEXT", "semantic_strength": "SOURCE_BACKED_SIGNAL", "requirement_ids": ["energy_storage_entitlement"],
        "observed_at": now, "expires_at": now + 3600, "freshness": "CURRENT", "source_hash": "county-hash", "human_review_required": True,
    }]
    return {"site_id": "site-1", "collected_at": now, "records": records, "sources": [{"provider": "ERCOT", "availability": "UNRESOLVED", "reason": "No approved generation or storage interconnection source is configured."}]}


def test_import_does_not_mutate_production_database():
    root = Path(__file__).parents[1]
    database = root / "app" / "data" / "workspaces.db"
    before = (database.stat().st_size, database.stat().st_mtime_ns, hashlib.sha256(database.read_bytes()).hexdigest())
    env = os.environ.copy()
    env["WORKSPACE_DB"] = str(database)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, "-c", "import app.main; assert not app.main.workspace_store._initialized"], cwd=root, env=env, capture_output=True, text=True)
    after = (database.stat().st_size, database.stat().st_mtime_ns, hashlib.sha256(database.read_bytes()).hexdigest())
    assert result.returncode == 0, result.stderr
    assert after == before


def test_explicit_temporary_store_initializes(tmp_path):
    store = WorkspaceStore(tmp_path / "phase12.db")
    store.create_workspace("workspace-1")
    assert store.db_path.exists()


def test_power_readiness_keeps_proximity_voltage_queue_and_generation_separate_from_export_capacity():
    project, candidate, snapshot, intelligence = project_and_snapshot()
    readiness = build_power_readiness(project, candidate, snapshot, intelligence, external_state(), now=100.0)
    items = {item["key"]: item for item in readiness["items"]}
    assert items["nearest_transmission"]["state"] == "SOURCE_BACKED"
    assert items["transmission_voltage"]["value"] == 345.0
    assert items["queue_context"]["value"] == 10000.0
    assert items["nearby_generation"]["value"] == 500.0
    assert items["confirmed_capacity"]["state"] == "UNRESOLVED"
    assert items["interconnection_pathway"]["state"] == "UNRESOLVED"
    assert items["phase_1_export_interconnection"]["state"] == "UNRESOLVED"
    assert items["expansion_export_interconnection"]["state"] == "UNRESOLVED"
    assert readiness["readiness_state"] == "PARTIAL"


def test_explicit_utility_or_iso_capacity_is_the_only_export_interconnection_proof():
    project, candidate, snapshot, intelligence = project_and_snapshot()
    snapshot["evidence"]["utility_or_iso_confirmed_export_injection_capacity_mw"] = evidence(150.0, source="UTILITY_OR_ISO_LETTER", strength="DIRECTLY_VERIFIED", scope="SITE")
    readiness = build_power_readiness(project, candidate, snapshot, intelligence, external_state(), now=100.0)
    items = {item["key"]: item for item in readiness["items"]}
    assert items["phase_1_export_interconnection"]["state"] == "VERIFIED"
    assert items["expansion_export_interconnection"]["state"] == "UNRESOLVED"


def test_entitlement_preserves_zoning_as_fact_not_legal_conclusion_and_builds_dependencies():
    project, candidate, snapshot, intelligence = project_and_snapshot()
    state = build_entitlement_state(project, candidate, snapshot, intelligence, external_state(), now=100.0)
    items = {item["key"]: item for item in state["items"]}
    graph = {item["step_id"]: item for item in state["dependency_graph"]}
    assert items["zoning_code"]["state"] == "VERIFIED"
    assert items["permitted_use"]["state"] == "UNRESOLVED"
    assert items["moratorium"]["state"] == "UNRESOLVED"
    assert graph["permitted_use_determination"]["state"] == "REQUIRES_CONFIRMATION"
    assert graph["permitted_use_determination"]["depends_on"] == ["zoning_determination"]
    assert state["human_review_required"] is True and state["legal_advice"] is False


def test_authoritative_document_adapter_preserves_citations_and_provenance():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "BOUNDARIES_jurisdictions" in url:
            return httpx.Response(200, json={"features": [{"attributes": {"CITY_NAME": "AUSTIN", "JURISDICTION_TYPE_SPECIFICS": "FULL PURPOSE"}}]})
        if "DDB_Phase_1" in url:
            return httpx.Response(200, json={"features": [{"attributes": {"ZONING_BASE": "LI", "ZONING_FULL": "LI"}}]})
        return httpx.Response(200, text="<html><body>A Basic Development Permit is required for land development outside municipal corporate boundaries.</body></html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = AuthoritativeSourceService(client)
    _project, _candidate, snapshot, _intelligence = project_and_snapshot()
    result = run(source.collect({"project_id": "project-1"}, snapshot))
    run(client.aclose())
    records = {item["field"]: item for item in result["records"]}
    assert {"austin_jurisdiction", "austin_base_zoning", "travis_county_development_permit_context"} <= set(records)
    assert next(item for item in result["sources"] if item["dataset"] == "BESS export / injection interconnection pathway")["availability"] == "UNRESOLVED"
    assert records["austin_base_zoning"]["document_type"] == "official_gis"
    assert "energy_storage_entitlement" in records["austin_base_zoning"]["requirement_ids"]
    assert records["travis_county_development_permit_context"]["human_review_required"] is True


class FakeSources:
    async def collect(self, project, snapshot):
        state = external_state(time.time())
        state["site_id"] = snapshot["site_id"]
        for record in state["records"]:
            record["expires_at"] = time.time() + 3600
        return state


def test_source_fusion_updates_existing_gaps_actions_readiness_and_rfi_context(tmp_path):
    store = WorkspaceStore(tmp_path / "phase12-project.db")
    client = FakeMireye()
    service = DiligenceService(store, SiteSnapshotService(store, client), FakeWorlds(), sources=FakeSources())
    project = _phase10_enrich(service, "Evaluate this 100 MW / 400 MWh BESS with sufficient grid capacity.", "workspace-phase12")
    candidate = next(item for item in project["candidates"] if item.get("site_id"))
    refreshed = run(service.refresh_authoritative_sources(project["project_id"], candidate["site_id"]))
    power, entitlement = refreshed["power_readiness"], refreshed["entitlement"]
    assert power["readiness_state"] in {"PARTIAL", "UNAVAILABLE"}
    assert next(item for item in power["items"] if item["key"] == "phase_1_export_interconnection")["state"] == "UNRESOLVED"
    assert next(item for item in entitlement["items"] if item["key"] == "permitted_use")["state"] == "UNRESOLVED"
    assert any(item["type"] == "ENERGY_STORAGE_ENTITLEMENT_RFI" for item in entitlement["next_best_actions"])
    action = next(item for item in service.next_actions(project["project_id"])["prioritized_actions"] if item["type"] == "BESS_EXPORT_INTERCONNECTION_RFI")
    draft = service.create_rfi_draft(project["project_id"], action["action_id"], "Please confirm export/injection capacity and the applicable storage interconnection study path for the supplied project and parcel.")
    assert draft["structured_context"]["project_requirements"]["phase_1_power_mw"] == 100.0
    assert draft["structured_context"]["site"]["parcel_id"]
    assert draft["human_approval_required"] is True
    current = service.evaluate_evidence_coverage(project["project_id"])
    assert any(item["requirement_id"] == "bess_export_interconnection" for item in current["evidence_gaps"])
    assert current["readiness"]["Power"]["status"] == "CRITICAL"


def test_power_and_entitlement_http_dossiers(monkeypatch, tmp_path):
    store = WorkspaceStore(tmp_path / "phase12-http.db")
    service = DiligenceService(store, SiteSnapshotService(store, FakeMireye()), FakeWorlds(), sources=FakeSources())
    project = _phase10_enrich(service, "Evaluate this 100 MW / 400 MWh BESS with sufficient grid capacity.", "workspace-phase12-http")
    candidate = project["candidates"][0]
    run(service.refresh_authoritative_sources(project["project_id"], candidate["site_id"]))
    monkeypatch.setattr(main, "diligence_service", service)
    http = TestClient(main.app)

    power = http.get(f"/v1/diligence/projects/{project['project_id']}/sites/{candidate['site_id']}/power-readiness")
    entitlement = http.get(f"/v1/diligence/projects/{project['project_id']}/sites/{candidate['site_id']}/entitlement")

    assert power.status_code == entitlement.status_code == 200
    assert power.json()["readiness_state"] == "PARTIAL"
    assert entitlement.json()["human_review_required"] is True


def test_compiler_records_fixed_bess_storage_requirements_without_invented_parameters():
    request = compile_project_request("Plan a 100 MW / 400 MWh BESS expandable to 300 MW / 1,200 MWh with grid capacity and energization date 2030-06-01.")
    requirements = request["storage_requirements"]
    assert requirements == {
        "phase_1_power_mw": 100.0, "phase_1_energy_mwh": 400.0, "duration_hours": 4.0,
        "expansion_power_mw": 300.0, "expansion_energy_mwh": 1200.0, "target_energization_date": "2030-06-01",
    }
    constraint_ids = {item["constraint_id"] for item in request["constraints"]}
    assert constraint_ids >= {"bess_export_interconnection", "energy_storage_entitlement"}
    assert constraint_ids.isdisjoint({"sufficient_grid_capacity", "data_center_entitlement"})


def test_agent_guard_rejects_invented_power_and_legal_conclusions(tmp_path):
    store = WorkspaceStore(tmp_path / "phase12-agent.db")
    client = FakeMireye()
    service = DiligenceService(store, SiteSnapshotService(store, client), FakeWorlds())
    project = _phase10_enrich(service, "Evaluate this 100 MW / 400 MWh BESS with sufficient grid capacity.", "workspace-phase12-agent")
    model = ScriptedModel([ModelReply(message="100 MW of export/injection capacity is available and energy-storage use is legally permitted.", tool_calls=[], response_items=[])])
    response = run(SandboxAgent(model=model, diligence=service).chat_project(project["project_id"], "phase12-agent", "Is this project ready?"))
    assert response["message"].startswith("I cannot support that power or entitlement conclusion")


def test_source_adapter_failure_is_explicitly_unavailable():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = AuthoritativeSourceService(client)
    _project, _candidate, snapshot, _intelligence = project_and_snapshot()
    result = run(source.collect({"project_id": "project-1"}, snapshot))
    run(client.aclose())
    assert result["records"] == []
    assert any(item["availability"] == "UNRESOLVED" for item in result["sources"])
    assert all(item["availability"] in {"UNRESOLVED", "UNAVAILABLE"} for item in result["sources"])
