from __future__ import annotations

import asyncio
import copy
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from app.ai.agents import SpecialistAgent
from app.ai.accounting import finish as finish_accounting
from app.ai.accounting import start as start_accounting
from app.ai.evaluation import VerificationEngine
from app.ai.memory import ProjectMemoryStore
from app.ai.planners import IntentInterpreter, TaskGraphPlanner
from app.ai.schemas.orchestration import (
    AgentObservation,
    AgentRole,
    DecisionProposal,
    MemoryKind,
    OrchestrationRun,
    PlannerContext,
    ModuleTokenUsage,
    RunAccounting,
    SuccessKind,
    VerifierContext,
    VerificationResult,
)
from app.ai.tools import PolicyToolRegistry, ToolEffect, ToolPolicy
from app.diligence import DiligenceService
from app.infrastructure.observability import span
from app.sandbox_agent import ModelUnavailableError
from app.sandbox_scenarios import ScenarioService


class OrchestrationError(ValueError):
    pass


class OrchestrationEngine:
    def __init__(
        self,
        diligence: DiligenceService,
        interpreter: IntentInterpreter,
        planner: TaskGraphPlanner,
        tools: PolicyToolRegistry,
        memory: ProjectMemoryStore,
        verifier: VerificationEngine | None = None,
    ):
        self.diligence, self.interpreter, self.planner = diligence, interpreter, planner
        self.tools, self.memory, self.verifier = tools, memory, verifier or VerificationEngine()
        self.specialists = {role: SpecialistAgent(role, interpreter.model, tools) for role in AgentRole}

    async def begin(self, project_id: str, message: str, run_id: str | None = None) -> dict[str, Any]:
        context = self._context(project_id)
        planner_context = self._planner_context(context)
        with span("ai.orchestration.run", **{"project.id": project_id}):
            model = str(getattr(self.interpreter.model, "model", self.interpreter.model.__class__.__name__))
            accounting_token = start_accounting(model)
            try:
                spec = await self.interpreter.interpret(message, planner_context)
                graph = await self.planner.plan(spec, planner_context)
            finally:
                usage = finish_accounting(accounting_token)
            started_at = datetime.now(timezone.utc)
            actual_run_id = run_id or f"run_{uuid.uuid4().hex}"
            operations = [item for item in context.get("metered_operations", []) if item.get("status") in {"SUCCEEDED", "PARTIAL"}]
            quoted_values = [float(item["quoted_credits"]) for item in operations if isinstance(item.get("quoted_credits"), (int, float))]
            charged_values = [float(item["charged_credits"]) for item in operations if isinstance(item.get("charged_credits"), (int, float))]
            quoted_credits: float | Literal["UNKNOWN"] = sum(quoted_values) if quoted_values else "UNKNOWN"
            charged_credits: float | Literal["UNKNOWN"] = sum(charged_values) if charged_values else "UNKNOWN"
            run = OrchestrationRun(
                run_id=actual_run_id,
                project_id=project_id,
                status="RUNNING",
                project_spec=spec,
                task_graph=graph,
                started_at=started_at,
                accounting=RunAccounting(
                    model=model,
                    model_pricing=copy.deepcopy(getattr(self.interpreter.model, "pricing", None)),
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    model_cost=usage["model_cost"],
                    mireye_quoted_credits=quoted_credits,
                    mireye_charged_credits=charged_credits,
                    external_api_cost="UNKNOWN",
                    total_known_cost=usage["model_cost"] if isinstance(usage["model_cost"], float) else 0.0,
                    unknown_cost_components=[
                        name
                        for name, value in (
                            ("model_cost", usage["model_cost"]),
                            ("external_api_cost", "UNKNOWN"),
                            ("mireye_quoted_credits", quoted_credits),
                            ("mireye_charged_credits", charged_credits),
                        )
                        if value == "UNKNOWN"
                    ],
                    model_usage_by_module=usage.get("model_usage_by_module", {}),
                    started_at=started_at,
                ),
            )
            self.memory.put_record(
                project_id,
                MemoryKind.WORKING,
                {"run_id": run.run_id, "project_spec": spec.model_dump(mode="json")},
                {"source": "USER", "request": message},
            )
            self._record_event(run, "RUN_STARTED")
            self._record_event(run, "PLANNING")
            self._save_run(run)
            return {"run": run.model_dump(mode="json"), "decision_request": None}

    async def run(self, project_id: str, message: str) -> dict[str, Any]:
        started = await self.begin(project_id, message)
        result = started
        while result["run"]["status"] == "RUNNING":
            result = await self.advance(project_id, result["run"]["run_id"])
        return result

    async def advance(self, project_id: str, run_id: str) -> dict[str, Any]:
        run = self.get_run(project_id, run_id)
        if run.status != "RUNNING":
            return {"run": run.model_dump(mode="json"), "decision_request": None}
        token = start_accounting(run.accounting.model if run.accounting else "UNKNOWN")
        try:
            result = await self._execute(run, self._context(project_id), max_batches=1)
        except Exception:
            usage = finish_accounting(token)
            failed = self.get_run(project_id, run_id)
            self._merge_accounting(failed, usage)
            self._save_run(failed)
            raise
        usage = finish_accounting(token)
        updated = OrchestrationRun.model_validate(result["run"])
        self._merge_accounting(updated, usage)
        self._save_run(updated)
        result["run"] = updated.model_dump(mode="json")
        return result

    async def resume(self, project_id: str, run_id: str) -> dict[str, Any]:
        result = await self.resume_batch(project_id, run_id)
        while result["run"]["status"] == "RUNNING":
            result = await self.advance(project_id, run_id)
        return result

    async def resume_batch(self, project_id: str, run_id: str) -> dict[str, Any]:
        run = self.get_run(project_id, run_id)
        if run.status != "WAITING_FOR_DECISION":
            raise OrchestrationError("Only a run waiting for a user decision can resume.")
        project = self.diligence.get(project_id)
        if project.get("status") == "CANCELLED":
            run.status = "CANCELLED"
            run.completed_at = run.started_at.__class__.now(run.started_at.tzinfo)
            self._record_event(run, "CANCELLED")
            self._save_run(run)
            return {"run": run.model_dump(mode="json"), "decision_request": None}
        if project.get("active_decision"):
            raise OrchestrationError("The active user decision must be answered before resume.")
        run.status = "RUNNING"
        self._record_event(run, "RESUMED")
        self._save_run(run)
        return await self.advance(project_id, run_id)

    def get_run(self, project_id: str, run_id: str) -> OrchestrationRun:
        project = self.diligence.get(project_id)
        item = next((item for item in project.get("orchestration_runs", []) if item.get("run_id") == run_id), None)
        if item is None:
            raise OrchestrationError("Orchestration run was not found.")
        return OrchestrationRun.model_validate(item)

    async def _execute(self, run: OrchestrationRun, context: dict[str, Any], max_batches: int | None = None) -> dict[str, Any]:
        completed = {item.task_id for item in run.observations}
        batches = 0
        try:
            while len(completed) < len(run.task_graph.tasks):
                ready = run.task_graph.ready(completed)
                if not ready:
                    raise OrchestrationError("No executable task remains in the task graph.")
                for task in ready:
                    self._record_event(run, "TASK_STARTED", task_id=task.task_id, role=task.agent_role.value)
                self._save_run(run)
                task_context = {
                    **context,
                    "project_spec": run.project_spec.model_dump(mode="json"),
                    "prior_observations": [
                        {"task_id": item.task_id, "status": item.status.value, "summary": item.summary}
                        for item in run.observations
                    ],
                }
                observations = await asyncio.gather(*[self.specialists[task.agent_role].execute(task, task_context) for task in ready])
                for task, observation in zip(ready, observations):
                    run.observations.append(observation)
                    completed.add(task.task_id)
                    if observation.evidence_results:
                        self._record_event(
                            run,
                            "EVIDENCE_FOUND",
                            task_id=task.task_id,
                            evidence_ids=sorted({item for result in observation.evidence_results for item in result.evidence_ids}),
                        )
                    verifier_context = VerifierContext(
                        evidence_items=[
                            item for item in context.get("evidence_items", [])
                            if item.get("evidence_id") in {evidence_id for claim in observation.claims for evidence_id in claim.evidence_ids}
                        ],
                        deterministic_outcomes={
                            key: value for key, value in context.get("deterministic_outcomes", {}).items()
                            if key in {claim.requirement_id for claim in observation.claims if claim.requirement_id}
                        },
                        now=context.get("now", time.time()),
                    )
                    verification = self.verifier.verify(observation, verifier_context.model_dump(mode="json"))
                    run.verifications.append(verification)
                    self._record_event(run, "VERIFICATION", task_id=task.task_id, state=verification.state.value)
                    if task.success_condition.kind == SuccessKind.USER_DECISION and observation.decision_proposal is None:
                        observation.decision_proposal = self._application_decision(context)
                    self._check_success(task.success_condition.kind, observation, verification, context)
                    requirement_ids = {claim.requirement_id for claim in observation.claims if claim.requirement_id}
                    canonical_gaps = [
                        item
                        for item in context.get("project_intelligence", {}).get("evidence_gaps", [])
                        if item.get("requirement_id") in requirement_ids
                    ]
                    if verification.replan_required and canonical_gaps and run.replans < 1:
                        required_evidence = sorted(
                            {
                                *verification.required_evidence,
                                *(field for gap in canonical_gaps for field in gap.get("missing_evidence", [])),
                            }
                        )
                        run.task_graph = self.planner.replan(
                            run.task_graph,
                            task.task_id,
                            required_evidence,
                            task.agent_role,
                        )
                        run.replans += 1
                        self._record_event(run, "REPLAN", task_id=task.task_id)
                    if observation.decision_proposal is not None:
                        decision = self.diligence.agent_decision(
                            run.project_id,
                            mode="ASK_USER",
                            decision_request=observation.decision_proposal.model_dump(mode="json"),
                        )
                        run.status = "WAITING_FOR_DECISION"
                        self._record_event(run, "NEEDS_USER_DECISION", task_id=task.task_id, decision_request=decision["decision_request"])
                        self._record_event(run, "TASK_COMPLETED", task_id=task.task_id)
                        self._save_run(run)
                        return {"run": run.model_dump(mode="json"), "decision_request": decision["decision_request"]}
                    self._record_event(run, "TASK_COMPLETED", task_id=task.task_id)
                self._save_run(run)
                batches += 1
                if max_batches is not None and batches >= max_batches:
                    return {"run": run.model_dump(mode="json"), "decision_request": None}
            run.status = "COMPLETED"
            self._record_event(run, "COMPLETED")
            run.completed_at = run.started_at.__class__.now(run.started_at.tzinfo)
            self._save_run(run)
            self.memory.put_record(
                run.project_id,
                MemoryKind.EPISODIC,
                {"run_id": run.run_id, "tasks": sorted(completed), "verification_states": [item.state.value for item in run.verifications]},
                {"source": "ORCHESTRATOR"},
            )
            return {"run": run.model_dump(mode="json"), "decision_request": None}
        except ModelUnavailableError:
            raise
        except Exception:
            run.status = "FAILED"
            self._record_event(run, "FAILED")
            run.completed_at = run.started_at.__class__.now(run.started_at.tzinfo)
            self._save_run(run)
            raise

    def fail(self, project_id: str, run_id: str) -> dict[str, Any]:
        run = self.get_run(project_id, run_id)
        if run.status not in {"COMPLETED", "FAILED", "CANCELLED"}:
            run.status = "FAILED"
            run.completed_at = datetime.now(timezone.utc)
            self._record_event(run, "FAILED")
            self._save_run(run)
        return {"run": run.model_dump(mode="json"), "decision_request": None}

    def _context(self, project_id: str) -> dict[str, Any]:
        project = self.diligence.get(project_id)
        intelligence = self.diligence.evaluate_evidence_coverage(project_id)
        now = time.time()
        evidence = intelligence.get("evidence_items", [])
        return {
            "project_id": project_id,
            "workspace_id": project.get("workspace_id"),
            "assumptions_permitted": project.get("request", {}).get("assumptions_permitted", False),
            "request": copy.deepcopy(project.get("request", {})),
            "requirement_context": self.diligence.requirement_context(project_id),
            "project_intelligence": intelligence,
            "evidence_items": copy.deepcopy(evidence),
            "current_evidence_ids": [
                item["evidence_id"] for item in evidence if item.get("expires_at") is None or item.get("expires_at", 0) > now
            ],
            "deterministic_outcomes": {item["requirement_id"]: item["status"] for item in intelligence.get("evidence_coverage", [])},
            "completed_task_ids": [],
            "metered_operations": copy.deepcopy(project.get("metered_operations", [])),
            "action_decisions": copy.deepcopy(project.get("action_decisions", [])),
            "application_confirmation": False,
            "now": now,
        }

    @staticmethod
    def _planner_context(context: dict[str, Any]) -> dict[str, Any]:
        intelligence = context.get("project_intelligence", {})
        packet = PlannerContext(
            project_id=context["project_id"],
            workspace_id=context.get("workspace_id"),
            assumptions_permitted=bool(context.get("assumptions_permitted")),
            request={
                key: copy.deepcopy(context.get("request", {}).get(key))
                for key in ("message", "project", "capacity_mw", "power_requirements", "constraints", "requirement_gaps")
                if key in context.get("request", {})
            },
            evidence_coverage=[
                {key: copy.deepcopy(item.get(key)) for key in (
                    "requirement_id", "domain", "status", "coverage", "evidence_ids", "missing_evidence",
                    "evidence_scope", "semantic_strength", "blocking", "impact",
                )}
                for item in intelligence.get("evidence_coverage", [])
            ],
            evidence_gaps=[
                {key: copy.deepcopy(item.get(key)) for key in (
                    "gap_id", "requirement_id", "domain", "status", "missing_evidence", "evidence_scope", "blocking", "impact",
                )}
                for item in intelligence.get("evidence_gaps", []) if item.get("status") != "RESOLVED"
            ],
            recommended_actions=[
                {key: copy.deepcopy(item.get(key)) for key in (
                    "action_id", "gap_id", "requirement_id", "type", "status", "required_evidence", "rank",
                )}
                for item in intelligence.get("recommended_actions", [])
            ],
            current_evidence_ids=context.get("current_evidence_ids", []),
            completed_task_ids=context.get("completed_task_ids", []),
            metered_operations=[
                {key: copy.deepcopy(item.get(key)) for key in (
                    "operation_id", "operation_type", "request_hash", "status", "attempts", "quoted_credits", "charged_credits",
                )}
                for item in context.get("metered_operations", [])
            ],
        )
        return packet.model_dump(mode="json")

    @staticmethod
    def _application_decision(context: dict[str, Any]) -> DecisionProposal | None:
        intelligence = context.get("project_intelligence", {})
        gaps = {item.get("gap_id"): item for item in intelligence.get("evidence_gaps", []) if item.get("status") != "RESOLVED"}
        for action in intelligence.get("recommended_actions", []):
            gap = gaps.get(action.get("gap_id"))
            if gap is None:
                continue
            target = gap.get("requirement_id")
            if target and target in context.get("deterministic_outcomes", {}):
                return DecisionProposal(
                    kind="clarification",
                    question="Required project evidence cannot currently be verified. How should MIREYE proceed?",
                    context=str(gap.get("description") or "The authoritative evidence is not available."),
                    why_it_matters=str(gap.get("why_it_matters") or "The requirement must remain unresolved without it."),
                    risk_level="HIGH" if gap.get("impact") in {"HIGH", "CRITICAL"} else "MEDIUM",
                    input_mode="single_choice",
                    constraint_targets=[target],
                )
        return None

    @staticmethod
    def _check_success(
        kind: SuccessKind,
        observation: AgentObservation,
        verification: VerificationResult,
        context: dict[str, Any],
    ) -> None:
        passed = {
            SuccessKind.OUTPUT_PRESENT: bool(observation.summary.strip()),
            SuccessKind.EVIDENCE_AVAILABLE: bool(observation.evidence_results),
            SuccessKind.DETERMINISTIC_OUTCOME: any(item.asserted_outcome for item in observation.claims),
            SuccessKind.USER_DECISION: observation.decision_proposal is not None,
            SuccessKind.STATE_HASH_CHANGED: any(
                item.get("postcondition") == "STATE_HASH_CHANGED"
                and context.get("before_state_hash") is not None
                and item.get("result", {}).get("state_hash") not in {None, context.get("before_state_hash")}
                for item in observation.tool_results
            ),
            SuccessKind.VERIFICATION_PASSED: not verification.replan_required,
        }[kind]
        if not passed:
            raise OrchestrationError(f"Task {observation.task_id} did not satisfy {kind.value}.")

    def _save_run(self, run: OrchestrationRun) -> None:
        project = self.diligence.get(run.project_id)
        for operation in project.get("metered_operations", []):
            if operation.get("status") in {"SUCCEEDED", "PARTIAL"}:
                consumers = operation.setdefault("consumed_by_runs", [])
                if run.run_id not in consumers:
                    consumers.append(run.run_id)
        runs = project.setdefault("orchestration_runs", [])
        payload = run.model_dump(mode="json")
        existing = next((index for index, item in enumerate(runs) if item.get("run_id") == run.run_id), None)
        if existing is None:
            runs.append(payload)
        else:
            runs[existing] = payload
        project["updated_at"] = time.time()
        self.diligence.store.save_diligence_project(project)

    @staticmethod
    def _record_event(run: OrchestrationRun, event_type: str, **payload: Any) -> None:
        run.events.append({"sequence": len(run.events) + 1, "type": event_type, "at": time.time(), **payload})

    @staticmethod
    def _merge_accounting(run: OrchestrationRun, usage: dict[str, Any]) -> None:
        if run.accounting is None:
            return
        run.accounting.input_tokens += int(usage.get("input_tokens", 0))
        run.accounting.output_tokens += int(usage.get("output_tokens", 0))
        for module, item in usage.get("model_usage_by_module", {}).items():
            bucket = run.accounting.model_usage_by_module.setdefault(module, ModuleTokenUsage())
            bucket.input_tokens += int(item.get("input_tokens", 0))
            bucket.output_tokens += int(item.get("output_tokens", 0))
        if isinstance(usage.get("model_cost"), float):
            known = run.accounting.model_cost if isinstance(run.accounting.model_cost, float) else 0.0
            run.accounting.model_cost = known + usage["model_cost"]
            run.accounting.total_known_cost += usage["model_cost"]
            run.accounting.unknown_cost_components = [item for item in run.accounting.unknown_cost_components if item != "model_cost"]
        charged = usage.get("mireye_charged_credits")
        if isinstance(charged, (int, float)):
            known = run.accounting.mireye_charged_credits if isinstance(run.accounting.mireye_charged_credits, float) else 0.0
            run.accounting.mireye_charged_credits = known + float(charged)
        quoted = usage.get("mireye_quoted_credits")
        if isinstance(quoted, (int, float)):
            known = run.accounting.mireye_quoted_credits if isinstance(run.accounting.mireye_quoted_credits, float) else 0.0
            run.accounting.mireye_quoted_credits = known + float(quoted)
            run.accounting.unknown_cost_components = [
                item for item in run.accounting.unknown_cost_components if item != "mireye_quoted_credits"
            ]
        if run.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            run.accounting.completed_at = run.completed_at


