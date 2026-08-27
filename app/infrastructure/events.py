from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, Field


class EventType(str, Enum):
    PROJECT_CREATED = "ProjectCreated"
    PROJECT_SPEC_CREATED = "ProjectSpecCreated"
    EVIDENCE_ADDED = "EvidenceAdded"
    EVIDENCE_CHANGED = "EvidenceChanged"
    SNAPSHOT_CREATED = "SnapshotCreated"
    CONSTRAINT_INVALIDATED = "ConstraintInvalidated"
    SCENARIO_STALE = "ScenarioStale"
    ACTION_CREATED = "ActionCreated"
    ACTION_COMPLETED = "ActionCompleted"
    READINESS_CHANGED = "ReadinessChanged"
    PROJECT_CHANGE_DETECTED = "ProjectChangeDetected"
    WORLD_SNAPSHOT_CREATED = "WorldSnapshotCreated"
    ORCHESTRATION_STARTED = "OrchestrationStarted"
    TASK_STARTED = "TaskStarted"
    TASK_COMPLETED = "TaskCompleted"
    VERIFICATION_COMPLETED = "VerificationCompleted"
    REPLAN_CREATED = "ReplanCreated"
    DECISION_REQUIRED = "DecisionRequired"
    DECISION_ANSWERED = "DecisionAnswered"
    ORCHESTRATION_COMPLETED = "OrchestrationCompleted"
    ORCHESTRATION_FAILED = "OrchestrationFailed"
    HUMAN_DECISION_REQUIRED = "HumanDecisionRequired"
    RFI_APPROVED = "RFIApproved"


class DomainEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"event_{uuid.uuid4().hex}")
    event_type: EventType
    aggregate_type: str
    aggregate_id: str
    workspace_id: str
    project_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str | None = None
    causation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = 1


EventHandler = Callable[[DomainEvent], None]


class OutboxRepository(Protocol):
    def append(self, event: DomainEvent) -> None: ...


class InProcessEventDispatcher:
    """Synchronous process-local dispatcher; handlers own retry/error policy."""

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    def publish(self, event: DomainEvent) -> None:
        for handler in tuple(self._handlers[event.event_type]):
            handler(event)
