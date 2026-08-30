"""PostgreSQL/PostGIS compatibility repository for the existing application services.

The application still speaks the established WorkspaceStore method surface.  This
adapter keeps that surface while the stable IDs and immutable payloads move to
PostgreSQL; it does not make SQLite a production dependency.
"""

from __future__ import annotations

import re
import json
import hashlib
from contextlib import AbstractContextManager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.infrastructure.observability import span
from app.infrastructure.events import DomainEvent, EventType
from app.infrastructure.outbox import PostgresOutbox
from app.workspace.store import WorkspaceStore


_INSERT_OR_IGNORE = re.compile(r"INSERT\s+OR\s+IGNORE\s+INTO", re.IGNORECASE)


def _sql(query: str) -> str:
    """Translate the narrow SQLite SQL subset used by WorkspaceStore."""
    ignore_conflicts = bool(_INSERT_OR_IGNORE.search(query))
    query = _INSERT_OR_IGNORE.sub("INSERT INTO", query)
    if ignore_conflicts:
        query = query.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return query.replace("?", "%s")


class _PostgresConnection(AbstractContextManager):
    def __init__(self, database_url: str):
        self._connection = psycopg.connect(database_url.replace("postgresql+psycopg://", "postgresql://", 1), row_factory=dict_row)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._connection.__exit__(*args)

    def execute(self, query: str, params: Any = None):
        return self._connection.execute(_sql(query), params)

    def executemany(self, query: str, params_seq: Any):
        return self._connection.cursor().executemany(_sql(query), params_seq)


class PostgresWorkspaceStore(WorkspaceStore):
    """Active production repository; schema changes are owned by Alembic."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._get_unchecked_conn() as conn:
            missing = [
                table
                for table in ("workspaces", "site_snapshots", "diligence_projects", "world_snapshots")
                if conn.execute("SELECT to_regclass(%s) AS name", (table,)).fetchone()["name"] is None
            ]
        if missing:
            raise RuntimeError("PostgreSQL schema is not migrated; run `alembic upgrade head` before starting the API.")
        self._initialized = True

    def _get_unchecked_conn(self) -> _PostgresConnection:
        return _PostgresConnection(self.database_url)

    def _get_conn(self) -> _PostgresConnection:
        if not self._initialized:
            raise RuntimeError("PostgresWorkspaceStore is not initialized. Call initialize() during application startup.")
        with span("database.connect", **{"db.system": "postgresql"}):
            return self._get_unchecked_conn()

    def save_diligence_project(self, project: dict) -> dict:
        """Save state and its creation event atomically through the outbox."""
        payload = json.dumps(project, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        with self._get_conn() as conn:
            previous = conn.execute("SELECT state_json FROM diligence_projects WHERE project_id = ?", (project["project_id"],)).fetchone()
            conn.execute(
                """INSERT INTO diligence_projects (project_id, workspace_id, status, state_json, state_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET status = excluded.status,
                state_json = excluded.state_json, state_hash = excluded.state_hash, updated_at = excluded.updated_at""",
                (
                    project["project_id"],
                    project["workspace_id"],
                    project["status"],
                    payload,
                    self._project_hash(project),
                    project["created_at"],
                    project["updated_at"],
                ),
            )
            if previous is None:
                PostgresOutbox(self.database_url).append(
                    conn._connection,
                    DomainEvent(
                        event_type=EventType.PROJECT_CREATED,
                        aggregate_type="Project",
                        aggregate_id=project["project_id"],
                        workspace_id=project["workspace_id"],
                        project_id=project["project_id"],
                        payload={"status": project["status"]},
                    ),
                )
            else:
                previous_state = json.loads(previous["state_json"])
                outbox = PostgresOutbox(self.database_url)
                for event in self._orchestration_events(previous_state, project):
                    outbox.append(conn._connection, event)
        # Graph rows are a queryable projection of authoritative project state.
        # Keeping this after the state transaction avoids a second source of truth.
        from app.ai.memory.graph import EvidenceGraphRepository

        EvidenceGraphRepository(self).sync_project(project)
        return project

    @staticmethod
    def _orchestration_events(previous: dict, current: dict) -> list[DomainEvent]:
        mapping = {
            "RUN_STARTED": EventType.ORCHESTRATION_STARTED,
            "TASK_STARTED": EventType.TASK_STARTED,
            "TASK_COMPLETED": EventType.TASK_COMPLETED,
            "VERIFICATION": EventType.VERIFICATION_COMPLETED,
            "REPLAN": EventType.REPLAN_CREATED,
            "NEEDS_USER_DECISION": EventType.DECISION_REQUIRED,
            "RESUMED": EventType.DECISION_ANSWERED,
            "COMPLETED": EventType.ORCHESTRATION_COMPLETED,
            "FAILED": EventType.ORCHESTRATION_FAILED,
        }
        seen = {
            (run.get("run_id"), event.get("sequence"))
            for run in previous.get("orchestration_runs", []) for event in run.get("events", [])
        }
        result = []
        for run in current.get("orchestration_runs", []):
            run_id = run.get("run_id")
            for item in run.get("events", []):
                event_type = mapping.get(item.get("type"))
                if event_type is None or (run_id, item.get("sequence")) in seen:
                    continue
                identity = f"{run_id}:{item.get('sequence')}:{item.get('type')}"
                payload = {key: value for key, value in item.items() if key not in {"decision_request"}}
                if isinstance(item.get("decision_request"), dict):
                    payload["decision_id"] = item["decision_request"].get("decision_id")
                result.append(DomainEvent(
                    event_id=f"event_{hashlib.sha256(identity.encode()).hexdigest()[:32]}",
                    event_type=event_type,
                    aggregate_type="OrchestrationRun",
                    aggregate_id=run_id,
                    workspace_id=current["workspace_id"],
                    project_id=current["project_id"],
                    correlation_id=run_id,
                    payload=payload,
                ))
        return result

    @staticmethod
    def _project_hash(project: dict) -> str:
        import hashlib

        return hashlib.sha256(json.dumps(project, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
