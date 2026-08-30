from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectType(str, Enum):
    DATA_CENTER = "DATA_CENTER"
    SOLAR = "SOLAR"
    INDUSTRIAL = "INDUSTRIAL"
    LOGISTICS = "LOGISTICS"
    RENEWABLE = "RENEWABLE"
    PROPERTY_DILIGENCE = "PROPERTY_DILIGENCE"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    GENERIC = "GENERIC"


class ConstraintClass(str, Enum):
    HARD = "HARD"
    SOFT = "SOFT"


class AssumptionSource(str, Enum):
    USER = "USER"
    MODEL = "MODEL"


class Geography(StrictModel):
    country: str | None = None
    state: str | None = None
    city: str | None = None
    description: str | None = None
    center_lat: float | None = Field(default=None, ge=-90, le=90)
    center_lng: float | None = Field(default=None, ge=-180, le=180)
    radius_km: float | None = Field(default=None, gt=0)


class ConstraintIntent(StrictModel):
    constraint_id: str
    classification: ConstraintClass
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str


class Assumption(StrictModel):
    assumption_id: str
    statement: str
    source: AssumptionSource
    authorized: bool
    reason: str
    affected_constraints: list[str] = Field(default_factory=list)
    confidence: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"
    overridable: bool = True


class Unknown(StrictModel):
    unknown_id: str
    question: str
    why_it_matters: str
    affected_constraints: list[str] = Field(default_factory=list)
    blocking: bool = False


class ProjectSpec(StrictModel):
    schema_version: Literal["project_spec_v1"] = "project_spec_v1"
    source_request: str
    project_type: ProjectType
    geography: Geography
    initial_capacity_mw: float | None = Field(default=None, gt=0)
    expansion_capacity_mw: float | None = Field(default=None, gt=0)
    target_date: date | None = None
    hard_constraints: list[ConstraintIntent] = Field(default_factory=list)
    soft_constraints: list[ConstraintIntent] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    risk_preferences: list[str] = Field(default_factory=list)
    evidence_requirements: list[str] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    requested_actions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_constraint_classes(self) -> "ProjectSpec":
        if any(item.classification != ConstraintClass.HARD for item in self.hard_constraints):
            raise ValueError("hard_constraints may contain only HARD constraints")
        if any(item.classification != ConstraintClass.SOFT for item in self.soft_constraints):
            raise ValueError("soft_constraints may contain only SOFT constraints")
        return self


class AgentRole(str, Enum):
    SITE_INTELLIGENCE = "SITE_INTELLIGENCE"
    POWER = "POWER"
    ENTITLEMENT = "ENTITLEMENT"
    GEOSPATIAL = "GEOSPATIAL"
    DOCUMENT = "DOCUMENT"
    SCENARIO = "SCENARIO"
    VERIFICATION = "VERIFICATION"


class TaskType(str, Enum):
    RESOLVE_CANDIDATE = "RESOLVE_CANDIDATE"
    INSPECT_MIREYE_EVIDENCE = "INSPECT_MIREYE_EVIDENCE"
    REQUEST_MISSING_FIELD = "REQUEST_MISSING_FIELD"
    INSPECT_DOCUMENT = "INSPECT_DOCUMENT"
    EVALUATE_CONSTRAINT = "EVALUATE_CONSTRAINT"
    RESEARCH_ENTITLEMENT = "RESEARCH_ENTITLEMENT"
    ASSESS_POWER = "ASSESS_POWER"
    COMPARE_SCENARIOS = "COMPARE_SCENARIOS"
    GENERATE_RFI = "GENERATE_RFI"
    REFRESH_EVIDENCE = "REFRESH_EVIDENCE"
    ASK_USER = "ASK_USER"
    VERIFY_CONCLUSION = "VERIFY_CONCLUSION"
    RESOLVE_VERIFICATION_GAP = "RESOLVE_VERIFICATION_GAP"


class SuccessKind(str, Enum):
    OUTPUT_PRESENT = "OUTPUT_PRESENT"
    EVIDENCE_AVAILABLE = "EVIDENCE_AVAILABLE"
    DETERMINISTIC_OUTCOME = "DETERMINISTIC_OUTCOME"
    USER_DECISION = "USER_DECISION"
    STATE_HASH_CHANGED = "STATE_HASH_CHANGED"
    VERIFICATION_PASSED = "VERIFICATION_PASSED"