def build_project_tool_registry(diligence: DiligenceService, scenarios: ScenarioService) -> PolicyToolRegistry:
    registry = PolicyToolRegistry()

    def add(name: str, description: str, properties: dict[str, Any], required: list[str], scopes: set[str], roles: set[AgentRole], handler):
        registry.register(
            ToolPolicy(
                name=name,
                description=description,
                input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
                required_scopes=frozenset(scopes),
                effect=ToolEffect.READ_ONLY,
                metered=False,
                confirmation_required=False,
                allowed_agents=frozenset(roles),
            ),
            handler,
        )

    add(
        "project.intelligence",
        "Read current evidence coverage and gaps.",
        {"project_id": {"type": "string"}},
        ["project_id"],
        {"read:project", "read:evidence"},
        {AgentRole.SITE_INTELLIGENCE, AgentRole.POWER, AgentRole.ENTITLEMENT, AgentRole.VERIFICATION},
        lambda args: diligence.evaluate_evidence_coverage(args["project_id"]),
    )
    add(
        "project.power_readiness",
        "Read deterministic power readiness.",
        {"project_id": {"type": "string"}, "site_id": {"type": "string"}},
        ["project_id", "site_id"],
        {"read:project", "read:evidence", "read:power"},
        {AgentRole.POWER},
        lambda args: diligence.power_readiness(args["project_id"], args["site_id"]),
    )
    add(
        "project.entitlement",
        "Read evidence-backed entitlement state.",
        {"project_id": {"type": "string"}, "site_id": {"type": "string"}},
        ["project_id", "site_id"],
        {"read:project", "read:evidence", "read:entitlement"},
        {AgentRole.ENTITLEMENT},
        lambda args: diligence.entitlement_state(args["project_id"], args["site_id"]),
    )

    def compare_scenarios(args: dict[str, Any]) -> dict[str, Any]:
        workspace_id = diligence.get(args["project_id"])["workspace_id"]
        versions = [scenarios.get(args[name]) for name in ("left_scenario_id", "right_scenario_id")]
        if any(version.get("workspace_id") != workspace_id for version in versions):
            raise OrchestrationError("A tool cannot access a scenario from another workspace.")
        return scenarios.compare(args["left_scenario_id"], args["right_scenario_id"])

    add(
        "scenario.compare",
        "Compare two persisted scenarios deterministically.",
        {"project_id": {"type": "string"}, "left_scenario_id": {"type": "string"}, "right_scenario_id": {"type": "string"}},
        ["project_id", "left_scenario_id", "right_scenario_id"],
        {"read:scenarios"},
        {AgentRole.SCENARIO},
        compare_scenarios,
    )
    return registry
