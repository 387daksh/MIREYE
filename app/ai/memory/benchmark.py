"""Offline retrieval benchmark; it measures retrieval, not model narration."""
from __future__ import annotations

import json
import copy
import hashlib
import time
from typing import Any

from app.ai.memory.graph import MemoryContextBuilder
from app.project_changes import changes_from_refresh
from app.sandbox import SiteSnapshotService


TEMPORAL_FIXTURE_MARKER = "TEST_FIXTURE_DERIVED_FROM_REAL_T1"


def temporal_fixture_from_t1(snapshot: dict[str, Any], field: str, value: Any) -> dict[str, Any]:
    """Make an in-memory test T2. It is never a provider observation or persisted history."""
    if field not in snapshot.get("evidence", {}):
        raise ValueError(f"T1 does not contain fixture field: {field}")
    fixture = copy.deepcopy(snapshot)
    fixture_key = f"{snapshot['snapshot_id']}:{field}:{value}"
    fixture["snapshot_id"] = f"test_t2_{hashlib.sha256(fixture_key.encode()).hexdigest()[:24]}"
    fixture["validation_fixture"] = {"kind": TEMPORAL_FIXTURE_MARKER, "derived_from_snapshot_id": snapshot["snapshot_id"], "changed_field": field}
    fixture.setdefault("provider_metadata", {})["validation_fixture"] = fixture["validation_fixture"]
    fixture["evidence"][field].pop("evidence_hash", None)
    fixture["evidence"][field]["value"] = value
    fixture["evidence"][field]["observed_at"] = float(snapshot.get("observed_at", 0)) + 1
    fixture["observed_at"] = float(snapshot.get("observed_at", 0)) + 1
    fixture["created_at"] = fixture["observed_at"]
    fixture["raw_response_hash"] = hashlib.sha256(json.dumps(fixture["raw_response"], sort_keys=True, default=str).encode()).hexdigest()
    return fixture


def temporal_fixture_changes(
    *, project_id: str, site_id: str, t1: dict[str, Any], test_t2: dict[str, Any], intelligence: dict[str, Any],
) -> list[dict[str, Any]]:
    """Exercise the production diff without persisting a test fixture as real history."""
    return changes_from_refresh(
        project_id=project_id, site_id=site_id, before_snapshot=t1, after_snapshot=test_t2,
        snapshot_diff=SiteSnapshotService.snapshot_diff(t1, test_t2),
        before_intelligence=intelligence, after_intelligence=intelligence,
        detected_at=float(test_t2["observed_at"]),
    )


def evaluate_retrieval(
    builder: MemoryContextBuilder, project_id: str, cases: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compare a flat current-context baseline with bounded graph retrieval."""
    started = time.perf_counter()
    baseline_hits = phase18_hits = 0
    phase18_tokens = 0
    results = []
    project = builder.graph._project(project_id)
    flat = json.dumps(project.get("project_intelligence") or {}, sort_keys=True).casefold()
    for case in cases:
        expected = {str(item) for item in case.get("expected_ids", [])}
        baseline = {item for item in expected if item.casefold() in flat}
        context = builder.build(project_id, str(case["query"]), "user")
        selected = json.dumps(context["selected_records"], sort_keys=True)
        retrieved = {item for item in expected if item in selected}
        baseline_hits += len(baseline)
        phase18_hits += len(retrieved)
        phase18_tokens += context["context_tokens"]
        results.append({"query": case["query"], "expected_ids": sorted(expected), "baseline_hits": sorted(baseline), "phase18_hits": sorted(retrieved), "context_tokens": context["context_tokens"]})
    total = sum(len(case.get("expected_ids", [])) for case in cases)
    return {
        "cases": results,
        "baseline": {"recall": baseline_hits / total if total else 1.0, "context_tokens": len(flat) // 4},
        "phase18": {"recall": phase18_hits / total if total else 1.0, "context_tokens": phase18_tokens},
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "note": "Citation correctness, temporal correctness, and answer grounding require a real project corpus and reviewed answers.",
    }
