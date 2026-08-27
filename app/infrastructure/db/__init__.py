"""Database adapter and migration boundary."""

from __future__ import annotations

from app.infrastructure.config.settings import Settings
from app.infrastructure.db.postgres import PostgresWorkspaceStore
from app.workspace.store import WorkspaceStore


def workspace_store_for(settings: Settings) -> WorkspaceStore:
    """Select the one authoritative repository for this environment."""
    url = settings.database_url
    if url and not url.startswith("sqlite"):
        return PostgresWorkspaceStore(url)
    if settings.app_env == "production":
        raise ValueError("Production requires a PostgreSQL DATABASE_URL.")
    # Preserve the explicit FastAPI-startup initialization contract for the
    # disposable SQLite backend.
    return WorkspaceStore()


__all__ = ["PostgresWorkspaceStore", "workspace_store_for"]
