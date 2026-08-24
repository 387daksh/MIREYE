import asyncio
import copy
import io
import json
import math

import numpy as np
import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from shapely import wkb
from shapely.geometry import LineString, Polygon

from app.sandbox import EARTH_RADIUS_M, scene_state_from_snapshot
from app.sandbox_scenarios import ScenarioError, ScenarioService
from app.workspace.store import WorkspaceStore
from app.world import (
    ArtifactStore,
    TERRAIN_ENCODING_VERSION,
    USGSTerrainProvider,
    WorldError,
    WorldSnapshotService,
    _canonical,
    _npy_bytes,
    _tile_xy,
    as_geoparquet,
    encode_terrain_rgb,
    _polygon_geojson,
    road_geojson,
    usgs_vertical_reference,
)


def run(coro):
    return asyncio.run(coro)


def site_snapshot(snapshot_id="site-world-test"):
    half = math.degrees(500 / EARTH_RADIUS_M)
    return {
        "snapshot_id": snapshot_id, "workspace_id": "ws-world", "is_expired": False,
        "parcel_identity": {
            "parcel_id": "parcel-world-1", "parcel_data_source": "MIREYE_TEST",
            "parcel_match_type": "exact_intersect", "parcel_match_distance_m": 0.0,
            "selected_point": {"lat": 0.0, "lng": 0.0},
        },
        "geometry": {"type": "Polygon", "coordinates": [[[-half, -half], [half, -half], [half, half], [-half, half], [-half, -half]]]},
        "evidence": {}, "raw_response": {}, "raw_response_hash": "raw", "request": {}, "request_hash": "request",
        "field_catalog_version": "test", "provider_metadata": {"provider": "MIREYE_TEST"},
        "observed_at": 1.0, "expires_at": 9_999_999_999.0, "created_at": 1.0,
    }


class FixtureTerrainProvider:
    async def build(self, aoi, artifacts, options):
        elevation = float(options.get("fixture_elevation_m", 42.0))
        grid = np.full((4, 4), elevation, dtype=np.float32)
        source = artifacts.put(b"fixture-usgs-dem-v1", extension="tif", media_type="image/tiff", role="source_dem")
        metadata = artifacts.put(_canonical({"source_id": "fixture-3dep-1m", "vertical_datum": "NAVD88"}), extension="json", media_type="application/json", role="source_metadata")
        elevation_grid = artifacts.put(_npy_bytes(grid), extension="npy", media_type="application/x-npy", role="elevation_grid")
        center_lng = (aoi["bbox"][0] + aoi["bbox"][2]) / 2
        center_lat = (aoi["bbox"][1] + aoi["bbox"][3]) / 2
        x, y = _tile_xy(center_lng, center_lat, 14)
        tile = artifacts.put(encode_terrain_rgb(np.full((256, 256), elevation)), extension="png", media_type="image/png", role="terrain_rgb_tile")
        tiles = {f"14/{x}/{y}": tile}
        manifest = artifacts.put(_canonical({"encoding": TERRAIN_ENCODING_VERSION, "zoom": 14, "tiles": tiles}), extension="json", media_type="application/json", role="terrain_tile_manifest")
        return {
            "layer": "terrain", "availability": "AVAILABLE", "quality_state": "VERIFIED_SOURCE",
            "source": {"provider": "USGS", "dataset": "3DEP fixture", "source_id": "fixture-3dep-1m", "license": "USGS public domain", "nominal_resolution_m": 1.0},
            "artifacts": {"source_dem": source, "source_metadata": metadata, "elevation_grid": elevation_grid, "tile_manifest": manifest},
            "terrain": {
                "actual_resolution_m": 1.0, "source_crs": "EPSG:26918", "vertical_reference": "NAVD88",
                "vertical_units": "meters", "grid_bbox": aoi["bbox"], "grid_width": 4, "grid_height": 4,
                "grid_orientation": "north_up", "min_tile_zoom": 14, "tile_zoom": 14, "tile_size": 256,
                "tile_pyramid_version": "aoi_tile_cover_v2",
                "encoding": TERRAIN_ENCODING_VERSION, "tiles": tiles,
            },
            "warnings": [], "conflicts": [],
        }


