import pytest
from pathlib import Path

from app.mireye_client import MireyeClient
from app.workspace.engine import WorkspaceEngine
from app.workspace.store import WorkspaceStore


@pytest.fixture
def engine(tmp_path: Path):
    db_file = tmp_path / "test_engine_workspaces.db"
    store = WorkspaceStore(db_path=db_file)
    client = MireyeClient(mode="local")
    return WorkspaceEngine(store=store, client=client)


def test_workspace_engine_flow(engine: WorkspaceEngine):
    import asyncio
    async def _test():
        ws_id = "ws_agentic_001"
        engine.open(ws_id, label="Battery Storage Siting")

        # Observe candidate
        r1 = await engine.observe(
            workspace_id=ws_id,
            local_key="PCL-000010",
            status="candidate",
            justification="Large acreage near 345kV substation",
            lat=32.5,
            lng=-97.0,
        )
        assert r1["local_key"] == "PCL-000010"
        assert r1["version"] == 1

        # Observe rejected site
        r2 = await engine.observe(
            workspace_id=ws_id,
            local_key="PCL-000020",
            status="rejected",
            justification="High slope and flood zone AE",
            lat=33.0,
            lng=-96.5,
        )
        assert r2["status"] == "rejected"

        # Verify state partitioning
        state = engine.state(ws_id)
        assert len(state["candidates"]) == 1
        assert len(state["rejected"]) == 1
        assert state["rejection_ledger_size"] == 1

        # Invalidate check
        diffs = await engine.invalidate_check(ws_id)
        assert isinstance(diffs, list)

    asyncio.run(_test())
