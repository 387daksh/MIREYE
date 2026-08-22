"""Real-parcel SiteSnapshot acquisition for the Site Sandbox."""
from __future__ import annotations

import copy
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

SITE_SNAPSHOT_FIELD_SCOPES = {
    "parcel_area_m2": "PARCEL",
    "parcel_zoning": "PARCEL",
    "slope_degrees": "POINT",
    "fema_flood_zone": "POINT",
    "within_floodplain_polygon": "POINT",
    "wetland_acres_on_parcel": "PARCEL",
    "wetland_fraction_of_parcel": "PARCEL",
    "nearest_substation_distance_m": "NEAREST_FEATURE",
    "nearest_substation_max_voltage_kv": "NEAREST_FEATURE",
    "nearest_substation_status": "NEAREST_FEATURE",
    "nearest_transmission_line_distance_m": "NEAREST_FEATURE",
    "nearest_transmission_line_voltage_kv": "NEAREST_FEATURE",
    "nearest_transmission_line_status": "NEAREST_FEATURE",
    "nearest_major_road_distance_m": "NEAREST_FEATURE",
}

REFRESH_IDENTITY_FIELDS = (
    "parcel_id",
    "parcel_boundary_geojson",
    "parcel_match_type",
    "parcel_match_distance_m",
)
REFRESH_CONFIRMATION_TTL_SECONDS = 900

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

    def __init__(self, store: WorkspaceStore, client: MireyeClient, scenarios: Any | None = None):
        self.store = store
        self.client = client
        self.scenarios = scenarios

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

    async def select_fields(self, fields: list[str]) -> tuple[list[str], dict]:
        """Validate a caller-planned field subset against the current MIREYE catalog."""
        return await self._catalog_selection(fields)

    def persist_dossier(
        self,
        *,
        workspace_id: str,
        lat: float,
        lng: float,
        fields: list[str],
        catalog: dict,
        quote: dict,
        dossier: dict,
        observed_at: float | None = None,
    ) -> dict:
        """Normalize one already-fetched batch result through normal snapshot identity checks."""
        if dossier.get("ok") is False:
            error = dossier.get("error") or {}
            message = error.get("message") or error.get("code") or "MIREYE batch enrichment failed without a provider error message."
            raise SandboxError(message)
        timestamp = time.time() if observed_at is None else float(observed_at)
        request = {"lat": float(lat), "lng": float(lng), "fields": list(fields)}
        snapshot = self._build_snapshot(
            workspace_id=workspace_id,
            request=request,
            dossier=dossier,
            catalog=catalog,
            quote=quote,
            observed_at=timestamp,
            selected_fields=fields,
        )
        self.store.create_site_snapshot(snapshot)
        return snapshot

    def freshness_status(
        self,
        snapshot_id: str,
        *,
        now: float | None = None,
        test_expiry_overrides: dict[str, float] | None = None,
        fields: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        """Classify persisted evidence without changing its provider TTL metadata.

        ``test_expiry_overrides`` exists only for explicit integration/demo refresh
        exercises. It is in-memory, caller-supplied, and never persisted on T1.
        """
        snapshot = self.store.get_site_snapshot(snapshot_id)
        if snapshot is None:
            raise SandboxError("SiteSnapshot not found.")
        current_time = time.time() if now is None else float(now)
        records = []
        checked_fields = list(fields) if fields is not None else list(snapshot.get("evidence", {}))
        for field in checked_fields:
            record = snapshot.get("evidence", {}).get(field)
            override = (test_expiry_overrides or {}).get(field)
            classification, reason = self._freshness_classification(
                field, record, current_time, test_expires_at=override,
            )
            records.append({"field": field, "classification": classification, "reason": reason})
        grouped = {
            classification: [item["field"] for item in records if item["classification"] == classification]
            for classification in ("fresh", "stale", "missing", "incompatible", "deprecated")
        }
        refresh_fields = sorted({item["field"] for item in records if item["classification"] != "fresh"})
        return {
            "snapshot_id": snapshot_id,
            "site_id": snapshot.get("site_id"),
            "checked_at": current_time,
            "status": "CURRENT" if not refresh_fields else "STALE_EVIDENCE",
            "fields": records,
            "fresh_fields": grouped["fresh"],
            "stale_fields": grouped["stale"],
            "missing_fields": grouped["missing"],
            "incompatible_fields": grouped["incompatible"],
            "deprecated_fields": grouped["deprecated"],
            "refresh_fields": refresh_fields,
            "refresh_required": bool(refresh_fields),
        }

    async def quote_refresh(
        self,
        snapshot_id: str,
        *,
        now: float | None = None,
        test_expiry_overrides: dict[str, float] | None = None,
        fields: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        snapshot = self.store.get_site_snapshot(snapshot_id)
        if snapshot is None:
            raise SandboxError("SiteSnapshot not found.")
        freshness = self.freshness_status(
            snapshot_id, now=now, test_expiry_overrides=test_expiry_overrides, fields=fields,
        )
        if not freshness["refresh_required"]:
            return {
                "status": "NO_REFRESH_REQUIRED",
                "snapshot_id": snapshot_id,
                "site_id": snapshot.get("site_id"),
                "freshness": freshness,
                "expected_credits": 0,
                "confirmation_required": False,
            }

        fields = sorted(set(freshness["refresh_fields"]) | set(REFRESH_IDENTITY_FIELDS))
        try:
            _selected_fields, catalog = await self._catalog_selection(fields)
            quote = await self.client.fetch_quote(locations=1, fields=fields)
        except httpx.HTTPError as exc:
            raise MireyeUnavailableError("MIREYE refresh quote is temporarily unavailable.") from exc
        created_at = time.time() if now is None else float(now)
        dependencies = self.store.affected_scenario_constraints(snapshot.get("site_id"), fields) if snapshot.get("site_id") else []
        affected_scenarios: dict[tuple[str, int], set[str]] = {}
        for dependency in dependencies:
            affected_scenarios.setdefault((dependency["scenario_id"], dependency["revision"]), set()).add(dependency["constraint_id"])
        provider_expiry = quote.get("expires_at") or quote.get("quote_expires_at")
        try:
            quote_expires_at = float(provider_expiry) if provider_expiry is not None else created_at + REFRESH_CONFIRMATION_TTL_SECONDS
            expiry_source = "mireye" if provider_expiry is not None else "application_confirmation_ttl"
        except (TypeError, ValueError):
            quote_expires_at, expiry_source = created_at + REFRESH_CONFIRMATION_TTL_SECONDS, "application_confirmation_ttl"
        spend_plan_id = f"spend_{uuid.uuid4().hex}"
        mireye_quote_id = quote.get("quote_id") or quote.get("id")
        spend_plan = {
            "spend_plan_id": spend_plan_id,
            "workspace_id": snapshot["workspace_id"],
            "site_id": snapshot.get("site_id"),
            "snapshot_id": snapshot_id,
            "status": "QUOTED",
            "requested_fields": fields,
            "candidate_site_count": 1,
            "batch_strategy": "single_location",
            "cache_hits": {"fresh_field_count": len(freshness["fresh_fields"]), "fresh_fields": freshness["fresh_fields"]},
            "freshness_reason": {item["field"]: item["classification"] for item in freshness["fields"] if item["classification"] != "fresh"},
            "field_catalog_version": self._catalog_version(catalog),
            "quote": quote,
            "quote_id": mireye_quote_id or spend_plan_id,
            "mireye_quote_id": mireye_quote_id,
            "quote_expires_at": quote_expires_at,
            "quote_expiry_source": expiry_source,
            "expected_credits": self._estimated_credits(quote),
            "workspace_budget_impact": {
                "policy": "explicit_application_confirmation_required",
                "estimated_credits": self._estimated_credits(quote),
                "remaining_budget": None,
            },
            "freshness": freshness,
            "test_freshness_override_fields": sorted((test_expiry_overrides or {}).keys()),
            "affected_scenarios": [
                {
                    "scenario_id": scenario_id,
                    "revision": revision,
                    "status": "STALE_EVIDENCE",
                    "affected_constraint_ids": sorted(constraint_ids),
                }
                for (scenario_id, revision), constraint_ids in sorted(affected_scenarios.items())
            ],
            "created_at": created_at,
            "confirmed_at": None,
            "completed_at": None,
        }
        self.store.create_mireye_spend_plan(spend_plan)
        return spend_plan

    async def confirm_and_refresh(self, spend_plan_id: str, *, confirmed_by_application: bool) -> dict:
        plan = self.store.get_mireye_spend_plan(spend_plan_id)
        if plan is None:
            raise SandboxError("MIREYE refresh spend plan was not found.")
        if not confirmed_by_application:
            raise ConfirmationRequired("MIREYE refresh requires explicit application confirmation.")
        if plan["status"] != "QUOTED":
            raise SandboxError("MIREYE refresh spend plan is no longer available for confirmation.")
        if time.time() >= float(plan["quote_expires_at"]):
            self.store.update_mireye_spend_plan(spend_plan_id, status="EXPIRED")
            raise ConfirmationRequired("MIREYE refresh quote expired. Request a new quote before confirming.")

        previous = self.store.get_site_snapshot(plan["snapshot_id"])
        if previous is None:
            raise SandboxError("SiteSnapshot not found.")
        self.store.update_mireye_spend_plan(spend_plan_id, status="CONFIRMED")
        fields = list(plan["requested_fields"])
        try:
            _selected_fields, catalog = await self._catalog_selection(fields)
            point = previous["parcel_identity"]["selected_point"]
            request = {"lat": float(point["lat"]), "lng": float(point["lng"]), "fields": fields}
            dossier = await self.client.fetch(lat=request["lat"], lng=request["lng"], fields=fields)
        except httpx.HTTPError as exc:
            raise MireyeUnavailableError("MIREYE refresh fetch is temporarily unavailable.") from exc
        if dossier.get("ok") is False:
            message = dossier.get("error", {}).get("message", "MIREYE could not refresh this location.")
            raise SandboxError(message)

        refreshed = self._build_refreshed_snapshot(
            previous=previous, request=request, dossier=dossier, catalog=catalog,
            spend_plan=plan, observed_at=time.time(),
        )
        if refreshed["parcel_identity"]["parcel_id"] != previous["parcel_identity"]["parcel_id"]:
            self.store.update_mireye_spend_plan(spend_plan_id, status="IDENTITY_MISMATCH")
            raise ParcelIdentityError("MIREYE refresh resolved a different parcel_id; refresh stopped without creating a new SiteSnapshot.")
        self.store.create_site_snapshot(refreshed)
        diff = self.snapshot_diff(previous, refreshed)
        evaluation_runs = []
        if self.scenarios is not None:
            evaluation_runs = self.scenarios.revalidate_after_refresh(previous, refreshed, diff)
        completed_at = time.time()
        self.store.update_mireye_spend_plan(spend_plan_id, status="COMPLETED", completed_at=completed_at)
        completed_plan = self.store.get_mireye_spend_plan(spend_plan_id)
        return {
            "status": "REFRESHED",
            "spend_plan": completed_plan,
            "previous_snapshot_id": previous["snapshot_id"],
            "snapshot": refreshed,
            "snapshot_diff": diff,
            "evaluation_runs": evaluation_runs,
        }

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
        return await self._catalog_selection(list(SITE_SNAPSHOT_FIELDS))

    async def _catalog_selection(self, fields: list[str]) -> tuple[list[str], dict]:
        catalog = await self.client.meta_fields()
        catalog_fields = {
            field.get("name"): field
            for field in catalog.get("fields", [])
            if isinstance(field, dict) and field.get("name")
        }
        missing = [name for name in fields if name not in catalog_fields]
        if missing:
            raise FieldCatalogError(f"Current MIREYE catalog lacks required SiteSnapshot fields: {', '.join(missing)}")
        return list(fields), catalog

    def _build_snapshot(
        self,
        *,
        workspace_id: str,
        request: dict,
        dossier: dict,
        catalog: dict,
        quote: dict,
        observed_at: float,
        selected_fields: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        fields = dossier.get("fields")
        if not isinstance(fields, dict):
            raise SandboxError("MIREYE fetch response did not include field records.")
        identity = self._validate_identity(fields, request)
        geometry = self._geometry_from_fields(fields)
        catalog_fields = {field["name"]: field for field in catalog["fields"] if isinstance(field, dict) and field.get("name")}
        evidence = self._normalize_evidence(fields, catalog_fields, observed_at, selected_fields=selected_fields)
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

    def _build_refreshed_snapshot(
        self,
        *,
        previous: dict,
        request: dict,
        dossier: dict,
        catalog: dict,
        spend_plan: dict,
        observed_at: float,
    ) -> dict:
        fields = dossier.get("fields")
        if not isinstance(fields, dict):
            raise SandboxError("MIREYE refresh response did not include field records.")
        catalog_fields = {
            field["name"] for field in catalog.get("fields", [])
            if isinstance(field, dict) and field.get("name")
        }
        refreshed_evidence = self._normalize_evidence(
            fields,
            {field["name"]: field for field in catalog["fields"] if isinstance(field, dict) and field.get("name")},
            observed_at,
            selected_fields=spend_plan["requested_fields"],
        )
        evidence = copy.deepcopy(previous["evidence"])
        for name, record in evidence.items():
            if name not in refreshed_evidence:
                record["carried_from_snapshot_id"] = record.get("carried_from_snapshot_id") or previous["snapshot_id"]
        evidence.update(refreshed_evidence)
        missing_catalog_fields = [name for name in spend_plan["requested_fields"] if name not in catalog_fields]
        if missing_catalog_fields:
            raise FieldCatalogError(f"Current MIREYE catalog lacks refresh fields: {', '.join(missing_catalog_fields)}")
        identity = self._validate_identity(evidence, request)
        geometry = self._geometry_from_fields(evidence)
        expires_at = min(record["expires_at"] for record in evidence.values())
        return {
            "snapshot_id": f"site_{uuid.uuid4().hex}",
            "site_id": previous.get("site_id"),
            "workspace_id": previous["workspace_id"],
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
                "refresh": {
                    "previous_snapshot_id": previous["snapshot_id"],
                    "spend_plan_id": spend_plan["spend_plan_id"],
                    "quote": spend_plan["quote"],
                    "requested_fields": spend_plan["requested_fields"],
                },
            },
            "observed_at": observed_at,
            "expires_at": expires_at,
            "created_at": observed_at,
        }

    @staticmethod
    def _freshness_classification(
        field: str,
        record: Any,
        now: float,
        *,
        test_expires_at: float | None = None,
    ) -> tuple[str, str]:
        if not isinstance(record, dict):
            return "missing", "Evidence record is missing."
        if str(record.get("lifecycle", "")).lower() == "deprecated":
            return "deprecated", "Field is deprecated in its captured catalog metadata."
        if record.get("value") is None:
            return "missing", "Evidence value is null or absent."
        if record.get("status") not in {"ok", None}:
            return "incompatible", f"Evidence status is not usable: {record.get('status')}."
        expected_scope = SITE_SNAPSHOT_FIELD_SCOPES.get(field)
        if expected_scope and record.get("scope") not in {expected_scope, None}:
            return "incompatible", f"Evidence scope is {record.get('scope')}, not {expected_scope}."
        try:
            expires_at = float(test_expires_at if test_expires_at is not None else record["expires_at"])
        except (KeyError, TypeError, ValueError):
            return "incompatible", "Evidence has invalid freshness metadata."
        if now >= expires_at:
            return "stale", "Test-time local expiration override has expired." if test_expires_at is not None else "Evidence TTL has expired."
        return "fresh", "Evidence is usable and within its field-level TTL."

    @staticmethod
    def _estimated_credits(quote: dict) -> float | int | None:
        for key in ("estimated_credits", "credits", "total_credits", "credits_total", "credits_per_location"):
            value = quote.get(key)
            if isinstance(value, (int, float)):
                return value
        estimate = quote.get("estimate")
        if isinstance(estimate, dict):
            return SiteSnapshotService._estimated_credits(estimate)
        return None

    @staticmethod
    def snapshot_diff(previous: dict, refreshed: dict) -> dict:
        identity_fields = (
            "parcel_id", "parcel_apn", "parcel_address", "parcel_data_source",
            "parcel_match_type", "parcel_match_distance_m", "parcel_match_radius_m",
        )
        identity_changes = {
            field: {"before": previous["parcel_identity"].get(field), "after": refreshed["parcel_identity"].get(field)}
            for field in identity_fields
            if previous["parcel_identity"].get(field) != refreshed["parcel_identity"].get(field)
        }
        previous_evidence, refreshed_evidence = previous.get("evidence", {}), refreshed.get("evidence", {})
        field_changes = {}
        changed_evidence_ids = []
        for field in sorted(set(previous_evidence) | set(refreshed_evidence)):
            before, after = previous_evidence.get(field), refreshed_evidence.get(field)
            before_hash = SiteSnapshotService._evidence_hash(before) if before is not None else None
            after_hash = SiteSnapshotService._evidence_hash(after) if after is not None else None
            changed = before_hash != after_hash
            if changed:
                changed_evidence_ids.append(field)
                field_changes[field] = {
                    "value": {"before": before.get("value") if isinstance(before, dict) else None, "after": after.get("value") if isinstance(after, dict) else None},
                    "status": {"before": before.get("status") if isinstance(before, dict) else None, "after": after.get("status") if isinstance(after, dict) else None},
                    "scope": {"before": before.get("scope") if isinstance(before, dict) else None, "after": after.get("scope") if isinstance(after, dict) else None},
                    "freshness": {"before": before.get("expires_at") if isinstance(before, dict) else None, "after": after.get("expires_at") if isinstance(after, dict) else None},
                    "evidence_hash": {"before": before_hash, "after": after_hash},
                }
        previous_geometry_hash, refreshed_geometry_hash = _hash(previous["geometry"]), _hash(refreshed["geometry"])
        return {
            "diff_version": "site_snapshot_diff_v1",
            "previous_snapshot_id": previous["snapshot_id"],
            "refreshed_snapshot_id": refreshed["snapshot_id"],
            "identity_changed": bool(identity_changes),
            "identity_changes": identity_changes,
            "geometry_changed": previous_geometry_hash != refreshed_geometry_hash,
            "geometry_hash": {"before": previous_geometry_hash, "after": refreshed_geometry_hash},
            "field_changes": field_changes,
            "changed_evidence_ids": changed_evidence_ids,
        }

    @staticmethod
    def _evidence_hash(record: dict) -> str:
        declared = record.get("evidence_hash")
        if isinstance(declared, str) and declared:
            return declared
        return _hash({key: value for key, value in record.items() if key not in {"carried_from_snapshot_id", "evidence_hash"}})

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
    def _normalize_evidence(
        source_fields: dict[str, Any], catalog_fields: dict[str, dict], observed_at: float, *, selected_fields: list[str] | tuple[str, ...] | None = None
    ) -> dict[str, dict]:
        evidence = {}
        for name in selected_fields or SITE_SNAPSHOT_FIELDS:
            source_record = source_fields.get(name)
            record = source_record if isinstance(source_record, dict) else {"value": source_record}
            metadata = catalog_fields[name]
            ttl_seconds = int(metadata.get("ttl_seconds") or 0)
            normalized = {
                "field": name,
                "value": record.get("value"),
                "status": record.get("status", "absent" if source_record is None else "ok"),
                "confidence": record.get("confidence"),
                "source": record.get("source") or metadata.get("source"),
                "unit": record.get("unit") or metadata.get("unit"),
                "lifecycle": metadata.get("lifecycle"),
                "scope": metadata.get("scope") or metadata.get("spatial_scope") or SITE_SNAPSHOT_FIELD_SCOPES.get(name),
                "ttl_seconds": ttl_seconds,
                "observed_at": observed_at,
                "expires_at": observed_at + ttl_seconds,
            }
            normalized["evidence_hash"] = _hash(normalized)
            evidence[name] = normalized
        return evidence
