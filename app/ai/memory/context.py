"""Deterministic, task-aware context selection for model activities."""
from __future__ import annotations

import copy
from typing import Any

from app.ai.memory.graph import MemoryContextBuilder
from app.ai.schemas.orchestration import AgentRole, TaskNode, TaskType


class ContextCompletenessError(ValueError):
    """Raised before a model call when required authoritative context is absent."""


_DOCUMENT_ROLES = {AgentRole.POWER, AgentRole.ENTITLEMENT, AgentRole.DOCUMENT}


class TaskContextBuilder:
    """Select identity, graph memory, and optional document candidates by task."""

    def __init__(self, memory: MemoryContextBuilder):
        self.memory = memory

    @staticmethod
    def needs_documents(task: TaskNode) -> bool:
        return task.agent_role in _DOCUMENT_ROLES or task.task_type == TaskType.INSPECT_DOCUMENT

    def build(
        self,
        project: dict[str, Any],
        task: TaskNode,
        *,
        project_spec: dict[str, Any],
        prior_observations: list[dict[str, Any]],
        document_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        identity = self._identity(project)
        required = ["project_id"]
        if task.task_type != TaskType.RESOLVE_CANDIDATE and identity["site_id"]:
            required.extend(["site_identity.site_id", "site_identity.parcel_id", "site_identity.canonical_address", "site_identity.site_snapshot_id", "site_identity.match_type"])
        missing = [path for path in required if not self._present(project, identity, path)]
        if missing:
            raise ContextCompletenessError(
                f"Task {task.task_id} is missing required authoritative context: {', '.join(missing)}"
            )
        query = " ".join([task.task_type.value, task.rationale, *task.required_inputs, *task.evidence_requirements])
        memory = self.memory.build(project["project_id"], query, "specialist", limit=6, token_limit=800)
        documents = self._documents((document_result or {}).get("document_chunks", []))
        return {
            "site_identity": identity,
            "memory_context": memory,
            "retrieval_context": {
                "order": ["exact_identifiers", "structured_filters", "graph_traversal", "temporal_filters", "vector_similarity"],
                "graph_record_ids": [
                    item.get("memory_id") or item.get("claim_id") for item in memory["selected_records"]
                ],
                "document_chunks": documents,
                "vector_queries": (document_result or {}).get("vector_queries", 0),
            },
            "context_selection": {
                "required": required,
                "optional": ["relevant graph records", "current document candidates", "prior task observations"],
                "excluded": ["unrelated evidence payloads", "unrelated scenarios", "unrelated episodes"],
                "missing": [],
            },
            "project_spec": copy.deepcopy(project_spec),
            "prior_observations": copy.deepcopy(prior_observations),
        }

    @staticmethod
    def _present(project: dict[str, Any], identity: dict[str, Any], path: str) -> bool:
        if path == "project_id":
            return bool(project.get("project_id"))
        return bool(identity.get(path.rsplit(".", 1)[-1]))

    @staticmethod
    def _identity(project: dict[str, Any]) -> dict[str, Any]:
        intelligence = project.get("project_intelligence") or {}
        active = intelligence.get("active_site") or {}
        candidate: dict[str, Any] = next(
            (
                item for item in project.get("candidates", [])
                if item.get("candidate_id") == active.get("candidate_id")
                or item.get("site_id") == active.get("site_id")
            ),
            {},
        )
        reconciliation = candidate.get("address_reconciliation") or {}
        return {
            "site_id": active.get("site_id") or candidate.get("site_id"),
            "parcel_id": reconciliation.get("parcel_id") or candidate.get("parcel_id"),
            "apn": reconciliation.get("apn") or candidate.get("apn"),
            "canonical_address": reconciliation.get("canonical_address") or (candidate.get("summary") or {}).get("title"),
            "site_snapshot_id": active.get("site_snapshot_id") or candidate.get("snapshot_id"),
            "match_type": reconciliation.get("match_type") or candidate.get("parcel_match_type"),
            "geometry_reference": active.get("site_snapshot_id") or candidate.get("snapshot_id"),
            "source": "MIREYE",
            "authority_level": "DIRECTLY_VERIFIED",
        }

    @staticmethod
    def _documents(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep semantic candidates useful without allowing documents to exhaust the task budget."""
        selected: list[dict[str, Any]] = []
        for chunk in chunks[:2]:
            metadata = copy.deepcopy(chunk.get("source_metadata") or {})
            selected.append({
                "record_id": chunk.get("chunk_id"),
                "type": "DOCUMENT_CHUNK",
                "document_id": chunk.get("document_id"),
                "content": str(chunk.get("content") or "")[:800],
                "source": metadata.get("provider"),
                "snapshot_id": metadata.get("snapshot_id"),
                "temporal_validity": {"retrieved_at": metadata.get("retrieved_at"), "effective_date": metadata.get("effective_date")},
                "scope": metadata.get("jurisdiction") or metadata.get("section"),
                "authority_level": "SOURCE_DOCUMENT",
                "relevance_reason": "pgvector semantic candidate",
                "distance": chunk.get("distance"),
            })
        return selected
