import time

import pytest
from app.ai.memory import ContextCompletenessError, EvidenceGraphRetriever, ProjectMemoryStore, TaskContextBuilder
from app.ai.memory.benchmark import TEMPORAL_FIXTURE_MARKER, evaluate_retrieval, temporal_fixture_changes, temporal_fixture_from_t1
from app.ai.schemas.orchestration import AgentRole, CostPolicy, MemoryKind, SuccessCondition, SuccessKind, TaskNode, TaskType
from app.diligence import DiligenceService
from app.sandbox import SiteSnapshotService
from app.workspace.store import WorkspaceStore
from tests.test_diligence import FakeMireye, FakeWorlds


class _ClaimConnection:
    def __init__(self):
        self.claims = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, query, params=()):
        if "SELECT claim_id FROM claim_records" in query:
            subject, predicate, value = params[1:]
            return _Rows([{"claim_id": item["claim_id"]} for item in self.claims.values() if item["normalized_subject"] == subject and item["predicate"] == predicate and item["normalized_object"] != value and item["status"] == "ACTIVE"])
        if query.lstrip().startswith("UPDATE claim_records"):
            self.claims[params[-1]]["status"] = "CONTESTED"
        if "INSERT INTO claim_records" in query:
            self.claims.setdefault(params[0], {"claim_id": params[0], "normalized_subject": params[5], "predicate": params[6], "normalized_object": params[7], "status": params[8]})
        return _Rows([])


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _ClaimStore(WorkspaceStore):
    database_url = "postgresql://test"

    def __init__(self, path):
        super().__init__(path)
        self.claim_connection = _ClaimConnection()

    def _get_conn(self):
        return self.claim_connection

    def get_diligence_project(self, project_id):
        return self.project if self.project["project_id"] == project_id else None


def _project(tmp_path):
    store = WorkspaceStore(tmp_path / "memory.db")
    service = DiligenceService(store, SiteSnapshotService(store, FakeMireye()), FakeWorlds())
    project = service.create_project(
        workspace_id="memory-workspace", message="Compare a 100 MW site close to transmission.", candidates=["1032 Robotic Ave"]
    )
    project["candidates"][0].update(site_id="site_robotic", snapshot_id="snapshot_t1", summary={"title": "1032 Robotic Ave"})
    project["project_intelligence"] = {
        "active_site": {"site_id": "site_robotic", "site_snapshot_id": "snapshot_t1", "title": "1032 Robotic Ave"},
        "evidence_items": [{"evidence_id": "tx_distance", "snapshot_id": "snapshot_t1", "source": "MIREYE", "scope": "POINT_TO_NEAREST_FEATURE", "semantic_strength": "SOURCE_BACKED_SIGNAL", "observed_at": time.time(), "evidence_hash": "sha256:tx"}],
        "evidence_coverage": [{"requirement_id": "sufficient_grid_capacity", "title": "100 MW deliverability", "status": "UNRESOLVED", "semantic_strength": "UNSUPPORTED_SEMANTICS", "snapshot_id": "snapshot_t1", "evidence_ids": ["tx_distance"], "evidence_scope": "UTILITY_COMMITTED_CAPACITY", "last_evaluated_at": time.time()}],
        "evidence_dependencies": [{"requirement_id": "sufficient_grid_capacity", "evidence_id": "tx_distance"}],
        "evidence_gaps": [{"gap_id": "gap_power", "requirement_id": "sufficient_grid_capacity", "status": "OPEN", "missing_evidence": ["utility_confirmed_capacity_mw"]}],
        "recommended_actions": [{"action_id": "action_rfi", "gap_id": "gap_power", "requirement_id": "sufficient_grid_capacity", "status": "PROPOSED"}],
    }
    project["decision_history"] = [{"decision_id": "decision_screen", "evidence_ids": ["tx_distance"], "status": "UNRESOLVED"}]
    store.save_diligence_project(project)
    return store, project


def test_claims_retain_evidence_scope_and_do_not_upgrade_signal(tmp_path):
    store, project = _project(tmp_path)
    graph = EvidenceGraphRetriever(store)

    claims = graph.find_claims_for_requirement(project["project_id"], "sufficient_grid_capacity")
    evidence = graph.find_supporting_evidence(project["project_id"], claims[0]["claim_id"])

    assert claims[0]["status"] == "UNSUPPORTED"
    assert claims[0]["semantic_strength"] == "INTERPRETATION"
    assert claims[0]["provenance"]["scope"] == "UTILITY_COMMITTED_CAPACITY"
    assert [item["evidence_id"] for item in evidence] == ["tx_distance"]
    assert graph.find_actions_resolving_gap(project["project_id"], "gap_power")[0]["action_id"] == "action_rfi"


