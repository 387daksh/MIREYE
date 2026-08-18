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


def test_spatial_discovery_representative_filters_still_work():
    discovery = SpatialDiscovery()
    candidates = discovery.search_candidates(
        min_lat=31.5,
        max_lat=34.5,
        min_lng=-98.5,
        max_lng=-95.0,
        min_acreage=10.0,
        max_acreage=850.0,
        max_slope_pct=22.0,
        flood_zones=["X", "AE", "A", "VE"],
        max_flood_risk_score=1.0,
        min_substation_capacity_mw=5.0,
        max_distance_to_substation_km=40.0,
        max_queue_depth=60,
        zoning_renewable_only=True,
        owner_types=["individual", "trust", "LLC", "municipal", "unknown"],
        limit=5,
    )

    assert 0 < len(candidates) <= 5
    for candidate in candidates:
        fields = candidate["fields"]
        assert 31.5 <= candidate["lat"] <= 34.5
        assert -98.5 <= candidate["lng"] <= -95.0
        assert fields["acreage"]["value"] >= 10.0
        assert fields["acreage"]["value"] <= 850.0
        assert fields["slope_pct"]["value"] <= 22.0
        assert fields["flood_zone"]["value"] in {"X", "AE", "A", "VE"}
        assert fields["flood_risk_score"]["value"] <= 1.0
        assert fields["substation_capacity_mw"]["value"] >= 5.0
        assert fields["distance_to_substation_km"]["value"] <= 40.0
        assert fields["interconnection_queue_depth"]["value"] <= 60
        assert fields["zoning_renewable_permitted"]["value"] is True
        assert fields["owner_type"]["value"] in {"individual", "trust", "LLC", "municipal", "unknown"}


def test_spatial_discovery_quotes_in_filters_are_bound_values():
    discovery = SpatialDiscovery()

    candidates = discovery.search_candidates(
        flood_zones=["X'); SELECT * FROM missing_table; --"],
        owner_types=["LLC'); SELECT * FROM missing_table; --"],
        limit=5,
    )

    assert candidates == []
    assert discovery.search_candidates(flood_zones=["X"], limit=1)


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
