"""Constrained conversational control for mutable Site Sandbox proposals."""
from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.config import OPENAI_API_KEY, SANDBOX_AGENT_MODEL, SANDBOX_AGENT_REASONING_EFFORT
from app.sandbox import ConfirmationRequired, SiteSnapshotService, campus_component_object, scene_state_from_snapshot
from app.sandbox_evaluator import SceneValidationError, build_oriented_footprint, evaluate_site
from app.sandbox_proposal import DEFAULT_MINIMUM_SETBACK_M, generate_data_center_proposal
from app.sandbox_scenarios import ScenarioError, ScenarioService


SYSTEM_INSTRUCTIONS = """You are the MIREYE Site Sandbox site-planning copilot. MIREYE/source evidence is authoritative for factual site data. OBSERVED data is factual, DERIVED data is deterministic, and PROPOSED campus objects are conceptual simulations. Begin every request with get_site_context using the provided snapshot ID. Use tools for all factual claims, proposal changes, and evaluations. PASS, FAIL, and UNRESOLVED only come from evaluate_scenario; never calculate or decide them yourself. Point-scoped flood, slope, and proximity results must never be described as parcel-wide or capacity/access proof. For an unqualified fit request, evaluate footprint_inside_parcel, footprint_area, and parcel_coverage only. Do not request minimum_setback without a numeric minimum_m. When proposing a campus, select only the conceptual element roles useful to the stated project; deterministic tools own their geometry. When the user asks for a second, alternative, or another layout and an active scenario exists, call branch_scenario, make a validated geometry change with transform_object or optimize_layout, and evaluate it; never remove or overwrite the existing layout, and never describe an identical branch as an alternative. The semantic campus components are conceptual massing inside the deterministically evaluated planning envelope, not engineering designs. Never invent values, parcel facts, geometry, or engineering conclusions. Do not claim engineering-grade analysis. If evidence cannot prove a request, state UNRESOLVED. You may inspect MIREYE freshness and request a quote. A MIREYE refresh can run only after the application has supplied explicit confirmation; you cannot create confirmation yourself. In the final user-facing response, use plain site-planning language and do not mention tool names, internal object IDs, evidence IDs, snapshot IDs, schema versions, or API implementation details."""

DILIGENCE_SYSTEM_INSTRUCTIONS = """You are the single MIREYE site-diligence orchestrator. Work only with candidates supplied in the current project; statewide inverse parcel discovery is unavailable and the synthetic screen endpoint is prohibited. Begin by reading the persisted requirement context and discovery capabilities. The requirement context includes the original request, current ConstraintSpec, machine-only capability schemas, evidence limitations, completed decisions, assumptions, candidate state, workflow step, and spend state. Decide whether to AUTO_CONTINUE, ASSUME_AND_CONTINUE, or ASK_USER. Ask only for the minimum information that materially blocks safe progress. For ASK_USER, generate the question, context, answer mode, useful options or custom input schema, recommendation, and explanation from this project's actual context; there is no predefined conversation tree. Every option and assumption must carry a typed constraint value allowed by the supplied capability schema. HARD_BLOCK is application-owned and cannot be created by you. Stop when a DecisionRequest is created and wait for its resume token. Use typed tools for candidate resolution, MIREYE field planning, quoting, enrichment, evaluation, ranking, evidence, and freshness. You cannot create confirmation: identity, enrichment cost, refresh, and MIREYE site questions require application-owned decisions. Never invent candidate facts, numerical scores, geometry, grid capacity, legal access, parcel-wide slope, parcel-wide flood safety, zoning meaning, or evaluator logic. Deterministic tools alone validate constraints and produce PASS, FAIL, and UNRESOLVED. Call a candidate a winner only when the deterministic decision status is DECISION_READY; ties and unresolved results are NO_DECISION_YET."""


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "name": name, "description": description, "strict": True, "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}


NUMBER = {"type": ["number", "null"]}
STRING = {"type": ["string", "null"]}
STRING_LIST = {"type": ["array", "null"], "items": {"type": "string"}}
BOOLEAN = {"type": ["boolean", "null"]}
CONSTRAINT_SPEC = {
    "type": "object",
    "properties": {
        "constraint_id": {"type": "string", "enum": ["footprint_inside_parcel", "minimum_setback", "footprint_area", "parcel_coverage", "object_collision", "max_nwi_wetland_fraction_of_parcel", "max_nwi_wetland_acres_on_parcel", "resolution_point_outside_fema_sfha", "max_resolution_point_slope_degrees", "max_resolution_point_substation_distance_m", "max_resolution_point_transmission_distance_m", "max_resolution_point_major_road_distance_m", "parcel_zoning_code_in", "parcel_outside_fema_sfha", "footprint_outside_fema_sfha", "max_slope_degrees", "industrial_zoning", "legal_access", "heavy_haul_suitability", "utilities_available", "utility_capacity", "substation_available_capacity_mw", "transmission_available_capacity_mw", "sufficient_grid_capacity"]},
        "object_id": STRING, "minimum_m": NUMBER, "min_m2": NUMBER, "max_m2": NUMBER, "max_percent": NUMBER, "max_degrees": NUMBER,
        "max_acres": NUMBER, "max_fraction": NUMBER, "max_distance_m": NUMBER, "allowed_codes": STRING_LIST, "required_statuses": STRING_LIST, "require_operational": BOOLEAN,
    },
    "required": ["constraint_id", "object_id", "minimum_m", "min_m2", "max_m2", "max_percent", "max_degrees", "max_acres", "max_fraction", "max_distance_m", "allowed_codes", "required_statuses", "require_operational"],
    "additionalProperties": False,
}
TOOL_DEFINITIONS = [
    _tool("get_site_context", "Read immutable site facts, evidence summary, available constraints, and the current session scene.", {"snapshot_id": {"type": "string"}}, ["snapshot_id"]),
    _tool("propose_data_center", "Generate a parcel-derived conceptual data-center campus. Select useful conceptual element roles from project context; deterministic tools place them. Omitted dimensions may be uniformly reduced; explicit dimensions remain fixed.", {
        "capacity_mw": {"type": "number"}, "width_m": NUMBER, "length_m": NUMBER, "height_m": NUMBER,
        "position": {"type": ["object", "null"], "properties": {"x_m": NUMBER, "y_m": NUMBER}, "required": ["x_m", "y_m"], "additionalProperties": False}, "rotation_deg": NUMBER,
        "minimum_setback_m": NUMBER,
        "elements": {"type": ["array", "null"], "items": {"type": "string", "enum": ["data_halls", "electrical_area", "cooling_plant", "internal_access", "service_parking", "expansion_reserve"]}},
    }, ["capacity_mw", "width_m", "length_m", "height_m", "position", "rotation_deg", "minimum_setback_m", "elements"]),
    _tool("transform_object", "Move, resize, rotate, or set capacity on one proposed object. Move uses local-meter deltas; rotate sets an absolute degree value.", {
        "object_id": {"type": "string"}, "operation": {"type": "string", "enum": ["move", "resize", "rotate", "set_capacity"]},
        "delta_x_m": NUMBER, "delta_y_m": NUMBER, "width_m": NUMBER, "length_m": NUMBER, "height_m": NUMBER, "rotation_deg": NUMBER, "capacity_mw": NUMBER,
    }, ["object_id", "operation", "delta_x_m", "delta_y_m", "width_m", "length_m", "height_m", "rotation_deg", "capacity_mw"]),
    _tool("evaluate_scenario", "Run the deterministic evaluator. Use only supported constraint IDs and report its output exactly.", {"requested_constraints": {"type": "array", "items": CONSTRAINT_SPEC}}, ["requested_constraints"]),
    _tool("get_evidence", "Read stored factual evidence only; this never fetches new data.", {"evidence_ids": {"type": "array", "items": {"type": "string"}}, "constraint_id": STRING}, ["evidence_ids", "constraint_id"]),
    _tool("check_evidence_freshness", "Check stored MIREYE evidence freshness. This never fetches or spends credits.", {"snapshot_id": {"type": "string"}}, ["snapshot_id"]),
    _tool("quote_mireye_refresh", "Create a MIREYE refresh spend plan for stale fields. This quotes but does not fetch or spend credits.", {"snapshot_id": {"type": "string"}}, ["snapshot_id"]),
    _tool("confirm_and_refresh_evidence", "Refresh only a previously quoted spend plan. It succeeds only when the application has separately confirmed that plan.", {"spend_plan_id": {"type": "string"}}, ["spend_plan_id"]),
    _tool("build_world_snapshot", "Select and build source-backed physical context relevant to this project.", {
        "snapshot_id": {"type": "string"},
        "requested_layers": {"type": "array", "items": {"type": "string", "enum": ["terrain", "roads", "buildings", "water", "land_cover", "transmission"]}},
    }, ["snapshot_id", "requested_layers"]),
    _tool("optimize_layout", "Deterministically reposition the existing campus planning envelope within a requested parcel setback.", {"object_id": {"type": "string"}, "minimum_setback_m": {"type": "number"}}, ["object_id", "minimum_setback_m"]),
    _tool("branch_scenario", "Create a persisted scenario branch from the active scenario.", {"scenario_id": {"type": "string"}, "user_intent": {"type": "string"}}, ["scenario_id", "user_intent"]),
    _tool("compare_scenarios", "Run deterministic comparison for two persisted scenarios.", {"left_scenario_id": {"type": "string"}, "right_scenario_id": {"type": "string"}}, ["left_scenario_id", "right_scenario_id"]),
    _tool("remove_object", "Remove one proposed object from this in-memory session.", {"object_id": {"type": "string"}}, ["object_id"]),
    _tool("reset_proposals", "Remove all proposed objects from this in-memory session.", {"snapshot_id": {"type": "string"}}, ["snapshot_id"]),
]

