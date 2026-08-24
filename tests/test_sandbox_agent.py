import asyncio
import copy
import json

import httpx
import pytest

from app.sandbox import scene_state_from_snapshot, serialize_scene_state
from app.sandbox_agent import InMemorySandboxSessions, ModelReply, ModelUnavailableError, OpenAIResponsesModel, SandboxAgent, SandboxSession, SandboxToolExecutor, TOOL_DEFINITIONS, ToolValidationError


def _run(coro):
    return asyncio.run(coro)


def snapshot():
    return {
        "snapshot_id": "site-agent-test",
        "is_expired": False,
        "parcel_identity": {"parcel_id": "parcel-1", "parcel_data_source": "MIREYE_TEST", "parcel_match_type": "exact_intersect", "parcel_match_distance_m": 0.0, "selected_point": {"lat": 0.0, "lng": 0.0}},
        "geometry": {"type": "Polygon", "coordinates": [[[-0.005, -0.005], [0.005, -0.005], [0.005, 0.005], [-0.005, 0.005], [-0.005, -0.005]]]},
        "evidence": {name: {"value": value, "status": "ok", "source": "MIREYE_TEST"} for name, value in {
            "parcel_id": "parcel-1", "parcel_boundary_geojson": "authoritative-boundary", "parcel_match_type": "exact_intersect", "parcel_match_distance_m": 0.0, "slope_degrees": 2.0,
        }.items()},
    }


class ScriptedModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def respond(self, input_items, tools):
        self.calls.append({"input": copy.deepcopy(input_items), "tools": tools})
        return self.replies.pop(0)


def test_openai_http_rejection_is_a_clear_model_unavailable_error(monkeypatch):
    request_payload = {}

    class RejectedResponse:
        def raise_for_status(self):
            request = httpx.Request("POST", "https://api.openai.com/v1/responses")
            response = httpx.Response(404, request=request, json={"error": {"message": "Model access is unavailable."}})
            raise httpx.HTTPStatusError("not found", request=request, response=response)

        @staticmethod
        def json():
            return {"error": {"message": "Model access is unavailable."}}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **kwargs):
            request_payload.update(kwargs["json"])
            return RejectedResponse()

    monkeypatch.setattr("app.sandbox_agent.httpx.AsyncClient", lambda **_kwargs: Client())
    with pytest.raises(ModelUnavailableError, match="Model access is unavailable"):
        _run(OpenAIResponsesModel(api_key="test-key").respond([], []))
    assert request_payload["model"] == "gpt-5.6-sol"
    assert request_payload["reasoning"] == {"effort": "high"}


