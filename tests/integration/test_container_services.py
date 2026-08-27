from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid

import boto3
import psycopg
import pytest
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from redis.asyncio import Redis
from temporalio.client import Client
from temporalio.worker import Worker

from app.infrastructure.events import DomainEvent, EventType
from app.infrastructure.jetstream import JetStreamConsumer, JetStreamPublisher
from app.infrastructure.outbox import PostgresOutbox, PostgresProcessedEvents
from app.infrastructure.storage import S3ArtifactStore
from app.infrastructure.db.postgres import PostgresWorkspaceStore
from tests.fixtures.temporal_runtime_fixture import RecoveryFixtureWorkflow, attempts, retry_once


pytestmark = pytest.mark.runtime


def require_runtime() -> None:
    if os.getenv("MIREYE_RUNTIME_INTEGRATION") != "1":
        pytest.skip("requires the containerized runtime")


def database_url() -> str:
    return os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)


def test_postgres_migrations_repository_and_postgis() -> None:
    require_runtime()
    store = PostgresWorkspaceStore(database_url())
    store.initialize()
    workspace_id = f"runtime-{uuid.uuid4().hex}"
    store.create_workspace(workspace_id, "Runtime validation")
    with psycopg.connect(database_url()) as connection:
        row = connection.execute("SELECT postgis_version(), extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
        geometry = connection.execute("SELECT ST_AsText(ST_GeomFromText('POINT(-97.7431 30.2672)', 4326))").fetchone()
    assert row and row[0] and row[1]
    assert geometry == ("POINT(-97.7431 30.2672)",)
    with psycopg.connect(database_url()) as connection:
        assert connection.execute("SELECT 1 FROM workspaces WHERE workspace_id = %s", (workspace_id,)).fetchone()


def test_redis_ttl_hit_and_invalidation() -> None:
    require_runtime()

    async def check() -> None:
        client = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
        key = f"runtime:{uuid.uuid4().hex}"
        await client.set(key, "cached", ex=30)
        assert await client.get(key) == "cached"
        assert 0 < await client.ttl(key) <= 30
        await client.delete(key)
        assert await client.get(key) is None
        await client.aclose()

    asyncio.run(check())


def test_minio_content_addressed_roundtrip(tmp_path) -> None:
    require_runtime()
    client = boto3.client(
        "s3",
        endpoint_url=os.getenv("S3_ENDPOINT_URL", "http://localhost:9000"),
        region_name="us-east-1",
        aws_access_key_id=os.getenv("S3_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY", "minioadmin"),
    )
    store = S3ArtifactStore(os.getenv("S3_BUCKET", "mireye-world"), client, tmp_path)
    data = b"runtime-artifact"
    artifact = store.put(data, extension="bin", media_type="application/octet-stream", role="runtime-check")
    assert artifact["sha256"] == hashlib.sha256(data).hexdigest()
    assert store.path(artifact).read_bytes() == data


def test_outbox_jetstream_duplicate_and_consumer_restart() -> None:
    require_runtime()

    async def check() -> None:
        workspace_id = f"runtime-{uuid.uuid4().hex}"
        event = DomainEvent(
            event_type=EventType.PROJECT_CREATED,
            aggregate_type="Project",
            aggregate_id=f"project-{uuid.uuid4().hex}",
            workspace_id=workspace_id,
        )
        publisher = JetStreamPublisher(os.getenv("NATS_URL", "nats://localhost:4222"), "MIREYE")
        await publisher.connect()
        durable = f"runtime-{uuid.uuid4().hex}"
        subscription = await publisher.nc.jetstream().pull_subscribe(
            f"mireye.{event.event_type.value}",
            durable=durable,
            stream="MIREYE",
            config=ConsumerConfig(durable_name=durable, deliver_policy=DeliverPolicy.NEW, ack_policy=AckPolicy.EXPLICIT),
        )
        with psycopg.connect(database_url()) as connection:
            connection.execute(
                "INSERT INTO workspaces (workspace_id, label, created_at) VALUES (%s, %s, %s)",
                (workspace_id, "Runtime events", time.time()),
            )
            PostgresOutbox(database_url()).append(connection, event)
        published = None
        for _ in range(100):
            with psycopg.connect(database_url()) as connection:
                published = connection.execute("SELECT published_at FROM outbox_events WHERE event_id = %s", (event.event_id,)).fetchone()
            if published and published[0]:
                break
            await asyncio.sleep(0.1)
        assert published and published[0]

        consumer = JetStreamConsumer(publisher, PostgresProcessedEvents(database_url()), durable)
        handled: list[str] = []
        message = (await subscription.fetch(1, timeout=10))[0]
        await consumer.handle(message, lambda item: _record(handled, item.event_id))

        await publisher.nc.jetstream().publish(
            f"mireye.{event.event_type.value}",
            event.model_dump_json().encode(),
            headers={"Nats-Msg-Id": f"duplicate-{uuid.uuid4().hex}"},
        )
        duplicate = (await subscription.fetch(1, timeout=10))[0]
        await consumer.handle(duplicate, lambda item: _record(handled, item.event_id))
        assert handled == [event.event_id]
        await publisher.close()

        restarted = JetStreamPublisher(os.getenv("NATS_URL", "nats://localhost:4222"), "MIREYE")
        await restarted.connect()
        resumed = await restarted.nc.jetstream().pull_subscribe(f"mireye.{event.event_type.value}", durable=durable, stream="MIREYE")
        await restarted.nc.jetstream().publish(
            f"mireye.{event.event_type.value}",
            DomainEvent(
                event_type=EventType.PROJECT_CREATED,
                aggregate_type="Project",
                aggregate_id=f"project-{uuid.uuid4().hex}",
                workspace_id=workspace_id,
            )
            .model_dump_json()
            .encode(),
        )
        assert await resumed.fetch(1, timeout=10)
        await restarted.close()

    asyncio.run(check())


async def _record(target: list[str], event_id: str) -> None:
    target.append(event_id)


def test_temporal_retry_signal_and_worker_recovery() -> None:
    require_runtime()

    async def check() -> None:
        client = await Client.connect(os.getenv("TEMPORAL_TARGET", "localhost:7233"), namespace="default")
        key = uuid.uuid4().hex
        task_queue = f"runtime-{key}"
        worker = Worker(client, task_queue=task_queue, workflows=[RecoveryFixtureWorkflow], activities=[retry_once])
        worker_task = asyncio.create_task(worker.run())
        handle = await client.start_workflow(
            RecoveryFixtureWorkflow.run,
            key,
            id=f"runtime-{key}",
            task_queue=task_queue,
        )
        for _ in range(100):
            if attempts.get(key) == 2:
                break
            await asyncio.sleep(0.1)
        assert attempts.get(key) == 2
        await worker.shutdown()
        await worker_task

        recovered = Worker(client, task_queue=task_queue, workflows=[RecoveryFixtureWorkflow], activities=[retry_once])
        recovered_task = asyncio.create_task(recovered.run())
        await handle.signal(RecoveryFixtureWorkflow.resume)
        await handle.signal(RecoveryFixtureWorkflow.resume)
        assert await handle.result() == "completed"
        await recovered.shutdown()
        await recovered_task

    asyncio.run(check())
