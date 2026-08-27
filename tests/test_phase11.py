import asyncio

from app.diligence import DiligenceService
from app.sandbox import SiteSnapshotService
from app.workspace.store import WorkspaceStore
from tests.test_diligence import FakeMireye, FakeWorlds, _phase10_enrich
from tests.test_sandbox import FakeMireyeClient
from tests.test_world import FixturePolygonProvider, world_service


def run(coro):
    return asyncio.run(coro)


def expanded_catalog(client):
    catalog = client._catalog()
    additions = {
        "nearest_osm_transmission_line_distance_m": ("OPENINFRAMAP_OSM", "meters", 2_592_000),
        "nearest_osm_transmission_line_voltage_kv": ("OPENINFRAMAP_OSM", "kV", 2_592_000),
        "interconnection_queue_active_capacity_county_mw": ("LBNL_QUEUED_UP", "MW", 2_592_000),
        "transmission_redundancy_flag": ("MIREYE_DERIVED_SITING", None, 2_592_000),
        "within_water_service_area": ("EPA_CWS_SERVICE_AREAS", None, 2_592_000),
        "water_service_area_provenance": ("EPA_CWS_SERVICE_AREAS", None, 2_592_000),
        "primary_building_overture_class": ("OVERTURE_BUILDINGS", None, 2_592_000),
        "parcel_owner": ("REGRID", None, 86_400),
    }
    existing = {item["name"] for item in catalog["fields"]}
    for name, (source, unit, ttl) in additions.items():
        if name not in existing:
            catalog["fields"].append({
                "name": name, "source": source, "source_url": f"https://example.test/{source.lower()}",
                "unit": unit, "ttl_seconds": ttl, "lifecycle": "stable",
                "billing": {"credits_per_location": 1, "metered_group": None},
            })
    catalog["presets"] = {"data_center_siting": [
        "nearest_osm_transmission_line_distance_m", "nearest_osm_transmission_line_voltage_kv",
        "interconnection_queue_active_capacity_county_mw", "transmission_redundancy_flag",
        "within_water_service_area",
    ]}
    return catalog


def test_phase11_catalog_plan_is_relevant_deduplicated_freshness_aware_and_offline(tmp_path):
    client = FakeMireyeClient()
    store = WorkspaceStore(tmp_path / "phase11-plan.db")
    service = SiteSnapshotService(store, client)
    snapshot = run(service.create_snapshot(workspace_id="ws-phase11", lat=32.0, lng=-97.0, confirmed=True))
    catalog = expanded_catalog(client)
    client.meta_fields = lambda: asyncio.sleep(0, result=catalog)
    quote_count, fetch_count = len(client.quote_calls), len(client.fetch_calls)

    plan = run(service.catalog_evidence_plan(
        project_type="Data center",
        requirements=[
            {"constraint_id": "sufficient_grid_capacity"},
            {"constraint_id": "resolution_point_outside_fema_sfha"},
            {"constraint_id": "water_capacity"},
        ],
        unresolved_gaps=[{"requirement_id": "sufficient_grid_capacity"}],
        requested_decision="site_diligence",
        snapshot_id=snapshot["snapshot_id"],
    ))

    assert plan["selected_presets"] == ["data_center_siting"]
    assert len(plan["fields"]) == len(set(plan["fields"]))
    assert {"parcel_owner", "parcel_boundary_geojson", "nearest_osm_transmission_line_distance_m", "within_water_service_area"} <= set(plan["fields"])
    assert "parcel_id" not in plan["refresh_fields"]
    assert "nearest_osm_transmission_line_distance_m" in plan["refresh_fields"]
    grid = next(item for item in plan["field_manifest"] if item["field"] == "nearest_osm_transmission_line_distance_m")
    parcel = next(item for item in plan["field_manifest"] if item["field"] == "parcel_area_m2")
    assert grid["spatial_scope"] == "NEAREST_FEATURE"
    assert parcel["spatial_scope"] == "PARCEL"
    assert grid["metered"] is True and grid["reason"] and grid["requirement_ids"]
    assert len(client.quote_calls) == quote_count and len(client.fetch_calls) == fetch_count


