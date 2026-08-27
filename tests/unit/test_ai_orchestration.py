import asyncio
import json
import time

import pytest

from app.ai.accounting import finish as finish_accounting
from app.ai.accounting import record_model, start as start_accounting
from app.ai.agents import SpecialistAgent
from app.ai.agents.specialists import _observation_schema, _scoped_context
from app.ai.evaluation import VerificationEngine, evaluate_cases
from app.ai.memory import EvidenceGraphRetriever, ProjectMemoryStore
from app.ai.planners import IntentInterpreter, ProjectSpecValidator, TaskGraphPlanner
from app.ai.planners.project import _project_spec_schema
from app.ai.runtime import OrchestrationEngine, build_project_tool_registry
from app.ai.schemas.orchestration import (
    AgentObservation,
    AgentRole,
    MemoryKind,
    ProjectSpec,
    TaskGraph,
)
from app.ai.tools import PolicyToolRegistry, ToolEffect, ToolPolicy, ToolPolicyError
from app.diligence import DiligenceService
from app.sandbox import SiteSnapshotService
from app.sandbox_scenarios import ScenarioService
from app.workspace.store import WorkspaceStore
from app.infrastructure.db.postgres import PostgresWorkspaceStore
from app.infrastructure.events import EventType
from tests.test_diligence import FakeMireye, FakeWorlds


def run(awaitable):
    return asyncio.run(awaitable)


class FakeModel:
    def __init__(self, responses):
        self.responses = {key: list(value) for key, value in responses.items()}
        self.calls = []

    async def generate(self, request):
        self.calls.append(request)
        queue = self.responses[request["module"]]
        if not queue:
            raise AssertionError(f"Unexpected extra model call: {request['module']}")
        return queue.pop(0)


def spec_payload(*, hard=None, soft=None, unknowns=None, assumptions=None):
    return {
        "schema_version": "project_spec_v1",
        "source_request": "model copy",
        "project_type": "DATA_CENTER",
        "geography": {"country": "US", "state": "Texas"},
        "initial_capacity_mw": 100,
        "expansion_capacity_mw": 300,
        "target_date": None,
        "hard_constraints": hard or [],
        "soft_constraints": soft or [],
        "preferences": [],
        "risk_preferences": [],
        "evidence_requirements": ["parcel_area_m2"],
        "unknowns": unknowns or [],
        "assumptions": assumptions or [],
        "requested_actions": ["compare candidates"],
    }


def task(
    task_id="task_site",
    *,
    task_type="INSPECT_MIREYE_EVIDENCE",
    role="SITE_INTELLIGENCE",
    dependencies=None,
    success="OUTPUT_PRESENT",
    permissions=None,
    metered=False,
    confirmation=False,
):
    return {
        "task_id": task_id,
        "task_type": task_type,
        "agent_role": role,
        "dependencies": dependencies or [],
        "required_inputs": ["project_state"],
        "expected_outputs": ["observation"],
        "evidence_requirements": [],
        "cost_policy": {
            "metered": metered,
            "confirmation_required": confirmation,
            "estimated_credits": 5 if metered else None,
            "estimated_model_calls": 1,
            "latency_class": "LOW",
            "rationale": "Use current evidence first.",
        },
        "permissions": permissions or ["read:project", "read:evidence"],
        "success_condition": {"kind": success, "field": "summary", "expected_value": None},
        "rationale": "Inspect only what the decision needs.",
    }


def graph_payload(tasks):
    return {"schema_version": "task_graph_v1", "planning_rationale": ["Minimal evidence path."], "tasks": tasks}


def observation(task_id="task_site", *, role="SITE_INTELLIGENCE", claims=None, evidence_results=None, decision=None, tool_requests=None):
    return {
        "task_id": task_id,
        "agent_role": role,
        "status": "COMPLETED" if decision is None else "BLOCKED",
        "summary": "Current evidence was inspected.",
        "claims": claims or [],
        "evidence_results": evidence_results or [],
        "tool_requests": tool_requests or [],
        "tool_results": [],
        "action_proposals": [],
        "decision_proposal": decision,
    }


