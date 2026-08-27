"""Mark the existing WorkspaceStore schema as the migration baseline.

Existing databases must be stamped to this revision after schema validation.
New databases continue to use explicit WorkspaceStore initialization until the
legacy DDL is converted into a subsequent declarative migration.
"""

revision = "20260825_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
