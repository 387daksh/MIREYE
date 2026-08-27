from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EvidenceRepository(Protocol):
    def get_site_snapshot(self, snapshot_id: str) -> dict | None: ...


@runtime_checkable
class ProjectRepository(Protocol):
    def save_diligence_project(self, project: dict) -> dict: ...
    def get_diligence_project(self, project_id: str) -> dict | None: ...


@runtime_checkable
class SnapshotRepository(Protocol):
    def create_site_snapshot(self, snapshot: dict) -> dict: ...
    def get_site_snapshot(self, snapshot_id: str) -> dict | None: ...


@runtime_checkable
class ArtifactStore(Protocol):
    def put(self, data: bytes, *, extension: str, media_type: str, role: str) -> dict: ...
    def path(self, artifact: dict) -> Path: ...


@runtime_checkable
class SourceAdapter(Protocol):
    async def collect(self, request: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class EventPublisher(Protocol):
    def publish(self, event: Any) -> None: ...
