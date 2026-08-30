"""Add the queryable, temporal evidence graph and retrieval-memory records.

The tables reference immutable snapshots and existing project state; they do
not copy raw MIREYE payloads or replace PostgreSQL as the authority.
"""

from alembic import op


revision = "20260828_0005"
down_revision = "20260825_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS evidence_records (
      project_id TEXT NOT NULL REFERENCES diligence_projects(project_id),
      evidence_id TEXT NOT NULL,
      workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
      site_id TEXT,
      snapshot_id TEXT REFERENCES site_snapshots(snapshot_id),
      source_type TEXT,
      authority_level TEXT,
      spatial_scope TEXT,
      observed_at DOUBLE PRECISION,
      expires_at DOUBLE PRECISION,
      content_hash TEXT,
      metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
      created_at DOUBLE PRECISION NOT NULL,
      PRIMARY KEY (project_id, evidence_id)
    );
    CREATE TABLE IF NOT EXISTS project_requirements (
      project_id TEXT NOT NULL REFERENCES diligence_projects(project_id),
      requirement_id TEXT NOT NULL,
      workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
      site_id TEXT,
      constraint_json JSONB NOT NULL,
      created_at DOUBLE PRECISION NOT NULL,
      updated_at DOUBLE PRECISION NOT NULL,
      PRIMARY KEY (project_id, requirement_id)
    );
    CREATE TABLE IF NOT EXISTS claim_records (
      claim_id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES diligence_projects(project_id),
      workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
      site_id TEXT,
      claim_text TEXT NOT NULL,
      normalized_subject TEXT NOT NULL,
      predicate TEXT NOT NULL,
      normalized_object TEXT,
      status TEXT NOT NULL CHECK (status IN ('ACTIVE','SUPERSEDED','CONTESTED','UNSUPPORTED','EXPIRED')),
      semantic_strength TEXT NOT NULL CHECK (semantic_strength IN ('OBSERVATION','SIGNAL','DERIVED','INTERPRETATION','DECISION')),
      provenance_json JSONB NOT NULL DEFAULT '{}'::jsonb,
      valid_from DOUBLE PRECISION,
      valid_until DOUBLE PRECISION,
      superseded_by TEXT REFERENCES claim_records(claim_id),
      verification_status TEXT NOT NULL,
      created_at DOUBLE PRECISION NOT NULL,
      updated_at DOUBLE PRECISION NOT NULL
    );
    CREATE TABLE IF NOT EXISTS evidence_graph_relationships (
      relationship_id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
      project_id TEXT NOT NULL REFERENCES diligence_projects(project_id),
      source_type TEXT NOT NULL,
      source_id TEXT NOT NULL,
      relationship_type TEXT NOT NULL CHECK (relationship_type IN ('SUPPORTS','CONTRADICTS','DEPENDS_ON','AFFECTS','INVALIDATES','DERIVED_FROM','RESOLVES','BLOCKS','SUPERSEDES','LOCATED_AT','APPLIES_TO','OCCURRED_DURING')),
      target_type TEXT NOT NULL,
      target_id TEXT NOT NULL,
      context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
      occurred_at DOUBLE PRECISION NOT NULL
    );
    CREATE TABLE IF NOT EXISTS project_memory_records (
      memory_id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
      project_id TEXT NOT NULL REFERENCES diligence_projects(project_id),
      site_id TEXT,
      kind TEXT NOT NULL CHECK (kind IN ('WORKING','EPISODIC','SEMANTIC','EVIDENCE')),
      content_json JSONB NOT NULL,
      provenance_json JSONB NOT NULL,
      valid_from DOUBLE PRECISION,
      valid_until DOUBLE PRECISION,
      created_at DOUBLE PRECISION NOT NULL
    );
    CREATE TABLE IF NOT EXISTS decision_records (
      decision_id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
      project_id TEXT NOT NULL REFERENCES diligence_projects(project_id),
      site_id TEXT,
      decision_type TEXT,
      status TEXT,
      payload_json JSONB NOT NULL,
      decided_at DOUBLE PRECISION,
      created_at DOUBLE PRECISION NOT NULL
    );
    CREATE TABLE IF NOT EXISTS action_records (
      action_id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
      project_id TEXT NOT NULL REFERENCES diligence_projects(project_id),
      site_id TEXT,
      gap_id TEXT,
      action_type TEXT,
      status TEXT,
      payload_json JSONB NOT NULL,
      created_at DOUBLE PRECISION NOT NULL
    );
    CREATE TABLE IF NOT EXISTS project_episodes (
      episode_id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
      project_id TEXT NOT NULL REFERENCES diligence_projects(project_id),
      site_id TEXT,
      event_type TEXT NOT NULL,
      summary TEXT NOT NULL,
      evidence_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
      decision_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
      action_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
      snapshot_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
      occurred_at DOUBLE PRECISION NOT NULL,
      created_at DOUBLE PRECISION NOT NULL
    );
    CREATE TABLE IF NOT EXISTS document_chunks (
      chunk_id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
      project_id TEXT REFERENCES diligence_projects(project_id),
      document_id TEXT NOT NULL,
      ordinal INTEGER NOT NULL,
      content TEXT NOT NULL,
      source_metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
      embedding vector(1536),
      embedding_model TEXT,
      embedding_dimensions INTEGER NOT NULL DEFAULT 1536 CHECK (embedding_dimensions = 1536),
      created_at DOUBLE PRECISION NOT NULL,
      UNIQUE (document_id, ordinal)
    );
    CREATE TABLE IF NOT EXISTS documents (
      document_id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
      project_id TEXT REFERENCES diligence_projects(project_id),
      source_url TEXT,
      source_type TEXT,
      content_hash TEXT,
      metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
      created_at DOUBLE PRECISION NOT NULL
    );
    ALTER TABLE document_chunks ADD CONSTRAINT fk_document_chunks_document
      FOREIGN KEY (document_id) REFERENCES documents(document_id);
    CREATE INDEX IF NOT EXISTS idx_evidence_records_project_site ON evidence_records(project_id, site_id, observed_at DESC);
    CREATE INDEX IF NOT EXISTS idx_claim_records_project_status ON claim_records(project_id, status, valid_from DESC);
    CREATE INDEX IF NOT EXISTS idx_claim_records_site ON claim_records(site_id, valid_from DESC);
    CREATE INDEX IF NOT EXISTS idx_graph_relationships_project_source ON evidence_graph_relationships(project_id, source_type, source_id);
    CREATE INDEX IF NOT EXISTS idx_graph_relationships_project_target ON evidence_graph_relationships(project_id, target_type, target_id);
    CREATE INDEX IF NOT EXISTS idx_memory_records_project_kind ON project_memory_records(project_id, kind, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_requirements_project_site ON project_requirements(project_id, site_id);
    CREATE INDEX IF NOT EXISTS idx_decisions_project_site_time ON decision_records(project_id, site_id, decided_at DESC);
    CREATE INDEX IF NOT EXISTS idx_actions_project_gap ON action_records(project_id, gap_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_project_episodes_project_time ON project_episodes(project_id, occurred_at DESC);
    CREATE INDEX IF NOT EXISTS idx_document_chunks_project_document ON document_chunks(project_id, document_id, ordinal);
    CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding_hnsw ON document_chunks USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL;
    """)


def downgrade() -> None:
    # Recovery is a database restore; immutable provenance records are not dropped automatically.
    pass