class FixtureRoadProvider:
    async def build(self, aoi, artifacts, options):
        bbox = aoi["bbox"]
        lat = (bbox[1] + bbox[3]) / 2
        table = pa.table({
            "id": ["gers-road-1"], "name": ["Observed Road"], "class": ["primary"], "subclass": [None],
            "geometry": [wkb.dumps(LineString([(bbox[0] - 0.01, lat), (bbox[2] + 0.01, lat)]))],
        })
        parquet = io.BytesIO()
        pq.write_table(table, parquet, compression="zstd")
        source = artifacts.put(parquet.getvalue(), extension="parquet", media_type="application/vnd.apache.parquet", role="overture_aoi_geoparquet")
        rendered = road_geojson(table, bbox)
        render = artifacts.put(_canonical(rendered), extension="geojson", media_type="application/geo+json", role="roads_render_geojson")
        return {
            "layer": "roads", "availability": "AVAILABLE", "quality_state": "VERIFIED_SOURCE",
            "source": {"provider": "Overture Maps Foundation", "release": "2026-08-19.0", "license": "ODbL-1.0", "attribution": "OpenStreetMap contributors, Overture Maps Foundation"},
            "artifacts": {"source_geoparquet": source, "render_geojson": render},
            "roads": {"feature_count": 1, "identity_field": "GERS id"},
            "warnings": ["Mapped road geometry does not prove legal access, frontage, easement, or heavy-haul suitability."], "conflicts": [],
        }


class FixturePolygonProvider:
    def __init__(self, layer):
        self.layer = layer

    async def build(self, aoi, artifacts, options):
        bbox = aoi["bbox"]
        geometry = Polygon([
            (bbox[0], bbox[1]), (bbox[0] + 0.001, bbox[1]),
            (bbox[0] + 0.001, bbox[1] + 0.001), (bbox[0], bbox[1] + 0.001),
        ])
        properties = {"id": [f"gers-{self.layer}-1"], "subtype": [self.layer], "geometry": [wkb.dumps(geometry)]}
        if self.layer == "buildings":
            properties.update({"name": ["Observed building"], "class": ["industrial"], "height_m": [12.0], "num_floors": [1]})
            render_properties = ["name", "class", "subtype", "height_m", "num_floors"]
        else:
            render_properties = ["subtype"]
        table = pa.table(properties)
        parquet = io.BytesIO()
        pq.write_table(table, parquet, compression="zstd")
        source = artifacts.put(parquet.getvalue(), extension="parquet", media_type="application/vnd.apache.parquet", role=f"{self.layer}_source")
        rendered = _polygon_geojson(table, bbox, render_properties)
        render = artifacts.put(_canonical(rendered), extension="geojson", media_type="application/geo+json", role=f"{self.layer}_render")
        return {
            "layer": self.layer, "availability": "AVAILABLE", "quality_state": "VERIFIED_SOURCE",
            "source": {"provider": "Overture Maps Foundation", "release": "2026-08-19.0", "license": "ODbL-1.0"},
            "artifacts": {"source_geoparquet": source, "render_geojson": render},
            self.layer: {"feature_count": 1, "identity_field": "GERS id"},
            "warnings": [], "conflicts": [],
        }


def world_service(tmp_path, snapshot_id="site-world-test"):
    store = WorkspaceStore(db_path=tmp_path / "world.db")
    site = site_snapshot(snapshot_id)
    store.create_site_snapshot(site)
    service = WorldSnapshotService(
        store, ArtifactStore(tmp_path / "assets"),
        terrain_provider=FixtureTerrainProvider(), road_provider=FixtureRoadProvider(),
    )
    return store, site, service