REQUIREMENT_CONSTRAINT_VALUE = {
    "type": "object",
    "properties": {
        "constraint_id": {"type": "string"}, "min_acres": NUMBER, "max_acres": NUMBER,
        "max_fraction": NUMBER, "max_degrees": NUMBER, "max_distance_m": NUMBER,
        "allowed_codes": STRING_LIST, "required_statuses": STRING_LIST, "require_operational": BOOLEAN,
    },
    "required": ["constraint_id", "min_acres", "max_acres", "max_fraction", "max_degrees", "max_distance_m", "allowed_codes", "required_statuses", "require_operational"],
    "additionalProperties": False,
}
DECISION_CUSTOM_FIELD = {
    "type": "object",
    "properties": {
        "name": {"type": "string"}, "label": {"type": "string"},
        "type": {"type": "string", "enum": ["number", "string", "string_list", "boolean"]},
        "unit": STRING, "minimum": NUMBER, "maximum": NUMBER,
    },
    "required": ["name", "label", "type", "unit", "minimum", "maximum"], "additionalProperties": False,
}
DECISION_REQUEST_INPUT = {
    "type": ["object", "null"],
    "properties": {
        "kind": {"type": "string", "enum": ["clarification", "assumption"]},
        "question": {"type": "string"}, "context": {"type": "string"}, "why_it_matters": {"type": "string"},
        "risk_level": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]}, "blocking": {"type": "boolean"},
        "input_mode": {"type": "string", "enum": ["single_choice", "multi_choice", "number", "range", "text", "boolean", "confirmation"]},
        "options": {"type": "array", "items": {
            "type": "object", "properties": {
                "id": {"type": "string"}, "label": {"type": "string"}, "description": {"type": "string"},
                "value": REQUIREMENT_CONSTRAINT_VALUE, "consequence": {"type": "string"},
            }, "required": ["id", "label", "description", "value", "consequence"], "additionalProperties": False,
        }},
        "recommended_option_id": STRING, "allow_custom": {"type": "boolean"},
        "custom_schema": {"type": ["object", "null"], "properties": {
            "constraint_id": {"type": "string"}, "fields": {"type": "array", "items": DECISION_CUSTOM_FIELD},
        }, "required": ["constraint_id", "fields"], "additionalProperties": False},
        "constraint_targets": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["kind", "question", "context", "why_it_matters", "risk_level", "blocking", "input_mode", "options", "recommended_option_id", "allow_custom", "custom_schema", "constraint_targets"],
    "additionalProperties": False,
}
ASSUMPTION_INPUT = {
    "type": ["array", "null"], "items": {
        "type": "object", "properties": {
            "assumption": {"type": "string"}, "reason": {"type": "string"},
            "confidence": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
            "overridable": {"type": "boolean"}, "constraint": REQUIREMENT_CONSTRAINT_VALUE,
        },
        "required": ["assumption", "reason", "confidence", "overridable", "constraint"], "additionalProperties": False,
    },
}

