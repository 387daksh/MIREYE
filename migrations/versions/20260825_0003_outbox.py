from alembic import op

revision = "20260825_0003"
down_revision = "20260825_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS outbox_events (
      event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
      workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id), project_id TEXT, occurred_at TIMESTAMPTZ NOT NULL,
      correlation_id TEXT, causation_id TEXT, payload TEXT NOT NULL, schema_version INTEGER NOT NULL,
      published_at TIMESTAMPTZ, attempts INTEGER NOT NULL DEFAULT 0);
      CREATE INDEX IF NOT EXISTS idx_outbox_unpublished ON outbox_events(occurred_at) WHERE published_at IS NULL;""")


def downgrade() -> None:
    pass