@pytest.fixture
def project_service(tmp_path):
    store = WorkspaceStore(tmp_path / "ai.db")
    service = DiligenceService(store, SiteSnapshotService(store, FakeMireye()), FakeWorlds())
    project = service.create_project(
        workspace_id="workspace-ai",
        message="Compare sites for a 100 MW data center close to transmission.",
        candidates=["1 Main Street, Austin, TX"],
    )
    return service, store, project


def test_project_spec_uses_model_semantic_extraction_and_preserves_hard_soft():
    hard = [
        {
            "constraint_id": "parcel_acreage_range",
            "classification": "HARD",
            "parameters": {"min_acres": 20, "max_acres": 50},
            "reason": "Required land.",
        }
    ]
    soft = [
        {
            "constraint_id": "max_resolution_point_transmission_distance_m",
            "classification": "SOFT",
            "parameters": {"max_distance_m": 5000},
            "reason": "Prefer proximity.",
        }
    ]
    model = FakeModel({"intent_interpreter": [spec_payload(hard=hard, soft=soft)]})
    spec = run(IntentInterpreter(model).interpret("Find 20-50 acre sites, preferably near transmission.", {}))
    assert spec.source_request.startswith("Find")
    assert spec.hard_constraints[0].classification.value == "HARD"
    assert spec.soft_constraints[0].classification.value == "SOFT"
    assert model.calls[0]["module"] == "intent_interpreter"


def test_project_spec_ambiguity_remains_unknown():
    unknown = [
        {
            "unknown_id": "u1",
            "question": "What distance is close?",
            "why_it_matters": "Ranking needs a threshold.",
            "affected_constraints": ["max_resolution_point_transmission_distance_m"],
            "blocking": True,
        }
    ]
    model = FakeModel({"intent_interpreter": [spec_payload(unknowns=unknown)]})
    spec = run(IntentInterpreter(model).interpret("Find sites close to transmission.", {}))
    assert spec.unknowns[0].blocking is True
    assert not spec.hard_constraints and not spec.soft_constraints


def test_project_spec_rejects_unknown_or_invalid_capability_values():
    invalid = ProjectSpec.model_validate(
        spec_payload(
            hard=[
                {
                    "constraint_id": "parcel_acreage_range",
                    "classification": "HARD",
                    "parameters": {"min_acres": -1, "max_acres": 20},
                    "reason": "bad",
                }
            ]
        )
    )
    with pytest.raises(ValueError, match="supported range"):
        ProjectSpecValidator().validate(invalid)


def test_model_assumption_requires_user_authorization():
    assumption = [
        {
            "assumption_id": "a1",
            "statement": "Use 5 km.",
            "source": "MODEL",
            "authorized": True,
            "reason": "Screening only.",
            "affected_constraints": ["max_resolution_point_transmission_distance_m"],
            "confidence": "LOW",
            "overridable": True,
        }
    ]
    spec = ProjectSpec.model_validate(spec_payload(assumptions=assumption))
    with pytest.raises(ValueError, match="authorization"):
        ProjectSpecValidator().validate(spec, assumptions_permitted=False)
    assert ProjectSpecValidator().validate(spec, assumptions_permitted=True).assumptions[0].source.value == "MODEL"


def test_task_graph_dependency_order_and_role_permissions():
    graph = TaskGraph.model_validate(
        graph_payload(
            [
                task("task_a"),
                task(
                    "task_b",
                    task_type="ASSESS_POWER",
                    role="POWER",
                    dependencies=["task_a"],
                    permissions=["read:project", "read:evidence", "read:power"],
                ),
            ]
        )
    )
    planner = TaskGraphPlanner(FakeModel({}))
    planner.validate(graph, {})
    assert [item.task_id for item in graph.ready(set())] == ["task_a"]
    assert [item.task_id for item in graph.ready({"task_a"})] == ["task_b"]
    graph.tasks[1].permissions.append("write:geometry")
    with pytest.raises(ValueError, match="unauthorized permission"):
        planner.validate(graph, {})


