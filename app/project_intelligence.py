"""Deterministic project evidence coverage, gap, action, and readiness state."""
from __future__ import annotations

import copy
import hashlib
import json
import time
from typing import Any, Callable, Literal, TypedDict


GapStatus = Literal["OPEN", "IN_PROGRESS", "RESOLVED", "WAIVED", "STALE"]


class ProjectIntelligence(TypedDict):
    schema_version: str
    project_id: str
    project_requirements: list[dict]
    candidate_sites: list[dict]
    active_site: dict | None
    canonical_constraints: list[dict]
    evidence_items: list[dict]
    evidence_semantics: dict
    evidence_dependencies: list[dict]
    decision_state: dict
    evidence_coverage: list[dict]
    unresolved_issues: list[dict]
    evidence_gaps: list[dict]
    recommended_actions: list[dict]
    assumptions: list[dict]
    readiness: dict
    risk_state: dict
    last_evaluated_at: float
    project_readiness_state: str
    change_impact: dict | None
    state_hash: str


_SEMANTIC_PROOF_GAPS = {
    "bess_export_interconnection": ["utility_or_iso_confirmed_export_injection_capacity_mw", "approved_bess_interconnection_path"],
    "substation_available_capacity_mw": ["utility_confirmed_available_capacity_mw"],
    "transmission_available_capacity_mw": ["utility_confirmed_available_capacity_mw"],
    "industrial_zoning": ["jurisdiction_aware_zoning_determination"],
    "energy_storage_entitlement": ["jurisdiction_aware_energy_storage_permitted_use_determination"],
    "legal_access": ["recorded_legal_access_or_access_opinion"],
    "parcel_outside_fema_sfha": ["parcel_wide_flood_geometry_or_study"],
    "footprint_outside_fema_sfha": ["footprint_level_flood_geometry_or_study"],
    "max_slope_degrees": ["parcel_or_footprint_slope_surface"],
    "water_capacity": ["provider_confirmed_water_capacity"],
    "fiber_diversity": ["carrier_confirmed_physically_diverse_routes"],
    "utility_capacity": ["provider_confirmed_utility_capacity"],
}