def test_world_snapshot_is_immutable_deterministic_and_content_addressed(tmp_path):
    store, site, service = world_service(tmp_path)
    first = run(service.create(site_snapshot_id=site["snapshot_id"], requested_layers=["terrain", "roads"]))
    second = run(service.create(site_snapshot_id=site["snapshot_id"], requested_layers=["terrain", "roads"]))

    assert first == second
    assert first["world_snapshot_id"].startswith("world_")
    assert first["query_aoi"]["buffer_m"] == 1000.0
    assert first["vertical_reference"] == "NAVD88"
    assert first["source_manifest"][0]["artifact_hashes"]
    assert first["layers"][0]["terrain"]["actual_resolution_m"] == 1.0
    assert first["layers"][1]["source"]["release"] == "2026-08-19.0"

    first["layers"][0]["quality_state"] = "MUTATED"
    assert store.get_world_snapshot(second["world_snapshot_id"])["layers"][0]["quality_state"] == "VERIFIED_SOURCE"


def test_world_snapshot_adds_real_context_layers_with_provenance(tmp_path):
    store, site, service = world_service(tmp_path)
    service.building_provider = FixturePolygonProvider("buildings")
    service.water_provider = FixturePolygonProvider("water")
    service.land_cover_provider = FixturePolygonProvider("land_cover")

    world = run(service.create(
        site_snapshot_id=site["snapshot_id"],
        requested_layers=["terrain", "roads", "buildings", "water", "land_cover", "transmission"],
    ))
    public = service.public(world)
    layers = {item["layer"]: item for item in public["layers"]}

    assert {"terrain", "roads", "buildings", "water", "land_cover", "transmission"} == set(layers)
    assert layers["buildings"]["buildings"]["feature_count"] == 1
    assert layers["water"]["render"]["type"] == "geojson"
    assert layers["land_cover"]["render"]["url"].endswith("/layers/land_cover")
    assert layers["transmission"]["availability"] == "UNAVAILABLE"
    assert all(entry["artifact_hashes"] for entry in world["source_manifest"] if entry["layer"] != "transmission")
    assert all(entry["acquired_at"] == public["created_at"] for entry in public["source_manifest"])


def test_dem_encoding_and_road_extraction_are_deterministic():
    elevations = np.array([[0.0, 1.25], [100.0, -5.0]], dtype=np.float32)
    assert encode_terrain_rgb(elevations) == encode_terrain_rgb(elevations.copy())

    table = pa.table({
        "id": ["gers-1", "gers-2"], "name": ["Crossing", "Outside"], "class": ["primary", "service"],
        "subclass": [None, None],
        "geometry": [wkb.dumps(LineString([(-2, 0), (2, 0)])), wkb.dumps(LineString([(3, 3), (4, 4)]))],
    })
    extracted = road_geojson(table, [-1, -1, 1, 1])
    assert [feature["properties"]["gers_id"] for feature in extracted["features"]] == ["gers-1"]
    assert extracted == road_geojson(table, [-1, -1, 1, 1])
    assert json.loads(as_geoparquet(table, [-1, -1, 1, 1]).schema.metadata[b"geo"])["primary_column"] == "geometry"
    assert usgs_vertical_reference({"body": "referenced to the North American Vertical Datum of 1988 (NAVD 88)"}) == "NAVD88"
    assert usgs_vertical_reference({"body": "vertical datum unavailable"}) == "UNKNOWN"


def test_oversized_one_meter_dem_falls_back_to_bounded_ten_meter_export(tmp_path, monkeypatch):
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.transform import from_bounds

    bbox = [-97.01, 32.99, -96.99, 33.01]
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=20, height=20, count=1, dtype="float32", crs="EPSG:4326", transform=from_bounds(*bbox, 20, 20)) as dataset:
            dataset.write(np.full((20, 20), 100.0, dtype=np.float32), 1)
        dem = memory.read()
    provider = USGSTerrainProvider(max_download_bytes=1)

    async def discover(_bbox, *, prefer_1m, force_10m=False):
        return ({"downloadURL": "https://example.test/dem.tif", "title": "fixture", "body": "NAVD 88"}, 10.0 if force_10m else 1.0)

    async def oversized(_url):
        raise WorldError("USGS DEM exceeds the bounded download limit.")

    async def export(_bbox):
        return dem, {"method": "3DEP_IMAGE_SERVER_EXPORT", "url": "https://example.test/export"}

    monkeypatch.setattr(provider, "_discover", discover)
    monkeypatch.setattr(provider, "_download", oversized)
    monkeypatch.setattr(provider, "_export_10m", export)
    layer = run(provider.build({"bbox": bbox}, ArtifactStore(tmp_path / "assets"), {"prefer_1m": True}))

    assert layer["source"]["nominal_resolution_m"] == 10.0
    assert "using approximately 10 m" in layer["warnings"][0]


