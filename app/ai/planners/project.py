from __future__ import annotations

import copy
from typing import Any, cast

from app.ai.contracts import ModelProvider
from app.ai.schemas.orchestration import (
    AgentRole,
    AssumptionSource,
    ProjectSpec,
    TaskGraph,
    TaskNode,
    TaskType,
)
from app.diligence import CONSTRAINT_CAPABILITIES
from app.infrastructure.observability import span


ROLE_TASKS: dict[AgentRole, set[TaskType]] = {
    AgentRole.SITE_INTELLIGENCE: {
        TaskType.RESOLVE_CANDIDATE,
        TaskType.INSPECT_MIREYE_EVIDENCE,
        TaskType.REQUEST_MISSING_FIELD,
        TaskType.ASK_USER,
        TaskType.REFRESH_EVIDENCE,
        TaskType.RESOLVE_VERIFICATION_GAP,
    },
    AgentRole.POWER: {TaskType.ASSESS_POWER, TaskType.GENERATE_RFI, TaskType.RESOLVE_VERIFICATION_GAP},
    AgentRole.ENTITLEMENT: {
        TaskType.RESEARCH_ENTITLEMENT,
        TaskType.GENERATE_RFI,
        TaskType.RESOLVE_VERIFICATION_GAP,
    },
    AgentRole.GEOSPATIAL: {TaskType.EVALUATE_CONSTRAINT, TaskType.RESOLVE_VERIFICATION_GAP},
    AgentRole.DOCUMENT: {TaskType.INSPECT_DOCUMENT, TaskType.RESOLVE_VERIFICATION_GAP},
    AgentRole.SCENARIO: {TaskType.COMPARE_SCENARIOS, TaskType.RESOLVE_VERIFICATION_GAP},
    AgentRole.VERIFICATION: {TaskType.VERIFY_CONCLUSION},
}
ROLE_SCOPES: dict[AgentRole, set[str]] = {
    AgentRole.SITE_INTELLIGENCE: {"read:project", "read:evidence", "plan:mireye", "request:decision"},
    AgentRole.POWER: {"read:project", "read:evidence", "read:power", "propose:action"},
    AgentRole.ENTITLEMENT: {"read:project", "read:evidence", "read:entitlement", "read:documents", "propose:action"},
    AgentRole.GEOSPATIAL: {"read:project", "read:evidence", "read:world", "read:evaluation"},
    AgentRole.DOCUMENT: {"read:project", "read:evidence", "read:documents"},
    AgentRole.SCENARIO: {"read:project", "read:evidence", "read:scenarios", "propose:scenario"},
    AgentRole.VERIFICATION: {"read:project", "read:evidence", "read:evaluation"},
}


