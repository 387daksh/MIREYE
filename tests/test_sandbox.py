import asyncio
import json

import pytest
import httpx

from app.mireye_client import MireyeClient
from app.sandbox import (
    ConfirmationRequired,
    ParcelIdentityError,
    MireyeUnavailableError,
    SandboxError,
    SITE_SNAPSHOT_FIELDS,
    SiteSnapshotService,
    deserialize_scene_state,
    serialize_scene_state,
)
from app.workspace.store import WorkspaceStore


def _run(coro):
    return asyncio.run(coro)


class FakeMireyeClient:
    mode = "live"
    base_url = "https://api.mireye.com"

    def __init__(self):
        self.lookup_response = {
            "disposition": "exact_match",
            "candidates": [{"lat": 32.0, "lng": -97.0, "parcel_id": "parcel-1"}],
        }
        self.fetch_calls = []
        self.quote_calls = []
        self.fields = self._fields()

    @staticmethod
    def _catalog():
        return {
            "version": "catalog-test-v1",
            "fields": [
                {
                    "name": name,
                    "source": "MIREYE_TEST",
                    "unit": None,
                    "lifecycle": "stable",
                    "ttl_seconds": 60,
                }
                for name in SITE_SNAPSHOT_FIELDS
            ],
        }

    @staticmethod
    def _fields():
        values = {
            "parcel_id": "parcel-1",
            "parcel_apn": "APN-1",
            "parcel_address": "1 Real Parcel Way",
            "parcel_area_m2": 10000.0,
            "parcel_boundary_geojson": json.dumps(
                {
                    "type": "Polygon",
                    "coordinates": [[[-97.01, 31.99], [-96.99, 31.99], [-96.99, 32.01], [-97.01, 32.01], [-97.01, 31.99]]],
                }
            ),
            "parcel_data_source": "regrid_paid",
            "parcel_match_type": "exact_intersect",
            "parcel_match_distance_m": 0.0,
            "parcel_match_radius_m": 0.0,
            "parcel_zoning": "IND",
            "elevation": 200.0,
            "slope_degrees": 2.0,
            "fema_flood_zone": "X",
            "within_floodplain_polygon": False,
            "wetland_acres_on_parcel": 0.0,
            "wetland_fraction_of_parcel": 0.0,
            "nearest_substation_distance_m": 1000.0,
            "nearest_substation_max_voltage_kv": 345.0,
            "nearest_substation_status": "IN SERVICE",
            "substations_within_radius_count": 2,
            "nearest_transmission_line_distance_m": 500.0,
            "nearest_transmission_line_voltage_kv": 345.0,
            "nearest_transmission_line_voltage_class": "345",
            "nearest_transmission_line_status": "IN SERVICE",
            "transmission_lines_within_radius_count": 2,
            "nearest_major_road_name": "State Road",
            "nearest_major_road_distance_m": 150.0,
            "nearest_major_road_class": "primary",
            "fiber_broadband_available": True,
            "fiber_provider_count": 2,
            "within_water_service_area": True,
            "water_system_name": "City Water",
            "within_sewer_service_area": True,
            "sewer_service_area_provider": "City Sewer",
        }
        return {name: {"value": values[name], "status": "ok", "confidence": "high"} for name in SITE_SNAPSHOT_FIELDS}

    async def lookup(self, **_kwargs):
        return self.lookup_response

    async def meta_fields(self):
        return self._catalog()

    async def fetch_quote(self, *, locations, fields, preset=None):
        self.quote_calls.append({"locations": locations, "fields": fields, "preset": preset})
        return {"estimated_credits": 317, "locations": locations, "fields": fields}

    async def fetch(self, *, lat, lng, fields, preset=None):
        self.fetch_calls.append({"lat": lat, "lng": lng, "fields": fields, "preset": preset})
        return {"ok": True, "fields": self.fields, "snapshot_ts": "2026-08-20T00:00:00Z"}


@pytest.fixture
def sandbox(tmp_path):
    client = FakeMireyeClient()
    store = WorkspaceStore(db_path=tmp_path / "sandbox.db")
    return SiteSnapshotService(store=store, client=client), client, store


def test_lookup_uses_current_live_payload(monkeypatch):
    client = MireyeClient(api_key="test-key", mode="live")
    captured = {}

    async def fake_request(method, path, json_body=None, params=None):
        captured.update(method=method, path=path, json_body=json_body, params=params)
        return {"disposition": "exact_match"}

    monkeypatch.setattr(client, "_request", fake_request)
    _run(client.lookup("APN-1", kind="apn", include_parcel=False))

    assert captured == {
        "method": "POST",
        "path": "/v1/lookup",
        "json_body": {"input": "APN-1", "kind": "apn", "include_parcel": False},
        "params": None,
    }


def test_quote_uses_current_live_payload_for_one_exact_field_list(monkeypatch):
    client = MireyeClient(api_key="test-key", mode="live")
    captured = {}

    async def fake_request(method, path, json_body=None, params=None):
        captured.update(method=method, path=path, json_body=json_body, params=params)
        return {"estimated_credits": 317}

    monkeypatch.setattr(client, "_request", fake_request)
    _run(client.fetch_quote(locations=1, fields=["parcel_id", "elevation"]))

    assert captured == {
        "method": "POST",
        "path": "/v1/fetch/quote",
        "json_body": {"locations": 1, "fields": ["parcel_id", "elevation"]},
        "params": None,
    }

    _run(client.fetch_quote(locations=[{"lat": 32.0, "lng": -97.0}], fields=["parcel_id"]))
    assert captured["json_body"] == {"locations": 1, "fields": ["parcel_id"]}