def test_unavailable_usgs_catalog_falls_back_to_real_bounded_export(tmp_path, monkeypatch):
    import httpx
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.transform import from_bounds

    bbox = [-97.01, 32.99, -96.99, 33.01]
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=20, height=20, count=1, dtype="float32", crs="EPSG:4326", transform=from_bounds(*bbox, 20, 20)) as dataset:
            dataset.write(np.full((20, 20), 100.0, dtype=np.float32), 1)
        dem = memory.read()
    provider = USGSTerrainProvider()

    async def unavailable(*_args, **_kwargs):
        raise httpx.ReadTimeout("catalog unavailable")

    async def export(_bbox):
        return dem, {"method": "3DEP_IMAGE_SERVER_EXPORT", "url": "https://example.test/export"}

    monkeypatch.setattr(provider, "_discover", unavailable)
    monkeypatch.setattr(provider, "_export_10m", export)
    layer = run(provider.build({"bbox": bbox}, ArtifactStore(tmp_path / "assets"), {"prefer_1m": True}))

    assert layer["source"]["source_id"] == "3DEP_ELEVATION_IMAGESERVER"
    assert layer["terrain"]["vertical_reference"] == "UNKNOWN"
    assert layer["terrain"]["min_tile_zoom"] == 12
    assert any(key.startswith("12/") for key in layer["terrain"]["tiles"])
    assert "catalog metadata was unavailable" in layer["warnings"][0]


def test_transmission_is_explicitly_unavailable_without_verified_source(tmp_path):
    _store, site, service = world_service(tmp_path)
    world = run(service.create(site_snapshot_id=site["snapshot_id"], requested_layers=["transmission"]))
    layer = world["layers"][0]
    assert layer["availability"] == "UNAVAILABLE"
    assert layer["quality_state"] == "SOURCE_UNVERIFIED"
    assert "MIREYE proximity evidence" in layer["warnings"][0]


def test_scenario_links_world_and_anchors_terrain_but_rejects_world_mismatch(tmp_path):
    store, site, worlds = world_service(tmp_path)
    first_world = run(worlds.create(site_snapshot_id=site["snapshot_id"], requested_layers=["terrain", "roads"]))
    second_world = run(worlds.create(site_snapshot_id=site["snapshot_id"], buffer_m=1200, requested_layers=["terrain", "roads"]))
    scenarios = ScenarioService(store, worlds=worlds)

    first_scene = scene_state_from_snapshot(site)
    first_scene["world_snapshot_id"] = first_world["world_snapshot_id"]
    first = scenarios.create(site, workspace_id=site["workspace_id"], user_intent="World A", scene_state=first_scene)
    anchor = first["scene_state"]["proposed"][0]["terrain_anchor"]
    assert first["world_snapshot_id"] == first_world["world_snapshot_id"]
    assert anchor["terrain_sample_elevation_m"] == 42.0
    assert anchor["dem_artifact_hash"] == first_world["layers"][0]["artifacts"]["source_dem"]["sha256"]
    assert ScenarioService(WorkspaceStore(db_path=store.db_path), worlds=worlds).get(first["scenario_id"])["world_snapshot_id"] == first_world["world_snapshot_id"]

    second_scene = scene_state_from_snapshot(site)
    second_scene["world_snapshot_id"] = second_world["world_snapshot_id"]
    second = scenarios.create(site, workspace_id=site["workspace_id"], user_intent="World B", scene_state=second_scene)
    with pytest.raises(ScenarioError, match="different WorldSnapshots"):
        scenarios.compare(first["scenario_id"], second["scenario_id"])


