import asyncio

import pytest

from app.infrastructure.events import DomainEvent, EventType
from app.infrastructure.jetstream import JetStreamConsumer


class FakeProcessed:
    def __init__(self):
        self.status = {}

    def claim(self, consumer, event_id):
        value = self.status.get((consumer, event_id))
        if value == "completed":
            return "completed"
        if value == "processing":
            return "busy"
        self.status[(consumer, event_id)] = "processing"
        return "claimed"

    def complete(self, consumer, event_id):
        self.status[(consumer, event_id)] = "completed"

    def release(self, consumer, event_id):
        self.status.pop((consumer, event_id), None)


class FakeMessage:
    def __init__(self, event):
        self.data = event.model_dump_json().encode()
        self.acks = 0
        self.naks = []

    async def ack(self):
        self.acks += 1

    async def nak(self, delay):
        self.naks.append(delay)


def event():
    return DomainEvent(event_type=EventType.PROJECT_CREATED, aggregate_type="Project", aggregate_id="project_1", workspace_id="workspace_1")


def test_consumer_acknowledges_and_suppresses_duplicate_delivery():
    async def check():
        store = FakeProcessed()
        consumer = JetStreamConsumer(object(), store, "durable")
        calls = 0

        async def handle(_event):
            nonlocal calls
            calls += 1

        first, duplicate = FakeMessage(event()), FakeMessage(event())
        duplicate.data = first.data
        await consumer.handle(first, handle)
        await consumer.handle(duplicate, handle)
        assert calls == 1 and first.acks == 1 and duplicate.acks == 1

    asyncio.run(check())


def test_consumer_releases_failed_delivery_for_replay():
    async def check():
        store = FakeProcessed()
        consumer = JetStreamConsumer(object(), store, "durable")
        message = FakeMessage(event())

        async def fail(_event):
            raise RuntimeError("retry")

        with pytest.raises(RuntimeError, match="retry"):
            await consumer.handle(message, fail)
        assert message.acks == 0 and message.naks == [5] and store.status == {}

    asyncio.run(check())


def test_consumer_reports_ready_only_after_durable_subscription_exists():
    class FakeJetStream:
        async def subscribe(self, *_args, **_kwargs):
            return object()

    class FakeConnection:
        def jetstream(self):
            return FakeJetStream()

    class FakePublisher:
        nc = FakeConnection()

        async def connect(self):
            return None

    async def check():
        ready = asyncio.Event()
        consumer = JetStreamConsumer(FakePublisher(), FakeProcessed(), "durable")
        task = asyncio.create_task(consumer.run("mireye.>", lambda _event: None, ready=ready.set))
        await asyncio.wait_for(ready.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(check())
