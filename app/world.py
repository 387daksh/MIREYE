"""Immutable, content-addressed physical-world snapshots for one sandbox AOI."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import duckdb
import httpx
import numpy as np
import pyarrow.parquet as pq
from PIL import Image
from shapely import wkb
from shapely.geometry import box, mapping, shape

from app.infrastructure.observability import traced_async
from app.infrastructure.storage import LocalArtifactStore
from app.infrastructure.storage.artifacts import ArtifactIntegrityError
from app.workspace.store import WorkspaceStore


WORLD_SCHEMA_VERSION = "world_snapshot_v2"
TERRAIN_ENCODING_VERSION = "mapbox_terrain_rgb_v1"
TERRAIN_SAMPLING_VERSION = "pinned_dem_bilinear_v1"
TERRAIN_MIN_ZOOM = 12
TERRAIN_TILE_PYRAMID_VERSION = "aoi_tile_cover_v2"
DEFAULT_OVERTURE_RELEASE = "2026-08-19.0"
OVERTURE_BUILD_TIMEOUT_SECONDS = 240
OVERTURE_RELEASE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
OVERTURE_STAC_INDEX = "https://stac.overturemaps.org/{release}/collections.parquet"
USGS_PRODUCTS_URL = "https://tnmaccess.nationalmap.gov/api/v1/products"
USGS_ELEVATION_EXPORT_URL = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
WEB_MERCATOR_LIMIT = 20037508.342789244


class WorldError(ValueError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def usgs_vertical_reference(product: dict) -> str:
    normalized = re.sub(r"[^A-Z0-9]", "", str(product.get("body", "")).upper())
    return "NAVD88" if "NAVD88" in normalized else "UNKNOWN"


class ArtifactStore(LocalArtifactStore):
    def path(self, artifact: dict) -> Path:
        try:
            return super().path(artifact)
        except ArtifactIntegrityError as exc:
            raise WorldError(str(exc)) from exc


def parcel_aoi(geometry: dict, *, buffer_m: float = 1000.0) -> dict:
    parcel = shape(geometry)
    if parcel.is_empty or parcel.geom_type not in {"Polygon", "MultiPolygon"}:
        raise WorldError("SiteSnapshot parcel geometry is unavailable.")
    min_lng, min_lat, max_lng, max_lat = parcel.bounds
    center_lat = (min_lat + max_lat) / 2
    lat_delta = buffer_m / 111_320.0
    lng_delta = buffer_m / (111_320.0 * max(math.cos(math.radians(center_lat)), 0.1))
    return {
        "geometry": mapping(box(min_lng - lng_delta, min_lat - lat_delta, max_lng + lng_delta, max_lat + lat_delta)),
        "bbox": [round(min_lng - lng_delta, 9), round(min_lat - lat_delta, 9), round(max_lng + lng_delta, 9), round(max_lat + lat_delta, 9)],
        "buffer_m": float(buffer_m), "crs": "EPSG:4326", "calculation": "parcel_bbox_geodesic_buffer_v1",
    }


def encode_terrain_rgb(elevations: np.ndarray) -> bytes:
    values = np.asarray(elevations, dtype=np.float64)
    encoded = np.clip(np.rint((np.nan_to_num(values, nan=-10_000.0) + 10_000.0) * 10.0), 0, 16_777_215).astype(np.uint32)
    rgb = np.dstack(((encoded >> 16) & 255, (encoded >> 8) & 255, encoded & 255)).astype(np.uint8)
    output = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(output, format="PNG", compress_level=9, optimize=False)
    return output.getvalue()


def _npy_bytes(values: np.ndarray) -> bytes:
    output = io.BytesIO()
    np.lib.format.write_array(output, np.asarray(values, dtype="<f4"), allow_pickle=False)
    return output.getvalue()


def _tile_xy(lng: float, lat: float, zoom: int) -> tuple[int, int]:
    scale = 2**zoom
    x = int((lng + 180.0) / 360.0 * scale)
    lat_rad = math.radians(max(min(lat, 85.05112878), -85.05112878))
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * scale)
    return x, y


def _tile_bounds_3857(x: int, y: int, zoom: int) -> tuple[float, float, float, float]:
    size = 2 * WEB_MERCATOR_LIMIT / (2**zoom)
    min_x = -WEB_MERCATOR_LIMIT + x * size
    max_y = WEB_MERCATOR_LIMIT - y * size
    return min_x, max_y - size, min_x + size, max_y


def _tile_cover_bbox(bbox: list[float], zoom: int) -> list[float]:
    min_x, max_y = _tile_xy(bbox[0], bbox[3], zoom)
    max_x, min_y = _tile_xy(bbox[2], bbox[1], zoom)
    scale = 2**zoom
    longitude = lambda x: x / scale * 360.0 - 180.0
    latitude = lambda y: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / scale))))
    return [longitude(min_x), latitude(min_y + 1), longitude(max_x + 1), latitude(max_y)]


def road_geojson(table, bbox: list[float]) -> dict:
    """Create deterministic AOI-clipped render geometry while retaining source rows separately."""
    clip = box(*bbox)
    features = []
    for row in table.to_pylist():
        geometry = wkb.loads(bytes(row["geometry"]))
        clipped = geometry.intersection(clip)
        if clipped.is_empty:
            continue
        features.append({
            "type": "Feature", "id": row["id"], "geometry": mapping(clipped),
            "properties": {"gers_id": row["id"], "name": row.get("name"), "class": row.get("class"), "subclass": row.get("subclass")},
        })
    return {"type": "FeatureCollection", "features": features}


def as_geoparquet(table, bbox: list[float], geometry_types: list[str] | None = None):
    metadata = dict(table.schema.metadata or {})
    metadata[b"geo"] = _canonical({
        "version": "1.1.0", "primary_column": "geometry",
        "columns": {"geometry": {"encoding": "WKB", "geometry_types": geometry_types or ["LineString", "MultiLineString"], "crs": "OGC:CRS84", "bbox": bbox}},
    })
    return table.replace_schema_metadata(metadata)


class USGSTerrainProvider:
    datasets = (
        ("Digital Elevation Model (DEM) 1 meter", 1.0),
        ("National Elevation Dataset (NED) 1/3 arc-second", 10.0),
    )

    def __init__(self, *, max_download_bytes: int = 250_000_000):
        self.max_download_bytes = max_download_bytes

    @traced_async("source.usgs.terrain")
    async def build(self, aoi: dict, artifacts: ArtifactStore, options: dict) -> dict:
        try:
            import rasterio
            from rasterio.io import MemoryFile
            from rasterio.transform import from_bounds
            from rasterio.warp import Resampling, reproject
        except ImportError as exc:
            raise WorldError("Real terrain requires rasterio; install requirements-world.txt.") from exc

        force_10m = options.get("_force_10m", False)
        catalog_warning = None
        try:
            product, nominal_resolution = await self._discover(
                aoi["bbox"], prefer_1m=options.get("prefer_1m", True), force_10m=force_10m,
            )
        except httpx.HTTPError:
            product = {
                "title": "USGS 3DEP Elevation ImageServer",
                "downloadURL": USGS_ELEVATION_EXPORT_URL,
                "sourceId": "3DEP_ELEVATION_IMAGESERVER",
            }
            nominal_resolution = 10.0
            force_10m = True
            catalog_warning = "USGS catalog metadata was unavailable; using the bounded 3DEP 10 m ImageServer export with vertical datum marked unknown."
        extraction = {"method": "TNM_PRODUCT_DOWNLOAD", "url": product["downloadURL"]}
        if nominal_resolution > 1:
            source_bytes, extraction = await self._export_10m(_tile_cover_bbox(aoi["bbox"], TERRAIN_MIN_ZOOM))
        else:
            try:
                source_bytes = await self._download(product["downloadURL"])
            except WorldError as exc:
                if not force_10m and "bounded download limit" in str(exc):
                    return await self.build(aoi, artifacts, {**options, "_force_10m": True})
                raise
        source_artifact = artifacts.put(source_bytes, extension="tif", media_type="image/tiff", role="source_dem")
        metadata_artifact = artifacts.put(_canonical({"catalog_product": product, "extraction": extraction}), extension="json", media_type="application/json", role="source_metadata")
        bbox = aoi["bbox"]
        warnings = [catalog_warning] if catalog_warning else []
        with MemoryFile(source_bytes) as memory:
            with memory.open() as source:
                if source.crs is None:
                    raise WorldError("USGS DEM does not declare a horizontal CRS.")
                source_crs = str(source.crs)
                resolution_m = self._resolution_m(source, bbox)
                vertical_reference = usgs_vertical_reference(product)
                if vertical_reference == "UNKNOWN":
                    warnings.append("DEM vertical datum was not explicit; terrain calculations are disabled.")
                grid = np.full((256, 256), np.nan, dtype=np.float32)
                reproject(
                    rasterio.band(source, 1), grid, src_transform=source.transform, src_crs=source.crs,
                    src_nodata=source.nodata, dst_transform=from_bounds(*bbox, 256, 256), dst_crs="EPSG:4326",
                    dst_nodata=np.nan, resampling=Resampling.bilinear,
                )
                if np.isfinite(grid).mean() < 0.95:
                    if nominal_resolution <= 1 and not force_10m:
                        return await self.build(aoi, artifacts, {**options, "_force_10m": True})
                    raise WorldError("USGS DEM does not contain usable elevation coverage for the sandbox AOI.")
                grid_artifact = artifacts.put(_npy_bytes(grid), extension="npy", media_type="application/x-npy", role="elevation_grid")
                zoom = 17 if resolution_m <= 2 else 14
                tiles = {}
                for tile_zoom in range(TERRAIN_MIN_ZOOM, zoom + 1):
                    min_x, max_y = _tile_xy(bbox[0], bbox[3], tile_zoom)
                    max_x, min_y = _tile_xy(bbox[2], bbox[1], tile_zoom)
                    for x in range(min_x, max_x + 1):
                        for y in range(max_y, min_y + 1):
                            tile = np.full((256, 256), np.nan, dtype=np.float32)
                            reproject(
                                rasterio.band(source, 1), tile, src_transform=source.transform, src_crs=source.crs,
                                src_nodata=source.nodata, dst_transform=from_bounds(*_tile_bounds_3857(x, y, tile_zoom), 256, 256),
                                dst_crs="EPSG:3857", dst_nodata=np.nan, resampling=Resampling.bilinear,
                            )
                            tile_artifact = artifacts.put(encode_terrain_rgb(tile), extension="png", media_type="image/png", role="terrain_rgb_tile")
                            tiles[f"{tile_zoom}/{x}/{y}"] = tile_artifact
                tile_manifest = {"encoding": TERRAIN_ENCODING_VERSION, "minzoom": TERRAIN_MIN_ZOOM, "maxzoom": zoom, "tile_size": 256, "tiles": tiles}
                tile_manifest_artifact = artifacts.put(_canonical(tile_manifest), extension="json", media_type="application/json", role="terrain_tile_manifest")
        if nominal_resolution > 1:
            warnings.append("USGS 1 m coverage was unavailable; using approximately 10 m 3DEP coverage.")
        return {
            "layer": "terrain", "availability": "AVAILABLE", "quality_state": "VERIFIED_SOURCE",
            "source": {
                "provider": "USGS", "dataset": product.get("title"), "source_id": product.get("sourceId"),
                "publication_date": product.get("publicationDate"), "last_updated": product.get("lastUpdated"),
                "download_url": extraction["url"], "catalog_download_url": product.get("downloadURL"),
                "extraction_method": extraction["method"], "metadata_url": product.get("vendorMetaUrl") or product.get("metaUrl"),
                "license": "USGS public domain", "nominal_resolution_m": nominal_resolution,
            },
            "artifacts": {"source_dem": source_artifact, "source_metadata": metadata_artifact, "elevation_grid": grid_artifact, "tile_manifest": tile_manifest_artifact},
            "terrain": {
                "actual_resolution_m": round(resolution_m, 6), "source_crs": source_crs,
                "vertical_reference": vertical_reference, "vertical_units": "meters", "grid_bbox": bbox,
                "grid_width": 256, "grid_height": 256, "grid_orientation": "north_up",
                "min_tile_zoom": TERRAIN_MIN_ZOOM, "tile_zoom": zoom, "tile_size": 256,
                "tile_pyramid_version": TERRAIN_TILE_PYRAMID_VERSION,
                "encoding": TERRAIN_ENCODING_VERSION, "tiles": tiles,
            },
            "warnings": warnings, "conflicts": [],
        }

    async def _discover(self, bbox: list[float], *, prefer_1m: bool, force_10m: bool = False) -> tuple[dict, float]:
        order = (self.datasets[1],) if force_10m else self.datasets if prefer_1m else tuple(reversed(self.datasets))
        async with httpx.AsyncClient(timeout=30) as client:
            for dataset, resolution in order:
                response = await client.get(USGS_PRODUCTS_URL, params={"datasets": dataset, "bbox": ",".join(map(str, bbox)), "outputFormat": "JSON", "max": 20})
                response.raise_for_status()
                candidates = [item for item in response.json().get("items", []) if item.get("downloadURL") and self._covers(item.get("boundingBox"), bbox)]
                if candidates:
                    candidates.sort(key=lambda item: (item.get("publicationDate") or "", item.get("lastUpdated") or "", item.get("sourceId") or ""), reverse=True)
                    return candidates[0], resolution
        raise WorldError("USGS 3DEP has no single 1 m or 10 m product covering the sandbox AOI.")

    @staticmethod
    def _covers(bounds: dict | None, bbox: list[float]) -> bool:
        return bool(bounds and bounds["minX"] <= bbox[0] and bounds["minY"] <= bbox[1] and bounds["maxX"] >= bbox[2] and bounds["maxY"] >= bbox[3])

    async def _download(self, url: str) -> bytes:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                declared = int(response.headers.get("content-length", 0))
                if declared and declared > self.max_download_bytes:
                    raise WorldError("USGS DEM exceeds the bounded download limit.")
                parts, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_download_bytes:
                        raise WorldError("USGS DEM exceeded the bounded download limit.")
                    parts.append(chunk)
        return b"".join(parts)

    async def _export_10m(self, bbox: list[float]) -> tuple[bytes, dict]:
        from rasterio.warp import transform_bounds

        center_lng, center_lat = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        zone = int((center_lng + 180) // 6) + 1
        output_epsg = (32600 if center_lat >= 0 else 32700) + zone
        projected_bbox = transform_bounds("EPSG:4326", f"EPSG:{output_epsg}", *bbox)
        width = max(2, math.ceil((projected_bbox[2] - projected_bbox[0]) / 10))
        height = max(2, math.ceil((projected_bbox[3] - projected_bbox[1]) / 10))
        params = {
            "bbox": ",".join(map(str, projected_bbox)), "bboxSR": output_epsg, "imageSR": output_epsg,
            "size": f"{width},{height}", "format": "tiff", "pixelType": "F32",
            "interpolation": "RSP_BilinearInterpolation", "f": "image",
        }
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            response = await client.get(USGS_ELEVATION_EXPORT_URL, params=params)
            response.raise_for_status()
        if len(response.content) > self.max_download_bytes:
            raise WorldError("USGS bounded DEM export exceeded the download limit.")
        if response.headers.get("content-type", "").startswith("application/json"):
            raise WorldError(f"USGS bounded DEM export failed: {response.json().get('error', {}).get('message', 'unknown error')}.")
        return response.content, {"method": "3DEP_IMAGE_SERVER_EXPORT", "url": USGS_ELEVATION_EXPORT_URL, "parameters": params}

    @staticmethod
    def _resolution_m(source, bbox: list[float]) -> float:
        if source.crs.is_geographic:
            return max(abs(source.res[0]) * 111_320 * math.cos(math.radians((bbox[1] + bbox[3]) / 2)), abs(source.res[1]) * 111_320)
        return max(abs(source.res[0]), abs(source.res[1]))


class OvertureRoadProvider:
    @traced_async("source.overture.roads")
    async def build(self, aoi: dict, artifacts: ArtifactStore, options: dict) -> dict:
        release = options.get("overture_release", DEFAULT_OVERTURE_RELEASE)
        if not OVERTURE_RELEASE_PATTERN.fullmatch(release):
            raise WorldError("Invalid Overture release identifier.")
        return await asyncio.to_thread(self._build_sync, aoi, artifacts, release)

    @staticmethod
    def _build_sync(aoi: dict, artifacts: ArtifactStore, release: str) -> dict:
        bbox = aoi["bbox"]
        paths = _overture_paths(release, "segment", bbox)
        connection = duckdb.connect()
        try:
            connection.execute("INSTALL httpfs")
            connection.execute("LOAD httpfs")
            connection.execute("SET s3_region='us-west-2'")
            table = connection.execute(
                """
                SELECT id, names.primary AS name, subtype, class, subclass, geometry, sources, bbox
                FROM read_parquet(?)
                WHERE subtype = 'road'
                  AND bbox.xmin <= ? AND bbox.xmax >= ?
                  AND bbox.ymin <= ? AND bbox.ymax >= ?
                ORDER BY id
                """,
                [paths, bbox[2], bbox[0], bbox[3], bbox[1]],
            ).fetch_arrow_table()
        finally:
            connection.close()
        table = as_geoparquet(table, bbox)
        parquet_buffer = io.BytesIO()
        pq.write_table(table, parquet_buffer, compression="zstd", version="2.6", write_statistics=True)
        source_artifact = artifacts.put(parquet_buffer.getvalue(), extension="parquet", media_type="application/vnd.apache.parquet", role="overture_aoi_geoparquet")
        geojson = road_geojson(table, bbox)
        render_artifact = artifacts.put(_canonical(geojson), extension="geojson", media_type="application/geo+json", role="roads_render_geojson")
        return {
            "layer": "roads", "availability": "AVAILABLE", "quality_state": "VERIFIED_SOURCE",
            "source": {
                "provider": "Overture Maps Foundation", "release": release, "schema_theme": "transportation/segment",
                "source_uri": paths, "license": "ODbL-1.0", "attribution": "OpenStreetMap contributors, Overture Maps Foundation",
            },
            "artifacts": {"source_geoparquet": source_artifact, "render_geojson": render_artifact},
            "roads": {"feature_count": len(geojson["features"]), "geometry": "source LineString; AOI-clipped render geometry", "identity_field": "GERS id"},
            "warnings": ["Mapped road geometry does not prove legal access, frontage, easement, or heavy-haul suitability."], "conflicts": [],
        }


def _polygon_geojson(table, bbox: list[float], properties: list[str]) -> dict:
    clip = box(*bbox)
    features = []
    for row in table.to_pylist():
        geometry = wkb.loads(bytes(row["geometry"])).intersection(clip)
        if geometry.is_empty:
            continue
        features.append({
            "type": "Feature", "id": row["id"], "geometry": mapping(geometry),
            "properties": {name: row.get(name) for name in properties},
        })
    return {"type": "FeatureCollection", "features": features}


def _overture_paths(release: str, source_type: str, bbox: list[float]) -> list[str]:
    response = httpx.get(OVERTURE_STAC_INDEX.format(release=release), timeout=30)
    response.raise_for_status()
    index = pq.read_table(io.BytesIO(response.content), columns=["collection", "type", "bbox", "assets"])
    paths = []
    for row in index.to_pylist():
        bounds = row["bbox"]
        if (
            row["collection"] == source_type
            and row["type"] == "Feature"
            and bounds["xmin"] < bbox[2]
            and bounds["xmax"] > bbox[0]
            and bounds["ymin"] < bbox[3]
            and bounds["ymax"] > bbox[1]
        ):
            paths.append(row["assets"]["aws"]["href"])
    if not paths:
        raise WorldError(f"Overture STAC index found no {source_type} files for the AOI.")
    return paths


class OvertureBuildingProvider:
    @traced_async("source.overture.buildings")
    async def build(self, aoi: dict, artifacts: ArtifactStore, options: dict) -> dict:
        release = options.get("overture_release", DEFAULT_OVERTURE_RELEASE)
        if not OVERTURE_RELEASE_PATTERN.fullmatch(release):
            raise WorldError("Invalid Overture release identifier.")
        return await asyncio.to_thread(self._build_sync, aoi, artifacts, release)

    @staticmethod
    def _build_sync(aoi: dict, artifacts: ArtifactStore, release: str) -> dict:
        bbox = aoi["bbox"]
        paths = _overture_paths(release, "building", bbox)
        connection = duckdb.connect()
        try:
            connection.execute("INSTALL httpfs")
            connection.execute("LOAD httpfs")
            connection.execute("SET s3_region='us-west-2'")
            table = connection.execute(
                """
                SELECT id, names.primary AS name, class, subtype, height AS height_m,
                       num_floors, geometry, sources, bbox
                FROM read_parquet(?)
                WHERE bbox.xmin <= ? AND bbox.xmax >= ?
                  AND bbox.ymin <= ? AND bbox.ymax >= ?
                ORDER BY id
                """,
                [paths, bbox[2], bbox[0], bbox[3], bbox[1]],
            ).fetch_arrow_table()
        finally:
            connection.close()
        table = as_geoparquet(table, bbox, ["Polygon", "MultiPolygon"])
        parquet_buffer = io.BytesIO()
        pq.write_table(table, parquet_buffer, compression="zstd", version="2.6", write_statistics=True)
        source_artifact = artifacts.put(parquet_buffer.getvalue(), extension="parquet", media_type="application/vnd.apache.parquet", role="overture_buildings_aoi_geoparquet")
        geojson = _polygon_geojson(table, bbox, ["name", "class", "subtype", "height_m", "num_floors"])
        render_artifact = artifacts.put(_canonical(geojson), extension="geojson", media_type="application/geo+json", role="buildings_render_geojson")
        return {
            "layer": "buildings", "availability": "AVAILABLE", "quality_state": "VERIFIED_SOURCE",
            "source": {
                "provider": "Overture Maps Foundation", "release": release,
                "schema_theme": "buildings/building", "source_uri": paths,
                "license": "ODbL-1.0", "attribution": "Overture Maps Foundation and source contributors",
            },
            "artifacts": {"source_geoparquet": source_artifact, "render_geojson": render_artifact},
            "buildings": {
                "feature_count": len(geojson["features"]), "identity_field": "GERS id",
                "height_policy": "source_height_only",
            },
            "warnings": ["Buildings without a published source height are rendered as footprints, not invented extrusions."],
            "conflicts": [],
        }


class OverturePolygonProvider:
    def __init__(self, layer: str, source_type: str):
        self.layer, self.source_type = layer, source_type

    @traced_async("source.overture.context")
    async def build(self, aoi: dict, artifacts: ArtifactStore, options: dict) -> dict:
        release = options.get("overture_release", DEFAULT_OVERTURE_RELEASE)
        if not OVERTURE_RELEASE_PATTERN.fullmatch(release):
            raise WorldError("Invalid Overture release identifier.")
        return await asyncio.to_thread(self._build_sync, aoi, artifacts, release)

    def _build_sync(self, aoi: dict, artifacts: ArtifactStore, release: str) -> dict:
        bbox = aoi["bbox"]
        paths = _overture_paths(release, self.source_type, bbox)
        connection = duckdb.connect()
        try:
            connection.execute("INSTALL httpfs")
            connection.execute("LOAD httpfs")
            connection.execute("SET s3_region='us-west-2'")
            table = connection.execute(
                """
                SELECT id, subtype, geometry, sources, bbox
                FROM read_parquet(?)
                WHERE bbox.xmin <= ? AND bbox.xmax >= ?
                  AND bbox.ymin <= ? AND bbox.ymax >= ?
                ORDER BY id
                """,
                [paths, bbox[2], bbox[0], bbox[3], bbox[1]],
            ).fetch_arrow_table()
        finally:
            connection.close()
        table = as_geoparquet(table, bbox, ["Polygon", "MultiPolygon"])
        parquet_buffer = io.BytesIO()
        pq.write_table(table, parquet_buffer, compression="zstd", version="2.6", write_statistics=True)
        source_artifact = artifacts.put(parquet_buffer.getvalue(), extension="parquet", media_type="application/vnd.apache.parquet", role=f"overture_{self.layer}_aoi_geoparquet")
        geojson = _polygon_geojson(table, bbox, ["subtype"])
        render_artifact = artifacts.put(_canonical(geojson), extension="geojson", media_type="application/geo+json", role=f"{self.layer}_render_geojson")
        return {
            "layer": self.layer, "availability": "AVAILABLE", "quality_state": "VERIFIED_SOURCE",
            "source": {
                "provider": "Overture Maps Foundation", "release": release,
                "schema_theme": f"base/{self.source_type}", "source_uri": paths,
                "license": "ODbL-1.0", "attribution": "Overture Maps Foundation and source contributors",
            },
            "artifacts": {"source_geoparquet": source_artifact, "render_geojson": render_artifact},
            self.layer: {"feature_count": len(geojson["features"]), "identity_field": "GERS id"},
            "warnings": [], "conflicts": [],
        }


class WorldSnapshotService:
    def __init__(
        self, store: WorkspaceStore, artifacts: ArtifactStore, *, terrain_provider=None,
        road_provider=None, building_provider=None, water_provider=None, land_cover_provider=None,
    ):
        self.store, self.artifacts = store, artifacts
        self.terrain_provider = terrain_provider or USGSTerrainProvider()
        self.road_provider = road_provider or OvertureRoadProvider()
        self.building_provider = building_provider or OvertureBuildingProvider()
        self.water_provider = water_provider or OverturePolygonProvider("water", "water")
        self.land_cover_provider = land_cover_provider or OverturePolygonProvider("land_cover", "land_cover")

    async def create(self, *, site_snapshot_id: str, buffer_m: float = 1000, requested_layers: list[str] | None = None, options: dict | None = None) -> dict:
        site = self.store.get_site_snapshot(site_snapshot_id)
        if site is None:
            raise WorldError("SiteSnapshot not found.")
        layers = requested_layers or ["terrain", "roads"]
        invalid = set(layers) - {"terrain", "roads", "buildings", "water", "land_cover", "transmission"}
        if invalid:
            raise WorldError(f"Unsupported world layers: {', '.join(sorted(invalid))}.")
        query_aoi = parcel_aoi(site["geometry"], buffer_m=buffer_m)
        latest = self.latest_for_site_snapshot(site_snapshot_id)
        if latest is None:
            latest = next((item for item in self.store.list_world_snapshots() if item.get("query_aoi", {}).get("bbox") == query_aoi["bbox"] and item["query_aoi"].get("buffer_m") == query_aoi["buffer_m"]), None)
        same_aoi = latest and latest.get("query_aoi", {}).get("bbox") == query_aoi["bbox"] and latest["query_aoi"].get("buffer_m") == query_aoi["buffer_m"]
        existing = {item["layer"]: item for item in latest.get("layers", [])} if same_aoi else {}
        built = []
        options = options or {}
        if "terrain" in layers:
            terrain = existing.get("terrain")
            try:
                built.append(terrain if terrain and terrain.get("terrain", {}).get("tile_pyramid_version") == TERRAIN_TILE_PYRAMID_VERSION else await self.terrain_provider.build(query_aoi, self.artifacts, options))
            except (WorldError, httpx.HTTPError) as exc:
                built.append({
                    "layer": "terrain", "availability": "UNAVAILABLE", "quality_state": "SOURCE_UNAVAILABLE",
                    "source": {"provider": "USGS", "dataset": "3DEP"}, "artifacts": {},
                    "warnings": [f"USGS terrain could not be loaded: {exc}"], "conflicts": [],
                })
        if "roads" in layers:
            roads = existing.get("roads")
            requested_release = options.get("overture_release", DEFAULT_OVERTURE_RELEASE)
            try:
                reusable = (
                    roads
                    and roads.get("availability") == "AVAILABLE"
                    and roads.get("source", {}).get("release") == requested_release
                )
                built.append(roads if reusable else await self.road_provider.build(query_aoi, self.artifacts, options))
            except (WorldError, duckdb.Error, httpx.HTTPError) as exc:
                built.append({
                    "layer": "roads", "availability": "UNAVAILABLE", "quality_state": "SOURCE_UNAVAILABLE",
                    "source": {"provider": "Overture Maps Foundation", "release": requested_release}, "artifacts": {},
                    "warnings": [f"Overture roads could not be loaded: {exc}"], "conflicts": [],
                })
        optional_layers = [item for item in (
            ("buildings", self.building_provider),
            ("water", self.water_provider),
            ("land_cover", self.land_cover_provider),
        ) if item[0] in layers]

        async def build_optional(layer_name, provider):
            current = existing.get(layer_name)
            requested_release = options.get("overture_release", DEFAULT_OVERTURE_RELEASE)
            try:
                if (
                    current
                    and current.get("availability") == "AVAILABLE"
                    and current.get("source", {}).get("release") == requested_release
                ):
                    return current
                return await asyncio.wait_for(
                    provider.build(query_aoi, self.artifacts, options),
                    timeout=OVERTURE_BUILD_TIMEOUT_SECONDS,
                )
            except (WorldError, duckdb.Error, httpx.HTTPError, asyncio.TimeoutError) as exc:
                return {
                    "layer": layer_name, "availability": "UNAVAILABLE", "quality_state": "SOURCE_UNAVAILABLE",
                    "source": {"provider": "Overture Maps Foundation", "release": requested_release},
                    "artifacts": {}, "warnings": [f"Overture {layer_name.replace('_', ' ')} could not be loaded within the bounded request: {str(exc) or 'timeout'}"], "conflicts": [],
                }

        if optional_layers:
            built.extend(await asyncio.gather(*(build_optional(*item) for item in optional_layers)))
        if "transmission" in layers:
            built.append({
                "layer": "transmission", "availability": "UNAVAILABLE", "quality_state": "SOURCE_UNVERIFIED",
                "source": None, "artifacts": {}, "warnings": ["No current release-pinned transmission geometry source with verified distribution metadata is configured. MIREYE proximity evidence remains available."], "conflicts": [],
            })
        for item in built:
            item.update({
                "origin": "OBSERVED", "aoi": query_aoi,
                "crs": "EPSG:3857" if item["layer"] == "terrain" else "OGC:CRS84",
                "freshness": {"policy": "PINNED_SOURCE_ARTIFACT", "state": "PINNED" if item.get("availability") == "AVAILABLE" else "UNAVAILABLE"},
            })
        source_manifest = [{
            "layer": item["layer"], "source": item["source"],
            "availability": item["availability"], "quality_state": item["quality_state"],
            "aoi": query_aoi, "spatial_reference": item["crs"],
            "license": (item.get("source") or {}).get("license"),
            "artifact_hashes": {name: artifact["sha256"] for name, artifact in sorted(item.get("artifacts", {}).items())},
        } for item in built if item.get("source")]
        content = {
            "schema_version": WORLD_SCHEMA_VERSION, "site_snapshot_id": site_snapshot_id,
            "query_aoi": query_aoi, "source_manifest": source_manifest, "layers": built,
            "crs": {"storage": "EPSG:4326", "terrain_render": "EPSG:3857"},
            "vertical_reference": next((item.get("terrain", {}).get("vertical_reference") for item in built if item["layer"] == "terrain"), "UNAVAILABLE"),
            "quality_conflicts": [conflict for item in built for conflict in item.get("conflicts", [])],
        }
        digest = _sha256(_canonical(content))
        snapshot = {**content, "world_snapshot_id": f"world_{digest[:32]}", "content_hash": digest, "created_at": time.time()}
        return self.store.create_world_snapshot(snapshot)

    def get(self, world_snapshot_id: str) -> dict | None:
        return self.store.get_world_snapshot(world_snapshot_id)

    def latest_for_site_snapshot(self, site_snapshot_id: str) -> dict | None:
        return self.store.latest_world_snapshot_for_site_snapshot(site_snapshot_id)

    def public(self, snapshot: dict) -> dict:
        result = json.loads(json.dumps(snapshot))
        for layer in result["layers"]:
            for artifact in layer.get("artifacts", {}).values():
                artifact.pop("storage_key", None)
            if layer["layer"] == "terrain" and layer["availability"] == "AVAILABLE":
                for artifact in layer["terrain"]["tiles"].values():
                    artifact.pop("storage_key", None)
                layer["render"] = {
                    "type": "raster-dem", "encoding": "mapbox", "tile_size": layer["terrain"]["tile_size"],
                    "minzoom": layer["terrain"].get("min_tile_zoom", layer["terrain"]["tile_zoom"]),
                    "maxzoom": layer["terrain"]["tile_zoom"],
                    "tiles": [f"/v1/sandbox/world-snapshots/{snapshot['world_snapshot_id']}/terrain/{{z}}/{{x}}/{{y}}"],
                }
            if layer["layer"] in {"roads", "buildings", "water", "land_cover"} and layer["availability"] == "AVAILABLE":
                layer["render"] = {"type": "geojson", "url": f"/v1/sandbox/world-snapshots/{snapshot['world_snapshot_id']}/layers/{layer['layer']}"}
        for entry in result["source_manifest"]:
            entry["acquired_at"] = result["created_at"]
        return result

    def artifact_for_tile(self, snapshot: dict, z: int, x: int, y: int) -> Path:
        layer = self._layer(snapshot, "terrain")
        artifact = layer.get("terrain", {}).get("tiles", {}).get(f"{z}/{x}/{y}")
        if artifact is None:
            raise WorldError("Terrain tile was not found.")
        return self.artifacts.path(artifact)

    def road_artifact(self, snapshot: dict) -> Path:
        return self.artifacts.path(self._layer(snapshot, "roads")["artifacts"]["render_geojson"])

    def vector_artifact(self, snapshot: dict, layer_name: str) -> Path:
        if layer_name not in {"roads", "buildings", "water", "land_cover"}:
            raise WorldError("World vector layer is unavailable.")
        return self.artifacts.path(self._layer(snapshot, layer_name)["artifacts"]["render_geojson"])

    def anchor_scene(self, scene_state: dict, world_snapshot_id: str) -> dict:
        snapshot = self.get(world_snapshot_id)
        if snapshot is None:
            raise WorldError("WorldSnapshot not found.")
        if scene_state.get("site_snapshot_id") != snapshot["site_snapshot_id"]:
            raise WorldError("WorldSnapshot does not reference this scene's SiteSnapshot.")
        layer = self._layer(snapshot, "terrain")
        if layer["availability"] != "AVAILABLE" or layer.get("terrain", {}).get("vertical_reference") == "UNKNOWN":
            result = json.loads(json.dumps(scene_state))
            result["world_snapshot_id"] = world_snapshot_id
            result["terrain_anchor_status"] = {
                "status": "UNRESOLVED",
                "reason": "Pinned terrain has no verified vertical reference; proposed elevations were not assigned.",
            }
            return result
        result = json.loads(json.dumps(scene_state))
        result["world_snapshot_id"] = world_snapshot_id
        result["terrain_anchor_status"] = {"status": "DERIVED", "vertical_reference": layer["terrain"]["vertical_reference"]}
        origin = result["frame"]["origin"]
        for item in result.get("proposed", []):
            x, y = item["geometry_local"]["center_xy_m"]
            lat = origin["lat"] + math.degrees(y / 6_371_008.8)
            lng = origin["lng"] + math.degrees(x / (6_371_008.8 * math.cos(math.radians(origin["lat"]))))
            elevation = self.sample_elevation(snapshot, lng=lng, lat=lat)
            item["terrain_anchor"] = {
                "terrain_sample_elevation_m": round(elevation, 4), "world_snapshot_id": world_snapshot_id,
                "sample_point": {"lng": round(lng, 9), "lat": round(lat, 9)},
                "dem_artifact_hash": layer["artifacts"]["source_dem"]["sha256"],
                "vertical_reference": layer["terrain"]["vertical_reference"], "calculation_version": TERRAIN_SAMPLING_VERSION,
            }
        return result

    def sample_elevation(self, snapshot: dict, *, lng: float, lat: float) -> float:
        layer = self._layer(snapshot, "terrain")
        metadata = layer["terrain"]
        grid = np.load(self.artifacts.path(layer["artifacts"]["elevation_grid"]), allow_pickle=False)
        min_lng, min_lat, max_lng, max_lat = metadata["grid_bbox"]
        if not (min_lng <= lng <= max_lng and min_lat <= lat <= max_lat):
            raise WorldError("Terrain sample point is outside the WorldSnapshot AOI.")
        column = (lng - min_lng) / (max_lng - min_lng) * (metadata["grid_width"] - 1)
        row = (max_lat - lat) / (max_lat - min_lat) * (metadata["grid_height"] - 1)
        x0, y0 = int(math.floor(column)), int(math.floor(row))
        x1, y1 = min(x0 + 1, grid.shape[1] - 1), min(y0 + 1, grid.shape[0] - 1)
        dx, dy = column - x0, row - y0
        values = np.array([grid[y0, x0], grid[y0, x1], grid[y1, x0], grid[y1, x1]], dtype=float)
        if np.isnan(values).any():
            raise WorldError("Pinned DEM has no elevation at the proposed object sample point.")
        return float(values[0] * (1 - dx) * (1 - dy) + values[1] * dx * (1 - dy) + values[2] * (1 - dx) * dy + values[3] * dx * dy)

    @staticmethod
    def _layer(snapshot: dict, name: str) -> dict:
        for layer in snapshot.get("layers", []):
            if layer.get("layer") == name:
                return layer
        raise WorldError(f"WorldSnapshot layer is unavailable: {name}.")