_POLICIES = {
    "bess_export_interconnection": {
        "title": "100 MW export / injection interconnection", "domain": "Power", "impact": "CRITICAL", "blocking": True,
        "action_type": "BESS_EXPORT_INTERCONNECTION_RFI", "recipient_category": "Serving utility or ISO/RTO storage interconnection team",
        "responsible_party": "Storage interconnection team", "resolution": ["Obtain written export/injection capability and point-of-interconnection pathway confirmation."],
        "disqualification_likelihood": "HIGH", "critical_milestone": True, "dependency_centrality": 1,
    },
    "industrial_zoning": {
        "title": "Jurisdiction-aware zoning determination", "domain": "Entitlement", "impact": "CRITICAL", "blocking": True,
        "action_type": "ZONING_ENTITLEMENT_RFI", "recipient_category": "Local planning or zoning authority",
        "responsible_party": "Entitlement counsel or planning authority", "resolution": ["Obtain a jurisdiction-aware written zoning or entitlement determination."],
        "disqualification_likelihood": "HIGH", "critical_milestone": True,
    },
    "energy_storage_entitlement": {
        "title": "Permitted energy-storage use", "domain": "Entitlement", "impact": "CRITICAL", "blocking": True,
        "action_type": "ENERGY_STORAGE_ENTITLEMENT_RFI", "recipient_category": "Controlling planning or zoning authority",
        "responsible_party": "Entitlement counsel or controlling authority", "resolution": ["Obtain a jurisdiction-aware written permitted-use and approval-path determination."],
        "disqualification_likelihood": "HIGH", "critical_milestone": True,
    },
    "legal_access": {
        "title": "Legal site access", "domain": "Access", "impact": "HIGH", "blocking": True,
        "action_type": "LEGAL_ACCESS_RFI", "recipient_category": "Title company or real-estate counsel",
        "responsible_party": "Title company or legal counsel", "resolution": ["Confirm recorded access, easements, and relevant title exceptions."],
        "disqualification_likelihood": "HIGH", "critical_milestone": True,
    },
    "water_capacity": {
        "title": "Water capacity", "domain": "Water", "impact": "HIGH", "blocking": True,
        "action_type": "WATER_CAPACITY_RFI", "recipient_category": "Water service provider",
        "responsible_party": "Water provider", "resolution": ["Obtain a capacity and service confirmation for the project demand."],
        "disqualification_likelihood": "MEDIUM", "critical_milestone": True,
    },
    "fiber_diversity": {
        "title": "Fiber route diversity", "domain": "Connectivity", "impact": "HIGH", "blocking": False,
        "action_type": "FIBER_DIVERSITY_RFI", "recipient_category": "Telecommunications carrier",
        "responsible_party": "Carrier or network consultant", "resolution": ["Obtain route-level diversity and service confirmation."],
        "disqualification_likelihood": "MEDIUM", "critical_milestone": False,
    },
    "parcel_outside_fema_sfha": {"title": "Parcel-wide flood exclusion", "domain": "Flood", "impact": "HIGH", "blocking": True},
    "footprint_outside_fema_sfha": {"title": "Footprint flood exclusion", "domain": "Flood", "impact": "HIGH", "blocking": True},
    "resolution_point_outside_fema_sfha": {"title": "Resolution-point flood status", "domain": "Flood", "impact": "HIGH", "blocking": True},
    "max_nwi_wetland_fraction_of_parcel": {"title": "Mapped wetland fraction", "domain": "Environmental", "impact": "HIGH", "blocking": True},
    "max_nwi_wetland_acres_on_parcel": {"title": "Mapped wetland acreage", "domain": "Environmental", "impact": "HIGH", "blocking": True},
    "parcel_acreage_range": {"title": "Parcel acreage", "domain": "Land", "impact": "HIGH", "blocking": True},
    "max_resolution_point_slope_degrees": {"title": "Resolution-point slope", "domain": "Terrain", "impact": "MEDIUM", "blocking": False},
    "max_slope_degrees": {"title": "Parcel or footprint slope", "domain": "Terrain", "impact": "HIGH", "blocking": True},
    "max_resolution_point_transmission_distance_m": {"title": "Transmission proximity", "domain": "Power", "impact": "HIGH", "blocking": False},
    "max_resolution_point_substation_distance_m": {"title": "Substation proximity", "domain": "Power", "impact": "HIGH", "blocking": False},
    "max_resolution_point_major_road_distance_m": {"title": "Major-road proximity", "domain": "Access", "impact": "MEDIUM", "blocking": False},
    "parcel_zoning_code_in": {"title": "Raw zoning code", "domain": "Entitlement", "impact": "HIGH", "blocking": True},
}

