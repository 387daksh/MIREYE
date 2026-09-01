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
from app.infrastructure.observability import traced_async
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

# The provider does not yet expose a BESS preset; fall back to explicit BESS fields.
BESS_SITING_PRESET = "bess_siting"
MIREYE_EXPLICIT_FIELD_LIMIT = 50
PROJECT_EVIDENCE_FIELDS = {
    "identity": (
        "parcel_id", "parcel_apn", "parcel_address", "parcel_owner", "parcel_zoning",
        "parcel_area_m2", "parcel_boundary_geojson", "parcel_data_source",
        "parcel_match_type", "parcel_match_distance_m", "parcel_match_radius_m",
    ),
    "grid": (
        "nearest_osm_transmission_line_distance_m", "nearest_osm_transmission_line_voltage_kv",
        "nearest_osm_transmission_line_circuits", "nearest_osm_transmission_line_operator",
        "nearest_osm_transmission_line_lifecycle", "nearest_osm_substation_distance_m",
        "nearest_osm_substation_name", "nearest_osm_substation_max_voltage_kv",
        "nearest_osm_substation_operator", "nearest_osm_substation_type",
        "nearest_osm_transmission_transformer_distance_m", "nearest_osm_transmission_transformer_rating_mva",
        "nearest_substation_distance_m", "nearest_substation_max_voltage_kv", "nearest_substation_status",
        "nearest_transmission_line_distance_m", "nearest_transmission_line_voltage_kv",
        "nearest_transmission_line_voltage_class", "nearest_transmission_line_voltage_basis",
        "nearest_transmission_line_status", "nearest_transmission_line_owner",
        "max_transmission_line_voltage_kv_within_radius", "max_transmission_line_voltage_class_within_radius",
        "transmission_lines_within_radius_count", "substations_within_radius_count", "substations_radius_m",
        "electric_utility_service_territory", "iso_rto", "interconnection_queue_active_capacity_county_mw",
        "interconnection_queue_active_capacity_ercot_mw",
        "nearest_power_plant_name", "nearest_power_plant_distance_m", "nearest_power_plant_primary_fuel",
        "nearest_power_plant_capacity_mw", "nearest_power_plant_operator",
        "nearest_power_plant_technology", "nearest_power_plant_sector", "nearest_gas_pipeline_distance_m",
        "nearest_gas_pipeline_operator", "nearest_gas_pipeline_type", "nearest_interstate_gas_pipeline_distance_m",
        "transmission_redundancy_flag",
    ),
    "terrain": (
        "elevation", "slope_degrees", "aspect_degrees", "aspect_cardinal", "soil_drainage_class",
        "soil_hydrologic_group", "soil_shrink_swell_class", "soil_restrictive_layer_depth_cm",
        "bedrock_depth_cm", "grading_difficulty_class", "lcms_class", "land_use_class", "tree_canopy_pct",
    ),
    "environment": (
        "within_floodplain_polygon", "fema_flood_zone", "intersects_wetland",
        "wetland_acres_on_parcel", "wetland_fraction_of_parcel", "nearest_wetland_distance_m",
        "surface_water_permanence_pct", "nearest_waterbody_name", "seismic_design_category",
        "design_wind_speed_mph", "wildfire_annual_frequency", "drought_category",
        "nearest_dam_distance_m", "nearest_dam_hazard_potential", "in_air_quality_nonattainment",
        "nearest_superfund_distance_m", "nearest_brownfield_distance_m",
    ),
    "access_utilities": (
        "nearest_major_road_distance_m", "nearest_major_road_name", "nearest_major_road_class",
        "roads_within_500m_count", "nearest_rail_line_distance_m", "nearest_long_haul_rail_corridor_distance_m",
        "fiber_provider_count", "fiber_broadband_available", "within_water_service_area", "water_system_name",
        "water_service_area_provenance", "within_sewer_service_area", "sewer_service_area_provider",
        "sewer_service_area_provenance", "nearest_wastewater_plant_distance_m",
        "public_water_system_population_served",
    ),
    "site_context": (
        "primary_building_overture_class", "primary_building_height_m", "primary_building_num_floors",
        "primary_building_footprint_sqm", "surface_management_agency", "in_opportunity_zone",
        "political_region", "political_county", "political_locality", "tract_geoid",
    ),
}
BESS_CONTEXT_FIELDS = tuple(dict.fromkeys(
    field for fields in PROJECT_EVIDENCE_FIELDS.values() for field in fields
))
BESS_EVIDENCE_DOMAINS = tuple(PROJECT_EVIDENCE_FIELDS)
REQUIREMENT_EVIDENCE_DOMAINS = {
    "parcel_acreage_range": ("identity",), "land_size_context": ("identity",),
    "resolution_point_outside_fema_sfha": ("environment",), "parcel_outside_fema_sfha": ("environment",),
    "footprint_outside_fema_sfha": ("environment",), "wetland_context": ("environment",),
    "max_nwi_wetland_fraction_of_parcel": ("environment",), "max_nwi_wetland_acres_on_parcel": ("environment",),
    "max_resolution_point_slope_degrees": ("terrain",), "max_slope_degrees": ("terrain",),
    "terrain_context": ("terrain",), "max_resolution_point_substation_distance_m": ("grid",),
    "max_resolution_point_transmission_distance_m": ("grid",), "transmission_proximity": ("grid",),
    "bess_export_interconnection": ("grid",), "max_resolution_point_major_road_distance_m": ("access_utilities",),
    "road_proximity": ("access_utilities",), "legal_access": ("access_utilities",),
    "parcel_zoning_code_in": ("identity",), "industrial_zoning": ("identity",), "zoning_context": ("identity",),
    "energy_storage_entitlement": ("identity", "site_context"),
    "water_capacity": ("access_utilities",), "fiber_diversity": ("access_utilities",),
}
DOMAIN_IMPACT = {
    "identity": "CRITICAL", "grid": "CRITICAL", "terrain": "MEDIUM",
    "environment": "HIGH", "access_utilities": "HIGH", "site_context": "MEDIUM",
}
EVIDENCE_SEMANTIC_VERSION = "mireye_evidence_semantics_v1"

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


