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

                CREATE INDEX IF NOT EXISTS idx_obs_ws_key ON observations(workspace_id, local_key);
                CREATE INDEX IF NOT EXISTS idx_obs_ws_created ON observations(workspace_id, created_at);
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