def test_episodic_memory_and_context_are_bounded(tmp_path):
    store, project = _project(tmp_path)
    memory = ProjectMemoryStore(store)
    memory.put_record(project["project_id"], MemoryKind.EPISODIC, {"summary": "Power remained unresolved", "evidence_ids": ["tx_distance"]}, {"source": "test"})
    memory.put_record(project["project_id"], MemoryKind.SEMANTIC, {"note": "unrelated " * 6_000}, {"source": "test"})

    episodes = memory.graph.find_project_episodes(project["project_id"], "why was power unresolved")
    context = memory.context_builder.build(project["project_id"], "power evidence", "user")

    assert episodes[0]["kind"] == "EPISODIC"
    assert context["context_tokens"] <= context["token_limit"]
    assert "selected_records" in context and "excluded_record_ids" in context


def test_entity_resolution_and_historical_change_retrieval_are_deterministic(tmp_path):
    store, project = _project(tmp_path)
    graph = EvidenceGraphRetriever(store)
    store.save_project_changes([{
        "change_id": "change_t2", "project_id": project["project_id"], "site_id": "site_robotic", "snapshot_before": "snapshot_t1", "snapshot_after": "snapshot_t2",
        "semantic_change_type": "VALUE_CHANGED", "significance": "HIGH", "source": "MIREYE", "detected_at": time.time(), "affected_scenarios": [{"scenario_id": "scenario_a", "revision": 1}],
    }])

    assert graph.graph.resolve_site(project["project_id"], "1032 Robotic Ave")["site_id"] == "site_robotic"
    assert graph.find_changes_since_snapshot(project["project_id"], "snapshot_t1")[0]["change_id"] == "change_t2"
    assert graph.find_scenarios_affected_by_change(project["project_id"], "change_t2") == [{"scenario_id": "scenario_a", "revision": 1}]


def test_snapshot_and_time_retrieval_never_substitute_current_state(tmp_path):
    store, project = _project(tmp_path)
    project["decision_history"][0]["created_at"] = 100.0
    project["project_intelligence"]["evidence_items"][0]["value"] = 989.3
    store.save_diligence_project(project)
    snapshot = {
        "snapshot_id": "snapshot_t1", "workspace_id": project["workspace_id"],
        "parcel_identity": {"parcel_id": "parcel_robotic", "selected_point": {"lat": 30.0, "lng": -97.0}, "parcel_match_type": "exact_intersect"},
        "geometry": {"type": "Point", "coordinates": [-97.0, 30.0]},
        "evidence": {"tx_distance": {"value": 989.3, "provider": "MIREYE", "scope": "POINT_TO_NEAREST_FEATURE", "semantic_strength": "SOURCE_BACKED_SIGNAL", "observed_at": 90.0, "expires_at": 200.0}},
        "raw_response": {}, "raw_response_hash": "raw", "request": {}, "request_hash": "request", "field_catalog_version": "test",
        "provider_metadata": {}, "observed_at": 90.0, "expires_at": 200.0, "created_at": 90.0,
    }
    store.create_site_snapshot(snapshot)
    graph = EvidenceGraphRetriever(store)
    store.save_project_changes([{
        "change_id": "change_t1_t2", "project_id": project["project_id"], "site_id": "site_robotic",
        "snapshot_before": "snapshot_t1", "snapshot_after": "snapshot_t2", "semantic_change_type": "FRESHNESS_CHANGED",
        "significance": "INFO", "source": "MIREYE", "detected_at": 110.0, "affected_scenarios": [],
    }])

    evidence = graph.find_evidence_at_snapshot(project["project_id"], "snapshot_t1")
    assert evidence[0]["snapshot_id"] == "snapshot_t1" and evidence[0]["value"] == 989.3
    assert graph.find_decisions_at_time(project["project_id"], 101.0)[0]["decision_id"] == "decision_screen"
    assert graph.find_changes_between_snapshots(project["project_id"], "snapshot_t1", "snapshot_t2")[0]["change_id"] == "change_t1_t2"


def test_temporal_fixture_is_explicitly_marked_and_never_persists_as_history(tmp_path):
    store, project = _project(tmp_path)
    t1 = {
        "snapshot_id": "real_t1", "site_id": "site_robotic", "workspace_id": project["workspace_id"],
        "parcel_identity": {"parcel_id": "parcel_robotic"}, "geometry": {"type": "Point", "coordinates": [-97, 30]},
        "evidence": {"tx_distance": {"value": 989.3, "status": "ok", "scope": "NEAREST_FEATURE", "source": "MIREYE", "observed_at": 10.0, "expires_at": 20.0}},
        "raw_response": {}, "raw_response_hash": "t1", "request": {}, "request_hash": "request", "field_catalog_version": "real-t1", "provider_metadata": {}, "observed_at": 10.0, "created_at": 10.0,
    }
    test_t2 = temporal_fixture_from_t1(t1, "tx_distance", 990.3)
    changes = temporal_fixture_changes(project_id=project["project_id"], site_id="site_robotic", t1=t1, test_t2=test_t2, intelligence=project["project_intelligence"])

    assert test_t2["validation_fixture"]["kind"] == TEMPORAL_FIXTURE_MARKER
    assert test_t2["validation_fixture"]["derived_from_snapshot_id"] == "real_t1"
    assert t1["evidence"]["tx_distance"]["value"] == 989.3
    assert {item["semantic_change_type"] for item in changes} >= {"VALUE_CHANGED"}
    assert store.get_site_snapshot(test_t2["snapshot_id"]) is None


