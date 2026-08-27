from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from nats.aio.client import Client as NATS
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from nats.js.errors import NotFoundError

from app.infrastructure.events import DomainEvent


logger = logging.getLogger(__name__)


class ProcessedEventStore(Protocol):
    def claim(self, consumer: str, event_id: str) -> str: ...
    def complete(self, consumer: str, event_id: str) -> None: ...
    def release(self, consumer: str, event_id: str) -> None: ...


class JetStreamPublisher:
    def __init__(self, url: str, stream: str):
        self.url, self.stream, self.nc = url, stream, NATS()

    async def connect(self) -> None:
        await self.nc.connect(self.url)
        js = self.nc.jetstream()
        try:
            await js.stream_info(self.stream)
        except NotFoundError:
            await js.add_stream(name=self.stream, subjects=["mireye.>"])

    async def close(self) -> None:
        await self.nc.drain()

    async def publish(self, event: DomainEvent) -> None:
        await self.nc.jetstream().publish(
            f"mireye.{event.event_type.value}",
            json.dumps(event.model_dump(mode="json"), default=str).encode(),
            headers={"Nats-Msg-Id": event.event_id},
        )

    async def replay(self, subject: str, handler: Callable[[DomainEvent], Awaitable[None]]) -> None:
        subscription = await self.nc.jetstream().subscribe(subject, durable="mireye-runtime")
        async for message in subscription.messages:
            event = DomainEvent.model_validate_json(message.data)
            await handler(event)
            await message.ack()


class JetStreamConsumer:
    """At-least-once consumer; event IDs make handlers replay-safe."""

    def __init__(self, publisher: JetStreamPublisher, processed: ProcessedEventStore, durable_name: str):
        self.publisher, self.processed, self.durable_name = publisher, processed, durable_name

    async def handle(self, message, handler: Callable[[DomainEvent], Awaitable[None]]) -> None:
        event = DomainEvent.model_validate_json(message.data)
        claim = await asyncio.to_thread(self.processed.claim, self.durable_name, event.event_id)
        if claim == "completed":
            await message.ack()
            return
        if claim == "busy":
            await message.nak(delay=1)
            return
        try:
            await handler(event)
            await asyncio.to_thread(self.processed.complete, self.durable_name, event.event_id)
            await message.ack()
        except Exception:
            await asyncio.to_thread(self.processed.release, self.durable_name, event.event_id)
            await message.nak(delay=5)
            raise

    async def run(
        self,
        subject: str,
        handler: Callable[[DomainEvent], Awaitable[None]],
        ready: Callable[[], None] | None = None,
    ) -> None:
        await self.publisher.connect()

        async def callback(message) -> None:
            try:
                await self.handle(message, handler)
            except Exception:
                logger.exception("JetStream event processing failed; delivery was negatively acknowledged")

        config = ConsumerConfig(
            durable_name=self.durable_name,
            ack_policy=AckPolicy.EXPLICIT,
            deliver_policy=DeliverPolicy.ALL,
            ack_wait=30,
            max_deliver=5,
            backoff=[1, 5, 30],
        )
        await self.publisher.nc.jetstream().subscribe(subject, config=config, manual_ack=True, cb=callback)
        if ready:
            ready()
        await asyncio.Event().wait()
