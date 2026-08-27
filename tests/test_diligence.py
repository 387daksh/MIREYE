import asyncio
import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.diligence import CONSTRAINT_CAPABILITIES, CONSTRAINT_FIELDS, DiligenceError, DiligenceService, UserSuppliedCandidateProvider, compile_project_request
from app.mireye_client import MireyeClient
from app.sandbox import DATA_CENTER_CONTEXT_FIELDS, ConfirmationRequired, SITE_SNAPSHOT_FIELDS, SITE_SNAPSHOT_FIELD_SCOPES, SiteSnapshotService
from app.sandbox_agent import DILIGENCE_TOOL_DEFINITIONS, TOOL_DEFINITIONS, ModelReply, SandboxAgent
from app.workspace.store import WorkspaceStore


def run(coro):
    return asyncio.run(coro)


class FakeWorlds:
    def latest_for_site_snapshot(self, _snapshot_id):
        return None


class FakeMireye:
    mode = "live"
    base_url = "https://api.mireye.com"

    def __init__(self):
        self.lookup_calls = []
        self.quote_calls = []
        self.batch_calls = []
        self.fetch_calls = []

    async def lookup(self, *, input, kind, include_parcel):
        self.lookup_calls.append({"input": input, "kind": kind, "include_parcel": include_parcel})
        if "Ambiguous" in input:
            return {"disposition": "clarify", "candidates": [{"lat": 32.0, "lng": -97.0}, {"lat": 32.1, "lng": -97.1}]}
        if "Missing" in input:
            return {"disposition": "not_found", "candidates": []}
        latitude = 33.0 if "Second" in input else 34.0 if "Broken" in input else 32.0
        return {"disposition": "exact_match", "candidates": [{"lat": latitude, "lng": -97.0, "address": input}]}

    async def meta_fields(self):
        fields = set(SITE_SNAPSHOT_FIELDS)
        fields.update(name for names in CONSTRAINT_FIELDS.values() for name in names)
        return {
            "version": "catalog-diligence-v1",
            "fields": [
                {"name": name, "source": "MIREYE_TEST", "ttl_seconds": 60, "scope": SITE_SNAPSHOT_FIELD_SCOPES.get(name)}
                for name in sorted(fields)
            ],
        }

    async def fetch_quote(self, *, locations, fields, preset=None):
        self.quote_calls.append({"locations": locations, "fields": list(fields), "preset": preset})
        return {"quote_id": f"quote-{len(self.quote_calls)}", "estimated_credits": locations * 10}

    async def fetch_batch(self, *, locations, fields, preset=None):
        self.batch_calls.append({"locations": copy.deepcopy(locations), "fields": list(fields), "preset": preset})
        return {"results": [self._dossier(location, fields) for location in locations]}

    async def fetch(self, *, lat, lng, fields, preset=None):
        self.fetch_calls.append({"lat": lat, "lng": lng, "fields": list(fields), "preset": preset})
        return self._dossier({"lat": lat, "lng": lng}, fields)

    async def ask(self, **kwargs):
        return {"answer": "MIREYE answer", "request": kwargs}

    @staticmethod
    def _dossier(location, fields):
        lat, lng = float(location["lat"]), float(location["lng"])
        if lat == 34.0:
            return {"ok": False, "error": {"message": "Candidate data unavailable."}}
        area_acres = 30.0 if lat == 32.0 else 60.0
        values = {
            "parcel_id": f"parcel-{lat:g}", "parcel_apn": f"APN-{lat:g}", "parcel_address": f"{lat:g} Test Road",
            "parcel_area_m2": area_acres * 4046.8564224,
            "parcel_boundary_geojson": json.dumps({"type": "Polygon", "coordinates": [[[lng - .02, lat - .02], [lng + .02, lat - .02], [lng + .02, lat + .02], [lng - .02, lat + .02], [lng - .02, lat - .02]]]}),
            "parcel_data_source": "MIREYE_TEST", "parcel_match_type": "exact_intersect",
            "parcel_match_distance_m": 0.0, "parcel_match_radius_m": 0.0,
            "parcel_zoning": "I-2" if lat == 32.0 else "R-1", "within_floodplain_polygon": lat != 32.0,
            "fema_flood_zone": "X" if lat == 32.0 else "AE", "slope_degrees": 2.0,
            "wetland_acres_on_parcel": 0.0, "wetland_fraction_of_parcel": 0.0,
            "nearest_substation_distance_m": 1200.0, "nearest_substation_status": "IN SERVICE", "nearest_substation_max_voltage_kv": 345.0,
            "nearest_transmission_line_distance_m": 1000.0 if lat == 32.0 else 4000.0,
            "nearest_transmission_line_status": "IN SERVICE", "nearest_transmission_line_voltage_kv": 345.0,
            "nearest_major_road_distance_m": 200.0 if lat == 32.0 else 1800.0, "nearest_major_road_name": "Test Road",
        }
        return {"ok": True, "fields": {field: {"value": values.get(field), "status": "ok"} for field in fields}, "snapshot_ts": "2026-08-22T00:00:00Z"}


@pytest.fixture
def diligence(tmp_path):
    client = FakeMireye()
    store = WorkspaceStore(tmp_path / "diligence.db")
    snapshots = SiteSnapshotService(store, client)
    return DiligenceService(store, snapshots, FakeWorlds()), client, store


def create_project(service, candidates=None):
    return service.create_project(
        workspace_id="workspace-1",
        message="Compare sites for a 100 MW data center, 20-50 acres, resolution point outside flood, within 2 km of transmission and within 1 km of road, with sufficient grid capacity.",
        candidates=candidates or ["First Site", "Second Site"],
    )


def approve_enrichment(service, quoted):
    decision = quoted["active_decision"]
    return run(service.answer_decision(
        quoted["project_id"], decision["decision_id"], resume_token=decision["resume_token"], option_id="continue",
    ))


def test_candidate_provider_ingests_typed_inputs_and_pages_without_discovery():
    provider = UserSuppliedCandidateProvider()
    inputs = ["1 Main Street", "32.0, -97.0", "APN: 123-ABC", "https://example.com/listing"]

    first = provider.enumerate(inputs, limit=2)
    second = provider.enumerate(inputs, cursor=first["next_cursor"], limit=2)

    assert [item["input_type"] for item in first["items"] + second["items"]] == ["address", "coordinate", "apn", "url"]
    assert second["items"][1]["reconciliation_status"] == "UNSUPPORTED"
    assert first["source"] == "user_supplied"


def test_agent_tool_contracts_cover_project_and_existing_sandbox_workflow():
    project_tools = {item["name"] for item in DILIGENCE_TOOL_DEFINITIONS}
    sandbox_tools = {item["name"] for item in TOOL_DEFINITIONS}

    assert {
        "compile_project_request", "get_discovery_capabilities", "enumerate_supplied_candidates",
        "resolve_candidate", "plan_mireye_fields", "quote_mireye_enrichment",
        "confirm_and_fetch_enrichment", "evaluate_candidates", "rank_candidates",
        "compare_candidates", "check_evidence_freshness", "quote_mireye_refresh",
        "confirm_and_refresh_evidence", "get_evidence", "ask_mireye_site", "build_world_snapshot",
    } <= project_tools
    assert {"get_site_context", "propose_data_center", "transform_object", "optimize_layout", "evaluate_scenario", "branch_scenario", "compare_scenarios", "reset_proposals"} <= sandbox_tools
    assert project_tools.isdisjoint({"lookup", "fetch", "fetch_batch", "mireye_client"})


