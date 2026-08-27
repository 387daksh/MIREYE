"""Create the PostgreSQL/PostGIS production schema.

This intentionally leaves SQLite untouched: SQLite remains a test/demo source
and must be copied with the explicit migration command.
"""

from alembic import op


revision = "20260825_0002"
down_revision = "20260825_0001"
branch_labels = None
depends_on = None


_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS workspaces (workspace_id TEXT PRIMARY KEY, label TEXT, created_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS site_mappings (workspace_id TEXT REFERENCES workspaces(workspace_id), local_key TEXT, site_id TEXT, created_at DOUBLE PRECISION, PRIMARY KEY (workspace_id, local_key));
CREATE TABLE IF NOT EXISTS observations (id BIGSERIAL PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id), local_key TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL, justification TEXT, dossier_snapshot TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS staleness_log (id BIGSERIAL PRIMARY KEY, workspace_id TEXT NOT NULL, local_key TEXT NOT NULL, field_name TEXT NOT NULL, old_value TEXT, new_value TEXT, detected_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS site_snapshots (
    snapshot_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id), parcel_id TEXT NOT NULL,
    identity_json TEXT NOT NULL, geometry_json TEXT NOT NULL, parcel_geometry geometry(GEOMETRY, 4326), evidence_json TEXT NOT NULL,
    raw_response_json TEXT NOT NULL, raw_response_hash TEXT NOT NULL, request_json TEXT NOT NULL, request_hash TEXT NOT NULL,
    field_catalog_version TEXT NOT NULL, provider_metadata_json TEXT NOT NULL, observed_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL, created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS sandbox_sites (site_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id), created_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS site_snapshot_sites (snapshot_id TEXT PRIMARY KEY REFERENCES site_snapshots(snapshot_id), site_id TEXT NOT NULL REFERENCES sandbox_sites(site_id), created_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS site_parcel_reconciliations (site_id TEXT NOT NULL REFERENCES sandbox_sites(site_id), snapshot_id TEXT NOT NULL REFERENCES site_snapshots(snapshot_id), mireye_parcel_id TEXT NOT NULL, parcel_apn TEXT, identity_hash TEXT NOT NULL, geometry_hash TEXT NOT NULL, match_type TEXT, created_at DOUBLE PRECISION NOT NULL, PRIMARY KEY (site_id, snapshot_id));
CREATE TABLE IF NOT EXISTS mireye_spend_plans (spend_plan_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id), site_id TEXT NOT NULL REFERENCES sandbox_sites(site_id), snapshot_id TEXT NOT NULL REFERENCES site_snapshots(snapshot_id), plan_json TEXT NOT NULL, status TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL, confirmed_at DOUBLE PRECISION, completed_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS world_snapshots (world_snapshot_id TEXT PRIMARY KEY, site_snapshot_id TEXT NOT NULL REFERENCES site_snapshots(snapshot_id), content_hash TEXT NOT NULL UNIQUE, snapshot_json TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS scenario_versions (scenario_id TEXT NOT NULL, workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id), revision INTEGER NOT NULL, parent_scenario_id TEXT, site_snapshot_id TEXT NOT NULL REFERENCES site_snapshots(snapshot_id), world_snapshot_id TEXT REFERENCES world_snapshots(world_snapshot_id), user_intent TEXT NOT NULL, scene_state_json TEXT NOT NULL, requested_constraints_json TEXT NOT NULL, evaluation_json TEXT NOT NULL, state_hash TEXT NOT NULL, geometry_engine_version TEXT NOT NULL, proposal_strategy_version TEXT NOT NULL, model_id TEXT, tool_schema_version TEXT NOT NULL, accepted_tool_calls_json TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL, PRIMARY KEY (scenario_id, revision));
CREATE TABLE IF NOT EXISTS scenario_evidence_dependencies (scenario_id TEXT NOT NULL, revision INTEGER NOT NULL, site_id TEXT NOT NULL REFERENCES sandbox_sites(site_id), snapshot_id TEXT NOT NULL REFERENCES site_snapshots(snapshot_id), constraint_id TEXT NOT NULL, evidence_id TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL, PRIMARY KEY (scenario_id, revision, constraint_id, evidence_id), FOREIGN KEY (scenario_id, revision) REFERENCES scenario_versions(scenario_id, revision));
CREATE TABLE IF NOT EXISTS scenario_evaluation_runs (evaluation_run_id TEXT PRIMARY KEY, scenario_id TEXT NOT NULL, revision INTEGER NOT NULL, site_id TEXT NOT NULL REFERENCES sandbox_sites(site_id), source_snapshot_id TEXT NOT NULL REFERENCES site_snapshots(snapshot_id), evaluated_snapshot_id TEXT NOT NULL REFERENCES site_snapshots(snapshot_id), status TEXT NOT NULL, affected_constraint_ids_json TEXT NOT NULL, snapshot_diff_json TEXT NOT NULL, evaluation_json TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS diligence_projects (project_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id), status TEXT NOT NULL, state_json TEXT NOT NULL, state_hash TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS project_changes (change_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES diligence_projects(project_id), site_id TEXT NOT NULL, snapshot_before TEXT, snapshot_after TEXT, change_type TEXT NOT NULL, significance TEXT NOT NULL, source TEXT, detected_at DOUBLE PRECISION NOT NULL, state_json TEXT NOT NULL);

CREATE INDEX IF NOT EXISTS idx_obs_ws_key ON observations(workspace_id, local_key);
CREATE INDEX IF NOT EXISTS idx_obs_ws_created ON observations(workspace_id, created_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_workspace_created ON site_snapshots(workspace_id, created_at);
CREATE INDEX IF NOT EXISTS idx_site_snapshots_parcel_geometry ON site_snapshots USING GIST(parcel_geometry);
CREATE INDEX IF NOT EXISTS idx_scenarios_workspace_created ON scenario_versions(workspace_id, created_at);
CREATE INDEX IF NOT EXISTS idx_scenarios_snapshot_created ON scenario_versions(site_snapshot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_snapshot_sites_site_created ON site_snapshot_sites(site_id, created_at);
CREATE INDEX IF NOT EXISTS idx_reconciliations_site_created ON site_parcel_reconciliations(site_id, created_at);
CREATE INDEX IF NOT EXISTS idx_spend_plans_snapshot_created ON mireye_spend_plans(snapshot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_dependencies_site_evidence ON scenario_evidence_dependencies(site_id, evidence_id);
CREATE INDEX IF NOT EXISTS idx_evaluation_runs_site_created ON scenario_evaluation_runs(site_id, created_at);
CREATE INDEX IF NOT EXISTS idx_world_snapshots_site_created ON world_snapshots(site_snapshot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_diligence_projects_workspace_updated ON diligence_projects(workspace_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_project_changes_project_detected ON project_changes(project_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_project_changes_site_detected ON project_changes(site_id, detected_at DESC);

CREATE OR REPLACE FUNCTION mireye_snapshot_geometry() RETURNS trigger AS $$
BEGIN
  NEW.parcel_geometry := ST_SetSRID(ST_GeomFromGeoJSON(NEW.geometry_json), 4326);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_mireye_snapshot_geometry ON site_snapshots;
CREATE TRIGGER trg_mireye_snapshot_geometry BEFORE INSERT OR UPDATE OF geometry_json ON site_snapshots
FOR EACH ROW EXECUTE FUNCTION mireye_snapshot_geometry();
"""


def upgrade() -> None:
    op.execute(_SCHEMA)


def downgrade() -> None:
    # Data is immutable and production rollback restores a database backup rather than dropping it.
    pass