def conceptual_bess_facility(
    *,
    center_xy_m: list[float],
    width_m: float,
    length_m: float,
    height_m: float,
    rotation_deg: float,
    power_mw: float,
    energy_mwh: float,
    duration_hours: float,
    expansion_power_mw: float = 300.0,
    expansion_energy_mwh: float = 1200.0,
    elements: list[str] | None = None,
) -> dict:
    """Return deterministic conceptual BESS massing used by the sandbox."""

    def component(
        component_id: str,
        kind: str,
        label: str,
        *,
        center_uv: tuple[float, float],
        width_ratio: float,
        length_ratio: float,
        component_height_m: float,
        render_class: str,
        phase: str,
        component_power_mw: float | None = None,
        component_energy_mwh: float | None = None,
    ) -> dict:
        attributes = {"phase": phase}
        if component_power_mw is not None:
            attributes["power_mw"] = round(component_power_mw, 3)
        if component_energy_mwh is not None:
            attributes["energy_mwh"] = round(component_energy_mwh, 3)
        return {
            "id": component_id,
            "kind": kind,
            "label": label,
            "origin": "PROPOSED",
            "semantic_class": f"proposed_{kind}",
            "render_class": render_class,
            "geometry_relative": {
                "shape": "oriented_rectangle",
                "center_uv": [center_uv[0], center_uv[1]],
                "width_ratio": width_ratio,
                "length_ratio": length_ratio,
                "height_m": component_height_m,
                "rotation_offset_deg": 0.0,
            },
            "attributes": attributes,
        }

    phase_power = min(float(power_mw), float(expansion_power_mw))
    phase_energy = min(float(energy_mwh), float(expansion_energy_mwh))
    expansion_power = max(0.0, float(expansion_power_mw) - phase_power)
    expansion_energy = max(0.0, float(expansion_energy_mwh) - phase_energy)
    components = [
        component("battery_enclosure_a", "battery_enclosure", "Battery enclosure block A", center_uv=(-0.27, -0.08), width_ratio=0.22, length_ratio=0.42, component_height_m=height_m, render_class="building", phase="PHASE_1", component_power_mw=phase_power / 2, component_energy_mwh=phase_energy / 2),
        component("battery_enclosure_b", "battery_enclosure", "Battery enclosure block B", center_uv=(-0.02, -0.08), width_ratio=0.22, length_ratio=0.42, component_height_m=height_m, render_class="building", phase="PHASE_1", component_power_mw=phase_power / 2, component_energy_mwh=phase_energy / 2),
        component("inverter_pcs_a", "inverter_pcs", "Inverter / PCS block A", center_uv=(0.18, -0.20), width_ratio=0.10, length_ratio=0.15, component_height_m=3.0, render_class="utility", phase="PHASE_1", component_power_mw=phase_power / 2),
        component("inverter_pcs_b", "inverter_pcs", "Inverter / PCS block B", center_uv=(0.18, 0.00), width_ratio=0.10, length_ratio=0.15, component_height_m=3.0, render_class="utility", phase="PHASE_1", component_power_mw=phase_power / 2),
        component("point_of_interconnection", "point_of_interconnection", "Point of interconnection", center_uv=(-0.27, -0.36), width_ratio=0.22, length_ratio=0.10, component_height_m=6.0, render_class="utility", phase="PHASE_1"),
        component("service_area", "service_area", "Service area", center_uv=(-0.12, 0.25), width_ratio=0.34, length_ratio=0.16, component_height_m=0.4, render_class="surface", phase="PHASE_1"),
        component("internal_access", "internal_access_road", "Internal access road", center_uv=(-0.46, 0.0), width_ratio=0.035, length_ratio=0.86, component_height_m=0.2, render_class="access", phase="PHASE_1"),
        component("expansion_reserve", "expansion_reserve", "Expansion reserve", center_uv=(0.36, 0.0), width_ratio=0.20, length_ratio=0.72, component_height_m=0.2, render_class="reserve", phase="FUTURE", component_power_mw=expansion_power, component_energy_mwh=expansion_energy),
    ]
    element_kinds = {
        "battery_enclosures": {"battery_enclosure"},
        "inverter_pcs": {"inverter_pcs"},
        "point_of_interconnection": {"point_of_interconnection"},
        "internal_access": {"internal_access_road"},
        "service_area": {"service_area"},
        "expansion_reserve": {"expansion_reserve"},
    }
    selected = list(element_kinds) if elements is None else list(dict.fromkeys(elements))
    invalid = set(selected) - set(element_kinds)
    if invalid or not selected:
        raise SandboxError(f"Unsupported conceptual BESS elements: {', '.join(sorted(invalid)) or 'none'}.")
    selected_kinds = set().union(*(element_kinds[name] for name in selected))
    components = [item for item in components if item["kind"] in selected_kinds]
    return {
        "id": "bess_1",
        "kind": "bess_facility",
        "origin": "PROPOSED",
        "semantic_class": "proposed_bess_facility",
        "render_class": "facility_boundary",
        "geometry_local": {
            "shape": "oriented_rectangle",
            "center_xy_m": [round(float(center_xy_m[0]), 3), round(float(center_xy_m[1]), 3)],
            "width_m": round(float(width_m), 6),
            "length_m": round(float(length_m), 6),
            "height_m": round(float(height_m), 6),
            "rotation_deg": round(float(rotation_deg), 6),
        },
        "attributes": {
            "capacity_mw": round(float(power_mw), 3),
            "power_mw": round(float(power_mw), 3),
            "energy_mwh": round(float(energy_mwh), 3),
            "duration_hours": round(float(duration_hours), 3),
            "phase": "PHASE_1",
            "expansion_power_mw": round(float(expansion_power_mw), 3),
            "expansion_energy_mwh": round(float(expansion_energy_mwh), 3),
            "layout_strategy": "bess_block_massing",
            "planning_basis": "conceptual_land_envelope_only",
            "selected_elements": selected,
        },
        "components": components,
        "assumption_profile": "conceptual_bess_100mw_400mwh_v1",
    }