def test_mireye_site_question_uses_documented_coordinate_ask_contract(monkeypatch):
    client = MireyeClient(api_key="test", mode="live")
    captured = {}

    async def request(method, path, json_body=None, params=None):
        captured.update(method=method, path=path, json_body=json_body, params=params)
        return {"answer": "ok"}

    monkeypatch.setattr(client, "_request", request)
    result = run(client.ask(lat=32, lng=-97, question="What is known?", include_trace=True))

    assert result == {"answer": "ok"}
    assert captured == {"method": "POST", "path": "/v1/ask", "json_body": {"lat": 32.0, "lng": -97.0, "question": "What is known?", "include_trace": True}, "params": None}


def test_constraint_compiler_preserves_supported_and_unresolved_semantics():
    compiled = compile_project_request("Compare sites, 20-50 acres, resolution point outside flood, within 2 km of transmission, raw zoning codes I-2 or M-1, and sufficient grid capacity.")
    supported = {item["constraint_id"] for item in compiled["supported_constraints"]}
    unresolved = {item["constraint_id"] for item in compiled["unresolved_constraints"]}

    assert {"parcel_acreage_range", "resolution_point_outside_fema_sfha", "max_resolution_point_transmission_distance_m", "parcel_zoning_code_in"} <= supported
    assert "sufficient_grid_capacity" in unresolved
    zoning = next(item for item in compiled["constraints"] if item["constraint_id"] == "parcel_zoning_code_in")
    assert zoning["allowed_codes"] == ["I-2", "M-1"]


def test_project_brief_plans_requested_context_without_inventing_thresholds(diligence):
    service, _client, _store = diligence
    message = (
        "I am evaluating sites for a 100 MW AI data center in Texas. Compare these candidate properties "
        "on land size, flood exposure, wetlands, terrain, transmission proximity, road proximity, zoning, "
        "and any other relevant site intelligence available from MIREYE."
    )
    project = service.create_project(workspace_id="workspace-brief", message=message, candidates=["First Site"])
    plan = service.plan_fields(project["project_id"])
    constraint_ids = {item["constraint_id"] for item in project["request"]["constraints"]}

    assert {"land_size_context", "wetland_context", "terrain_context", "road_proximity", "zoning_context"} <= constraint_ids
    assert {"parcel_area_m2", "wetland_fraction_of_parcel", "wetland_acres_on_parcel", "elevation", "slope_degrees", "nearest_major_road_distance_m", "parcel_zoning"} <= set(plan["fields"])
    unresolved_ids = {item["constraint_id"] for item in project["request"]["unresolved_constraints"]}
    assert {"land_size_context", "wetland_context", "terrain_context", "road_proximity", "zoning_context"} <= unresolved_ids


def test_field_plan_is_constraint_driven_and_does_not_fetch_full_catalog(diligence):
    service, client, _store = diligence
    project = create_project(service)

    plan = service.plan_fields(project["project_id"])

    assert {"parcel_id", "parcel_boundary_geojson", "parcel_area_m2", "within_floodplain_polygon", "nearest_transmission_line_distance_m", "nearest_major_road_distance_m"} <= set(plan["fields"])
    assert "fiber_provider_count" not in plan["fields"]
    assert len(plan["fields"]) < len(DATA_CENTER_CONTEXT_FIELDS)
    assert client.lookup_calls == client.quote_calls == client.batch_calls == []


def test_resolution_and_enrichment_have_separate_approval_gates(diligence):
    service, client, _store = diligence
    project = create_project(service)

    with pytest.raises(ConfirmationRequired):
        run(service.resolve_and_quote(project["project_id"], confirmed_resolution=False))
    assert client.lookup_calls == []

    quoted = run(service.resolve_and_quote(project["project_id"], confirmed_resolution=True))
    assert quoted["status"] == "NEEDS_USER_DECISION"
    assert quoted["spend_plan"]["expected_credits"] == 20
    assert client.quote_calls[0]["fields"] == service.plan_fields(project["project_id"])["fields"]
    assert client.quote_calls[0]["preset"] is None
    assert client.batch_calls == []


def test_quote_planning_uses_verified_live_batch_size(diligence):
    service, client, _store = diligence
    candidates = [{"lat": 32.0 + index / 1000, "lng": -97.0} for index in range(5)]
    project = create_project(service, candidates)

    quoted = run(service.resolve_and_quote(project["project_id"], confirmed_resolution=True))

    assert [item["location_count"] for item in quoted["spend_plan"]["provider_quotes"]] == [2, 2, 1]
    assert [call["locations"] for call in client.quote_calls] == [2, 2, 1]
    assert quoted["spend_plan"]["batch_strategy"] == {"max_batch_size": 2, "batch_count": 3}

    with pytest.raises(ConfirmationRequired):
        run(service.confirm_and_fetch(project["project_id"], quoted["spend_plan"]["spend_plan_id"], confirmed=False))
    assert client.batch_calls == []


def test_batch_enrichment_preserves_partial_failure_and_ranks_deterministically(diligence):
    service, client, store = diligence
    project = create_project(service, ["First Site", "Second Site", "Broken Site"])
    quoted = run(service.resolve_and_quote(project["project_id"], confirmed_resolution=True))

    completed = approve_enrichment(service, quoted)

    assert completed["status"] == "NO_DECISION_YET"
    assert len(client.batch_calls) == 2
    states = {item["raw_input"]: item["reconciliation_status"] for item in completed["candidates"]}
    assert states == {"First Site": "ENRICHED", "Second Site": "ENRICHED", "Broken Site": "ENRICHMENT_FAILED"}
    assert completed["ranking"][0]["candidate_id"] == completed["candidates"][0]["candidate_id"]
    assert completed["ranking"][0]["outcome_counts"]["FAIL"] == 0
    assert completed["ranking"][1]["outcome_counts"]["FAIL"] > 0
    assert completed["ranking"][-1]["status"] == "ENRICHMENT_FAILED"
    assert store.get_site_snapshot(completed["candidates"][0]["snapshot_id"])["parcel_identity"]["parcel_match_type"] == "exact_intersect"


def test_failed_batch_candidate_requotes_without_repeating_lookup(diligence):
    service, client, _store = diligence
    project = create_project(service, ["Broken Site"])
    quoted = run(service.resolve_and_quote(project["project_id"], confirmed_resolution=True))
    failed = approve_enrichment(service, quoted)
    lookup_count = len(client.lookup_calls)

    retried = run(service.resolve_and_quote(
        project["project_id"], confirmed_resolution=True,
        retry_reason="Retry after the recorded batch enrichment failure.",
    ))

    assert failed["candidates"][0]["reconciliation_status"] == "ENRICHMENT_FAILED"
    assert retried["status"] == "NEEDS_USER_DECISION"
    assert len(client.lookup_calls) == lookup_count
    assert retried["candidates"][0]["reconciliation_status"] == "RESOLVED"