def test_phase11_semantic_strength_never_upgrades_signals_or_missing_values():
    metadata = {
        "parcel_zoning": {"source": "REGRID", "ttl_seconds": 60},
        "within_floodplain_polygon": {"source": "FEMA_NFHL", "ttl_seconds": 60},
        "nearest_osm_transmission_line_distance_m": {"source": "OPENINFRAMAP_OSM", "unit": "meters", "ttl_seconds": 60},
        "transmission_redundancy_flag": {"source": "MIREYE_DERIVED_SITING", "ttl_seconds": 60},
        "water_system_name": {"source": "EPA_CWS_SERVICE_AREAS", "ttl_seconds": 60},
    }
    normalized = SiteSnapshotService._normalize_evidence({
        "parcel_zoning": {"value": "I-2", "status": "ok"},
        "within_floodplain_polygon": {"value": False, "status": "ok"},
        "nearest_osm_transmission_line_distance_m": {"value": 900.0, "status": "ok"},
        "transmission_redundancy_flag": {"value": True, "status": "ok"},
        "water_system_name": {"value": None, "status": "absent"},
    }, metadata, 100.0, selected_fields=list(metadata))

    assert normalized["parcel_zoning"]["semantic_strength"] == "DIRECTLY_VERIFIED"
    assert normalized["within_floodplain_polygon"]["semantic_class"] == "POINT_SCOPED_SIGNAL"
    assert normalized["nearest_osm_transmission_line_distance_m"]["semantic_strength"] == "SOURCE_BACKED_SIGNAL"
    assert "do not prove available or deliverable capacity" in normalized["nearest_osm_transmission_line_distance_m"]["claim_limits"][0]
    assert normalized["transmission_redundancy_flag"]["semantic_strength"] == "DERIVED"
    assert normalized["water_system_name"]["semantic_strength"] == "INSUFFICIENT_EVIDENCE"


def test_phase11_project_intelligence_keeps_water_fiber_and_grid_as_partial_signals(tmp_path):
    class ContextMireye(FakeMireye):
        @staticmethod
        def _dossier(location, fields):
            dossier = FakeMireye._dossier(location, fields)
            values = {
                "within_water_service_area": True, "water_system_name": "Test Water",
                "water_service_area_provenance": "EPA service-area map",
                "fiber_broadband_available": True, "fiber_provider_count": 3,
            }
            for name, value in values.items():
                if name in dossier.get("fields", {}):
                    dossier["fields"][name] = {"value": value, "status": "ok"}
            return dossier

    client = ContextMireye()
    store = WorkspaceStore(tmp_path / "phase11-project.db")
    service = DiligenceService(store, SiteSnapshotService(store, client), FakeWorlds())
    project = _phase10_enrich(
        service,
        "Evaluate a 100 MW data center, 20-50 acres, with sufficient grid capacity, water capacity, and fiber diversity. Use reasonable assumptions.",
        "ws-phase11-project",
    )
    coverage = {item["requirement_id"]: item for item in project["project_intelligence"]["evidence_coverage"]}

    for requirement_id in ("sufficient_grid_capacity", "water_capacity", "fiber_diversity"):
        assert coverage[requirement_id]["status"] == "UNRESOLVED"
        assert coverage[requirement_id]["semantic_strength"] == "UNSUPPORTED_SEMANTICS"
        assert coverage[requirement_id]["missing_evidence"]
    assert coverage["water_capacity"]["evidence_available"] is True
    assert "provider_confirmed_water_capacity" in coverage["water_capacity"]["missing_evidence"]
    assert "utility_confirmed_deliverable_capacity_mw" in coverage["sufficient_grid_capacity"]["missing_evidence"]
    assert any(gap["requirement_id"] == "fiber_diversity" for gap in project["project_intelligence"]["evidence_gaps"])


def test_phase11_world_snapshot_fuses_observed_sources_with_complete_manifest(tmp_path):
    store, site, service = world_service(tmp_path, "site-phase11-world")
    service.building_provider = FixturePolygonProvider("buildings")
    service.water_provider = FixturePolygonProvider("water")
    service.land_cover_provider = FixturePolygonProvider("land_cover")

    world = run(service.create(
        site_snapshot_id=site["snapshot_id"],
        requested_layers=["terrain", "roads", "buildings", "water", "land_cover", "transmission"],
    ))
    layers = {item["layer"]: item for item in world["layers"]}

    assert world["schema_version"] == "world_snapshot_v2"
    assert world["site_snapshot_id"] == site["snapshot_id"]
    assert all(layers[name]["origin"] == "OBSERVED" for name in layers)
    assert all(layers[name]["aoi"] == world["query_aoi"] for name in layers)
    assert layers["transmission"]["availability"] == "UNAVAILABLE"
    assert {entry["layer"] for entry in world["source_manifest"]} == {"terrain", "roads", "buildings", "water", "land_cover"}
    assert all(entry["artifact_hashes"] and entry["spatial_reference"] and entry["aoi"] for entry in world["source_manifest"])
    assert store.get_site_snapshot(site["snapshot_id"])["geometry"] == site["geometry"]
