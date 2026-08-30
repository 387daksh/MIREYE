from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from app.ai.schemas.orchestration import MemoryKind, MemoryRecord
from app.ai.memory.graph import EvidenceGraphRepository, MemoryContextBuilder
from app.workspace.store import WorkspaceStore


def _stable_id(*parts: Any) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return "memory_" + hashlib.sha256(payload.encode()).hexdigest()[:24]


class ProjectMemoryStore:
    """Small durable memory on the existing project record; evidence remains referenced, not copied."""

    def __init__(self, store: WorkspaceStore):
        self.store = store
        self.graph = EvidenceGraphRepository(store)
        self.context_builder = MemoryContextBuilder(self.graph)

    def put_record(
        self,
        project_id: str,
        kind: MemoryKind,
        content: dict[str, Any],
        provenance: dict[str, Any],
    ) -> MemoryRecord:
        project = self._project(project_id)
        record = MemoryRecord(
            memory_id=_stable_id(project_id, kind.value, content, provenance),
            project_id=project_id,
            kind=kind,
            content=copy.deepcopy(content),
            provenance=copy.deepcopy(provenance),
        )
        memory = project.setdefault("orchestration_memory", [])
        if not any(item.get("memory_id") == record.memory_id for item in memory):
            memory.append(record.model_dump(mode="json"))
            project["updated_at"] = record.created_at.timestamp()
            self.store.save_diligence_project(project)
            self.graph.write_memory(record.model_dump(mode="json"), project["workspace_id"])
        return record

    def list(self, project_id: str, kind: MemoryKind | None = None) -> list[MemoryRecord]:
        project = self._project(project_id)
        return [
            MemoryRecord.model_validate(item)
            for item in project.get("orchestration_memory", [])
            if kind is None or item.get("kind") == kind.value
        ]

    def get(self, scope_id: str) -> dict[str, Any] | None:
        records = self.list(scope_id)
        return {"records": [item.model_dump(mode="json") for item in records]} if records else None

    def put(self, scope_id: str, value: dict[str, Any]) -> None:
        self.put_record(scope_id, MemoryKind.WORKING, value, {"source": "orchestrator"})

    def _project(self, project_id: str) -> dict[str, Any]:
        project = self.store.get_diligence_project(project_id)
        if project is None:
            raise ValueError("Diligence project was not found.")
        return project


class EvidenceGraphRetriever:
    def __init__(self, store: WorkspaceStore):
        self.store = store
        self.graph = EvidenceGraphRepository(store)

    def find_supporting_evidence(self, project_id: str, claim: str) -> list[dict[str, Any]]:
        return self.graph.find_supporting_evidence(project_id, claim)

    def find_evidence_at_snapshot(self, project_id: str, snapshot_id: str) -> list[dict[str, Any]]:
        return self.graph.find_evidence_at_snapshot(project_id, snapshot_id)

    def find_dependent_constraints(self, project_id: str, evidence_id: str) -> list[str]:
        return sorted({item["requirement_id"] for item in self._intelligence(project_id).get("evidence_dependencies", []) if item.get("evidence_id") == evidence_id})

    def find_affected_decisions(self, project_id: str, constraint_id: str) -> dict[str, list[dict[str, Any]]]:
        intelligence = self._intelligence(project_id)
        return {
            "gaps": [item for item in intelligence.get("evidence_gaps", []) if item.get("requirement_id") == constraint_id],
            "actions": [item for item in intelligence.get("recommended_actions", []) if item.get("requirement_id") == constraint_id],
        }

    def find_required_actions(self, project_id: str, gap_id: str) -> list[dict[str, Any]]:
        return self.graph.find_actions_resolving_gap(project_id, gap_id)

    def find_actions_resolving_gap(self, project_id: str, gap_id: str) -> list[dict[str, Any]]:
        return self.graph.find_actions_resolving_gap(project_id, gap_id)

    def find_conflicts(self, project_id: str, requirement_id: str) -> list[dict[str, Any]]:
        return self.graph.find_conflicts(project_id, requirement_id)

    def find_claims_for_requirement(self, project_id: str, requirement_id: str, *, as_of: float | None = None) -> list[dict[str, Any]]:
        return self.graph.find_claims_for_requirement(project_id, requirement_id, as_of=as_of)

    def find_claims_at_time(self, project_id: str, requirement_id: str, at: float) -> list[dict[str, Any]]:
        return self.graph.find_claims_at_time(project_id, requirement_id, at)

    def find_decisions_for_site(self, project_id: str, site_id: str) -> list[dict[str, Any]]:
        return self.graph.find_decisions_for_site(project_id, site_id)

    def find_decisions_at_time(self, project_id: str, at: float) -> list[dict[str, Any]]:
        return self.graph.find_decisions_at_time(project_id, at)

    def find_evidence_for_decision(self, project_id: str, decision_id: str) -> list[dict[str, Any]]:
        return self.graph.find_evidence_for_decision(project_id, decision_id)

    def find_changes_since_snapshot(self, project_id: str, snapshot_id: str) -> list[dict[str, Any]]:
        return self.graph.find_changes_since_snapshot(project_id, snapshot_id)

    def find_changes_between_snapshots(self, project_id: str, before_snapshot_id: str, after_snapshot_id: str) -> list[dict[str, Any]]:
        return self.graph.find_changes_between_snapshots(project_id, before_snapshot_id, after_snapshot_id)

    def find_scenarios_affected_by_change(self, project_id: str, change_id: str) -> list[dict[str, Any]]:
        return self.graph.find_scenarios_affected_by_change(project_id, change_id)

    def find_project_episodes(self, project_id: str, query: str) -> list[dict[str, Any]]:
        return self.graph.find_project_episodes(project_id, query)

    def find_relevant_memory(self, project_id: str, query: str) -> list[dict[str, Any]]:
        return self.graph.find_relevant_memory(project_id, query)

    def _intelligence(self, project_id: str) -> dict[str, Any]:
        project = self.store.get_diligence_project(project_id)
        if project is None:
            raise ValueError("Diligence project was not found.")
        return project.get("project_intelligence") or {}
