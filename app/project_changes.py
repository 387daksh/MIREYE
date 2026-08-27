"""Deterministic project change records derived from immutable snapshots."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Literal, TypedDict


ChangeType = Literal[
    "VALUE_CHANGED", "FRESHNESS_CHANGED", "STATUS_CHANGED", "SCOPE_CHANGED",
    "GEOMETRY_CHANGED", "IDENTITY_CHANGED", "SOURCE_CHANGED", "EVIDENCE_REMOVED", "EVIDENCE_ADDED",
]
Significance = Literal["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]


class ProjectChange(TypedDict):
    change_id: str
    project_id: str
    site_id: str
    snapshot_before: str | None
    snapshot_after: str | None
    world_snapshot_before: str | None
    world_snapshot_after: str | None
    evidence_id: str | None
    field: str
    old_value: Any
    new_value: Any
    semantic_change_type: ChangeType
    source: str | None
    source_timestamp: float | None
    detected_at: float
    scope: str | None
    significance: Significance
    affected_requirements: list[str]
    affected_constraints: list[str]
    affected_scenarios: list[dict]
    affected_actions: list[str]
    affected_readiness: list[dict]
    status: str
    what_changed: str
    why_it_matters: str
    what_is_affected: list[str]
    what_happens_next: str


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _stable_id(*parts: Any) -> str:
    return "change_" + hashlib.sha256(_canonical(parts).encode("utf-8")).hexdigest()[:32]


def _dependencies(intelligence: dict | None, evidence_id: str | None) -> set[str]:
    if not evidence_id:
        return set()
    return {
        item["requirement_id"] for item in (intelligence or {}).get("evidence_dependencies", [])
        if item.get("evidence_id") == evidence_id
    }


def _coverage(intelligence: dict | None) -> dict[str, dict]:
    return {item["requirement_id"]: item for item in (intelligence or {}).get("evidence_coverage", [])}


def _readiness_changes(before: dict | None, after: dict | None, requirements: set[str]) -> list[dict]:
    before_state, after_state = (before or {}).get("readiness", {}), (after or {}).get("readiness", {})
    changes = []
    for domain in sorted(set(before_state) | set(after_state)):
        old, new = before_state.get(domain, {}).get("status"), after_state.get(domain, {}).get("status")
        domain_requirements = set(before_state.get(domain, {}).get("requirements", [])) | set(after_state.get(domain, {}).get("requirements", []))
        if old != new and (not requirements or requirements.intersection(domain_requirements)):
            changes.append({"domain": domain, "before": old, "after": new})
    return changes


def _significance(change_type: ChangeType, requirements: set[str], scenarios: list[dict], before: dict | None, after: dict | None) -> Significance:
    if change_type in {"IDENTITY_CHANGED", "GEOMETRY_CHANGED"}:
        return "CRITICAL"
    coverage = {**_coverage(before), **_coverage(after)}
    impacts = {coverage[item].get("impact") for item in requirements if item in coverage}
    if "CRITICAL" in impacts:
        return "CRITICAL"
    if "HIGH" in impacts or scenarios:
        return "HIGH"
    if "MEDIUM" in impacts or requirements:
        return "MEDIUM"
    return "LOW" if change_type in {"FRESHNESS_CHANGED", "STATUS_CHANGED", "SCOPE_CHANGED", "SOURCE_CHANGED"} else "INFO"


def _actions(intelligence: dict | None, requirements: set[str]) -> set[str]:
    return {
        item["action_id"] for item in (intelligence or {}).get("recommended_actions", [])
        if item.get("requirement_id") in requirements
    }


def _record(
    *, project_id: str, site_id: str, before_snapshot: dict, after_snapshot: dict,
    field: str, evidence_id: str | None, change_type: ChangeType, old_value: Any, new_value: Any,
    before_intelligence: dict | None, after_intelligence: dict | None, evaluation_runs: list[dict],
    scenario_dependencies: dict[str, list[dict]] | None,
    world_before: dict | None, world_after: dict | None, detected_at: float,
) -> ProjectChange:
    requirements = _dependencies(before_intelligence, evidence_id) | _dependencies(after_intelligence, evidence_id)
    state_aliases = {"NEEDS_GEOMETRY_REBASE": "REQUIRES_REBASE", "INVALIDATED_BY_REFRESH": "INVALIDATED", "UNRESOLVED": "STALE"}
    dependent_scenarios = {
        (item["scenario_id"], item["revision"])
        for item in (scenario_dependencies or {}).get(evidence_id or "", [])
    }
    scenarios = [
        {"scenario_id": run["scenario_id"], "revision": run["revision"], "state": state_aliases.get(run.get("status"), run.get("status"))}
        for run in evaluation_runs
        if (run["scenario_id"], run["revision"]) in dependent_scenarios or change_type in {"GEOMETRY_CHANGED", "IDENTITY_CHANGED"}
    ]
    readiness = _readiness_changes(before_intelligence, after_intelligence, requirements)
    actions = _actions(before_intelligence, requirements) | _actions(after_intelligence, requirements)
    after_record = after_snapshot.get("evidence", {}).get(evidence_id, {}) if evidence_id else {}
    significance = _significance(change_type, requirements, scenarios, before_intelligence, after_intelligence)
    affected = sorted({*requirements, *(item["domain"] for item in readiness), *(item["scenario_id"] for item in scenarios)})
    next_step = (
        "Stop and confirm the parcel identity before continuing." if change_type == "IDENTITY_CHANGED" else
        "Review and explicitly rebase affected scenario geometry." if change_type == "GEOMETRY_CHANGED" else
        "Review the recalculated readiness and next action." if requirements else
        "Review the recalculated scenario state." if scenarios else
        "No decision action is required unless this field becomes a project dependency."
    )
    return {
        "change_id": _stable_id(project_id, site_id, before_snapshot.get("snapshot_id"), after_snapshot.get("snapshot_id"), field, change_type),
        "project_id": project_id, "site_id": site_id,
        "snapshot_before": before_snapshot.get("snapshot_id"), "snapshot_after": after_snapshot.get("snapshot_id"),
        "world_snapshot_before": (world_before or {}).get("world_snapshot_id"), "world_snapshot_after": (world_after or {}).get("world_snapshot_id"),
        "evidence_id": evidence_id, "field": field, "old_value": old_value, "new_value": new_value,
        "semantic_change_type": change_type, "source": after_record.get("source") or after_record.get("provider"),
        "source_timestamp": after_record.get("observed_at"), "detected_at": detected_at,
        "scope": after_record.get("scope"), "significance": significance,
        "affected_requirements": sorted(requirements), "affected_constraints": sorted(requirements),
        "affected_scenarios": scenarios, "affected_actions": sorted(actions), "affected_readiness": readiness,
        "status": "CURRENT", "what_changed": f"{field.replace('_', ' ').title()}: {change_type.replace('_', ' ').lower()}.",
        "why_it_matters": (
            "This change affects an active project requirement." if requirements else
            "This change affects evidence used by an existing scenario." if scenarios else
            "This source change is not used by an active project requirement."
        ),
        "what_is_affected": affected, "what_happens_next": next_step,
    }


def changes_from_refresh(
    *, project_id: str, site_id: str, before_snapshot: dict, after_snapshot: dict, snapshot_diff: dict,
    before_intelligence: dict | None, after_intelligence: dict | None, evaluation_runs: list[dict] | None = None,
    scenario_dependencies: dict[str, list[dict]] | None = None,
    world_before: dict | None = None, world_after: dict | None = None, detected_at: float | None = None,
) -> list[ProjectChange]:
    """Convert the existing authoritative snapshot diff into durable project changes."""
    now = time.time() if detected_at is None else float(detected_at)
    runs = evaluation_runs or []
    changes: list[ProjectChange] = []
    if snapshot_diff.get("identity_changed"):
        changes.append(_record(
            project_id=project_id, site_id=site_id, before_snapshot=before_snapshot, after_snapshot=after_snapshot,
            field="parcel_identity", evidence_id=None, change_type="IDENTITY_CHANGED",
            old_value={key: value.get("before") for key, value in snapshot_diff.get("identity_changes", {}).items()},
            new_value={key: value.get("after") for key, value in snapshot_diff.get("identity_changes", {}).items()},
            before_intelligence=before_intelligence, after_intelligence=after_intelligence, evaluation_runs=runs,
            scenario_dependencies=scenario_dependencies,
            world_before=world_before, world_after=world_after, detected_at=now,
        ))
    if snapshot_diff.get("geometry_changed"):
        changes.append(_record(
            project_id=project_id, site_id=site_id, before_snapshot=before_snapshot, after_snapshot=after_snapshot,
            field="parcel_geometry", evidence_id=None, change_type="GEOMETRY_CHANGED",
            old_value=snapshot_diff.get("geometry_hash", {}).get("before"), new_value=snapshot_diff.get("geometry_hash", {}).get("after"),
            before_intelligence=before_intelligence, after_intelligence=after_intelligence, evaluation_runs=runs,
            scenario_dependencies=scenario_dependencies,
            world_before=world_before, world_after=world_after, detected_at=now,
        ))
    dimensions: tuple[tuple[str, ChangeType], ...] = (
        ("value", "VALUE_CHANGED"), ("status", "STATUS_CHANGED"), ("scope", "SCOPE_CHANGED"),
        ("freshness", "FRESHNESS_CHANGED"), ("source", "SOURCE_CHANGED"),
    )
    for field, diff in sorted(snapshot_diff.get("field_changes", {}).items()):
        existence = diff.get("existence", {})
        if existence.get("before") is False and existence.get("after") is True:
            selected: list[tuple[str, ChangeType]] = [("existence", "EVIDENCE_ADDED")]
        elif existence.get("before") is True and existence.get("after") is False:
            selected = [("existence", "EVIDENCE_REMOVED")]
        else:
            selected = [(key, kind) for key, kind in dimensions if diff.get(key, {}).get("before") != diff.get(key, {}).get("after")]
        for key, kind in selected:
            values = diff.get(key, {})
            changes.append(_record(
                project_id=project_id, site_id=site_id, before_snapshot=before_snapshot, after_snapshot=after_snapshot,
                field=field, evidence_id=field, change_type=kind, old_value=values.get("before"), new_value=values.get("after"),
                before_intelligence=before_intelligence, after_intelligence=after_intelligence, evaluation_runs=runs,
                scenario_dependencies=scenario_dependencies,
                world_before=world_before, world_after=world_after, detected_at=now,
            ))
    return changes


def world_snapshot_diff(before: dict, after: dict) -> list[dict]:
    """Diff immutable world layers by availability, source, and content-addressed artifacts."""
    old = {item["layer"]: item for item in before.get("layers", [])}
    new = {item["layer"]: item for item in after.get("layers", [])}
    result = []
    for layer in sorted(set(old) | set(new)):
        left, right = old.get(layer), new.get(layer)
        left_state = None if left is None else {"availability": left.get("availability"), "source": left.get("source"), "artifacts": {key: value.get("sha256") for key, value in left.get("artifacts", {}).items()}}
        right_state = None if right is None else {"availability": right.get("availability"), "source": right.get("source"), "artifacts": {key: value.get("sha256") for key, value in right.get("artifacts", {}).items()}}
        if left_state != right_state:
            result.append({"layer": layer, "before": left_state, "after": right_state})
    return result


def changes_from_world_refresh(
    *, project_id: str, site_id: str, site_snapshot: dict, before_world: dict, after_world: dict,
    intelligence: dict | None = None, detected_at: float | None = None,
) -> list[ProjectChange]:
    """Record source-backed world-layer replacements without inventing decision dependencies."""
    now = time.time() if detected_at is None else float(detected_at)
    changes = []
    for diff in world_snapshot_diff(before_world, after_world):
        record = _record(
            project_id=project_id, site_id=site_id, before_snapshot=site_snapshot, after_snapshot=site_snapshot,
            field=f"world_layer:{diff['layer']}", evidence_id=f"world:{diff['layer']}", change_type="SOURCE_CHANGED",
            old_value=diff["before"], new_value=diff["after"], before_intelligence=intelligence,
            after_intelligence=intelligence, evaluation_runs=[], scenario_dependencies=None,
            world_before=before_world, world_after=after_world,
            detected_at=now,
        )
        source = (next((item.get("source") for item in after_world.get("layers", []) if item.get("layer") == diff["layer"]), None) or {})
        record.update(
            source=source.get("provider"), source_timestamp=after_world.get("created_at"), significance="INFO",
            why_it_matters="This observed world layer changed, but no active deterministic constraint depends on it.",
            what_happens_next="Review the layer provenance; no scenario state changes automatically.",
        )
        changes.append(record)
    return changes