def facility_component_object(facility: dict, component: dict) -> dict:
    """Materialize one relative facility component in the local-meter frame."""
    parent = facility["geometry_local"]
    relative = component["geometry_relative"]
    radians = math.radians(float(parent["rotation_deg"]))
    x = float(relative["center_uv"][0]) * float(parent["width_m"])
    y = float(relative["center_uv"][1]) * float(parent["length_m"])
    return {
        **copy.deepcopy(component),
        "geometry_local": {
            "shape": "oriented_rectangle",
            "center_xy_m": [
                float(parent["center_xy_m"][0]) + x * math.cos(radians) - y * math.sin(radians),
                float(parent["center_xy_m"][1]) + x * math.sin(radians) + y * math.cos(radians),
            ],
            "width_m": float(relative["width_ratio"]) * float(parent["width_m"]),
            "length_m": float(relative["length_ratio"]) * float(parent["length_m"]),
            "height_m": float(relative["height_m"]),
            "rotation_deg": float(parent["rotation_deg"]) + float(relative.get("rotation_offset_deg", 0)),
        },
        "attributes": {
            **copy.deepcopy(component.get("attributes", {})),
            "capacity_mw": max(1.0, float(component.get("attributes", {}).get("power_mw", 1.0))),
        },
    }


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
        "proposed": [conceptual_bess_facility(
            center_xy_m=_local_xy(centroid.y, centroid.x, origin),
            width_m=410.792,
            length_m=547.723,
            height_m=4.0,
            rotation_deg=0.0,
            power_mw=100.0,
            energy_mwh=400.0,
            duration_hours=4.0,
        )],
        "render_contract": {
            "version": "semantic_scene_v1",
            "authoritative_state": "scene_state",
            "future_outputs": ["rgb", "depth", "semantic_segmentation", "proposed_geometry_metadata"],
            "origin_classes": ["OBSERVED", "DERIVED", "PROPOSED", "GENERATED"],
            "semantic_masks": {
                "parcel": "OBSERVED.parcel_boundary",
                "terrain": "OBSERVED.world.terrain",
                "roads": "OBSERVED.world.roads",
                "buildings": "OBSERVED.world.buildings",
                "water": "OBSERVED.world.water",
                "land_cover": "OBSERVED.world.land_cover",
                "proposed_objects": "PROPOSED.*",
            },
        },
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


