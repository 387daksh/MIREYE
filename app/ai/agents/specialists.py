from __future__ import annotations

import copy
from typing import Any

from app.ai.contracts import ModelProvider
from app.ai.schemas.orchestration import AgentObservation, AgentRole, SpecialistContext, TaskNode
from app.ai.tools import PolicyToolRegistry
from app.infrastructure.observability import span


ROLE_INSTRUCTIONS = {
    AgentRole.SITE_INTELLIGENCE: "Inspect only site identity and MIREYE evidence. Do not resolve facts without evidence.",
    AgentRole.POWER: "Assess power evidence only. Proximity, voltage, and queue context do not prove BESS export or injection interconnection capability.",
    AgentRole.ENTITLEMENT: "Assess entitlement evidence only. Raw zoning is not a legal permitted-use conclusion.",
    AgentRole.GEOSPATIAL: "Use source-backed world context and deterministic evaluations; do not calculate geometry.",
    AgentRole.DOCUMENT: "Return cited document findings and require human review for legal significance.",
    AgentRole.SCENARIO: "Propose scenario work only through authorized tools; never claim a mutation without its postcondition.",
    AgentRole.VERIFICATION: "Identify support and gaps; do not replace the deterministic verifier.",
}


class SpecialistAgent:
    def __init__(self, role: AgentRole, model: ModelProvider, tools: PolicyToolRegistry):
        self.role, self.model, self.tools = role, model, tools

    async def execute(self, task: TaskNode, context: dict[str, Any]) -> AgentObservation:
        if task.agent_role != self.role:
            raise ValueError("Specialist role does not match the assigned task.")
        tool_results: list[dict[str, Any]] = []
        scoped_context = _scoped_context(self.role, task, context)
        observation_schema = _observation_schema(sorted(scoped_context.get("deterministic_outcomes", {})))
        for attempt in range(2):
            with span("ai.specialist", **{"task.id": task.task_id, "agent.role": self.role.value, "attempt": attempt + 1}):
                payload = await self.model.generate(
                    {
                        "module": "specialist_observation",
                        "instructions": ROLE_INSTRUCTIONS[self.role]
                        + " Return a typed observation. Cite evidence IDs for every factual claim."
                        + " Do not request a decision already recorded in user_decisions; report the gap as unresolved.",
                        "input": {
                            "task": task.model_dump(mode="json"),
                            "context": scoped_context,
                            "available_tools": self.tools.schemas(self.role),
                            "tool_results": tool_results,
                        },
                        "schema_name": "agent_observation",
                        "schema": observation_schema,
                    }
                )
            observation = AgentObservation.model_validate(payload)
            if observation.task_id != task.task_id or observation.agent_role != self.role:
                raise ValueError("Specialist returned an observation for another task or role.")
            if observation.tool_results:
                raise ValueError("The model cannot supply authoritative tool results.")
            known_requirements = set(context.get("deterministic_outcomes", {}))
            if any(claim.requirement_id and claim.requirement_id not in known_requirements for claim in observation.claims):
                raise ValueError("Specialist claim targets an unknown project requirement.")
            if not observation.tool_requests:
                observation.tool_results = tool_results
                return observation
            if attempt == 1:
                raise ValueError("Specialist exceeded the bounded tool loop.")
            for request in observation.tool_requests:
                project_id = context.get("project_id")
                if project_id and request.arguments.get("project_id", project_id) != project_id:
                    raise ValueError("A tool cannot access a different project than its orchestration run.")
                result = await self.tools.execute(
                    request.name,
                    request.arguments,
                    role=self.role,
                    granted_scopes=set(task.permissions),
                    application_confirmation=bool(context.get("application_confirmation")),
                    before_state_hash=context.get("before_state_hash"),
                    available_evidence=set(context.get("current_evidence_ids", [])),
                )
                policy = self.tools.policy(request.name)
                tool_results.append({"tool": request.name, "result": result, "postcondition": policy.postcondition})
        raise AssertionError("unreachable")


