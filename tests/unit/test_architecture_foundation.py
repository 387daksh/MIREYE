from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.application.workflows import LocalAsyncWorkflowExecutor
from app.domain.ports import ArtifactStore, SourceAdapter
from app.infrastructure.config.settings import Settings
from app.infrastructure.db import PostgresWorkspaceStore, workspace_store_for
from app.infrastructure.events import DomainEvent, EventType, InProcessEventDispatcher
from app.infrastructure.storage import LocalArtifactStore
from app.infrastructure.storage.artifacts import ArtifactIntegrityError


def test_import_has_no_database_or_artifact_side_effect(tmp_path: Path):
    db, assets = tmp_path / "never-created.db", tmp_path / "never-created-assets"
    env = {**os.environ, "WORKSPACE_DB": str(db), "WORLD_ASSET_DIR": str(assets), "MIREYE_API_KEY": "", "OPENAI_API_KEY": ""}
    subprocess.run([sys.executable, "-c", "import app.main"], check=True, env=env, cwd=Path(__file__).resolve().parents[2])
    assert not db.exists()
    assert not assets.exists()


def test_settings_are_typed_and_production_validates_required_configuration(tmp_path: Path):
    configured = Settings(app_env="test", workspace_db=tmp_path / "test.db", world_asset_dir=tmp_path / "assets")
    assert configured.effective_database_url.startswith("sqlite:///")
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings(app_env="production", mireye_api_key="configured")


def test_production_selects_postgres_and_rejects_sqlite(tmp_path: Path):
    configured = Settings(
        app_env="production",
        database_url="postgresql+psycopg://mireye:mireye@localhost/mireye",
        mireye_api_key="configured",
        openai_api_key="configured",
        artifact_store_backend="s3",
        s3_bucket="mireye-world",
        workflow_backend="temporal",
        temporal_target="localhost:7233",
        redis_url="redis://localhost:6379/0",
        nats_url="nats://localhost:4222",
        cors_origins=["https://app.example.com"],
    )
    assert isinstance(workspace_store_for(configured), PostgresWorkspaceStore)
    with pytest.raises(ValueError, match="SQLite"):
        Settings(
            app_env="production",
            database_url="sqlite:///tmp.db",
            mireye_api_key="configured",
            artifact_store_backend="s3",
            s3_bucket="mireye-world",
            workflow_backend="temporal",
            temporal_target="localhost:7233",
            redis_url="redis://localhost:6379/0",
            nats_url="nats://localhost:4222",
            cors_origins=["https://app.example.com"],
        )


def test_local_artifact_store_is_content_addressed_and_verifies_integrity(tmp_path: Path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    assert isinstance(store, ArtifactStore)
    artifact = store.put(b"real-source-artifact", extension="bin", media_type="application/octet-stream", role="fixture")
    assert artifact["sha256"] == hashlib.sha256(b"real-source-artifact").hexdigest()
    path = store.path(artifact)
    path.write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        store.path(artifact)


def test_source_adapter_contract_and_in_process_events():
    class Adapter:
        async def collect(self, request):
            return request

    assert isinstance(Adapter(), SourceAdapter)
    received = []
    dispatcher = InProcessEventDispatcher()
    dispatcher.subscribe(EventType.EVIDENCE_CHANGED, received.append)
    event = DomainEvent(
        event_type=EventType.EVIDENCE_CHANGED,
        aggregate_type="Site",
        aggregate_id="site_1",
        workspace_id="ws_1",
        payload={"field": "zoning"},
    )
    dispatcher.publish(event)
    assert received == [event]


def test_local_workflow_executor_runs_and_submits():
    async def workflow(value: int) -> int:
        return value + 1

    async def verify():
        executor = LocalAsyncWorkflowExecutor()
        assert await executor.execute(workflow, 1) == 2
        handle = executor.submit(workflow, 2)
        assert handle.workflow_id.startswith("workflow_")
        assert await handle.task == 3

    asyncio.run(verify())


def test_openapi_contract_is_generated_without_initializing_storage():
    from app.main import app

    schema = app.openapi()
    assert schema["openapi"].startswith("3.")
    assert "/v1/diligence/projects/{project_id}/changes" in schema["paths"]