def _catalog_scope(name: str, metadata: dict) -> str | None:
    declared = metadata.get("scope") or metadata.get("spatial_scope") or SITE_SNAPSHOT_FIELD_SCOPES.get(name)
    if declared:
        return str(declared).upper().replace("-", "_")
    if name.startswith("parcel_") or name.endswith("_on_parcel") or "fraction_of_parcel" in name:
        return "PARCEL"
    if name.startswith("nearest_") or name.endswith("_distance_m") or "within_radius" in name:
        return "NEAREST_FEATURE"
    return "POINT"


def _field_semantics(name: str, record: dict, metadata: dict) -> dict:
    status, value = record.get("status"), record.get("value")
    scope = _catalog_scope(name, metadata)
    if status not in {None, "ok"} or value is None:
        return {
            "semantic_strength": "INSUFFICIENT_EVIDENCE", "semantic_class": "UNAVAILABLE_VALUE",
            "is_direct": False, "claim_limits": ["No usable value was returned for this field."],
        }
    source = str(record.get("source") or metadata.get("source") or "").upper()
    derivation = metadata.get("derivation")
    derived = bool(derivation) or source.startswith("MIREYE_DERIVED") or name.startswith(("estimated_", "modeled_")) or name in {
        "transmission_redundancy_flag", "grading_difficulty_class", "developable_acres_proxy",
    }
    if derived:
        strength, semantic_class = "DERIVED", "SOURCE_DERIVED_SIGNAL"
    elif name in {
        "parcel_id", "parcel_apn", "parcel_address", "parcel_owner", "parcel_area_m2",
        "parcel_boundary_geojson", "parcel_data_source", "parcel_match_type", "parcel_match_distance_m",
    }:
        strength, semantic_class = "DIRECTLY_VERIFIED", "PARCEL_IDENTITY_FACT"
    elif name == "parcel_zoning":
        strength, semantic_class = "DIRECTLY_VERIFIED", "RAW_ZONING_FACT"
    elif scope == "PARCEL":
        strength, semantic_class = "DIRECTLY_VERIFIED", "PARCEL_SCOPED_FACT"
    elif scope == "POINT":
        strength, semantic_class = "SOURCE_BACKED_SIGNAL", "POINT_SCOPED_SIGNAL"
    else:
        strength, semantic_class = "SOURCE_BACKED_SIGNAL", "PROXIMITY_OR_CONTEXT_SIGNAL"
    limits = []
    if name.startswith("nearest_") or "within_radius" in name or name in {"transmission_redundancy_flag", "interconnection_queue_active_capacity_county_mw"}:
        limits.append("Infrastructure proximity, voltage, counts, queue totals, and redundancy signals do not prove export or injection interconnection capability.")
    if name == "parcel_zoning":
        limits.append("A raw zoning value does not establish permitted energy-storage use or entitlement.")
    if name in {"within_floodplain_polygon", "fema_flood_zone"}:
        limits.append("This point-scoped FEMA evidence does not prove whole-parcel or footprint flood exclusion.")
    if name == "slope_degrees":
        limits.append("Point slope does not prove parcel-wide or footprint-wide slope.")
    if name.startswith("nearest_major_road") or name == "roads_within_500m_count":
        limits.append("Mapped-road proximity does not prove legal access, frontage, easements, or heavy-haul suitability.")
    if name in {"within_water_service_area", "water_system_name", "public_water_system_population_served"}:
        limits.append("Service-area context does not prove project water capacity or a service commitment.")
    if name in {"fiber_provider_count", "fiber_broadband_available"}:
        limits.append("Provider availability does not prove physically diverse routes or contracted capacity.")
    hint = metadata.get("interpretation_hints")
    if hint:
        limits.append(str(hint))
    return {
        "semantic_strength": strength, "semantic_class": semantic_class,
        "is_direct": strength == "DIRECTLY_VERIFIED", "claim_limits": list(dict.fromkeys(limits)),
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
        project_profile: str | None = None,
    ) -> dict:
        snapshot = self.store.get_site_snapshot(snapshot_id)
        if snapshot is None:
            raise SandboxError("SiteSnapshot not found.")
        project_plan = None
        preset = None
        fetch_fields = None
        if project_profile is not None:
            project_plan = await self.project_intelligence_plan(snapshot_id, profile=project_profile, now=now)
            fields = project_plan["fields"]
        freshness = self.freshness_status(snapshot_id, now=now, test_expiry_overrides=test_expiry_overrides, fields=fields)
        if not freshness["refresh_required"]:
            return {
                "status": "NO_REFRESH_REQUIRED",
                "snapshot_id": snapshot_id,
                "site_id": snapshot.get("site_id"),
                "freshness": freshness,
                "expected_credits": 0,
                "confirmation_required": False,
            }

        if project_plan is not None:
            preset_fields = set(project_plan["preset_fields"])
            refresh_fields = set(freshness["refresh_fields"])
            explicit_fields = refresh_fields | set(REFRESH_IDENTITY_FIELDS)
            preset = project_profile if len(explicit_fields) > MIREYE_EXPLICIT_FIELD_LIMIT and refresh_fields & preset_fields else None
            fetch_fields = sorted((refresh_fields - preset_fields if preset else refresh_fields) | set(REFRESH_IDENTITY_FIELDS))
            fields = sorted((preset_fields if preset else set()) | set(fetch_fields))
        else:
            fields = sorted(set(freshness["refresh_fields"]) | set(REFRESH_IDENTITY_FIELDS))
            fetch_fields = fields
        try:
            _selected_fields, catalog = await self._catalog_selection(fetch_fields if preset else fields)
            if preset or len(fetch_fields) <= MIREYE_EXPLICIT_FIELD_LIMIT:
                batch_fields = [fetch_fields]
            else:
                identity = [field for field in PROJECT_EVIDENCE_FIELDS["identity"] if field in fetch_fields]
                ordered = identity + [field for field in fetch_fields if field not in identity]
                batch_fields = [ordered[offset:offset + MIREYE_EXPLICIT_FIELD_LIMIT] for offset in range(0, len(ordered), MIREYE_EXPLICIT_FIELD_LIMIT)]
            fetch_batches = []
            for selected in batch_fields:
                batch_quote = await self.client.fetch_quote(locations=1, fields=selected, preset=preset)
                fetch_batches.append({"fields": selected, "preset": preset, "quote": batch_quote})
            batch_costs = [self._estimated_credits(item["quote"]) for item in fetch_batches]
            quote = fetch_batches[0]["quote"] if len(fetch_batches) == 1 else {
                "estimated_credits": sum(batch_costs) if all(isinstance(cost, (int, float)) for cost in batch_costs) else None,
                "batch_quotes": [item["quote"] for item in fetch_batches],
            }
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
            "fetch_fields": fetch_fields,
            "fetch_batches": fetch_batches,
            "preset": preset,
            "project_profile": project_profile,
            "field_manifest": project_plan["field_manifest"] if project_plan else None,
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

    @traced_async("workflow.evidence_refresh")
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
            _selected_fields, catalog = await self._catalog_selection(plan.get("fetch_fields") or fields)
            point = previous["parcel_identity"]["selected_point"]
            request = {
                "lat": float(point["lat"]), "lng": float(point["lng"]),
                "fields": plan.get("fetch_fields") or fields,
            }
            if plan.get("preset"):
                request["preset"] = plan["preset"]
            responses = []
            for batch in plan.get("fetch_batches") or [{"fields": request["fields"], "preset": plan.get("preset")}]:
                response = await self.client.fetch(
                    lat=request["lat"], lng=request["lng"],
                    fields=batch["fields"], preset=batch.get("preset"),
                )
                if response.get("ok") is False:
                    message = response.get("error", {}).get("message", "MIREYE could not refresh this location.")
                    raise SandboxError(message)
                responses.append(response)
            if len(responses) == 1:
                dossier = responses[0]
            else:
                dossier = {
                    "ok": True,
                    "fields": {name: record for response in responses for name, record in response.get("fields", {}).items()},
                    "batches": responses,
                }
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

    async def catalog_evidence_plan(
        self,
        *,
        project_type: str,
        requirements: list[dict] | None = None,
        unresolved_gaps: list[dict] | None = None,
        requested_decision: str = "site_diligence",
        snapshot_id: str | None = None,
        profile: str | None = None,
        now: float | None = None,
        require_profile: bool = False,
    ) -> dict:
        """Select relevant live-catalog evidence without executing a metered request."""
        snapshot = self.store.get_site_snapshot(snapshot_id) if snapshot_id else None
        if snapshot_id and snapshot is None:
            raise SandboxError("SiteSnapshot not found.")
        try:
            catalog = await self.client.meta_fields()
        except httpx.HTTPError as exc:
            raise MireyeUnavailableError("MIREYE field catalog is temporarily unavailable.") from exc
        presets = catalog.get("presets") or {}
        is_bess = any(value in str(project_type).casefold() for value in ("battery energy storage", "energy storage", "bess"))
        selected_profile = profile or (BESS_SITING_PRESET if is_bess else None)
        preset_fields = presets.get(selected_profile, []) if selected_profile else []
        if require_profile and (not isinstance(preset_fields, list) or not preset_fields):
            raise FieldCatalogError(f"Current MIREYE catalog lacks the {profile} preset.")
        metadata = {
            item["name"]: item for item in catalog.get("fields", [])
            if isinstance(item, dict) and item.get("name")
        }
        requirement_ids = [item.get("constraint_id") for item in requirements or [] if item.get("constraint_id")]
        broad_diligence = is_bess and requested_decision in {"candidate_screening", "site_diligence", "project_readiness"}
        domains = list(BESS_EVIDENCE_DOMAINS if broad_diligence else ("identity",))
        for requirement_id in requirement_ids:
            domains.extend(REQUIREMENT_EVIDENCE_DOMAINS.get(requirement_id, ()))
        domains = list(dict.fromkeys(domains))
        domain_fields = [field for domain in domains for field in PROJECT_EVIDENCE_FIELDS[domain] if field in metadata]
        usable_preset = list(preset_fields) if broad_diligence and isinstance(preset_fields, list) else []
        fields = list(dict.fromkeys([*usable_preset, *domain_fields]))
        supplemental = [name for name in fields if name not in usable_preset]
        freshness = self.freshness_status(snapshot_id, now=now, fields=fields) if snapshot_id else {
            "status": "MISSING_EVIDENCE", "fresh_fields": [], "refresh_fields": fields,
            "stale_fields": [], "missing_fields": fields, "incompatible_fields": [], "deprecated_fields": [],
            "refresh_required": bool(fields), "fields": [{"field": name, "classification": "missing", "reason": "No SiteSnapshot evidence exists yet."} for name in fields],
        }
        freshness_by_field = {item["field"]: item["classification"] for item in freshness["fields"]}
        gap_requirements = {
            gap.get("requirement_id") for gap in unresolved_gaps or [] if isinstance(gap, dict) and gap.get("requirement_id")
        }
        field_manifest = []
        for name in fields:
            field_metadata = metadata.get(name, {})
            field_domains = [domain for domain in domains if name in PROJECT_EVIDENCE_FIELDS[domain]]
            affected = [requirement_id for requirement_id in requirement_ids if set(field_domains) & set(REQUIREMENT_EVIDENCE_DOMAINS.get(requirement_id, ()))]
            if not affected and name in usable_preset:
                affected = sorted(gap_requirements or requirement_ids)
            billing = field_metadata.get("billing") or {}
            planned_semantics = _field_semantics(name, {"status": "ok", "value": "planned"}, field_metadata)
            field_manifest.append({
                "field": name,
                "reason": f"Supports {', '.join(field_domains).replace('_', ' ') or 'BESS site'} evidence for {requested_decision.replace('_', ' ')}.",
                "requirement_ids": affected,
                "decision_impact": max((DOMAIN_IMPACT[domain] for domain in field_domains), default="MEDIUM", key=lambda value: ("LOW", "MEDIUM", "HIGH", "CRITICAL").index(value)),
                "spatial_scope": _catalog_scope(name, field_metadata),
                "freshness_required": {"policy": "PROVIDER_TTL", "ttl_seconds": field_metadata.get("ttl_seconds")},
                "source": "MIREYE", "provider_source": field_metadata.get("source"),
                "metered": billing.get("credits_per_location") is not None or billing.get("metered_group") is not None,
                "billing": billing, "unit": field_metadata.get("unit"), "source_url": field_metadata.get("source_url"),
                "semantic_strength": planned_semantics["semantic_strength"], "semantic_class": planned_semantics["semantic_class"],
                "claim_limits": planned_semantics["claim_limits"], "catalog_presets": field_metadata.get("presets") or [],
                "selection_status": freshness_by_field.get(name, "missing"),
                "metadata_status": "AVAILABLE" if name in metadata else "PRESET_ONLY",
            })
        refresh_fields = freshness["refresh_fields"]
        refresh_preset_fields = [name for name in refresh_fields if name in usable_preset]
        use_preset = bool(selected_profile and len(refresh_preset_fields) > MIREYE_EXPLICIT_FIELD_LIMIT)
        return {
            "project_type": project_type, "requested_decision": requested_decision,
            "profile": selected_profile if usable_preset else None,
            "catalog_version": self._catalog_version(catalog),
            "selected_presets": [selected_profile] if usable_preset else [],
            "preset_fields": usable_preset,
            "supplemental_fields": supplemental,
            "fields": fields,
            "field_count": len(fields),
            "refresh_fields": refresh_fields,
            "freshness": freshness,
            "field_manifest": field_manifest,
            "request_strategy": {
                "preset": selected_profile if use_preset else None,
                "explicit_fields": sorted(set(refresh_fields) - (set(usable_preset) if use_preset else set())),
                "reason": "Use the release-defined project preset when more than the explicit-field limit needs refresh; otherwise request only stale or missing fields.",
            },
            "known_gaps": [{
                "requested_field": "utility_or_iso_confirmed_export_injection_capacity_mw",
                "reason": "A 100 MW phase and 300 MW expansion require confirmed export or injection interconnection capability.",
                "nearest_available_fields": [
                    "nearest_osm_transmission_transformer_rating_mva",
                    "interconnection_queue_active_capacity_county_mw",
                    "transmission_redundancy_flag",
                ],
                "status": "NOT_PROVEN_BY_CURRENT_CATALOG",
                "decision_blocking": True,
            }, {
                "requested_field": "jurisdiction_aware_energy_storage_entitlement",
                "reason": "Raw zoning does not establish permitted energy-storage use.",
                "nearest_available_fields": ["parcel_zoning"], "status": "UNSUPPORTED_SEMANTICS", "decision_blocking": True,
            }, {
                "requested_field": "recorded_legal_access",
                "reason": "Mapped-road proximity does not establish legal access or easements.",
                "nearest_available_fields": ["nearest_major_road_distance_m", "nearest_major_road_name"],
                "status": "UNSUPPORTED_SEMANTICS", "decision_blocking": True,
            }, {
                "requested_field": "provider_confirmed_water_capacity",
                "reason": "Service-area context does not establish committed project capacity.",
                "nearest_available_fields": ["within_water_service_area", "water_system_name"],
                "status": "NOT_PROVEN_BY_CURRENT_CATALOG", "decision_blocking": True,
            }, {
                "requested_field": "carrier_confirmed_physically_diverse_routes",
                "reason": "Provider availability does not establish route diversity.",
                "nearest_available_fields": ["fiber_provider_count", "fiber_broadband_available"],
                "status": "NOT_PROVEN_BY_CURRENT_CATALOG", "decision_blocking": False,
            }],
        }

    async def project_intelligence_plan(
        self, snapshot_id: str, *, profile: str = BESS_SITING_PRESET, now: float | None = None,
    ) -> dict:
        return await self.catalog_evidence_plan(
            project_type="Battery energy storage system", requested_decision="site_diligence", snapshot_id=snapshot_id,
            profile=profile, now=now,
        )

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
        missing_catalog_fields = [
            name for name in spend_plan.get("fetch_fields", spend_plan["requested_fields"])
            if name not in catalog_fields
        ]
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
        status = str(record.get("status") or "ok").lower()
        if status not in {"ok", "absent"}:
            return "incompatible", f"Evidence status is not usable: {record.get('status')}."
        if status == "ok" and record.get("value") is None:
            return "missing", "Evidence value is missing."
        expected_scope = SITE_SNAPSHOT_FIELD_SCOPES.get(field)
        if expected_scope and record.get("scope") not in {expected_scope, None}:
            return "incompatible", f"Evidence scope is {record.get('scope')}, not {expected_scope}."
        try:
            expires_at = float(test_expires_at if test_expires_at is not None else record["expires_at"])
        except (KeyError, TypeError, ValueError):
            return "incompatible", "Evidence has invalid freshness metadata."
        if now >= expires_at:
            return "stale", "Test-time local expiration override has expired." if test_expires_at is not None else "Evidence TTL has expired."
        if status == "absent" or record.get("value") is None:
            return "fresh", "Provider-confirmed absence is cached within its field-level TTL."
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
                    "existence": {"before": before is not None, "after": after is not None},
                    "value": {"before": before.get("value") if isinstance(before, dict) else None, "after": after.get("value") if isinstance(after, dict) else None},
                    "status": {"before": before.get("status") if isinstance(before, dict) else None, "after": after.get("status") if isinstance(after, dict) else None},
                    "scope": {"before": before.get("scope") if isinstance(before, dict) else None, "after": after.get("scope") if isinstance(after, dict) else None},
                    "freshness": {"before": before.get("expires_at") if isinstance(before, dict) else None, "after": after.get("expires_at") if isinstance(after, dict) else None},
                    "source": {
                        "before": {key: before.get(key) for key in ("provider", "source", "source_url")} if isinstance(before, dict) else None,
                        "after": {key: after.get(key) for key in ("provider", "source", "source_url")} if isinstance(after, dict) else None,
                    },
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
            metadata = catalog_fields.get(name, {})
            ttl_seconds = int(metadata.get("ttl_seconds") or 0)
            normalized = {
                "field": name,
                "value": record.get("value"),
                "status": record.get("status", "absent" if source_record is None else "ok"),
                "confidence": record.get("confidence"),
                "provider": record.get("provider") or metadata.get("provider") or "MIREYE",
                "source": record.get("source") or metadata.get("source"),
                "source_url": record.get("source_url") or metadata.get("source_url"),
                "unit": record.get("unit") or metadata.get("unit"),
                "lifecycle": metadata.get("lifecycle"),
                "scope": _catalog_scope(name, metadata),
                "spatial_scope": _catalog_scope(name, metadata),
                "description": metadata.get("description"),
                "interpretation_hints": metadata.get("interpretation_hints"),
                "null_meaning": metadata.get("null_meaning"),
                "derivation": metadata.get("derivation"),
                "presets": metadata.get("presets") or [],
                "billing": metadata.get("billing"),
                "ttl_seconds": ttl_seconds,
                "fetched_at": observed_at,
                "observed_at": observed_at,
                "expires_at": observed_at + ttl_seconds,
                "semantic_version": EVIDENCE_SEMANTIC_VERSION,
            }
            normalized.update(_field_semantics(name, normalized, metadata))
            normalized["evidence_hash"] = _hash(normalized)
            evidence[name] = normalized
        return evidence
