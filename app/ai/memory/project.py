from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from app.ai.schemas.orchestration import MemoryKind, MemoryRecord
from app.workspace.store import WorkspaceStore


def _stable_id(*parts: Any) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return "memory_" + hashlib.sha256(payload.encode()).hexdigest()[:24]


class ProjectMemoryStore:
    """Small durable memory on the existing project record; evidence remains referenced, not copied."""

    def __init__(self, store: WorkspaceStore):
        self.store = store

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

    def find_supporting_evidence(self, project_id: str, claim: str) -> list[dict[str, Any]]:
        intelligence = self._intelligence(project_id)
        claim_key = claim.casefold()
        coverage = [
            item
            for item in intelligence.get("evidence_coverage", [])
            if claim_key in {str(item.get("requirement_id", "")).casefold(), str(item.get("title", "")).casefold()}
        ]
        evidence_ids = {evidence_id for item in coverage for evidence_id in item.get("evidence_ids", [])}
        return [item for item in intelligence.get("evidence_items", []) if item.get("evidence_id") in evidence_ids]

    def find_dependent_constraints(self, project_id: str, evidence_id: str) -> list[str]:
        return sorted(
            {
                item["requirement_id"]
                for item in self._intelligence(project_id).get("evidence_dependencies", [])
                if item.get("evidence_id") == evidence_id
            }
        )

    def find_affected_decisions(self, project_id: str, constraint_id: str) -> dict[str, list[dict[str, Any]]]:
        intelligence = self._intelligence(project_id)
        return {
            "gaps": [item for item in intelligence.get("evidence_gaps", []) if item.get("requirement_id") == constraint_id],
            "actions": [item for item in intelligence.get("recommended_actions", []) if item.get("requirement_id") == constraint_id],
        }

    def find_required_actions(self, project_id: str, gap_id: str) -> list[dict[str, Any]]:
        return [item for item in self._intelligence(project_id).get("recommended_actions", []) if item.get("gap_id") == gap_id]

    def _intelligence(self, project_id: str) -> dict[str, Any]:
        project = self.store.get_diligence_project(project_id)
        if project is None:
            raise ValueError("Diligence project was not found.")
        return project.get("project_intelligence") or {}