def test_unverified_model_interpretation_cannot_become_durable_claim(tmp_path):
    store, project = _project(tmp_path)
    graph = EvidenceGraphRetriever(store)

    with pytest.raises(ValueError, match="must be verified"):
        graph.graph.record_validated_claim(project["project_id"], {
            "claim_text": "The site has committed 100 MW.", "normalized_subject": "sufficient_grid_capacity",
            "predicate": "has_deliverability", "normalized_object": "PASS", "semantic_strength": "INTERPRETATION", "verification_status": "UNSUPPORTED",
        })


def test_conflicting_attributed_claims_remain_contested(tmp_path):
    _store, project = _project(tmp_path)
    postgres_store = _ClaimStore(tmp_path / "claims.db")
    postgres_store.project = project
    graph = EvidenceGraphRetriever(postgres_store).graph
    first = graph.record_validated_claim(project["project_id"], {
        "claim_text": "Jurisdiction A interprets the use as industrial.", "normalized_subject": "permitted_use",
        "predicate": "has_interpretation", "normalized_object": "industrial", "semantic_strength": "INTERPRETATION",
        "verification_status": "NEEDS_HUMAN_REVIEW", "provenance": {"source": "jurisdiction-a", "scope": "boundary-a"},
    })
    second = graph.record_validated_claim(project["project_id"], {
        "claim_text": "Jurisdiction B interprets the use as agricultural.", "normalized_subject": "permitted_use",
        "predicate": "has_interpretation", "normalized_object": "agricultural", "semantic_strength": "INTERPRETATION",
        "verification_status": "NEEDS_HUMAN_REVIEW", "provenance": {"source": "jurisdiction-b", "scope": "boundary-b"},
    })

    assert first["status"] == "ACTIVE"
    assert second["status"] == "CONTESTED"
    assert postgres_store.get_diligence_project(project["project_id"])["project_id"] == project["project_id"]


def test_retrieval_benchmark_reports_recall_tokens_and_latency(tmp_path):
    store, project = _project(tmp_path)
    memory = ProjectMemoryStore(store)
    result = evaluate_retrieval(memory.context_builder, project["project_id"], [{"query": "power status", "expected_ids": ["tx_distance"]}])

    assert result["phase18"]["recall"] == 1.0
    assert result["phase18"]["context_tokens"] <= 1_000
    assert result["latency_ms"] >= 0


def _site_task() -> TaskNode:
    return TaskNode(
        task_id="task_site_identity", task_type=TaskType.INSPECT_MIREYE_EVIDENCE, agent_role=AgentRole.SITE_INTELLIGENCE,
        cost_policy=CostPolicy(rationale="Current evidence only"), success_condition=SuccessCondition(kind=SuccessKind.OUTPUT_PRESENT),
        rationale="Assess canonical parcel identity from current evidence.",
    )


def test_task_context_requires_and_includes_canonical_identity(tmp_path):
    store, project = _project(tmp_path)
    candidate = project["candidates"][0]
    candidate["address_reconciliation"] = {
        "parcel_id": "parcel_robotic", "canonical_address": "1032 Robotic Ave", "match_type": "exact_intersect",
    }
    store.save_diligence_project(project)
    packet = TaskContextBuilder(ProjectMemoryStore(store).context_builder).build(
        project, _site_task(), project_spec={}, prior_observations=[],
    )

    assert packet["site_identity"]["parcel_id"] == "parcel_robotic"
    assert packet["site_identity"]["canonical_address"] == "1032 Robotic Ave"
    assert packet["context_selection"]["missing"] == []
    assert packet["memory_context"]["context_tokens"] <= 800
    assert "graph_records" not in packet["retrieval_context"]


def test_task_context_blocks_model_call_when_canonical_identity_is_incomplete(tmp_path):
    store, project = _project(tmp_path)
    project["candidates"][0]["address_reconciliation"] = {"canonical_address": "1032 Robotic Ave", "match_type": "exact_intersect"}
    store.save_diligence_project(project)

    with pytest.raises(ContextCompletenessError, match="site_identity.parcel_id"):
        TaskContextBuilder(ProjectMemoryStore(store).context_builder).build(project, _site_task(), project_spec={}, prior_observations=[])
