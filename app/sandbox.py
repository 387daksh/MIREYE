"""Real-parcel SiteSnapshot acquisition for the Site Sandbox."""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from typing import Any

import httpx
from shapely.geometry import Point, mapping, shape

from app.mireye_client import MireyeClient
from app.workspace.store import WorkspaceStore


SITE_SNAPSHOT_FIELDS = (
    "parcel_id",
    "parcel_apn",
    "parcel_address",
    "parcel_area_m2",
    "parcel_boundary_geojson",
    "parcel_data_source",
    "parcel_match_type",
    "parcel_match_distance_m",
    "parcel_match_radius_m",
    "parcel_zoning",
    "elevation",
    "slope_degrees",
    "fema_flood_zone",
    "within_floodplain_polygon",
    "wetland_acres_on_parcel",
    "wetland_fraction_of_parcel",
    "nearest_substation_distance_m",
    "nearest_substation_max_voltage_kv",
    "nearest_substation_status",
    "substations_within_radius_count",
    "nearest_transmission_line_distance_m",
    "nearest_transmission_line_voltage_kv",
    "nearest_transmission_line_voltage_class",
    "nearest_transmission_line_status",
    "transmission_lines_within_radius_count",
    "nearest_major_road_name",
    "nearest_major_road_distance_m",
    "nearest_major_road_class",
    "fiber_broadband_available",
    "fiber_provider_count",
    "within_water_service_area",
    "water_system_name",
    "within_sewer_service_area",
    "sewer_service_area_provider",
)

SCENE_SCHEMA_VERSION = "1"
EARTH_RADIUS_M = 6_371_008.8


class SandboxError(ValueError):
    """Base error for an invalid or incomplete Site Sandbox operation."""


class ConfirmationRequired(SandboxError):
    pass


class ParcelIdentityError(SandboxError):
    pass


class FieldCatalogError(SandboxError):
    pass


