"""
SQLite-backed persistence for the Agentic Memory Architecture (Workspaces).

Maintains an append-only ledger of observations, versions, site mappings,
and staleness logs. Enables fast state retrieval, rejection tracking,
and historical time-travel replay.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.config import WORKSPACE_DB


class WorkspaceStore:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path or WORKSPACE_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS workspaces (
                    workspace_id TEXT PRIMARY KEY,
                    label TEXT,
                    created_at REAL
                );

                CREATE TABLE IF NOT EXISTS site_mappings (
                    workspace_id TEXT,
                    local_key TEXT,
                    site_id TEXT,
                    created_at REAL,
                    PRIMARY KEY (workspace_id, local_key)
                );

                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT NOT NULL,
                    local_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    justification TEXT,
                    dossier_snapshot TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
                );

                CREATE TABLE IF NOT EXISTS staleness_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT NOT NULL,
                    local_key TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    detected_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS site_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    parcel_id TEXT NOT NULL,
                    identity_json TEXT NOT NULL,
                    geometry_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    raw_response_json TEXT NOT NULL,
                    raw_response_hash TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    field_catalog_version TEXT NOT NULL,
                    provider_metadata_json TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
                );

                CREATE TABLE IF NOT EXISTS scenario_versions (
                    scenario_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    parent_scenario_id TEXT,
                    site_snapshot_id TEXT NOT NULL,
                    user_intent TEXT NOT NULL,
                    scene_state_json TEXT NOT NULL,
                    requested_constraints_json TEXT NOT NULL,
                    evaluation_json TEXT NOT NULL,
                    state_hash TEXT NOT NULL,
                    geometry_engine_version TEXT NOT NULL,
                    proposal_strategy_version TEXT NOT NULL,
                    model_id TEXT,
                    tool_schema_version TEXT NOT NULL,
                    accepted_tool_calls_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (scenario_id, revision),
                    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id),
                    FOREIGN KEY (site_snapshot_id) REFERENCES site_snapshots(snapshot_id)
                );

                CREATE INDEX IF NOT EXISTS idx_obs_ws_key ON observations(workspace_id, local_key);
                CREATE INDEX IF NOT EXISTS idx_obs_ws_created ON observations(workspace_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_snapshots_workspace_created ON site_snapshots(workspace_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_scenarios_workspace_created ON scenario_versions(workspace_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_scenarios_snapshot_created ON scenario_versions(site_snapshot_id, created_at);
            """)

    def create_workspace(self, workspace_id: str, label: str = "") -> None:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO workspaces (workspace_id, label, created_at) VALUES (?, ?, ?)",
                (workspace_id, label, time.time()),
            )

    def get_site_id(self, workspace_id: str, local_key: str) -> str | None:
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT site_id FROM site_mappings WHERE workspace_id = ? AND local_key = ?",
                (workspace_id, local_key),
            )
            row = cur.fetchone()
            return row["site_id"] if row else None

    def register_site_id(self, workspace_id: str, local_key: str, site_id: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO site_mappings (workspace_id, local_key, site_id, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace_id, local_key) DO UPDATE SET site_id = excluded.site_id
                """,
                (workspace_id, local_key, site_id, time.time()),
            )

    def create_site_snapshot(self, snapshot: dict) -> None:
        """Persist an immutable real-site snapshot. Existing snapshots are never updated."""
        self.create_workspace(snapshot["workspace_id"])
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO site_snapshots (
                    snapshot_id, workspace_id, parcel_id, identity_json, geometry_json,
                    evidence_json, raw_response_json, raw_response_hash, request_json,
                    request_hash, field_catalog_version, provider_metadata_json,
                    observed_at, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot["snapshot_id"],
                    snapshot["workspace_id"],
                    snapshot["parcel_identity"]["parcel_id"],
                    json.dumps(snapshot["parcel_identity"], sort_keys=True),
                    json.dumps(snapshot["geometry"], sort_keys=True),
                    json.dumps(snapshot["evidence"], sort_keys=True),
                    json.dumps(snapshot["raw_response"], sort_keys=True),
                    snapshot["raw_response_hash"],
                    json.dumps(snapshot["request"], sort_keys=True),
                    snapshot["request_hash"],
                    snapshot["field_catalog_version"],
                    json.dumps(snapshot["provider_metadata"], sort_keys=True),
                    snapshot["observed_at"],
                    snapshot["expires_at"],
                    snapshot["created_at"],
                ),
            )

    def get_site_snapshot(self, snapshot_id: str) -> dict | None:
        with self._get_conn() as conn:
            row = conn.execute(
                """
                SELECT snapshot_id, workspace_id, identity_json, geometry_json, evidence_json,
                       raw_response_json, raw_response_hash, request_json, request_hash,
                       field_catalog_version, provider_metadata_json, observed_at, expires_at,
                       created_at
                FROM site_snapshots WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "snapshot_id": row["snapshot_id"],
            "workspace_id": row["workspace_id"],
            "parcel_identity": json.loads(row["identity_json"]),
            "geometry": json.loads(row["geometry_json"]),
            "evidence": json.loads(row["evidence_json"]),
            "raw_response": json.loads(row["raw_response_json"]),
            "raw_response_hash": row["raw_response_hash"],
            "request": json.loads(row["request_json"]),
            "request_hash": row["request_hash"],
            "field_catalog_version": row["field_catalog_version"],
            "provider_metadata": json.loads(row["provider_metadata_json"]),
            "observed_at": row["observed_at"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
        }

    def create_scenario_version(self, scenario: dict) -> None:
        self.create_workspace(scenario["workspace_id"])
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO scenario_versions (
                    scenario_id, workspace_id, revision, parent_scenario_id,
                    site_snapshot_id, user_intent, scene_state_json,
                    requested_constraints_json, evaluation_json, state_hash,
                    geometry_engine_version, proposal_strategy_version, model_id,
                    tool_schema_version, accepted_tool_calls_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scenario["scenario_id"], scenario["workspace_id"], scenario["revision"],
                    scenario.get("parent_scenario_id"), scenario["site_snapshot_id"], scenario["user_intent"],
                    json.dumps(scenario["scene_state"], sort_keys=True),
                    json.dumps(scenario["requested_constraints"], sort_keys=True),
                    json.dumps(scenario["evaluation"], sort_keys=True), scenario["state_hash"],
                    scenario["geometry_engine_version"], scenario["proposal_strategy_version"],
                    scenario.get("model_id"), scenario["tool_schema_version"],
                    json.dumps(scenario["accepted_tool_calls"], sort_keys=True), scenario["created_at"],
                ),
            )

    def get_scenario_version(self, scenario_id: str, revision: int | None = None) -> dict | None:
        query = "SELECT * FROM scenario_versions WHERE scenario_id = ?"
        params: tuple = (scenario_id,)
        if revision is not None:
            query += " AND revision = ?"
            params = (scenario_id, revision)
        query += " ORDER BY revision DESC LIMIT 1"
        with self._get_conn() as conn:
            row = conn.execute(query, params).fetchone()
        return self._scenario_row(row) if row else None

    def list_scenario_versions(self, scenario_id: str) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM scenario_versions WHERE scenario_id = ? ORDER BY revision ASC", (scenario_id,)
            ).fetchall()
        return [self._scenario_row(row) for row in rows]

    @staticmethod
    def _scenario_row(row: sqlite3.Row) -> dict:
        return {
            "scenario_id": row["scenario_id"], "workspace_id": row["workspace_id"],
            "revision": row["revision"], "parent_scenario_id": row["parent_scenario_id"],
            "site_snapshot_id": row["site_snapshot_id"], "user_intent": row["user_intent"],
            "scene_state": json.loads(row["scene_state_json"]),
            "requested_constraints": json.loads(row["requested_constraints_json"]),
            "evaluation": json.loads(row["evaluation_json"]), "state_hash": row["state_hash"],
            "geometry_engine_version": row["geometry_engine_version"],
            "proposal_strategy_version": row["proposal_strategy_version"],
            "model_id": row["model_id"], "tool_schema_version": row["tool_schema_version"],
            "accepted_tool_calls": json.loads(row["accepted_tool_calls_json"]),
            "created_at": row["created_at"],
        }

    def observe(
        self,
        workspace_id: str,
        local_key: str,
        status: str,
        justification: str,
        snapshot: dict,
    ) -> int:
        """Append an observation and return the new version number for this key."""
        self.create_workspace(workspace_id)
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next_ver FROM observations WHERE workspace_id = ? AND local_key = ?",
                (workspace_id, local_key),
            )
            next_version = cur.fetchone()["next_ver"]

            snapshot_json = json.dumps(snapshot)
            conn.execute(
                """
                INSERT INTO observations (workspace_id, local_key, version, status, justification, dossier_snapshot, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (workspace_id, local_key, next_version, status, justification, snapshot_json, time.time()),
            )
            return next_version

    def state(self, workspace_id: str) -> list[dict]:
        """Return the latest observation record for each local_key in the workspace."""
        with self._get_conn() as conn:
            cur = conn.execute(
                """
                SELECT o.local_key, o.version, o.status, o.justification, o.dossier_snapshot, o.created_at
                FROM observations o
                INNER JOIN (
                    SELECT local_key, MAX(version) as max_version
                    FROM observations
                    WHERE workspace_id = ?
                    GROUP BY local_key
                ) latest ON o.local_key = latest.local_key AND o.version = latest.max_version
                WHERE o.workspace_id = ?
                ORDER BY o.created_at DESC
                """,
                (workspace_id, workspace_id),
            )
            results = []
            for row in cur.fetchall():
                results.append({
                    "local_key": row["local_key"],
                    "version": row["version"],
                    "status": row["status"],
                    "justification": row["justification"],
                    "dossier_snapshot": json.loads(row["dossier_snapshot"]),
                    "created_at": row["created_at"],
                })
            return results

    def log_staleness(
        self,
        workspace_id: str,
        local_key: str,
        field_name: str,
        old_val: Any,
        new_val: Any,
    ) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO staleness_log (workspace_id, local_key, field_name, old_value, new_value, detected_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    local_key,
                    field_name,
                    json.dumps(old_val),
                    json.dumps(new_val),
                    time.time(),
                ),
            )

    def replay(self, workspace_id: str, as_of_ts: float) -> list[dict]:
        """Reconstruct the exact workspace state as it existed at timestamp `as_of_ts`."""
        with self._get_conn() as conn:
            cur = conn.execute(
                """
                SELECT o.local_key, o.version, o.status, o.justification, o.dossier_snapshot, o.created_at
                FROM observations o
                INNER JOIN (
                    SELECT local_key, MAX(version) as max_version
                    FROM observations
                    WHERE workspace_id = ? AND created_at <= ?
                    GROUP BY local_key
                ) latest ON o.local_key = latest.local_key AND o.version = latest.max_version
                WHERE o.workspace_id = ? AND o.created_at <= ?
                ORDER BY o.created_at DESC
                """,
                (workspace_id, as_of_ts, workspace_id, as_of_ts),
            )
            results = []
            for row in cur.fetchall():
                results.append({
                    "local_key": row["local_key"],
                    "version": row["version"],
                    "status": row["status"],
                    "justification": row["justification"],
                    "dossier_snapshot": json.loads(row["dossier_snapshot"]),
                    "created_at": row["created_at"],
                })
            return results

    def history(self, workspace_id: str, local_key: str) -> list[dict]:
        """Return the chronological ledger of all observations for a site."""
        with self._get_conn() as conn:
            cur = conn.execute(
                """
                SELECT local_key, version, status, justification, dossier_snapshot, created_at
                FROM observations
                WHERE workspace_id = ? AND local_key = ?
                ORDER BY version ASC
                """,
                (workspace_id, local_key),
            )
            results = []
            for row in cur.fetchall():
                results.append({
                    "local_key": row["local_key"],
                    "version": row["version"],
                    "status": row["status"],
                    "justification": row["justification"],
                    "dossier_snapshot": json.loads(row["dossier_snapshot"]),
                    "created_at": row["created_at"],
                })
            return results