class SuccessCondition(StrictModel):
    kind: SuccessKind
    field: str | None = None
    expected_value: Any = None


class CostPolicy(StrictModel):
    metered: bool = False
    confirmation_required: bool = False
    estimated_credits: float | None = Field(default=None, ge=0)
    estimated_model_calls: int = Field(default=1, ge=0, le=5)
    latency_class: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"
    rationale: str


class TaskNode(StrictModel):
    task_id: str = Field(pattern=r"^task_[a-zA-Z0-9_-]{1,80}$")
    task_type: TaskType
    agent_role: AgentRole
    dependencies: list[str] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    evidence_requirements: list[str] = Field(default_factory=list)
    cost_policy: CostPolicy
    permissions: list[str] = Field(default_factory=list)
    success_condition: SuccessCondition
    rationale: str


class TaskGraph(StrictModel):
    schema_version: Literal["task_graph_v1"] = "task_graph_v1"
    planning_rationale: list[str]
    tasks: list[TaskNode]

    @model_validator(mode="after")
    def validate_graph(self) -> "TaskGraph":
        identifiers = [task.task_id for task in self.tasks]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Task IDs must be unique")
        known = set(identifiers)
        if any(dependency not in known for task in self.tasks for dependency in task.dependencies):
            raise ValueError("Task dependency references an unknown task")
        pending = {task.task_id: set(task.dependencies) for task in self.tasks}
        completed: set[str] = set()
        while pending:
            ready = {task_id for task_id, dependencies in pending.items() if dependencies <= completed}
            if not ready:
                raise ValueError("Task graph contains a dependency cycle")
            completed.update(ready)
            for task_id in ready:
                pending.pop(task_id)
        return self

    def ready(self, completed: set[str]) -> list[TaskNode]:
        return [task for task in self.tasks if task.task_id not in completed and set(task.dependencies) <= completed]


