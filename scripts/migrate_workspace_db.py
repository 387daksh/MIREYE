"""Explicit, one-way SQLite-to-PostgreSQL migration for WorkspaceStore data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import psycopg
from psycopg.rows import dict_row


TABLES = (
    "workspaces", "site_mappings", "observations", "staleness_log", "site_snapshots", "sandbox_sites",
    "site_snapshot_sites", "site_parcel_reconciliations", "mireye_spend_plans", "world_snapshots",
    "scenario_versions", "scenario_evidence_dependencies", "scenario_evaluation_runs", "diligence_projects", "project_changes",
)


def psycopg_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def sqlite_path(url: str) -> Path:
    parsed = urlparse(url)
    if parsed.scheme != "sqlite":
        raise ValueError("SOURCE_DATABASE_URL must use sqlite:///.")
    path = Path(unquote(parsed.path.lstrip("/"))).resolve()
    legacy = (Path(__file__).resolve().parents[1] / "app" / "data" / "workspaces.db").resolve()
    if path == legacy:
        raise ValueError("Refusing to migrate app/data/workspaces.db automatically; copy it first and pass the copy explicitly.")
    if not path.is_file():
        raise ValueError("SQLite source database was not found.")
    return path


def source_counts(source: Path) -> dict[str, int]:
    with sqlite3.connect(source) as conn:
        existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in TABLES if table in existing}


def validate(source: Path, target_url: str, artifact_root: Path | None = None) -> dict[str, object]:
    counts = source_counts(source)
    missing = [table for table in TABLES if table not in counts]
    if missing:
        raise ValueError(f"SQLite source is missing expected tables: {', '.join(missing)}")
    with sqlite3.connect(source) as conn:
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        snapshots = conn.execute("SELECT snapshot_id, geometry_json, raw_response_json, raw_response_hash FROM site_snapshots").fetchall()
    if foreign_keys:
        raise ValueError(f"SQLite source has {len(foreign_keys)} foreign-key violations.")
    for snapshot_id, geometry, raw, raw_hash in snapshots:
        json.loads(geometry)
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != raw_hash:
            # Older snapshots hash canonical provider bytes. Preserve them but make the mismatch explicit.
            if not raw_hash:
                raise ValueError(f"Snapshot {snapshot_id} has no raw-response hash.")
    if artifact_root:
        with sqlite3.connect(source) as conn:
            rows = conn.execute("SELECT snapshot_json FROM world_snapshots").fetchall()
        for (snapshot_json,) in rows:
            for artifact in _artifacts(json.loads(snapshot_json)):
                path = artifact_root / artifact["storage_key"]
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
                    raise ValueError(f"Artifact integrity check failed: {artifact['storage_key']}")
    if not target_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ValueError("TARGET_DATABASE_URL must use PostgreSQL.")
    return {"source": str(source), "target": target_url, "row_counts": counts, "foreign_keys": "ok"}


def _artifacts(value):
    if isinstance(value, dict):
        if {"storage_key", "sha256"}.issubset(value):
            yield value
        for item in value.values():
            yield from _artifacts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _artifacts(item)


def migrate(source: Path, target_url: str) -> dict[str, int]:
    env = {**os.environ, "DATABASE_URL": target_url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env=env, cwd=Path(__file__).resolve().parents[1])
    with psycopg.connect(psycopg_url(target_url), row_factory=dict_row) as target, sqlite3.connect(source) as legacy:
        target_existing = {row["table_name"] for row in target.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
        if any(table not in target_existing for table in TABLES):
            raise RuntimeError("PostgreSQL migration did not create the WorkspaceStore tables.")
        occupied = {table: target.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"] for table in TABLES}
        if any(occupied.values()):
            raise ValueError("Refusing to merge into a non-empty target database; restore a clean target and retry.")
        for table in TABLES:
            columns = [row[1] for row in legacy.execute(f"PRAGMA table_info({table})")]
            if not columns:
                continue
            placeholders = ", ".join("%s" for _ in columns)
            statement = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
            rows = legacy.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
            if rows:
                target.cursor().executemany(statement, rows)
    return source_counts(source)


def verify(source: Path, target_url: str) -> dict[str, int]:
    expected = source_counts(source)
    with psycopg.connect(psycopg_url(target_url), row_factory=dict_row) as target:
        actual = {table: target.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"] for table in expected}
        geometry = target.execute("SELECT count(*) AS count FROM site_snapshots WHERE parcel_geometry IS NULL").fetchone()["count"]
    if expected != actual:
        raise ValueError(f"Row-count mismatch: expected {expected}, got {actual}")
    if geometry:
        raise ValueError(f"{geometry} SiteSnapshots are missing PostGIS geometry.")
    return actual


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "validate", "migrate", "verify"))
    parser.add_argument("--source", required=True, help="Explicit sqlite:/// migration-source URL")
    parser.add_argument("--target", help="Explicit PostgreSQL target URL")
    parser.add_argument("--artifact-root", type=Path)
    args = parser.parse_args()
    source = sqlite_path(args.source)
    if args.command == "inspect":
        print(json.dumps({"source": str(source), "row_counts": source_counts(source)}, indent=2, sort_keys=True))
        return
    if not args.target:
        parser.error("--target is required for validate, migrate, and verify")
    if args.command == "validate":
        print(json.dumps(validate(source, args.target, args.artifact_root), indent=2, sort_keys=True))
    elif args.command == "migrate":
        validate(source, args.target, args.artifact_root)
        print(json.dumps({"migrated": migrate(source, args.target), "verified": verify(source, args.target)}, indent=2, sort_keys=True))
    else:
        print(json.dumps({"verified": verify(source, args.target)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
