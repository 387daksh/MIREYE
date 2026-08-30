"""Opt-in live MIREYE integration and Site Sandbox lifecycle check.

Run only with real credentials intentionally supplied through environment
variables. This script creates a temporary SQLite database and never imports
the FastAPI singleton that targets app/data/workspaces.db.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_ADDRESS = "1600 Pennsylvania Avenue NW, Washington, DC"
WORKSPACE_ID = "live-mireye-integration"
BATCH_FIELDS = ["parcel_id", "parcel_match_type", "parcel_match_distance_m", "parcel_boundary_geojson"]


class LiveIntegrationError(RuntimeError):
    pass


def _environment() -> tuple[str, str]:
    load_dotenv(ROOT / ".env")
    key = os.environ.get("MIREYE_API_KEY")
    base_url = os.environ.get("MIREYE_BASE_URL", "https://api.mireye.com").rstrip("/")
    if not key:
        raise LiveIntegrationError("MIREYE_API_KEY is required. Normal pytest never needs it.")
    return key, base_url


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _credit_value(payload: Any) -> float | None:
    if isinstance(payload, dict):
        for key in ("credits_remaining", "credits_used", "remaining_credits", "used_credits"):
            value = payload.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        credits = payload.get("credits")
        if isinstance(credits, dict):
            for key in ("remaining", "used"):
                value = credits.get(key)
                if isinstance(value, (int, float)):
                    return float(value)
    return None


def _batch_results(payload: dict) -> list[dict]:
    for key in ("results", "items", "locations"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _refresh_constraint(snapshot: dict) -> tuple[str, dict]:
    evidence = snapshot["evidence"]
    candidates = (
        ("slope_degrees", {"constraint_id": "max_resolution_point_slope_degrees", "max_degrees": 90.0}),
        ("within_floodplain_polygon", {"constraint_id": "resolution_point_outside_fema_sfha"}),
        ("wetland_fraction_of_parcel", {"constraint_id": "max_nwi_wetland_fraction_of_parcel", "max_fraction": 1.0}),
        ("nearest_major_road_distance_m", {"constraint_id": "max_resolution_point_major_road_distance_m", "max_distance_m": 1_000_000.0}),
    )
    for field, constraint in candidates:
        record = evidence.get(field, {})
        if record.get("value") is not None and record.get("status") in {"ok", None}:
            return field, constraint
    # Parcel geometry is always needed for a valid SiteSnapshot and is a safe
    # fallback for exercising dependency-driven re-evaluation.
    return "parcel_boundary_geojson", {"constraint_id": "footprint_inside_parcel"}


async def _expect_http_error(coro) -> int | None:
    try:
        await coro
    except httpx.HTTPStatusError as exc:
        return exc.response.status_code
    return None


async def run(address: str, report_path: Path | None = None) -> dict:
    api_key, base_url = _environment()
    fd, db_name = tempfile.mkstemp(prefix="mireye-live-integration-", suffix=".db")
    os.close(fd)
    db_path = Path(db_name)
    report: dict[str, Any] = {"address": address, "database": str(db_path), "database_is_temporary": True}
    try:
        from app.mireye_client import MireyeClient
        from app.sandbox import ConfirmationRequired, SiteSnapshotService, scene_state_from_snapshot
        from app.sandbox_agent import SandboxSession, SandboxToolExecutor
        from app.sandbox_proposal import DEFAULT_MINIMUM_SETBACK_M, generate_bess_proposal
        from app.sandbox_scenarios import ScenarioService
        from app.workspace.store import WorkspaceStore

        class LedgerClient(MireyeClient):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.request_ledger: list[dict] = []

            async def _request(self, method: str, path: str, json_body: dict | None = None, params: dict | None = None) -> dict:
                body = json_body or {}
                locations = body.get("locations")
                location_count = len(locations) if isinstance(locations, list) else locations if isinstance(locations, int) else 1 if path in {"/v1/lookup", "/v1/fetch"} else 0
                entry = {
                    "order": len(self.request_ledger) + 1,
                    "started_at": _utc_now(),
                    "method": method,
                    "endpoint": path,
                    "location_count": location_count,
                    "field_count": len(body.get("fields") or []),
                    "fields": body.get("fields"),
                    "preset": body.get("preset"),
                    "metered": path in {"/v1/fetch", "/v1/fetch/batch"} or (path == "/v1/lookup" and body.get("include_parcel", True)),
                }
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "Mireye-Agent-Platform/1.0",
                }
                try:
                    async with httpx.AsyncClient(timeout=self.timeout) as transport:
                        response = await transport.request(method, f"{self.base_url}{path}", json=json_body, params=params, headers=headers)
                    entry.update({
                        "completed_at": _utc_now(),
                        "status_code": response.status_code,
                        "request_id": response.headers.get("x-request-id") or response.headers.get("request-id") or response.headers.get("x-correlation-id"),
                    })
                    response.raise_for_status()
                    payload = response.json()
                    entry["quoted_credits"] = payload.get("credits_total") if path == "/v1/fetch/quote" and isinstance(payload, dict) else None
                    entry["response_credit_metadata"] = {
                        key: payload[key]
                        for key in ("credits", "credits_charged", "credits_total", "usage", "allowance")
                        if isinstance(payload, dict) and key in payload
                    }
                    return payload
                except httpx.HTTPStatusError as exc:
                    entry.update({"completed_at": _utc_now(), "status_code": exc.response.status_code, "metered": False})
                    raise
                finally:
                    self.request_ledger.append(entry)

        client = LedgerClient(api_key=api_key, base_url=base_url, mode="live")
        store = WorkspaceStore(db_path)
        scenarios = ScenarioService(store)
        service = SiteSnapshotService(store, client, scenarios=scenarios)

        openapi = await client._request("GET", "/v1/openapi.json")
        catalog = await client.meta_fields()
        report["mireye"] = {
            "base_url": base_url,
            "api_version": openapi.get("info", {}).get("version"),
            "field_catalog_version": catalog.get("version") or catalog.get("catalog_version"),
            "field_count": len(catalog.get("fields", [])),
            "contract_paths_checked": [path for path in ("/v1/meta/fields", "/v1/lookup", "/v1/fetch/quote", "/v1/fetch", "/v1/fetch/batch") if path in openapi.get("paths", {})],
        }
        usage_before = await client.usage()

        resolved = await service.resolve(input=address, kind="address")
        if resolved["requires_selection"]:
            raise LiveIntegrationError("Demo address is ambiguous; choose a stable address that resolves to one parcel.")
        if len(resolved["candidates"]) != 1:
            raise LiveIntegrationError("Demo address did not resolve to one usable MIREYE location.")
        location = resolved["candidates"][0]
        report["lookup"] = {"status": resolved["status"], "candidate_count": len(resolved["candidates"]), "location": {"lat": location["lat"], "lng": location["lng"]}}

        quote = await service.quote(lat=location["lat"], lng=location["lng"])
        report["quote"] = {
            "requested_fields": quote["fields"], "location_count": 1,
            "quote_id": quote["quote"].get("quote_id") or quote["quote"].get("id"),
            "quote_expiry": quote["quote"].get("expires_at") or quote["quote"].get("quote_expires_at"),
            "estimated_credits": SiteSnapshotService._estimated_credits(quote["quote"]),
        }

        snapshot_t1 = await service.create_snapshot(
            workspace_id=WORKSPACE_ID, lat=location["lat"], lng=location["lng"], confirmed=True,
        )
        stored_t1 = service.get_snapshot(snapshot_t1["snapshot_id"])
        if stored_t1 is None or stored_t1["raw_response_hash"] != snapshot_t1["raw_response_hash"]:
            raise LiveIntegrationError("Persisted SiteSnapshot hash is not stable.")
        identity = snapshot_t1["parcel_identity"]
        if identity["parcel_match_type"] != "exact_intersect" or identity["parcel_match_distance_m"] != 0:
            raise LiveIntegrationError("Live fetch did not produce an exact, zero-distance parcel match.")
        if snapshot_t1["geometry"].get("type") not in {"Polygon", "MultiPolygon"}:
            raise LiveIntegrationError("Live fetch did not return parcel Polygon/MultiPolygon geometry.")
        report["snapshot_t1"] = {
            "snapshot_id": snapshot_t1["snapshot_id"], "site_id": snapshot_t1.get("site_id"),
            "parcel_id": identity["parcel_id"], "geometry_type": snapshot_t1["geometry"]["type"],
            "raw_response_hash": snapshot_t1["raw_response_hash"], "request_hash": snapshot_t1["request_hash"],
            "evidence_count": len(snapshot_t1["evidence"]),
            "provenance_sources": sorted({str(item.get("source")) for item in snapshot_t1["evidence"].values() if item.get("source")}),
        }

        scene = scene_state_from_snapshot(snapshot_t1)
        proposal = generate_bess_proposal(
            snapshot_t1, scene, power_mw=100, energy_mwh=400, duration_hours=4,
            expansion_power_mw=300, expansion_energy_mwh=1200,
            minimum_setback_m=DEFAULT_MINIMUM_SETBACK_M,
        )
        if proposal["status"] not in {"PLACED", "ADJUSTED"}:
            raise LiveIntegrationError(f"No deterministic 100 MW / 400 MWh BESS proposal could be placed: {proposal.get('reason')}")
        refresh_field, refresh_constraint = _refresh_constraint(snapshot_t1)
        scenario = scenarios.create(
            snapshot_t1, workspace_id=WORKSPACE_ID, user_intent="Live integration scenario.",
            scene_state=proposal["scene_state"],
            requested_constraints=[{"constraint_id": "footprint_inside_parcel"}, refresh_constraint],
            model_id="live-integration-runner",
        )
        context = SandboxToolExecutor(snapshot_t1, SandboxSession(scene_state=proposal["scene_state"])).execute(
            "get_site_context", {"snapshot_id": snapshot_t1["snapshot_id"]},
        )
        report["sandbox"] = {
            "scene_loaded": context["observed_geometry"]["origin"] == "OBSERVED",
            "scenario_id": scenario["scenario_id"], "scenario_status": scenario["evaluation"]["overall_status"],
            "agent_context_evidence_count": len(context["evidence_summary"]), "refresh_constraint": refresh_constraint["constraint_id"],
        }

        batch = None
        if "/v1/fetch/batch" in openapi.get("paths", {}):
            batch_quote = await client.fetch_quote(locations=2, fields=BATCH_FIELDS)
            batch_payload = await client.fetch_batch(
                locations=[{"lat": location["lat"], "lng": location["lng"]}] * 2, fields=BATCH_FIELDS,
            )
            results = _batch_results(batch_payload)
            batch = {
                "requested_fields": BATCH_FIELDS, "location_count": 2,
                "result_count": len(results), "quote_credits": SiteSnapshotService._estimated_credits(batch_quote),
                "parcel_ids": [item.get("parcel_id") or item.get("fields", {}).get("parcel_id", {}).get("value") for item in results],
                "per_location_errors": [item.get("error") for item in results if item.get("error")],
            }
            if len(results) != 2:
                raise LiveIntegrationError("Live batch response did not preserve two-location alignment.")
        report["batch"] = batch

        fresh_before = service.freshness_status(snapshot_t1["snapshot_id"])
        override = {refresh_field: time.time() - 1}
        expired_plan = await service.quote_refresh(snapshot_t1["snapshot_id"], now=0, test_expiry_overrides={refresh_field: -1})
        try:
            await service.confirm_and_refresh(expired_plan["spend_plan_id"], confirmed_by_application=True)
            raise LiveIntegrationError("Expired refresh confirmation was accepted.")
        except ConfirmationRequired:
            pass
        spend_plan = await service.quote_refresh(snapshot_t1["snapshot_id"], test_expiry_overrides=override)
        if not spend_plan.get("confirmation_required", True):
            raise LiveIntegrationError("Refresh plan unexpectedly bypassed application confirmation.")
        refreshed = await service.confirm_and_refresh(spend_plan["spend_plan_id"], confirmed_by_application=True)
        snapshot_t2 = refreshed["snapshot"]
        if service.get_snapshot(snapshot_t1["snapshot_id"])["raw_response_hash"] != snapshot_t1["raw_response_hash"]:
            raise LiveIntegrationError("Refresh mutated immutable T1.")
        report["refresh"] = {
            "test_time_freshness_override": {"field": refresh_field, "local_expiry": override[refresh_field], "provider_ttl_unchanged": True},
            "freshness_before": fresh_before["status"], "spend_plan_id": spend_plan["spend_plan_id"],
            "requested_fields": spend_plan["requested_fields"], "quote_id": spend_plan["quote_id"],
            "quote_expiry": spend_plan["quote_expires_at"], "estimated_credits": spend_plan["expected_credits"],
            "snapshot_t2": snapshot_t2["snapshot_id"], "changed_evidence": refreshed["snapshot_diff"]["changed_evidence_ids"],
            "evaluation_runs": [{"scenario_id": item["scenario_id"], "revision": item["revision"], "status": item["status"]} for item in refreshed["evaluation_runs"]],
        }
        usage_after = await client.usage()
        before_remaining = _credit_value({"credits": usage_before.get("credits", {})})
        after_remaining = _credit_value({"credits": usage_after.get("credits", {})})
        report["credits"] = {
            "usage_before": usage_before, "usage_after": usage_after,
            "actual_credits_charged": before_remaining - after_remaining if before_remaining is not None and after_remaining is not None else None,
        }

        report["live_error_checks"] = {
            "invalid_lookup_status": await _expect_http_error(client._request("POST", "/v1/lookup", json_body={"input": ""})),
            "unavailable_field_quote_status": await _expect_http_error(client.fetch_quote(locations=1, fields=["__not_a_mireye_field__"])),
        }
        report["request_ledger"] = client.request_ledger
        if report_path:
            report_path.write_text(_json(report), encoding="utf-8")
        return report
    finally:
        # Keep the temporary DB only while this explicit live run is executing.
        db_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run opt-in live MIREYE integration checks with a temporary SQLite DB.")
    parser.add_argument("--address", default=DEFAULT_ADDRESS)
    parser.add_argument("--report", type=Path, help="Optional JSON report output path (never contains credentials).")
    args = parser.parse_args()
    try:
        print(_json(asyncio.run(run(args.address, args.report))))
        return 0
    except (LiveIntegrationError, httpx.HTTPError, ValueError) as exc:
        print(f"Live MIREYE integration failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
