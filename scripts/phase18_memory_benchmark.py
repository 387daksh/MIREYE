"""Small, read-only Phase 18 comparison against an existing project."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

from app.ai.accounting import finish, start
from app.ai.evaluation.verifier import VerificationEngine
from app.ai.memory.benchmark import TEMPORAL_FIXTURE_MARKER, temporal_fixture_changes, temporal_fixture_from_t1
from app.ai.providers import OpenAIStructuredModelProvider
from app.ai.schemas.orchestration import AgentObservation, AgentRole, Claim, ObservationStatus
from app.main import document_memory, workspace_store


SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": ["SUPPORTED", "UNSUPPORTED"]},
        "temporal_scope": {"type": "string", "enum": ["CURRENT", "T1", "TEST_FIXTURE_T2"]},
    },
    "required": ["answer", "evidence_ids", "status", "temporal_scope"],
    "additionalProperties": False,
}


def _evidence(snapshot: dict[str, Any], fields: list[str]) -> list[dict[str, Any]]:
    return [dict(record, evidence_id=field, snapshot_id=snapshot["snapshot_id"]) for field in fields if (record := snapshot["evidence"].get(field))]


async def _answer(model: OpenAIStructuredModelProvider, question: str, context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], float]:
    token = start(model.model)
    began = time.perf_counter()
    try:
        answer = await model.generate({
            "module": "phase18_memory_benchmark",
            "schema_name": "grounded_memory_answer",
            "schema": SCHEMA,
            "instructions": (
                "Answer only from the supplied context. Cite only supplied evidence/action IDs. "
                "Never claim that proximity, voltage, queue totals, or mapped infrastructure proves deliverable capacity. "
                f"{TEMPORAL_FIXTURE_MARKER} is test data, never an observed real-world event."
            ),
            "input": {"question": question, "context": context},
        })
    finally:
        usage = finish(token)
    return answer, usage, round((time.perf_counter() - began) * 1000, 3)


def _score(answer: dict[str, Any], case: dict[str, Any], verification_state: str) -> dict[str, Any]:
    cited = {str(item) for item in answer["evidence_ids"]}
    expected = set(case["expected_ids"])
    return {
        "citation_recall": len(cited & expected) / len(expected) if expected else 1.0,
        "citation_precision": len(cited & expected) / len(cited) if cited else 0.0,
        "status_correct": answer["status"] == case["expected_status"],
        "temporal_correct": answer["temporal_scope"] == case["temporal_scope"],
        "verifier_state": verification_state,
    }


async def main(project_id: str, snapshot_id: str) -> None:
    workspace_store.initialize()
    project = workspace_store.get_diligence_project(project_id)
    snapshot = workspace_store.get_site_snapshot(snapshot_id)
    if not project or not snapshot or project["workspace_id"] != snapshot["workspace_id"]:
        raise SystemExit("Project and immutable T1 snapshot must exist in the same workspace.")
    changed_field = "nearest_transmission_line_distance_m"
    before = snapshot["evidence"].get(changed_field, {}).get("value")
    if not isinstance(before, (int, float)):
        raise SystemExit(f"T1 lacks numeric {changed_field} for a controlled fixture.")
    test_t2 = temporal_fixture_from_t1(snapshot, changed_field, before + 1)
    intelligence = project["project_intelligence"]
    actions = intelligence.get("recommended_actions") or []
    power = next(item for item in intelligence["evidence_coverage"] if item["requirement_id"] == "bess_export_interconnection")
    entitlement = next(item for item in intelligence["evidence_coverage"] if item["requirement_id"] == "energy_storage_entitlement")
    cases = [
        ("site identity", ["parcel_id", "parcel_address", "parcel_match_type"], "SUPPORTED", "CURRENT"),
        ("parcel scale", ["parcel_area_m2"], "SUPPORTED", "CURRENT"),
        ("transmission context", ["nearest_transmission_line_distance_m", "nearest_transmission_line_voltage_kv"], "SUPPORTED", "CURRENT"),
        ("substation context", ["nearest_substation_distance_m", "nearest_substation_max_voltage_kv", "nearest_substation_status"], "SUPPORTED", "CURRENT"),
        ("utility and ISO", ["electric_utility_service_territory", "iso_rto"], "SUPPORTED", "CURRENT"),
        ("queue context", ["interconnection_queue_active_capacity_county_mw", "interconnection_queue_active_capacity_ercot_mw"], "SUPPORTED", "CURRENT"),
        ("what evidence supports the current power status", list(power["evidence_ids"]), "UNSUPPORTED", "CURRENT"),
        ("what zoning or entitlement information exists", list(entitlement["evidence_ids"]), "UNSUPPORTED", "CURRENT"),
        ("which action resolves the highest-priority blocker", [], "SUPPORTED", "CURRENT"),
        ("what was true at T1 about transmission distance", [changed_field], "SUPPORTED", "T1"),
        ("what is true in TEST T2 about transmission distance", [changed_field], "SUPPORTED", "TEST_FIXTURE_T2"),
        ("what changed between T1 and TEST T2", [changed_field], "SUPPORTED", "TEST_FIXTURE_T2"),
    ]
    model, verifier = OpenAIStructuredModelProvider(), VerificationEngine()
    flat = {"project_intelligence": intelligence, "decision_history": project.get("decision_history", []), "evidence": _evidence(snapshot, list(snapshot["evidence"]))}
    output: list[dict[str, Any]] = []
    for index, (question, fields, expected_status, temporal_scope) in enumerate(cases, 1):
        active_snapshot = test_t2 if temporal_scope == "TEST_FIXTURE_T2" else snapshot
        expected = fields or [str(actions[0]["action_id"])]
        structured = _evidence(active_snapshot, fields)
        retrieval = await document_memory.retrieve(project_id, question, limit=3, as_of=active_snapshot["observed_at"])
        hybrid = {
            "retrieval_order": ["exact identifiers", "structured filters", "graph traversal", "temporal filters", "vector similarity"],
            "temporal_scope": temporal_scope,
            "validation_fixture": test_t2.get("validation_fixture") if active_snapshot is test_t2 else None,
            "structured_evidence": structured,
            "graph_records": retrieval["graph_records"],
            "document_chunks": retrieval["document_chunks"],
            "actions": actions,
        }
        case = {"id": index, "question": question, "expected_ids": expected, "expected_status": expected_status, "temporal_scope": temporal_scope}
        baseline_context = {**flat, "temporal_scope": temporal_scope, "test_t2": test_t2 if active_snapshot is test_t2 else None}
        baseline, baseline_usage, baseline_latency = await _answer(model, question, baseline_context)
        hybrid_answer, hybrid_usage, hybrid_latency = await _answer(model, question, hybrid)
        evidence = structured or _evidence(snapshot, list(snapshot["evidence"]))
        observation = AgentObservation(task_id=f"benchmark_{index}", agent_role=AgentRole.SITE_INTELLIGENCE, status=ObservationStatus.COMPLETED, summary=hybrid_answer["answer"], claims=[Claim(claim_id=f"benchmark_claim_{index}", text=hybrid_answer["answer"], evidence_ids=[item for item in hybrid_answer["evidence_ids"] if item in {row["evidence_id"] for row in evidence}])])
        verification = verifier.verify(observation, {"evidence_items": evidence, "deterministic_outcomes": {}, "now": time.time()})
        output.append({**case, "vector_queries": retrieval["vector_queries"], "baseline": {"usage": baseline_usage, "latency_ms": baseline_latency, "score": _score(baseline, case, "NOT_RUN")}, "phase18": {"usage": hybrid_usage, "latency_ms": hybrid_latency, "score": _score(hybrid_answer, case, verification.state.value)}})
    test_changes = temporal_fixture_changes(project_id=project_id, site_id=snapshot["site_id"], t1=snapshot, test_t2=test_t2, intelligence=intelligence)
    def mean_usage(section: str, key: str) -> float:
        values = [float(item[section]["usage"].get(key, 0)) for item in output]
        return round(sum(values) / len(values), 3)
    def mean_value(section: str, key: str) -> float:
        return round(sum(float(item[section][key]) for item in output) / len(output), 3)
    def passed(section: str, key: str) -> int:
        return sum(bool(item[section]["score"][key]) for item in output)
    print(json.dumps({
        "project_id": project_id, "t1": snapshot_id, "test_t2": test_t2["validation_fixture"],
        "test_t2_change_types": sorted({item["semantic_change_type"] for item in test_changes}),
        "model": model.model, "case_count": len(output), "results": output,
        "summary": {
            "baseline_input_tokens_mean": mean_usage("baseline", "input_tokens"), "phase18_input_tokens_mean": mean_usage("phase18", "input_tokens"),
            "baseline_latency_ms_mean": mean_value("baseline", "latency_ms"), "phase18_latency_ms_mean": mean_value("phase18", "latency_ms"),
            "baseline_status_correct": passed("baseline", "status_correct"), "phase18_status_correct": passed("phase18", "status_correct"),
        },
    }, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--snapshot", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.project, args.snapshot))