def test_ambiguous_lookup_requires_explicit_selection(sandbox):
    service, client, _store = sandbox
    client.lookup_response = {
        "disposition": "clarify",
        "candidates": [{"lat": 32.0, "lng": -97.0}, {"lat": 33.0, "lng": -96.0}],
    }

    result = _run(service.resolve(input="Main Street", kind="address"))

    assert result["status"] == "ambiguous"
    assert result["requires_selection"] is True
    assert len(result["candidates"]) == 2
    assert client.fetch_calls == []


def test_exact_match_creates_immutable_snapshot_after_confirmation(sandbox):
    service, client, store = sandbox

    with pytest.raises(ConfirmationRequired):
        _run(service.create_snapshot(workspace_id="ws-1", lat=32.0, lng=-97.0, confirmed=False))
    assert client.fetch_calls == []

    snapshot = _run(service.create_snapshot(workspace_id="ws-1", lat=32.0, lng=-97.0, confirmed=True))
    stored = store.get_site_snapshot(snapshot["snapshot_id"])

    assert snapshot["parcel_identity"]["parcel_id"] == "parcel-1"
    assert snapshot["parcel_identity"]["parcel_match_type"] == "exact_intersect"
    assert snapshot["geometry"]["type"] == "Polygon"
    assert snapshot["evidence"]["parcel_boundary_geojson"]["ttl_seconds"] == 60
    assert client.quote_calls[-1]["locations"] == 1
    assert client.fetch_calls[-1]["fields"] == list(SITE_SNAPSHOT_FIELDS)
    assert stored["raw_response_hash"] == snapshot["raw_response_hash"]
    assert stored["request_hash"] == snapshot["request_hash"]


def test_nearest_parcel_fallback_is_rejected(sandbox):
    service, client, _store = sandbox
    client.fields["parcel_match_type"]["value"] = "nearest_within_radius"
    client.fields["parcel_match_distance_m"]["value"] = 12.0

    with pytest.raises(ParcelIdentityError, match="exact_intersect"):
        _run(service.create_snapshot(workspace_id="ws-1", lat=32.0, lng=-97.0, confirmed=True))


def test_snapshot_hashes_are_stable_and_ttl_expiry_is_detected(sandbox):
    service, _client, _store = sandbox

    first = _run(service.create_snapshot(workspace_id="ws-1", lat=32.0, lng=-97.0, confirmed=True))
    second = _run(service.create_snapshot(workspace_id="ws-1", lat=32.0, lng=-97.0, confirmed=True))
    loaded = service.get_snapshot(first["snapshot_id"], now=first["expires_at"])

    assert first["raw_response_hash"] == second["raw_response_hash"]
    assert first["request_hash"] == second["request_hash"]
    assert loaded["is_expired"] is True


def test_live_transport_failure_is_explicit(sandbox):
    service, client, _store = sandbox

    async def unavailable(**_kwargs):
        raise httpx.ConnectError("blocked")

    client.lookup = unavailable
    with pytest.raises(MireyeUnavailableError, match="temporarily unavailable"):
        _run(service.resolve(input="1600 Pennsylvania Avenue NW", kind="address"))


def test_scene_loads_observed_parcel_geometry(sandbox):
    service, _client, _store = sandbox
    snapshot = _run(service.create_snapshot(workspace_id="ws-1", lat=32.0, lng=-97.0, confirmed=True))

    scene = service.scene_state(snapshot["snapshot_id"])
    boundary = next(item for item in scene["observed"] if item["kind"] == "parcel_boundary")
    resolution_point = next(item for item in scene["observed"] if item["kind"] == "resolution_point")

    assert scene["site_snapshot_id"] == snapshot["snapshot_id"]
    assert boundary["geometry"] == snapshot["geometry"]
    assert resolution_point["geometry"]["coordinates"] == [-97.0, 32.0]
    assert scene["proposed"][0]["attributes"]["capacity_mw"] == 100


def test_proposed_scene_state_does_not_mutate_snapshot(sandbox):
    service, _client, store = sandbox
    snapshot = _run(service.create_snapshot(workspace_id="ws-1", lat=32.0, lng=-97.0, confirmed=True))
    scene = service.scene_state(snapshot["snapshot_id"])

    scene["proposed"][0]["geometry_local"]["width_m"] = 500
    stored = store.get_site_snapshot(snapshot["snapshot_id"])

    assert "proposed" not in stored
    assert stored["geometry"] == snapshot["geometry"]
    assert stored["raw_response_hash"] == snapshot["raw_response_hash"]


def test_scene_state_is_deterministic_and_missing_snapshot_is_explicit(sandbox):
    service, _client, _store = sandbox
    snapshot = _run(service.create_snapshot(workspace_id="ws-1", lat=32.0, lng=-97.0, confirmed=True))
    scene = service.scene_state(snapshot["snapshot_id"])

    serialized = serialize_scene_state(scene)
    assert serialize_scene_state(deserialize_scene_state(serialized)) == serialized

    with pytest.raises(SandboxError, match="SiteSnapshot not found"):
        service.scene_state("missing-snapshot")