def test_openai_transport_failure_is_a_clear_model_unavailable_error(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            raise httpx.ConnectError("blocked")

    monkeypatch.setattr("app.sandbox_agent.httpx.AsyncClient", lambda **_kwargs: Client())
    with pytest.raises(ModelUnavailableError, match="temporarily unavailable"):
        _run(OpenAIResponsesModel(api_key="test-key").respond([], []))


def call(name, arguments, call_id="call-1"):
    return ModelReply(message="", tool_calls=[{"id": call_id, "name": name, "arguments": json.dumps(arguments)}], response_items=[])


def executor():
    site = snapshot()
    return site, SandboxToolExecutor(site, SandboxSession(scene_state_from_snapshot(site)))


def test_natural_language_create_and_evaluate_flow_uses_tools_only():
    model = ScriptedModel([
        call("get_site_context", {"snapshot_id": "site-agent-test"}),
        call("propose_data_center", {"capacity_mw": 100, "width_m": None, "length_m": None, "height_m": None, "position": None, "rotation_deg": None, "minimum_setback_m": 10, "elements": ["data_halls", "electrical_area", "cooling_plant", "internal_access", "expansion_reserve"]}),
        call("evaluate_scenario", {"requested_constraints": [
            {"constraint_id": "footprint_inside_parcel"}, {"constraint_id": "footprint_area"}, {"constraint_id": "parcel_coverage", "max_percent": 20},
        ]}),
        ModelReply(message="The conceptual 100 MW data center was evaluated using the deterministic sandbox.", tool_calls=[], response_items=[]),
    ])
    agent = SandboxAgent(model=model, sessions=InMemorySandboxSessions())

    response = _run(agent.chat(snapshot(), "session-1", "Put a 100 MW data center on this parcel."))

    assert response["evaluation"]["overall_status"] == "PASS"
    assert response["scene_state"]["proposed"][0]["attributes"]["capacity_mw"] == 100
    assert "service_parking" not in {item["kind"] for item in response["scene_state"]["proposed"][0]["components"]}
    assert [item["tool"] for item in response["tool_trace"]] == ["get_site_context", "propose_data_center", "evaluate_scenario"]
    assert "snapshot_id: site-agent-test" in model.calls[0]["input"][0]["content"]
    assert all("mireye" not in item["tool"] for item in response["tool_trace"])


def test_move_resize_and_rotate_validate_mutable_proposal():
    _site, tools = executor()
    original = copy.deepcopy(tools.session.scene_state["proposed"][0]["geometry_local"])
    tools.execute("transform_object", {"object_id": "data_center_1", "operation": "move", "delta_x_m": 0, "delta_y_m": 200, "width_m": None, "length_m": None, "height_m": None, "rotation_deg": None, "capacity_mw": None})
    tools.execute("transform_object", {"object_id": "data_center_1", "operation": "resize", "delta_x_m": None, "delta_y_m": None, "width_m": 300, "length_m": 400, "height_m": None, "rotation_deg": None, "capacity_mw": None})
    tools.execute("transform_object", {"object_id": "data_center_1", "operation": "rotate", "delta_x_m": None, "delta_y_m": None, "width_m": None, "length_m": None, "height_m": None, "rotation_deg": 45, "capacity_mw": None})
    tools.execute("transform_object", {"object_id": "data_center_1", "operation": "set_capacity", "delta_x_m": None, "delta_y_m": None, "width_m": None, "length_m": None, "height_m": None, "rotation_deg": None, "capacity_mw": 150})

    geometry = tools.session.scene_state["proposed"][0]["geometry_local"]
    assert geometry["center_xy_m"][1] == original["center_xy_m"][1] + 200
    assert (geometry["width_m"], geometry["length_m"], geometry["rotation_deg"]) == (300, 400, 45)
    campus = tools.session.scene_state["proposed"][0]
    assert campus["attributes"]["capacity_mw"] == 150
    halls = [item for item in campus["components"] if item["kind"] == "data_hall"]
    assert sum(item["attributes"]["capacity_mw"] for item in halls) == 150
    assert next(item for item in campus["components"] if item["kind"] == "expansion_reserve")["attributes"]["capacity_mw"] == 150


def test_component_move_is_validated_inside_campus_envelope():
    _site, tools = executor()
    campus = tools.session.scene_state["proposed"][0]
    original = copy.deepcopy(next(item for item in campus["components"] if item["id"] == "electrical_yard")["geometry_relative"])

    tools.execute("transform_object", {
        "object_id": "electrical_yard", "operation": "move", "delta_x_m": 10, "delta_y_m": 0,
        "width_m": None, "length_m": None, "height_m": None, "rotation_deg": None, "capacity_mw": None,
    })
    moved = next(item for item in tools.session.scene_state["proposed"][0]["components"] if item["id"] == "electrical_yard")
    assert moved["geometry_relative"]["center_uv"][0] > original["center_uv"][0]

    with pytest.raises(ToolValidationError, match="leaves the campus planning envelope"):
        tools.execute("transform_object", {
            "object_id": "electrical_yard", "operation": "move", "delta_x_m": 1000, "delta_y_m": 0,
            "width_m": None, "length_m": None, "height_m": None, "rotation_deg": None, "capacity_mw": None,
        })


def test_evaluation_tool_returns_deterministic_authority_output():
    _site, tools = executor()
    first = tools.execute("evaluate_scenario", {"requested_constraints": [{"constraint_id": "footprint_inside_parcel"}]})
    second = tools.execute("evaluate_scenario", {"requested_constraints": [{"constraint_id": "footprint_inside_parcel"}]})

    assert first == second
    assert first["constraint_results"][0]["calculation"] == "sandbox_geometry.parcel_containment.v1"


def test_observed_geometry_cannot_be_modified_and_arbitrary_geojson_is_rejected():
    site, tools = executor()
    observed_before = copy.deepcopy(site["geometry"])
    tools.execute("transform_object", {"object_id": "data_center_1", "operation": "move", "delta_x_m": 1, "delta_y_m": 1, "width_m": None, "length_m": None, "height_m": None, "rotation_deg": None, "capacity_mw": None})
    assert site["geometry"] == observed_before

    with pytest.raises(ToolValidationError, match="Unexpected tool arguments"):
        tools.execute("propose_data_center", {"capacity_mw": 100, "width_m": None, "length_m": None, "height_m": None, "position": None, "rotation_deg": None, "minimum_setback_m": 10, "elements": None, "geometry": {"type": "Polygon"}})


def test_unsupported_constraint_is_unresolved_and_malformed_tool_call_fails_safely():
    _site, tools = executor()
    evaluation = tools.execute("evaluate_scenario", {"requested_constraints": [{"constraint_id": "max_slope_degrees", "max_degrees": 5}]})
    assert evaluation["overall_status"] == "UNRESOLVED"

    model = ScriptedModel([ModelReply(message="", tool_calls=[{"id": "bad", "name": "transform_object", "arguments": "{"}], response_items=[])])
    response = _run(SandboxAgent(model=model).chat(snapshot(), "bad-call", "Move it north."))
    assert response["tool_trace"][0]["status"] == "rejected"


def test_invalid_tool_inputs_and_sequences_are_deterministic_without_mireye_access():
    site_one, tools_one = executor()
    site_two, tools_two = executor()
    invalid = {"object_id": "data_center_1", "operation": "move", "delta_x_m": None, "delta_y_m": None, "width_m": None, "length_m": None, "height_m": None, "rotation_deg": None, "capacity_mw": None}
    with pytest.raises(ToolValidationError, match="move requires"):
        tools_one.execute("transform_object", invalid)

    move = {**invalid, "delta_x_m": 30, "delta_y_m": 40}
    tools_one.execute("transform_object", move)
    tools_two.execute("transform_object", move)
    assert serialize_scene_state(tools_one.session.scene_state) == serialize_scene_state(tools_two.session.scene_state)
    assert not hasattr(tools_one, "mireye_client")
    assert {definition["name"] for definition in TOOL_DEFINITIONS}.isdisjoint({"fetch", "fetch_quote", "lookup", "mireye_fetch"})
    assert site_one["geometry"] == site_two["geometry"]


def test_agent_exposes_phase6_evidence_constraints_without_fetch_tools():
    constraint_ids = next(definition for definition in TOOL_DEFINITIONS if definition["name"] == "evaluate_scenario")["parameters"]["properties"]["requested_constraints"]["items"]["properties"]["constraint_id"]["enum"]
    _site, tools = executor()
    context = tools.execute("get_site_context", {"snapshot_id": "site-agent-test"})

    assert {"max_nwi_wetland_fraction_of_parcel", "max_nwi_wetland_acres_on_parcel", "resolution_point_outside_fema_sfha", "max_resolution_point_slope_degrees", "max_resolution_point_substation_distance_m", "max_resolution_point_transmission_distance_m", "max_resolution_point_major_road_distance_m", "parcel_zoning_code_in"}.issubset(constraint_ids)
    assert "max_nwi_wetland_fraction_of_parcel" in context["available_constraints"]
    assert "sufficient_grid_capacity" in context["unresolved_constraints"]


def test_alternative_layout_branch_updates_session_and_remains_comparable():
    site = snapshot()
    base_scene = scene_state_from_snapshot(site)

    class Scenarios:
        def __init__(self):
            self.records = {
                "scenario-a": {"scenario_id": "scenario-a", "revision": 1, "site_snapshot_id": site["snapshot_id"], "workspace_id": "workspace-a", "scene_state": copy.deepcopy(base_scene), "evaluation": None, "requested_constraints": [], "parent_scenario_id": None},
            }
            self.compared = None

        def get(self, scenario_id):
            return copy.deepcopy(self.records[scenario_id])

        def branch(self, scenario_id, *, user_intent):
            result = {**copy.deepcopy(self.records[scenario_id]), "scenario_id": "scenario-b", "parent_scenario_id": scenario_id}
            self.records["scenario-b"] = result
            return copy.deepcopy(result)

        def record_accepted_tool(self, _snapshot, *, active_scenario_id, scene_state, **_kwargs):
            result = {**copy.deepcopy(self.records[active_scenario_id]), "revision": 2, "scene_state": copy.deepcopy(scene_state)}
            self.records[active_scenario_id] = result
            return copy.deepcopy(result)

        def compare(self, left_scenario_id, right_scenario_id):
            self.compared = (left_scenario_id, right_scenario_id)
            return {"left": left_scenario_id, "right": right_scenario_id}

    model = ScriptedModel([
        call("get_site_context", {"snapshot_id": site["snapshot_id"]}),
        call("branch_scenario", {"scenario_id": "scenario-a", "user_intent": "Try a second layout."}),
        call("transform_object", {"object_id": "data_center_1", "operation": "move", "delta_x_m": 10, "delta_y_m": 0, "width_m": None, "length_m": None, "height_m": None, "rotation_deg": None, "capacity_mw": None}),
        call("evaluate_scenario", {"requested_constraints": [{"constraint_id": "footprint_inside_parcel"}]}),
        ModelReply(message="Alternative created.", tool_calls=[], response_items=[]),
        call("get_site_context", {"snapshot_id": site["snapshot_id"]}),
        call("compare_scenarios", {"left_scenario_id": "scenario-a", "right_scenario_id": "scenario-b"}),
        ModelReply(message="Compared.", tool_calls=[], response_items=[]),
    ])
    scenarios = Scenarios()
    agent = SandboxAgent(model=model, sessions=InMemorySandboxSessions(), scenarios=scenarios)

    alternative = _run(agent.chat(site, "branch-session", "Try a second layout.", scenario_id="scenario-a"))
    compared = _run(agent.chat(site, "branch-session", "Compare the two layouts."))

    assert alternative["scenario"]["scenario_id"] == "scenario-b"
    assert scenarios.records["scenario-a"]["scene_state"] == base_scene
    assert scenarios.compared == ("scenario-a", "scenario-b")
    assert "available scenario_ids: scenario-a, scenario-b" in model.calls[5]["input"][0]["content"]
    assert [item["tool"] for item in alternative["tool_trace"]] == ["get_site_context", "branch_scenario", "transform_object", "evaluate_scenario"]
    assert [item["tool"] for item in compared["tool_trace"]] == ["get_site_context", "compare_scenarios"]


def test_alternative_claim_without_geometry_change_is_rejected_after_bounded_retries():
    model = ScriptedModel([
        ModelReply(message="I created an alternative layout.", tool_calls=[], response_items=[]),
        ModelReply(message="The new geometry is ready.", tool_calls=[], response_items=[]),
        ModelReply(message="The alternative was completed.", tool_calls=[], response_items=[]),
    ])
    agent = SandboxAgent(model=model, sessions=InMemorySandboxSessions())

    response = _run(agent.chat(snapshot(), "false-change", "Try another layout."))

    assert response["message"] == "No validated alternative layout was produced; the existing layout remains unchanged."
    assert response["scene_state"]["proposed"] == []
    assert len(model.calls) == 3
