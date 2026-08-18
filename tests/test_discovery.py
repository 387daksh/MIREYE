import pytest
from app.discovery.screen import DiscoveryEngine, FilterRule, generate_grid
from app.discovery.spatial import SpatialDiscovery
from app.mireye_client import MireyeClient


def test_filter_rule():
    rule = FilterRule(field="acreage", op=">=", value=100.0)
    assert rule.matches({"value": 150.0, "status": "ok"}) is True
    assert rule.matches({"value": 50.0, "status": "ok"}) is False
    assert rule.matches({"value": 150.0, "status": "failed"}) is False
    assert rule.matches(None) is False


def test_generate_grid():
    grid = generate_grid(min_lat=32.0, min_lng=-97.0, max_lat=32.2, max_lng=-96.8, h3_resolution=7)
    assert len(grid) > 0
    assert "lat" in grid[0]
    assert "lng" in grid[0]
    assert "h3_cell" in grid[0]


def test_spatial_discovery_duckdb():
    discovery = SpatialDiscovery()
    candidates = discovery.search_candidates(
        min_acreage=50.0,
        max_slope_pct=10.0,
        flood_zones=["X"],
        limit=10,
    )
    assert isinstance(candidates, list)
    if candidates:
        first = candidates[0]
        assert "parcel_id" in first
        assert "lat" in first
        assert "lng" in first
        assert first["fields"]["acreage"]["value"] >= 50.0
        assert first["fields"]["slope_pct"]["value"] <= 10.0
        assert first["fields"]["flood_zone"]["value"] == "X"


def test_discovery_engine_screen():
    import asyncio
    async def _test():
        client = MireyeClient(mode="local")
        engine = DiscoveryEngine(client=client)

        candidates = [
            {"lat": 32.5, "lng": -97.0},
            {"lat": 33.0, "lng": -96.0},
        ]
        filters = [
            FilterRule(field="acreage", op=">", value=0.0),
        ]

        res = await engine.screen(candidates=candidates, filters=filters)
        assert "shortlisted" in res
        assert "candidates_screened" in res
        assert res["candidates_screened"] == 2

    asyncio.run(_test())