def test_candidate_handoff_watch_and_restart_safe_persistence(diligence):
    service, _client, store = diligence
    project = create_project(service, ["First Site"])
    quoted = run(service.resolve_and_quote(project["project_id"], confirmed_resolution=True))
    completed = approve_enrichment(service, quoted)
    candidate_id = completed["candidates"][0]["candidate_id"]

    handoff = service.open_candidate(project["project_id"], candidate_id)
    service.set_watch(project["project_id"], enabled=True)
    check = service.check_now(project["project_id"])
    restarted = DiligenceService(WorkspaceStore(store.db_path), service.sandbox, FakeWorlds()).get(project["project_id"])

    assert handoff["site_id"] == completed["candidates"][0]["site_id"]
    assert handoff["sandbox_url"].startswith("/sandbox/site_")
    assert check["candidate_states"][0]["status"] == "STALE_EVIDENCE"
    assert restarted["watch"]["enabled"] is True


def test_candidate_refresh_creates_t2_and_re_evaluates_project(monkeypatch, diligence):
    service, client, store = diligence
    project = create_project(service, ["First Site"])
    quoted = run(service.resolve_and_quote(project["project_id"], confirmed_resolution=True))
    completed = approve_enrichment(service, quoted)
    candidate = completed["candidates"][0]
    original_snapshot = store.get_site_snapshot(candidate["snapshot_id"])
    future = original_snapshot["observed_at"] + 61
    monkeypatch.setattr("app.sandbox.time.time", lambda: future)

    refresh_plan = run(service.quote_candidate_refresh(project["project_id"], candidate["candidate_id"]))
    refreshed = run(service.confirm_candidate_refresh(project["project_id"], candidate["candidate_id"], refresh_plan["spend_plan_id"], confirmed=True))
    updated_candidate = refreshed["project"]["candidates"][0]

    assert refresh_plan["requested_fields"] == sorted(set(completed["requested_fields"]) | {"parcel_id", "parcel_boundary_geojson", "parcel_match_type", "parcel_match_distance_m"})
    assert updated_candidate["snapshot_id"] != original_snapshot["snapshot_id"]
    assert updated_candidate["evaluation"]["site_snapshot_id"] == updated_candidate["snapshot_id"]
    assert store.get_site_snapshot(original_snapshot["snapshot_id"])["raw_response_hash"] == original_snapshot["raw_response_hash"]
    assert len(client.fetch_calls) == 1


class ScriptedModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def respond(self, input_items, tools):
        self.calls.append({"input": copy.deepcopy(input_items), "tools": tools})
        return self.replies.pop(0)


def tool(name, arguments, call_id):
    return ModelReply(message="", tool_calls=[{"id": call_id, "name": name, "arguments": json.dumps(arguments)}], response_items=[])


def test_single_orchestrator_runs_mocked_project_end_to_end(diligence):
    service, client, _store = diligence
    project = create_project(service, ["First Site", "Second Site"])
    project_id = project["project_id"]
    planning_model = ScriptedModel([
        tool("compile_project_request", {"project_id": project_id}, "compile"),
        tool("get_discovery_capabilities", {"project_id": project_id}, "capabilities"),
        tool("quote_mireye_enrichment", {"project_id": project_id}, "quote"),
        ModelReply(message="The supplied candidates are resolved and the MIREYE enrichment quote is ready for approval.", tool_calls=[], response_items=[]),
    ])
    agent = SandboxAgent(model=planning_model, diligence=service)

    planned = run(agent.chat_project(project_id, "session-1", "Plan this shortlist.", confirmed_resolution_project_id=project_id))
    assert planned["project"]["status"] == "NEEDS_USER_DECISION"
    assert client.batch_calls == []
    approve_enrichment(service, planned["project"])

    execution_model = ScriptedModel([
        tool("rank_candidates", {"project_id": project_id}, "rank"),
        ModelReply(message="The shortlist is ranked from deterministic evidence; grid capacity remains unresolved.", tool_calls=[], response_items=[]),
    ])
    agent.model = execution_model
    completed = run(agent.chat_project(project_id, "session-1", "Rank the approved enrichment."))

    assert completed["project"]["status"] == "NO_DECISION_YET"
    assert [item["tool"] for item in planned["tool_trace"]] == ["compile_project_request", "get_discovery_capabilities", "quote_mireye_enrichment"]
    assert [item["tool"] for item in completed["tool_trace"]] == ["rank_candidates"]
    assert all(definition["name"] not in {"fetch", "fetch_batch", "lookup"} for definition in DILIGENCE_TOOL_DEFINITIONS)


def test_agent_cannot_mint_resolution_or_enrichment_confirmation(diligence):
    service, client, _store = diligence
    project = create_project(service, ["First Site"])
    project_id = project["project_id"]
    model = ScriptedModel([tool("quote_mireye_enrichment", {"project_id": project_id}, "quote")])

    response = run(SandboxAgent(model=model, diligence=service).chat_project(project_id, "session", "Resolve everything."))

    assert response["tool_trace"][0]["status"] == "rejected"
    assert client.lookup_calls == client.batch_calls == []


def test_diligence_http_flow_uses_service_and_preserves_approval(monkeypatch, diligence):
    service, client, _store = diligence
    monkeypatch.setattr(main, "diligence_service", service)
    http = TestClient(main.app)

    created = http.post("/v1/diligence/projects", json={
        "workspace_id": "workspace-http", "message": "Compare 20-50 acre sites.", "candidates": ["First Site"],
    })
    assert created.status_code == 200
    project_id = created.json()["project_id"]
    denied = http.post(f"/v1/diligence/projects/{project_id}/plan", json={"confirmed_resolution": False})
    assert denied.status_code == 409
    assert client.lookup_calls == []

    quoted = http.post(f"/v1/diligence/projects/{project_id}/plan", json={"confirmed_resolution": True})
    assert quoted.status_code == 200
    decision = quoted.json()["active_decision"]
    completed = http.post(
        f"/v1/diligence/projects/{project_id}/decisions/{decision['decision_id']}/answer",
        json={"resume_token": decision["resume_token"], "option_id": "continue"},
    )
    assert completed.status_code == 200
    assert completed.json()["spend_plan"]["status"] == "COMPLETED"
    assert http.get(f"/v1/diligence/projects/{project_id}/candidates").json()["total"] == 1


def test_threshold_free_request_requires_clarification_before_ranking(diligence):
    service, client, _store = diligence
    project = service.create_project(
        workspace_id="workspace-clarify",
        message="Compare sites on land size, flood exposure, wetlands, terrain, transmission proximity, road proximity, and zoning.",
        candidates=["First Site"],
    )

    planned = run(service.resolve_and_quote(project["project_id"], confirmed_resolution=True))

    assert planned["status"] == "NEEDS_USER_DECISION"
    assert planned["decision"]["status"] == "NO_DECISION_YET"
    assert planned["ranking"] == []
    assert planned["request"]["requirement_gaps"]
    assert planned["active_decision"] is None
    assert client.lookup_calls == client.quote_calls == []

    defaulted = compile_project_request("Compare transmission proximity and road proximity using reasonable defaults.")
    assert defaulted["requirement_status"] == "REVIEW_REQUIRED"
    assert defaulted["assumptions_permitted"] is True