def test_unknown_vertical_reference_keeps_world_link_without_inventing_anchor(tmp_path):
    store, site, worlds = world_service(tmp_path)
    world = run(worlds.create(site_snapshot_id=site["snapshot_id"], requested_layers=["terrain", "roads"]))
    stored = store.get_world_snapshot(world["world_snapshot_id"])
    stored["layers"][0]["terrain"]["vertical_reference"] = "UNKNOWN"
    original_get = worlds.get
    worlds.get = lambda world_id: stored if world_id == world["world_snapshot_id"] else original_get(world_id)
    scene = scene_state_from_snapshot(site)
    scene["world_snapshot_id"] = world["world_snapshot_id"]

    scenario = ScenarioService(store, worlds=worlds).create(
        site, workspace_id=site["workspace_id"], user_intent="Unknown datum",
        scene_state=scene,
    )

    assert scenario["world_snapshot_id"] == world["world_snapshot_id"]
    assert "terrain_anchor" not in scenario["scene_state"]["proposed"][0]
    assert scenario["scene_state"]["terrain_anchor_status"]["status"] == "UNRESOLVED"


def test_world_snapshot_api_uses_existing_site_and_returns_render_contract(monkeypatch):
    import app.main as main

    site = site_snapshot("site-world-api-test")
    main.workspace_store.create_site_snapshot(site)
    monkeypatch.setattr(main.world_service, "terrain_provider", FixtureTerrainProvider())
    monkeypatch.setattr(main.world_service, "road_provider", FixtureRoadProvider())
    client = TestClient(main.app)

    response = client.post("/v1/sandbox/world-snapshots", json={"site_snapshot_id": site["snapshot_id"], "requested_layers": ["terrain", "roads"]})
    assert response.status_code == 200
    created = response.json()
    assert created["layers"][0]["render"]["type"] == "raster-dem"
    assert created["layers"][1]["render"]["type"] == "geojson"
    assert all("storage_key" not in artifact for layer in created["layers"] for artifact in layer.get("artifacts", {}).values())
    assert client.get(f"/v1/sandbox/world-snapshots/{created['world_snapshot_id']}").json()["content_hash"] == created["content_hash"]


def test_identical_aoi_reuses_real_world_artifacts_across_site_snapshots(tmp_path):
    store, first_site, service = world_service(tmp_path, "site-world-first")
    first = run(service.create(site_snapshot_id=first_site["snapshot_id"], requested_layers=["terrain", "roads"]))
    second_site = site_snapshot("site-world-second")
    second_site["workspace_id"] = "ws-world-second"
    store.create_site_snapshot(second_site)

    async def should_not_build(*_args, **_kwargs):
        raise AssertionError("identical AOI artifacts should be reused")

    service.terrain_provider.build = should_not_build
    service.road_provider.build = should_not_build
    second = run(service.create(site_snapshot_id=second_site["snapshot_id"], requested_layers=["terrain", "roads"]))

    assert second["site_snapshot_id"] == second_site["snapshot_id"]
    assert second["layers"] == first["layers"]
    assert second["world_snapshot_id"] != first["world_snapshot_id"]


def test_road_timeout_does_not_block_available_world_layers(tmp_path):
    store, site, service = world_service(tmp_path)

    async def unavailable(*_args, **_kwargs):
        raise duckdb.IOException("upstream timeout")

    service.road_provider.build = unavailable
    world = run(service.create(site_snapshot_id=site["snapshot_id"], requested_layers=["terrain", "roads"]))

    assert world["layers"][0]["availability"] == "AVAILABLE"
    assert world["layers"][1]["availability"] == "UNAVAILABLE"
    assert "upstream timeout" in world["layers"][1]["warnings"][0]
