"""Constrained conversational control for mutable Site Sandbox proposals."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.config import OPENAI_API_KEY, SANDBOX_AGENT_MODEL
from app.sandbox import scene_state_from_snapshot
from app.sandbox_evaluator import SceneValidationError, build_oriented_footprint, evaluate_site
from app.sandbox_proposal import DEFAULT_MINIMUM_SETBACK_M, generate_data_center_proposal
from app.sandbox_scenarios import ScenarioError, ScenarioService


SYSTEM_INSTRUCTIONS = """You are the MIREYE Site Sandbox assistant. MIREYE/source evidence is authoritative for factual site data. OBSERVED data is factual, DERIVED data is deterministic, and PROPOSED objects are simulations. Begin every request with get_site_context using the provided snapshot ID. Use tools for all factual claims, proposal changes, and evaluations. PASS, FAIL, and UNRESOLVED only come from evaluate_scenario; never calculate or decide them yourself. For an unqualified fit request, evaluate footprint_inside_parcel, footprint_area, and parcel_coverage only. Do not request minimum_setback without a numeric minimum_m. Never invent values, parcel facts, geometry, or engineering conclusions. Do not claim engineering-grade analysis. If evidence cannot prove a request, state UNRESOLVED. Never call or suggest external data fetching."""


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "name": name, "description": description, "strict": True, "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}


NUMBER = {"type": ["number", "null"]}
STRING = {"type": ["string", "null"]}
CONSTRAINT_SPEC = {
    "type": "object",
    "properties": {
        "constraint_id": {"type": "string", "enum": ["footprint_inside_parcel", "minimum_setback", "footprint_area", "parcel_coverage", "object_collision", "max_slope_degrees", "industrial_zoning"]},
        "object_id": STRING, "minimum_m": NUMBER, "min_m2": NUMBER, "max_m2": NUMBER, "max_percent": NUMBER, "max_degrees": NUMBER,
    },
    "required": ["constraint_id", "object_id", "minimum_m", "min_m2", "max_m2", "max_percent", "max_degrees"],
    "additionalProperties": False,
}
TOOL_DEFINITIONS = [
    _tool("get_site_context", "Read immutable site facts, evidence summary, available constraints, and the current session scene.", {"snapshot_id": {"type": "string"}}, ["snapshot_id"]),
    _tool("propose_data_center", "Generate a parcel-derived conceptual data-center placement. Omitted dimensions may be uniformly reduced; explicit dimensions remain fixed. Position is local meters.", {
        "capacity_mw": {"type": "number"}, "width_m": NUMBER, "length_m": NUMBER, "height_m": NUMBER,
        "position": {"type": ["object", "null"], "properties": {"x_m": NUMBER, "y_m": NUMBER}, "required": ["x_m", "y_m"], "additionalProperties": False}, "rotation_deg": NUMBER,
        "minimum_setback_m": NUMBER,
    }, ["capacity_mw", "width_m", "length_m", "height_m", "position", "rotation_deg", "minimum_setback_m"]),
    _tool("transform_object", "Move, resize, rotate, or set capacity on one proposed object. Move uses local-meter deltas; rotate sets an absolute degree value.", {
        "object_id": {"type": "string"}, "operation": {"type": "string", "enum": ["move", "resize", "rotate", "set_capacity"]},
        "delta_x_m": NUMBER, "delta_y_m": NUMBER, "width_m": NUMBER, "length_m": NUMBER, "height_m": NUMBER, "rotation_deg": NUMBER, "capacity_mw": NUMBER,
    }, ["object_id", "operation", "delta_x_m", "delta_y_m", "width_m", "length_m", "height_m", "rotation_deg", "capacity_mw"]),
    _tool("evaluate_scenario", "Run the deterministic evaluator. Use only supported constraint IDs and report its output exactly.", {"requested_constraints": {"type": "array", "items": CONSTRAINT_SPEC}}, ["requested_constraints"]),
    _tool("get_evidence", "Read stored factual evidence only; this never fetches new data.", {"evidence_ids": {"type": "array", "items": {"type": "string"}}, "constraint_id": STRING}, ["evidence_ids", "constraint_id"]),
    _tool("remove_object", "Remove one proposed object from this in-memory session.", {"object_id": {"type": "string"}}, ["object_id"]),
    _tool("reset_proposals", "Remove all proposed objects from this in-memory session.", {"snapshot_id": {"type": "string"}}, ["snapshot_id"]),
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

    def __init__(self, api_key: str = OPENAI_API_KEY, model: str = SANDBOX_AGENT_MODEL):
        self.api_key, self.model = api_key, model

    async def respond(self, input_items: list[dict], tools: list[dict]) -> ModelReply:
        if not self.api_key:
            raise ModelUnavailableError("Sandbox chat requires OPENAI_API_KEY configuration.")
        payload = {"model": self.model, "instructions": SYSTEM_INSTRUCTIONS, "input": input_items, "tools": tools, "tool_choice": "auto", "parallel_tool_calls": False, "store": False}
        async with httpx.AsyncClient(timeout=45) as client:
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
                build_oriented_footprint(object_state)
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

    def _get_site_context(self, arguments: dict) -> dict:
        self._only(arguments, {"snapshot_id"}, {"snapshot_id"})
        if arguments["snapshot_id"] != self.snapshot["snapshot_id"]:
            raise ToolValidationError("Tool snapshot_id does not match this session.")
        return {
            "parcel_identity": copy.deepcopy(self.snapshot["parcel_identity"]),
            "observed_geometry": {"type": self.snapshot["geometry"].get("type"), "source": self.snapshot["parcel_identity"].get("parcel_data_source"), "origin": "OBSERVED"},
            "evidence_summary": {key: {"status": value.get("status"), "source": value.get("source"), "expires_at": value.get("expires_at")} for key, value in self.snapshot.get("evidence", {}).items()},
            "available_constraints": ["footprint_inside_parcel", "minimum_setback", "footprint_area", "parcel_coverage", "object_collision"],
            "unresolved_constraints": ["max_slope_degrees", "industrial_zoning"],
            "scene_state": copy.deepcopy(self.session.scene_state),
        }

    def _propose_data_center(self, arguments: dict) -> dict:
        allowed = {"capacity_mw", "width_m", "length_m", "height_m", "position", "rotation_deg", "minimum_setback_m"}
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
        object_state = self._find_object(arguments["object_id"], scene)
        operation, geometry = arguments["operation"], object_state["geometry_local"]
        if operation == "move":
            if arguments["delta_x_m"] is None or arguments["delta_y_m"] is None:
                raise ToolValidationError("move requires delta_x_m and delta_y_m.")
            try:
                geometry["center_xy_m"] = [float(geometry["center_xy_m"][0]) + float(arguments["delta_x_m"]), float(geometry["center_xy_m"][1]) + float(arguments["delta_y_m"])]
            except (TypeError, ValueError) as exc:
                raise ToolValidationError("move deltas must be numeric.") from exc
        elif operation == "resize":
            changed = False
            for key in ("width_m", "length_m", "height_m"):
                if arguments[key] is not None:
                    geometry[key] = arguments[key]
                    changed = True
            if not changed:
                raise ToolValidationError("resize requires width_m, length_m, or height_m.")
        elif operation == "rotate":
            if arguments["rotation_deg"] is None:
                raise ToolValidationError("rotate requires rotation_deg.")
            geometry["rotation_deg"] = arguments["rotation_deg"]
        elif operation == "set_capacity":
            if arguments["capacity_mw"] is None:
                raise ToolValidationError("set_capacity requires capacity_mw.")
            object_state["attributes"]["capacity_mw"] = arguments["capacity_mw"]
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

    def _remove_object(self, arguments: dict) -> dict:
        self._only(arguments, {"object_id"}, {"object_id"})
        scene = copy.deepcopy(self.session.scene_state)
        self._find_object(arguments["object_id"], scene)
        scene["proposed"] = [item for item in scene["proposed"] if item["id"] != arguments["object_id"]]
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
    def __init__(self, model: AgentModel | None = None, sessions: InMemorySandboxSessions | None = None, scenarios: ScenarioService | None = None):
        self.model = model or OpenAIResponsesModel()
        self.sessions = sessions or InMemorySandboxSessions()
        self.scenarios = scenarios

    async def chat(self, snapshot: dict, session_id: str, message: str, *, workspace_id: str | None = None, scenario_id: str | None = None) -> dict:
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
        if workspace_id is not None:
            if workspace_id != snapshot.get("workspace_id"):
                raise ToolValidationError("workspace_id must match the SiteSnapshot workspace_id.")
            session.workspace_id = workspace_id
        executor = SandboxToolExecutor(snapshot, session)
        input_items = [{"role": "user", "content": f"[Sandbox snapshot_id: {snapshot['snapshot_id']}]\n{message.strip()}"}]
        trace, final_message = [], ""
        for _ in range(8):
            reply = await self.model.respond(input_items, TOOL_DEFINITIONS)
            if not reply.tool_calls:
                final_message = reply.message or "No deterministic sandbox action was taken."
                break
            input_items.extend(reply.response_items)
            outputs = []
            for call in reply.tool_calls:
                previous_session = copy.deepcopy(session)
                try:
                    arguments = json.loads(call["arguments"]) if isinstance(call.get("arguments"), str) else call.get("arguments")
                    result = executor.execute(call.get("name"), arguments)
                    if self.scenarios and call.get("name") in {"propose_data_center", "transform_object", "remove_object", "reset_proposals", "evaluate_scenario"}:
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
                            session.workspace_id = scenario["workspace_id"]
                            session.last_evaluation = copy.deepcopy(scenario["evaluation"])
                            result["scenario"] = {"scenario_id": scenario["scenario_id"], "revision": scenario["revision"]}
                    trace.append({"tool": call.get("name"), "status": "ok", "result": result})
                except (json.JSONDecodeError, KeyError, ToolValidationError, ScenarioError, TypeError, ValueError) as exc:
                    session.scene_state = previous_session.scene_state
                    session.last_evaluation = previous_session.last_evaluation
                    session.active_scenario_id = previous_session.active_scenario_id
                    session.workspace_id = previous_session.workspace_id
                    session.requested_constraints = previous_session.requested_constraints
                    error = str(exc)
                    trace.append({"tool": call.get("name"), "status": "rejected", "error": error})
                    return {"message": f"Tool call rejected: {error}", "session_id": session_id, "tool_trace": trace, "scene_state": copy.deepcopy(session.scene_state), "evaluation": session.last_evaluation}
                outputs.append({"type": "function_call_output", "call_id": call.get("id"), "output": json.dumps(result, sort_keys=True, separators=(",", ":"))})
            input_items.extend(outputs)
        else:
            final_message = "Tool-call limit reached; no further action was taken."
        scenario = {"scenario_id": session.active_scenario_id, "revision": self.scenarios.get(session.active_scenario_id)["revision"]} if self.scenarios and session.active_scenario_id else None
        return {"message": final_message, "session_id": session_id, "tool_trace": trace, "scene_state": copy.deepcopy(session.scene_state), "evaluation": copy.deepcopy(session.last_evaluation), "scenario": scenario}