def test_planner_uses_typed_model_task_graph():
    payload = graph_payload([task()])
    model = FakeModel({"task_graph_planner": [payload]})
    graph = run(TaskGraphPlanner(model).plan(ProjectSpec.model_validate(spec_payload()), {}))
    assert graph.tasks[0].task_type.value == "INSPECT_MIREYE_EVIDENCE"
    assert model.calls[0]["schema_name"] == "task_graph"


def test_cost_aware_planner_rejects_unconfirmed_metered_and_redundant_refresh():
    planner = TaskGraphPlanner(FakeModel({}))
    unconfirmed = TaskGraph.model_validate(graph_payload([task("task_paid", task_type="REFRESH_EVIDENCE", metered=True)]))
    with pytest.raises(ValueError, match="Metered"):
        planner.validate(unconfirmed, {})
    redundant = TaskGraph.model_validate(
        graph_payload([task("task_refresh", task_type="REFRESH_EVIDENCE", metered=True, confirmation=True)])
    )
    redundant.tasks[0].evidence_requirements = ["field_a"]
    with pytest.raises(ValueError, match="already marked current"):
        planner.validate(redundant, {"current_evidence_ids": ["field_a"]})


def test_specialist_cannot_cross_role_or_tool_scope():
    registry = PolicyToolRegistry()
    registry.register(
        ToolPolicy(
            "power.read",
            "Read power.",
            {"type": "object", "properties": {}, "required": []},
            frozenset({"read:power"}),
            ToolEffect.READ_ONLY,
            False,
            False,
            frozenset({AgentRole.POWER}),
        ),
        lambda _args: {"ok": True},
    )
    model = FakeModel({"specialist_observation": [observation(tool_requests=[{"name": "power.read", "arguments": {}}])]})
    agent = SpecialistAgent(AgentRole.SITE_INTELLIGENCE, model, registry)
    with pytest.raises(ToolPolicyError, match="not allowed"):
        run(agent.execute(TaskGraph.model_validate(graph_payload([task()])).tasks[0], {}))


def test_specialist_cannot_forge_tool_results():
    forged = observation()
    forged["tool_results"] = [{"tool": "scenario.mutate", "result": {"state_hash": "forged"}}]
    agent = SpecialistAgent(AgentRole.SITE_INTELLIGENCE, FakeModel({"specialist_observation": [forged]}), PolicyToolRegistry())
    with pytest.raises(ValueError, match="cannot supply"):
        run(agent.execute(TaskGraph.model_validate(graph_payload([task()])).tasks[0], {}))


def test_specialist_cannot_manufacture_project_requirements():
    fabricated = observation(
        claims=[
            {
                "claim_id": "c1",
                "text": "Invented conclusion.",
                "requirement_id": "invented_requirement",
                "evidence_ids": [],
                "required_scope": None,
                "asserted_outcome": None,
            }
        ]
    )
    agent = SpecialistAgent(AgentRole.SITE_INTELLIGENCE, FakeModel({"specialist_observation": [fabricated]}), PolicyToolRegistry())
    with pytest.raises(ValueError, match="unknown project requirement"):
        run(agent.execute(TaskGraph.model_validate(graph_payload([task()])).tasks[0], {"deterministic_outcomes": {"known": "UNRESOLVED"}}))


def test_tool_policy_enforces_scenario_state_hash_postcondition():
    registry = PolicyToolRegistry()
    policy = ToolPolicy(
        "scenario.mutate",
        "Mutate.",
        {"type": "object", "properties": {}, "required": []},
        frozenset({"write:scenario"}),
        ToolEffect.MUTATION,
        False,
        False,
        frozenset({AgentRole.SCENARIO}),
        "STATE_HASH_CHANGED",
    )
    registry.register(policy, lambda _args: {"state_hash": "same"})
    with pytest.raises(ToolPolicyError, match="STATE_HASH_CHANGED"):
        run(registry.execute("scenario.mutate", {}, role=AgentRole.SCENARIO, granted_scopes={"write:scenario"}, before_state_hash="same"))