def test_all_unresolved_candidates_return_no_decision_and_no_winner(diligence):
    service, _client, _store = diligence
    project = service.create_project(workspace_id="workspace-unresolved", message="Compare sites within 2 km of transmission.", candidates=["First Site", "Second Site"])
    project = service.get(project["project_id"])
    for candidate in project["candidates"]:
        candidate.update(
            reconciliation_status="ENRICHED",
            evaluation={"overall_status": "UNRESOLVED", "constraint_results": [{"constraint_id": "max_resolution_point_transmission_distance_m", "outcome": "UNRESOLVED"}]},
        )
    service._save(project)

    result = service.rank_candidates(project["project_id"])

    assert result["decision"]["status"] == "NO_DECISION_YET"
    assert "winner_candidate_id" not in result["decision"]
    assert all(item["rank"] for item in result["ranking"])


def test_valid_thresholded_ranking_allows_unique_fully_passing_winner(diligence):
    service, _client, _store = diligence
    project = service.create_project(workspace_id="workspace-winner", message="Compare sites within 2 km of transmission.", candidates=["First Site", "Second Site"])
    project = service.get(project["project_id"])
    first, second = project["candidates"]
    first.update(reconciliation_status="ENRICHED", evaluation={"overall_status": "PASS", "constraint_results": [{"constraint_id": "max_resolution_point_transmission_distance_m", "outcome": "PASS"}]})
    second.update(reconciliation_status="ENRICHED", evaluation={"overall_status": "FAIL", "constraint_results": [{"constraint_id": "max_resolution_point_transmission_distance_m", "outcome": "FAIL"}]})
    service._save(project)

    result = service.rank_candidates(project["project_id"])

    assert result["decision"] == {
        "status": "DECISION_READY", "winner_candidate_id": first["candidate_id"],
        "reason": "This is the unique fully passing candidate under the explicit requirements.",
    }


def test_material_canonical_address_mismatch_requires_confirmation(diligence):
    service, _client, _store = diligence
    project = service.create_project(workspace_id="workspace-address", message="Compare 20-50 acre sites.", candidates=["100 Main Street, Austin, TX"])
    quoted = run(service.resolve_and_quote(project["project_id"], confirmed_resolution=True))
    completed = approve_enrichment(service, quoted)
    candidate = completed["candidates"][0]

    assert candidate["reconciliation_status"] == "ADDRESS_CONFIRMATION_REQUIRED"
    assert candidate["address_reconciliation"]["submitted_address"] == "100 Main Street, Austin, TX"
    assert candidate["address_reconciliation"]["canonical_address"] == "32 Test Road"
    assert candidate["address_reconciliation"]["status"] == "CONFIRMATION_REQUIRED"
    with pytest.raises(DiligenceError, match="address mismatch confirmed"):
        service.open_candidate(project["project_id"], candidate["candidate_id"])

    confirmed = service.confirm_canonical_address(project["project_id"], candidate["candidate_id"], confirmed=True)
    assert confirmed["candidates"][0]["reconciliation_status"] == "ENRICHED"
    assert confirmed["candidates"][0]["address_reconciliation"]["status"] == "CONFIRMED"


def test_failed_batch_never_escalates_above_configured_size(diligence):
    service, client, _store = diligence
    project = service.create_project(
        workspace_id="workspace-batch",
        message="Compare 20-50 acre sites.",
        candidates=[{"lat": 32.0 + index / 1000, "lng": -97.0} for index in range(3)],
    )
    batch_sizes = []

    async def fail_batch(*, locations, fields, preset=None):
        batch_sizes.append(len(locations))
        raise RuntimeError("provider batch failure")

    client.fetch_batch = fail_batch
    quoted = run(service.resolve_and_quote(project["project_id"], confirmed_resolution=True))
    completed = approve_enrichment(service, quoted)

    assert quoted["spend_plan"]["batch_strategy"] == {"max_batch_size": 2, "batch_count": 2}
    assert batch_sizes == [2, 1]
    assert all(candidate["reconciliation_status"] == "ENRICHMENT_FAILED" for candidate in completed["candidates"])


def requirement_value(constraint_id, **values):
    return {"constraint_id": constraint_id, **values}


def number_decision(question, *, target="max_resolution_point_transmission_distance_m", field="max_distance_m", unit="m"):
    return {
        "kind": "clarification", "question": question, "context": "A numeric limit is needed for deterministic comparison.",
        "why_it_matters": "Without a limit, proximity cannot produce a deterministic outcome.", "risk_level": "MEDIUM",
        "blocking": False, "input_mode": "number", "options": [], "recommended_option_id": None,
        "allow_custom": True, "custom_schema": {"constraint_id": target, "fields": [{
            "name": field, "label": "Maximum distance", "type": "number", "unit": unit, "minimum": 0, "maximum": 10_000_000,
        }]}, "constraint_targets": [target],
    }


def choice_decision(question):
    return {
        "kind": "clarification", "question": question, "context": "Available evidence has different spatial scopes.",
        "why_it_matters": "The selected scope controls what the evaluator can prove.", "risk_level": "MEDIUM",
        "blocking": False, "input_mode": "single_choice", "options": [
            {"id": "point", "label": "Use the point signal", "description": "Evaluate only the resolved point.", "value": requirement_value("resolution_point_outside_fema_sfha"), "consequence": "This does not prove the whole parcel."},
            {"id": "parcel", "label": "Keep parcel-wide proof", "description": "Retain the stronger requested scope.", "value": requirement_value("parcel_outside_fema_sfha"), "consequence": "The result will remain unresolved with current evidence."},
        ], "recommended_option_id": "point", "allow_custom": False, "custom_schema": None,
        "constraint_targets": ["resolution_point_outside_fema_sfha", "parcel_outside_fema_sfha"],
    }


def text_decision(question):
    return {
        "kind": "clarification", "question": question, "context": "Only explicit raw zoning codes can be compared.",
        "why_it_matters": "The evaluator cannot infer industrial use from an unmapped code.", "risk_level": "MEDIUM",
        "blocking": False, "input_mode": "text", "options": [], "recommended_option_id": None,
        "allow_custom": True, "custom_schema": {"constraint_id": "parcel_zoning_code_in", "fields": [{
            "name": "allowed_codes", "label": "Allowed raw zoning codes", "type": "string_list", "unit": None, "minimum": None, "maximum": None,
        }]}, "constraint_targets": ["parcel_zoning_code_in"],
    }


def ask_model(service, project, decision):
    project_id = project["project_id"]
    model = ScriptedModel([
        tool("compile_project_request", {"project_id": project_id}, "compile"),
        tool("request_user_decision", {"project_id": project_id, "mode": "ASK_USER", "decision_request": decision, "assumptions": None}, "ask"),
    ])
    return run(SandboxAgent(model=model, diligence=service).chat_project(project_id, "session", "Continue safely."))