class ProjectSpecValidator:
    def __init__(self, capabilities: dict[str, dict[str, Any]] | None = None):
        self.capabilities = cast(dict[str, dict[str, Any]], capabilities or CONSTRAINT_CAPABILITIES)

    def validate(self, spec: ProjectSpec, *, assumptions_permitted: bool = False) -> ProjectSpec:
        constraints = [*spec.hard_constraints, *spec.soft_constraints]
        identifiers = [item.constraint_id for item in constraints]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("A constraint may appear only once in ProjectSpec.")
        for constraint in constraints:
            capability = self.capabilities.get(constraint.constraint_id)
            if capability is None:
                raise ValueError(f"Unknown constraint_id: {constraint.constraint_id}")
            self._validate_parameters(constraint.parameters, capability["input_schema"])
        for unknown in spec.unknowns:
            self._known_targets(unknown.affected_constraints)
        for assumption in spec.assumptions:
            self._known_targets(assumption.affected_constraints)
            if assumption.source == AssumptionSource.MODEL:
                if not assumptions_permitted or not assumption.authorized:
                    raise ValueError("Model assumptions require explicit user authorization.")
                if any(not self.capabilities[target].get("assumption_allowed") for target in assumption.affected_constraints):
                    raise ValueError("A model assumption targets a capability that cannot be assumed.")
        return spec

    def _known_targets(self, targets: list[str]) -> None:
        unknown = sorted(set(targets) - self.capabilities.keys())
        if unknown:
            raise ValueError(f"Unknown constraint targets: {', '.join(unknown)}")

    @staticmethod
    def _validate_parameters(parameters: dict[str, Any], schema: dict[str, Any]) -> None:
        properties = schema.get("properties", {})
        unexpected = sorted(set(parameters) - set(properties))
        missing = sorted(set(schema.get("required", [])) - set(parameters))
        if unexpected or missing:
            raise ValueError(f"Invalid constraint parameters; unexpected={unexpected}, missing={missing}")
        for name, value in parameters.items():
            rule = properties[name]
            expected = rule.get("type")
            if expected == "number":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{name} must be numeric")
                if value < rule.get("minimum", value) or value > rule.get("maximum", value):
                    raise ValueError(f"{name} is outside the supported range")
            elif expected == "boolean" and not isinstance(value, bool):
                raise ValueError(f"{name} must be boolean")
            elif expected == "string_list":
                if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                    raise ValueError(f"{name} must be a non-empty string list")
                if not rule.get("min_items", 0) <= len(value) <= rule.get("max_items", len(value)):
                    raise ValueError(f"{name} has an unsupported number of values")
        if "min_acres" in parameters and "max_acres" in parameters and parameters["min_acres"] > parameters["max_acres"]:
            raise ValueError("min_acres cannot exceed max_acres")


class IntentInterpreter:
    def __init__(self, model: ModelProvider, validator: ProjectSpecValidator | None = None):
        self.model = model
        self.validator = validator or ProjectSpecValidator()

    async def interpret(self, message: str, context: dict[str, Any]) -> ProjectSpec:
        if not message.strip():
            raise ValueError("Project request must not be empty.")
        with span("ai.intent_interpretation"):
            request = context.get("request", {})
            requested_targets = {
                item.get("constraint_id") for item in request.get("constraints", []) if isinstance(item, dict)
            }
            capabilities = {
                key: value for key, value in self.validator.capabilities.items() if not requested_targets or key in requested_targets
            }
            payload = await self.model.generate(
                {
                    "module": "intent_interpreter",
                    "instructions": (
                        "Extract the request into ProjectSpec. Preserve hard constraints, soft preferences, user assumptions, "
                        "model assumptions, and unknowns separately. Never invent a threshold or silently make an ambiguity hard. "
                        "Constraint parameters may contain only keys declared in that capability's input_schema.properties; "
                        "do not copy capability metadata such as scope, semantics, or evaluator support into parameters."
                    ),
                    "input": {"request": message, "context": context, "capabilities": capabilities},
                    "schema_name": "project_spec",
                    "schema": _project_spec_schema(capabilities),
                }
            )
        spec = ProjectSpec.model_validate(payload)
        spec.source_request = message
        return self.validator.validate(spec, assumptions_permitted=bool(context.get("assumptions_permitted")))


