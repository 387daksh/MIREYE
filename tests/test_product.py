import asyncio
import json

from fastapi.testclient import TestClient

from app import main
from app.product import ProductExperienceService, compile_request
from app.sandbox import SITE_SNAPSHOT_FIELDS, SiteSnapshotService
from app.workspace.store import WorkspaceStore


class ProductMireyeClient:
    mode = "live"
    base_url = "https://api.mireye.com"

    def __init__(self):
        self.lookup_calls = 0
        self.fetch_calls = 0
        self.lookup_response = {"disposition": "exact_match", "candidates": [{"lat": 32.0, "lng": -97.0, "address": "1 Real Parcel Way"}]}

    async def lookup(self, **_kwargs):
        self.lookup_calls += 1
        return self.lookup_response

    async def meta_fields(self):
        return {"version": "product-test-v1", "fields": [{"name": name, "source": "MIREYE_TEST", "ttl_seconds": 3600} for name in SITE_SNAPSHOT_FIELDS]}

    async def fetch_quote(self, **_kwargs):
        return {"estimated_credits": 322}

    async def fetch(self, **_kwargs):
        self.fetch_calls += 1
        values = {name: None for name in SITE_SNAPSHOT_FIELDS}
        values.update({
            "parcel_id": "parcel-1", "parcel_apn": "APN-1", "parcel_address": "1 Real Parcel Way",
            "parcel_area_m2": 173205.0,
            "parcel_boundary_geojson": json.dumps({"type": "Polygon", "coordinates": [[[-97.01, 31.99], [-96.99, 31.99], [-96.99, 32.01], [-97.01, 32.01], [-97.01, 31.99]]]}),
            "parcel_data_source": "licensed_parcel_source", "parcel_match_type": "exact_intersect",
            "parcel_match_distance_m": 0.0, "parcel_match_radius_m": 0.0,
            "parcel_zoning": "I-2", "within_floodplain_polygon": False,
            "nearest_transmission_line_distance_m": 1800.0, "nearest_major_road_distance_m": 400.0,
        })
        return {"ok": True, "fields": {name: {"value": value, "status": "ok"} for name, value in values.items()}}


class NoWorlds:
    @staticmethod
    def latest_for_site_snapshot(_snapshot_id):
        return None


def _service(tmp_path):
    client = ProductMireyeClient()
    snapshots = SiteSnapshotService(WorkspaceStore(tmp_path / "product.db"), client)
    return ProductExperienceService(snapshots, NoWorlds()), client


def test_constraint_compiler_extracts_request_without_claiming_unsupported_semantics():
    compiled = compile_request("Find me a good site for a 100 MW data center in Texas, 20-50 acres, low flood risk, close to transmission and roads, with sufficient grid capacity.")

    assert compiled["capacity_mw"] == 100
    assert compiled["acreage"] == [20, 50]
    assert compiled["region"] == "Texas"
    assert {item["constraint_id"] for item in compiled["constraints"]} >= {
        "parcel_acreage_range", "parcel_outside_fema_sfha", "transmission_proximity", "legal_access", "sufficient_grid_capacity",
    }


def test_broad_discovery_is_honest_and_does_not_call_mireye(tmp_path):
    service, client = _service(tmp_path)
    result = asyncio.run(service.start("Find me a 100 MW data center site in Texas."))

    assert result["status"] == "DISCOVERY_UNAVAILABLE"
    assert result["stages"][1]["status"] == "unavailable"
    assert "specific property" in result["message"]
    assert client.lookup_calls == 0
    assert client.fetch_calls == 0


def test_specific_property_added_to_a_broad_request_uses_the_real_property_flow(tmp_path):
    service, client = _service(tmp_path)

    result = asyncio.run(service.start("Find a 100 MW data center site in Texas at 1 Real Parcel Way"))

    assert result["status"] == "CONFIRMATION_REQUIRED"
    assert client.lookup_calls == 1
    assert client.fetch_calls == 0


def test_specific_property_quotes_then_fetches_only_after_confirmation(tmp_path):
    service, client = _service(tmp_path)

    quoted = asyncio.run(service.start("Diligence 1 Real Parcel Way"))
    assert quoted["status"] == "CONFIRMATION_REQUIRED"
    assert quoted["confirmation"]["estimated_credits"] == 322
    assert client.fetch_calls == 0

    completed = asyncio.run(service.confirm(quoted["request_id"], True))
    assert completed["status"] == "COMPLETE"
    assert client.fetch_calls == 1
    candidate = completed["candidates"][0]
    assert candidate["area_acres"] == 42.8
    assert candidate["sandbox_url"].startswith("/sandbox/")


def test_ambiguous_property_requires_one_explicit_choice(tmp_path):
    service, client = _service(tmp_path)
    client.lookup_response = {
        "disposition": "clarify",
        "candidates": [
            {"lat": 32.0, "lng": -97.0, "address": "1 Main Street"},
            {"lat": 32.1, "lng": -97.1, "address": "10 Main Street"},
        ],
    }

    result = asyncio.run(service.start("Diligence property at 1 Main Street"))
    assert result["status"] == "CLARIFICATION_REQUIRED"
    assert [choice["label"] for choice in result["choices"]] == ["1 Main Street", "10 Main Street"]
    assert client.fetch_calls == 0


def test_product_api_and_primary_pages_use_product_language(tmp_path, monkeypatch):
    service, _client = _service(tmp_path)
    monkeypatch.setattr(main, "product_service", service)
    client = TestClient(main.app)

    response = client.post("/v1/product/requests", json={"message": "Find a site in Texas"})
    assert response.status_code == 200
    assert response.json()["status"] == "DISCOVERY_UNAVAILABLE"

    home = client.get("/").text
    sandbox = client.get("/sandbox/example").text
    assert "What are you looking for?" in home
    assert "/v1/screen" not in home
    assert "DuckDB" not in home
    assert "SiteSnapshot" not in home
    assert "Ask about this site" in sandbox
    assert "View sources" in sandbox
    assert "tool trace" not in sandbox.lower()
    assert "evidence id" not in sandbox.lower()
    assert "Analyze a real property" in home
    assert "propertyHandoff" in client.get("/static/app.js").text