def test_dynamic_decision_same_capability_allows_context_specific_questions(diligence):
    service, _client, _store = diligence
    first = service.create_project(workspace_id="workspace-context-a", message="Compare sites close to transmission for an initial phase.", candidates=["First Site"])
    second = service.create_project(workspace_id="workspace-context-b", message="Compare sites close to transmission with room for expansion.", candidates=["Second Site"])

    first_response = ask_model(service, first, number_decision("What transmission distance should qualify for the initial phase?"))
    second_response = ask_model(service, second, number_decision("How far may an expansion site be from transmission?"))

    assert first_response["project"]["active_decision"]["question"] != second_response["project"]["active_decision"]["question"]
    assert first_response["project"]["active_decision"]["constraint_targets"] == second_response["project"]["active_decision"]["constraint_targets"]


def test_dynamic_decision_model_selects_number_choice_and_text_modes(diligence):
    service, _client, _store = diligence
    transmission = service.create_project(workspace_id="workspace-mode-number", message="Compare sites close to transmission.", candidates=["First Site"])
    flood = service.create_project(workspace_id="workspace-mode-choice", message="Compare sites on flood risk.", candidates=["First Site"])
    zoning = service.create_project(workspace_id="workspace-mode-text", message="Compare sites by zoning.", candidates=["First Site"])

    decisions = [
        ask_model(service, transmission, number_decision("What distance should I use?"))["project"]["active_decision"],
        ask_model(service, flood, choice_decision("Which flood evidence scope should I apply?"))["project"]["active_decision"],
        ask_model(service, zoning, text_decision("Which raw zoning codes should qualify?"))["project"]["active_decision"],
    ]

    assert [item["input_mode"] for item in decisions] == ["number", "single_choice", "text"]


def test_dynamic_decision_generated_options_are_schema_validated(diligence):
    service, _client, _store = diligence
    project = service.create_project(workspace_id="workspace-options", message="Compare sites on flood risk.", candidates=["First Site"])

    decision = ask_model(service, project, choice_decision("Which scope fits this screening pass?"))["project"]["active_decision"]

    assert {item["value"]["constraint_id"] for item in decision["options"]} == {
        "resolution_point_outside_fema_sfha", "parcel_outside_fema_sfha",
    }
    assert decision["recommended_option_id"] in {item["id"] for item in decision["options"]}


def test_dynamic_decision_invalid_generated_schema_is_rejected(diligence):
    service, _client, _store = diligence
    project = service.create_project(workspace_id="workspace-invalid", message="Compare sites close to transmission.", candidates=["First Site"])
    invalid = number_decision("What distance should qualify?", unit="km")

    with pytest.raises(DiligenceError, match="type or unit"):
        service.agent_decision(project["project_id"], mode="ASK_USER", decision_request=invalid)

    assert service.get(project["project_id"])["active_decision"] is None


def test_unknown_model_decision_target_is_never_persisted(diligence):
    service, _client, _store = diligence
    project = service.create_project(
        workspace_id="workspace-invalid-target",
        message="Screen one site for a 100 MW data center.",
        candidates=["First Site"],
    )
    invalid = number_decision(
        "What unsupported capability should be used?",
        target="model_created_constraint",
        field="invented_value",
    )

    with pytest.raises(DiligenceError, match="unavailable constraint capability"):
        service.agent_decision(project["project_id"], mode="ASK_USER", decision_request=invalid)

    assert service.get(project["project_id"])["active_decision"] is None


def test_grid_evidence_gap_becomes_application_owned_user_action(diligence):
    service, _client, _store = diligence
    project = service.create_project(
        workspace_id="workspace-grid-gap",
        message="Screen one site for a 100 MW data center within 2 km of transmission.",
        candidates=["First Site"],
    )
    proposal = choice_decision("How should I prove 100 MW deliverability?")
    proposal["constraint_targets"] = ["sufficient_grid_capacity"]
    proposal["options"] = [{
        "id": "invented", "label": "Invented model choice", "description": "Must not become canonical.",
        "value": requirement_value("sufficient_grid_capacity"), "consequence": "No application authority.",
    }]
    proposal["recommended_option_id"] = "invented"

    result = service.agent_decision(project["project_id"], mode="ASK_USER", decision_request=proposal)
    decision = result["decision_request"]

    assert decision["originating_step"] == "project_intelligence"
    assert {item["id"] for item in decision["options"]} == {"authorize_next_action", "keep_unresolved"}
    assert service.get(project["project_id"])["active_decision"]["resume_action"]["type"] == "project_action"


def test_mixed_unknown_decision_target_is_removed_before_canonical_action_is_persisted(diligence):
    service, _client, _store = diligence
    project = service.create_project(
        workspace_id="workspace-grid-mixed-target",
        message="Screen one site for a 100 MW data center.",
        candidates=["First Site"],
    )
    proposal = choice_decision("How should I prove 100 MW deliverability?")
    proposal["constraint_targets"] = ["sufficient_grid_capacity", "model_created_constraint"]

    decision = service.agent_decision(
        project["project_id"], mode="ASK_USER", decision_request=proposal,
    )["decision_request"]

    assert decision["constraint_targets"] == ["sufficient_grid_capacity"]
    assert "model_created_constraint" not in str(service.get(project["project_id"])["active_decision"])


def test_metered_operation_identity_reuses_success_and_bounds_retry(diligence):
    service, _client, _store = diligence
    project = service.create_project(
        workspace_id="workspace-metered-guard",
        message="Compare sites within 2 km of transmission.",
        candidates=["First Site"],
    )
    stored = service.get(project["project_id"])
    request = {"candidate_id": stored["candidates"][0]["candidate_id"], "value": "First Site"}
    first = service._begin_metered_operation(stored, "MIREYE_RESOLVE", request, confirmed=True, retry_reason=None)
    first.update(status="SUCCEEDED", result={"reconciliation_status": "RESOLVED"})
    assert service._begin_metered_operation(stored, "MIREYE_RESOLVE", request, confirmed=True, retry_reason=None) is first

    failed_request = {"candidate_id": "candidate_failed", "value": "Failed Site"}
    failed = service._begin_metered_operation(stored, "MIREYE_RESOLVE", failed_request, confirmed=True, retry_reason=None)
    failed["status"] = "FAILED"
    with pytest.raises(DiligenceError, match="explicit reason"):
        service._begin_metered_operation(stored, "MIREYE_RESOLVE", failed_request, confirmed=True, retry_reason=None)
    retried = service._begin_metered_operation(
        stored, "MIREYE_RESOLVE", failed_request, confirmed=True, retry_reason="Provider timeout recorded for this task.",
    )
    retried["status"] = "FAILED"
    with pytest.raises(DiligenceError, match="retry limit"):
        service._begin_metered_operation(
            stored, "MIREYE_RESOLVE", failed_request, confirmed=True, retry_reason="Second provider timeout was recorded.",
        )