_IMPACT_SCORE = {"LOW": 10, "MEDIUM": 40, "HIGH": 70, "CRITICAL": 100}
_DISQUALIFICATION_SCORE = {"UNKNOWN": 0, "LOW": 5, "MEDIUM": 15, "HIGH": 25}
_SCOPE_ALIASES = {"POINT_TO_NEAREST_FEATURE": "NEAREST_FEATURE"}
RFI_ACTION_TYPES = {
    "BESS_EXPORT_INTERCONNECTION_RFI", "ENERGY_STORAGE_ENTITLEMENT_RFI", "ZONING_ENTITLEMENT_RFI", "WATER_CAPACITY_RFI",
    "FIBER_DIVERSITY_RFI", "LEGAL_ACCESS_RFI",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _stable_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha256(_canonical(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _title(constraint_id: str) -> str:
    return constraint_id.replace("_", " ").strip().title()


def _policy(constraint_id: str) -> dict:
    return {
        "title": _title(constraint_id), "domain": "Other", "impact": "MEDIUM", "blocking": False,
        "action_type": "EVIDENCE_REVIEW", "recipient_category": "Project diligence team",
        "responsible_party": "Project diligence team", "resolution": ["Obtain authoritative evidence that matches the requested semantic and scope."],
        "disqualification_likelihood": "UNKNOWN", "critical_milestone": False, "dependency_centrality": 0,
        **_POLICIES.get(constraint_id, {}),
    }


def _active_candidate(project: dict) -> dict | None:
    candidate_id = project.get("active_candidate_id") or (project.get("decision") or {}).get("winner_candidate_id")
    if candidate_id:
        selected = next((item for item in project.get("candidates", []) if item.get("candidate_id") == candidate_id), None)
        if selected and selected.get("snapshot_id"):
            return selected
    return next((item for item in project.get("candidates", []) if item.get("snapshot_id") and item.get("reconciliation_status") == "ENRICHED"), None)


def _evaluation_result(candidate: dict | None, constraint_id: str) -> dict | None:
    results = ((candidate or {}).get("evaluation") or {}).get("constraint_results", [])
    return next((item for item in results if item.get("constraint_id") == constraint_id), None)


def _coverage_item(
    constraint: dict,
    candidate: dict | None,
    snapshot: dict | None,
    capabilities: dict[str, dict],
    constraint_fields: dict[str, tuple[str, ...]],
    now: float,
) -> dict:
    constraint_id = constraint["constraint_id"]
    capability = capabilities.get(constraint_id, {})
    expected_fields = list(dict.fromkeys(capability.get("evidence_fields") or constraint_fields.get(constraint_id, ())))
    expected_scope = _SCOPE_ALIASES.get(capability.get("spatial_scope"), capability.get("spatial_scope"))
    evidence = (snapshot or {}).get("evidence", {})
    available, current, incompatible, missing, stale = [], [], [], [], []
    for field in expected_fields:
        record = evidence.get(field)
        if not isinstance(record, dict) or record.get("value") is None or record.get("status") != "ok":
            missing.append(field)
            continue
        available.append(field)
        actual_scope = str(record.get("scope") or "").upper().replace("-", "_") or None
        if expected_scope not in {None, "SITE", "MIXED_CONTEXT"} and actual_scope and actual_scope != expected_scope:
            incompatible.append(field)
            continue
        try:
            fresh = float(record.get("expires_at")) > now
        except (TypeError, ValueError):
            fresh = False
        if fresh:
            current.append(field)
        else:
            missing.append(field)
            stale.append(field)
    semantic_missing = list(_SEMANTIC_PROOF_GAPS.get(constraint_id, ()))
    result = _evaluation_result(candidate, constraint_id)
    recorded_outcome = result.get("outcome") if result else "UNRESOLVED"
    proof_fields = list(result.get("evidence_ids", [])) if result else []
    proof_current = bool(proof_fields)
    for field in proof_fields:
        record = evidence.get(field)
        try:
            proof_current = proof_current and isinstance(record, dict) and record.get("status") == "ok" and record.get("value") is not None and float(record.get("expires_at")) > now
        except (TypeError, ValueError):
            proof_current = False
    decision_provable = recorded_outcome in {"PASS", "FAIL"} and proof_current
    outcome = recorded_outcome if decision_provable else "UNRESOLVED"
    unsupported = list(capability.get("unsupported_semantics") or ())
    if capability.get("evaluator_support") == "UNRESOLVED_ONLY" and not unsupported:
        unsupported = ["The deterministic evaluator cannot represent the requested semantic."]
    missing_evidence = list(dict.fromkeys(missing + semantic_missing))
    coverage = "COMPLETE" if decision_provable else "PARTIAL" if available else "NONE"
    policy = _policy(constraint_id)
    semantic_strength = (
        "DERIVED" if decision_provable else
        "UNSUPPORTED_SEMANTICS" if unsupported or semantic_missing else
        "SOURCE_BACKED_SIGNAL" if available else
        "INSUFFICIENT_EVIDENCE"
    )
    return {
        "requirement_id": constraint_id, "title": policy["title"], "domain": policy["domain"],
        "required": True, "status": outcome, "coverage": coverage,
        "evidence_available": bool(available), "evidence_current": bool(expected_fields) and set(current) == set(expected_fields),
        "evidence_scope_compatible": not incompatible and bool(available), "decision_provable": decision_provable,
        "evidence_ids": proof_fields if result else available,
        "available_evidence": available, "missing_evidence": missing_evidence,
        "incompatible_evidence": incompatible, "stale_evidence": stale, "unsupported_semantics": unsupported,
        "semantic_strength": semantic_strength,
        "evidence_scope": capability.get("spatial_scope"),
        "outcome_explanation": result.get("explanation") if result else "No deterministic evaluation is available for this requirement.",
        "blocking": bool(policy["blocking"]), "impact": policy["impact"],
        "site_id": (candidate or {}).get("site_id"), "snapshot_id": (snapshot or {}).get("snapshot_id"),
    }


def _affected_scenarios(
    site_id: str | None,
    evidence_ids: list[str],
    dependency_lookup: Callable[[str, list[str]], list[dict]] | None,
) -> list[dict]:
    if not site_id or not evidence_ids or dependency_lookup is None:
        return []
    rows = dependency_lookup(site_id, evidence_ids)
    return [
        {"scenario_id": row["scenario_id"], "revision": row["revision"], "constraint_id": row["constraint_id"]}
        for row in rows
    ]


def _gap_from_coverage(
    project: dict,
    coverage: dict,
    now: float,
    previous: dict | None,
    dependency_lookup: Callable[[str, list[str]], list[dict]] | None,
) -> dict:
    requirement_id = coverage["requirement_id"]
    policy = _policy(requirement_id)
    gap_id = _stable_id("gap", project["project_id"], coverage.get("site_id"), requirement_id)
    previous_gap = next((item for item in (previous or {}).get("evidence_gaps", []) if item.get("gap_id") == gap_id), None)
    current_ids = list(dict.fromkeys(coverage["available_evidence"] + coverage["evidence_ids"]))
    return {
        "gap_id": gap_id, "project_id": project["project_id"], "site_id": coverage.get("site_id"),
        "requirement_id": requirement_id, "title": coverage.get("title") or policy["title"],
        "domain": coverage.get("domain") or policy["domain"],
        "description": coverage["outcome_explanation"],
        "why_it_matters": f"This {policy['impact'].lower()}-impact requirement is not currently decision-provable.",
        "current_evidence": current_ids, "missing_evidence": coverage["missing_evidence"],
        "evidence_scope": coverage.get("evidence_scope"),
        "blocking": policy["blocking"], "impact": policy["impact"], "confidence": "EVIDENCE_LIMITED",
        "possible_resolution_methods": copy.deepcopy(policy["resolution"]), "responsible_party": policy["responsible_party"],
        "estimated_time": None, "estimated_cost": None, "dependencies": [],
        "affected_constraints": [requirement_id],
        "affected_scenarios": _affected_scenarios(coverage.get("site_id"), current_ids, dependency_lookup),
        "created_at": previous_gap.get("created_at", now) if previous_gap else now,
        "last_evaluated_at": now,
        "status": "STALE" if coverage.get("stale_evidence") else previous_gap.get("status", "OPEN") if previous_gap else "OPEN",
        "action_type": policy["action_type"], "recipient_category": policy["recipient_category"],
        "disqualification_likelihood": policy["disqualification_likelihood"],
        "critical_milestone": policy["critical_milestone"],
    }


def _rank_actions(gaps: list[dict], project_id: str, rfis: list[dict] | None = None) -> list[dict]:
    requests = {item.get("action_id"): item for item in (rfis or [])}
    actions = []
    for gap in gaps:
        downstream = len(gap["affected_constraints"]) + len({item["scenario_id"] for item in gap["affected_scenarios"]})
        impact_score = _IMPACT_SCORE[gap["impact"]]
        disqualification_score = _DISQUALIFICATION_SCORE[gap["disqualification_likelihood"]]
        downstream_score = min(30, downstream * 5)
        milestone_score = 30 if gap["blocking"] and gap["critical_milestone"] else 0
        stale_score = 10 if gap["status"] == "STALE" else 0
        centrality = int(_policy(gap["requirement_id"])["dependency_centrality"])
        centrality_score = min(20, centrality * 5)
        score = impact_score + disqualification_score + downstream_score + milestone_score + stale_score + centrality_score
        action_id = _stable_id("action", project_id, gap["site_id"], gap["requirement_id"], gap["action_type"])
        action = {
            "action_id": action_id, "gap_id": gap["gap_id"], "type": gap["action_type"],
            "title": gap["possible_resolution_methods"][0], "recipient_category": gap["recipient_category"],
            "project_id": project_id, "site_id": gap["site_id"], "requirement_id": gap["requirement_id"],
            "required_evidence": copy.deepcopy(gap["missing_evidence"]), "dependencies": copy.deepcopy(gap["dependencies"]),
            "affected_decisions": copy.deepcopy(gap["affected_constraints"]),
            "affected_scenarios": copy.deepcopy(gap["affected_scenarios"]), "status": "PROPOSED",
            "score": score,
            "score_provenance": {
                "version": "project_next_action_priority_v1", "impact": {"category": gap["impact"], "score": impact_score},
                "disqualification_likelihood": {"category": gap["disqualification_likelihood"], "score": disqualification_score, "source": "deterministic_requirement_policy"},
                "downstream_decisions": {"count": downstream, "score": downstream_score},
                "dependency_centrality": {"count": centrality, "score": centrality_score},
                "evidence_expiration": {"stale": gap["status"] == "STALE", "score": stale_score},
                "critical_milestone": {"blocking": gap["blocking"] and gap["critical_milestone"], "score": milestone_score},
                "cost": {"value": None, "score_adjustment": 0}, "expected_time": {"value": None, "score_adjustment": 0},
            },
            "expected_impact": gap["impact"], "estimated_time": None, "estimated_cost": None,
        }
        if action_id in requests:
            request = requests[action_id]
            action.update(
                status={"DRAFT": "DRAFTED"}.get(request.get("status"), request.get("status", "DRAFTED")),
                rfi_id=request["rfi_id"],
            )
        actions.append(action)
    actions.sort(key=lambda item: (-item["score"], item["action_id"]))
    for rank, action in enumerate(actions, 1):
        action["rank"] = rank
    return actions


def _readiness(coverage: list[dict], gaps: list[dict], actions: list[dict]) -> tuple[dict, dict, str]:
    domains: dict[str, list[dict]] = {}
    for item in coverage:
        domains.setdefault(item["domain"], []).append(item)
    readiness = {}
    for domain, items in sorted(domains.items()):
        if any(item["status"] == "FAIL" for item in items):
            state = "FAILED"
        elif any(item["impact"] == "CRITICAL" and item["blocking"] and not item["decision_provable"] for item in items):
            state = "CRITICAL"
        elif all(item["status"] == "PASS" for item in items):
            state = "READY"
        elif any(item["coverage"] == "PARTIAL" for item in items):
            state = "PARTIAL"
        else:
            state = "UNRESOLVED"
        readiness[domain] = {"status": state, "requirements": [item["requirement_id"] for item in items]}
    critical = [item for item in gaps if item["blocking"] and item["impact"] == "CRITICAL"]
    risk = {
        "critical_blockers": len(critical), "material_unknowns": len(gaps),
        "failed_requirements": sum(item["status"] == "FAIL" for item in coverage),
        "recommended_next_action_id": actions[0]["action_id"] if actions else None,
    }
    if risk["failed_requirements"]:
        state = "NOT_READY"
    elif critical:
        state = "BLOCKED"
    elif gaps:
        state = "UNRESOLVED"
    elif coverage and all(item["decision_provable"] for item in coverage):
        state = "READY_FOR_CURRENT_DECISION"
    else:
        state = "NO_ACTIVE_SITE"
    return readiness, risk, state


def build_project_intelligence(
    project: dict,
    snapshot_loader: Callable[[str], dict | None],
    capabilities: dict[str, dict],
    constraint_fields: dict[str, tuple[str, ...]],
    *,
    dependency_lookup: Callable[[str, list[str]], list[dict]] | None = None,
    now: float | None = None,
    changed_evidence_ids: list[str] | None = None,
) -> ProjectIntelligence:
    evaluated_at = time.time() if now is None else float(now)
    previous = project.get("project_intelligence")
    candidate = _active_candidate(project)
    snapshot = snapshot_loader(candidate["snapshot_id"]) if candidate and candidate.get("snapshot_id") else None
    constraints = copy.deepcopy(project.get("request", {}).get("constraints", []))
    coverage = [
        _coverage_item(item, candidate, snapshot, capabilities, constraint_fields, evaluated_at)
        for item in constraints
    ]
    external_state = (project.get("external_evidence_by_site") or {}).get((candidate or {}).get("site_id"), {})
    external_records = [item for item in external_state.get("records", []) if isinstance(item, dict) and item.get("evidence_id")]
    for item in coverage:
        matching = [record for record in external_records if item["requirement_id"] in record.get("requirement_ids", []) and record.get("status") == "ok"]
        for record in matching:
            if record["evidence_id"] not in item["available_evidence"]:
                item["available_evidence"].append(record["evidence_id"])
            if record["evidence_id"] not in item["evidence_ids"]:
                item["evidence_ids"].append(record["evidence_id"])
        if matching and not item["decision_provable"]:
            item["evidence_available"] = True
            item["coverage"] = "PARTIAL"
            item["semantic_strength"] = "UNSUPPORTED_SEMANTICS" if item["unsupported_semantics"] or item["missing_evidence"] else "SOURCE_BACKED_SIGNAL"
    storage_requirements = project.get("request", {}).get("storage_requirements") or {}
    for item in coverage:
        if item["requirement_id"] == "bess_export_interconnection":
            phase_mw = storage_requirements.get("phase_1_power_mw")
            if isinstance(phase_mw, (int, float)):
                item["title"] = f"{phase_mw:g} MW export / injection interconnection"
            if isinstance(storage_requirements.get("expansion_power_mw"), (int, float)):
                item["missing_evidence"] = list(dict.fromkeys([*item["missing_evidence"], "utility_or_iso_confirmed_expansion_export_injection_capacity_mw"]))
    gaps = [
        _gap_from_coverage(project, item, evaluated_at, previous, dependency_lookup)
        for item in coverage if not item["decision_provable"]
    ]
    changed = set(changed_evidence_ids or ())
    resolved_gaps = []
    active_ids = {item["gap_id"] for item in gaps}
    for old in (previous or {}).get("evidence_gaps", []):
        if old.get("gap_id") not in active_ids:
            closed = copy.deepcopy(old)
            closed.update(status="RESOLVED", last_evaluated_at=evaluated_at)
            resolved_gaps.append(closed)
    actions = _rank_actions(gaps, project["project_id"], project.get("rfis"))
    readiness, risk, project_state = _readiness(coverage, gaps, actions)
    evidence_items = []
    dependencies = []
    for item in coverage:
        for evidence_id in item["available_evidence"]:
            record = (snapshot or {}).get("evidence", {}).get(evidence_id, {})
            ref = {
                "evidence_id": evidence_id, "snapshot_id": item["snapshot_id"], "site_id": item["site_id"],
                "status": record.get("status"), "scope": record.get("scope"), "provider": record.get("provider"),
                "source": record.get("source"), "source_url": record.get("source_url"), "unit": record.get("unit"),
                "semantic_strength": record.get("semantic_strength", "INSUFFICIENT_EVIDENCE"),
                "semantic_class": record.get("semantic_class"), "claim_limits": copy.deepcopy(record.get("claim_limits", [])),
                "observed_at": record.get("observed_at"), "expires_at": record.get("expires_at"),
                "evidence_hash": record.get("evidence_hash"),
            }
            if ref not in evidence_items:
                evidence_items.append(ref)
        for evidence_id in dict.fromkeys([*item["available_evidence"], *item["evidence_ids"]]):
            dependency = {"requirement_id": item["requirement_id"], "evidence_id": evidence_id, "snapshot_id": item["snapshot_id"]}
            if dependency not in dependencies:
                dependencies.append(dependency)
    for record in external_records:
        ref = {
            "evidence_id": record["evidence_id"], "snapshot_id": (snapshot or {}).get("snapshot_id"),
            "site_id": (candidate or {}).get("site_id"), "status": record.get("status"),
            "scope": record.get("scope"), "provider": record.get("provider"), "source": record.get("dataset"),
            "source_url": record.get("source_url"), "unit": record.get("unit"),
            "semantic_strength": record.get("semantic_strength", "SOURCE_BACKED_SIGNAL"),
            "semantic_class": "AUTHORITATIVE_EXTERNAL_RECORD", "claim_limits": [],
            "observed_at": record.get("observed_at"), "expires_at": record.get("expires_at"),
            "evidence_hash": record.get("source_hash"), "document_title": record.get("document_title"),
            "section_reference": record.get("section_reference"), "excerpt": record.get("excerpt"),
            "human_review_required": record.get("human_review_required", True),
        }
        if ref not in evidence_items:
            evidence_items.append(ref)
        for requirement_id in record.get("requirement_ids", []):
            dependency = {"requirement_id": requirement_id, "evidence_id": record["evidence_id"], "snapshot_id": (snapshot or {}).get("snapshot_id")}
            if dependency not in dependencies:
                dependencies.append(dependency)
    active_site = None if candidate is None else {
        "candidate_id": candidate["candidate_id"], "site_id": candidate.get("site_id"),
        "site_snapshot_id": candidate.get("snapshot_id"), "title": (candidate.get("summary") or {}).get("title"),
    }
    state: ProjectIntelligence = {
        "schema_version": "project_intelligence_v1", "project_id": project["project_id"],
        "project_requirements": constraints,
        "candidate_sites": [
            {"candidate_id": item["candidate_id"], "site_id": item.get("site_id"), "site_snapshot_id": item.get("snapshot_id"), "status": item.get("reconciliation_status")}
            for item in project.get("candidates", [])
        ],
        "active_site": active_site, "canonical_constraints": constraints,
        "evidence_items": evidence_items,
        "evidence_semantics": {
            strength: sorted(item["evidence_id"] for item in evidence_items if item.get("semantic_strength") == strength)
            for strength in ("DIRECTLY_VERIFIED", "SOURCE_BACKED_SIGNAL", "DERIVED", "INSUFFICIENT_EVIDENCE", "UNSUPPORTED_SEMANTICS")
        },
        "evidence_dependencies": dependencies,
        "decision_state": copy.deepcopy(project.get("decision", {})), "evidence_coverage": coverage,
        "unresolved_issues": gaps, "evidence_gaps": gaps + resolved_gaps,
        "recommended_actions": actions, "assumptions": copy.deepcopy(project.get("assumptions", [])),
        "readiness": readiness, "risk_state": risk, "last_evaluated_at": evaluated_at,
        "project_readiness_state": project_state,
        "change_impact": None if not changed else {
            "changed_evidence_ids": sorted(changed),
            "affected_requirement_ids": sorted({item["requirement_id"] for item in coverage if changed.intersection(item["evidence_ids"])}),
            "affected_gap_ids": sorted(item["gap_id"] for item in gaps if changed.intersection(item["current_evidence"])),
            "readiness_recalculated": True,
        },
        "state_hash": "",
    }
    state["state_hash"] = hashlib.sha256(_canonical({key: value for key, value in state.items() if key not in {"state_hash", "last_evaluated_at"}}).encode("utf-8")).hexdigest()
    return state
