from __future__ import annotations
import asyncio
from temporalio.client import Client
from temporalio.worker import Worker
from app.application.orchestration.temporal import AIOrchestrationWorkflow, OrchestrationActivities
from app.infrastructure.config import get_settings
from app.infrastructure.worker_health import start_health_server
from app.main import orchestration_engine, workspace_store


async def main() -> None:
    settings = get_settings()
    if not settings.temporal_target:
        raise RuntimeError("TEMPORAL_TARGET is required")
    workspace_store.initialize()
    client = await Client.connect(settings.temporal_target, namespace=settings.temporal_namespace)
    activities = OrchestrationActivities(orchestration_engine)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[AIOrchestrationWorkflow],
        activities=[activities.begin, activities.advance, activities.resume, activities.fail],
    )
    host, port = _target(settings.temporal_target)
    start_health_server(8091, {"temporal": (host, port)})
    await worker.run()


def _target(value: str) -> tuple[str, int]:
    host, separator, port = value.rpartition(":")
    return (host, int(port)) if separator else (value, 7233)


if __name__ == "__main__":
    asyncio.run(main())
