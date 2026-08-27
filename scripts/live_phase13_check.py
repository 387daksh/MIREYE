"""Bounded live Phase 13 refresh validation against an existing temporary project."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import MIREYE_API_KEY, MIREYE_BASE_URL
from app.diligence import DiligenceService
from app.mireye_client import MireyeClient
from app.sandbox import SiteSnapshotService
from app.sandbox_scenarios import ScenarioService
from app.workspace.store import WorkspaceStore


def _credits(usage: dict) -> float | int | None:
    value = usage.get("credits")
    if isinstance(value, dict):
        for key in ("used", "usage", "consumed"):
            if isinstance(value.get(key), (int, float)):
                return value[key]
    return value if isinstance(value, (int, float)) else None


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Existing temporary integration SQLite database")
    parser.add_argument("--project", required=True)
    parser.add_argument("--field", default="nearest_transmission_line_distance_m")
    parser.add_argument("--confirm", action="store_true", help="Explicitly approve the quoted MIREYE refresh")
    args = parser.parse_args()
    if not MIREYE_API_KEY:
        raise SystemExit("MIREYE_API_KEY is required.")

    store = WorkspaceStore(args.db)
    client = MireyeClient(api_key=MIREYE_API_KEY, base_url=MIREYE_BASE_URL, mode="live")
    scenarios = ScenarioService(store)
    sandbox = SiteSnapshotService(store, client, scenarios=scenarios)
    diligence = DiligenceService(store, sandbox)
    project = diligence.get(args.project)
    candidate = next((item for item in project["candidates"] if item.get("snapshot_id")), None)
    if candidate is None:
        raise SystemExit("Project has no enriched candidate.")

    normal_check = diligence.check_now(args.project)
    usage_before = await client.usage()
    plan = await sandbox.quote_refresh(
        candidate["snapshot_id"], fields=[args.field],
        test_expiry_overrides={args.field: time.time() - 1},
    )
    report = {
        "test_only_freshness_override": [args.field], "normal_check": normal_check,
        "quote": {
            "spend_plan_id": plan.get("spend_plan_id"), "requested_fields": plan.get("requested_fields"),
            "fetch_fields": plan.get("fetch_fields"), "quote_id": plan.get("mireye_quote_id"),
            "quote_expires_at": plan.get("quote_expires_at"), "estimated_credits": plan.get("expected_credits"),
        },
        "confirmed": args.confirm,
    }
    if args.confirm:
        completed = await diligence.confirm_candidate_refresh(
            args.project, candidate["candidate_id"], plan["spend_plan_id"], confirmed=True,
        )
        usage_after = await client.usage()
        before_credits, after_credits = _credits(usage_before), _credits(usage_after)
        report.update({
            "actual_credit_delta": after_credits - before_credits if isinstance(before_credits, (int, float)) and isinstance(after_credits, (int, float)) else None,
            "snapshot_before": completed["refresh"]["previous_snapshot_id"],
            "snapshot_after": completed["refresh"]["snapshot"]["snapshot_id"],
            "changed_fields": sorted(completed["refresh"]["snapshot_diff"]["field_changes"]),
            "changes": [{
                "field": item["field"], "type": item["semantic_change_type"], "significance": item["significance"],
                "affected_requirements": item["affected_requirements"], "affected_scenarios": item["affected_scenarios"],
                "affected_readiness": item["affected_readiness"], "affected_actions": item["affected_actions"],
            } for item in completed["changes"]],
            "evaluation_runs": [{
                "scenario_id": item["scenario_id"], "revision": item["revision"], "status": item["status"],
                "affected_constraint_ids": item["affected_constraint_ids"],
            } for item in completed["refresh"]["evaluation_runs"]],
            "readiness": completed["project"].get("project_intelligence", {}).get("readiness"),
            "next_actions": completed["project"].get("project_intelligence", {}).get("recommended_actions", []),
        })
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    asyncio.run(main())
