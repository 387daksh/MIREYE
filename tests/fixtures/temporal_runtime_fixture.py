from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


attempts: dict[str, int] = {}


@activity.defn
async def retry_once(key: str) -> str:
    attempts[key] = attempts.get(key, 0) + 1
    if attempts[key] == 1:
        raise RuntimeError("retryable fixture failure")
    return "ready"


@workflow.defn
class RecoveryFixtureWorkflow:
    def __init__(self) -> None:
        self.resumed = False

    @workflow.run
    async def run(self, key: str) -> str:
        await workflow.execute_activity(
            retry_once,
            key,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        await workflow.wait_condition(lambda: self.resumed)
        return "completed"

    @workflow.signal
    def resume(self) -> None:
        self.resumed = True
