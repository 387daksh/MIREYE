"""Typed, provenance-preserving retrieval over existing project truth.

The graph stores references to snapshots/evidence.  It intentionally does not
promote model prose into facts or duplicate raw provider payloads.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from app.workspace.store import WorkspaceStore


def _stable_id(prefix: str, *parts: Any) -> str:
    value = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}_{hashlib.sha256(value.encode()).hexdigest()[:24]}"


def _words(value: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", value.casefold()) if len(word) > 2}


def _strength(value: str | None) -> str:
    return {
        "DIRECTLY_VERIFIED": "OBSERVATION",
        "SOURCE_BACKED_SIGNAL": "SIGNAL",
        "DERIVED": "DERIVED",
    }.get(value or "", "INTERPRETATION")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _decoded(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return copy.deepcopy(value)
    return json.loads(value)


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


@dataclass(frozen=True)
class ContextBudget:
    name: str
    token_limit: int


CONTEXT_BUDGETS = {
    "planner": ContextBudget("PlannerContext", 3_000),
    "specialist": ContextBudget("SpecialistContext", 2_000),
    "verifier": ContextBudget("VerifierContext", 1_500),
    "user": ContextBudget("UserDecisionContext", 1_000),
}


class EvidenceGraphRepository:
    """A Postgres graph repository with a deterministic SQLite-test fallback."""

    def __init__(self, store: WorkspaceStore):
        self.store = store

    @property
    def _postgres(self) -> bool:
        return hasattr(self.store, "database_url")

    def sync_project(self, project: dict[str, Any]) -> None:
        """Project intelligence is materialized as auditable graph references."""
        if not self._postgres:
            return
        intelligence = project.get("project_intelligence") or {}
        workspace_id, project_id = project["workspace_id"], project["project_id"]
        now = time.time()
        active_site = intelligence.get("active_site") or {}
        site_id = active_site.get("site_id")
        evidence = {item["evidence_id"]: item for item in intelligence.get("evidence_items", []) if item.get("evidence_id")}
        with self.store._get_conn() as conn:
            for constraint in project.get("request", {}).get("constraints", []):
                requirement_id = constraint.get("constraint_id")
                if requirement_id:
                    conn.execute(
                        """INSERT INTO project_requirements (project_id,requirement_id,workspace_id,site_id,constraint_json,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?) ON CONFLICT(project_id,requirement_id) DO UPDATE SET constraint_json=excluded.constraint_json,site_id=excluded.site_id,updated_at=excluded.updated_at""",
                        (project_id, requirement_id, workspace_id, site_id, _json(constraint), now, now),
                    )
            for evidence_id, item in evidence.items():
                conn.execute(
                    """INSERT INTO evidence_records (project_id,evidence_id,workspace_id,site_id,snapshot_id,source_type,authority_level,spatial_scope,observed_at,expires_at,content_hash,metadata_json,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id,evidence_id) DO UPDATE SET
                    site_id=excluded.site_id,snapshot_id=excluded.snapshot_id,source_type=excluded.source_type,authority_level=excluded.authority_level,
                    spatial_scope=excluded.spatial_scope,observed_at=excluded.observed_at,expires_at=excluded.expires_at,content_hash=excluded.content_hash,metadata_json=excluded.metadata_json""",
                    (project_id, evidence_id, workspace_id, site_id, item.get("snapshot_id"), item.get("provider") or item.get("source"),
                     item.get("semantic_class"), item.get("scope"), item.get("observed_at"), item.get("expires_at"), item.get("evidence_hash"),
                     _json({key: item.get(key) for key in ("source", "source_url", "unit", "claim_limits", "semantic_strength")}), now),
                )
            for coverage in intelligence.get("evidence_coverage", []):
                requirement_id = coverage.get("requirement_id")
                if not requirement_id:
                    continue
                evidence_ids = sorted({*coverage.get("evidence_ids", []), *coverage.get("available_evidence", [])})
                status = "ACTIVE"
                if coverage.get("semantic_strength") == "UNSUPPORTED_SEMANTICS":
                    status = "UNSUPPORTED"
                claim_id = _stable_id("claim", project_id, requirement_id, coverage.get("snapshot_id"), coverage.get("status"), evidence_ids)
                provenance = {"snapshot_id": coverage.get("snapshot_id"), "evidence_ids": evidence_ids, "scope": coverage.get("evidence_scope")}
                conn.execute(
                    """INSERT INTO claim_records (claim_id,project_id,workspace_id,site_id,claim_text,normalized_subject,predicate,normalized_object,status,semantic_strength,provenance_json,valid_from,valid_until,verification_status,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(claim_id) DO UPDATE SET status=excluded.status,provenance_json=excluded.provenance_json,valid_until=excluded.valid_until,verification_status=excluded.verification_status,updated_at=excluded.updated_at""",
                    (claim_id, project_id, workspace_id, site_id, f"{coverage.get('title') or requirement_id} is {coverage.get('status', 'UNRESOLVED')}", requirement_id,
                     "has_readiness", coverage.get("status"), status, _strength(coverage.get("semantic_strength")), _json(provenance),
                    coverage.get("last_evaluated_at") or now, None, coverage.get("semantic_strength") or "UNVERIFIED", now, now),
                )
                previous_claims = conn.execute(
                    "SELECT claim_id FROM claim_records WHERE project_id = ? AND normalized_subject = ? AND claim_id <> ? AND status = 'ACTIVE'",
                    (project_id, requirement_id, claim_id),
                ).fetchall()
                for previous in previous_claims:
                    conn.execute("UPDATE claim_records SET status = 'SUPERSEDED', superseded_by = ?, valid_until = ?, updated_at = ? WHERE claim_id = ?", (claim_id, now, now, previous["claim_id"]))
                    self._relationship(conn, workspace_id, project_id, "Claim", claim_id, "SUPERSEDES", "Claim", previous["claim_id"], provenance, now)
                self._relationship(conn, workspace_id, project_id, "Claim", claim_id, "APPLIES_TO", "Requirement", requirement_id, provenance, now)
                for evidence_id in evidence_ids:
                    self._relationship(conn, workspace_id, project_id, "Evidence", evidence_id, "SUPPORTS", "Claim", claim_id, provenance, now)
                for gap in intelligence.get("evidence_gaps", []):
                    if gap.get("requirement_id") == requirement_id and gap.get("status") != "RESOLVED":
                        self._relationship(conn, workspace_id, project_id, "EvidenceGap", gap["gap_id"], "BLOCKS", "Claim", claim_id, {"missing_evidence": gap.get("missing_evidence", [])}, now)
            for action in intelligence.get("recommended_actions", []):
                if action.get("action_id") and action.get("gap_id"):
                    conn.execute(
                        """INSERT INTO action_records (action_id,workspace_id,project_id,site_id,gap_id,action_type,status,payload_json,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(action_id) DO UPDATE SET status=excluded.status,payload_json=excluded.payload_json""",
                        (action["action_id"], workspace_id, project_id, site_id, action["gap_id"], action.get("type"), action.get("status"), _json(action), now),
                    )
                    self._relationship(conn, workspace_id, project_id, "Action", action["action_id"], "RESOLVES", "EvidenceGap", action["gap_id"], {"status": action.get("status")}, now)
            for decision in [*project.get("decision_history", []), *project.get("action_decisions", [])]:
                decision_id = decision.get("decision_id") or _stable_id("decision", project_id, decision)
                conn.execute(
                    """INSERT INTO decision_records (decision_id,workspace_id,project_id,site_id,decision_type,status,payload_json,decided_at,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(decision_id) DO UPDATE SET status=excluded.status,payload_json=excluded.payload_json,decided_at=excluded.decided_at""",
                    (decision_id, workspace_id, project_id, site_id, decision.get("kind") or decision.get("mode"), decision.get("status"), _json(decision), decision.get("answered_at") or decision.get("created_at") or now, now),
                )
                for evidence_id in decision.get("evidence_ids", []):
                    self._relationship(conn, workspace_id, project_id, "Evidence", evidence_id, "SUPPORTS", "Decision", decision_id, {"decision_status": decision.get("status")}, now)
            for change in self.store.list_project_changes(project_id):
                for evidence_id in filter(None, [change.get("evidence_id")]):
                    for claim in self._claims_for_evidence(conn, project_id, evidence_id):
                        conn.execute("UPDATE claim_records SET status = 'CONTESTED', updated_at = ? WHERE claim_id = ? AND status = 'ACTIVE'", (now, claim))
                        self._relationship(conn, workspace_id, project_id, "ProjectChange", change["change_id"], "INVALIDATES", "Claim", claim, {"change_type": change.get("semantic_change_type")}, now)
                for scenario in change.get("affected_scenarios", []):
                    scenario_id = scenario.get("scenario_id")
                    if scenario_id:
                        self._relationship(conn, workspace_id, project_id, "ProjectChange", change["change_id"], "AFFECTS", "Scenario", scenario_id, {"revision": scenario.get("revision"), "state": scenario.get("state")}, now)

    def record_validated_claim(self, project_id: str, claim: dict[str, Any]) -> dict[str, Any]:
        """Persist only an evidence-backed, verified interpretation and mark conflicts explicit."""
        required = {"claim_text", "normalized_subject", "predicate", "normalized_object", "semantic_strength", "verification_status"}
        missing = sorted(required - set(claim))
        if missing:
            raise ValueError(f"Claim is missing required fields: {', '.join(missing)}")
        if claim["semantic_strength"] == "INTERPRETATION" and claim["verification_status"] not in {"VERIFIED", "PARTIALLY_VERIFIED", "NEEDS_HUMAN_REVIEW"}:
            raise ValueError("An interpretation must be verified before it becomes durable memory.")
        project = self._project(project_id)
        workspace_id = project["workspace_id"]
        now = time.time()
        payload = {**copy.deepcopy(claim), "claim_id": claim.get("claim_id") or _stable_id("claim", project_id, claim), "project_id": project_id, "status": "ACTIVE", "created_at": now, "updated_at": now}
        if not self._postgres:
            return payload
        with self.store._get_conn() as conn:
            conflicts = conn.execute(
                """SELECT claim_id FROM claim_records WHERE project_id = ? AND normalized_subject = ? AND predicate = ?
                AND normalized_object <> ? AND status = 'ACTIVE'""",
                (project_id, payload["normalized_subject"], payload["predicate"], payload["normalized_object"]),
            ).fetchall()
            if conflicts:
                payload["status"] = "CONTESTED"
                for conflict in conflicts:
                    conn.execute("UPDATE claim_records SET status = 'CONTESTED', updated_at = ? WHERE claim_id = ?", (now, conflict["claim_id"]))
                    self._relationship(conn, workspace_id, project_id, "Claim", payload["claim_id"], "CONTRADICTS", "Claim", conflict["claim_id"], {"reason": "same subject and predicate, different object"}, now)
            conn.execute(
                """INSERT INTO claim_records (claim_id,project_id,workspace_id,site_id,claim_text,normalized_subject,predicate,normalized_object,status,semantic_strength,provenance_json,valid_from,valid_until,superseded_by,verification_status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(claim_id) DO NOTHING""",
                (payload["claim_id"], project_id, workspace_id, claim.get("site_id"), claim["claim_text"], claim["normalized_subject"], claim["predicate"], claim["normalized_object"], payload["status"], claim["semantic_strength"], _json(claim.get("provenance", {})), claim.get("valid_from", now), claim.get("valid_until"), None, claim["verification_status"], now, now),
            )
        return payload

    @staticmethod
    def _relationship(conn: Any, workspace_id: str, project_id: str, source_type: str, source_id: str, relation: str, target_type: str, target_id: str, context: dict[str, Any], occurred_at: float) -> None:
        relationship_id = _stable_id("edge", project_id, source_type, source_id, relation, target_type, target_id, context)
        conn.execute(
            """INSERT INTO evidence_graph_relationships (relationship_id,workspace_id,project_id,source_type,source_id,relationship_type,target_type,target_id,context_json,occurred_at)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(relationship_id) DO NOTHING""",
            (relationship_id, workspace_id, project_id, source_type, source_id, relation, target_type, target_id, _json(context), occurred_at),
        )

    @staticmethod
    def _claims_for_evidence(conn: Any, project_id: str, evidence_id: str) -> list[str]:
        return [row["target_id"] for row in conn.execute(
            "SELECT target_id FROM evidence_graph_relationships WHERE project_id = ? AND source_type = 'Evidence' AND source_id = ? AND relationship_type = 'SUPPORTS' AND target_type = 'Claim'",
            (project_id, evidence_id),
        ).fetchall()]

    def write_memory(self, record: dict[str, Any], workspace_id: str, site_id: str | None = None) -> None:
        if not self._postgres:
            return
        with self.store._get_conn() as conn:
            conn.execute(
                """INSERT INTO project_memory_records (memory_id,workspace_id,project_id,site_id,kind,content_json,provenance_json,valid_from,valid_until,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(memory_id) DO NOTHING""",
                (record["memory_id"], workspace_id, record["project_id"], site_id, record["kind"], _json(record["content"]), _json(record["provenance"]), None, None, _timestamp(record["created_at"])),
            )
            if record["kind"] == "EPISODIC":
                content = record["content"]
                conn.execute(
                    """INSERT INTO project_episodes (episode_id,workspace_id,project_id,site_id,event_type,summary,evidence_ids_json,decision_ids_json,action_ids_json,snapshot_ids_json,occurred_at,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(episode_id) DO NOTHING""",
                    (record["memory_id"], workspace_id, record["project_id"], site_id, content.get("event_type", "PROJECT_EVENT"), content.get("summary", "Project episode"),
                     _json(content.get("evidence_ids", [])), _json(content.get("decision_ids", [])), _json(content.get("action_ids", [])), _json(content.get("snapshot_ids", [])), _timestamp(record["created_at"]), _timestamp(record["created_at"])),
                )

    def store_document(self, document: dict[str, Any], chunks: list[dict[str, Any]]) -> None:
        """Store source metadata and retrieval vectors; the source artifact remains immutable in S3."""
        if not self._postgres:
            return
        with self.store._get_conn() as conn:
            conn.execute(
                """INSERT INTO documents (document_id,workspace_id,project_id,source_url,source_type,content_hash,metadata_json,created_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(document_id) DO NOTHING""",
                (document["document_id"], document["workspace_id"], document.get("project_id"), document.get("source_url"), document.get("source_type"),
                 document["content_hash"], _json(document["metadata"]), document["created_at"]),
            )
            for chunk in chunks:
                embedding = "[" + ",".join(format(float(value), ".9g") for value in chunk["embedding"]) + "]"
                conn.execute(
                    """INSERT INTO document_chunks (chunk_id,workspace_id,project_id,document_id,ordinal,content,source_metadata_json,embedding,embedding_model,embedding_dimensions,created_at)
                    VALUES (?,?,?,?,?,?,?,?::vector,?,?,?) ON CONFLICT(document_id,ordinal) DO NOTHING""",
                    (chunk["chunk_id"], document["workspace_id"], document.get("project_id"), document["document_id"], chunk["ordinal"], chunk["content"],
                     _json(chunk["source_metadata"]), embedding, chunk["embedding_model"], chunk["embedding_dimensions"], chunk["created_at"]),
                )

    def search_document_chunks(self, project_id: str, query_embedding: list[float], limit: int = 6, *, as_of: float | None = None) -> list[dict[str, Any]]:
        """Vector candidate retrieval; callers must combine it with graph and temporal constraints."""
        if not self._postgres:
            return []
        vector = "[" + ",".join(format(float(value), ".9g") for value in query_embedding) + "]"
        query = """SELECT chunk_id,document_id,ordinal,content,source_metadata_json,embedding_model,created_at,embedding <=> ?::vector AS distance
                FROM document_chunks WHERE project_id = ? AND embedding IS NOT NULL"""
        params: list[Any] = [vector, project_id]
        if as_of is not None:
            query += " AND COALESCE((source_metadata_json->>'retrieved_at')::double precision, created_at) <= ?"
            params.append(as_of)
        query += " ORDER BY embedding <=> ?::vector LIMIT ?"
        params.extend([vector, limit])
        with self.store._get_conn() as conn:
            rows = conn.execute(
                query, params,
            ).fetchall()
        return [{"chunk_id": row["chunk_id"], "document_id": row["document_id"], "ordinal": row["ordinal"], "content": row["content"],
                 "source_metadata": _decoded(row["source_metadata_json"], {}), "embedding_model": row["embedding_model"], "created_at": row["created_at"],
                 "distance": float(row["distance"])} for row in rows]

    def resolve_site(self, project_id: str, reference: str) -> dict[str, Any] | None:
        project = self._project(project_id)
        target = reference.casefold().strip()
        for candidate in project.get("candidates", []):
            summary = candidate.get("summary") or {}
            identifiers = [candidate.get("site_id"), candidate.get("parcel_id"), summary.get("title"), candidate.get("raw_input")]
            if any(target == str(value).casefold().strip() for value in identifiers if value):
                return {"site_id": candidate.get("site_id"), "snapshot_id": candidate.get("snapshot_id"), "candidate_id": candidate.get("candidate_id")}
        return None

    def find_supporting_evidence(self, project_id: str, claim: str) -> list[dict[str, Any]]:
        project = self._project(project_id)
        intelligence = project.get("project_intelligence") or {}
        if self._postgres:
            self.sync_project(project)
            with self.store._get_conn() as conn:
                rows = conn.execute(
                    """SELECT records.* FROM evidence_records AS records JOIN evidence_graph_relationships AS edges
                    ON edges.source_type='Evidence' AND edges.source_id=records.evidence_id AND edges.project_id=records.project_id
                    WHERE records.project_id = ? AND edges.target_type='Claim' AND edges.relationship_type='SUPPORTS'
                    AND (edges.target_id = ? OR EXISTS (SELECT 1 FROM claim_records claims WHERE claims.claim_id=edges.target_id AND claims.claim_text ILIKE ?))
                    ORDER BY records.observed_at DESC NULLS LAST""", (project_id, claim, f"%{claim}%"),
                ).fetchall()
                return [self._evidence_row(row) for row in rows]
        claim_words = _words(claim)
        coverage = [
            item for item in intelligence.get("evidence_coverage", [])
            if claim == self._claim_from_coverage(project_id, item)["claim_id"]
            or claim_words & _words(f"{item.get('requirement_id','')} {item.get('title','')}")
        ]
        ids = {value for item in coverage for value in item.get("evidence_ids", [])}
        return [copy.deepcopy(item) for item in intelligence.get("evidence_items", []) if item.get("evidence_id") in ids]

    def find_evidence_at_snapshot(self, project_id: str, snapshot_id: str) -> list[dict[str, Any]]:
        """Read immutable snapshot evidence; never replace it with current state."""
        project = self._project(project_id)
        snapshot = self.store.get_site_snapshot(snapshot_id)
        if snapshot is None or snapshot.get("workspace_id") != project.get("workspace_id"):
            return []
        return [
            {
                "evidence_id": field, "snapshot_id": snapshot_id, "site_id": snapshot.get("site_id"),
                "observed_at": record.get("observed_at"), "expires_at": record.get("expires_at"),
                "source": record.get("provider") or record.get("source"), "scope": record.get("scope"),
                "semantic_strength": record.get("semantic_strength"), "value": copy.deepcopy(record.get("value")),
                "evidence_hash": hashlib.sha256(_json(record).encode()).hexdigest(),
            }
            for field, record in sorted(snapshot.get("evidence", {}).items()) if isinstance(record, dict)
        ]

    def find_claims_for_requirement(self, project_id: str, requirement_id: str, *, as_of: float | None = None) -> list[dict[str, Any]]:
        if self._postgres:
            project = self._project(project_id)
            self.sync_project(project)
            query = "SELECT * FROM claim_records WHERE project_id = ? AND normalized_subject = ?"
            params: list[Any] = [project_id, requirement_id]
            if as_of is not None:
                query += " AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)"
                params.extend([as_of, as_of])
            query += " ORDER BY valid_from DESC"
            with self.store._get_conn() as conn:
                return [self._claim_row(row) for row in conn.execute(query, params).fetchall()]
        intelligence = self._project(project_id).get("project_intelligence") or {}
        return [self._claim_from_coverage(project_id, item) for item in intelligence.get("evidence_coverage", []) if item.get("requirement_id") == requirement_id]

    def find_claims_at_time(self, project_id: str, requirement_id: str, at: float) -> list[dict[str, Any]]:
        return self.find_claims_for_requirement(project_id, requirement_id, as_of=at)

    def find_decisions_for_site(self, project_id: str, site_id: str) -> list[dict[str, Any]]:
        project = self._project(project_id)
        if self._postgres:
            self.sync_project(project)
            with self.store._get_conn() as conn:
                rows = conn.execute("SELECT decision_id, decision_type, status, payload_json, decided_at FROM decision_records WHERE project_id = ? AND site_id = ? ORDER BY decided_at DESC", (project_id, site_id)).fetchall()
            return [{"decision_id": row["decision_id"], "decision_type": row["decision_type"], "status": row["status"], "decided_at": row["decided_at"], **_decoded(row["payload_json"], {})} for row in rows]
        active = (project.get("project_intelligence") or {}).get("active_site") or {}
        if active.get("site_id") != site_id:
            return []
        return [copy.deepcopy(item) for item in project.get("decision_history", [])]

    def find_decisions_at_time(self, project_id: str, at: float) -> list[dict[str, Any]]:
        project = self._project(project_id)
        if self._postgres:
            self.sync_project(project)
            with self.store._get_conn() as conn:
                rows = conn.execute(
                    "SELECT decision_id, decision_type, status, payload_json, decided_at FROM decision_records WHERE project_id = ? AND decided_at <= ? ORDER BY decided_at DESC",
                    (project_id, at),
                ).fetchall()
            return [{"decision_id": row["decision_id"], "decision_type": row["decision_type"], "status": row["status"], "decided_at": row["decided_at"], **_decoded(row["payload_json"], {})} for row in rows]
        return [copy.deepcopy(item) for item in project.get("decision_history", []) if float(item.get("answered_at") or item.get("created_at") or 0) <= at]

    def find_evidence_for_decision(self, project_id: str, decision_id: str) -> list[dict[str, Any]]:
        project = self._project(project_id)
        if self._postgres:
            self.sync_project(project)
            with self.store._get_conn() as conn:
                rows = conn.execute(
                    """SELECT records.* FROM evidence_records AS records JOIN evidence_graph_relationships AS edges
                    ON edges.source_type = 'Evidence' AND edges.source_id = records.evidence_id AND edges.project_id = records.project_id
                    WHERE records.project_id = ? AND edges.relationship_type = 'SUPPORTS' AND edges.target_type = 'Decision' AND edges.target_id = ?""",
                    (project_id, decision_id),
                ).fetchall()
            return [self._evidence_row(row) for row in rows]
        decision = next((item for item in project.get("decision_history", []) if item.get("decision_id") == decision_id), None)
        if decision is None:
            return []
        evidence_ids = decision.get("evidence_ids", [])
        return [item for item in (project.get("project_intelligence") or {}).get("evidence_items", []) if item.get("evidence_id") in evidence_ids]

    def find_changes_since_snapshot(self, project_id: str, snapshot_id: str) -> list[dict[str, Any]]:
        return [item for item in self.store.list_project_changes(project_id) if item.get("snapshot_before") == snapshot_id or item.get("snapshot_after") == snapshot_id]

    def find_changes_between_snapshots(self, project_id: str, before_snapshot_id: str, after_snapshot_id: str) -> list[dict[str, Any]]:
        return [item for item in self.store.list_project_changes(project_id) if item.get("snapshot_before") == before_snapshot_id and item.get("snapshot_after") == after_snapshot_id]

    def find_scenarios_affected_by_change(self, project_id: str, change_id: str) -> list[dict[str, Any]]:
        change = next((item for item in self.store.list_project_changes(project_id) if item.get("change_id") == change_id), None)
        return copy.deepcopy(change.get("affected_scenarios", [])) if change else []

    def find_actions_resolving_gap(self, project_id: str, gap_id: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(item) for item in (self._project(project_id).get("project_intelligence") or {}).get("recommended_actions", []) if item.get("gap_id") == gap_id]

    def find_conflicts(self, project_id: str, requirement_id: str) -> list[dict[str, Any]]:
        if not self._postgres:
            return [item for item in self.find_claims_for_requirement(project_id, requirement_id) if item.get("status") == "CONTESTED"]
        project = self._project(project_id)
        self.sync_project(project)
        with self.store._get_conn() as conn:
            rows = conn.execute("SELECT * FROM claim_records WHERE project_id = ? AND normalized_subject = ? AND status = 'CONTESTED' ORDER BY updated_at DESC", (project_id, requirement_id)).fetchall()
        return [self._claim_row(row) for row in rows]

    def find_project_episodes(self, project_id: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
        records = self._memory_records(project_id, "EPISODIC")
        return self._rank(records, query, limit)

    def find_relevant_memory(self, project_id: str, query: str, limit: int = 12) -> list[dict[str, Any]]:
        project = self._project(project_id)
        items = self._memory_records(project_id, None)
        for coverage in (project.get("project_intelligence") or {}).get("evidence_coverage", []):
            items.append(self._claim_from_coverage(project_id, coverage))
        return self._rank(items, query, limit)

    def _memory_records(self, project_id: str, kind: str | None) -> list[dict[str, Any]]:
        if self._postgres:
            project = self._project(project_id)
            self.sync_project(project)
            query, params = "SELECT * FROM project_memory_records WHERE project_id = ?", [project_id]
            if kind:
                query += " AND kind = ?"
                params.append(kind)
            query += " ORDER BY created_at DESC"
            with self.store._get_conn() as conn:
                return [{"memory_id": row["memory_id"], "kind": row["kind"], "content": _decoded(row["content_json"], {}), "provenance": _decoded(row["provenance_json"], {}), "created_at": row["created_at"]} for row in conn.execute(query, params).fetchall()]
        return [copy.deepcopy(item) for item in self._project(project_id).get("orchestration_memory", []) if kind is None or item.get("kind") == kind]

    @staticmethod
    def _rank(items: Iterable[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
        terms = _words(query)
        scored = []
        for item in items:
            text = _json(item)
            overlap = len(terms & _words(text))
            if overlap:
                scored.append((overlap, item))
        return [copy.deepcopy(item) for _score, item in sorted(scored, key=lambda pair: pair[0], reverse=True)[:limit]]

    def _project(self, project_id: str) -> dict[str, Any]:
        project = self.store.get_diligence_project(project_id)
        if project is None:
            raise ValueError("Diligence project was not found.")
        return project

    @staticmethod
    def _evidence_row(row: Any) -> dict[str, Any]:
        return {"evidence_id": row["evidence_id"], "project_id": row["project_id"], "site_id": row["site_id"], "snapshot_id": row["snapshot_id"], "source": row["source_type"], "scope": row["spatial_scope"], "observed_at": row["observed_at"], "expires_at": row["expires_at"], "evidence_hash": row["content_hash"], **_decoded(row["metadata_json"], {})}

    @staticmethod
    def _claim_row(row: Any) -> dict[str, Any]:
        return {key: row[key] for key in ("claim_id", "project_id", "site_id", "claim_text", "normalized_subject", "predicate", "normalized_object", "status", "semantic_strength", "valid_from", "valid_until", "superseded_by", "verification_status", "created_at", "updated_at")} | {"provenance": _decoded(row["provenance_json"], {})}

    @staticmethod
    def _claim_from_coverage(project_id: str, coverage: dict[str, Any]) -> dict[str, Any]:
        evidence_ids = list(coverage.get("evidence_ids", []))
        status = "UNSUPPORTED" if coverage.get("semantic_strength") == "UNSUPPORTED_SEMANTICS" else "ACTIVE"
        return {"claim_id": _stable_id("claim", project_id, coverage.get("requirement_id"), coverage.get("snapshot_id"), coverage.get("status"), evidence_ids), "project_id": project_id, "claim_text": f"{coverage.get('title') or coverage.get('requirement_id')} is {coverage.get('status', 'UNRESOLVED')}", "normalized_subject": coverage.get("requirement_id"), "predicate": "has_readiness", "normalized_object": coverage.get("status"), "status": status, "semantic_strength": _strength(coverage.get("semantic_strength")), "verification_status": coverage.get("semantic_strength") or "UNVERIFIED", "provenance": {"snapshot_id": coverage.get("snapshot_id"), "evidence_ids": evidence_ids, "scope": coverage.get("evidence_scope")}, "valid_from": coverage.get("last_evaluated_at")}


class MemoryContextBuilder:
    """Build bounded packets; full state stays in PostgreSQL/project records."""

    def __init__(self, graph: EvidenceGraphRepository):
        self.graph = graph

    def build(self, project_id: str, query: str, audience: str, *, limit: int | None = None, token_limit: int | None = None) -> dict[str, Any]:
        budget = CONTEXT_BUDGETS[audience]
        if token_limit is not None:
            budget = ContextBudget(budget.name, min(budget.token_limit, token_limit))
        records = self.graph.find_relevant_memory(project_id, query, limit=limit or 20)
        selected: list[dict[str, Any]] = []
        excluded: list[str] = []
        used = 0
        for record in records:
            cost = max(1, len(_json(record)) // 4)
            identifier = str(record.get("memory_id") or record.get("claim_id") or "record")
            if used + cost > budget.token_limit:
                excluded.append(identifier)
                continue
            selected.append(record)
            used += cost
        return {"budget": budget.name, "token_limit": budget.token_limit, "context_tokens": used, "selected_records": selected, "excluded_record_ids": excluded}