class ToolRequest(StrictModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class EvidenceResult(StrictModel):
    claim: str
    evidence_ids: list[str] = Field(default_factory=list)
    semantic_strength: Literal[
        "DIRECTLY_VERIFIED",
        "SOURCE_BACKED_SIGNAL",
        "DERIVED",
        "INSUFFICIENT_EVIDENCE",
        "UNSUPPORTED_SEMANTICS",
    ]


class Claim(StrictModel):
    claim_id: str
    text: str
    requirement_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    required_scope: str | None = None
    asserted_outcome: Literal["PASS", "FAIL", "UNRESOLVED"] | None = None


class ActionProposal(StrictModel):
    action_type: str
    rationale: str
    required_evidence: list[str] = Field(default_factory=list)


class DecisionOption(StrictModel):
    id: str
    label: str
    description: str
    value: dict[str, Any]
    consequence: str


class DecisionProposal(StrictModel):
    kind: Literal["clarification", "assumption"]
    question: str
    context: str
    why_it_matters: str
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    blocking: Literal[False] = False
    input_mode: Literal["single_choice", "multi_choice", "number", "range", "text", "boolean", "confirmation"]
    options: list[DecisionOption] = Field(default_factory=list, max_length=12)
    recommended_option_id: str | None = None
    allow_custom: bool = False
    custom_schema: dict[str, Any] | None = None
    constraint_targets: list[str]


class ObservationStatus(str, Enum):
    COMPLETED = "COMPLETED"
    NEEDS_TOOL = "NEEDS_TOOL"
    BLOCKED = "BLOCKED"
    UNRESOLVED = "UNRESOLVED"


class AgentObservation(StrictModel):
    task_id: str
    agent_role: AgentRole
    status: ObservationStatus
    summary: str
    claims: list[Claim] = Field(default_factory=list)
    evidence_results: list[EvidenceResult] = Field(default_factory=list)
    tool_requests: list[ToolRequest] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    action_proposals: list[ActionProposal] = Field(default_factory=list)
    decision_proposal: DecisionProposal | None = None


class VerificationState(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    UNSUPPORTED = "UNSUPPORTED"
    CONFLICTED = "CONFLICTED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"


class ClaimVerification(StrictModel):
    claim_id: str
    state: VerificationState
    reasons: list[str]
    evidence_ids: list[str]


class VerificationResult(StrictModel):
    task_id: str
    state: VerificationState
    claims: list[ClaimVerification]
    replan_required: bool
    required_evidence: list[str] = Field(default_factory=list)


class MemoryKind(str, Enum):
    WORKING = "WORKING"
    EPISODIC = "EPISODIC"
    SEMANTIC = "SEMANTIC"
    EVIDENCE = "EVIDENCE"


class MemoryRecord(StrictModel):
    memory_id: str
    project_id: str
    kind: MemoryKind
    content: dict[str, Any]
    provenance: dict[str, Any]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PlannerContext(StrictModel):
    project_id: str
    workspace_id: str | None = None
    assumptions_permitted: bool = False
    request: dict[str, Any] = Field(default_factory=dict)
    evidence_coverage: list[dict[str, Any]] = Field(default_factory=list)
    evidence_gaps: list[dict[str, Any]] = Field(default_factory=list)
    recommended_actions: list[dict[str, Any]] = Field(default_factory=list)
    current_evidence_ids: list[str] = Field(default_factory=list)
    completed_task_ids: list[str] = Field(default_factory=list)
    metered_operations: list[dict[str, Any]] = Field(default_factory=list)
    memory_context: dict[str, Any] = Field(default_factory=dict)


class SpecialistContext(StrictModel):
    project_id: str
    project_spec: dict[str, Any] = Field(default_factory=dict)
    evidence_items: list[dict[str, Any]] = Field(default_factory=list)
    evidence_gaps: list[dict[str, Any]] = Field(default_factory=list)
    recommended_actions: list[dict[str, Any]] = Field(default_factory=list)
    deterministic_outcomes: dict[str, str] = Field(default_factory=dict)
    prior_observations: list[dict[str, Any]] = Field(default_factory=list)
    user_decisions: list[dict[str, Any]] = Field(default_factory=list)
    site_identity: dict[str, Any] = Field(default_factory=dict)
    retrieval_context: dict[str, Any] = Field(default_factory=dict)
    context_selection: dict[str, Any] = Field(default_factory=dict)
    memory_context: dict[str, Any] = Field(default_factory=dict)


class VerifierContext(StrictModel):
    evidence_items: list[dict[str, Any]] = Field(default_factory=list)
    deterministic_outcomes: dict[str, str] = Field(default_factory=dict)
    now: float
    memory_context: dict[str, Any] = Field(default_factory=dict)


class UserDecisionContext(StrictModel):
    project_id: str
    capability_ids: list[str] = Field(default_factory=list)
    evidence_gaps: list[dict[str, Any]] = Field(default_factory=list)
    recommended_actions: list[dict[str, Any]] = Field(default_factory=list)
    assumptions_permitted: bool = False


class ModuleTokenUsage(StrictModel):
    input_tokens: int = 0
    output_tokens: int = 0


class RunAccounting(StrictModel):
    model: str
    model_pricing: dict[str, float] | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    model_cost: float | Literal["UNKNOWN"] = "UNKNOWN"
    mireye_quoted_credits: float | Literal["UNKNOWN"] = "UNKNOWN"
    mireye_charged_credits: float | Literal["UNKNOWN"] = "UNKNOWN"
    external_api_cost: float | Literal["UNKNOWN"] = "UNKNOWN"
    total_known_cost: float = 0.0
    unknown_cost_components: list[str] = Field(default_factory=list)
    model_usage_by_module: dict[str, ModuleTokenUsage] = Field(default_factory=dict)
    started_at: datetime
    completed_at: datetime | None = None


class OrchestrationRun(StrictModel):
    run_id: str
    project_id: str
    status: Literal["PLANNED", "RUNNING", "WAITING_FOR_DECISION", "COMPLETED", "FAILED", "CANCELLED"]
    project_spec: ProjectSpec
    task_graph: TaskGraph
    observations: list[AgentObservation] = Field(default_factory=list)
    verifications: list[VerificationResult] = Field(default_factory=list)
    replans: int = 0
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    accounting: RunAccounting | None = None