def test_dynamic_decision_resumes_same_project(diligence):
    service, _client, _store = diligence
    project = service.create_project(workspace_id="workspace-resume", message="Compare sites on flood risk.", candidates=["First Site"])
    candidate_ids = [item["candidate_id"] for item in project["candidates"]]
    decision = ask_model(service, project, choice_decision("Which evidence scope should drive this pass?"))["project"]["active_decision"]

    resumed = run(service.answer_decision(
        project["project_id"], decision["decision_id"], resume_token=decision["resume_token"], option_id="point",
    ))

    assert resumed["project_id"] == project["project_id"]
    assert [item["candidate_id"] for item in resumed["candidates"]] == candidate_ids
    assert resumed["agent_state"]["resume_count"] == 1
    assert resumed["active_decision"] is None


def test_dynamic_decision_user_answer_becomes_typed_constraint(diligence):
    service, _client, _store = diligence
    project = service.create_project(workspace_id="workspace-typed", message="Compare sites close to transmission.", candidates=["First Site"])
    decision = ask_model(service, project, number_decision("What maximum distance should qualify?"))["project"]["active_decision"]

    resumed = run(service.answer_decision(
        project["project_id"], decision["decision_id"], resume_token=decision["resume_token"], value=2750.0,
    ))

    assert {"constraint_id": "max_resolution_point_transmission_distance_m", "max_distance_m": 2750.0} in resumed["request"]["constraints"]
    assert resumed["request"]["decisions"][-1]["source"] == "USER"


def test_dynamic_decision_free_text_is_model_interpreted_then_validated(diligence):
    service, _client, _store = diligence
    project = service.create_project(workspace_id="workspace-text-answer", message="Compare sites by zoning.", candidates=["First Site"])
    decision = ask_model(service, project, text_decision("Which raw zoning codes should qualify?"))["project"]["active_decision"]
    interpreter = ScriptedModel([tool(
        "submit_decision_answer", {"constraint": requirement_value("parcel_zoning_code_in", allowed_codes=["I-2", "M-1"])}, "submit",
    )])

    resumed = run(SandboxAgent(model=interpreter, diligence=service).interpret_project_decision_answer(
        project["project_id"], decision["decision_id"], resume_token=decision["resume_token"], text="Use I-2 and M-1.",
    ))

    assert {"constraint_id": "parcel_zoning_code_in", "allowed_codes": ["I-2", "M-1"]} in resumed["request"]["constraints"]


def test_dynamic_decision_resume_does_not_repeat_mireye_work(diligence):
    service, client, _store = diligence
    project = service.create_project(workspace_id="workspace-once", message="Compare sites within 2 km of transmission.", candidates=["First Site"])
    quoted = run(service.resolve_and_quote(project["project_id"], confirmed_resolution=True))
    decision = quoted["active_decision"]
    prior = (len(client.lookup_calls), len(client.quote_calls))

    completed = run(service.answer_decision(
        project["project_id"], decision["decision_id"], resume_token=decision["resume_token"], option_id="continue",
    ))

    assert (len(client.lookup_calls), len(client.quote_calls)) == prior
    assert len(client.batch_calls) == 1
    assert completed["spend_plan"]["status"] == "COMPLETED"


def test_dynamic_decision_low_risk_assumption_has_provenance(diligence):
    service, _client, _store = diligence
    project = service.create_project(
        workspace_id="workspace-assumption", message="Compare sites close to transmission using reasonable assumptions.", candidates=["First Site"],
    )
    assumptions = [{
        "assumption": "Use a project-specific transmission screening radius.", "reason": "The user authorized a reasonable screening assumption.",
        "confidence": "MEDIUM", "overridable": True,
        "constraint": requirement_value("max_resolution_point_transmission_distance_m", max_distance_m=3200.0),
    }]

    result = service.agent_decision(project["project_id"], mode="ASSUME_AND_CONTINUE", assumptions=assumptions)

    assert result["assumptions"][0]["source"] == "AGENT_ASSUMPTION"
    assert result["assumptions"][0]["authorized_by"] == "USER_REQUEST"
    assert result["assumptions"][0]["constraint"]["max_distance_m"] == 3200.0


def test_dynamic_decision_hard_blocks_remain_application_controlled(diligence):
    service, client, _store = diligence
    ambiguous = service.create_project(workspace_id="workspace-no-hardblock", message="Compare sites on flood risk.", candidates=["First Site"])
    with pytest.raises(DiligenceError, match="Only the application"):
        service.agent_decision(ambiguous["project_id"], mode="HARD_BLOCK", decision_request=choice_decision("Confirm this?"))

    quoted_project = service.create_project(workspace_id="workspace-cost-block", message="Compare sites within 2 km of transmission.", candidates=["First Site"])
    quoted = run(service.resolve_and_quote(quoted_project["project_id"], confirmed_resolution=True))
    with pytest.raises(ConfirmationRequired, match="answered cost DecisionRequest"):
        run(service.confirm_and_fetch(quoted_project["project_id"], quoted["spend_plan"]["spend_plan_id"], confirmed=True))
    assert client.batch_calls == []


def test_dynamic_decision_no_hardcoded_question_dictionary_remains():
    source = (Path(__file__).parents[1] / "app" / "diligence.py").read_text(encoding="utf-8")

    assert "REQUIREMENT_DECISIONS" not in source
    assert "CLARIFICATION_QUESTIONS" not in source
    assert all("question" not in capability for capability in CONSTRAINT_CAPABILITIES.values())


def test_dynamic_decision_capabilities_have_no_fixed_conversation_options():
    forbidden = {"options", "recommended_option", "recommended_option_id", "question", "context", "why_it_matters"}

    assert all(forbidden.isdisjoint(capability) for capability in CONSTRAINT_CAPABILITIES.values())
    assert all("default" not in key for capability in CONSTRAINT_CAPABILITIES.values() for key in capability)


def test_dynamic_decision_persistence_survives_project_reload(diligence):
    service, _client, store = diligence
    project = service.create_project(workspace_id="workspace-reload", message="Compare sites close to transmission.", candidates=["First Site"])
    decision = ask_model(service, project, number_decision("What limit should this project use?"))["project"]["active_decision"]

    reloaded = DiligenceService(store, service.sandbox, FakeWorlds()).get(project["project_id"])
    frontend = (Path(__file__).parents[1] / "app" / "static" / "app.js").read_text(encoding="utf-8")

    assert reloaded["active_decision"] == decision
    assert "mireye-active-project-id" in frontend
    assert "/v1/diligence/projects/" in frontend


def test_candidate_resolution_ux_maps_each_candidate_status_and_reason(diligence):
    service, _client, _store = diligence
    project = service.create_project(
        workspace_id="workspace-resolution-statuses", message="Compare sites within 2 km of transmission.",
        candidates=["Exact", "Confirm", "Ambiguous", "Missing", "Failed"],
    )
    project = service.get(project["project_id"])
    project["candidates"][0]["reconciliation_status"] = "RESOLVED"
    project["candidates"][1].update(
        reconciliation_status="ADDRESS_CONFIRMATION_REQUIRED",
        address_reconciliation={"submitted_address": "100 Main St", "canonical_address": "200 Main St", "parcel_id": "parcel-1", "match_type": "exact_intersect", "match_distance_m": 0.0},
    )
    project["candidates"][2].update(reconciliation_status="AMBIGUOUS", resolution_options=[])
    project["candidates"][3]["reconciliation_status"] = "NOT_FOUND"
    project["candidates"][4]["reconciliation_status"] = "ERROR"

    view = service._save(project)["candidate_resolution"]

    assert [item["status"] for item in view["items"]] == ["EXACT_MATCH", "NEEDS_CONFIRMATION", "AMBIGUOUS", "UNRESOLVED", "FAILED"]
    assert [item["reason"] for item in view["items"][1:]] == [
        "MIREYE returned a different canonical parcel address.", "Multiple parcel candidates were returned.",
        "No exact parcel match was found.", "MIREYE request failed.",
    ]
    assert view["items"][1]["details"]["parcel_id"] == "parcel-1"