def test_tool_policy_blocks_metered_work_without_application_confirmation():
    registry = PolicyToolRegistry()
    registry.register(
        ToolPolicy(
            "mireye.fetch",
            "Fetch.",
            {"type": "object", "properties": {}, "required": []},
            frozenset({"metered:mireye"}),
            ToolEffect.READ_ONLY,
            True,
            True,
            frozenset({AgentRole.SITE_INTELLIGENCE}),
        ),
        lambda _args: {"ok": True},
    )
    with pytest.raises(ToolPolicyError, match="application confirmation"):
        run(registry.execute("mireye.fetch", {}, role=AgentRole.SITE_INTELLIGENCE, granted_scopes={"metered:mireye"}))


def test_verifier_catches_unsupported_claim_and_stale_evidence():
    claim = {
        "claim_id": "c1",
        "text": "Power is available.",
        "requirement_id": "sufficient_grid_capacity",
        "evidence_ids": ["grid"],
        "required_scope": "REGION",
        "asserted_outcome": "PASS",
    }
    obs = AgentObservation.model_validate(observation(claims=[claim]))
    result = VerificationEngine().verify(
        obs,
        {
            "evidence_items": [
                {
                    "evidence_id": "grid",
                    "status": "ok",
                    "expires_at": time.time() - 1,
                    "scope": "REGION",
                    "semantic_strength": "SOURCE_BACKED_SIGNAL",
                }
            ],
            "deterministic_outcomes": {"sufficient_grid_capacity": "UNRESOLVED"},
        },
    )
    assert result.state.value == "UNSUPPORTED" and result.replan_required
    assert "stale" in result.claims[0].reasons[0].lower()


def test_verifier_does_not_upgrade_signal_or_override_evaluator():
    claim = {
        "claim_id": "c1",
        "text": "Transmission is nearby.",
        "requirement_id": "max_resolution_point_transmission_distance_m",
        "evidence_ids": ["tx"],
        "required_scope": "NEAREST_FEATURE",
        "asserted_outcome": None,
    }
    obs = AgentObservation.model_validate(observation(claims=[claim]))
    context = {
        "evidence_items": [
            {
                "evidence_id": "tx",
                "status": "ok",
                "expires_at": time.time() + 60,
                "scope": "POINT_TO_NEAREST_FEATURE",
                "semantic_strength": "SOURCE_BACKED_SIGNAL",
            }
        ],
        "deterministic_outcomes": {},
    }
    assert VerificationEngine().verify(obs, context).state.value == "PARTIALLY_VERIFIED"
    obs.claims[0].asserted_outcome = "PASS"
    assert VerificationEngine().verify(obs, context).state.value == "UNSUPPORTED"


def test_verification_failure_creates_bounded_replan_task():
    graph = TaskGraph.model_validate(graph_payload([task()]))
    updated = TaskGraphPlanner.replan(graph, "task_site", ["utility_confirmation"], AgentRole.SITE_INTELLIGENCE)
    assert len(updated.tasks) == 2
    assert updated.tasks[-1].dependencies == ["task_site"]
    assert updated.tasks[-1].evidence_requirements == ["utility_confirmation"]


def test_four_memory_classes_persist_with_provenance(project_service):
    _service, store, project = project_service
    memory = ProjectMemoryStore(store)
    for kind in MemoryKind:
        memory.put_record(project["project_id"], kind, {"kind": kind.value}, {"source": "test"})
    assert {item.kind for item in memory.list(project["project_id"])} == set(MemoryKind)
    assert all(item.provenance["source"] == "test" for item in memory.list(project["project_id"]))


