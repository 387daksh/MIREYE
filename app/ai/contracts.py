from __future__ import annotations

from typing import Any, Protocol


class ModelProvider(Protocol):
    async def generate(self, request: dict[str, Any]) -> dict[str, Any]: ...


class EmbeddingProvider(Protocol):
    """Retrieval-only model surface; embeddings never establish project truth."""

    async def embed(self, texts: list[str]) -> dict[str, Any]: ...


class AgentRuntime(Protocol):
    async def run(self, state: dict[str, Any], message: str) -> dict[str, Any]: ...


class ToolRegistry(Protocol):
    def schemas(self) -> list[dict[str, Any]]: ...
    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class Planner(Protocol):
    async def plan(self, state: dict[str, Any], objective: str) -> dict[str, Any]: ...


class Verifier(Protocol):
    def verify(self, state: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]: ...


class MemoryStore(Protocol):
    def get(self, scope_id: str) -> dict[str, Any] | None: ...
    def put(self, scope_id: str, value: dict[str, Any]) -> None: ...
