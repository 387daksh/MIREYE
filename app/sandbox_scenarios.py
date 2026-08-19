"""Durable, complete-state Site Sandbox scenario revisions and comparison."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from app.sandbox_evaluator import SceneValidationError, evaluate_site
from app.sandbox_proposal import PLACEMENT_STRATEGY
from app.workspace.store import WorkspaceStore


TOOL_SCHEMA_VERSION = "sandbox_tools_v1"
DEFAULT_SCENARIO_CONSTRAINTS = [
    {"constraint_id": "footprint_inside_parcel"},
    {"constraint_id": "minimum_setback", "minimum_m": 10.0},
    {"constraint_id": "footprint_area"},
    {"constraint_id": "parcel_coverage"},
    {"constraint_id": "object_collision"},
]


class ScenarioError(ValueError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _empty_evaluation(snapshot_id: str) -> dict:
    return {
        "evaluator_version": "site_sandbox_geometry_v1",
        "site_snapshot_id": snapshot_id,
        "overall_status": "UNRESOLVED",
        "constraint_results": [{
            "constraint_id": "proposed_object_present", "basis": "DERIVED", "outcome": "UNRESOLVED",
            "evidence_ids": [], "calculation": "sandbox_scenario.empty_scene.v1", "inputs": {},
            "result": None, "units": None, "explanation": "No proposed object is present to evaluate.",
        }],
        "derived_geometry_metrics": {},
    }


class ScenarioService:
    def __init__(self, store: WorkspaceStore):
        self.store = store

    def evaluate(self, snapshot: dict, scene_state: dict, requested_constraints: list[dict] | None) -> tuple[list[dict], dict]:
        constraints = requested_constraints or list(DEFAULT_SCENARIO_CONSTRAINTS)
        if not scene_state.get("proposed"):
            return constraints, _empty_evaluation(snapshot["snapshot_id"])
        try:
            return constraints, evaluate_site(snapshot, scene_state, constraints)
        except SceneValidationError as exc:
            raise ScenarioError(str(exc)) from exc

    def create(
        self,
        snapshot: dict,
        *,
        workspace_id: str,
        user_intent: str,
        scene_state: dict,
        requested_constraints: list[dict] | None = None,
        model_id: str | None = None,
        accepted_tool_calls: list[dict] | None = None,
        scenario_id: str | None = None,
        parent_scenario_id: str | None = None,
    ) -> dict:
        if snapshot.get("workspace_id") != workspace_id:
            raise ScenarioError("Scenario workspace_id must match the immutable SiteSnapshot workspace_id.")
        if self.store.get_site_snapshot(snapshot["snapshot_id"]) is None:
            raise ScenarioError("SiteSnapshot must be persisted before creating a scenario.")
        constraints, evaluation = self.evaluate(snapshot, scene_state, requested_constraints)
        return self._write(
            scenario_id=scenario_id or f"scn_{uuid.uuid4().hex}", workspace_id=workspace_id,
            revision=1, parent_scenario_id=parent_scenario_id, snapshot=snapshot, user_intent=user_intent,
            scene_state=scene_state, requested_constraints=constraints, evaluation=evaluation,
            model_id=model_id, accepted_tool_calls=accepted_tool_calls or [],
        )

    def append(
        self,
        snapshot: dict,
        *,
        scenario_id: str,
        user_intent: str,
        scene_state: dict,
        requested_constraints: list[dict] | None = None,
        model_id: str | None = None,
        accepted_tool_calls: list[dict] | None = None,
        evaluation: dict | None = None,
    ) -> dict:
        head = self.get(scenario_id)
        if head["site_snapshot_id"] != snapshot["snapshot_id"]:
            raise ScenarioError("Scenario does not reference this SiteSnapshot.")
        constraints, calculated = self.evaluate(snapshot, scene_state, requested_constraints or head["requested_constraints"])
        return self._write(
            scenario_id=scenario_id, workspace_id=head["workspace_id"], revision=head["revision"] + 1,
            parent_scenario_id=head["parent_scenario_id"], snapshot=snapshot, user_intent=user_intent,
            scene_state=scene_state, requested_constraints=constraints, evaluation=evaluation or calculated,
            model_id=model_id or head.get("model_id"), accepted_tool_calls=accepted_tool_calls or [],
        )

    def branch(self, scenario_id: str, *, user_intent: str, model_id: str | None = None) -> dict:
        source = self.get(scenario_id)
        snapshot = self.store.get_site_snapshot(source["site_snapshot_id"])
        if snapshot is None:
            raise ScenarioError("Referenced SiteSnapshot is unavailable.")
        snapshot["is_expired"] = snapshot["expires_at"] <= time.time()
        return self.create(
            snapshot, workspace_id=source["workspace_id"], user_intent=user_intent,
            scene_state=source["scene_state"], requested_constraints=source["requested_constraints"],
            model_id=model_id or source.get("model_id"), accepted_tool_calls=[],
            parent_scenario_id=scenario_id,
        )

    def get(self, scenario_id: str, revision: int | None = None) -> dict:
        scenario = self.store.get_scenario_version(scenario_id, revision)
        if scenario is None:
            raise ScenarioError("Scenario was not found.")
        return scenario

    def list_revisions(self, scenario_id: str) -> list[dict]:
        revisions = self.store.list_scenario_versions(scenario_id)
        if not revisions:
            raise ScenarioError("Scenario was not found.")
        return revisions

    def record_accepted_tool(
        self,
        snapshot: dict,
        *,
        active_scenario_id: str | None,
        workspace_id: str,
        user_intent: str,
        scene_state: dict,
        requested_constraints: list[dict] | None,
        tool_name: str,
        arguments: dict,
        model_id: str | None,
        evaluation: dict | None = None,
    ) -> dict:
        call = [{"name": tool_name, "arguments": arguments}]
        if active_scenario_id is None:
            return self.create(
                snapshot, workspace_id=workspace_id, user_intent=user_intent, scene_state=scene_state,
                requested_constraints=requested_constraints, model_id=model_id, accepted_tool_calls=call,
            )
        return self.append(
            snapshot, scenario_id=active_scenario_id, user_intent=user_intent, scene_state=scene_state,
            requested_constraints=requested_constraints, model_id=model_id, accepted_tool_calls=call,
            evaluation=evaluation,
        )

    def compare(self, left_scenario_id: str, right_scenario_id: str, *, left_revision: int | None = None, right_revision: int | None = None) -> dict:
        left, right = self.get(left_scenario_id, left_revision), self.get(right_scenario_id, right_revision)
        if left["site_snapshot_id"] != right["site_snapshot_id"]:
            raise ScenarioError("Scenarios must reference the same SiteSnapshot to compare them.")
        object_changes = self._object_changes(left["scene_state"], right["scene_state"])
        metric_changes = self._changes(left["evaluation"].get("derived_geometry_metrics", {}), right["evaluation"].get("derived_geometry_metrics", {}))
        constraint_changes = self._constraint_changes(left["evaluation"], right["evaluation"])
        unresolved = {
            "left": sorted(item["constraint_id"] for item in left["evaluation"].get("constraint_results", []) if item["outcome"] == "UNRESOLVED"),
            "right": sorted(item["constraint_id"] for item in right["evaluation"].get("constraint_results", []) if item["outcome"] == "UNRESOLVED"),
        }
        versions = {
            "left": self._versions(left["evaluation"]), "right": self._versions(right["evaluation"]),
        }
        dominance = self._dominance(left["evaluation"], right["evaluation"])
        summary = []
        if object_changes:
            summary.append("Proposed object geometry or capacity changed.")
        if metric_changes:
            summary.append("Derived geometry metrics changed.")
        if constraint_changes:
            summary.append("Constraint outcomes, evidence, or calculations changed.")
        if not summary:
            summary.append("No deterministic scenario differences were found.")
        return {
            "comparison_version": "sandbox_scenario_compare_v1",
            "left": {"scenario_id": left["scenario_id"], "revision": left["revision"], "state_hash": left["state_hash"]},
            "right": {"scenario_id": right["scenario_id"], "revision": right["revision"], "state_hash": right["state_hash"]},
            "what_changed": summary, "object_changes": object_changes, "metric_changes": metric_changes,
            "constraint_changes": constraint_changes, "unresolved_constraints": unresolved,
            "evaluation_versions": versions, "dominance": dominance,
        }

    def _write(self, **values: Any) -> dict:
        record = {
            **values,
            "geometry_engine_version": values["evaluation"].get("evaluator_version", "site_sandbox_geometry_v1"),
            "proposal_strategy_version": PLACEMENT_STRATEGY,
            "tool_schema_version": TOOL_SCHEMA_VERSION,
            "created_at": time.time(),
        }
        record["state_hash"] = hashlib.sha256(_canonical({
            "site_snapshot_id": record["snapshot"]["snapshot_id"], "scene_state": record["scene_state"],
            "requested_constraints": record["requested_constraints"], "evaluation": record["evaluation"],
            "geometry_engine_version": record["geometry_engine_version"],
            "proposal_strategy_version": record["proposal_strategy_version"], "model_id": record["model_id"],
            "tool_schema_version": record["tool_schema_version"], "accepted_tool_calls": record["accepted_tool_calls"],
        }).encode("utf-8")).hexdigest()
        record["site_snapshot_id"] = record.pop("snapshot")["snapshot_id"]
        self.store.create_scenario_version(record)
        return record

    @staticmethod
    def _objects(scene_state: dict) -> dict:
        return {item["id"]: item for item in scene_state.get("proposed", []) if item.get("id")}

    def _object_changes(self, left_scene: dict, right_scene: dict) -> dict:
        left, right = self._objects(left_scene), self._objects(right_scene)
        changes = {}
        for object_id in sorted(set(left) | set(right)):
            if object_id not in left:
                changes[object_id] = {"change": "added", "right": right[object_id]}
            elif object_id not in right:
                changes[object_id] = {"change": "removed", "left": left[object_id]}
            else:
                fields = self._changes(left[object_id], right[object_id])
                if fields:
                    changes[object_id] = {"change": "modified", "fields": fields}
        return changes

    @staticmethod
    def _changes(left: Any, right: Any) -> dict:
        if not isinstance(left, dict) or not isinstance(right, dict):
            return {} if left == right else {"before": left, "after": right}
        result = {}
        for key in sorted(set(left) | set(right)):
            if left.get(key) != right.get(key):
                result[key] = {"before": left.get(key), "after": right.get(key)}
        return result

    @staticmethod
    def _constraint_changes(left: dict, right: dict) -> dict:
        def index(evaluation: dict) -> dict:
            return {item["constraint_id"]: item for item in evaluation.get("constraint_results", [])}
        left_items, right_items = index(left), index(right)
        changes = {}
        for constraint_id in sorted(set(left_items) | set(right_items)):
            if left_items.get(constraint_id) != right_items.get(constraint_id):
                changes[constraint_id] = {"before": left_items.get(constraint_id), "after": right_items.get(constraint_id)}
        return changes

    @staticmethod
    def _versions(evaluation: dict) -> dict:
        return {
            "evaluator_version": evaluation.get("evaluator_version"),
            "calculations": sorted({item.get("calculation") for item in evaluation.get("constraint_results", []) if item.get("calculation")}),
        }

    @staticmethod
    def _dominance(left: dict, right: dict) -> dict:
        order = {"FAIL": 0, "UNRESOLVED": 1, "PASS": 2}
        left_items = {item["constraint_id"]: item["outcome"] for item in left.get("constraint_results", [])}
        right_items = {item["constraint_id"]: item["outcome"] for item in right.get("constraint_results", [])}
        if set(left_items) != set(right_items):
            return {"result": "neither", "basis": "constraint sets differ"}
        left_better = all(order[left_items[key]] >= order[right_items[key]] for key in left_items) and any(order[left_items[key]] > order[right_items[key]] for key in left_items)
        right_better = all(order[right_items[key]] >= order[left_items[key]] for key in left_items) and any(order[right_items[key]] > order[left_items[key]] for key in left_items)
        return {"result": "left" if left_better else "right" if right_better else "neither", "basis": "PASS > UNRESOLVED > FAIL per matching deterministic constraints"}