def test_evidence_graph_retrieval_uses_typed_dependencies(project_service):
    service, store, project = project_service
    persisted = service.get(project["project_id"])
    persisted["project_intelligence"] = {
        "evidence_coverage": [{"requirement_id": "power", "title": "Power", "evidence_ids": ["e1"]}],
        "evidence_items": [{"evidence_id": "e1", "source": "MIREYE"}],
        "evidence_dependencies": [{"requirement_id": "power", "evidence_id": "e1"}],
        "evidence_gaps": [{"gap_id": "g1", "requirement_id": "power"}],
        "recommended_actions": [{"action_id": "a1", "gap_id": "g1", "requirement_id": "power"}],
    }
    store.save_diligence_project(persisted)
    graph = EvidenceGraphRetriever(store)
    assert graph.find_supporting_evidence(project["project_id"], "power")[0]["evidence_id"] == "e1"
    assert graph.find_dependent_constraints(project["project_id"], "e1") == ["power"]
    assert graph.find_required_actions(project["project_id"], "g1")[0]["action_id"] == "a1"


def test_decision_interrupt_resumes_same_run_without_duplicate_work(project_service):
    service, store, project = project_service
    unknown = [
        {
            "unknown_id": "u1",
            "question": "Transmission threshold?",
            "why_it_matters": "Ranking needs a threshold.",
            "affected_constraints": ["max_resolution_point_transmission_distance_m"],
            "blocking": True,
        }
    ]
    decision = {
        "kind": "clarification",
        "question": "What maximum transmission distance should I use?",
        "context": "The request says close but gives no distance.",
        "why_it_matters": "A threshold is required for deterministic ranking.",
        "risk_level": "MEDIUM",
        "blocking": False,
        "input_mode": "single_choice",
        "options": [
            {
                "id": "five_km",
                "label": "5 km",
                "description": "Use five kilometres.",
                "value": {"constraint_id": "max_resolution_point_transmission_distance_m", "max_distance_m": 5000},
                "consequence": "Sites farther away fail this screen.",
            }
        ],
        "recommended_option_id": "five_km",
        "allow_custom": False,
        "custom_schema": None,
        "constraint_targets": ["max_resolution_point_transmission_distance_m"],
    }
    ask = task("task_ask", task_type="ASK_USER", success="USER_DECISION", permissions=["read:project", "request:decision"])
    after = task("task_after", dependencies=["task_ask"])
    model = FakeModel(
        {
            "intent_interpreter": [spec_payload(unknowns=unknown)],
            "task_graph_planner": [graph_payload([ask, after])],
            "specialist_observation": [observation("task_ask", decision=decision), observation("task_after")],
        }
    )
    tools = build_project_tool_registry(service, ScenarioService(store))
    engine = OrchestrationEngine(service, IntentInterpreter(model), TaskGraphPlanner(model), tools, ProjectMemoryStore(store))
    interrupted = run(engine.run(project["project_id"], "Compare sites close to transmission."))
    assert interrupted["run"]["status"] == "WAITING_FOR_DECISION"
    request = interrupted["decision_request"]
    run(service.answer_decision(project["project_id"], request["decision_id"], resume_token=request["resume_token"], option_id="five_km"))
    completed = run(engine.resume(project["project_id"], interrupted["run"]["run_id"]))
    assert completed["run"]["status"] == "COMPLETED"
    assert [item["task_id"] for item in completed["run"]["observations"]] == ["task_ask", "task_after"]
    assert [item["module"] for item in model.calls].count("intent_interpreter") == 1
    assert [item["module"] for item in model.calls].count("task_graph_planner") == 1
    assert [item["type"] for item in completed["run"]["events"]] == [
        "RUN_STARTED", "PLANNING", "TASK_STARTED", "VERIFICATION", "NEEDS_USER_DECISION", "TASK_COMPLETED",
        "RESUMED", "TASK_STARTED", "VERIFICATION", "TASK_COMPLETED", "COMPLETED",
    ]