def test_candidate_resolution_ux_partial_success_continues_to_quote(diligence):
    service, client, _store = diligence
    project = service.create_project(
        workspace_id="workspace-partial-resolution", message="Compare sites within 2 km of transmission.",
        candidates=["First Site", "Ambiguous Site"],
    )

    quoted = run(service.resolve_and_quote(project["project_id"], confirmed_resolution=True))

    assert quoted["spend_plan"]["candidate_count"] == 1
    assert len(client.quote_calls) == 1
    assert [item["status"] for item in quoted["candidate_resolution"]["items"]] == ["EXACT_MATCH", "AMBIGUOUS"]
    assert quoted["candidate_resolution"]["exact_count"] == 1


def test_candidate_resolution_ux_ambiguous_choice_requires_explicit_selection(diligence):
    service, _client, _store = diligence
    project = service.create_project(
        workspace_id="workspace-ambiguous-choice", message="Compare sites within 2 km of transmission.", candidates=["Ambiguous Site"],
    )
    project = service.get(project["project_id"])
    project["candidates"][0].update(reconciliation_status="AMBIGUOUS", resolution_options=[
        {"address": "101 Alpha Rd", "parcel_id": "parcel-a", "lat": 32.0, "lng": -97.0, "parcel_match_distance_m": 0.0},
        {"address": "202 Beta Rd", "parcel_id": "parcel-b", "lat": 32.1, "lng": -97.1, "parcel_match_distance_m": 12.0},
    ])
    saved = service._save(project)

    choices = saved["candidate_resolution"]["items"][0]["choices"]
    assert saved["candidates"][0]["selected_location"] is None
    assert choices[1] == {"index": 1, "address": "202 Beta Rd", "parcel_id": "parcel-b", "lat": 32.1, "lng": -97.1, "match_distance_m": 12.0}

    selected = run(service.select_resolution(project["project_id"], project["candidates"][0]["candidate_id"], 1))
    assert selected["candidates"][0]["selected_location"]["parcel_id"] == "parcel-b"


def test_candidate_resolution_ux_canonical_mismatch_details_and_reject(diligence):
    service, _client, _store = diligence
    project = service.create_project(
        workspace_id="workspace-canonical-resolution", message="Compare 20-50 acre sites.", candidates=["100 Main Street, Austin, TX"],
    )
    quoted = run(service.resolve_and_quote(project["project_id"], confirmed_resolution=True))
    cost = quoted["active_decision"]
    mismatch = run(service.answer_decision(
        project["project_id"], cost["decision_id"], resume_token=cost["resume_token"], option_id="continue",
    ))
    item = mismatch["candidate_resolution"]["items"][0]

    assert item["status"] == "NEEDS_CONFIRMATION"
    assert item["details"] == {
        "submitted_address": "100 Main Street, Austin, TX", "canonical_address": "32 Test Road",
        "parcel_id": "parcel-32", "match_type": "exact_intersect", "match_distance_m": 0.0,
        "status": "CONFIRMATION_REQUIRED",
    }

    decision = mismatch["active_decision"]
    rejected = run(service.answer_decision(
        project["project_id"], decision["decision_id"], resume_token=decision["resume_token"], option_id="reject",
    ))
    assert rejected["status"] != "CANCELLED"
    assert rejected["candidate_resolution"]["items"][0]["status"] == "UNRESOLVED"
    persisted = service.get(project["project_id"])
    assert persisted["active_decision"] is None
    assert persisted["decision_history"][-1]["status"] == "ANSWERED"


def test_candidate_resolution_ux_frontend_is_specific_and_actionable():
    root = Path(__file__).parents[1]
    markup = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
    script = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="candidateResolution"' in markup
    assert "Confirm this parcel" in script
    assert "data-option-index" in script
    assert "Submitted address" in script and "MIREYE canonical address" in script
    assert "Candidate resolution needs attention" not in script


def _phase10_enrich(service, message, workspace_id="workspace-phase10"):
    project = service.create_project(workspace_id=workspace_id, message=message, candidates=["First Site"])
    quoted = run(service.resolve_and_quote(project["project_id"], confirmed_resolution=True))
    decision = quoted["active_decision"]
    return run(service.answer_decision(
        project["project_id"], decision["decision_id"], resume_token=decision["resume_token"], option_id="continue",
    ))


def _phase10_blocked_project(service, workspace_id="workspace-phase10-blocked"):
    return _phase10_enrich(
        service,
        "Compare this site for a 100 MW data center, 20-50 acres, within 2 km of transmission, "
        "within 1 km of road, with sufficient grid capacity, water capacity, and fiber diversity.",
        workspace_id,
    )


def test_phase10_complete_evidence_coverage(diligence):
    service, _client, _store = diligence
    project = _phase10_enrich(
        service,
        "Compare this site, 20-50 acres, resolution point outside flood, within 2 km of transmission, "
        "within 1 km of road, raw zoning codes I-2 or M-1.",
        "workspace-phase10-complete",
    )
    coverage = project["project_intelligence"]["evidence_coverage"]

    assert coverage
    assert all(item["coverage"] == "COMPLETE" and item["decision_provable"] for item in coverage)
    assert project["project_intelligence"]["project_readiness_state"] == "READY_FOR_CURRENT_DECISION"


def test_phase10_partial_and_missing_critical_evidence_create_gap(diligence):
    service, _client, _store = diligence
    project = _phase10_blocked_project(service)
    intelligence = project["project_intelligence"]
    grid = next(item for item in intelligence["evidence_coverage"] if item["requirement_id"] == "sufficient_grid_capacity")
    gap = next(item for item in intelligence["unresolved_issues"] if item["requirement_id"] == "sufficient_grid_capacity")

    assert grid["coverage"] == "PARTIAL"
    assert grid["status"] == "UNRESOLVED"
    assert "utility_confirmed_deliverable_capacity_mw" in grid["missing_evidence"]
    assert gap["blocking"] is True and gap["impact"] == "CRITICAL"


def test_phase10_evidence_gap_deduplicates_across_recalculation(diligence):
    service, _client, _store = diligence
    project = _phase10_blocked_project(service, "workspace-phase10-dedupe")
    first = next(item for item in project["project_intelligence"]["unresolved_issues"] if item["requirement_id"] == "sufficient_grid_capacity")
    second_state = service.evaluate_evidence_coverage(project["project_id"])
    second = next(item for item in second_state["unresolved_issues"] if item["requirement_id"] == "sufficient_grid_capacity")

    assert second["gap_id"] == first["gap_id"]
    assert second["created_at"] == first["created_at"]
    assert len({item["gap_id"] for item in second_state["evidence_gaps"]}) == len(second_state["evidence_gaps"])


