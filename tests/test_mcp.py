import pytest
from app.mcp.server import TOOLS_REGISTRY, execute_tool


def test_tools_registry():
    assert len(TOOLS_REGISTRY) == 7
    names = [t["name"] for t in TOOLS_REGISTRY]
    assert "screen_parcels" in names
    assert "get_grid_capacity" in names
    assert "verify_parcel" in names
    assert "workspace_observe" in names
    assert "workspace_state" in names
    assert "workspace_invalidate" in names
    assert "workspace_replay" in names


def test_mcp_execute_tools():
    import asyncio
    async def _test():
        # 1. Screen
        res = await execute_tool("screen_parcels", {"min_acreage": 10.0, "limit": 3})
        assert "shortlist" in res

        # 2. Grid
        res_grid = await execute_tool("get_grid_capacity", {"lat": 32.5, "lng": -97.0, "compress_tokens": True})
        assert "sub" in res_grid

        # 3. Verify
        res_verify = await execute_tool("verify_parcel", {"lat": 32.5, "lng": -97.0})
        assert "dossier" in res_verify

        # 4. Observe
        import time
        ws_id = f"ws_mcp_test_{int(time.time() * 1000)}"
        res_obs = await execute_tool(
            "workspace_observe",
            {
                "workspace_id": ws_id,
                "local_key": "PCL-MCP-01",
                "status": "shortlisted",
                "justification": "Approved via MCP Agent loop",
                "lat": 32.5,
                "lng": -97.0,
            },
        )
        assert res_obs["version"] == 1

        # 5. State
        res_state = await execute_tool("workspace_state", {"workspace_id": ws_id})
        assert len(res_state["shortlisted"]) == 1

    asyncio.run(_test())