def test_orchestration_run_persists_for_browser_refresh(project_service):
    service, store, project = project_service
    model = FakeModel(
        {
            "intent_interpreter": [spec_payload()],
            "task_graph_planner": [graph_payload([task()])],
            "specialist_observation": [observation()],
        }
    )
    engine = OrchestrationEngine(
        service,
        IntentInterpreter(model),
        TaskGraphPlanner(model),
        build_project_tool_registry(service, ScenarioService(store)),
        ProjectMemoryStore(store),
    )
    result = run(engine.run(project["project_id"], "Inspect the current evidence."))
    rebuilt = OrchestrationEngine(
        service,
        IntentInterpreter(FakeModel({})),
        TaskGraphPlanner(FakeModel({})),
        build_project_tool_registry(service, ScenarioService(store)),
        ProjectMemoryStore(store),
    )
    assert rebuilt.get_run(project["project_id"], result["run"]["run_id"]).status == "COMPLETED"


def test_benchmark_harness_scores_deterministic_fixture():
    fixture = json.loads((__import__("pathlib").Path(__file__).parents[1] / "fixtures" / "ai_orchestration_benchmark.json").read_text())
    result = evaluate_cases(fixture, fixture)
    assert result.cases == 2
    assert result.metrics["project_spec_accuracy"] == 1.0
    assert result.metrics["task_validity"] == 1.0
    assert result.metrics["unnecessary_task_rate"] == 0.0
    assert result.metrics["missing_task_rate"] == 0.0
    assert result.metrics["false_pass_rate"] == 0.0


def test_orchestration_api_contract_is_exposed_without_replacing_existing_chat():
    from app.main import OrchestrationRequest, ScreenRequest, app

    paths = app.openapi()["paths"]
    assert "/v1/ai/projects/{project_id}/orchestrate" in paths
    assert "/v1/ai/projects/{project_id}/orchestration/{run_id}" in paths
    assert "/v1/ai/projects/{project_id}/orchestration/{run_id}/resume" in paths
    assert "/v1/diligence/projects/{project_id}/chat" in paths
    assert ScreenRequest().apply_confidence_scoring is True
    assert "apply_confidence_scoring" not in OrchestrationRequest.model_fields


def test_typed_model_contexts_exclude_unrelated_state_and_payloads():
    context = {
        "project_id": "project_1", "workspace_id": "workspace_1", "request": {"project": "Data center"},
        "project_spec": spec_payload(hard=[{
            "constraint_id": "sufficient_grid_capacity", "classification": "HARD", "parameters": {}, "reason": "Power",
        }]),
        "project_intelligence": {
            "evidence_coverage": [
                {"requirement_id": "sufficient_grid_capacity", "domain": "Power", "evidence_ids": ["power_1"]},
                {"requirement_id": "parcel_acreage_range", "domain": "Land", "evidence_ids": ["land_1"]},
            ],
            "evidence_gaps": [{"gap_id": "gap_power", "requirement_id": "sufficient_grid_capacity", "domain": "Power"}],
            "recommended_actions": [{"action_id": "action_power", "gap_id": "gap_power", "requirement_id": "sufficient_grid_capacity"}],
        },
        "evidence_items": [
            {"evidence_id": "power_1", "status": "ok", "source": "MIREYE", "payload": "must not be copied"},
            {"evidence_id": "land_1", "status": "ok", "source": "MIREYE", "payload": "unrelated"},
        ],
        "deterministic_outcomes": {"sufficient_grid_capacity": "UNRESOLVED", "parcel_acreage_range": "PASS"},
        "action_decisions": [
            {"gap_id": "gap_power", "action_id": "action_power", "selection": "keep_unresolved", "status": "DEFERRED"},
            {"gap_id": "gap_land", "action_id": "action_land", "selection": "authorize_next_action", "status": "AUTHORIZED"},
        ],
    }
    node_payload = task("task_power", task_type="ASSESS_POWER", role="POWER", permissions=["read:project", "read:evidence", "read:power"])
    node_payload["required_inputs"] = ["recommended_action:action_power"]
    packet = _scoped_context(AgentRole.POWER, TaskGraph.model_validate(graph_payload([node_payload])).tasks[0], context)
    planner = OrchestrationEngine._planner_context({
        **context, "assumptions_permitted": False, "current_evidence_ids": ["power_1", "land_1"],
        "completed_task_ids": [], "metered_operations": [],
    })

    assert [item["evidence_id"] for item in packet["evidence_items"]] == ["power_1"]
    assert "payload" not in packet["evidence_items"][0]
    assert list(packet["deterministic_outcomes"]) == ["sufficient_grid_capacity"]
    assert packet["user_decisions"] == [{
        "gap_id": "gap_power", "action_id": "action_power", "selection": "keep_unresolved", "status": "DEFERRED",
    }]
    assert "evidence_items" not in planner and "requirement_context" not in planner


