from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BenchmarkResult:
    cases: int
    metrics: dict[str, float]


def evaluate_cases(cases: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> BenchmarkResult:
    """Deterministic fixture scorer; it measures behavior but does not claim model improvement."""
    if len(cases) != len(predictions):
        raise ValueError("Benchmark cases and predictions must align.")
    totals = {
        "project_spec_accuracy": 0,
        "task_validity": 0,
        "citation_correctness": 0,
        "unsupported_claim_rejection": 0,
        "tool_choice_accuracy": 0,
        "scenario_postcondition_rate": 0,
    }
    unnecessary_tasks = missing_tasks = expected_tasks = actual_tasks = 0
    false_passes = false_fails = unnecessary_calls = 0
    credits = tokens = latency_ms = 0.0
    for expected, actual in zip(cases, predictions):
        totals["project_spec_accuracy"] += actual.get("project_spec") == expected.get("project_spec")
        totals["task_validity"] += actual.get("task_types") == expected.get("task_types")
        totals["citation_correctness"] += set(actual.get("evidence_ids", [])) == set(expected.get("evidence_ids", []))
        totals["unsupported_claim_rejection"] += actual.get("verification") == expected.get("verification")
        totals["tool_choice_accuracy"] += actual.get("tool") == expected.get("tool")
        totals["scenario_postcondition_rate"] += actual.get("state_hash_changed") == expected.get("state_hash_changed")
        expected_set, actual_set = set(expected.get("task_types", [])), set(actual.get("task_types", []))
        unnecessary_tasks += len(actual_set - expected_set)
        missing_tasks += len(expected_set - actual_set)
        expected_tasks += len(expected_set)
        actual_tasks += len(actual_set)
        false_passes += int(bool(actual.get("false_pass")))
        false_fails += int(bool(actual.get("false_fail")))
        unnecessary_calls += int(actual.get("unnecessary_calls", 0))
        credits += float(actual.get("mireye_credits", 0))
        tokens += float(actual.get("model_tokens", 0))
        latency_ms += float(actual.get("latency_ms", 0))
    count = len(cases)
    metrics = {key: (value / count if count else 0.0) for key, value in totals.items()}
    metrics.update(
        {
            "unnecessary_task_rate": unnecessary_tasks / max(actual_tasks, 1),
            "missing_task_rate": missing_tasks / max(expected_tasks, 1),
            "false_pass_rate": false_passes / max(count, 1),
            "false_fail_rate": false_fails / max(count, 1),
            "unnecessary_call_rate": unnecessary_calls / max(actual_tasks, 1),
            "mireye_credits_per_task": credits / max(actual_tasks, 1),
            "model_tokens_per_task": tokens / max(actual_tasks, 1),
            "latency_ms_per_task": latency_ms / max(actual_tasks, 1),
        }
    )
    return BenchmarkResult(count, metrics)