def _scoped_context(role: AgentRole, task: TaskNode, context: dict[str, Any]) -> dict[str, Any]:
    domains = {
        AgentRole.SITE_INTELLIGENCE: set(),
        AgentRole.POWER: {"Power"},
        AgentRole.ENTITLEMENT: {"Entitlement"},
        AgentRole.GEOSPATIAL: {"Land", "Flood", "Environmental", "Terrain", "Access"},
        AgentRole.DOCUMENT: {"Entitlement", "Power", "Water", "Connectivity", "Access"},
        AgentRole.SCENARIO: set(),
        AgentRole.VERIFICATION: set(),
    }[role]
    intelligence = context.get("project_intelligence", {})
    referenced = set(task.required_inputs) | set(task.evidence_requirements)
    referenced |= {item.rsplit(":", 1)[-1] for item in referenced}
    matching_actions = [
        item for item in intelligence.get("recommended_actions", [])
        if item.get("action_id") in referenced or item.get("requirement_id") in referenced
    ]
    matching_gap_ids = {item.get("gap_id") for item in matching_actions}
    matching_requirements = {item.get("requirement_id") for item in matching_actions}
    coverage = [
        item for item in intelligence.get("evidence_coverage", [])
        if item.get("requirement_id") in matching_requirements
        or (not matching_requirements and (role == AgentRole.SITE_INTELLIGENCE or item.get("domain") in domains))
    ]
    evidence_ids = {evidence_id for item in coverage for evidence_id in item.get("evidence_ids", []) + item.get("available_evidence", [])}
    gaps = [
        item for item in intelligence.get("evidence_gaps", [])
        if item.get("gap_id") in matching_gap_ids
        or item.get("requirement_id") in {coverage_item.get("requirement_id") for coverage_item in coverage}
    ]
    actions = matching_actions or [
        item for item in intelligence.get("recommended_actions", [])
        if item.get("gap_id") in {gap.get("gap_id") for gap in gaps}
    ]
    spec = context.get("project_spec", {})
    requirements = {item.get("requirement_id") for item in coverage}
    scoped_spec = {
        "project_type": spec.get("project_type"),
        "initial_capacity_mw": spec.get("initial_capacity_mw"),
        "hard_constraints": [item for item in spec.get("hard_constraints", []) if item.get("constraint_id") in requirements],
        "soft_constraints": [item for item in spec.get("soft_constraints", []) if item.get("constraint_id") in requirements],
        "unknowns": [item for item in spec.get("unknowns", []) if requirements.intersection(item.get("affected_constraints", []))],
    }
    packet = SpecialistContext(
        project_id=str(context.get("project_id") or ""),
        project_spec=scoped_spec,
        evidence_items=[
            {key: item.get(key) for key in (
                "evidence_id", "status", "scope", "provider", "source", "unit", "semantic_strength",
                "semantic_class", "observed_at", "expires_at", "human_review_required",
            )}
            for item in context.get("evidence_items", []) if item.get("evidence_id") in evidence_ids
        ],
        evidence_gaps=[
            {key: item.get(key) for key in (
                "gap_id", "requirement_id", "domain", "description", "current_evidence", "missing_evidence",
                "evidence_scope", "blocking", "impact", "status",
            )}
            for item in gaps
        ],
        recommended_actions=[
            {key: item.get(key) for key in (
                "action_id", "gap_id", "requirement_id", "type", "title", "required_evidence", "status", "rank",
            )}
            for item in actions
        ],
        deterministic_outcomes={key: value for key, value in context.get("deterministic_outcomes", {}).items() if key in requirements},
        prior_observations=context.get("prior_observations", []),
        user_decisions=[
            {key: item.get(key) for key in ("gap_id", "action_id", "selection", "status")}
            for item in context.get("action_decisions", [])
            if item.get("gap_id") in {gap.get("gap_id") for gap in gaps}
        ],
        site_identity=context.get("task_context", {}).get("site_identity", {}),
        retrieval_context=context.get("task_context", {}).get("retrieval_context", {}),
        context_selection=context.get("task_context", {}).get("context_selection", {}),
        memory_context=context.get("task_context", {}).get("memory_context", context.get("memory_context", {})),
    )
    return packet.model_dump(mode="json")


def _observation_schema(requirement_ids: list[str]) -> dict[str, Any]:
    schema = copy.deepcopy(AgentObservation.model_json_schema())
    definitions = schema["$defs"]
    definitions["Claim"]["properties"]["requirement_id"]["anyOf"][0]["enum"] = requirement_ids
    definitions["DecisionProposal"]["properties"]["constraint_targets"]["items"]["enum"] = requirement_ids
    return schema
