from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class WorkflowHandle:
    workflow_id: str
    task: asyncio.Task[Any]


class WorkflowExecutor(Protocol):
    async def execute(self, workflow: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any: ...
    def submit(self, workflow: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> WorkflowHandle: ...


class LocalAsyncWorkflowExecutor:
    """Process-local executor for development and short-running workflows."""

    async def execute(self, workflow: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        return await workflow(*args, **kwargs)

    def submit(self, workflow: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> WorkflowHandle:
        async def invoke() -> Any:
            return await workflow(*args, **kwargs)

        return WorkflowHandle(workflow_id=f"workflow_{uuid.uuid4().hex}", task=asyncio.create_task(invoke()))
