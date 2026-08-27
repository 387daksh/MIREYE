from __future__ import annotations

import asyncio

from app.infrastructure.config import get_settings
from app.infrastructure.events import DomainEvent
from app.infrastructure.jetstream import JetStreamConsumer, JetStreamPublisher
from app.infrastructure.outbox import PostgresProcessedEvents
from app.infrastructure.worker_health import start_health_server
from event_worker import _database_target, _service_target


async def main() -> None:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required")
    publisher = JetStreamPublisher(settings.nats_url, settings.nats_stream)
    processed = PostgresProcessedEvents(settings.database_url)
    consumer = JetStreamConsumer(publisher, processed, "mireye-project-events-v1")

    async def record_delivery(_event: DomainEvent) -> None:
        return None

    await consumer.run(
        "mireye.>",
        record_delivery,
        ready=lambda: start_health_server(
            8093,
            {"postgres": _database_target(settings.database_url), "nats": _service_target(settings.nats_url, 4222)},
        ),
    )


if __name__ == "__main__":
    asyncio.run(main())
