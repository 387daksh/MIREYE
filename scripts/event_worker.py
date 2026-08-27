"""Publish committed PostgreSQL outbox events to JetStream."""

from __future__ import annotations
import asyncio
from app.infrastructure.config import get_settings
from app.infrastructure.jetstream import JetStreamPublisher
from app.infrastructure.outbox import PostgresOutbox
from app.infrastructure.worker_health import start_health_server


async def main() -> None:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required")
    outbox = PostgresOutbox(settings.database_url)
    publisher = JetStreamPublisher(settings.nats_url, settings.nats_stream)
    await publisher.connect()
    start_health_server(8092, {"postgres": (_database_target(settings.database_url)), "nats": (_service_target(settings.nats_url, 4222))})
    try:
        while True:
            for event in outbox.claim():
                await publisher.publish(event)
                outbox.mark_published(event.event_id)
            await asyncio.sleep(1)
    finally:
        await publisher.close()


def _service_target(value: str, default_port: int) -> tuple[str, int]:
    from urllib.parse import urlparse

    parsed = urlparse(value)
    return parsed.hostname or "localhost", parsed.port or default_port


def _database_target(value: str) -> tuple[str, int]:
    return _service_target(value.replace("postgresql+psycopg://", "postgresql://", 1), 5432)


if __name__ == "__main__":
    asyncio.run(main())
