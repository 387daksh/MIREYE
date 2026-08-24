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
from app.product import compile_request
from app.sandbox import ConfirmationRequired, SandboxError, SiteSnapshotService, scene_state_from_snapshot
from app.sandbox_evaluator import SceneValidationError, evaluate_site
from app.workspace.store import WorkspaceStore


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
    "zoning_context": ("parcel_zoning",),
    "sufficient_grid_capacity": (
        "nearest_substation_distance_m", "nearest_substation_status", "nearest_substation_max_voltage_kv",
        "nearest_transmission_line_distance_m", "nearest_transmission_line_voltage_kv",
    ),
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
    "legal_access": {
        "semantic_description": "Request legal access, frontage, or right-of-way proof.",
        "input_schema": {"required": [], "properties": {}}, "evidence_fields": list(CONSTRAINT_FIELDS["legal_access"]),
        "spatial_scope": "PARCEL", "evaluator_support": "UNRESOLVED_ONLY",
        "unsupported_semantics": ["mapped-road proximity does not prove legal access"], "confirmation_mandatory": False, "assumption_allowed": False,
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
    normalize = lambda value: [aliases.get(token, token) for token in re.findall(r"[a-z0-9]+", value.casefold())]
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
    return {
        **compiled,
        "constraints": deduped,
        "supported_constraints": [item for item in deduped if item["constraint_id"] in SUPPORTED_CONSTRAINTS],
        "unresolved_constraints": [item for item in deduped if item["constraint_id"] not in SUPPORTED_CONSTRAINTS],
        "requirement_status": "REVIEW_REQUIRED" if requirement_gaps else "READY",
        "requirement_gaps": requirement_gaps,
        "assumptions_permitted": assumptions_permitted,
        "compiler_version": "diligence_constraints_v3",
    }


class DiligenceService:
    def __init__(self, store: WorkspaceStore, sandbox: SiteSnapshotService, worlds: Any | None = None, provider: CandidateProvider | None = None):
        self.store, self.sandbox, self.worlds = store, sandbox, worlds
        self.provider = provider or UserSuppliedCandidateProvider()

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
            "spend_plan": None, "ranking": [],
            "decision": self._decision(request, []),
            "agent_state": {"status": "RUNNING", "step": "requirement_planning", "resume_count": 0, "transitions": [{"status": "RUNNING", "at": now}]},
            "active_decision": None, "decision_history": [], "assumptions": [],
            "watch": {"enabled": False, "last_checked_at": None, "candidate_states": []},
            "created_at": now, "updated_at": now,
        }
        self.store.create_workspace(workspace_id, "Site diligence")
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
        specification, gap_ids = _validate_model_decision(decision_request, gaps)
        decision = self._interrupt(project, specification, {"type": "requirement", "gap_ids": gap_ids})
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
            return self.confirm_canonical_address(project_id, action["candidate_id"], confirmed=selected_options[0]["id"] == "confirm")
        raise DiligenceError("The decision resume action is unsupported.")

    def plan_fields(self, project_id: str) -> dict:
        project = self.get(project_id)
        fields = list(IDENTITY_FIELDS)
        for constraint in project["request"]["constraints"]:
            fields.extend(CONSTRAINT_FIELDS.get(constraint["constraint_id"], ()))
        project["requested_fields"] = list(dict.fromkeys(fields))
        self._save(project)
        return {"project_id": project_id, "fields": project["requested_fields"], "field_count": len(project["requested_fields"]), "constraints": project["request"]["constraints"]}

    async def resolve_and_quote(self, project_id: str, *, confirmed_resolution: bool) -> dict:
        project = self.get(project_id)
        if project["request"]["requirement_status"] != "READY":
            project["status"] = "NEEDS_USER_DECISION"
            project["decision"] = self._decision(project["request"], [])
            return self._save(project)
        if not confirmed_resolution:
            raise ConfirmationRequired("Candidate resolution requires explicit application confirmation.")
        fields = self.plan_fields(project_id)["fields"]
        for candidate in project["candidates"]:
            if candidate["reconciliation_status"] == "ENRICHMENT_FAILED" and candidate.get("selected_location"):
                candidate.update(reconciliation_status="RESOLVED", error=None)
                continue
            if candidate["reconciliation_status"] in {"UNSUPPORTED", "AMBIGUOUS", "RESOLVED", "ENRICHED"}:
                continue
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
            except Exception as exc:
                candidate["reconciliation_status"], candidate["error"] = "ERROR", str(exc)
        resolved_candidates = [item for item in project["candidates"] if item["reconciliation_status"] == "RESOLVED"]
        if not resolved_candidates:
            project["status"] = "RESOLUTION_REQUIRED"
            return self._save(project)
        selected_fields, catalog = await self.sandbox.select_fields(fields)
        provider_quotes, estimates = [], []
        for offset in range(0, len(resolved_candidates), BATCH_SIZE):
            count = len(resolved_candidates[offset:offset + BATCH_SIZE])
            quote = await self.sandbox.client.fetch_quote(locations=count, fields=selected_fields)
            provider_quotes.append({"location_count": count, "quote": quote})
            credits = self.sandbox._estimated_credits(quote)
            if isinstance(credits, (int, float)):
                estimates.append(float(credits))
        now = time.time()
        quote_expiries = [self._quote_expiry(item["quote"], now + QUOTE_TTL_SECONDS) for item in provider_quotes]
        plan = {
            "spend_plan_id": f"spend_{uuid.uuid4().hex}", "project_id": project_id,
            "status": "QUOTED", "requested_fields": selected_fields,
            "candidate_count": len(resolved_candidates), "batch_strategy": {"max_batch_size": BATCH_SIZE, "batch_count": len(provider_quotes)},
            "provider_quotes": provider_quotes, "expected_credits": sum(estimates) if len(estimates) == len(provider_quotes) else None,
            "provider_quote_ids": [item["quote"].get("quote_id") or item["quote"].get("id") for item in provider_quotes],
            "field_catalog_version": self.sandbox._catalog_version(catalog),
            "quote_expires_at": min(quote_expiries),
            "cache_hits": {"candidate_count": 0, "field_count": 0},
            "freshness_reason": "initial_candidate_enrichment",
            "workspace_budget_impact": {"policy": "explicit_application_confirmation_required", "estimated_credits": sum(estimates) if len(estimates) == len(provider_quotes) else None},
            "confirmation_required": True, "created_at": now,
        }
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
        if candidate["input_type"] == "coordinate":
            resolved = await self.sandbox.resolve(**candidate["coordinate"])
        else:
            value = candidate["apn"] if candidate["input_type"] == "apn" else candidate["address"]
            resolved = await self.sandbox.resolve(input=value, kind=candidate["input_type"])
        if resolved["status"] == "resolved":
            candidate.update(selected_location=resolved["candidates"][0], reconciliation_status="RESOLVED", error=None)
        elif resolved["status"] == "ambiguous":
            candidate.update(resolution_options=resolved["candidates"], reconciliation_status="AMBIGUOUS")
        else:
            candidate.update(reconciliation_status="NOT_FOUND", error="MIREYE could not resolve this candidate.")
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
        fields, catalog = await self.sandbox.select_fields(plan["requested_fields"])
        resolved = [item for item in project["candidates"] if item["reconciliation_status"] == "RESOLVED"]
        for offset in range(0, len(resolved), BATCH_SIZE):
            batch = resolved[offset:offset + BATCH_SIZE]
            locations = [{"lat": item["selected_location"]["lat"], "lng": item["selected_location"]["lng"]} for item in batch]
            try:
                payload = await self.sandbox.client.fetch_batch(locations=locations, fields=fields)
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
        plan["status"] = "COMPLETED"
        plan["completed_at"] = time.time()
        project["ranking"] = self._rank(project["candidates"])
        project["decision"] = self._decision(project["request"], project["ranking"])
        if not project.get("active_decision"):
            project["status"] = "EVALUATED" if project["decision"]["status"] == "DECISION_READY" else project["decision"]["status"]
        return self._save(project)

    def rank_candidates(self, project_id: str) -> dict:
        project = self.get(project_id)
        if project["request"]["requirement_status"] != "READY":
            project["ranking"] = []
            project["decision"] = self._decision(project["request"], [])
            project["status"] = "NEEDS_USER_DECISION" if project.get("active_decision") else "NO_DECISION_YET"
            self._save(project)
            return {"project_id": project_id, "ranking": [], "decision": project["decision"], "ranking_version": "deterministic_outcome_order_v2"}
        project["ranking"] = self._rank(project["candidates"])
        project["decision"] = self._decision(project["request"], project["ranking"])
        project["status"] = "EVALUATED" if project["decision"]["status"] == "DECISION_READY" else project["decision"]["status"]
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
        layers = requested_layers or ["terrain", "roads"]
        world = await self.worlds.create(site_snapshot_id=candidate["snapshot_id"], requested_layers=layers)
        return {"candidate_id": candidate_id, "world_snapshot_id": world["world_snapshot_id"], "reused": existing is not None and existing["world_snapshot_id"] == world["world_snapshot_id"]}

    def set_watch(self, project_id: str, *, enabled: bool) -> dict:
        project = self.get(project_id)
        project["watch"]["enabled"] = bool(enabled)
        self._save(project)
        return project["watch"]

    def check_now(self, project_id: str) -> dict:
        project = self.get(project_id)
        states = []
        for candidate in project["candidates"]:
            if candidate.get("snapshot_id"):
                freshness = self.sandbox.freshness_status(candidate["snapshot_id"], fields=project["requested_fields"])
                states.append({"candidate_id": candidate["candidate_id"], "snapshot_id": candidate["snapshot_id"], "status": freshness["status"], "refresh_fields": freshness["refresh_fields"]})
        project["watch"].update(last_checked_at=time.time(), candidate_states=states)
        self._save(project)
        return copy.deepcopy(project["watch"])

    async def quote_candidate_refresh(self, project_id: str, candidate_id: str) -> dict:
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        if not candidate.get("snapshot_id"):
            raise DiligenceError("Candidate has no SiteSnapshot to refresh.")
        return await self.sandbox.quote_refresh(candidate["snapshot_id"], fields=project["requested_fields"])

    async def confirm_candidate_refresh(self, project_id: str, candidate_id: str, spend_plan_id: str, *, confirmed: bool) -> dict:
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        result = await self.sandbox.confirm_and_refresh(spend_plan_id, confirmed_by_application=confirmed)
        snapshot = result["snapshot"]
        scene = scene_state_from_snapshot(snapshot)
        constraints = project["request"]["constraints"] or [{"constraint_id": "footprint_inside_parcel"}]
        candidate.update(snapshot_id=snapshot["snapshot_id"], site_id=snapshot.get("site_id"), evaluation=evaluate_site(snapshot, scene, constraints))
        project["ranking"] = self._rank(project["candidates"])
        project["decision"] = self._decision(project["request"], project["ranking"])
        project["status"] = "EVALUATED" if project["decision"]["status"] == "DECISION_READY" else project["decision"]["status"]
        self._save(project)
        return {"project": project, "refresh": result}

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

    def _finalize_candidate(self, project: dict, candidate: dict, snapshot: dict) -> None:
        scene = scene_state_from_snapshot(snapshot)
        constraints = project["request"]["constraints"] or [{"constraint_id": "footprint_inside_parcel"}]
        evidence = snapshot.get("evidence", {})
        value = lambda field: (evidence.get(field) or {}).get("value")
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
        score = lambda item: (item["outcome_counts"]["FAIL"], item["outcome_counts"]["UNRESOLVED"], -item["outcome_counts"]["PASS"])
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

    def _save(self, project: dict) -> dict:
        project["updated_at"] = time.time()
        project["candidate_resolution"] = self._candidate_resolution_view(project["candidates"])
        self.store.save_diligence_project(project)
        return copy.deepcopy(project)
