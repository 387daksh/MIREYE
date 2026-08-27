from alembic import op

revision = "20260825_0004"
down_revision = "20260825_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS processed_events (
      consumer_name TEXT NOT NULL,
      event_id TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('processing', 'completed')),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      completed_at TIMESTAMPTZ,
      PRIMARY KEY (consumer_name, event_id)
    );""")


def downgrade() -> None:
    pass
