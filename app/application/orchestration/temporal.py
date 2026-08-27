"""Temporal coordination; Phase 15 planning and verification remain in application activities."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy

if TYPE_CHECKING:
    from app.ai.runtime import OrchestrationEngine


_ACTIVITY_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=2,
    non_retryable_error_types=["DiligenceError", "OrchestrationError", "ValueError"],
)


class OrchestrationActivities:
    def __init__(self, engine: "OrchestrationEngine"):
        self.engine = engine

    @activity.defn
    async def begin(self, project_id: str, message: str, run_id: str) -> dict[str, Any]:
        return await self.engine.begin(project_id, message, run_id)

    @activity.defn
    async def advance(self, project_id: str, run_id: str) -> dict[str, Any]:
        return await self.engine.advance(project_id, run_id)

    @activity.defn
    async def resume(self, project_id: str, run_id: str) -> dict[str, Any]:
        return await self.engine.resume_batch(project_id, run_id)

    @activity.defn
    async def fail(self, project_id: str, run_id: str) -> dict[str, Any]:
        return self.engine.fail(project_id, run_id)


@workflow.defn
class AIOrchestrationWorkflow:
    def __init__(self) -> None:
        self._decision_received = False

    @workflow.run
    async def run(self, project_id: str, message: str, run_id: str) -> dict[str, Any]:
        try:
            result = await workflow.execute_activity(
                "begin", args=[project_id, message, run_id], start_to_close_timeout=timedelta(minutes=5), retry_policy=_ACTIVITY_RETRY_POLICY,
            )
            while result["run"]["status"] in {"RUNNING", "WAITING_FOR_DECISION"}:
                if result["run"]["status"] == "WAITING_FOR_DECISION":
                    await workflow.wait_condition(lambda: self._decision_received)
                    self._decision_received = False
                    result = await workflow.execute_activity(
                        "resume", args=[project_id, run_id], start_to_close_timeout=timedelta(minutes=10), retry_policy=_ACTIVITY_RETRY_POLICY,
                    )
                else:
                    result = await workflow.execute_activity(
                        "advance", args=[project_id, run_id], start_to_close_timeout=timedelta(minutes=10), retry_policy=_ACTIVITY_RETRY_POLICY,
                    )
            return result
        except Exception:
            return await workflow.execute_activity(
                "fail", args=[project_id, run_id], start_to_close_timeout=timedelta(minutes=1), retry_policy=_ACTIVITY_RETRY_POLICY,
            )

    @workflow.signal
    def decision_answered(self) -> None:
        self._decision_received = True


class TemporalOrchestrationExecutor:
    def __init__(self, target: str, namespace: str, task_queue: str):
        self.target, self.namespace, self.task_queue = target, namespace, task_queue

    async def start(self, project_id: str, message: str, run_id: str) -> str:
        client = await Client.connect(self.target, namespace=self.namespace)
        await client.start_workflow(
            AIOrchestrationWorkflow.run,
            args=[project_id, message, run_id],
            id=run_id,
            task_queue=self.task_queue,
        )
        return run_id

    async def signal_decision(self, run_id: str) -> None:
        client = await Client.connect(self.target, namespace=self.namespace)
        await client.get_workflow_handle(run_id).signal(AIOrchestrationWorkflow.decision_answered)
