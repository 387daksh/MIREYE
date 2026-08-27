"""Single-agent orchestration for customer-supplied site candidate lists."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from app.config import MIREYE_ENRICHMENT_BATCH_SIZE
from app.infrastructure.observability import traced_async
from app.product import compile_request
from app.project_changes import changes_from_refresh, changes_from_world_refresh
from app.project_intelligence import RFI_ACTION_TYPES, build_project_intelligence
from app.project_readiness import AuthoritativeSourceService, build_entitlement_state, build_power_readiness
from app.sandbox import ConfirmationRequired, SandboxError, SiteSnapshotService, scene_state_from_snapshot
from app.sandbox_evaluator import SceneValidationError, evaluate_site
from app.workspace.store import WorkspaceStore
from app.world import WorldError


MAX_CANDIDATES = 500
# Live validation on 2026-08-22 returned blank failures for four locations
# despite OpenAPI allowing 25; two-location batches completed reliably.
BATCH_SIZE = MIREYE_ENRICHMENT_BATCH_SIZE
QUOTE_TTL_SECONDS = 900
IDENTITY_FIELDS = (
    "parcel_id", "parcel_apn", "parcel_address", "parcel_area_m2",
    "parcel_boundary_geojson", "parcel_data_source", "parcel_match_type",
    "parcel_match_distance_m", "parcel_match_radius_m",
)
CONSTRAINT_FIELDS = {
    "land_size_context": ("parcel_area_m2",),
    "parcel_acreage_range": ("parcel_area_m2",),
    "resolution_point_outside_fema_sfha": ("within_floodplain_polygon", "fema_flood_zone"),
    "parcel_outside_fema_sfha": ("within_floodplain_polygon", "fema_flood_zone"),
    "footprint_outside_fema_sfha": ("within_floodplain_polygon", "fema_flood_zone"),
    "max_nwi_wetland_fraction_of_parcel": ("wetland_fraction_of_parcel",),
    "max_nwi_wetland_acres_on_parcel": ("wetland_acres_on_parcel",),
    "wetland_context": ("wetland_fraction_of_parcel", "wetland_acres_on_parcel"),
    "max_resolution_point_slope_degrees": ("slope_degrees",),
    "max_slope_degrees": ("slope_degrees",),
    "terrain_context": ("elevation", "slope_degrees"),
    "max_resolution_point_substation_distance_m": (
        "nearest_substation_distance_m", "nearest_substation_status", "nearest_substation_max_voltage_kv",
    ),
    "max_resolution_point_transmission_distance_m": (
        "nearest_transmission_line_distance_m", "nearest_transmission_line_status",
        "nearest_transmission_line_voltage_kv",
    ),
    "transmission_proximity": ("nearest_transmission_line_distance_m",),
    "max_resolution_point_major_road_distance_m": ("nearest_major_road_distance_m", "nearest_major_road_name"),
    "legal_access": ("nearest_major_road_distance_m", "nearest_major_road_name"),
    "road_proximity": ("nearest_major_road_distance_m", "nearest_major_road_name"),
    "parcel_zoning_code_in": ("parcel_zoning",),
    "industrial_zoning": ("parcel_zoning",),
    "data_center_entitlement": ("parcel_zoning", "political_county", "political_locality", "political_region"),
    "zoning_context": ("parcel_zoning",),
    "sufficient_grid_capacity": (
        "nearest_substation_distance_m", "nearest_substation_status", "nearest_substation_max_voltage_kv",
        "nearest_transmission_line_distance_m", "nearest_transmission_line_voltage_kv",
        "nearest_transmission_line_voltage_class", "nearest_transmission_line_voltage_basis",
        "nearest_transmission_line_status", "nearest_transmission_line_owner",
        "max_transmission_line_voltage_kv_within_radius", "max_transmission_line_voltage_class_within_radius",
        "nearest_osm_transmission_line_distance_m", "nearest_osm_transmission_line_voltage_kv",
        "nearest_osm_substation_distance_m", "nearest_osm_substation_max_voltage_kv",
        "electric_utility_service_territory", "iso_rto", "interconnection_queue_active_capacity_county_mw",
        "interconnection_queue_active_capacity_ercot_mw",
        "transmission_redundancy_flag",
    ),
    "water_capacity": ("within_water_service_area", "water_system_name", "water_service_area_provenance"),
    "fiber_diversity": ("fiber_broadband_available", "fiber_provider_count"),
}
SUPPORTED_CONSTRAINTS = {
    "parcel_acreage_range", "resolution_point_outside_fema_sfha",
    "max_nwi_wetland_fraction_of_parcel", "max_nwi_wetland_acres_on_parcel",
    "max_resolution_point_slope_degrees", "max_resolution_point_substation_distance_m",
    "max_resolution_point_transmission_distance_m", "max_resolution_point_major_road_distance_m",
    "parcel_zoning_code_in",
}
CONSTRAINT_CAPABILITIES = {
    "parcel_acreage_range": {
        "semantic_description": "Compare authoritative parcel area to a caller-supplied acreage range.",
        "input_schema": {"required": ["min_acres", "max_acres"], "properties": {
            "min_acres": {"type": "number", "unit": "acres", "minimum": 0, "maximum": 1_000_000},
            "max_acres": {"type": "number", "unit": "acres", "minimum": 0, "maximum": 1_000_000},
        }},
        "evidence_fields": ["parcel_area_m2"], "spatial_scope": "PARCEL", "evaluator_support": "PASS_FAIL",
        "unsupported_semantics": [], "confirmation_mandatory": False, "assumption_allowed": True,
    },
    "resolution_point_outside_fema_sfha": {
        "semantic_description": "Evaluate FEMA floodplain status only at the parcel resolution point.",
        "input_schema": {"required": [], "properties": {}}, "evidence_fields": ["within_floodplain_polygon", "fema_flood_zone"],
        "spatial_scope": "POINT", "evaluator_support": "PASS_FAIL",
        "unsupported_semantics": ["whole-parcel flood exclusion", "whole-footprint flood exclusion"],
        "confirmation_mandatory": False, "assumption_allowed": True,
    },
    "parcel_outside_fema_sfha": {
        "semantic_description": "Request whole-parcel FEMA flood exclusion.",
        "input_schema": {"required": [], "properties": {}}, "evidence_fields": ["within_floodplain_polygon", "fema_flood_zone"],
        "spatial_scope": "PARCEL", "evaluator_support": "UNRESOLVED_ONLY",
        "unsupported_semantics": ["current evidence is point-scoped"], "confirmation_mandatory": False, "assumption_allowed": False,
    },
    "max_nwi_wetland_fraction_of_parcel": {
        "semantic_description": "Compare mapped NWI wetland overlap fraction to a caller-supplied maximum.",
        "input_schema": {"required": ["max_fraction"], "properties": {
            "max_fraction": {"type": "number", "unit": "fraction", "minimum": 0, "maximum": 1},
        }},
        "evidence_fields": ["wetland_fraction_of_parcel"], "spatial_scope": "PARCEL", "evaluator_support": "PASS_FAIL",
        "unsupported_semantics": ["survey-grade wetland absence", "USACE jurisdiction"], "confirmation_mandatory": False, "assumption_allowed": True,
    },
    "max_nwi_wetland_acres_on_parcel": {
        "semantic_description": "Compare mapped NWI wetland acres to a caller-supplied maximum.",
        "input_schema": {"required": ["max_acres"], "properties": {
            "max_acres": {"type": "number", "unit": "acres", "minimum": 0, "maximum": 1_000_000},
        }},
        "evidence_fields": ["wetland_acres_on_parcel"], "spatial_scope": "PARCEL", "evaluator_support": "PASS_FAIL",
        "unsupported_semantics": ["survey-grade wetland absence", "USACE jurisdiction"], "confirmation_mandatory": False, "assumption_allowed": True,
    },
    "max_resolution_point_slope_degrees": {
        "semantic_description": "Compare slope at the parcel resolution point to a caller-supplied maximum.",
        "input_schema": {"required": ["max_degrees"], "properties": {
            "max_degrees": {"type": "number", "unit": "degrees", "minimum": 0, "maximum": 90},
        }},
        "evidence_fields": ["slope_degrees"], "spatial_scope": "POINT", "evaluator_support": "PASS_FAIL",
        "unsupported_semantics": ["whole-parcel slope", "whole-footprint slope"], "confirmation_mandatory": False, "assumption_allowed": True,
    },
    "max_slope_degrees": {
        "semantic_description": "Request parcel-wide or footprint-wide maximum slope.",
        "input_schema": {"required": ["max_degrees"], "properties": {
            "max_degrees": {"type": "number", "unit": "degrees", "minimum": 0, "maximum": 90},
        }},
        "evidence_fields": ["slope_degrees"], "spatial_scope": "PARCEL", "evaluator_support": "UNRESOLVED_ONLY",
        "unsupported_semantics": ["current evidence is point-scoped"], "confirmation_mandatory": False, "assumption_allowed": False,
    },
    "max_resolution_point_substation_distance_m": {
        "semantic_description": "Compare resolution-point distance to the nearest mapped substation.",
        "input_schema": {"required": ["max_distance_m"], "properties": {
            "max_distance_m": {"type": "number", "unit": "m", "minimum": 0, "maximum": 10_000_000},
            "required_statuses": {"type": "string_list", "unit": None}, "require_operational": {"type": "boolean", "unit": None},
        }},
        "evidence_fields": list(CONSTRAINT_FIELDS["max_resolution_point_substation_distance_m"]), "spatial_scope": "POINT_TO_NEAREST_FEATURE", "evaluator_support": "PASS_FAIL",
        "unsupported_semantics": ["available MW", "deliverability"], "confirmation_mandatory": False, "assumption_allowed": True,
    },
    "max_resolution_point_transmission_distance_m": {
        "semantic_description": "Compare resolution-point distance to the nearest mapped transmission line.",
        "input_schema": {"required": ["max_distance_m"], "properties": {
            "max_distance_m": {"type": "number", "unit": "m", "minimum": 0, "maximum": 10_000_000},
            "required_statuses": {"type": "string_list", "unit": None},
        }},
        "evidence_fields": list(CONSTRAINT_FIELDS["max_resolution_point_transmission_distance_m"]), "spatial_scope": "POINT_TO_NEAREST_FEATURE", "evaluator_support": "PASS_FAIL",
        "unsupported_semantics": ["available MW", "deliverability"], "confirmation_mandatory": False, "assumption_allowed": True,
    },
    "max_resolution_point_major_road_distance_m": {
        "semantic_description": "Compare resolution-point distance to the nearest mapped major road.",
        "input_schema": {"required": ["max_distance_m"], "properties": {
            "max_distance_m": {"type": "number", "unit": "m", "minimum": 0, "maximum": 10_000_000},
        }},
        "evidence_fields": list(CONSTRAINT_FIELDS["max_resolution_point_major_road_distance_m"]), "spatial_scope": "POINT_TO_NEAREST_FEATURE", "evaluator_support": "PASS_FAIL",
        "unsupported_semantics": ["legal access", "frontage", "heavy-haul suitability"], "confirmation_mandatory": False, "assumption_allowed": True,
    },
    "parcel_zoning_code_in": {
        "semantic_description": "Compare the normalized raw parcel zoning code to a caller-supplied allow-list.",
        "input_schema": {"required": ["allowed_codes"], "properties": {
            "allowed_codes": {"type": "string_list", "unit": None, "min_items": 1, "max_items": 100},
        }},
        "evidence_fields": ["parcel_zoning"], "spatial_scope": "PARCEL", "evaluator_support": "PASS_FAIL",
        "unsupported_semantics": ["industrial-use interpretation without a jurisdiction mapping"], "confirmation_mandatory": False, "assumption_allowed": False,
    },
    "industrial_zoning": {
        "semantic_description": "Request semantic industrial zoning without a jurisdiction-aware code mapping.",
        "input_schema": {"required": [], "properties": {}}, "evidence_fields": ["parcel_zoning"], "spatial_scope": "PARCEL",
        "evaluator_support": "UNRESOLVED_ONLY", "unsupported_semantics": ["raw codes do not establish industrial use"],
        "confirmation_mandatory": False, "assumption_allowed": False,
    },
    "data_center_entitlement": {
        "semantic_description": "Request a jurisdiction-specific data-center permitted-use and approval-path determination.",
        "input_schema": {"required": [], "properties": {}},
        "evidence_fields": list(CONSTRAINT_FIELDS["data_center_entitlement"]), "spatial_scope": "SITE",
        "evaluator_support": "UNRESOLVED_ONLY",
        "unsupported_semantics": ["raw zoning and postal locality do not establish permitted use or entitlement"],
        "confirmation_mandatory": False, "assumption_allowed": False,
    },
    "legal_access": {
        "semantic_description": "Request legal access, frontage, or right-of-way proof.",
        "input_schema": {"required": [], "properties": {}}, "evidence_fields": list(CONSTRAINT_FIELDS["legal_access"]),
        "spatial_scope": "PARCEL", "evaluator_support": "UNRESOLVED_ONLY",
        "unsupported_semantics": ["mapped-road proximity does not prove legal access"], "confirmation_mandatory": False, "assumption_allowed": False,
    },
    "sufficient_grid_capacity": {
        "semantic_description": "Request utility-confirmed grid capacity or deliverability.",
        "input_schema": {"required": [], "properties": {}},
        "evidence_fields": list(CONSTRAINT_FIELDS["sufficient_grid_capacity"]), "spatial_scope": "SITE",
        "evaluator_support": "UNRESOLVED_ONLY",
        "unsupported_semantics": ["proximity, voltage, status, and queue context do not prove deliverability"],
        "confirmation_mandatory": False, "assumption_allowed": False,
    },
    "water_capacity": {
        "semantic_description": "Request provider-confirmed water service capacity.",
        "input_schema": {"required": [], "properties": {}}, "evidence_fields": list(CONSTRAINT_FIELDS["water_capacity"]), "spatial_scope": "POINT",
        "evaluator_support": "UNRESOLVED_ONLY", "unsupported_semantics": ["current evidence does not prove provider capacity"],
        "confirmation_mandatory": False, "assumption_allowed": False,
    },
    "fiber_diversity": {
        "semantic_description": "Request carrier-confirmed physically diverse fiber routes.",
        "input_schema": {"required": [], "properties": {}}, "evidence_fields": list(CONSTRAINT_FIELDS["fiber_diversity"]), "spatial_scope": "POINT",
        "evaluator_support": "UNRESOLVED_ONLY", "unsupported_semantics": ["proximity or provider counts do not prove route diversity"],
        "confirmation_mandatory": False, "assumption_allowed": False,
    },
}
REQUIREMENT_GAP_TARGETS = {
    "land_size_context": ["parcel_acreage_range"],
    "parcel_outside_fema_sfha": ["resolution_point_outside_fema_sfha", "parcel_outside_fema_sfha"],
    "wetland_context": ["max_nwi_wetland_fraction_of_parcel", "max_nwi_wetland_acres_on_parcel"],
    "terrain_context": ["max_resolution_point_slope_degrees", "max_slope_degrees"],
    "max_slope_degrees": ["max_resolution_point_slope_degrees", "max_slope_degrees"],
    "transmission_proximity": ["max_resolution_point_transmission_distance_m"],
    "road_proximity": ["max_resolution_point_major_road_distance_m"],
    "legal_access": ["max_resolution_point_major_road_distance_m", "legal_access"],
    "zoning_context": ["parcel_zoning_code_in", "industrial_zoning"],
    "industrial_zoning": ["parcel_zoning_code_in", "industrial_zoning"],
}


class DiligenceError(ValueError):
    pass


class CandidateProvider(Protocol):
    def enumerate(self, inputs: list[Any], *, cursor: str | None = None, limit: int = 25) -> dict: ...


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _material_address_difference(submitted: str | None, canonical: str | None) -> bool:
    if not submitted or not canonical or not re.search(r"\d", submitted):
        return False
    aliases = {"street": "st", "road": "rd", "drive": "dr", "avenue": "ave", "boulevard": "blvd", "highway": "hwy"}
    def normalize(value: str) -> list[str]:
        return [aliases.get(token, token) for token in re.findall(r"[a-z0-9]+", value.casefold())]
    submitted_tokens, canonical_tokens = normalize(submitted), normalize(canonical)
    submitted_numbers = [token for token in submitted_tokens if token.isdigit()]
    canonical_numbers = [token for token in canonical_tokens if token.isdigit()]
    if submitted_numbers and canonical_numbers and submitted_numbers[0] != canonical_numbers[0]:
        return True
    submitted_set, canonical_set = set(submitted_tokens), set(canonical_tokens)
    return not (submitted_set <= canonical_set or canonical_set <= submitted_set)


def _requirement_gaps(constraints: list[dict], message: str) -> tuple[list[dict], list[dict], bool]:
    lower = message.casefold()
    assumptions_permitted = "reasonable assumptions" in lower or "reasonable defaults" in lower
    if any(
        phrase in lower for phrase in ("do not use reasonable defaults", "don't use reasonable defaults", "without reasonable defaults")
    ):
        assumptions_permitted = False
    gaps = []
    for constraint in constraints:
        constraint_id = constraint["constraint_id"]
        targets = REQUIREMENT_GAP_TARGETS.get(constraint_id)
        if targets and not any(item["gap_id"] == constraint_id for item in gaps):
            gaps.append({
                "gap_id": constraint_id,
                "original_constraint": copy.deepcopy(constraint),
                "candidate_constraint_ids": copy.deepcopy(targets),
                "originating_step": "requirement_compilation",
            })
    return constraints, gaps, assumptions_permitted


def _allowed_targets(gaps: list[dict]) -> set[str]:
    return {constraint_id for gap in gaps for constraint_id in gap["candidate_constraint_ids"]}


def _validate_constraint(value: Any, allowed_ids: set[str]) -> dict:
    if not isinstance(value, dict):
        raise DiligenceError("A generated constraint value must be an object.")
    constraint = {key: item for key, item in value.items() if item is not None}
    constraint_id = constraint.get("constraint_id")
    if constraint_id not in allowed_ids or constraint_id not in CONSTRAINT_CAPABILITIES:
        raise DiligenceError("The generated decision targets an unknown or unrelated constraint_id.")
    schema = CONSTRAINT_CAPABILITIES[constraint_id]["input_schema"]
    properties = schema["properties"]
    unknown = set(constraint) - {"constraint_id", *properties}
    if unknown:
        raise DiligenceError(f"The generated constraint contains unsupported input field(s): {', '.join(sorted(unknown))}.")
    missing = [name for name in schema["required"] if name not in constraint]
    if missing:
        raise DiligenceError(f"The generated constraint is missing required input field(s): {', '.join(missing)}.")
    for name, item in constraint.items():
        if name == "constraint_id":
            continue
        field = properties[name]
        field_type = field["type"]
        valid = (
            field_type == "number" and isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item)
            or field_type == "boolean" and isinstance(item, bool)
            or field_type == "string" and isinstance(item, str) and bool(item.strip())
            or field_type == "string_list" and isinstance(item, list) and all(isinstance(entry, str) and entry.strip() for entry in item)
        )
        if not valid:
            raise DiligenceError(f"The generated value for {name} has the wrong type.")
        if field_type == "number" and not field.get("minimum", -math.inf) <= float(item) <= field.get("maximum", math.inf):
            raise DiligenceError(f"The generated value for {name} is outside the supported range.")
        if field_type == "string_list" and not field.get("min_items", 0) <= len(item) <= field.get("max_items", math.inf):
            raise DiligenceError(f"The generated value for {name} has an unsupported item count.")
    if constraint_id == "parcel_acreage_range" and constraint["min_acres"] > constraint["max_acres"]:
        raise DiligenceError("The generated acreage minimum cannot exceed its maximum.")
    return constraint


def _validate_custom_schema(value: Any, targets: set[str], input_mode: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"constraint_id", "fields"}:
        raise DiligenceError("custom_schema must contain only constraint_id and fields.")
    constraint_id = value.get("constraint_id")
    if constraint_id not in targets:
        raise DiligenceError("custom_schema targets an unavailable constraint.")
    fields = value.get("fields")
    if not isinstance(fields, list) or not fields:
        raise DiligenceError("custom_schema requires at least one field.")
    registry_fields = CONSTRAINT_CAPABILITIES[constraint_id]["input_schema"]["properties"]
    normalized = []
    for item in fields:
        if not isinstance(item, dict) or set(item) - {"name", "label", "type", "unit", "minimum", "maximum"}:
            raise DiligenceError("custom_schema contains unsupported field metadata.")
        name = item.get("name")
        capability = registry_fields.get(name)
        if capability is None or item.get("type") != capability["type"] or item.get("unit") != capability.get("unit"):
            raise DiligenceError("custom_schema field type or unit does not match the capability registry.")
        if not isinstance(item.get("label"), str) or not item["label"].strip():
            raise DiligenceError("custom_schema fields require a generated label.")
        minimum = item.get("minimum", capability.get("minimum"))
        maximum = item.get("maximum", capability.get("maximum"))
        if capability["type"] == "number" and (
            minimum is not None and minimum < capability.get("minimum", -math.inf)
            or maximum is not None and maximum > capability.get("maximum", math.inf)
            or minimum is not None and maximum is not None and minimum > maximum
        ):
            raise DiligenceError("custom_schema numeric bounds exceed the capability registry.")
        normalized.append({"name": name, "label": item["label"].strip(), "type": item["type"], "unit": item.get("unit"), "minimum": minimum, "maximum": maximum})
    expected_types = {
        "number": len(normalized) == 1 and normalized[0]["type"] == "number",
        "range": len(normalized) == 2 and all(item["type"] == "number" for item in normalized),
        "text": len(normalized) == 1 and normalized[0]["type"] in {"string", "string_list"},
    }
    if input_mode in expected_types and not expected_types[input_mode]:
        raise DiligenceError("custom_schema does not match the generated input_mode.")
    return {"constraint_id": constraint_id, "fields": normalized}


def _validate_model_decision(value: Any, gaps: list[dict]) -> tuple[dict, list[str]]:
    if not isinstance(value, dict):
        raise DiligenceError("ASK_USER requires a generated DecisionRequest object.")
    required = {
        "kind", "question", "context", "why_it_matters", "risk_level", "blocking", "input_mode", "options",
        "recommended_option_id", "allow_custom", "custom_schema", "constraint_targets",
    }
    if set(value) != required:
        raise DiligenceError("The generated DecisionRequest has missing or unsupported fields.")
    if value["kind"] not in {"clarification", "assumption"} or value["risk_level"] not in {"LOW", "MEDIUM", "HIGH"}:
        raise DiligenceError("The generated DecisionRequest kind or risk level is invalid.")
    if value["blocking"] is not False:
        raise DiligenceError("The model cannot create an application-controlled hard block.")
    input_mode = value["input_mode"]
    if input_mode not in {"single_choice", "multi_choice", "number", "range", "text", "boolean", "confirmation"}:
        raise DiligenceError("The generated DecisionRequest input_mode is unsupported.")
    for name in ("question", "context", "why_it_matters"):
        if not isinstance(value[name], str) or not value[name].strip() or len(value[name]) > 1200:
            raise DiligenceError(f"The generated DecisionRequest {name} is invalid.")
    if not isinstance(value["allow_custom"], bool) or not isinstance(value["constraint_targets"], list):
        raise DiligenceError("The generated DecisionRequest flags or targets are invalid.")
    available = _allowed_targets(gaps)
    targets = list(dict.fromkeys(value["constraint_targets"]))
    if not targets or any(not isinstance(item, str) or item not in available for item in targets):
        raise DiligenceError("The generated DecisionRequest targets an unavailable constraint capability.")
    options = value["options"]
    if not isinstance(options, list) or len(options) > 12:
        raise DiligenceError("The generated DecisionRequest options are invalid.")
    normalized_options, option_ids = [], set()
    for option in options:
        if not isinstance(option, dict) or set(option) != {"id", "label", "description", "value", "consequence"}:
            raise DiligenceError("A generated decision option has missing or unsupported fields.")
        option_id = option.get("id")
        if not isinstance(option_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", option_id) or option_id in option_ids:
            raise DiligenceError("Generated decision option IDs must be unique safe identifiers.")
        if any(not isinstance(option[name], str) or not option[name].strip() or len(option[name]) > 600 for name in ("label", "description", "consequence")):
            raise DiligenceError("Generated decision option text is invalid.")
        normalized_options.append({
            **option,
            "label": option["label"].strip(), "description": option["description"].strip(), "consequence": option["consequence"].strip(),
            "value": _validate_constraint(option["value"], set(targets)),
        })
        option_ids.add(option_id)
    if input_mode in {"single_choice", "multi_choice", "boolean", "confirmation"} and not normalized_options:
        raise DiligenceError("The generated interaction mode requires options.")
    recommendation = value["recommended_option_id"]
    if recommendation is not None and recommendation not in option_ids:
        raise DiligenceError("The generated recommendation is not one of the generated options.")
    custom_schema = _validate_custom_schema(value["custom_schema"], set(targets), input_mode)
    if value["allow_custom"] != (custom_schema is not None):
        raise DiligenceError("allow_custom and custom_schema must agree.")
    if input_mode in {"number", "range", "text"} and custom_schema is None:
        raise DiligenceError("The generated input mode requires a validated custom_schema.")
    relevant_gaps = [gap["gap_id"] for gap in gaps if set(gap["candidate_constraint_ids"]) & set(targets)]
    return {
        "kind": value["kind"], "question": value["question"].strip(), "context": value["context"].strip(),
        "why_it_matters": value["why_it_matters"].strip(), "risk_level": value["risk_level"], "blocking": False,
        "input_mode": input_mode, "options": normalized_options, "recommended_option_id": recommendation,
        "allow_custom": value["allow_custom"], "custom_schema": custom_schema, "constraint_targets": targets,
        "originating_step": "requirement_compilation",
    }, relevant_gaps


def _candidate_id(raw: Any, ordinal: int) -> str:
    digest = hashlib.sha256(f"{ordinal}:{_canonical(raw)}".encode("utf-8")).hexdigest()[:20]
    return f"candidate_{digest}"


class UserSuppliedCandidateProvider:
    """Enumerate only candidates explicitly supplied by the user."""

    def enumerate(self, inputs: list[Any], *, cursor: str | None = None, limit: int = 25) -> dict:
        if not isinstance(inputs, list) or not inputs:
            raise DiligenceError("Provide at least one candidate address, coordinate, or APN.")
        if len(inputs) > MAX_CANDIDATES:
            raise DiligenceError(f"A candidate list may contain at most {MAX_CANDIDATES} entries.")
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise DiligenceError("Candidate page limit must be from 1 to 100.")
        try:
            start = int(cursor or 0)
        except (TypeError, ValueError) as exc:
            raise DiligenceError("Candidate cursor is invalid.") from exc
        records = [self._record(raw, ordinal) for ordinal, raw in enumerate(inputs)]
        page = records[start:start + limit]
        next_cursor = str(start + limit) if start + limit < len(records) else None
        return {"items": page, "next_cursor": next_cursor, "total": len(records), "source": "user_supplied"}

    @staticmethod
    def _record(raw: Any, ordinal: int) -> dict:
        record = {
            "candidate_id": _candidate_id(raw, ordinal), "raw_input": copy.deepcopy(raw),
            "input_type": None, "address": None, "coordinate": None, "apn": None,
            "source": "user_supplied", "source_metadata": {"ordinal": ordinal},
            "reconciliation_status": "PENDING", "resolution_options": [], "selected_location": None,
            "snapshot_id": None, "site_id": None, "evaluation": None, "error": None,
        }
        if isinstance(raw, dict):
            record["source"] = str(raw.get("source") or "user_supplied")
            record["source_metadata"].update(copy.deepcopy(raw.get("source_metadata") or {}))
            if raw.get("lat") is not None or raw.get("lng") is not None:
                if raw.get("lat") is None or raw.get("lng") is None:
                    raise DiligenceError("Candidate coordinates require both lat and lng.")
                record["input_type"] = "coordinate"
                record["coordinate"] = {"lat": float(raw["lat"]), "lng": float(raw["lng"])}
            elif raw.get("apn"):
                record["input_type"], record["apn"] = "apn", str(raw["apn"]).strip()
            elif raw.get("address"):
                record["input_type"], record["address"] = "address", str(raw["address"]).strip()
            else:
                raise DiligenceError("Candidate objects require address, APN, or lat/lng.")
        elif isinstance(raw, str) and raw.strip():
            value = raw.strip()
            coordinate = re.fullmatch(r"\s*(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*", value)
            if coordinate:
                record["input_type"] = "coordinate"
                record["coordinate"] = {"lat": float(coordinate.group(1)), "lng": float(coordinate.group(2))}
            elif re.match(r"^apn\s*[:#]", value, re.IGNORECASE):
                record["input_type"], record["apn"] = "apn", re.sub(r"^apn\s*[:#]\s*", "", value, flags=re.IGNORECASE)
            elif re.match(r"^https?://", value, re.IGNORECASE):
                record["input_type"], record["reconciliation_status"] = "url", "UNSUPPORTED"
                record["error"] = "Candidate URLs are not parsed in this MVP; provide an address, coordinate, or APN."
            else:
                record["input_type"], record["address"] = "address", value
        else:
            raise DiligenceError("Each candidate must be an address, coordinate, APN, or typed object.")
        coordinate = record.get("coordinate")
        if coordinate and not (-90 <= coordinate["lat"] <= 90 and -180 <= coordinate["lng"] <= 180):
            raise DiligenceError("Candidate coordinates are outside valid latitude/longitude bounds.")
        return record


def compile_project_request(message: str) -> dict:
    compiled = compile_request(message)
    text = compiled["message"]
    lower = text.lower()
    constraints = copy.deepcopy(compiled["constraints"])
    if "road proximity" in lower and "legal access" not in lower:
        constraints = [
            {"constraint_id": "road_proximity"}
            if item["constraint_id"] == "legal_access" else item
            for item in constraints
        ]
    if "resolution point" in lower or "point-scoped" in lower:
        constraints = [
            {"constraint_id": "resolution_point_outside_fema_sfha"}
            if item["constraint_id"] == "parcel_outside_fema_sfha" else item
            for item in constraints
        ]
    point_slope = re.search(r"(?:resolution[- ]point|point[- ]scoped)\s+slope\s*(?:<|under|below|max(?:imum)?)?\s*(\d+(?:\.\d+)?)", lower)
    if point_slope:
        constraints.append({"constraint_id": "max_resolution_point_slope_degrees", "max_degrees": float(point_slope.group(1))})
    elif "slope" in lower:
        constraints.append({"constraint_id": "max_slope_degrees"})
    zoning_codes = re.search(r"(?:raw\s+)?zoning\s+codes?\s*(?:are|:|=|in)?\s*([a-z0-9-]+(?:\s*(?:,|or)\s*[a-z0-9-]+)+)", lower)
    if zoning_codes:
        allowed = [value.strip().upper() for value in re.split(r"\s*(?:,|or)\s*", zoning_codes.group(1)) if value.strip() and value.strip().casefold() not in {"and", "with", "for"}]
        constraints = [item for item in constraints if item["constraint_id"] != "industrial_zoning"]
        constraints.append({"constraint_id": "parcel_zoning_code_in", "allowed_codes": allowed})
    if any(phrase in lower for phrase in ("water capacity", "water availability", "water service capacity")):
        constraints.append({"constraint_id": "water_capacity"})
    if any(phrase in lower for phrase in ("fiber diversity", "diverse fiber", "redundant fiber routes")):
        constraints.append({"constraint_id": "fiber_diversity"})
    if compiled.get("project") == "Data center" and isinstance(compiled.get("capacity_mw"), (int, float)):
        if not any(item["constraint_id"] == "sufficient_grid_capacity" for item in constraints):
            constraints.append({"constraint_id": "sufficient_grid_capacity"})
        if not any(item["constraint_id"] in {"data_center_entitlement", "industrial_zoning"} for item in constraints):
            constraints.append({"constraint_id": "data_center_entitlement"})
    requested_context = (
        ("land_size_context", "land size" in lower or "acreage" in lower, {"parcel_acreage_range"}),
        ("wetland_context", "wetland" in lower, {"max_nwi_wetland_fraction_of_parcel", "max_nwi_wetland_acres_on_parcel"}),
        ("terrain_context", "terrain" in lower, {"max_resolution_point_slope_degrees", "max_slope_degrees"}),
        ("zoning_context", "zoning" in lower, {"parcel_zoning_code_in", "industrial_zoning"}),
    )
    existing_ids = {item["constraint_id"] for item in constraints}
    for constraint_id, requested, specific_ids in requested_context:
        if requested and existing_ids.isdisjoint(specific_ids):
            constraints.append({"constraint_id": constraint_id})
    deduped = []
    for item in constraints:
        if item not in deduped:
            deduped.append(item)
    deduped, requirement_gaps, assumptions_permitted = _requirement_gaps(deduped, text)
    expansion = re.search(r"(?:expand(?:able)?\s+(?:to|target)|expansion(?:\s+target)?(?:\s+of|\s*:)?)[^\d]{0,20}(\d+(?:\.\d+)?)\s*mw", lower)
    energization = re.search(r"(?:target\s+)?energization\s+date\s*[:=]?\s*(\d{4}-\d{2}-\d{2})", lower)
    reliability = next((value for phrase, value in (("n+1", "N+1"), ("n+2", "N+2"), ("tier iv", "Tier IV"), ("tier iii", "Tier III")) if phrase in lower), None)
    redundancy = next((value for phrase, value in (("dual feed", "dual feed"), ("two independent feeds", "two independent feeds"), ("redundant feed", "redundant feed")) if phrase in lower), None)
    load_profile = [value for phrase, value in (("24/7", "24/7"), ("24x7", "24/7"), ("constant load", "constant load")) if phrase in lower]
    return {
        **compiled,
        "constraints": deduped,
        "supported_constraints": [item for item in deduped if item["constraint_id"] in SUPPORTED_CONSTRAINTS],
        "unresolved_constraints": [item for item in deduped if item["constraint_id"] not in SUPPORTED_CONSTRAINTS],
        "requirement_status": "REVIEW_REQUIRED" if requirement_gaps else "READY",
        "requirement_gaps": requirement_gaps,
        "assumptions_permitted": assumptions_permitted,
        "power_requirements": {
            "phase_1_mw": compiled.get("capacity_mw"),
            "expansion_mw": float(expansion.group(1)) if expansion else None,
            "target_energization_date": energization.group(1) if energization else None,
            "reliability_requirement": reliability, "redundancy_requirement": redundancy,
            "load_profile_characteristics": list(dict.fromkeys(load_profile)),
        },
        "compiler_version": "diligence_constraints_v3",
    }


class DiligenceService:
    def __init__(self, store: WorkspaceStore, sandbox: SiteSnapshotService, worlds: Any | None = None, provider: CandidateProvider | None = None, sources: AuthoritativeSourceService | None = None):
        self.store, self.sandbox, self.worlds = store, sandbox, worlds
        self.provider = provider or UserSuppliedCandidateProvider()
        self.sources = sources

    def create_project(self, *, workspace_id: str, message: str, candidates: list[Any]) -> dict:
        if not isinstance(message, str) or not message.strip():
            raise DiligenceError("Project request must not be empty.")
        page = self.provider.enumerate(candidates, limit=min(MAX_CANDIDATES, len(candidates)))
        now = time.time()
        request = compile_project_request(message)
        project = {
            "project_id": f"project_{uuid.uuid4().hex}", "workspace_id": workspace_id,
            "status": "CANDIDATES_SUPPLIED", "request": request,
            "candidates": page["items"], "candidate_count": page["total"], "requested_fields": [],
            "evidence_plan": None, "spend_plan": None, "metered_operations": [], "ranking": [],
            "decision": self._decision(request, []),
            "agent_state": {"status": "RUNNING", "step": "requirement_planning", "resume_count": 0, "transitions": [{"status": "RUNNING", "at": now}]},
            "active_decision": None, "decision_history": [], "assumptions": [],
            "active_candidate_id": None, "project_intelligence": None, "rfis": [],
            "external_evidence_by_site": {}, "power_readiness_by_site": {}, "entitlement_by_site": {},
            "watch": {
                "watch_id": f"watch_{uuid.uuid4().hex}", "project_id": None, "site_id": None,
                "source": "MIREYE", "fields": [], "layers": [], "cadence_policy": "MANUAL",
                "last_checked_at": None, "last_known_state": None, "candidate_states": [],
                "enabled": False, "cost_policy": {"metered_refresh": "EXPLICIT_CONFIRMATION_REQUIRED"},
            },
            "created_at": now, "updated_at": now,
        }
        project["watch"]["project_id"] = project["project_id"]
        self.store.create_workspace(workspace_id, "Site diligence")
        self._update_project_intelligence(project)
        return self._save(project)

    def get(self, project_id: str) -> dict:
        project = self.store.get_diligence_project(project_id)
        if project is None:
            raise DiligenceError("Diligence project was not found.")
        return project

    def candidate_page(self, project_id: str, *, cursor: str | None = None, limit: int = 25) -> dict:
        project = self.get(project_id)
        try:
            start = int(cursor or 0)
        except (TypeError, ValueError) as exc:
            raise DiligenceError("Candidate cursor is invalid.") from exc
        if not 1 <= limit <= 100:
            raise DiligenceError("Candidate page limit must be from 1 to 100.")
        items = project["candidates"][start:start + limit]
        return {"items": items, "total": len(project["candidates"]), "next_cursor": str(start + limit) if start + limit < len(project["candidates"]) else None}

    @staticmethod
    def discovery_capabilities() -> dict:
        return {
            "provider": "user_supplied", "supported_inputs": ["address", "coordinate", "apn"],
            "statewide_inverse_search": False, "synthetic_screen_used": False,
            "message": "Candidates must be supplied by the customer; MIREYE resolves and enriches them.",
        }

    def requirement_context(self, project_id: str) -> dict:
        project = self.get(project_id)
        gaps = project["request"].get("requirement_gaps", [])
        capability_ids = _allowed_targets(gaps)
        return {
            "original_request": project["request"]["message"],
            "constraint_spec": copy.deepcopy(project["request"]["constraints"]),
            "requirement_gaps": copy.deepcopy(gaps),
            "constraint_capabilities": {key: copy.deepcopy(CONSTRAINT_CAPABILITIES[key]) for key in sorted(capability_ids)},
            "completed_decisions": copy.deepcopy(project["decision_history"]),
            "assumptions": copy.deepcopy(project["assumptions"]),
            "assumptions_permitted": project["request"].get("assumptions_permitted", False),
            "candidate_context": {"count": project["candidate_count"], "states": [item["reconciliation_status"] for item in project["candidates"]]},
            "workflow": copy.deepcopy(project["agent_state"]),
            "spend_plan": copy.deepcopy(project.get("spend_plan")),
            "project_intelligence": copy.deepcopy(project.get("project_intelligence")),
            "recent_changes": self.changes(project_id, limit=10)["items"],
        }

    def agent_decision(
        self, project_id: str, *, mode: str, decision_request: dict | None = None,
        assumptions: list[dict] | None = None,
    ) -> dict:
        project = self.get(project_id)
        if project.get("active_decision"):
            return {"mode": "ASK_USER", "interrupted": True, "decision_request": project["active_decision"]}
        gaps = project["request"].get("requirement_gaps", [])
        if mode == "AUTO_CONTINUE":
            if gaps:
                raise DiligenceError("AUTO_CONTINUE is unavailable while important requirement gaps remain.")
            project["request"]["requirement_status"] = "READY"
            self._transition(project, "RUNNING", "candidate_resolution")
            return {"mode": mode, "interrupted": False, "project": self._save(project)}
        if mode == "ASSUME_AND_CONTINUE":
            if not project["request"].get("assumptions_permitted") or not gaps:
                raise DiligenceError("ASSUME_AND_CONTINUE requires explicit user permission and an unresolved requirement gap.")
            validated = self._validate_assumptions(assumptions, gaps)
            self._apply_requirement_constraints(project, [item["constraint"] for item in validated], {
                "source": "AGENT_ASSUMPTION", "assumptions": validated,
            })
            project["assumptions"].extend(validated)
            self._transition(project, "RUNNING", "requirement_planning" if project["request"]["requirement_gaps"] else "candidate_resolution")
            return {"mode": mode, "interrupted": False, "assumptions": copy.deepcopy(validated), "project": self._save(project)}
        if mode not in {"ASK_USER", "HARD_BLOCK"}:
            raise DiligenceError("Unknown agent decision mode.")
        if mode == "HARD_BLOCK":
            raise DiligenceError("Only the application can create a hard-block DecisionRequest.")
        try:
            specification, gap_ids = _validate_model_decision(decision_request, gaps)
            resume_action = {"type": "requirement", "gap_ids": gap_ids}
        except DiligenceError:
            fallback = self._canonical_action_decision(project, decision_request)
            if fallback is None:
                raise
            specification, resume_action = fallback
        decision = self._interrupt(project, specification, resume_action)
        self._save(project)
        return {"mode": mode, "interrupted": True, "decision_request": decision}

    async def answer_decision(
        self, project_id: str, decision_id: str, *, resume_token: str, option_id: str | None = None,
        option_ids: list[str] | None = None, value: Any = None, interpreted_constraint: dict | None = None,
        cancelled: bool = False,
    ) -> dict:
        project = self.get(project_id)
        decision = project.get("active_decision")
        current_id = (decision or {}).get("decision_id") or (decision or {}).get("id")
        if not decision or current_id != decision_id or decision["resume_token"] != resume_token:
            raise DiligenceError("The decision request or resume token is invalid.")
        if decision.get("status") != "PENDING":
            raise DiligenceError("This decision has already been answered.")
        selected_options, constraints = self._decision_answer(
            decision, option_id=option_id, option_ids=option_ids, value=value,
            interpreted_constraint=interpreted_constraint, cancelled=cancelled,
        )
        resolved = copy.deepcopy(decision)
        resolved.update(
            status="CANCELLED" if cancelled else "ANSWERED", answered_at=time.time(), selected_option_id=option_id,
            selected_option_ids=option_ids, answer_value=copy.deepcopy(value), source="USER",
        )
        project["decision_history"].append(resolved)
        project["active_decision"] = None
        action = decision["resume_action"]
        if cancelled or any(option.get("action") == "cancel" for option in selected_options):
            project["status"] = "CANCELLED"
            self._transition(project, "CANCELLED", action["type"])
            return self._save(project)
        self._transition(project, "RESUMED", action["type"])
        if action["type"] == "requirement":
            self._apply_requirement_constraints(project, constraints, {
                "source": "USER", "decision_id": decision_id,
                "selected_option_ids": [item["id"] for item in selected_options], "answer_value": copy.deepcopy(value),
            })
            project["status"] = "CANDIDATES_SUPPLIED"
            return self._save(project)
        if action["type"] == "confirm_enrichment":
            plan = project.get("spend_plan") or {}
            if plan.get("spend_plan_id") != action["spend_plan_id"]:
                raise DiligenceError("The quoted spend plan changed before resume.")
            plan["approved_decision_id"] = decision_id
            self._transition(project, "RUNNING", "mireye_enrichment")
            self._save(project)
            return await self.confirm_and_fetch(project_id, plan["spend_plan_id"], confirmed=True)
        if action["type"] == "confirm_address":
            self._save(project)
            return self.confirm_canonical_address(project_id, action["candidate_id"], confirmed=selected_options[0]["id"] == "confirm")
        if action["type"] == "project_action":
            selected = selected_options[0]
            project.setdefault("action_decisions", []).append({
                "decision_id": decision_id,
                "gap_id": action["gap_id"],
                "action_id": action["action_id"],
                "selection": selected["id"],
                "status": "AUTHORIZED" if selected.get("action") == "authorize" else "DEFERRED",
                "created_at": time.time(),
            })
            project["status"] = (project.get("decision") or {}).get("status", "NO_DECISION_YET")
            return self._save(project)
        raise DiligenceError("The decision resume action is unsupported.")

    def _canonical_action_decision(self, project: dict, proposal: Any) -> tuple[dict, dict] | None:
        """Map model interaction text onto an application-owned EvidenceGap action."""
        if not isinstance(proposal, dict) or not isinstance(proposal.get("constraint_targets"), list):
            return None
        targets = list(dict.fromkeys(proposal["constraint_targets"]))
        if not targets or any(not isinstance(target, str) for target in targets):
            return None
        targets = [target for target in targets if target in CONSTRAINT_CAPABILITIES]
        if not targets:
            return None
        if any(
            value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 1200)
            for value in (proposal.get("question"), proposal.get("context"), proposal.get("why_it_matters"))
        ):
            return None
        self._update_project_intelligence(project)
        intelligence = project.get("project_intelligence") or {}
        gaps = [
            gap for gap in intelligence.get("evidence_gaps", [])
            if gap.get("status") != "RESOLVED" and gap.get("requirement_id") in targets
        ]
        if not gaps:
            return None
        for gap in gaps:
            capability = CONSTRAINT_CAPABILITIES[gap["requirement_id"]]
            if not capability.get("evaluator_support"):
                return None
            expected_scope = str(capability.get("spatial_scope") or "").upper()
            actual_scope = str(gap.get("evidence_scope") or "").upper()
            if expected_scope and actual_scope and expected_scope != actual_scope:
                return None
        actions = [
            action for action in intelligence.get("recommended_actions", [])
            if action.get("gap_id") in {gap["gap_id"] for gap in gaps}
        ]
        if not actions:
            return None
        action = actions[0]
        gap = next(item for item in gaps if item["gap_id"] == action["gap_id"])
        action_label = "Generate utility RFI" if action.get("type") in RFI_ACTION_TYPES else "Prepare next evidence action"
        specification = {
            "kind": "clarification",
            "question": proposal.get("question") or f"How should MIREYE proceed with {gap['title']}?",
            "context": proposal.get("context") or gap["description"],
            "why_it_matters": proposal.get("why_it_matters") or gap["why_it_matters"],
            "risk_level": proposal.get("risk_level") if proposal.get("risk_level") in {"LOW", "MEDIUM", "HIGH"} else {"CRITICAL": "HIGH"}.get(gap["impact"], gap["impact"]),
            "blocking": False,
            "input_mode": "single_choice",
            "options": [
                {
                    "id": "authorize_next_action", "label": action_label,
                    "description": action["title"], "value": None,
                    "consequence": "Record authorization to pursue the canonical missing evidence.", "action": "authorize",
                },
                {
                    "id": "keep_unresolved", "label": "Keep unresolved and continue",
                    "description": "Continue screening without treating the missing evidence as proven.", "value": None,
                    "consequence": f"{gap['title']} remains unresolved.", "action": "defer",
                },
            ],
            "recommended_option_id": "authorize_next_action",
            "allow_custom": False,
            "custom_schema": None,
            "constraint_targets": [gap["requirement_id"]],
            "originating_step": "project_intelligence",
        }
        return specification, {"type": "project_action", "gap_id": gap["gap_id"], "action_id": action["action_id"]}

    def plan_fields(self, project_id: str) -> dict:
        project = self.get(project_id)
        fields = list(IDENTITY_FIELDS)
        for constraint in project["request"]["constraints"]:
            fields.extend(CONSTRAINT_FIELDS.get(constraint["constraint_id"], ()))
        project["requested_fields"] = list(dict.fromkeys(fields))
        self._save(project)
        return {"project_id": project_id, "fields": project["requested_fields"], "field_count": len(project["requested_fields"]), "constraints": project["request"]["constraints"]}

    async def plan_project_evidence(self, project_id: str, *, requested_decision: str = "candidate_screening") -> dict:
        project = self.get(project_id)
        snapshot_id = next((item.get("snapshot_id") for item in project["candidates"] if item.get("snapshot_id")), None)
        plan = await self.sandbox.catalog_evidence_plan(
            project_type=project["request"].get("project") or "Site analysis",
            requirements=project["request"].get("constraints", []),
            unresolved_gaps=(project.get("project_intelligence") or {}).get("evidence_gaps", []),
            requested_decision=requested_decision,
            snapshot_id=snapshot_id,
        )
        project["evidence_plan"] = plan
        project["requested_fields"] = plan["fields"]
        self._save(project)
        return copy.deepcopy(plan)

    def evaluate_evidence_coverage(self, project_id: str) -> dict:
        project = self.get(project_id)
        self._update_project_intelligence(project)
        self._save(project)
        return copy.deepcopy(project["project_intelligence"])

    def next_actions(self, project_id: str) -> dict:
        intelligence = self.evaluate_evidence_coverage(project_id)
        return {
            "project_id": project_id, "prioritized_actions": intelligence["recommended_actions"],
            "project_readiness_state": intelligence["project_readiness_state"],
            "critical_blockers": intelligence["risk_state"]["critical_blockers"],
            "ranking_version": "project_next_action_priority_v1",
        }

    async def refresh_authoritative_sources(self, project_id: str, site_id: str) -> dict:
        project = self.get(project_id)
        candidate = next((item for item in project["candidates"] if item.get("site_id") == site_id and item.get("snapshot_id")), None)
        if candidate is None:
            raise DiligenceError("The requested site is not an enriched candidate in this project.")
        snapshot = self.store.get_site_snapshot(candidate["snapshot_id"])
        if snapshot is None:
            raise DiligenceError("The candidate SiteSnapshot is unavailable.")
        if self.sources is None:
            external = {
                "site_id": site_id, "collected_at": time.time(), "records": [],
                "sources": [{"provider": "Authoritative public sources", "availability": "UNAVAILABLE", "reason": "No authoritative source adapter is configured."}],
            }
        else:
            external = await self.sources.collect(project, snapshot)
        project.setdefault("external_evidence_by_site", {})[site_id] = external
        self._build_site_readiness(project, candidate)
        self._update_project_intelligence(project)
        self._save(project)
        return {
            "project_id": project_id, "site_id": site_id, "source_result": copy.deepcopy(external),
            "power_readiness": copy.deepcopy(project["power_readiness_by_site"][site_id]),
            "entitlement": copy.deepcopy(project["entitlement_by_site"][site_id]),
        }

    def power_readiness(self, project_id: str, site_id: str) -> dict:
        project = self.get(project_id)
        candidate = self._site_candidate(project, site_id)
        self._build_site_readiness(project, candidate)
        self._save(project)
        return copy.deepcopy(project.get("power_readiness_by_site", {}).get(site_id))

    def entitlement_state(self, project_id: str, site_id: str) -> dict:
        project = self.get(project_id)
        candidate = self._site_candidate(project, site_id)
        self._build_site_readiness(project, candidate)
        self._save(project)
        return copy.deepcopy(project.get("entitlement_by_site", {}).get(site_id))

    def create_rfi_draft(self, project_id: str, action_id: str, generated_request: str) -> dict:
        project = self.get(project_id)
        self._update_project_intelligence(project)
        action = next((item for item in project["project_intelligence"]["recommended_actions"] if item["action_id"] == action_id), None)
        if action is None or action["type"] not in RFI_ACTION_TYPES:
            raise DiligenceError("This next action does not support an RFI draft.")
        if not isinstance(generated_request, str) or not 40 <= len(generated_request.strip()) <= 8000:
            raise DiligenceError("The generated RFI must be a substantive request of at most 8,000 characters.")
        draft = {
            "rfi_id": f"rfi_{uuid.uuid4().hex}", "action_id": action_id, "type": action["type"],
            "recipient_category": action["recipient_category"], "project_id": project_id, "site_id": action["site_id"],
            "required_evidence": copy.deepcopy(action["required_evidence"]),
            "generated_request": generated_request.strip(), "dependencies": copy.deepcopy(action["dependencies"]),
            "structured_context": self._rfi_context(project, action),
            "status": "DRAFT", "human_approval_required": True, "created_at": time.time(),
        }
        project.setdefault("rfis", []).append(draft)
        for item in project["project_intelligence"]["recommended_actions"]:
            if item["action_id"] == action_id:
                item["status"] = "DRAFTED"
                item["rfi_id"] = draft["rfi_id"]
        self._save(project)
        return copy.deepcopy(draft)

    @staticmethod
    def _find_metered_operation(project: dict, operation_type: str, request: dict) -> dict | None:
        request_hash = hashlib.sha256(_canonical(request).encode("utf-8")).hexdigest()
        return next(
            (
                item for item in reversed(project.get("metered_operations", []))
                if item.get("operation_type") == operation_type and item.get("request_hash") == request_hash
            ),
            None,
        )

    def _begin_metered_operation(
        self,
        project: dict,
        operation_type: str,
        request: dict,
        *,
        confirmed: bool,
        retry_reason: str | None,
        status: str = "EXECUTING",
    ) -> dict:
        existing = self._find_metered_operation(project, operation_type, request)
        if existing and existing.get("status") == "SUCCEEDED":
            return existing
        if existing:
            if existing.get("status") == "QUOTED":
                return existing
            if existing.get("status") == "EXECUTING":
                raise DiligenceError("An equivalent paid operation is already executing; duplicate execution was blocked.")
            if not retry_reason or len(retry_reason.strip()) < 8:
                raise DiligenceError("Retrying a failed paid operation requires an explicit reason.")
            if int(existing.get("attempts", 1)) >= 2:
                raise DiligenceError("The paid operation retry limit has been reached.")
            existing.update(
                status=status, attempts=int(existing.get("attempts", 1)) + 1,
                retry_reason=retry_reason.strip(), started_at=time.time(), completed_at=None, error=None,
            )
            return existing
        request_hash = hashlib.sha256(_canonical(request).encode("utf-8")).hexdigest()
        operation = {
            "operation_id": f"mop_{request_hash[:24]}", "operation_type": operation_type,
            "request_hash": request_hash, "request": copy.deepcopy(request), "project_id": project["project_id"],
            "run_id": None, "task_id": None, "status": status, "attempts": 1,
            "quote": None, "quoted_credits": "UNKNOWN", "charged_credits": "UNKNOWN",
            "confirmation_required": True, "confirmed_at": time.time() if confirmed else None,
            "provider_request_ids": [], "result": None, "error": None,
            "started_at": time.time(), "completed_at": None,
        }
        project.setdefault("metered_operations", []).append(operation)
        return operation

    async def resolve_and_quote(self, project_id: str, *, confirmed_resolution: bool, retry_reason: str | None = None) -> dict:
        project = self.get(project_id)
        if project["request"]["requirement_status"] != "READY":
            project["status"] = "NEEDS_USER_DECISION"
            project["decision"] = self._decision(project["request"], [])
            return self._save(project)
        if not confirmed_resolution:
            raise ConfirmationRequired("Candidate resolution requires explicit application confirmation.")
        evidence_plan = await self.plan_project_evidence(project_id)
        planned_fields = self.plan_fields(project_id)["fields"]
        project = self.get(project_id)
        fields = planned_fields
        for candidate in project["candidates"]:
            if candidate["reconciliation_status"] == "ENRICHMENT_FAILED" and candidate.get("selected_location"):
                if not retry_reason or len(retry_reason.strip()) < 8:
                    raise DiligenceError("Retrying a failed paid operation requires an explicit reason.")
                candidate.update(reconciliation_status="RESOLVED", error=None)
                continue
            if candidate["reconciliation_status"] in {"UNSUPPORTED", "AMBIGUOUS", "RESOLVED", "ENRICHED"}:
                continue
            request = {
                "candidate_id": candidate["candidate_id"], "input_type": candidate["input_type"],
                "value": candidate.get("coordinate") or candidate.get("apn") or candidate.get("address"),
            }
            operation = self._begin_metered_operation(
                project, "MIREYE_RESOLVE", request, confirmed=True, retry_reason=retry_reason,
            )
            if operation["status"] == "SUCCEEDED":
                candidate.update(copy.deepcopy(operation["result"]))
                continue
            self._save(project)
            try:
                if candidate["input_type"] == "coordinate":
                    resolved = await self.sandbox.resolve(**candidate["coordinate"])
                else:
                    value = candidate["apn"] if candidate["input_type"] == "apn" else candidate["address"]
                    resolved = await self.sandbox.resolve(input=value, kind=candidate["input_type"])
                if resolved["status"] == "resolved":
                    candidate["selected_location"] = resolved["candidates"][0]
                    candidate["reconciliation_status"] = "RESOLVED"
                elif resolved["status"] == "ambiguous":
                    candidate["resolution_options"] = resolved["candidates"]
                    candidate["reconciliation_status"] = "AMBIGUOUS"
                else:
                    candidate["reconciliation_status"], candidate["error"] = "NOT_FOUND", "MIREYE could not resolve this candidate."
                operation.update(
                    status="SUCCEEDED", result={
                        key: copy.deepcopy(candidate.get(key))
                        for key in ("selected_location", "resolution_options", "reconciliation_status", "error")
                    }, completed_at=time.time(),
                )
            except Exception as exc:
                candidate["reconciliation_status"], candidate["error"] = "ERROR", str(exc)
                operation.update(status="FAILED", error=str(exc), completed_at=time.time())
            self._save(project)
        resolved_candidates = [item for item in project["candidates"] if item["reconciliation_status"] == "RESOLVED"]
        if not resolved_candidates:
            project["status"] = "RESOLUTION_REQUIRED"
            return self._save(project)
        selected_fields, catalog = await self.sandbox.select_fields(fields)
        preset = None
        fetch_fields = selected_fields
        operation_request = {
            "candidate_ids": [item["candidate_id"] for item in resolved_candidates],
            "locations": [
                {"lat": item["selected_location"]["lat"], "lng": item["selected_location"]["lng"]}
                for item in resolved_candidates
            ],
            "fields": fetch_fields, "preset": preset,
        }
        existing = self._find_metered_operation(project, "MIREYE_FETCH", operation_request)
        if existing and existing["status"] == "SUCCEEDED":
            return self._save(project)
        if existing and existing["status"] == "QUOTED" and project.get("spend_plan", {}).get("operation_id") == existing["operation_id"]:
            return self._save(project)
        if existing and existing["status"] in {"FAILED", "PARTIAL"} and (not retry_reason or len(retry_reason.strip()) < 8):
            raise DiligenceError("Retrying a failed paid operation requires an explicit reason.")
        provider_quotes, estimates = [], []
        for offset in range(0, len(resolved_candidates), BATCH_SIZE):
            count = len(resolved_candidates[offset:offset + BATCH_SIZE])
            quote = await self.sandbox.client.fetch_quote(locations=count, fields=fetch_fields or None, preset=preset)
            provider_quotes.append({"location_count": count, "quote": quote})
            credits = self.sandbox._estimated_credits(quote)
            if isinstance(credits, (int, float)):
                estimates.append(float(credits))
        now = time.time()
        quote_expiries = [self._quote_expiry(item["quote"], now + QUOTE_TTL_SECONDS) for item in provider_quotes]
        plan = {
            "spend_plan_id": f"spend_{uuid.uuid4().hex}", "project_id": project_id,
            "status": "QUOTED", "requested_fields": selected_fields,
            "fetch_fields": fetch_fields, "preset": preset,
            "field_manifest": [item for item in evidence_plan["field_manifest"] if item["field"] in selected_fields],
            "candidate_count": len(resolved_candidates), "batch_strategy": {"max_batch_size": BATCH_SIZE, "batch_count": len(provider_quotes)},
            "provider_quotes": provider_quotes, "expected_credits": sum(estimates) if len(estimates) == len(provider_quotes) else None,
            "provider_quote_ids": [item["quote"].get("quote_id") or item["quote"].get("id") for item in provider_quotes],
            "field_catalog_version": self.sandbox._catalog_version(catalog),
            "quote_expires_at": min(quote_expiries),
            "cache_hits": {"candidate_count": 0, "field_count": 0},
            "freshness_reason": "initial_candidate_enrichment", "evidence_plan_catalog_version": evidence_plan["catalog_version"],
            "workspace_budget_impact": {"policy": "explicit_application_confirmation_required", "estimated_credits": sum(estimates) if len(estimates) == len(provider_quotes) else None},
            "confirmation_required": True, "created_at": now,
        }
        operation = self._begin_metered_operation(
            project, "MIREYE_FETCH", operation_request, confirmed=False, retry_reason=retry_reason, status="QUOTED",
        )
        operation.update(
            quote=copy.deepcopy(provider_quotes), quoted_credits=plan["expected_credits"],
            provider_request_ids=copy.deepcopy(plan["provider_quote_ids"]),
        )
        plan["operation_id"] = operation["operation_id"]
        project.update(status="AWAITING_ENRICHMENT_APPROVAL", requested_fields=selected_fields, spend_plan=plan)
        credits = plan["expected_credits"]
        cost = "an unknown number of" if credits is None else f"about {credits:g}"
        self._interrupt(project, {
            "kind": "confirmation",
            "question": f"Continue with MIREYE enrichment for {cost} credits?",
            "context": f"MIREYE will check {len(selected_fields)} fields across {len(resolved_candidates)} supplied candidate(s).",
            "why_it_matters": "This is a metered operation and cannot run without explicit approval.",
            "options": [
                {"id": "continue", "label": "Continue", "description": "Approve this quoted operation.", "value": None, "consequence": "Run the confirmed MIREYE enrichment using the existing quote."},
                {"id": "cancel", "label": "Cancel", "description": "Keep the current project without enrichment.", "value": None, "consequence": "Do not spend credits or fetch candidate evidence.", "action": "cancel"},
            ],
            "recommended_option_id": None, "risk_level": "HIGH", "blocking": True, "input_mode": "confirmation",
            "allow_custom": False, "custom_schema": None, "constraint_targets": [], "originating_step": "mireye_enrichment",
        }, {"type": "confirm_enrichment", "spend_plan_id": plan["spend_plan_id"]})
        return self._save(project)

    async def resolve_candidate(self, project_id: str, candidate_id: str, *, confirmed_resolution: bool) -> dict:
        if not confirmed_resolution:
            raise ConfirmationRequired("Candidate resolution requires explicit application confirmation.")
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        if candidate["reconciliation_status"] == "UNSUPPORTED":
            return copy.deepcopy(candidate)
        request = {
            "candidate_id": candidate_id, "input_type": candidate["input_type"],
            "value": candidate.get("coordinate") or candidate.get("apn") or candidate.get("address"),
        }
        operation = self._begin_metered_operation(project, "MIREYE_RESOLVE", request, confirmed=True, retry_reason=None)
        if operation["status"] == "SUCCEEDED":
            candidate.update(copy.deepcopy(operation["result"]))
            return copy.deepcopy(candidate)
        self._save(project)
        try:
            if candidate["input_type"] == "coordinate":
                resolved = await self.sandbox.resolve(**candidate["coordinate"])
            else:
                value = candidate["apn"] if candidate["input_type"] == "apn" else candidate["address"]
                resolved = await self.sandbox.resolve(input=value, kind=candidate["input_type"])
        except Exception as exc:
            operation.update(status="FAILED", error=str(exc), completed_at=time.time())
            self._save(project)
            raise
        if resolved["status"] == "resolved":
            candidate.update(selected_location=resolved["candidates"][0], reconciliation_status="RESOLVED", error=None)
        elif resolved["status"] == "ambiguous":
            candidate.update(resolution_options=resolved["candidates"], reconciliation_status="AMBIGUOUS")
        else:
            candidate.update(reconciliation_status="NOT_FOUND", error="MIREYE could not resolve this candidate.")
        operation.update(
            status="SUCCEEDED", completed_at=time.time(),
            result={key: copy.deepcopy(candidate.get(key)) for key in ("selected_location", "resolution_options", "reconciliation_status", "error")},
        )
        self._save(project)
        return copy.deepcopy(candidate)

    async def select_resolution(self, project_id: str, candidate_id: str, option_index: int) -> dict:
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        options = candidate.get("resolution_options") or []
        if candidate.get("reconciliation_status") != "AMBIGUOUS" or not 0 <= option_index < len(options):
            raise DiligenceError("That candidate resolution option is unavailable.")
        candidate.update(selected_location=options[option_index], reconciliation_status="RESOLVED", error=None)
        project["spend_plan"] = None
        project["status"] = "CANDIDATES_RESOLVED"
        self._save(project)
        return await self.resolve_and_quote(project_id, confirmed_resolution=True)

    async def confirm_and_fetch(self, project_id: str, spend_plan_id: str, *, confirmed: bool) -> dict:
        if not confirmed:
            raise ConfirmationRequired("MIREYE enrichment requires explicit application confirmation.")
        project = self.get(project_id)
        plan = project.get("spend_plan") or {}
        if plan.get("spend_plan_id") != spend_plan_id or plan.get("status") != "QUOTED":
            raise DiligenceError("MIREYE enrichment spend plan is unavailable or already used.")
        if not plan.get("approved_decision_id"):
            raise ConfirmationRequired("MIREYE enrichment requires an answered cost DecisionRequest.")
        if time.time() >= float(plan["quote_expires_at"]):
            raise ConfirmationRequired("MIREYE enrichment quote has expired; prepare a new spend plan.")
        operation = next(
            (item for item in project.get("metered_operations", []) if item.get("operation_id") == plan.get("operation_id")),
            None,
        )
        if operation is None:
            raise DiligenceError("The metered operation record is unavailable.")
        if operation["status"] == "SUCCEEDED":
            return self._save(project)
        if operation["status"] != "QUOTED":
            raise DiligenceError("The paid operation is not eligible for execution.")
        operation.update(status="EXECUTING", confirmed_at=time.time(), confirmation_decision_id=plan["approved_decision_id"])
        self._save(project)
        fields, catalog = await self.sandbox.select_fields(plan["requested_fields"])
        fetch_fields, preset = plan.get("fetch_fields") or fields, plan.get("preset")
        resolved = [item for item in project["candidates"] if item["reconciliation_status"] == "RESOLVED"]
        known_charges: list[float] = []
        provider_request_ids = list(operation.get("provider_request_ids") or [])
        for offset in range(0, len(resolved), BATCH_SIZE):
            batch = resolved[offset:offset + BATCH_SIZE]
            locations = [{"lat": item["selected_location"]["lat"], "lng": item["selected_location"]["lng"]} for item in batch]
            try:
                payload = await self.sandbox.client.fetch_batch(locations=locations, fields=fetch_fields, preset=preset)
                request_id = payload.get("request_id") or payload.get("id")
                if request_id:
                    provider_request_ids.append(str(request_id))
                charge = next((payload.get(key) for key in ("credits_charged", "charged_credits", "actual_credits") if isinstance(payload.get(key), (int, float))), None)
                if isinstance(charge, (int, float)):
                    known_charges.append(float(charge))
                results = self._batch_results(payload)
            except Exception as exc:
                results = [{"ok": False, "error": {"message": str(exc)}} for _ in batch]
            for index, candidate in enumerate(batch):
                result = results[index] if index < len(results) else {"ok": False, "error": {"message": "MIREYE batch response omitted this candidate."}}
                try:
                    snapshot = self.sandbox.persist_dossier(
                        workspace_id=project["workspace_id"], lat=locations[index]["lat"], lng=locations[index]["lng"],
                        fields=fields, catalog=catalog, quote=plan, dossier=result,
                    )
                    canonical_address = snapshot["parcel_identity"].get("parcel_address")
                    if _material_address_difference(candidate.get("address"), canonical_address):
                        candidate.update(
                            reconciliation_status="ADDRESS_CONFIRMATION_REQUIRED",
                            snapshot_id=snapshot["snapshot_id"], site_id=snapshot.get("site_id"), evaluation=None, error=None,
                            address_reconciliation={
                                "submitted_address": candidate["address"], "canonical_address": canonical_address,
                                "parcel_id": snapshot["parcel_identity"].get("parcel_id"),
                                "match_type": snapshot["parcel_identity"].get("parcel_match_type"),
                                "match_distance_m": snapshot["parcel_identity"].get("parcel_match_distance_m"),
                                "status": "CONFIRMATION_REQUIRED",
                            },
                        )
                        self._queue_address_confirmation(project)
                        continue
                    self._finalize_candidate(project, candidate, snapshot)
                except (SandboxError, SceneValidationError, KeyError, TypeError, ValueError) as exc:
                    candidate.update(reconciliation_status="ENRICHMENT_FAILED", error=str(exc), evaluation=None)
        failures = [item["candidate_id"] for item in resolved if item.get("reconciliation_status") == "ENRICHMENT_FAILED"]
        operation.update(
            status="PARTIAL" if failures else "SUCCEEDED", completed_at=time.time(),
            charged_credits=sum(known_charges) if known_charges else "UNKNOWN",
            provider_request_ids=list(dict.fromkeys(provider_request_ids)),
            result={"snapshot_ids": [item.get("snapshot_id") for item in resolved if item.get("snapshot_id")], "failed_candidate_ids": failures},
        )
        plan["status"] = "COMPLETED"
        plan["completed_at"] = time.time()
        project["ranking"] = self._rank(project["candidates"])
        project["decision"] = self._decision(project["request"], project["ranking"])
        if not project.get("active_decision"):
            project["status"] = "EVALUATED" if project["decision"]["status"] == "DECISION_READY" else project["decision"]["status"]
        self._update_project_intelligence(project)
        return self._save(project)

    def rank_candidates(self, project_id: str) -> dict:
        project = self.get(project_id)
        if project["request"]["requirement_status"] != "READY":
            project["ranking"] = []
            project["decision"] = self._decision(project["request"], [])
            project["status"] = "NEEDS_USER_DECISION" if project.get("active_decision") else "NO_DECISION_YET"
            self._update_project_intelligence(project)
            self._save(project)
            return {"project_id": project_id, "ranking": [], "decision": project["decision"], "ranking_version": "deterministic_outcome_order_v2"}
        project["ranking"] = self._rank(project["candidates"])
        project["decision"] = self._decision(project["request"], project["ranking"])
        project["status"] = "EVALUATED" if project["decision"]["status"] == "DECISION_READY" else project["decision"]["status"]
        self._update_project_intelligence(project)
        self._save(project)
        return {"project_id": project_id, "ranking": project["ranking"], "decision": project["decision"], "ranking_version": "deterministic_outcome_order_v2"}

    def confirm_canonical_address(self, project_id: str, candidate_id: str, *, confirmed: bool) -> dict:
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        reconciliation = candidate.get("address_reconciliation") or {}
        if candidate.get("reconciliation_status") != "ADDRESS_CONFIRMATION_REQUIRED":
            raise DiligenceError("This candidate has no canonical-address mismatch awaiting confirmation.")
        if not confirmed:
            reconciliation["status"] = "REJECTED"
            candidate.update(reconciliation_status="ADDRESS_MISMATCH_REJECTED", error="The canonical parcel address was not confirmed.")
        else:
            snapshot = self.sandbox.get_snapshot(candidate["snapshot_id"])
            if snapshot is None:
                raise DiligenceError("The candidate SiteSnapshot is unavailable.")
            reconciliation["status"] = "CONFIRMED"
            candidate["address_reconciliation"] = reconciliation
            self._finalize_candidate(project, candidate, snapshot)
        project["ranking"] = self._rank(project["candidates"])
        project["decision"] = self._decision(project["request"], project["ranking"])
        if not self._queue_address_confirmation(project):
            project["status"] = "EVALUATED" if project["decision"]["status"] == "DECISION_READY" else project["decision"]["status"]
        self._update_project_intelligence(project)
        return self._save(project)

    def _queue_address_confirmation(self, project: dict) -> dict | None:
        if project.get("active_decision"):
            return project["active_decision"]
        candidate = next((item for item in project["candidates"] if item["reconciliation_status"] == "ADDRESS_CONFIRMATION_REQUIRED"), None)
        if candidate is None:
            return None
        details = candidate["address_reconciliation"]
        return self._interrupt(project, {
            "kind": "confirmation", "question": "Is this the property you intended to analyze?",
            "context": f"Submitted: {details['submitted_address']}\nMIREYE parcel address: {details['canonical_address']}",
            "why_it_matters": "An exact point intersection does not prove that a materially different canonical address is the intended property.",
            "options": [
                {"id": "confirm", "label": "Yes, this is the property", "description": "Confirm the resolved parcel identity.", "value": None, "consequence": "Accept the exact-intersect parcel and continue evaluation."},
                {"id": "reject", "label": "No, go back", "description": "Reject this resolved parcel.", "value": None, "consequence": "Reject this parcel match and stop using it."},
            ],
            "recommended_option_id": None, "risk_level": "HIGH", "blocking": True, "input_mode": "confirmation",
            "allow_custom": False, "custom_schema": None, "constraint_targets": [], "originating_step": "candidate_identity",
        }, {"type": "confirm_address", "candidate_id": candidate["candidate_id"]})

    def compare_candidates(self, project_id: str, candidate_ids: list[str]) -> dict:
        project = self.get(project_id)
        if not isinstance(candidate_ids, list) or len(candidate_ids) < 2:
            raise DiligenceError("Compare requires at least two candidate IDs.")
        comparisons = []
        for candidate_id in candidate_ids:
            candidate = self._candidate(project, candidate_id)
            results = (candidate.get("evaluation") or {}).get("constraint_results", [])
            comparisons.append({
                "candidate_id": candidate_id,
                "overall_status": (candidate.get("evaluation") or {}).get("overall_status", "UNRESOLVED"),
                "constraints": {item["constraint_id"]: {"outcome": item["outcome"], "result": item.get("result"), "units": item.get("units"), "evidence_ids": item.get("evidence_ids", [])} for item in results},
            })
        return {"project_id": project_id, "comparison_version": "candidate_evidence_comparison_v1", "candidates": comparisons}

    def open_candidate(self, project_id: str, candidate_id: str) -> dict:
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        if candidate.get("reconciliation_status") != "ENRICHED" or not candidate.get("snapshot_id"):
            raise DiligenceError("Candidate must be enriched and any address mismatch confirmed before opening the sandbox.")
        project["active_candidate_id"] = candidate_id
        self._update_project_intelligence(project)
        self._save(project)
        world = self.worlds.latest_for_site_snapshot(candidate["snapshot_id"]) if self.worlds else None
        world_id = world.get("world_snapshot_id") if world else None
        query = f"?world={world_id}" if world_id else ""
        return {"candidate_id": candidate_id, "site_id": candidate.get("site_id"), "site_snapshot_id": candidate["snapshot_id"], "world_snapshot_id": world_id, "sandbox_url": f"/sandbox/{candidate['snapshot_id']}{query}"}

    async def build_world_snapshot(self, project_id: str, candidate_id: str, *, requested_layers: list[str] | None = None) -> dict:
        if self.worlds is None:
            raise DiligenceError("WorldSnapshot support is unavailable.")
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        if not candidate.get("snapshot_id"):
            raise DiligenceError("Candidate must be enriched before building its world.")
        existing = self.worlds.latest_for_site_snapshot(candidate["snapshot_id"])
        layers = requested_layers or ["terrain", "roads", "buildings", "water", "land_cover", "transmission"]
        world = await self.worlds.create(site_snapshot_id=candidate["snapshot_id"], requested_layers=layers)
        changes = []
        if existing and existing["world_snapshot_id"] != world["world_snapshot_id"]:
            snapshot = self.sandbox.get_snapshot(candidate["snapshot_id"])
            changes = changes_from_world_refresh(
                project_id=project_id, site_id=candidate["site_id"], site_snapshot=snapshot,
                before_world=existing, after_world=world, intelligence=project.get("project_intelligence"),
            )
            self.store.save_project_changes(changes)
            project["recent_changes"] = copy.deepcopy(changes[-10:])
        candidate.setdefault("summary", {})["sandbox_url"] = f"/sandbox/{candidate['snapshot_id']}?world={world['world_snapshot_id']}"
        self._save(project)
        return {
            "candidate_id": candidate_id, "world_snapshot_id": world["world_snapshot_id"],
            "reused": existing is not None and existing["world_snapshot_id"] == world["world_snapshot_id"],
            "changes": changes,
        }

    def set_watch(self, project_id: str, *, enabled: bool) -> dict:
        project = self.get(project_id)
        watch = project.setdefault("watch", {})
        watch.setdefault("watch_id", f"watch_{uuid.uuid4().hex}")
        watch.update(
            project_id=project_id, source="MIREYE", cadence_policy="MANUAL", enabled=bool(enabled),
            fields=copy.deepcopy(project.get("requested_fields", [])), layers=watch.get("layers", []),
            cost_policy={"metered_refresh": "EXPLICIT_CONFIRMATION_REQUIRED"},
        )
        self._save(project)
        return copy.deepcopy(watch)

    def check_now(self, project_id: str) -> dict:
        project = self.get(project_id)
        states = []
        for candidate in project["candidates"]:
            if candidate.get("snapshot_id"):
                freshness = self.sandbox.freshness_status(candidate["snapshot_id"], fields=project["requested_fields"])
                states.append({"candidate_id": candidate["candidate_id"], "snapshot_id": candidate["snapshot_id"], "status": freshness["status"], "refresh_fields": freshness["refresh_fields"]})
        watch = project.setdefault("watch", {})
        watch.update(
            project_id=project_id, source="MIREYE", fields=copy.deepcopy(project.get("requested_fields", [])),
            cadence_policy="MANUAL", last_checked_at=time.time(), candidate_states=states,
            last_known_state={item["candidate_id"]: {"snapshot_id": item["snapshot_id"], "status": item["status"]} for item in states},
        )
        self._save(project)
        return copy.deepcopy(watch)

    @traced_async("workflow.check_now")
    async def check_now_workflow(
        self, project_id: str, *, candidate_id: str | None = None,
        spend_plan_id: str | None = None, confirmed: bool = False,
    ) -> dict:
        """Inspect first; quote only stale evidence; execute only an explicitly confirmed plan."""
        if spend_plan_id:
            if not candidate_id:
                raise DiligenceError("candidate_id is required to confirm a check-now refresh.")
            if not confirmed:
                raise ConfirmationRequired("MIREYE refresh requires explicit application confirmation.")
            result = await self.confirm_candidate_refresh(project_id, candidate_id, spend_plan_id, confirmed=True)
            return {
                "status": "CHECK_COMPLETE", "watch": result["project"]["watch"],
                "refresh": result["refresh"], "impact_summary": result["impact_summary"],
            }
        watch = self.check_now(project_id)
        stale = [item for item in watch["candidate_states"] if item["status"] != "CURRENT" and (candidate_id is None or item["candidate_id"] == candidate_id)]
        if not stale:
            return {"status": "CURRENT", "watch": watch, "refresh_plans": [], "changes": self.changes(project_id, limit=10)}
        plans = []
        for item in stale:
            plan = await self.quote_candidate_refresh(project_id, item["candidate_id"])
            if plan.get("status") != "NO_REFRESH_REQUIRED":
                plans.append({"candidate_id": item["candidate_id"], **plan})
        return {
            "status": "AWAITING_CONFIRMATION" if plans else "CURRENT", "watch": watch,
            "refresh_plans": plans, "confirmation_required": bool(plans),
        }

    async def quote_candidate_refresh(self, project_id: str, candidate_id: str) -> dict:
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        if not candidate.get("snapshot_id"):
            raise DiligenceError("Candidate has no SiteSnapshot to refresh.")
        profile = (project.get("evidence_plan") or {}).get("profile")
        return await self.sandbox.quote_refresh(
            candidate["snapshot_id"],
            project_profile=profile,
            fields=None if profile else project["requested_fields"],
        )

    async def confirm_candidate_refresh(self, project_id: str, candidate_id: str, spend_plan_id: str, *, confirmed: bool) -> dict:
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        previous_snapshot = self.sandbox.get_snapshot(candidate.get("snapshot_id", ""))
        if previous_snapshot is None:
            raise DiligenceError("Candidate has no SiteSnapshot to refresh.")
        previous_intelligence = copy.deepcopy(project.get("project_intelligence"))
        previous_world = self.worlds.latest_for_site_snapshot(previous_snapshot["snapshot_id"]) if self.worlds else None
        result = await self.sandbox.confirm_and_refresh(spend_plan_id, confirmed_by_application=confirmed)
        snapshot = result["snapshot"]
        current_world = None
        if previous_world and self.worlds and hasattr(self.worlds, "create"):
            road_source = next((item.get("source") for item in previous_world.get("layers", []) if item.get("layer") == "roads"), {}) or {}
            try:
                current_world = await self.worlds.create(
                    site_snapshot_id=snapshot["snapshot_id"],
                    buffer_m=previous_world.get("query_aoi", {}).get("buffer_m", 1000),
                    requested_layers=[item["layer"] for item in previous_world.get("layers", [])],
                    options={"overture_release": road_source["release"]} if road_source.get("release") else {},
                )
            except WorldError:
                current_world = None
        scene = scene_state_from_snapshot(snapshot)
        constraints = project["request"]["constraints"] or [{"constraint_id": "footprint_inside_parcel"}]
        candidate.update(snapshot_id=snapshot["snapshot_id"], site_id=snapshot.get("site_id"), evaluation=evaluate_site(snapshot, scene, constraints))
        candidate.setdefault("summary", {})["sandbox_url"] = (
            f"/sandbox/{snapshot['snapshot_id']}?world={current_world['world_snapshot_id']}" if current_world
            else f"/sandbox/{snapshot['snapshot_id']}"
        )
        project["ranking"] = self._rank(project["candidates"])
        project["decision"] = self._decision(project["request"], project["ranking"])
        project["status"] = "EVALUATED" if project["decision"]["status"] == "DECISION_READY" else project["decision"]["status"]
        self._update_project_intelligence(project, changed_evidence_ids=result["snapshot_diff"].get("changed_evidence_ids", []))
        current_intelligence = project.get("project_intelligence")
        self._propagate_action_lifecycle(project, previous_intelligence, current_intelligence, previous_snapshot["snapshot_id"], snapshot["snapshot_id"])
        current_world = current_world or (self.worlds.latest_for_site_snapshot(snapshot["snapshot_id"]) if self.worlds else None)
        changes = changes_from_refresh(
            project_id=project_id, site_id=snapshot["site_id"], before_snapshot=previous_snapshot,
            after_snapshot=snapshot, snapshot_diff=result["snapshot_diff"],
            before_intelligence=previous_intelligence, after_intelligence=current_intelligence,
            evaluation_runs=result.get("evaluation_runs", []), world_before=previous_world, world_after=current_world,
            scenario_dependencies={
                evidence_id: self.store.affected_scenario_constraints(snapshot["site_id"], [evidence_id])
                for evidence_id in result["snapshot_diff"].get("changed_evidence_ids", [])
            },
        )
        self.store.save_project_changes(changes)
        project["recent_changes"] = copy.deepcopy(changes[-10:])
        project.setdefault("watch", {}).update(
            last_checked_at=time.time(),
            last_known_state={candidate_id: {"snapshot_id": snapshot["snapshot_id"], "status": "CURRENT"}},
            candidate_states=[{"candidate_id": candidate_id, "snapshot_id": snapshot["snapshot_id"], "status": "CURRENT", "refresh_fields": []}],
        )
        self._save(project)
        return {"project": project, "refresh": result, "changes": changes, "impact_summary": self._change_summary(changes)}

    def changes(
        self, project_id: str, *, site_id: str | None = None, significance: str | None = None,
        source: str | None = None, change_type: str | None = None, requirement_id: str | None = None,
        since: float | None = None, limit: int = 100,
    ) -> dict:
        self.get(project_id)
        items = self.store.list_project_changes(
            project_id, site_id=site_id, significance=significance, source=source,
            change_type=change_type, since=since,
        )
        if requirement_id:
            items = [item for item in items if requirement_id in item.get("affected_requirements", [])]
        items = items[:max(1, min(int(limit), 500))]
        return {"project_id": project_id, **self._change_summary(items), "items": items}

    @staticmethod
    def _change_summary(changes: list[dict]) -> dict:
        material = [item for item in changes if item.get("significance") in {"MEDIUM", "HIGH", "CRITICAL"}]
        return {
            "change_count": len(changes), "material_change_count": len(material),
            "highest_significance": next((level for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO") if any(item.get("significance") == level for item in changes)), None),
            "affected_scenarios": sorted({item["scenario_id"] for change in changes for item in change.get("affected_scenarios", [])}),
            "affected_requirements": sorted({item for change in changes for item in change.get("affected_requirements", [])}),
        }

    @staticmethod
    def _propagate_action_lifecycle(project: dict, before: dict | None, after: dict | None, snapshot_before: str, snapshot_after: str) -> None:
        old_actions = {item["action_id"]: item for item in (before or {}).get("recommended_actions", [])}
        new_actions = {item["action_id"]: item for item in (after or {}).get("recommended_actions", [])}
        coverage = {item["requirement_id"]: item for item in (after or {}).get("evidence_coverage", [])}
        transitions = project.setdefault("action_transitions", [])
        for action in new_actions.values():
            action["lifecycle_status"] = "CURRENT"
        for action_id, old in old_actions.items():
            current = new_actions.get(action_id)
            if current and old.get("required_evidence") == current.get("required_evidence"):
                continue
            if current:
                state = "STALE"
            else:
                state = "COMPLETED" if coverage.get(old.get("requirement_id"), {}).get("decision_provable") else "SUPERSEDED"
            transition = {
                "action_id": action_id, "from": old.get("lifecycle_status", "CURRENT"), "to": state,
                "snapshot_before": snapshot_before, "snapshot_after": snapshot_after,
            }
            if transition not in transitions:
                transitions.append(transition)
        if after:
            after["state_hash"] = hashlib.sha256(_canonical({
                key: value for key, value in after.items() if key not in {"state_hash", "last_evaluated_at"}
            }).encode("utf-8")).hexdigest()

    def get_evidence(self, project_id: str, candidate_id: str, evidence_ids: list[str] | None = None) -> dict:
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        snapshot = self.sandbox.get_snapshot(candidate.get("snapshot_id", ""))
        if snapshot is None:
            raise DiligenceError("Candidate has no available SiteSnapshot evidence.")
        selected = evidence_ids or list(snapshot.get("evidence", {}))
        return {"candidate_id": candidate_id, "snapshot_id": snapshot["snapshot_id"], "evidence": {key: copy.deepcopy(snapshot["evidence"][key]) for key in selected if key in snapshot["evidence"]}}

    async def ask_mireye_site(self, project_id: str, candidate_id: str, question: str, *, confirmed_candidate_id: str | None) -> dict:
        if candidate_id != confirmed_candidate_id:
            raise ConfirmationRequired("The application has not confirmed this MIREYE site question.")
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        location = candidate.get("selected_location")
        if not location:
            raise DiligenceError("Candidate must be resolved before asking MIREYE.")
        response = await self.sandbox.client.ask(lat=location["lat"], lng=location["lng"], question=question, include_trace=True)
        project.setdefault("site_questions", []).append({"candidate_id": candidate_id, "question": question, "response": response, "created_at": time.time()})
        self._save(project)
        return response

    @staticmethod
    def _transition(project: dict, status: str, step: str) -> None:
        state = project.setdefault("agent_state", {"status": "RUNNING", "step": step, "resume_count": 0, "transitions": []})
        if status == "RESUMED":
            state["resume_count"] = int(state.get("resume_count", 0)) + 1
        state.update(status=status, step=step)
        state.setdefault("transitions", []).append({"status": status, "step": step, "at": time.time()})

    def _interrupt(self, project: dict, specification: dict, resume_action: dict) -> dict:
        if project.get("active_decision"):
            return project["active_decision"]
        now = time.time()
        decision_id = f"decision_{uuid.uuid4().hex}"
        options = []
        for option in specification.get("options", []):
            normalized = copy.deepcopy(option)
            normalized.setdefault("description", normalized.get("consequence", ""))
            normalized.setdefault("value", None)
            options.append(normalized)
        decision = {
            "decision_id": decision_id, "id": decision_id, "kind": specification.get("kind", "confirmation"),
            "question": specification["question"], "context": specification["context"],
            "why_it_matters": specification["why_it_matters"], "risk_level": specification["risk_level"],
            "blocking": bool(specification["blocking"]), "input_mode": specification.get("input_mode", "confirmation"),
            "options": options,
            "recommended_option_id": specification.get("recommended_option_id"),
            "allow_custom": bool(specification.get("allow_custom", False)),
            "custom_schema": copy.deepcopy(specification.get("custom_schema")),
            "constraint_targets": copy.deepcopy(specification.get("constraint_targets", [])),
            "resume_token": f"resume_{uuid.uuid4().hex}", "originating_step": specification["originating_step"],
            "created_at": now, "status": "PENDING", "resume_action": copy.deepcopy(resume_action),
        }
        project["active_decision"] = decision
        project["status"] = "NEEDS_USER_DECISION"
        self._transition(project, "NEEDS_USER_DECISION", specification["originating_step"])
        self._transition(project, "WAITING_FOR_DECISION", specification["originating_step"])
        return decision

    @staticmethod
    def _decision_answer(
        decision: dict, *, option_id: str | None, option_ids: list[str] | None, value: Any,
        interpreted_constraint: dict | None, cancelled: bool,
    ) -> tuple[list[dict], list[dict]]:
        if cancelled:
            return [], []
        options_by_id = {item["id"]: item for item in decision.get("options", [])}
        mode = decision.get("input_mode", "single_choice")
        selected_ids = option_ids if mode == "multi_choice" else ([option_id] if option_id else [])
        if mode == "multi_choice" and (not isinstance(selected_ids, list) or not selected_ids):
            raise DiligenceError("Select at least one available decision option.")
        if len(selected_ids) != len(set(selected_ids)) or any(item not in options_by_id for item in selected_ids):
            raise DiligenceError("The decision answer contains an unavailable option.")
        selected = [options_by_id[item] for item in selected_ids]
        constraints = [copy.deepcopy(item["value"]) for item in selected if item.get("value") is not None]
        if interpreted_constraint is not None:
            if mode != "text":
                raise DiligenceError("Model interpretation is allowed only for a text DecisionRequest.")
            constraints.append(_validate_constraint(interpreted_constraint, set(decision["constraint_targets"])))
        elif value is not None:
            schema = decision.get("custom_schema")
            if not decision.get("allow_custom") or not schema:
                raise DiligenceError("This DecisionRequest does not accept a custom answer.")
            fields = schema["fields"]
            if mode == "number":
                values = {fields[0]["name"]: value}
            elif mode == "range":
                if not isinstance(value, dict) or set(value) != {item["name"] for item in fields}:
                    raise DiligenceError("The range answer does not match the DecisionRequest schema.")
                values = value
            elif mode == "text":
                values = {fields[0]["name"]: value}
            else:
                raise DiligenceError("Custom input is unsupported for this interaction mode.")
            constraints.append(_validate_constraint({"constraint_id": schema["constraint_id"], **values}, set(decision["constraint_targets"])))
        elif mode in {"number", "range", "text"} and not selected:
            raise DiligenceError("This DecisionRequest requires a custom answer.")
        elif mode in {"single_choice", "boolean", "confirmation"} and len(selected) != 1:
            raise DiligenceError("Select one available decision option.")
        if decision["resume_action"]["type"] == "requirement" and not constraints:
            raise DiligenceError("The decision answer did not produce a validated constraint value.")
        return selected, constraints

    @staticmethod
    def _validate_assumptions(assumptions: Any, gaps: list[dict]) -> list[dict]:
        if not isinstance(assumptions, list) or not assumptions:
            raise DiligenceError("ASSUME_AND_CONTINUE requires at least one generated assumption.")
        allowed = _allowed_targets(gaps)
        normalized = []
        for item in assumptions:
            if not isinstance(item, dict) or set(item) != {"assumption", "reason", "confidence", "overridable", "constraint"}:
                raise DiligenceError("A generated assumption has missing or unsupported fields.")
            constraint = _validate_constraint(item["constraint"], allowed)
            if not CONSTRAINT_CAPABILITIES[constraint["constraint_id"]]["assumption_allowed"]:
                raise DiligenceError("The generated assumption targets a capability that cannot be assumed.")
            if any(not isinstance(item[name], str) or not item[name].strip() for name in ("assumption", "reason")):
                raise DiligenceError("Generated assumption text is invalid.")
            if item["confidence"] not in {"LOW", "MEDIUM", "HIGH"} or item["overridable"] is not True:
                raise DiligenceError("Generated assumption confidence or override policy is invalid.")
            normalized.append({
                "assumption_id": f"assumption_{uuid.uuid4().hex}", "assumption": item["assumption"].strip(),
                "reason": item["reason"].strip(), "confidence": item["confidence"], "overridable": True,
                "constraint": constraint, "source": "AGENT_ASSUMPTION", "authorized_by": "USER_REQUEST",
                "created_at": time.time(),
            })
        return normalized

    def _apply_requirement_constraints(self, project: dict, constraints: list[dict], provenance: dict) -> None:
        request = project["request"]
        gaps = request.get("requirement_gaps", [])
        resolved_gap_ids = {
            gap["gap_id"] for constraint in constraints for gap in gaps
            if constraint["constraint_id"] in gap["candidate_constraint_ids"]
        }
        if not resolved_gap_ids:
            raise DiligenceError("The validated answer does not resolve an active requirement gap.")
        request["constraints"] = [item for item in request["constraints"] if item["constraint_id"] not in resolved_gap_ids]
        for constraint in constraints:
            if constraint not in request["constraints"]:
                request["constraints"].append(copy.deepcopy(constraint))
        request["requirement_gaps"] = [item for item in gaps if item["gap_id"] not in resolved_gap_ids]
        request.setdefault("decisions", []).append({
            "gap_ids": sorted(resolved_gap_ids), "constraints": copy.deepcopy(constraints), **copy.deepcopy(provenance),
        })
        request["supported_constraints"] = [item for item in request["constraints"] if item["constraint_id"] in SUPPORTED_CONSTRAINTS]
        request["unresolved_constraints"] = [item for item in request["constraints"] if item["constraint_id"] not in SUPPORTED_CONSTRAINTS]
        request["requirement_status"] = "REVIEW_REQUIRED" if request["requirement_gaps"] else "READY"
        request["constraint_revision"] = int(request.get("constraint_revision", 0)) + 1
        project["decision"] = self._decision(request, project.get("ranking", []))
        self._update_project_intelligence(project)

    def _finalize_candidate(self, project: dict, candidate: dict, snapshot: dict) -> None:
        scene = scene_state_from_snapshot(snapshot)
        constraints = project["request"]["constraints"] or [{"constraint_id": "footprint_inside_parcel"}]
        evidence = snapshot.get("evidence", {})
        def value(field: str) -> Any:
            return (evidence.get(field) or {}).get("value")
        world = self.worlds.latest_for_site_snapshot(snapshot["snapshot_id"]) if self.worlds else None
        world_query = f"?world={world['world_snapshot_id']}" if world else ""
        candidate.update(
            reconciliation_status="ENRICHED", snapshot_id=snapshot["snapshot_id"], site_id=snapshot.get("site_id"),
            evaluation=evaluate_site(snapshot, scene, constraints), error=None,
            summary={
                "title": snapshot["parcel_identity"].get("parcel_address") or candidate.get("address") or "Verified property",
                "area_acres": round(float(value("parcel_area_m2")) / 4046.8564224, 2) if isinstance(value("parcel_area_m2"), (int, float)) else None,
                "transmission_distance_m": value("nearest_transmission_line_distance_m"),
                "road_distance_m": value("nearest_major_road_distance_m"),
                "zoning_code": value("parcel_zoning"),
                "sandbox_url": f"/sandbox/{snapshot['snapshot_id']}{world_query}",
            },
        )

    @staticmethod
    def _decision(request: dict, ranking: list[dict]) -> dict:
        if request.get("requirement_status") != "READY":
            return {
                "status": "NO_DECISION_YET",
                "reason": "The agent is deciding whether one important requirement needs user input.",
                "assumptions_permitted": bool(request.get("assumptions_permitted")),
            }
        enriched = [item for item in ranking if item["status"] == "ENRICHED"]
        if not enriched:
            return {"status": "NO_DECISION_YET", "reason": "No confirmed enriched candidate is available."}
        if all(item["outcome_counts"]["PASS"] == item["outcome_counts"]["FAIL"] == 0 for item in enriched):
            return {"status": "NO_DECISION_YET", "reason": "All evaluated candidate constraints remain unresolved."}
        top = enriched[0]
        def score(item: dict) -> tuple[int, int, int]:
            return (item["outcome_counts"]["FAIL"], item["outcome_counts"]["UNRESOLVED"], -item["outcome_counts"]["PASS"])
        if top["overall_status"] != "PASS":
            return {"status": "NO_DECISION_YET", "reason": "The leading candidate still has failed or unresolved constraints."}
        if len(enriched) > 1 and score(top) == score(enriched[1]):
            return {"status": "NO_DECISION_YET", "reason": "The leading candidates are tied on deterministic outcomes."}
        return {
            "status": "DECISION_READY", "winner_candidate_id": top["candidate_id"],
            "reason": "This is the unique fully passing candidate under the explicit requirements.",
        }

    @staticmethod
    def _rank(candidates: list[dict]) -> list[dict]:
        ranked = []
        for candidate in candidates:
            results = (candidate.get("evaluation") or {}).get("constraint_results", [])
            counts = {outcome: sum(item.get("outcome") == outcome for item in results) for outcome in ("PASS", "FAIL", "UNRESOLVED")}
            ranked.append({
                "candidate_id": candidate["candidate_id"], "snapshot_id": candidate.get("snapshot_id"),
                "status": candidate["reconciliation_status"], "overall_status": (candidate.get("evaluation") or {}).get("overall_status", "UNRESOLVED"),
                "outcome_counts": counts, "constraint_results": results, "error": candidate.get("error"),
            })
        ranked.sort(key=lambda item: (
            0 if item["status"] == "ENRICHED" else 1,
            item["outcome_counts"]["FAIL"], item["outcome_counts"]["UNRESOLVED"],
            -item["outcome_counts"]["PASS"], item["candidate_id"],
        ))
        for position, item in enumerate(ranked, 1):
            item["rank"] = position
        return ranked

    @staticmethod
    def _batch_results(payload: dict) -> list[dict]:
        for key in ("results", "items", "locations"):
            if isinstance(payload.get(key), list):
                return [item if isinstance(item, dict) else {"ok": False, "error": {"message": "Invalid MIREYE batch item."}} for item in payload[key]]
        return []

    @staticmethod
    def _quote_expiry(quote: dict, fallback: float) -> float:
        value = quote.get("expires_at") or quote.get("quote_expires_at")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
            except ValueError:
                pass
        return fallback

    @staticmethod
    def _candidate(project: dict, candidate_id: str) -> dict:
        for candidate in project["candidates"]:
            if candidate["candidate_id"] == candidate_id:
                return candidate
        raise DiligenceError("Candidate was not found in this project.")

    @staticmethod
    def _site_candidate(project: dict, site_id: str) -> dict:
        candidate = next((item for item in project.get("candidates", []) if item.get("site_id") == site_id and item.get("snapshot_id")), None)
        if candidate is None:
            raise DiligenceError("The requested site is not an enriched candidate in this project.")
        return candidate

    def _rfi_context(self, project: dict, action: dict) -> dict:
        candidate = next((item for item in project.get("candidates", []) if item.get("site_id") == action.get("site_id")), None)
        snapshot = self.store.get_site_snapshot(candidate["snapshot_id"]) if candidate and candidate.get("snapshot_id") else None
        identity = (snapshot or {}).get("parcel_identity", {})
        power = project.get("power_readiness_by_site", {}).get(action.get("site_id"))
        entitlement = project.get("entitlement_by_site", {}).get(action.get("site_id"))
        return {
            "project_requirements": copy.deepcopy(project.get("request", {}).get("power_requirements", {})),
            "site": {
                "site_id": action.get("site_id"), "parcel_id": identity.get("parcel_id"),
                "address": identity.get("parcel_address"), "selected_point": copy.deepcopy(identity.get("selected_point")),
            },
            "known_evidence": copy.deepcopy((power or entitlement or {}).get("items", [])),
            "required_evidence": copy.deepcopy(action.get("required_evidence", [])),
            "recipient_category": action.get("recipient_category"),
            "timeline": "UNKNOWN", "human_approval_required": True,
        }

    def _build_site_readiness(self, project: dict, candidate: dict) -> tuple[dict, dict]:
        snapshot = self.store.get_site_snapshot(candidate["snapshot_id"])
        if snapshot is None:
            raise DiligenceError("The candidate SiteSnapshot is unavailable.")
        scoped_project = copy.deepcopy(project)
        scoped_project["active_candidate_id"] = candidate["candidate_id"]
        intelligence = build_project_intelligence(
            scoped_project, self.store.get_site_snapshot, CONSTRAINT_CAPABILITIES, CONSTRAINT_FIELDS,
            dependency_lookup=self.store.affected_scenario_constraints,
        )
        external = project.get("external_evidence_by_site", {}).get(candidate["site_id"], {})
        power = build_power_readiness(project, candidate, snapshot, intelligence, external)
        entitlement = build_entitlement_state(project, candidate, snapshot, intelligence, external)
        project.setdefault("power_readiness_by_site", {})[candidate["site_id"]] = power
        project.setdefault("entitlement_by_site", {})[candidate["site_id"]] = entitlement
        return power, entitlement

    @staticmethod
    def _candidate_resolution_view(candidates: list[dict]) -> dict:
        status_map = {
            "RESOLVED": ("EXACT_MATCH", None), "ENRICHED": ("EXACT_MATCH", None),
            "ADDRESS_CONFIRMATION_REQUIRED": ("NEEDS_CONFIRMATION", "MIREYE returned a different canonical parcel address."),
            "AMBIGUOUS": ("AMBIGUOUS", "Multiple parcel candidates were returned."),
            "NOT_FOUND": ("UNRESOLVED", "No exact parcel match was found."),
            "UNSUPPORTED": ("UNRESOLVED", "Input could not be resolved."),
            "PENDING": ("UNRESOLVED", "This candidate has not been resolved yet."),
            "ADDRESS_MISMATCH_REJECTED": ("UNRESOLVED", "The canonical parcel match was rejected."),
            "ERROR": ("FAILED", "MIREYE request failed."),
            "ENRICHMENT_FAILED": ("FAILED", "MIREYE request failed."),
        }
        items = []
        for candidate in candidates:
            status, reason = status_map.get(candidate.get("reconciliation_status"), ("FAILED", "Candidate resolution failed."))
            choices = []
            for index, option in enumerate(candidate.get("resolution_options") or []):
                choices.append({
                    "index": index,
                    "address": option.get("address") or option.get("parcel_address") or option.get("label"),
                    "parcel_id": option.get("parcel_id"),
                    "lat": option.get("lat"), "lng": option.get("lng"),
                    "match_distance_m": option.get("parcel_match_distance_m", option.get("match_distance_m")),
                })
            items.append({
                "candidate_id": candidate["candidate_id"], "raw_input": copy.deepcopy(candidate["raw_input"]),
                "status": status, "reason": reason, "details": copy.deepcopy(candidate.get("address_reconciliation")),
                "choices": choices,
            })
        exact_count = sum(item["status"] == "EXACT_MATCH" for item in items)
        return {
            "items": items, "exact_count": exact_count, "attention_count": len(items) - exact_count,
            "has_attention": exact_count != len(items),
        }

    def _update_project_intelligence(self, project: dict, *, changed_evidence_ids: list[str] | None = None) -> None:
        intelligence = build_project_intelligence(
            project, self.store.get_site_snapshot, CONSTRAINT_CAPABILITIES, CONSTRAINT_FIELDS,
            dependency_lookup=self.store.affected_scenario_constraints,
            changed_evidence_ids=changed_evidence_ids,
        )
        active = intelligence.get("active_site")
        if active and active.get("site_id") and active.get("site_snapshot_id"):
            candidate = self._site_candidate(project, active["site_id"])
            snapshot = self.store.get_site_snapshot(active["site_snapshot_id"])
            if snapshot:
                external = project.get("external_evidence_by_site", {}).get(active["site_id"], {})
                power = build_power_readiness(project, candidate, snapshot, intelligence, external)
                entitlement = build_entitlement_state(project, candidate, snapshot, intelligence, external)
                project.setdefault("power_readiness_by_site", {})[active["site_id"]] = power
                project.setdefault("entitlement_by_site", {})[active["site_id"]] = entitlement
                intelligence["power_readiness"] = copy.deepcopy(power)
                intelligence["entitlement"] = copy.deepcopy(entitlement)
                intelligence["state_hash"] = hashlib.sha256(_canonical({key: value for key, value in intelligence.items() if key not in {"state_hash", "last_evaluated_at"}}).encode("utf-8")).hexdigest()
        project["project_intelligence"] = intelligence

    def _save(self, project: dict) -> dict:
        project["updated_at"] = time.time()
        project["candidate_resolution"] = self._candidate_resolution_view(project["candidates"])
        self.store.save_diligence_project(project)
        return copy.deepcopy(project)