def test_project_spec_model_schema_only_allows_application_capabilities():
    schema = _project_spec_schema({"sufficient_grid_capacity": {}, "data_center_entitlement": {}})
    definitions = schema["$defs"]

    assert definitions["ConstraintIntent"]["properties"]["constraint_id"]["enum"] == [
        "data_center_entitlement", "sufficient_grid_capacity",
    ]
    assert definitions["Unknown"]["properties"]["affected_constraints"]["items"]["enum"] == [
        "data_center_entitlement", "sufficient_grid_capacity",
    ]


def test_specialist_schema_only_allows_scoped_application_requirements():
    schema = _observation_schema(["sufficient_grid_capacity"])

    assert schema["$defs"]["Claim"]["properties"]["requirement_id"]["anyOf"][0]["enum"] == [
        "sufficient_grid_capacity",
    ]
    assert schema["$defs"]["DecisionProposal"]["properties"]["constraint_targets"]["items"]["enum"] == [
        "sufficient_grid_capacity",
    ]


def test_missing_model_decision_uses_existing_canonical_evidence_gap():
    proposal = OrchestrationEngine._application_decision({
        "deterministic_outcomes": {"sufficient_grid_capacity": "UNRESOLVED"},
        "project_intelligence": {
            "evidence_gaps": [{
                "gap_id": "gap_power", "requirement_id": "sufficient_grid_capacity", "status": "OPEN",
                "description": "Utility deliverability is not confirmed.", "why_it_matters": "100 MW is required.", "impact": "CRITICAL",
            }],
            "recommended_actions": [{"action_id": "action_power", "gap_id": "gap_power"}],
        },
    })

    assert proposal is not None
    assert proposal.constraint_targets == ["sufficient_grid_capacity"]


def test_model_usage_is_attributed_per_orchestration_module():
    token = start_accounting("gpt-5.6-sol")
    record_model({"input_tokens": 100, "output_tokens": 5}, module="intent_interpreter")
    record_model({"input_tokens": 40, "output_tokens": 7}, module="specialist_observation")
    usage = finish_accounting(token)

    assert usage["input_tokens"] == 140 and usage["output_tokens"] == 12
    assert usage["model_usage_by_module"] == {
        "intent_interpreter": {"input_tokens": 100, "output_tokens": 5},
        "specialist_observation": {"input_tokens": 40, "output_tokens": 7},
    }


def test_orchestration_lifecycle_events_are_outbox_ready_and_idempotent():
    previous = {"orchestration_runs": []}
    current = {
        "workspace_id": "workspace_1", "project_id": "project_1",
        "orchestration_runs": [{"run_id": "run_1", "events": [
            {"sequence": index + 1, "type": name, "at": float(index)} for index, name in enumerate((
                "RUN_STARTED", "TASK_STARTED", "TASK_COMPLETED", "VERIFICATION", "REPLAN",
                "NEEDS_USER_DECISION", "RESUMED", "COMPLETED", "FAILED",
            ))
        ]}],
    }
    events = PostgresWorkspaceStore._orchestration_events(previous, current)

    assert [event.event_type for event in events] == [
        EventType.ORCHESTRATION_STARTED, EventType.TASK_STARTED, EventType.TASK_COMPLETED,
        EventType.VERIFICATION_COMPLETED, EventType.REPLAN_CREATED, EventType.DECISION_REQUIRED,
        EventType.DECISION_ANSWERED, EventType.ORCHESTRATION_COMPLETED, EventType.ORCHESTRATION_FAILED,
    ]
    assert PostgresWorkspaceStore._orchestration_events(current, current) == []