class MireyeUnavailableError(SandboxError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def serialize_scene_state(scene_state: dict) -> str:
    """Serialize a scene deterministically for browser-local state transport."""
    return _canonical_json(scene_state)


def deserialize_scene_state(payload: str) -> dict:
    scene_state = json.loads(payload)
    required = {"schema_version", "site_snapshot_id", "observed", "proposed", "camera"}
    if not isinstance(scene_state, dict) or not required.issubset(scene_state):
        raise SandboxError("Invalid scene state payload.")
    if scene_state["schema_version"] != SCENE_SCHEMA_VERSION:
        raise SandboxError("Unsupported scene state schema version.")
    return scene_state


def _local_xy(lat: float, lng: float, origin: dict[str, float]) -> list[float]:
    lat_delta = math.radians(lat - origin["lat"])
    lng_delta = math.radians(lng - origin["lng"])
    x = EARTH_RADIUS_M * math.cos(math.radians(origin["lat"])) * lng_delta
    y = EARTH_RADIUS_M * lat_delta
    return [round(x, 3), round(y, 3)]


def scene_state_from_snapshot(snapshot: dict) -> dict:
    """Build the mutable visual state from one immutable SiteSnapshot."""
    parcel = shape(snapshot["geometry"])
    if parcel.geom_type not in {"Polygon", "MultiPolygon"} or parcel.is_empty:
        raise SandboxError("SiteSnapshot does not contain a renderable parcel geometry.")
    selected_point = snapshot["parcel_identity"]["selected_point"]
    origin = {"lat": float(selected_point["lat"]), "lng": float(selected_point["lng"])}
    centroid = parcel.centroid
    centroid_geometry = {"type": "Point", "coordinates": [centroid.x, centroid.y]}
    ground_geometry = json.loads(json.dumps(mapping(parcel.envelope)))

    return {
        "schema_version": SCENE_SCHEMA_VERSION,
        "scene_version": 1,
        "site_snapshot_id": snapshot["snapshot_id"],
        "frame": {
            "geographic_crs": "EPSG:4326",
            "origin": origin,
            "local_units": "meters",
            "coordinate_frame_version": "local_tangent_plane_v1",
        },
        "observed": [
            {
                "id": "parcel_boundary",
                "kind": "parcel_boundary",
                "origin": "OBSERVED",
                "geometry": snapshot["geometry"],
                "evidence_ids": ["parcel_id", "parcel_boundary_geojson"],
            },
            {
                "id": "resolution_point",
                "kind": "resolution_point",
                "origin": "OBSERVED",
                "geometry": {"type": "Point", "coordinates": [origin["lng"], origin["lat"]]},
                "evidence_ids": ["parcel_match_type", "parcel_match_distance_m"],
            },
        ],
        "derived": [
            {
                "id": "parcel_centroid",
                "kind": "parcel_centroid",
                "origin": "DERIVED",
                "geometry": centroid_geometry,
                "derivation": "geometry_centroid",
            },
            {
                "id": "flat_ground_plane",
                "kind": "ground_plane",
                "origin": "DERIVED",
                "geometry": ground_geometry,
                "representation": "CONCEPTUAL_FLAT",
            },
        ],
        "proposed": [
            {
                "id": "data_center_1",
                "kind": "data_center",
                "origin": "PROPOSED",
                "geometry_local": {
                    "shape": "oriented_rectangle",
                    "center_xy_m": _local_xy(centroid.y, centroid.x, origin),
                    "width_m": 250.0,
                    "length_m": 350.0,
                    "height_m": 28.0,
                    "rotation_deg": 0.0,
                },
                "attributes": {"capacity_mw": 100.0},
                "assumption_profile": "conceptual_data_center_v1",
            }
        ],
        "camera": {"center": {"lng": centroid.x, "lat": centroid.y}, "zoom": 17.0, "pitch": 55.0, "bearing": 0.0},
    }


def _field_value(fields: dict[str, Any], name: str) -> Any:
    record = fields.get(name)
    return record.get("value") if isinstance(record, dict) else record


def _coerce_location(value: dict[str, Any]) -> dict[str, Any] | None:
    location = value.get("location") if isinstance(value.get("location"), dict) else value
    lat = location.get("lat", location.get("latitude"))
    lng = location.get("lng", location.get("lon", location.get("longitude")))
    if lat is None or lng is None:
        return None
    return {
        "lat": float(lat),
        "lng": float(lng),
        "parcel_id": value.get("parcel_id") or location.get("parcel_id"),
        "address": value.get("address") or location.get("address"),
        "label": value.get("label") or value.get("display_name") or value.get("address"),
    }


class SiteSnapshotService:
    """Coordinates explicit resolution, quote, fetch, validation, and persistence."""

    def __init__(self, store: WorkspaceStore, client: MireyeClient):
        self.store = store
        self.client = client

    async def resolve(
        self,
        *,
        input: str | None = None,
        kind: str | None = None,
        lat: float | None = None,
        lng: float | None = None,
    ) -> dict:
        if (lat is None) != (lng is None):
            raise SandboxError("Latitude and longitude must be supplied together.")
        if lat is not None:
            return {
                "status": "resolved",
                "requires_selection": False,
                "candidates": [{"lat": float(lat), "lng": float(lng)}],
            }
        if not input:
            raise SandboxError("Provide an address, APN, or latitude/longitude pair.")

        try:
            lookup = await self.client.lookup(input=input, kind=kind, include_parcel=True)
        except httpx.HTTPError as exc:
            raise MireyeUnavailableError("MIREYE lookup is temporarily unavailable.") from exc
        raw_candidates = lookup.get("candidates") or lookup.get("results") or []
        if not raw_candidates and isinstance(lookup.get("candidate"), dict):
            raw_candidates = [lookup["candidate"]]
        if not raw_candidates and _coerce_location(lookup):
            raw_candidates = [lookup]
        candidates = [candidate for item in raw_candidates if (candidate := _coerce_location(item))]
        disposition = str(lookup.get("disposition", "")).lower()
        ambiguous = disposition in {"ambiguous", "clarify"} or len(candidates) > 1
        if ambiguous:
            return {
                "status": "ambiguous",
                "requires_selection": True,
                "candidates": candidates,
                "lookup": lookup,
            }
        if len(candidates) != 1:
            return {"status": "not_found", "requires_selection": False, "candidates": [], "lookup": lookup}
        return {
            "status": "resolved",
            "requires_selection": False,
            "candidates": candidates,
            "lookup": lookup,
        }

    async def quote(self, *, lat: float, lng: float) -> dict:
        try:
            fields, catalog = await self._field_selection()
            quote = await self.client.fetch_quote(locations=1, fields=fields)
        except httpx.HTTPError as exc:
            raise MireyeUnavailableError("MIREYE quote is temporarily unavailable.") from exc
        request = {"lat": float(lat), "lng": float(lng), "fields": fields}
        return {
            "location": {"lat": float(lat), "lng": float(lng)},
            "fields": fields,
            "request_hash": _hash(request),
            "field_catalog_version": self._catalog_version(catalog),
            "quote": quote,
        }

    async def create_snapshot(
        self,
        *,
        workspace_id: str,
        lat: float,
        lng: float,
        confirmed: bool,
    ) -> dict:
        if not confirmed:
            raise ConfirmationRequired("A quote must be explicitly confirmed before fetching MIREYE data.")

        try:
            fields, catalog = await self._field_selection()
            # Re-quote immediately before the paid fetch so the confirmed field list is exact.
            quote = await self.client.fetch_quote(locations=1, fields=fields)
            request = {"lat": float(lat), "lng": float(lng), "fields": fields}
            dossier = await self.client.fetch(lat=lat, lng=lng, fields=fields)
        except httpx.HTTPError as exc:
            raise MireyeUnavailableError("MIREYE fetch is temporarily unavailable.") from exc
        if dossier.get("ok") is False:
            message = dossier.get("error", {}).get("message", "MIREYE could not resolve this location.")
            raise SandboxError(message)

        observed_at = time.time()
        snapshot = self._build_snapshot(
            workspace_id=workspace_id,
            request=request,
            dossier=dossier,
            catalog=catalog,
            quote=quote,
            observed_at=observed_at,
        )
        self.store.create_site_snapshot(snapshot)
        return snapshot

    def get_snapshot(self, snapshot_id: str, *, now: float | None = None) -> dict | None:
        snapshot = self.store.get_site_snapshot(snapshot_id)
        if snapshot is not None:
            snapshot["is_expired"] = self.is_expired(snapshot, now=now)
        return snapshot

    def scene_state(self, snapshot_id: str) -> dict:
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            raise SandboxError("SiteSnapshot not found.")
        return scene_state_from_snapshot(snapshot)

    @staticmethod
    def is_expired(snapshot: dict, *, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= snapshot["expires_at"]

    async def _field_selection(self) -> tuple[list[str], dict]:
        catalog = await self.client.meta_fields()
        catalog_fields = {
            field.get("name"): field
            for field in catalog.get("fields", [])
            if isinstance(field, dict) and field.get("name")
        }
        missing = [name for name in SITE_SNAPSHOT_FIELDS if name not in catalog_fields]
        if missing:
            raise FieldCatalogError(f"Current MIREYE catalog lacks required SiteSnapshot fields: {', '.join(missing)}")
        return list(SITE_SNAPSHOT_FIELDS), catalog

    def _build_snapshot(
        self,
        *,
        workspace_id: str,
        request: dict,
        dossier: dict,
        catalog: dict,
        quote: dict,
        observed_at: float,
    ) -> dict:
        fields = dossier.get("fields")
        if not isinstance(fields, dict):
            raise SandboxError("MIREYE fetch response did not include field records.")
        identity = self._validate_identity(fields, request)
        geometry = self._geometry_from_fields(fields)
        catalog_fields = {field["name"]: field for field in catalog["fields"] if isinstance(field, dict) and field.get("name")}
        evidence = self._normalize_evidence(fields, catalog_fields, observed_at)
        expires_at = min(record["expires_at"] for record in evidence.values())

        return {
            "snapshot_id": f"site_{uuid.uuid4().hex}",
            "workspace_id": workspace_id,
            "parcel_identity": identity,
            "geometry": geometry,
            "evidence": evidence,
            "raw_response": dossier,
            "raw_response_hash": _hash(dossier),
            "request": request,
            "request_hash": _hash(request),
            "field_catalog_version": self._catalog_version(catalog),
            "provider_metadata": {
                "provider": "mireye",
                "mode": self.client.mode,
                "base_url": self.client.base_url,
                "mireye_snapshot_ts": dossier.get("snapshot_ts"),
                "quote": quote,
            },
            "observed_at": observed_at,
            "expires_at": expires_at,
            "created_at": observed_at,
        }

    @staticmethod
    def _catalog_version(catalog: dict) -> str:
        declared = catalog.get("version") or catalog.get("catalog_version")
        return str(declared) if declared else f"sha256:{_hash(catalog)}"

    @staticmethod
    def _geometry_from_fields(fields: dict[str, Any]) -> dict:
        raw_geometry = _field_value(fields, "parcel_boundary_geojson")
        if isinstance(raw_geometry, str):
            try:
                geometry = json.loads(raw_geometry)
            except json.JSONDecodeError as exc:
                raise ParcelIdentityError("MIREYE returned invalid parcel_boundary_geojson.") from exc
        elif isinstance(raw_geometry, dict):
            geometry = raw_geometry
        else:
            raise ParcelIdentityError("MIREYE did not return parcel boundary geometry.")
        return geometry

    @staticmethod
    def _validate_identity(fields: dict[str, Any], request: dict) -> dict:
        parcel_id = _field_value(fields, "parcel_id")
        match_type = _field_value(fields, "parcel_match_type")
        match_distance = _field_value(fields, "parcel_match_distance_m")
        if not parcel_id:
            raise ParcelIdentityError("MIREYE did not return a parcel_id.")
        if match_type != "exact_intersect":
            raise ParcelIdentityError("Site Sandbox accepts only exact_intersect parcel matches.")
        try:
            if match_distance is None or not math.isclose(float(match_distance), 0.0, abs_tol=1e-6):
                raise ParcelIdentityError("Exact parcel matches must have zero parcel_match_distance_m.")
        except (TypeError, ValueError) as exc:
            raise ParcelIdentityError("MIREYE returned an invalid parcel_match_distance_m.") from exc

        geometry = SiteSnapshotService._geometry_from_fields(fields)
        try:
            parcel = shape(geometry)
        except Exception as exc:
            raise ParcelIdentityError("MIREYE returned unparsable parcel geometry.") from exc
        if parcel.geom_type not in {"Polygon", "MultiPolygon"} or parcel.is_empty or not parcel.is_valid:
            raise ParcelIdentityError("MIREYE returned an invalid parcel Polygon/MultiPolygon.")
        if not parcel.covers(Point(request["lng"], request["lat"])):
            raise ParcelIdentityError("Selected point is not contained by the returned parcel geometry.")
        return {
            "parcel_id": str(parcel_id),
            "parcel_apn": _field_value(fields, "parcel_apn"),
            "parcel_address": _field_value(fields, "parcel_address"),
            "parcel_data_source": _field_value(fields, "parcel_data_source"),
            "parcel_match_type": match_type,
            "parcel_match_distance_m": float(match_distance),
            "parcel_match_radius_m": _field_value(fields, "parcel_match_radius_m"),
            "selected_point": {"lat": request["lat"], "lng": request["lng"]},
        }

    @staticmethod
    def _normalize_evidence(fields: dict[str, Any], catalog_fields: dict[str, dict], observed_at: float) -> dict[str, dict]:
        evidence = {}
        for name in SITE_SNAPSHOT_FIELDS:
            source_record = fields.get(name)
            record = source_record if isinstance(source_record, dict) else {"value": source_record}
            metadata = catalog_fields[name]
            ttl_seconds = int(metadata.get("ttl_seconds") or 0)
            evidence[name] = {
                "field": name,
                "value": record.get("value"),
                "status": record.get("status", "absent" if source_record is None else "ok"),
                "confidence": record.get("confidence"),
                "source": record.get("source") or metadata.get("source"),
                "unit": record.get("unit") or metadata.get("unit"),
                "lifecycle": metadata.get("lifecycle"),
                "ttl_seconds": ttl_seconds,
                "observed_at": observed_at,
                "expires_at": observed_at + ttl_seconds,
            }
        return evidence