class TaskGraphPlanner:
    def __init__(self, model: ModelProvider):
        self.model = model

    async def plan(self, spec: ProjectSpec, context: dict[str, Any]) -> TaskGraph:
        with span("ai.task_graph_planning"):
            payload = await self.model.generate(
                {
                    "module": "task_graph_planner",
                    "instructions": (
                        "Create the smallest valid TaskGraph. Prefer existing fresh/cached evidence, then unmetered sources, "
                        "then minimal metered fields. Paid tasks require confirmation. Do not repeat completed work. "
                        "Every task_id must start with task_ and contain only letters, numbers, underscores, or hyphens. "
                        "Each task may request only permissions listed for its agent role in role_scopes. "
                        "If ProjectSpec has a blocking unknown, include an ASK_USER task assigned to SITE_INTELLIGENCE."
                    ),
                    "input": {
                        "project_spec": spec.model_dump(mode="json"),
                        "project_state": context,
                        "role_tasks": _role_tasks_json(),
                        "role_scopes": {role.value: sorted(scopes) for role, scopes in ROLE_SCOPES.items()},
                    },
                    "schema_name": "task_graph",
                    "schema": TaskGraph.model_json_schema(),
                }
            )
        return self.validate(TaskGraph.model_validate(payload), context, spec)

    def validate(self, graph: TaskGraph, context: dict[str, Any], spec: ProjectSpec | None = None) -> TaskGraph:
        completed = set(context.get("completed_task_ids", []))
        current_evidence = set(context.get("current_evidence_ids", []))
        for task in graph.tasks:
            if task.task_type not in ROLE_TASKS[task.agent_role]:
                raise ValueError(f"{task.agent_role.value} cannot execute {task.task_type.value}")
            if not set(task.permissions) <= ROLE_SCOPES[task.agent_role]:
                raise ValueError(f"{task.agent_role.value} requested an unauthorized permission")
            if task.cost_policy.metered and not task.cost_policy.confirmation_required:
                raise ValueError("Metered tasks must require application confirmation.")
            if (
                task.task_type == TaskType.REFRESH_EVIDENCE
                and task.evidence_requirements
                and set(task.evidence_requirements) <= current_evidence
            ):
                raise ValueError("The planner may not refresh evidence already marked current.")
            if task.task_id in completed:
                raise ValueError("The planner may not repeat a completed task.")
        if spec and any(item.blocking for item in spec.unknowns) and not any(task.task_type == TaskType.ASK_USER for task in graph.tasks):
            raise ValueError("A blocking ProjectSpec unknown requires an ASK_USER task.")
        return graph

    @staticmethod
    def replan(graph: TaskGraph, verification_task_id: str, required_evidence: list[str], role: AgentRole) -> TaskGraph:
        suffix = 1
        identifiers = {task.task_id for task in graph.tasks}
        while f"task_replan_{suffix}" in identifiers:
            suffix += 1
        task = TaskNode.model_validate(
            {
                "task_id": f"task_replan_{suffix}",
                "task_type": "RESOLVE_VERIFICATION_GAP",
                "agent_role": role.value if TaskType.RESOLVE_VERIFICATION_GAP in ROLE_TASKS[role] else AgentRole.SITE_INTELLIGENCE.value,
                "dependencies": [verification_task_id],
                "required_inputs": ["verification_result"],
                "expected_outputs": ["evidence_result"],
                "evidence_requirements": required_evidence,
                "cost_policy": {
                    "metered": False,
                    "confirmation_required": False,
                    "estimated_model_calls": 1,
                    "latency_class": "LOW",
                    "rationale": "Resolve the verifier-identified evidence gap without repeating prior work.",
                },
                "permissions": ["read:evidence"],
                "success_condition": {"kind": "EVIDENCE_AVAILABLE", "field": "evidence_results"},
                "rationale": "Verification found an unsupported claim.",
            }
        )
        updated = copy.deepcopy(graph)
        updated.tasks.append(task)
        updated.planning_rationale.append(f"Replanned after verification failure in {verification_task_id}.")
        return TaskGraph.model_validate(updated.model_dump())


def _role_tasks_json() -> dict[str, list[str]]:
    return {role.value: sorted(task.value for task in tasks) for role, tasks in ROLE_TASKS.items()}


def _project_spec_schema(capabilities: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Constrain model choices to application-owned capability identifiers."""
    schema = copy.deepcopy(ProjectSpec.model_json_schema())
    identifiers = sorted(capabilities)
    definitions = schema.get("$defs", {})
    definitions["ConstraintIntent"]["properties"]["constraint_id"]["enum"] = identifiers
    definitions["Unknown"]["properties"]["affected_constraints"]["items"]["enum"] = identifiers
    definitions["Assumption"]["properties"]["affected_constraints"]["items"]["enum"] = identifiers
    return schema