DILIGENCE_TOOL_DEFINITIONS = [
    _tool("compile_project_request", "Read the persisted request, workflow state, requirement gaps, and machine-only constraint capability schemas.", {"project_id": {"type": "string"}}, ["project_id"]),
    _tool("request_user_decision", "Apply the agent policy. For ASK_USER the model supplies dynamic conversational content and typed values; the application validates all capability semantics.", {
        "project_id": {"type": "string"}, "mode": {"type": "string", "enum": ["AUTO_CONTINUE", "ASSUME_AND_CONTINUE", "ASK_USER", "HARD_BLOCK"]},
        "decision_request": DECISION_REQUEST_INPUT, "assumptions": ASSUMPTION_INPUT,
    }, ["project_id", "mode", "decision_request", "assumptions"]),
    _tool("get_discovery_capabilities", "Report the real candidate-provider boundary. Statewide inverse search is unavailable.", {"project_id": {"type": "string"}}, ["project_id"]),
    _tool("enumerate_supplied_candidates", "Page through only the customer-supplied candidate list.", {"project_id": {"type": "string"}, "cursor": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["project_id", "cursor", "limit"]),
    _tool("resolve_candidate", "Resolve one supplied candidate with MIREYE. Application confirmation is required.", {"project_id": {"type": "string"}, "candidate_id": {"type": "string"}}, ["project_id", "candidate_id"]),
    _tool("plan_mireye_fields", "Compute the minimum catalog field selection required by the typed constraints.", {"project_id": {"type": "string"}}, ["project_id"]),
    _tool("quote_mireye_enrichment", "Resolve remaining supplied candidates and quote the exact shared field selection without fetching.", {"project_id": {"type": "string"}}, ["project_id"]),
    _tool("confirm_and_fetch_enrichment", "Execute only a previously quoted enrichment plan confirmed by the application.", {"project_id": {"type": "string"}, "spend_plan_id": {"type": "string"}}, ["project_id", "spend_plan_id"]),
    _tool("evaluate_candidates", "Return deterministic candidate evaluations created from immutable SiteSnapshots.", {"project_id": {"type": "string"}}, ["project_id"]),
    _tool("rank_candidates", "Order candidates deterministically by failure, uncertainty, and pass counts; never invent a suitability score.", {"project_id": {"type": "string"}}, ["project_id"]),
    _tool("compare_candidates", "Compare deterministic outcomes, values, units, and evidence IDs for selected candidates.", {"project_id": {"type": "string"}, "candidate_ids": {"type": "array", "items": {"type": "string"}, "minItems": 2}}, ["project_id", "candidate_ids"]),
    _tool("check_evidence_freshness", "Check field-level freshness for enriched project candidates without fetching.", {"project_id": {"type": "string"}}, ["project_id"]),
    _tool("quote_mireye_refresh", "Quote the minimum stale fields for one enriched candidate without fetching.", {"project_id": {"type": "string"}, "candidate_id": {"type": "string"}}, ["project_id", "candidate_id"]),
    _tool("confirm_and_refresh_evidence", "Execute a candidate refresh only for an application-confirmed spend plan.", {"project_id": {"type": "string"}, "candidate_id": {"type": "string"}, "spend_plan_id": {"type": "string"}}, ["project_id", "candidate_id", "spend_plan_id"]),
    _tool("get_evidence", "Read persisted MIREYE evidence for an enriched candidate.", {"project_id": {"type": "string"}, "candidate_id": {"type": "string"}, "evidence_ids": {"type": ["array", "null"], "items": {"type": "string"}}}, ["project_id", "candidate_id", "evidence_ids"]),
    _tool("ask_mireye_site", "Ask the documented MIREYE coordinate Q&A endpoint only after application confirmation.", {"project_id": {"type": "string"}, "candidate_id": {"type": "string"}, "question": {"type": "string"}}, ["project_id", "candidate_id", "question"]),
    _tool("build_world_snapshot", "Select and build source-backed physical context relevant to this project.", {
        "project_id": {"type": "string"}, "candidate_id": {"type": "string"},
        "requested_layers": {"type": "array", "items": {"type": "string", "enum": ["terrain", "roads", "buildings", "water", "land_cover", "transmission"]}},
    }, ["project_id", "candidate_id", "requested_layers"]),
]


class ToolValidationError(ValueError):
    pass


class ModelUnavailableError(RuntimeError):
    pass


@dataclass
class ModelReply:
    message: str
    tool_calls: list[dict]
    response_items: list[dict]


class AgentModel(Protocol):
    async def respond(self, input_items: list[dict], tools: list[dict]) -> ModelReply: ...


class OpenAIResponsesModel:
    """Minimal direct Responses API adapter; the sandbox remains tool-authoritative."""

    def __init__(self, api_key: str = OPENAI_API_KEY, model: str = SANDBOX_AGENT_MODEL, reasoning_effort: str = SANDBOX_AGENT_REASONING_EFFORT):
        self.api_key, self.model, self.reasoning_effort = api_key, model, reasoning_effort

    async def respond(self, input_items: list[dict], tools: list[dict]) -> ModelReply:
        return await self.respond_with_instructions(input_items, tools, SYSTEM_INSTRUCTIONS)

    async def respond_with_instructions(self, input_items: list[dict], tools: list[dict], instructions: str) -> ModelReply:
        if not self.api_key:
            raise ModelUnavailableError("Sandbox chat requires OPENAI_API_KEY configuration.")
        payload = {"model": self.model, "reasoning": {"effort": self.reasoning_effort}, "instructions": instructions, "input": input_items, "tools": tools, "tool_choice": "auto", "parallel_tool_calls": False, "store": False}
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
            try:
                response = await client.post("https://api.openai.com/v1/responses", headers={"Authorization": f"Bearer {self.api_key}"}, json=payload)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = response.json().get("error", {}).get("message", "OpenAI rejected the sandbox chat request.")
                raise ModelUnavailableError(detail) from exc
            except httpx.RequestError as exc:
                raise ModelUnavailableError("OpenAI sandbox chat is temporarily unavailable.") from exc
        body = response.json()
        calls = []
        for item in body.get("output", []):
            if item.get("type") == "function_call":
                calls.append({"id": item.get("call_id"), "name": item.get("name"), "arguments": item.get("arguments")})
        message = body.get("output_text", "")
        if not message:
            message = "\n".join(part.get("text", "") for item in body.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text")
        return ModelReply(message=message, tool_calls=calls, response_items=body.get("output", []))


@dataclass
class SandboxSession:
    scene_state: dict
    last_evaluation: dict | None = None
    active_scenario_id: str | None = None
    workspace_id: str | None = None
    requested_constraints: list[dict] = field(default_factory=list)
    scenario_ids: list[str] = field(default_factory=list)


class InMemorySandboxSessions:
    def __init__(self):
        self._sessions: dict[tuple[str, str], SandboxSession] = {}

    def get(self, snapshot: dict, session_id: str) -> SandboxSession:
        key = (snapshot["snapshot_id"], session_id)
        if key not in self._sessions:
            scene_state = scene_state_from_snapshot(snapshot)
            scene_state["proposed"] = []
            self._sessions[key] = SandboxSession(scene_state=scene_state)
        return self._sessions[key]


class SandboxToolExecutor:
    """The sole mutable path for an in-memory proposal scene."""

    def __init__(self, snapshot: dict, session: SandboxSession):
        self.snapshot, self.session = snapshot, session

    def execute(self, name: str, arguments: Any) -> dict:
        if not isinstance(arguments, dict):
            raise ToolValidationError("Tool arguments must be a JSON object.")
        handlers = {
            "get_site_context": self._get_site_context,
            "propose_data_center": self._propose_data_center,
            "transform_object": self._transform_object,
            "evaluate_scenario": self._evaluate_scenario,
            "optimize_layout": self._optimize_layout,
            "get_evidence": self._get_evidence,
            "remove_object": self._remove_object,
            "reset_proposals": self._reset_proposals,
        }
        if name not in handlers:
            raise ToolValidationError(f"Tool is not available: {name}.")
        return handlers[name](arguments)

    @staticmethod
    def _only(arguments: dict, allowed: set[str], required: set[str]) -> None:
        unexpected, missing = set(arguments) - allowed, required - set(arguments)
        if unexpected:
            raise ToolValidationError(f"Unexpected tool arguments: {', '.join(sorted(unexpected))}.")
        if missing:
            raise ToolValidationError(f"Missing tool arguments: {', '.join(sorted(missing))}.")

    def _bump(self) -> None:
        self.session.scene_state["scene_version"] = int(self.session.scene_state.get("scene_version", 0)) + 1

    def _validate_scene(self, scene: dict) -> None:
        for object_state in scene.get("proposed", []):
            if object_state.get("origin") != "PROPOSED":
                raise ToolValidationError("Only PROPOSED objects may be changed.")
            try:
                parent_footprint = build_oriented_footprint(object_state)
                component_footprints = []
                for component in object_state.get("components", []):
                    footprint = build_oriented_footprint(campus_component_object(object_state, component))
                    if not parent_footprint.covers(footprint):
                        raise ToolValidationError(f"Proposed component leaves the campus planning envelope: {component['id']}.")
                    if any(footprint.intersects(other) for other in component_footprints):
                        raise ToolValidationError(f"Proposed component overlaps another campus component: {component['id']}.")
                    component_footprints.append(footprint)
            except SceneValidationError as exc:
                raise ToolValidationError(str(exc)) from exc

    def _commit(self, scene: dict) -> None:
        self._validate_scene(scene)
        self.session.scene_state = scene
        self._bump()

    def _find_object(self, object_id: str, scene: dict) -> dict:
        for object_state in scene.get("proposed", []):
            if object_state.get("id") == object_id:
                return object_state
        raise ToolValidationError(f"Proposed object not found: {object_id}.")

    def _find_target(self, object_id: str, scene: dict) -> tuple[dict, dict | None]:
        for object_state in scene.get("proposed", []):
            if object_state.get("id") == object_id:
                return object_state, None
            for component in object_state.get("components", []):
                if component.get("id") == object_id:
                    return component, object_state
        raise ToolValidationError(f"Proposed object not found: {object_id}.")

    def _get_site_context(self, arguments: dict) -> dict:
        self._only(arguments, {"snapshot_id"}, {"snapshot_id"})
        if arguments["snapshot_id"] != self.snapshot["snapshot_id"]:
            raise ToolValidationError("Tool snapshot_id does not match this session.")
        return {
            "parcel_identity": copy.deepcopy(self.snapshot["parcel_identity"]),
            "observed_geometry": {"type": self.snapshot["geometry"].get("type"), "source": self.snapshot["parcel_identity"].get("parcel_data_source"), "origin": "OBSERVED"},
            "evidence_summary": {key: {"status": value.get("status"), "scope": value.get("scope"), "source": value.get("source"), "expires_at": value.get("expires_at")} for key, value in self.snapshot.get("evidence", {}).items()},
            "available_constraints": ["footprint_inside_parcel", "minimum_setback", "footprint_area", "parcel_coverage", "object_collision", "max_nwi_wetland_fraction_of_parcel", "max_nwi_wetland_acres_on_parcel", "resolution_point_outside_fema_sfha", "max_resolution_point_slope_degrees", "max_resolution_point_substation_distance_m", "max_resolution_point_transmission_distance_m", "max_resolution_point_major_road_distance_m", "parcel_zoning_code_in"],
            "unresolved_constraints": ["parcel_outside_fema_sfha", "footprint_outside_fema_sfha", "max_slope_degrees", "industrial_zoning", "legal_access", "heavy_haul_suitability", "utilities_available", "utility_capacity", "substation_available_capacity_mw", "transmission_available_capacity_mw", "sufficient_grid_capacity"],
            "scene_state": copy.deepcopy(self.session.scene_state),
        }

    def _propose_data_center(self, arguments: dict) -> dict:
        allowed = {"capacity_mw", "width_m", "length_m", "height_m", "position", "rotation_deg", "minimum_setback_m", "elements"}
        self._only(arguments, allowed, allowed)
        try:
            proposal = generate_data_center_proposal(
                self.snapshot,
                self.session.scene_state,
                capacity_mw=arguments["capacity_mw"],
                width_m=arguments["width_m"],
                length_m=arguments["length_m"],
                height_m=arguments["height_m"],
                position=arguments["position"],
                rotation_deg=arguments["rotation_deg"],
                minimum_setback_m=arguments["minimum_setback_m"] if arguments["minimum_setback_m"] is not None else DEFAULT_MINIMUM_SETBACK_M,
                elements=arguments["elements"],
            )
        except SceneValidationError as exc:
            raise ToolValidationError(str(exc)) from exc
        if proposal["status"] in {"PLACED", "ADJUSTED"}:
            self._commit(proposal.pop("scene_state"))
            self.session.last_evaluation = proposal["evaluation"]
        proposal["scene_version"] = self.session.scene_state["scene_version"]
        proposal["scene_state"] = copy.deepcopy(self.session.scene_state)
        return proposal

    def _transform_object(self, arguments: dict) -> dict:
        allowed = {"object_id", "operation", "delta_x_m", "delta_y_m", "width_m", "length_m", "height_m", "rotation_deg", "capacity_mw"}
        self._only(arguments, allowed, allowed)
        scene = copy.deepcopy(self.session.scene_state)
        object_state, parent = self._find_target(arguments["object_id"], scene)
        operation = arguments["operation"]
        geometry = object_state["geometry_local"] if parent is None else object_state["geometry_relative"]
        if operation == "move":
            if arguments["delta_x_m"] is None or arguments["delta_y_m"] is None:
                raise ToolValidationError("move requires delta_x_m and delta_y_m.")
            try:
                delta_x, delta_y = float(arguments["delta_x_m"]), float(arguments["delta_y_m"])
                if parent is None:
                    geometry["center_xy_m"] = [float(geometry["center_xy_m"][0]) + delta_x, float(geometry["center_xy_m"][1]) + delta_y]
                else:
                    radians = math.radians(float(parent["geometry_local"]["rotation_deg"]))
                    parent_x = delta_x * math.cos(radians) + delta_y * math.sin(radians)
                    parent_y = -delta_x * math.sin(radians) + delta_y * math.cos(radians)
                    geometry["center_uv"] = [
                        float(geometry["center_uv"][0]) + parent_x / float(parent["geometry_local"]["width_m"]),
                        float(geometry["center_uv"][1]) + parent_y / float(parent["geometry_local"]["length_m"]),
                    ]
            except (TypeError, ValueError) as exc:
                raise ToolValidationError("move deltas must be numeric.") from exc
        elif operation == "resize":
            changed = False
            for key in ("width_m", "length_m", "height_m"):
                if arguments[key] is not None:
                    target_key = key
                    value = arguments[key]
                    if parent is not None and key in {"width_m", "length_m"}:
                        target_key = "width_ratio" if key == "width_m" else "length_ratio"
                        value = float(value) / float(parent["geometry_local"][key])
                    geometry[target_key] = value
                    changed = True
            if not changed:
                raise ToolValidationError("resize requires width_m, length_m, or height_m.")
        elif operation == "rotate":
            if arguments["rotation_deg"] is None:
                raise ToolValidationError("rotate requires rotation_deg.")
            if parent is None:
                geometry["rotation_deg"] = arguments["rotation_deg"]
            else:
                geometry["rotation_offset_deg"] = float(arguments["rotation_deg"]) - float(parent["geometry_local"]["rotation_deg"])
        elif operation == "set_capacity":
            if arguments["capacity_mw"] is None:
                raise ToolValidationError("set_capacity requires capacity_mw.")
            if parent is not None:
                raise ToolValidationError("Set capacity on the campus, not an individual conceptual component.")
            try:
                capacity = float(arguments["capacity_mw"])
            except (TypeError, ValueError) as exc:
                raise ToolValidationError("capacity_mw must be numeric.") from exc
            if capacity <= 0:
                raise ToolValidationError("capacity_mw must be positive.")
            object_state["attributes"]["capacity_mw"] = capacity
            target = max(capacity, float(object_state["attributes"].get("expansion_target_mw", capacity)))
            object_state["attributes"]["expansion_target_mw"] = target
            halls = [item for item in object_state.get("components", []) if item.get("kind") == "data_hall"]
            for hall in halls:
                hall["attributes"]["capacity_mw"] = round(capacity / len(halls), 3)
            reserve = next((item for item in object_state.get("components", []) if item.get("kind") == "expansion_reserve"), None)
            if reserve:
                reserve["attributes"]["capacity_mw"] = round(target - capacity, 3)
        else:
            raise ToolValidationError("operation must be move, resize, rotate, or set_capacity.")
        self._commit(scene)
        return {"object_id": object_state["id"], "scene_version": self.session.scene_state["scene_version"], "scene_state": copy.deepcopy(self.session.scene_state)}

    def _evaluate_scenario(self, arguments: dict) -> dict:
        self._only(arguments, {"requested_constraints"}, {"requested_constraints"})
        try:
            evaluation = evaluate_site(self.snapshot, self.session.scene_state, arguments["requested_constraints"])
        except SceneValidationError as exc:
            raise ToolValidationError(str(exc)) from exc
        self.session.last_evaluation = evaluation
        return evaluation

    def _get_evidence(self, arguments: dict) -> dict:
        self._only(arguments, {"evidence_ids", "constraint_id"}, {"evidence_ids", "constraint_id"})
        if not isinstance(arguments["evidence_ids"], list) or not all(isinstance(item, str) for item in arguments["evidence_ids"]):
            raise ToolValidationError("evidence_ids must be a list of strings.")
        evidence = self.snapshot.get("evidence", {})
        return {"constraint_id": arguments["constraint_id"], "evidence": {item: copy.deepcopy(evidence[item]) for item in arguments["evidence_ids"] if item in evidence}}

    def _optimize_layout(self, arguments: dict) -> dict:
        self._only(arguments, {"object_id", "minimum_setback_m"}, {"object_id", "minimum_setback_m"})
        current = self._find_object(arguments["object_id"], self.session.scene_state)
        geometry = current["geometry_local"]
        try:
            proposal = generate_data_center_proposal(
                self.snapshot, self.session.scene_state,
                capacity_mw=current["attributes"]["capacity_mw"],
                width_m=geometry["width_m"], length_m=geometry["length_m"], height_m=geometry["height_m"],
                position=None, rotation_deg=None, minimum_setback_m=arguments["minimum_setback_m"],
                elements=current.get("attributes", {}).get("selected_elements"),
            )
        except SceneValidationError as exc:
            raise ToolValidationError(str(exc)) from exc
        if proposal["status"] in {"PLACED", "ADJUSTED"}:
            self._commit(proposal.pop("scene_state"))
            self.session.last_evaluation = proposal["evaluation"]
        proposal["scene_state"] = copy.deepcopy(self.session.scene_state)
        proposal["scene_version"] = self.session.scene_state["scene_version"]
        return proposal

    def _remove_object(self, arguments: dict) -> dict:
        self._only(arguments, {"object_id"}, {"object_id"})
        scene = copy.deepcopy(self.session.scene_state)
        _target, parent = self._find_target(arguments["object_id"], scene)
        if parent is None:
            scene["proposed"] = [item for item in scene["proposed"] if item["id"] != arguments["object_id"]]
        else:
            parent["components"] = [item for item in parent["components"] if item["id"] != arguments["object_id"]]
        self._commit(scene)
        return {"removed_object_id": arguments["object_id"], "scene_version": self.session.scene_state["scene_version"], "scene_state": copy.deepcopy(self.session.scene_state)}

    def _reset_proposals(self, arguments: dict) -> dict:
        self._only(arguments, {"snapshot_id"}, {"snapshot_id"})
        if arguments["snapshot_id"] != self.snapshot["snapshot_id"]:
            raise ToolValidationError("Tool snapshot_id does not match this session.")
        scene = copy.deepcopy(self.session.scene_state)
        scene["proposed"] = []
        self._commit(scene)
        return {"scene_version": self.session.scene_state["scene_version"], "scene_state": copy.deepcopy(self.session.scene_state)}


class SandboxAgent:
    def __init__(
        self,
        model: AgentModel | None = None,
        sessions: InMemorySandboxSessions | None = None,
        scenarios: ScenarioService | None = None,
        intelligence: SiteSnapshotService | None = None,
        diligence: Any | None = None,
    ):
        self.model = model or OpenAIResponsesModel()
        self.sessions = sessions or InMemorySandboxSessions()
        self.scenarios = scenarios
        self.intelligence = intelligence
        self.diligence = diligence

    async def chat(
        self,
        snapshot: dict,
        session_id: str,
        message: str,
        *,
        workspace_id: str | None = None,
        scenario_id: str | None = None,
        world_snapshot_id: str | None = None,
        confirmed_refresh_plan_id: str | None = None,
    ) -> dict:
        if not isinstance(message, str) or not message.strip():
            raise ToolValidationError("message must not be empty.")
        session = self.sessions.get(snapshot, session_id)
        if scenario_id is not None:
            if self.scenarios is None:
                raise ToolValidationError("Scenario persistence is unavailable.")
            scenario = self.scenarios.get(scenario_id)
            if scenario["site_snapshot_id"] != snapshot["snapshot_id"]:
                raise ToolValidationError("Scenario does not reference this SiteSnapshot.")
            session.scene_state = copy.deepcopy(scenario["scene_state"])
            session.last_evaluation = copy.deepcopy(scenario["evaluation"])
            session.requested_constraints = copy.deepcopy(scenario["requested_constraints"])
            session.active_scenario_id = scenario_id
            session.workspace_id = scenario["workspace_id"]
            if scenario_id not in session.scenario_ids:
                session.scenario_ids.append(scenario_id)
        if world_snapshot_id is not None:
            if self.scenarios is None or self.scenarios.worlds is None:
                raise ToolValidationError("WorldSnapshot support is unavailable.")
            current_world_id = session.scene_state.get("world_snapshot_id")
            if current_world_id is not None and current_world_id != world_snapshot_id:
                raise ToolValidationError("Chat session cannot switch WorldSnapshots.")
            try:
                session.scene_state = self.scenarios.worlds.anchor_scene(session.scene_state, world_snapshot_id)
            except ValueError as exc:
                raise ToolValidationError(str(exc)) from exc
        if workspace_id is not None:
            if workspace_id != snapshot.get("workspace_id"):
                raise ToolValidationError("workspace_id must match the SiteSnapshot workspace_id.")
            session.workspace_id = workspace_id
        executor = SandboxToolExecutor(snapshot, session)
        alternative_requested = bool(re.search(r"\b(second|alternative|another)\s+(?:layout|option|scenario)\b", message, re.IGNORECASE))
        initial_proposals = json.dumps(session.scene_state.get("proposed", []), sort_keys=True, separators=(",", ":"))
        alternative_guard_attempts = 0
        scenario_context = f"\n[Active scenario_id: {session.active_scenario_id}; available scenario_ids: {', '.join(session.scenario_ids)}]" if session.active_scenario_id else ""
        input_items = [{"role": "user", "content": f"[Sandbox snapshot_id: {snapshot['snapshot_id']}]{scenario_context}\n{message.strip()}"}]
        trace, final_message = [], ""
        for _ in range(10):
            reply = await self.model.respond(input_items, TOOL_DEFINITIONS)
            if not reply.tool_calls:
                tools_used = {item["tool"] for item in trace if item["status"] == "ok"}
                proposals_changed = json.dumps(session.scene_state.get("proposed", []), sort_keys=True, separators=(",", ":")) != initial_proposals
                alternative_complete = proposals_changed and "evaluate_scenario" in tools_used
                if alternative_requested and not alternative_complete and alternative_guard_attempts < 2:
                    missing = "a validated geometry change" if not proposals_changed else "deterministic evaluation"
                    input_items.append({"role": "user", "content": f"[Deterministic guard: the alternative is incomplete; {missing} is still required. Continue with the validated tools and do not narrate completion yet.]"})
                    alternative_guard_attempts += 1
                    continue
                if alternative_requested and not alternative_complete:
                    final_message = "No validated alternative layout was produced; the existing layout remains unchanged."
                    break
                final_message = reply.message or "No deterministic sandbox action was taken."
                break
            input_items.extend(reply.response_items)
            outputs = []
            for call in reply.tool_calls:
                previous_session = copy.deepcopy(session)
                try:
                    arguments = json.loads(call["arguments"]) if isinstance(call.get("arguments"), str) else call.get("arguments")
                    if call.get("name") in {"check_evidence_freshness", "quote_mireye_refresh", "confirm_and_refresh_evidence", "build_world_snapshot", "branch_scenario", "compare_scenarios"}:
                        result = await self._execute_intelligence_tool(
                            snapshot, call.get("name"), arguments,
                            confirmed_refresh_plan_id=confirmed_refresh_plan_id,
                        )
                        if call.get("name") == "branch_scenario":
                            session.active_scenario_id = result["scenario_id"]
                            session.workspace_id = result["workspace_id"]
                            session.scene_state = copy.deepcopy(result["scene_state"])
                            session.last_evaluation = copy.deepcopy(result["evaluation"])
                            session.requested_constraints = copy.deepcopy(result["requested_constraints"])
                            for known_id in (arguments["scenario_id"], result["scenario_id"]):
                                if known_id not in session.scenario_ids:
                                    session.scenario_ids.append(known_id)
                    else:
                        result = executor.execute(call.get("name"), arguments)
                    if self.scenarios and call.get("name") in {"propose_data_center", "transform_object", "optimize_layout", "remove_object", "reset_proposals", "evaluate_scenario"}:
                        if call.get("name") == "propose_data_center" and result.get("status") not in {"PLACED", "ADJUSTED"}:
                            pass
                        else:
                            if call.get("name") == "evaluate_scenario":
                                session.requested_constraints = copy.deepcopy(arguments["requested_constraints"])
                            scenario = self.scenarios.record_accepted_tool(
                                snapshot,
                                active_scenario_id=session.active_scenario_id,
                                workspace_id=session.workspace_id or snapshot["workspace_id"],
                                user_intent=message.strip(), scene_state=session.scene_state,
                                requested_constraints=session.requested_constraints or None,
                                tool_name=call["name"], arguments=arguments,
                                model_id=getattr(self.model, "model", type(self.model).__name__),
                                evaluation=result if call.get("name") == "evaluate_scenario" else None,
                            )
                            session.active_scenario_id = scenario["scenario_id"]
                            if scenario["scenario_id"] not in session.scenario_ids:
                                session.scenario_ids.append(scenario["scenario_id"])
                            session.workspace_id = scenario["workspace_id"]
                            session.scene_state = copy.deepcopy(scenario["scene_state"])
                            session.last_evaluation = copy.deepcopy(scenario["evaluation"])
                            result["scenario"] = {"scenario_id": scenario["scenario_id"], "revision": scenario["revision"]}
                    trace.append({"tool": call.get("name"), "status": "ok", "result": result})
                except (json.JSONDecodeError, KeyError, ToolValidationError, ScenarioError, TypeError, ValueError) as exc:
                    session.scene_state = previous_session.scene_state
                    session.last_evaluation = previous_session.last_evaluation
                    session.active_scenario_id = previous_session.active_scenario_id
                    session.workspace_id = previous_session.workspace_id
                    session.requested_constraints = previous_session.requested_constraints
                    session.scenario_ids = previous_session.scenario_ids
                    error = str(exc)
                    trace.append({"tool": call.get("name"), "status": "rejected", "error": error})
                    return {"message": f"Tool call rejected: {error}", "session_id": session_id, "tool_trace": trace, "scene_state": copy.deepcopy(session.scene_state), "evaluation": session.last_evaluation}
                outputs.append({"type": "function_call_output", "call_id": call.get("id"), "output": json.dumps(result, sort_keys=True, separators=(",", ":"))})
            input_items.extend(outputs)
        else:
            final_message = "Tool-call limit reached; no further action was taken."
        scenario = {"scenario_id": session.active_scenario_id, "revision": self.scenarios.get(session.active_scenario_id)["revision"]} if self.scenarios and session.active_scenario_id else None
        return {"message": final_message, "session_id": session_id, "tool_trace": trace, "scene_state": copy.deepcopy(session.scene_state), "evaluation": copy.deepcopy(session.last_evaluation), "scenario": scenario}

    async def chat_project(
        self,
        project_id: str,
        session_id: str,
        message: str,
        *,
        confirmed_resolution_project_id: str | None = None,
        confirmed_enrichment_plan_id: str | None = None,
        confirmed_refresh_plan_id: str | None = None,
        confirmed_ask_candidate_id: str | None = None,
    ) -> dict:
        if self.diligence is None:
            raise ToolValidationError("Diligence orchestration is unavailable.")
        if not isinstance(message, str) or not message.strip():
            raise ToolValidationError("message must not be empty.")
        project = self.diligence.get(project_id)
        input_items = [{"role": "user", "content": f"[Diligence project_id: {project_id}]\n{message.strip()}"}]
        trace, final_message = [], ""
        for _ in range(10):
            if hasattr(self.model, "respond_with_instructions"):
                reply = await self.model.respond_with_instructions(input_items, DILIGENCE_TOOL_DEFINITIONS, DILIGENCE_SYSTEM_INSTRUCTIONS)
            else:
                reply = await self.model.respond(input_items, DILIGENCE_TOOL_DEFINITIONS)
            if not reply.tool_calls:
                final_message = reply.message or "No deterministic diligence action was taken."
                break
            input_items.extend(reply.response_items)
            outputs = []
            for call in reply.tool_calls:
                try:
                    arguments = json.loads(call["arguments"]) if isinstance(call.get("arguments"), str) else call.get("arguments")
                    result = await self._execute_project_tool(
                        project_id, call.get("name"), arguments,
                        confirmed_resolution_project_id=confirmed_resolution_project_id,
                        confirmed_enrichment_plan_id=confirmed_enrichment_plan_id,
                        confirmed_refresh_plan_id=confirmed_refresh_plan_id,
                        confirmed_ask_candidate_id=confirmed_ask_candidate_id,
                    )
                    trace.append({"tool": call.get("name"), "status": "ok", "result": result})
                    current_project = self.diligence.get(project_id)
                    if current_project.get("active_decision"):
                        return {
                            "message": "I need one decision before I continue.", "session_id": session_id,
                            "project": current_project, "tool_trace": trace,
                        }
                except (json.JSONDecodeError, KeyError, ToolValidationError, ConfirmationRequired, TypeError, ValueError) as exc:
                    trace.append({"tool": call.get("name"), "status": "rejected", "error": str(exc)})
                    return {"message": f"Tool call rejected: {exc}", "session_id": session_id, "project": self.diligence.get(project_id), "tool_trace": trace}
                outputs.append({"type": "function_call_output", "call_id": call.get("id"), "output": json.dumps(result, sort_keys=True, separators=(",", ":"))})
            input_items.extend(outputs)
            project = self.diligence.get(project_id)
        else:
            final_message = "Tool-call limit reached; no further action was taken."
        return {"message": final_message, "session_id": session_id, "project": self.diligence.get(project_id), "tool_trace": trace}

    async def interpret_project_decision_answer(
        self, project_id: str, decision_id: str, *, resume_token: str, text: str,
    ) -> dict:
        if self.diligence is None or not isinstance(text, str) or not text.strip():
            raise ToolValidationError("A non-empty decision answer is required.")
        project = self.diligence.get(project_id)
        decision = project.get("active_decision") or {}
        current_id = decision.get("decision_id") or decision.get("id")
        if current_id != decision_id or decision.get("resume_token") != resume_token or decision.get("input_mode") != "text":
            raise ToolValidationError("This text answer does not match the active DecisionRequest.")
        submit_tool = _tool(
            "submit_decision_answer",
            "Interpret the user's text only as a typed constraint allowed by the supplied DecisionRequest schema.",
            {"constraint": REQUIREMENT_CONSTRAINT_VALUE}, ["constraint"],
        )
        instructions = "Interpret only the user's answer against the supplied custom schema. Call submit_decision_answer only when the mapping is unambiguous. Do not invent values, fields, units, or evaluator logic. If ambiguous, return a brief clarification message without a tool call."
        input_items = [{"role": "user", "content": json.dumps({
            "decision": {key: decision.get(key) for key in ("question", "context", "custom_schema", "constraint_targets")},
            "answer": text.strip(),
        }, sort_keys=True)}]
        if hasattr(self.model, "respond_with_instructions"):
            reply = await self.model.respond_with_instructions(input_items, [submit_tool], instructions)
        else:
            reply = await self.model.respond(input_items, [submit_tool])
        if len(reply.tool_calls) != 1 or reply.tool_calls[0].get("name") != "submit_decision_answer":
            raise ToolValidationError(reply.message or "The answer is ambiguous; the DecisionRequest remains active.")
        arguments = json.loads(reply.tool_calls[0]["arguments"]) if isinstance(reply.tool_calls[0].get("arguments"), str) else reply.tool_calls[0].get("arguments")
        SandboxToolExecutor._only(arguments, {"constraint"}, {"constraint"})
        return await self.diligence.answer_decision(
            project_id, decision_id, resume_token=resume_token, interpreted_constraint=arguments["constraint"],
        )

    async def _execute_project_tool(
        self,
        project_id: str,
        name: str | None,
        arguments: Any,
        *,
        confirmed_resolution_project_id: str | None,
        confirmed_enrichment_plan_id: str | None,
        confirmed_refresh_plan_id: str | None,
        confirmed_ask_candidate_id: str | None,
    ) -> dict:
        if not isinstance(arguments, dict):
            raise ToolValidationError("Tool arguments must be a JSON object.")
        if arguments.get("project_id") != project_id:
            raise ToolValidationError("Tool project_id does not match this session.")
        if name == "compile_project_request":
            SandboxToolExecutor._only(arguments, {"project_id"}, {"project_id"})
            return self.diligence.requirement_context(project_id)
        if name == "request_user_decision":
            SandboxToolExecutor._only(arguments, {"project_id", "mode", "decision_request", "assumptions"}, {"project_id", "mode", "decision_request", "assumptions"})
            return self.diligence.agent_decision(
                project_id, mode=arguments["mode"], decision_request=arguments["decision_request"], assumptions=arguments["assumptions"],
            )
        if name == "get_discovery_capabilities":
            SandboxToolExecutor._only(arguments, {"project_id"}, {"project_id"})
            return self.diligence.discovery_capabilities()
        if name == "enumerate_supplied_candidates":
            SandboxToolExecutor._only(arguments, {"project_id", "cursor", "limit"}, {"project_id", "cursor", "limit"})
            return self.diligence.candidate_page(project_id, cursor=arguments["cursor"], limit=arguments["limit"])
        if name == "resolve_candidate":
            SandboxToolExecutor._only(arguments, {"project_id", "candidate_id"}, {"project_id", "candidate_id"})
            return await self.diligence.resolve_candidate(
                project_id, arguments["candidate_id"],
                confirmed_resolution=confirmed_resolution_project_id == project_id,
            )
        if name == "plan_mireye_fields":
            SandboxToolExecutor._only(arguments, {"project_id"}, {"project_id"})
            return self.diligence.plan_fields(project_id)
        if name == "quote_mireye_enrichment":
            SandboxToolExecutor._only(arguments, {"project_id"}, {"project_id"})
            return await self.diligence.resolve_and_quote(
                project_id, confirmed_resolution=confirmed_resolution_project_id == project_id,
            )
        if name == "confirm_and_fetch_enrichment":
            SandboxToolExecutor._only(arguments, {"project_id", "spend_plan_id"}, {"project_id", "spend_plan_id"})
            return await self.diligence.confirm_and_fetch(
                project_id, arguments["spend_plan_id"],
                confirmed=confirmed_enrichment_plan_id == arguments["spend_plan_id"],
            )
        if name in {"evaluate_candidates", "rank_candidates"}:
            SandboxToolExecutor._only(arguments, {"project_id"}, {"project_id"})
            return self.diligence.rank_candidates(project_id)
        if name == "compare_candidates":
            SandboxToolExecutor._only(arguments, {"project_id", "candidate_ids"}, {"project_id", "candidate_ids"})
            return self.diligence.compare_candidates(project_id, arguments["candidate_ids"])
        if name == "check_evidence_freshness":
            SandboxToolExecutor._only(arguments, {"project_id"}, {"project_id"})
            return self.diligence.check_now(project_id)
        if name == "quote_mireye_refresh":
            SandboxToolExecutor._only(arguments, {"project_id", "candidate_id"}, {"project_id", "candidate_id"})
            return await self.diligence.quote_candidate_refresh(project_id, arguments["candidate_id"])
        if name == "confirm_and_refresh_evidence":
            SandboxToolExecutor._only(arguments, {"project_id", "candidate_id", "spend_plan_id"}, {"project_id", "candidate_id", "spend_plan_id"})
            return await self.diligence.confirm_candidate_refresh(
                project_id, arguments["candidate_id"], arguments["spend_plan_id"],
                confirmed=confirmed_refresh_plan_id == arguments["spend_plan_id"],
            )
        if name == "get_evidence":
            SandboxToolExecutor._only(arguments, {"project_id", "candidate_id", "evidence_ids"}, {"project_id", "candidate_id", "evidence_ids"})
            return self.diligence.get_evidence(project_id, arguments["candidate_id"], arguments["evidence_ids"])
        if name == "ask_mireye_site":
            SandboxToolExecutor._only(arguments, {"project_id", "candidate_id", "question"}, {"project_id", "candidate_id", "question"})
            return await self.diligence.ask_mireye_site(
                project_id, arguments["candidate_id"], arguments["question"],
                confirmed_candidate_id=confirmed_ask_candidate_id,
            )
        if name == "build_world_snapshot":
            SandboxToolExecutor._only(arguments, {"project_id", "candidate_id", "requested_layers"}, {"project_id", "candidate_id", "requested_layers"})
            return await self.diligence.build_world_snapshot(project_id, arguments["candidate_id"], requested_layers=arguments["requested_layers"])
        raise ToolValidationError(f"Tool is not available: {name}.")

    async def _execute_intelligence_tool(
        self,
        snapshot: dict,
        name: str | None,
        arguments: Any,
        *,
        confirmed_refresh_plan_id: str | None,
    ) -> dict:
        if not isinstance(arguments, dict):
            raise ToolValidationError("Tool arguments must be a JSON object.")
        if name in {"check_evidence_freshness", "quote_mireye_refresh"}:
            if self.intelligence is None:
                raise ToolValidationError("MIREYE lifecycle tools are unavailable.")
            SandboxToolExecutor._only(arguments, {"snapshot_id"}, {"snapshot_id"})
            if arguments["snapshot_id"] != snapshot["snapshot_id"]:
                raise ToolValidationError("Tool snapshot_id does not match this session.")
            if name == "check_evidence_freshness":
                return self.intelligence.freshness_status(snapshot["snapshot_id"])
            return await self.intelligence.quote_refresh(snapshot["snapshot_id"])
        if name == "confirm_and_refresh_evidence":
            if self.intelligence is None:
                raise ToolValidationError("MIREYE lifecycle tools are unavailable.")
            SandboxToolExecutor._only(arguments, {"spend_plan_id"}, {"spend_plan_id"})
            plan_id = arguments["spend_plan_id"]
            if plan_id != confirmed_refresh_plan_id:
                raise ConfirmationRequired("The application has not confirmed this MIREYE refresh spend plan.")
            plan = self.intelligence.store.get_mireye_spend_plan(plan_id)
            if plan is None or plan["snapshot_id"] != snapshot["snapshot_id"]:
                raise ToolValidationError("Refresh spend plan does not belong to this SiteSnapshot.")
            return await self.intelligence.confirm_and_refresh(plan_id, confirmed_by_application=True)
        if name == "build_world_snapshot":
            SandboxToolExecutor._only(arguments, {"snapshot_id", "requested_layers"}, {"snapshot_id", "requested_layers"})
            if arguments["snapshot_id"] != snapshot["snapshot_id"]:
                raise ToolValidationError("Tool snapshot_id does not match this session.")
            if self.scenarios is None or self.scenarios.worlds is None:
                raise ToolValidationError("WorldSnapshot support is unavailable.")
            existing = self.scenarios.worlds.latest_for_site_snapshot(snapshot["snapshot_id"])
            world = await self.scenarios.worlds.create(site_snapshot_id=snapshot["snapshot_id"], requested_layers=arguments["requested_layers"])
            return {"world_snapshot_id": world["world_snapshot_id"], "reused": existing is not None and existing["world_snapshot_id"] == world["world_snapshot_id"]}
        if name == "branch_scenario":
            SandboxToolExecutor._only(arguments, {"scenario_id", "user_intent"}, {"scenario_id", "user_intent"})
            if self.scenarios is None:
                raise ToolValidationError("Scenario persistence is unavailable.")
            source = self.scenarios.get(arguments["scenario_id"])
            if source["site_snapshot_id"] != snapshot["snapshot_id"]:
                raise ToolValidationError("Scenario does not reference this SiteSnapshot.")
            return self.scenarios.branch(arguments["scenario_id"], user_intent=arguments["user_intent"])
        if name == "compare_scenarios":
            SandboxToolExecutor._only(arguments, {"left_scenario_id", "right_scenario_id"}, {"left_scenario_id", "right_scenario_id"})
            if self.scenarios is None:
                raise ToolValidationError("Scenario persistence is unavailable.")
            return self.scenarios.compare(arguments["left_scenario_id"], arguments["right_scenario_id"])
        raise ToolValidationError(f"Tool is not available: {name}.")
