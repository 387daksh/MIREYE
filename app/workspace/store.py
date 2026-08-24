"""
SQLite-backed persistence for the Agentic Memory Architecture (Workspaces).

Maintains an append-only ledger of observations, versions, site mappings,
and staleness logs. Enables fast state retrieval, rejection tracking,
and historical time-travel replay.
"""
from __future__ import annotations

import json
import hashlib
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import WORKSPACE_DB


def _json_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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

                CREATE TABLE IF NOT EXISTS sandbox_sites (
                    site_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
                );

                CREATE TABLE IF NOT EXISTS site_snapshot_sites (
                    snapshot_id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (snapshot_id) REFERENCES site_snapshots(snapshot_id),
                    FOREIGN KEY (site_id) REFERENCES sandbox_sites(site_id)
                );

                CREATE TABLE IF NOT EXISTS site_parcel_reconciliations (
                    site_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    mireye_parcel_id TEXT NOT NULL,
                    parcel_apn TEXT,
                    identity_hash TEXT NOT NULL,
                    geometry_hash TEXT NOT NULL,
                    match_type TEXT,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (site_id, snapshot_id),
                    FOREIGN KEY (site_id) REFERENCES sandbox_sites(site_id),
                    FOREIGN KEY (snapshot_id) REFERENCES site_snapshots(snapshot_id)
                );

                CREATE TABLE IF NOT EXISTS mireye_spend_plans (
                    spend_plan_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    confirmed_at REAL,
                    completed_at REAL,
                    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id),
                    FOREIGN KEY (site_id) REFERENCES sandbox_sites(site_id),
                    FOREIGN KEY (snapshot_id) REFERENCES site_snapshots(snapshot_id)
                );

                CREATE TABLE IF NOT EXISTS scenario_evidence_dependencies (
                    scenario_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    site_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    constraint_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (scenario_id, revision, constraint_id, evidence_id),
                    FOREIGN KEY (scenario_id, revision) REFERENCES scenario_versions(scenario_id, revision),
                    FOREIGN KEY (site_id) REFERENCES sandbox_sites(site_id),
                    FOREIGN KEY (snapshot_id) REFERENCES site_snapshots(snapshot_id)
                );

                CREATE TABLE IF NOT EXISTS scenario_evaluation_runs (
                    evaluation_run_id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    site_id TEXT NOT NULL,
                    source_snapshot_id TEXT NOT NULL,
                    evaluated_snapshot_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    affected_constraint_ids_json TEXT NOT NULL,
                    snapshot_diff_json TEXT NOT NULL,
                    evaluation_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (site_id) REFERENCES sandbox_sites(site_id),
                    FOREIGN KEY (source_snapshot_id) REFERENCES site_snapshots(snapshot_id),
                    FOREIGN KEY (evaluated_snapshot_id) REFERENCES site_snapshots(snapshot_id)
                );

                CREATE TABLE IF NOT EXISTS world_snapshots (
                    world_snapshot_id TEXT PRIMARY KEY,
                    site_snapshot_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    snapshot_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (site_snapshot_id) REFERENCES site_snapshots(snapshot_id)
                );

                CREATE TABLE IF NOT EXISTS diligence_projects (
                    project_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    state_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
                );

                CREATE INDEX IF NOT EXISTS idx_obs_ws_key ON observations(workspace_id, local_key);
                CREATE INDEX IF NOT EXISTS idx_obs_ws_created ON observations(workspace_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_snapshots_workspace_created ON site_snapshots(workspace_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_scenarios_workspace_created ON scenario_versions(workspace_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_scenarios_snapshot_created ON scenario_versions(site_snapshot_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_snapshot_sites_site_created ON site_snapshot_sites(site_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_reconciliations_site_created ON site_parcel_reconciliations(site_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_spend_plans_snapshot_created ON mireye_spend_plans(snapshot_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_dependencies_site_evidence ON scenario_evidence_dependencies(site_id, evidence_id);
                CREATE INDEX IF NOT EXISTS idx_evaluation_runs_site_created ON scenario_evaluation_runs(site_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_world_snapshots_site_created ON world_snapshots(site_snapshot_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_diligence_projects_workspace_updated ON diligence_projects(workspace_id, updated_at);
            """)
            scenario_columns = {row["name"] for row in conn.execute("PRAGMA table_info(scenario_versions)").fetchall()}
            if "world_snapshot_id" not in scenario_columns:
                conn.execute("ALTER TABLE scenario_versions ADD COLUMN world_snapshot_id TEXT REFERENCES world_snapshots(world_snapshot_id)")

    def create_workspace(self, workspace_id: str, label: str = "") -> None:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO workspaces (workspace_id, label, created_at) VALUES (?, ?, ?)",
                (workspace_id, label, time.time()),
            )

    def save_diligence_project(self, project: dict) -> dict:
        payload = json.dumps(project, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO diligence_projects (
                    project_id, workspace_id, status, state_json, state_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    status = excluded.status,
                    state_json = excluded.state_json,
                    state_hash = excluded.state_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    project["project_id"], project["workspace_id"], project["status"], payload,
                    _json_hash(project), project["created_at"], project["updated_at"],
                ),
            )
        return project

    def get_diligence_project(self, project_id: str) -> dict | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT state_json FROM diligence_projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def list_diligence_projects(self, workspace_id: str) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT state_json FROM diligence_projects WHERE workspace_id = ? ORDER BY updated_at DESC",
                (workspace_id,),
            ).fetchall()
        return [json.loads(row["state_json"]) for row in rows]

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

    def get_or_create_sandbox_site(self, workspace_id: str, parcel_identity: dict, *, site_id: str | None = None) -> str:
        """Return a stable internal Site identity without equating it to a provider parcel ID."""
        self.create_workspace(workspace_id)
        parcel_id = str(parcel_identity["parcel_id"])
        with self._get_conn() as conn:
            if site_id is not None:
                row = conn.execute(
                    "SELECT site_id FROM sandbox_sites WHERE site_id = ? AND workspace_id = ?",
                    (site_id, workspace_id),
                ).fetchone()
                if row is None:
                    raise ValueError("Site does not belong to this workspace.")
                return row["site_id"]
            row = conn.execute(
                """
                SELECT sites.site_id
                FROM sandbox_sites AS sites
                JOIN site_parcel_reconciliations AS reconciliations ON reconciliations.site_id = sites.site_id
                WHERE sites.workspace_id = ? AND reconciliations.mireye_parcel_id = ?
                ORDER BY reconciliations.created_at DESC
                LIMIT 1
                """,
                (workspace_id, parcel_id),
            ).fetchone()
            if row is not None:
                return row["site_id"]
            new_site_id = f"site_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO sandbox_sites (site_id, workspace_id, created_at) VALUES (?, ?, ?)",
                (new_site_id, workspace_id, time.time()),
            )
            return new_site_id

    def get_site_id_for_snapshot(self, snapshot_id: str) -> str | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT site_id FROM site_snapshot_sites WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
        return row["site_id"] if row else None

    def ensure_snapshot_site(self, snapshot: dict) -> str:
        """Add a relationship for pre-lifecycle snapshots without rewriting their content."""
        existing = self.get_site_id_for_snapshot(snapshot["snapshot_id"])
        if existing is not None:
            return existing
        site_id = self.get_or_create_sandbox_site(snapshot["workspace_id"], snapshot["parcel_identity"])
        identity = snapshot["parcel_identity"]
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO site_snapshot_sites (snapshot_id, site_id, created_at) VALUES (?, ?, ?)",
                (snapshot["snapshot_id"], site_id, snapshot["created_at"]),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO site_parcel_reconciliations (
                    site_id, snapshot_id, mireye_parcel_id, parcel_apn, identity_hash,
                    geometry_hash, match_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    site_id, snapshot["snapshot_id"], identity["parcel_id"], identity.get("parcel_apn"),
                    _json_hash(identity), _json_hash(snapshot["geometry"]), identity.get("parcel_match_type"),
                    snapshot["created_at"],
                ),
            )
        return site_id

    def list_site_snapshots(self, site_id: str) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT snapshot_id FROM site_snapshot_sites
                WHERE site_id = ? ORDER BY created_at ASC
                """,
                (site_id,),
            ).fetchall()
        return [snapshot for row in rows if (snapshot := self.get_site_snapshot(row["snapshot_id"])) is not None]

    def create_site_snapshot(self, snapshot: dict) -> None:
        """Persist an immutable real-site snapshot. Existing snapshots are never updated."""
        self.create_workspace(snapshot["workspace_id"])
        site_id = self.get_or_create_sandbox_site(
            snapshot["workspace_id"], snapshot["parcel_identity"], site_id=snapshot.get("site_id")
        )
        snapshot["site_id"] = site_id
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
            identity = snapshot["parcel_identity"]
            conn.execute(
                "INSERT INTO site_snapshot_sites (snapshot_id, site_id, created_at) VALUES (?, ?, ?)",
                (snapshot["snapshot_id"], site_id, snapshot["created_at"]),
            )
            conn.execute(
                """
                INSERT INTO site_parcel_reconciliations (
                    site_id, snapshot_id, mireye_parcel_id, parcel_apn, identity_hash,
                    geometry_hash, match_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    site_id,
                    snapshot["snapshot_id"],
                    identity["parcel_id"],
                    identity.get("parcel_apn"),
                    _json_hash(identity),
                    _json_hash(snapshot["geometry"]),
                    identity.get("parcel_match_type"),
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
        snapshot = {
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
        snapshot["site_id"] = self.ensure_snapshot_site(snapshot)
        return snapshot

    def create_mireye_spend_plan(self, plan: dict) -> None:
        self.create_workspace(plan["workspace_id"])
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO mireye_spend_plans (
                    spend_plan_id, workspace_id, site_id, snapshot_id, plan_json,
                    status, created_at, confirmed_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan["spend_plan_id"], plan["workspace_id"], plan["site_id"],
                    plan["snapshot_id"], json.dumps(plan, sort_keys=True), plan["status"],
                    plan["created_at"], plan.get("confirmed_at"), plan.get("completed_at"),
                ),
            )

    def get_mireye_spend_plan(self, spend_plan_id: str) -> dict | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT plan_json, status, confirmed_at, completed_at FROM mireye_spend_plans WHERE spend_plan_id = ?",
                (spend_plan_id,),
            ).fetchone()
        if row is None:
            return None
        plan = json.loads(row["plan_json"])
        plan.update(status=row["status"], confirmed_at=row["confirmed_at"], completed_at=row["completed_at"])
        return plan

    def update_mireye_spend_plan(self, spend_plan_id: str, *, status: str, completed_at: float | None = None) -> None:
        confirmed_at = time.time() if status == "CONFIRMED" else None
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE mireye_spend_plans
                SET status = ?, confirmed_at = COALESCE(?, confirmed_at), completed_at = COALESCE(?, completed_at)
                WHERE spend_plan_id = ?
                """,
                (status, confirmed_at, completed_at, spend_plan_id),
            )

    def create_scenario_evaluation_run(self, run: dict) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO scenario_evaluation_runs (
                    evaluation_run_id, scenario_id, revision, site_id, source_snapshot_id,
                    evaluated_snapshot_id, status, affected_constraint_ids_json,
                    snapshot_diff_json, evaluation_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["evaluation_run_id"], run["scenario_id"], run["revision"], run["site_id"],
                    run["source_snapshot_id"], run["evaluated_snapshot_id"], run["status"],
                    json.dumps(run["affected_constraint_ids"], sort_keys=True),
                    json.dumps(run["snapshot_diff"], sort_keys=True),
                    json.dumps(run["evaluation"], sort_keys=True), run["created_at"],
                ),
            )

    def list_scenario_evaluation_runs(self, scenario_id: str, revision: int | None = None) -> list[dict]:
        query = "SELECT * FROM scenario_evaluation_runs WHERE scenario_id = ?"
        params: tuple[Any, ...] = (scenario_id,)
        if revision is not None:
            query += " AND revision = ?"
            params = (scenario_id, revision)
        query += " ORDER BY created_at ASC"
        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [{
            "evaluation_run_id": row["evaluation_run_id"], "scenario_id": row["scenario_id"],
            "revision": row["revision"], "site_id": row["site_id"],
            "source_snapshot_id": row["source_snapshot_id"], "evaluated_snapshot_id": row["evaluated_snapshot_id"],
            "status": row["status"], "affected_constraint_ids": json.loads(row["affected_constraint_ids_json"]),
            "snapshot_diff": json.loads(row["snapshot_diff_json"]), "evaluation": json.loads(row["evaluation_json"]),
            "created_at": row["created_at"],
        } for row in rows]

    def affected_scenario_constraints(self, site_id: str, evidence_ids: list[str]) -> list[dict]:
        if not evidence_ids:
            return []
        placeholders = ", ".join("?" for _ in evidence_ids)
        with self._get_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT dependencies.scenario_id, dependencies.revision, dependencies.constraint_id
                FROM scenario_evidence_dependencies AS dependencies
                WHERE dependencies.site_id = ? AND dependencies.evidence_id IN ({placeholders})
                ORDER BY dependencies.scenario_id, dependencies.revision, dependencies.constraint_id
                """,
                (site_id, *evidence_ids),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_scenario_version(self, scenario: dict) -> None:
        self.create_workspace(scenario["workspace_id"])
        site_id = self.get_site_id_for_snapshot(scenario["site_snapshot_id"])
        if site_id is None:
            raise ValueError("Scenario SiteSnapshot is not linked to a stable Site.")
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO scenario_versions (
                    scenario_id, workspace_id, revision, parent_scenario_id,
                    site_snapshot_id, world_snapshot_id, user_intent, scene_state_json,
                    requested_constraints_json, evaluation_json, state_hash,
                    geometry_engine_version, proposal_strategy_version, model_id,
                    tool_schema_version, accepted_tool_calls_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scenario["scenario_id"], scenario["workspace_id"], scenario["revision"],
                    scenario.get("parent_scenario_id"), scenario["site_snapshot_id"], scenario.get("world_snapshot_id"), scenario["user_intent"],
                    json.dumps(scenario["scene_state"], sort_keys=True),
                    json.dumps(scenario["requested_constraints"], sort_keys=True),
                    json.dumps(scenario["evaluation"], sort_keys=True), scenario["state_hash"],
                    scenario["geometry_engine_version"], scenario["proposal_strategy_version"],
                    scenario.get("model_id"), scenario["tool_schema_version"],
                    json.dumps(scenario["accepted_tool_calls"], sort_keys=True), scenario["created_at"],
                ),
            )
            for result in scenario["evaluation"].get("constraint_results", []):
                constraint_id = result.get("constraint_id")
                if not isinstance(constraint_id, str):
                    continue
                for evidence_id in result.get("evidence_ids", []):
                    if isinstance(evidence_id, str) and evidence_id:
                        conn.execute(
                            """
                            INSERT INTO scenario_evidence_dependencies (
                                scenario_id, revision, site_id, snapshot_id, constraint_id, evidence_id, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                scenario["scenario_id"], scenario["revision"], site_id,
                                scenario["site_snapshot_id"], constraint_id, evidence_id,
                                scenario["created_at"],
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

    def create_world_snapshot(self, snapshot: dict) -> dict:
        if self.get_site_snapshot(snapshot["site_snapshot_id"]) is None:
            raise ValueError("WorldSnapshot SiteSnapshot was not found.")
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO world_snapshots (
                    world_snapshot_id, site_snapshot_id, content_hash, snapshot_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snapshot["world_snapshot_id"], snapshot["site_snapshot_id"], snapshot["content_hash"],
                    json.dumps(snapshot, sort_keys=True), snapshot["created_at"],
                ),
            )
            row = conn.execute(
                "SELECT snapshot_json FROM world_snapshots WHERE content_hash = ?", (snapshot["content_hash"],)
            ).fetchone()
        return json.loads(row["snapshot_json"])

    def get_world_snapshot(self, world_snapshot_id: str) -> dict | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM world_snapshots WHERE world_snapshot_id = ?", (world_snapshot_id,)
            ).fetchone()
        return json.loads(row["snapshot_json"]) if row else None

    def latest_world_snapshot_for_site_snapshot(self, site_snapshot_id: str) -> dict | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM world_snapshots WHERE site_snapshot_id = ? ORDER BY created_at DESC LIMIT 1",
                (site_snapshot_id,),
            ).fetchone()
        return json.loads(row["snapshot_json"]) if row else None

    def list_world_snapshots(self) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT snapshot_json FROM world_snapshots ORDER BY created_at DESC"
            ).fetchall()
        return [json.loads(row["snapshot_json"]) for row in rows]

    @staticmethod
    def _scenario_row(row: sqlite3.Row) -> dict:
        return {
            "scenario_id": row["scenario_id"], "workspace_id": row["workspace_id"],
            "revision": row["revision"], "parent_scenario_id": row["parent_scenario_id"],
            "site_snapshot_id": row["site_snapshot_id"], "world_snapshot_id": row["world_snapshot_id"], "user_intent": row["user_intent"],
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