def test_phase10_next_action_ranking_is_deterministic_and_prioritizes_critical_blocker(diligence):
    service, _client, _store = diligence
    project = _phase10_blocked_project(service, "workspace-phase10-ranking")
    first = service.next_actions(project["project_id"])
    second = service.next_actions(project["project_id"])

    assert [(item["action_id"], item["score"]) for item in first["prioritized_actions"]] == [
        (item["action_id"], item["score"]) for item in second["prioritized_actions"]
    ]
    assert first["prioritized_actions"][0]["type"] == "UTILITY_CAPACITY_INTERCONNECTION_RFI"
    assert first["prioritized_actions"][0]["score_provenance"]["critical_milestone"]["blocking"] is True


def test_phase10_project_readiness_is_categorical_without_fake_percentage(diligence):
    service, _client, _store = diligence
    project = _phase10_blocked_project(service, "workspace-phase10-readiness")
    intelligence = project["project_intelligence"]

    assert intelligence["project_readiness_state"] == "BLOCKED"
    assert intelligence["readiness"]["Power"]["status"] == "CRITICAL"
    assert intelligence["risk_state"]["critical_blockers"] >= 1
    assert "percentage" not in json.dumps(intelligence).casefold()


def test_phase10_generated_rfi_references_validated_missing_evidence(diligence):
    service, _client, _store = diligence
    project = _phase10_blocked_project(service, "workspace-phase10-rfi")
    action = service.next_actions(project["project_id"])["prioritized_actions"][0]
    draft = service.create_rfi_draft(
        project["project_id"], action["action_id"],
        "Please confirm the capacity and interconnection pathway available for this proposed 100 MW site and identify the required study process.",
    )

    assert draft["type"] == "UTILITY_CAPACITY_INTERCONNECTION_RFI"
    assert draft["required_evidence"] == action["required_evidence"]
    assert "utility_confirmed_deliverable_capacity_mw" in draft["required_evidence"]
    assert draft["human_approval_required"] is True and draft["status"] == "DRAFT"
    refreshed_action = next(item for item in service.evaluate_evidence_coverage(project["project_id"])["recommended_actions"] if item["action_id"] == action["action_id"])
    assert refreshed_action["status"] == "DRAFTED" and refreshed_action["rfi_id"] == draft["rfi_id"]


def test_phase10_agent_generates_rfi_wording_for_validated_action(diligence):
    service, _client, _store = diligence
    project = _phase10_blocked_project(service, "workspace-phase10-agent-rfi")
    action = service.next_actions(project["project_id"])["prioritized_actions"][0]
    request_text = "Please confirm deliverable capacity for the proposed 100 MW phase and identify the applicable interconnection studies and required submissions."
    model = ScriptedModel([
        tool("get_next_actions", {"project_id": project["project_id"]}, "actions"),
        tool("draft_project_rfi", {"project_id": project["project_id"], "action_id": action["action_id"], "generated_request": request_text}, "draft"),
        ModelReply(message="The utility request is drafted for your review; it has not been sent.", tool_calls=[], response_items=[]),
    ])

    response = run(SandboxAgent(model=model, diligence=service).chat_project(project["project_id"], "phase10-rfi", "Draft the highest-priority request."))

    assert [item["tool"] for item in response["tool_trace"]] == ["get_next_actions", "draft_project_rfi"]
    assert response["project"]["rfis"][0]["generated_request"] == request_text
    assert response["project"]["rfis"][0]["human_approval_required"] is True


def test_phase10_agent_explains_unresolved_requirement_from_structured_state(diligence):
    service, _client, _store = diligence
    project = _phase10_blocked_project(service, "workspace-phase10-agent")
    model = ScriptedModel([
        tool("get_project_intelligence", {"project_id": project["project_id"]}, "intelligence"),
        ModelReply(message="Power remains unresolved because utility-confirmed deliverability is missing. The first next action is a utility interconnection request.", tool_calls=[], response_items=[]),
    ])

    response = run(SandboxAgent(model=model, diligence=service).chat_project(project["project_id"], "phase10-agent", "Why is this site not ready?"))

    assert response["tool_trace"][0]["tool"] == "get_project_intelligence"
    assert "utility-confirmed deliverability is missing" in response["message"]


def test_phase10_agent_cannot_invent_pass_fail_result(diligence):
    service, _client, _store = diligence
    project = _phase10_blocked_project(service, "workspace-phase10-authority")
    model = ScriptedModel([ModelReply(message="100 MW deliverability: PASS", tool_calls=[], response_items=[])])

    response = run(SandboxAgent(model=model, diligence=service).chat_project(project["project_id"], "phase10-authority", "Is power ready?"))

    assert response["message"].startswith("I cannot support that outcome claim")
    grid = next(item for item in response["project"]["project_intelligence"]["evidence_coverage"] if item["requirement_id"] == "sufficient_grid_capacity")
    assert grid["status"] == "UNRESOLVED"


def test_phase10_refresh_change_recalculates_affected_project_state(monkeypatch, diligence):
    service, _client, store = diligence
    project = _phase10_blocked_project(service, "workspace-phase10-refresh")
    candidate = project["candidates"][0]
    original = store.get_site_snapshot(candidate["snapshot_id"])
    monkeypatch.setattr("app.sandbox.time.time", lambda: original["observed_at"] + 61)

    plan = run(service.quote_candidate_refresh(project["project_id"], candidate["candidate_id"]))
    refreshed = run(service.confirm_candidate_refresh(project["project_id"], candidate["candidate_id"], plan["spend_plan_id"], confirmed=True))
    impact = refreshed["project"]["project_intelligence"]["change_impact"]

    assert impact["changed_evidence_ids"]
    assert impact["readiness_recalculated"] is True
    assert set(impact["affected_requirement_ids"]) <= {item["constraint_id"] for item in refreshed["project"]["request"]["constraints"]}
    assert store.get_site_snapshot(original["snapshot_id"])["raw_response_hash"] == original["raw_response_hash"]


def test_phase10_intelligence_api_and_frontend_work_surface(monkeypatch, diligence):
    service, _client, _store = diligence
    project = _phase10_blocked_project(service, "workspace-phase10-http")
    monkeypatch.setattr(main, "diligence_service", service)
    http = TestClient(main.app)

    intelligence = http.get(f"/v1/diligence/projects/{project['project_id']}/intelligence")
    actions = http.post(f"/v1/diligence/projects/{project['project_id']}/next-actions")
    markup = (Path(__file__).parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8")
    script = (Path(__file__).parents[1] / "app" / "static" / "app.js").read_text(encoding="utf-8")

    assert intelligence.status_code == actions.status_code == 200
    assert actions.json()["prioritized_actions"][0]["required_evidence"]
    assert 'id="projectIntelligence"' in markup
    assert "What should happen next" in markup and "renderProjectIntelligence" in script
