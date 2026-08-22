"""Single-agent orchestration for customer-supplied site candidate lists."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from app.product import compile_request
from app.sandbox import ConfirmationRequired, SandboxError, SiteSnapshotService, scene_state_from_snapshot
from app.sandbox_evaluator import SceneValidationError, evaluate_site
from app.workspace.store import WorkspaceStore


MAX_CANDIDATES = 500
# Live validation on 2026-08-22 returned blank failures for four locations
# despite OpenAPI allowing 25; two-location batches completed reliably.
BATCH_SIZE = 2
QUOTE_TTL_SECONDS = 900
IDENTITY_FIELDS = (
    "parcel_id", "parcel_apn", "parcel_address", "parcel_area_m2",
    "parcel_boundary_geojson", "parcel_data_source", "parcel_match_type",
    "parcel_match_distance_m", "parcel_match_radius_m",
)
CONSTRAINT_FIELDS = {
    "land_size_context": ("parcel_area_m2",),
    "parcel_acreage_range": ("parcel_area_m2",),
    "resolution_point_outside_fema_sfha": ("within_floodplain_polygon", "fema_flood_zone"),
    "parcel_outside_fema_sfha": ("within_floodplain_polygon", "fema_flood_zone"),
    "footprint_outside_fema_sfha": ("within_floodplain_polygon", "fema_flood_zone"),
    "max_nwi_wetland_fraction_of_parcel": ("wetland_fraction_of_parcel",),
    "max_nwi_wetland_acres_on_parcel": ("wetland_acres_on_parcel",),
    "wetland_context": ("wetland_fraction_of_parcel", "wetland_acres_on_parcel"),
    "max_resolution_point_slope_degrees": ("slope_degrees",),
    "max_slope_degrees": ("slope_degrees",),
    "terrain_context": ("elevation", "slope_degrees"),
    "max_resolution_point_substation_distance_m": (
        "nearest_substation_distance_m", "nearest_substation_status", "nearest_substation_max_voltage_kv",
    ),
    "max_resolution_point_transmission_distance_m": (
        "nearest_transmission_line_distance_m", "nearest_transmission_line_status",
        "nearest_transmission_line_voltage_kv",
    ),
    "transmission_proximity": ("nearest_transmission_line_distance_m",),
    "max_resolution_point_major_road_distance_m": ("nearest_major_road_distance_m", "nearest_major_road_name"),
    "legal_access": ("nearest_major_road_distance_m", "nearest_major_road_name"),
    "road_proximity": ("nearest_major_road_distance_m", "nearest_major_road_name"),
    "parcel_zoning_code_in": ("parcel_zoning",),
    "industrial_zoning": ("parcel_zoning",),
    "zoning_context": ("parcel_zoning",),
    "sufficient_grid_capacity": (
        "nearest_substation_distance_m", "nearest_substation_status", "nearest_substation_max_voltage_kv",
        "nearest_transmission_line_distance_m", "nearest_transmission_line_voltage_kv",
    ),
}
SUPPORTED_CONSTRAINTS = {
    "parcel_acreage_range", "resolution_point_outside_fema_sfha",
    "max_nwi_wetland_fraction_of_parcel", "max_nwi_wetland_acres_on_parcel",
    "max_resolution_point_slope_degrees", "max_resolution_point_substation_distance_m",
    "max_resolution_point_transmission_distance_m", "max_resolution_point_major_road_distance_m",
    "parcel_zoning_code_in",
}


class DiligenceError(ValueError):
    pass


class CandidateProvider(Protocol):
    def enumerate(self, inputs: list[Any], *, cursor: str | None = None, limit: int = 25) -> dict: ...


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _candidate_id(raw: Any, ordinal: int) -> str:
    digest = hashlib.sha256(f"{ordinal}:{_canonical(raw)}".encode("utf-8")).hexdigest()[:20]
    return f"candidate_{digest}"


class UserSuppliedCandidateProvider:
    """Enumerate only candidates explicitly supplied by the user."""

    def enumerate(self, inputs: list[Any], *, cursor: str | None = None, limit: int = 25) -> dict:
        if not isinstance(inputs, list) or not inputs:
            raise DiligenceError("Provide at least one candidate address, coordinate, or APN.")
        if len(inputs) > MAX_CANDIDATES:
            raise DiligenceError(f"A candidate list may contain at most {MAX_CANDIDATES} entries.")
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise DiligenceError("Candidate page limit must be from 1 to 100.")
        try:
            start = int(cursor or 0)
        except (TypeError, ValueError) as exc:
            raise DiligenceError("Candidate cursor is invalid.") from exc
        records = [self._record(raw, ordinal) for ordinal, raw in enumerate(inputs)]
        page = records[start:start + limit]
        next_cursor = str(start + limit) if start + limit < len(records) else None
        return {"items": page, "next_cursor": next_cursor, "total": len(records), "source": "user_supplied"}

    @staticmethod
    def _record(raw: Any, ordinal: int) -> dict:
        record = {
            "candidate_id": _candidate_id(raw, ordinal), "raw_input": copy.deepcopy(raw),
            "input_type": None, "address": None, "coordinate": None, "apn": None,
            "source": "user_supplied", "source_metadata": {"ordinal": ordinal},
            "reconciliation_status": "PENDING", "resolution_options": [], "selected_location": None,
            "snapshot_id": None, "site_id": None, "evaluation": None, "error": None,
        }
        if isinstance(raw, dict):
            record["source"] = str(raw.get("source") or "user_supplied")
            record["source_metadata"].update(copy.deepcopy(raw.get("source_metadata") or {}))
            if raw.get("lat") is not None or raw.get("lng") is not None:
                if raw.get("lat") is None or raw.get("lng") is None:
                    raise DiligenceError("Candidate coordinates require both lat and lng.")
                record["input_type"] = "coordinate"
                record["coordinate"] = {"lat": float(raw["lat"]), "lng": float(raw["lng"])}
            elif raw.get("apn"):
                record["input_type"], record["apn"] = "apn", str(raw["apn"]).strip()
            elif raw.get("address"):
                record["input_type"], record["address"] = "address", str(raw["address"]).strip()
            else:
                raise DiligenceError("Candidate objects require address, APN, or lat/lng.")
        elif isinstance(raw, str) and raw.strip():
            value = raw.strip()
            coordinate = re.fullmatch(r"\s*(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*", value)
            if coordinate:
                record["input_type"] = "coordinate"
                record["coordinate"] = {"lat": float(coordinate.group(1)), "lng": float(coordinate.group(2))}
            elif re.match(r"^apn\s*[:#]", value, re.IGNORECASE):
                record["input_type"], record["apn"] = "apn", re.sub(r"^apn\s*[:#]\s*", "", value, flags=re.IGNORECASE)
            elif re.match(r"^https?://", value, re.IGNORECASE):
                record["input_type"], record["reconciliation_status"] = "url", "UNSUPPORTED"
                record["error"] = "Candidate URLs are not parsed in this MVP; provide an address, coordinate, or APN."
            else:
                record["input_type"], record["address"] = "address", value
        else:
            raise DiligenceError("Each candidate must be an address, coordinate, APN, or typed object.")
        coordinate = record.get("coordinate")
        if coordinate and not (-90 <= coordinate["lat"] <= 90 and -180 <= coordinate["lng"] <= 180):
            raise DiligenceError("Candidate coordinates are outside valid latitude/longitude bounds.")
        return record


def compile_project_request(message: str) -> dict:
    compiled = compile_request(message)
    text = compiled["message"]
    lower = text.lower()
    constraints = copy.deepcopy(compiled["constraints"])
    if "road proximity" in lower and "legal access" not in lower:
        constraints = [
            {"constraint_id": "road_proximity"}
            if item["constraint_id"] == "legal_access" else item
            for item in constraints
        ]
    if "resolution point" in lower or "point-scoped" in lower:
        constraints = [
            {"constraint_id": "resolution_point_outside_fema_sfha"}
            if item["constraint_id"] == "parcel_outside_fema_sfha" else item
            for item in constraints
        ]
    point_slope = re.search(r"(?:resolution[- ]point|point[- ]scoped)\s+slope\s*(?:<|under|below|max(?:imum)?)?\s*(\d+(?:\.\d+)?)", lower)
    if point_slope:
        constraints.append({"constraint_id": "max_resolution_point_slope_degrees", "max_degrees": float(point_slope.group(1))})
    elif "slope" in lower:
        constraints.append({"constraint_id": "max_slope_degrees"})
    zoning_codes = re.search(r"(?:raw\s+)?zoning\s+codes?\s*(?:are|:|=|in)?\s*([a-z0-9-]+(?:\s*(?:,|or)\s*[a-z0-9-]+)+)", lower)
    if zoning_codes:
        allowed = [value.strip().upper() for value in re.split(r"\s*(?:,|or)\s*", zoning_codes.group(1)) if value.strip() and value.strip().casefold() not in {"and", "with", "for"}]
        constraints = [item for item in constraints if item["constraint_id"] != "industrial_zoning"]
        constraints.append({"constraint_id": "parcel_zoning_code_in", "allowed_codes": allowed})
    requested_context = (
        ("land_size_context", "land size" in lower or "acreage" in lower, {"parcel_acreage_range"}),
        ("wetland_context", "wetland" in lower, {"max_nwi_wetland_fraction_of_parcel", "max_nwi_wetland_acres_on_parcel"}),
        ("terrain_context", "terrain" in lower, {"max_resolution_point_slope_degrees", "max_slope_degrees"}),
        ("zoning_context", "zoning" in lower, {"parcel_zoning_code_in", "industrial_zoning"}),
    )
    existing_ids = {item["constraint_id"] for item in constraints}
    for constraint_id, requested, specific_ids in requested_context:
        if requested and existing_ids.isdisjoint(specific_ids):
            constraints.append({"constraint_id": constraint_id})
    deduped = []
    for item in constraints:
        if item not in deduped:
            deduped.append(item)
    return {
        **compiled,
        "constraints": deduped,
        "supported_constraints": [item for item in deduped if item["constraint_id"] in SUPPORTED_CONSTRAINTS],
        "unresolved_constraints": [item for item in deduped if item["constraint_id"] not in SUPPORTED_CONSTRAINTS],
        "compiler_version": "diligence_constraints_v1",
    }


class DiligenceService:
    def __init__(self, store: WorkspaceStore, sandbox: SiteSnapshotService, worlds: Any | None = None, provider: CandidateProvider | None = None):
        self.store, self.sandbox, self.worlds = store, sandbox, worlds
        self.provider = provider or UserSuppliedCandidateProvider()

    def create_project(self, *, workspace_id: str, message: str, candidates: list[Any]) -> dict:
        if not isinstance(message, str) or not message.strip():
            raise DiligenceError("Project request must not be empty.")
        page = self.provider.enumerate(candidates, limit=min(MAX_CANDIDATES, len(candidates)))
        now = time.time()
        project = {
            "project_id": f"project_{uuid.uuid4().hex}", "workspace_id": workspace_id,
            "status": "CANDIDATES_SUPPLIED", "request": compile_project_request(message),
            "candidates": page["items"], "candidate_count": page["total"], "requested_fields": [],
            "spend_plan": None, "ranking": [], "watch": {"enabled": False, "last_checked_at": None, "candidate_states": []},
            "created_at": now, "updated_at": now,
        }
        self.store.create_workspace(workspace_id, "Site diligence")
        return self._save(project)

    def get(self, project_id: str) -> dict:
        project = self.store.get_diligence_project(project_id)
        if project is None:
            raise DiligenceError("Diligence project was not found.")
        return project

    def candidate_page(self, project_id: str, *, cursor: str | None = None, limit: int = 25) -> dict:
        project = self.get(project_id)
        try:
            start = int(cursor or 0)
        except (TypeError, ValueError) as exc:
            raise DiligenceError("Candidate cursor is invalid.") from exc
        if not 1 <= limit <= 100:
            raise DiligenceError("Candidate page limit must be from 1 to 100.")
        items = project["candidates"][start:start + limit]
        return {"items": items, "total": len(project["candidates"]), "next_cursor": str(start + limit) if start + limit < len(project["candidates"]) else None}

    @staticmethod
    def discovery_capabilities() -> dict:
        return {
            "provider": "user_supplied", "supported_inputs": ["address", "coordinate", "apn"],
            "statewide_inverse_search": False, "synthetic_screen_used": False,
            "message": "Candidates must be supplied by the customer; MIREYE resolves and enriches them.",
        }

    def plan_fields(self, project_id: str) -> dict:
        project = self.get(project_id)
        fields = list(IDENTITY_FIELDS)
        for constraint in project["request"]["constraints"]:
            fields.extend(CONSTRAINT_FIELDS.get(constraint["constraint_id"], ()))
        project["requested_fields"] = list(dict.fromkeys(fields))
        self._save(project)
        return {"project_id": project_id, "fields": project["requested_fields"], "field_count": len(project["requested_fields"]), "constraints": project["request"]["constraints"]}

    async def resolve_and_quote(self, project_id: str, *, confirmed_resolution: bool) -> dict:
        if not confirmed_resolution:
            raise ConfirmationRequired("Candidate resolution requires explicit application confirmation.")
        project = self.get(project_id)
        fields = self.plan_fields(project_id)["fields"]
        for candidate in project["candidates"]:
            if candidate["reconciliation_status"] == "ENRICHMENT_FAILED" and candidate.get("selected_location"):
                candidate.update(reconciliation_status="RESOLVED", error=None)
                continue
            if candidate["reconciliation_status"] in {"UNSUPPORTED", "AMBIGUOUS", "RESOLVED", "ENRICHED"}:
                continue
            try:
                if candidate["input_type"] == "coordinate":
                    resolved = await self.sandbox.resolve(**candidate["coordinate"])
                else:
                    value = candidate["apn"] if candidate["input_type"] == "apn" else candidate["address"]
                    resolved = await self.sandbox.resolve(input=value, kind=candidate["input_type"])
                if resolved["status"] == "resolved":
                    candidate["selected_location"] = resolved["candidates"][0]
                    candidate["reconciliation_status"] = "RESOLVED"
                elif resolved["status"] == "ambiguous":
                    candidate["resolution_options"] = resolved["candidates"]
                    candidate["reconciliation_status"] = "AMBIGUOUS"
                else:
                    candidate["reconciliation_status"], candidate["error"] = "NOT_FOUND", "MIREYE could not resolve this candidate."
            except Exception as exc:
                candidate["reconciliation_status"], candidate["error"] = "ERROR", str(exc)
        resolved_candidates = [item for item in project["candidates"] if item["reconciliation_status"] == "RESOLVED"]
        if not resolved_candidates:
            project["status"] = "RESOLUTION_REQUIRED"
            return self._save(project)
        selected_fields, catalog = await self.sandbox.select_fields(fields)
        provider_quotes, estimates = [], []
        for offset in range(0, len(resolved_candidates), BATCH_SIZE):
            count = len(resolved_candidates[offset:offset + BATCH_SIZE])
            quote = await self.sandbox.client.fetch_quote(locations=count, fields=selected_fields)
            provider_quotes.append({"location_count": count, "quote": quote})
            credits = self.sandbox._estimated_credits(quote)
            if isinstance(credits, (int, float)):
                estimates.append(float(credits))
        now = time.time()
        quote_expiries = [self._quote_expiry(item["quote"], now + QUOTE_TTL_SECONDS) for item in provider_quotes]
        plan = {
            "spend_plan_id": f"spend_{uuid.uuid4().hex}", "project_id": project_id,
            "status": "QUOTED", "requested_fields": selected_fields,
            "candidate_count": len(resolved_candidates), "batch_strategy": {"max_batch_size": BATCH_SIZE, "batch_count": len(provider_quotes)},
            "provider_quotes": provider_quotes, "expected_credits": sum(estimates) if len(estimates) == len(provider_quotes) else None,
            "provider_quote_ids": [item["quote"].get("quote_id") or item["quote"].get("id") for item in provider_quotes],
            "field_catalog_version": self.sandbox._catalog_version(catalog),
            "quote_expires_at": min(quote_expiries),
            "cache_hits": {"candidate_count": 0, "field_count": 0},
            "freshness_reason": "initial_candidate_enrichment",
            "workspace_budget_impact": {"policy": "explicit_application_confirmation_required", "estimated_credits": sum(estimates) if len(estimates) == len(provider_quotes) else None},
            "confirmation_required": True, "created_at": now,
        }
        project.update(status="AWAITING_ENRICHMENT_APPROVAL", requested_fields=selected_fields, spend_plan=plan)
        return self._save(project)

    async def resolve_candidate(self, project_id: str, candidate_id: str, *, confirmed_resolution: bool) -> dict:
        if not confirmed_resolution:
            raise ConfirmationRequired("Candidate resolution requires explicit application confirmation.")
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        if candidate["reconciliation_status"] == "UNSUPPORTED":
            return copy.deepcopy(candidate)
        if candidate["input_type"] == "coordinate":
            resolved = await self.sandbox.resolve(**candidate["coordinate"])
        else:
            value = candidate["apn"] if candidate["input_type"] == "apn" else candidate["address"]
            resolved = await self.sandbox.resolve(input=value, kind=candidate["input_type"])
        if resolved["status"] == "resolved":
            candidate.update(selected_location=resolved["candidates"][0], reconciliation_status="RESOLVED", error=None)
        elif resolved["status"] == "ambiguous":
            candidate.update(resolution_options=resolved["candidates"], reconciliation_status="AMBIGUOUS")
        else:
            candidate.update(reconciliation_status="NOT_FOUND", error="MIREYE could not resolve this candidate.")
        self._save(project)
        return copy.deepcopy(candidate)

    async def select_resolution(self, project_id: str, candidate_id: str, option_index: int) -> dict:
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        options = candidate.get("resolution_options") or []
        if candidate.get("reconciliation_status") != "AMBIGUOUS" or not 0 <= option_index < len(options):
            raise DiligenceError("That candidate resolution option is unavailable.")
        candidate.update(selected_location=options[option_index], reconciliation_status="RESOLVED", error=None)
        project["spend_plan"] = None
        project["status"] = "CANDIDATES_RESOLVED"
        self._save(project)
        return await self.resolve_and_quote(project_id, confirmed_resolution=True)

    async def confirm_and_fetch(self, project_id: str, spend_plan_id: str, *, confirmed: bool) -> dict:
        if not confirmed:
            raise ConfirmationRequired("MIREYE enrichment requires explicit application confirmation.")
        project = self.get(project_id)
        plan = project.get("spend_plan") or {}
        if plan.get("spend_plan_id") != spend_plan_id or plan.get("status") != "QUOTED":
            raise DiligenceError("MIREYE enrichment spend plan is unavailable or already used.")
        if time.time() >= float(plan["quote_expires_at"]):
            raise ConfirmationRequired("MIREYE enrichment quote has expired; prepare a new spend plan.")
        fields, catalog = await self.sandbox.select_fields(plan["requested_fields"])
        resolved = [item for item in project["candidates"] if item["reconciliation_status"] == "RESOLVED"]
        for offset in range(0, len(resolved), BATCH_SIZE):
            batch = resolved[offset:offset + BATCH_SIZE]
            locations = [{"lat": item["selected_location"]["lat"], "lng": item["selected_location"]["lng"]} for item in batch]
            try:
                payload = await self.sandbox.client.fetch_batch(locations=locations, fields=fields)
                results = self._batch_results(payload)
            except Exception as exc:
                results = [{"ok": False, "error": {"message": str(exc)}} for _ in batch]
            for index, candidate in enumerate(batch):
                result = results[index] if index < len(results) else {"ok": False, "error": {"message": "MIREYE batch response omitted this candidate."}}
                try:
                    snapshot = self.sandbox.persist_dossier(
                        workspace_id=project["workspace_id"], lat=locations[index]["lat"], lng=locations[index]["lng"],
                        fields=fields, catalog=catalog, quote=plan, dossier=result,
                    )
                    scene = scene_state_from_snapshot(snapshot)
                    constraints = project["request"]["constraints"] or [{"constraint_id": "footprint_inside_parcel"}]
                    evaluation = evaluate_site(snapshot, scene, constraints)
                    evidence = snapshot.get("evidence", {})
                    value = lambda field: (evidence.get(field) or {}).get("value")
                    world = self.worlds.latest_for_site_snapshot(snapshot["snapshot_id"]) if self.worlds else None
                    world_query = f"?world={world['world_snapshot_id']}" if world else ""
                    candidate.update(
                        reconciliation_status="ENRICHED", snapshot_id=snapshot["snapshot_id"], site_id=snapshot.get("site_id"),
                        evaluation=evaluation, error=None,
                        summary={
                            "title": snapshot["parcel_identity"].get("parcel_address") or candidate.get("address") or "Verified property",
                            "area_acres": round(float(value("parcel_area_m2")) / 4046.8564224, 2) if isinstance(value("parcel_area_m2"), (int, float)) else None,
                            "transmission_distance_m": value("nearest_transmission_line_distance_m"),
                            "road_distance_m": value("nearest_major_road_distance_m"),
                            "zoning_code": value("parcel_zoning"),
                            "sandbox_url": f"/sandbox/{snapshot['snapshot_id']}{world_query}",
                        },
                    )
                except (SandboxError, SceneValidationError, KeyError, TypeError, ValueError) as exc:
                    candidate.update(reconciliation_status="ENRICHMENT_FAILED", error=str(exc), evaluation=None)
        plan["status"] = "COMPLETED"
        plan["completed_at"] = time.time()
        project["status"] = "EVALUATED"
        project["ranking"] = self._rank(project["candidates"])
        return self._save(project)

    def rank_candidates(self, project_id: str) -> dict:
        project = self.get(project_id)
        project["ranking"] = self._rank(project["candidates"])
        self._save(project)
        return {"project_id": project_id, "ranking": project["ranking"], "ranking_version": "deterministic_outcome_order_v1"}

    def compare_candidates(self, project_id: str, candidate_ids: list[str]) -> dict:
        project = self.get(project_id)
        if not isinstance(candidate_ids, list) or len(candidate_ids) < 2:
            raise DiligenceError("Compare requires at least two candidate IDs.")
        comparisons = []
        for candidate_id in candidate_ids:
            candidate = self._candidate(project, candidate_id)
            results = (candidate.get("evaluation") or {}).get("constraint_results", [])
            comparisons.append({
                "candidate_id": candidate_id,
                "overall_status": (candidate.get("evaluation") or {}).get("overall_status", "UNRESOLVED"),
                "constraints": {item["constraint_id"]: {"outcome": item["outcome"], "result": item.get("result"), "units": item.get("units"), "evidence_ids": item.get("evidence_ids", [])} for item in results},
            })
        return {"project_id": project_id, "comparison_version": "candidate_evidence_comparison_v1", "candidates": comparisons}

    def open_candidate(self, project_id: str, candidate_id: str) -> dict:
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        if not candidate.get("snapshot_id"):
            raise DiligenceError("Candidate must be enriched before opening the sandbox.")
        world = self.worlds.latest_for_site_snapshot(candidate["snapshot_id"]) if self.worlds else None
        world_id = world.get("world_snapshot_id") if world else None
        query = f"?world={world_id}" if world_id else ""
        return {"candidate_id": candidate_id, "site_id": candidate.get("site_id"), "site_snapshot_id": candidate["snapshot_id"], "world_snapshot_id": world_id, "sandbox_url": f"/sandbox/{candidate['snapshot_id']}{query}"}

    async def build_world_snapshot(self, project_id: str, candidate_id: str) -> dict:
        if self.worlds is None:
            raise DiligenceError("WorldSnapshot support is unavailable.")
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        if not candidate.get("snapshot_id"):
            raise DiligenceError("Candidate must be enriched before building its world.")
        existing = self.worlds.latest_for_site_snapshot(candidate["snapshot_id"])
        world = existing or await self.worlds.create(site_snapshot_id=candidate["snapshot_id"], requested_layers=["terrain", "roads"])
        return {"candidate_id": candidate_id, "world_snapshot_id": world["world_snapshot_id"], "reused": existing is not None}

    def set_watch(self, project_id: str, *, enabled: bool) -> dict:
        project = self.get(project_id)
        project["watch"]["enabled"] = bool(enabled)
        self._save(project)
        return project["watch"]

    def check_now(self, project_id: str) -> dict:
        project = self.get(project_id)
        states = []
        for candidate in project["candidates"]:
            if candidate.get("snapshot_id"):
                freshness = self.sandbox.freshness_status(candidate["snapshot_id"], fields=project["requested_fields"])
                states.append({"candidate_id": candidate["candidate_id"], "snapshot_id": candidate["snapshot_id"], "status": freshness["status"], "refresh_fields": freshness["refresh_fields"]})
        project["watch"].update(last_checked_at=time.time(), candidate_states=states)
        self._save(project)
        return copy.deepcopy(project["watch"])

    async def quote_candidate_refresh(self, project_id: str, candidate_id: str) -> dict:
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        if not candidate.get("snapshot_id"):
            raise DiligenceError("Candidate has no SiteSnapshot to refresh.")
        return await self.sandbox.quote_refresh(candidate["snapshot_id"], fields=project["requested_fields"])

    async def confirm_candidate_refresh(self, project_id: str, candidate_id: str, spend_plan_id: str, *, confirmed: bool) -> dict:
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        result = await self.sandbox.confirm_and_refresh(spend_plan_id, confirmed_by_application=confirmed)
        snapshot = result["snapshot"]
        scene = scene_state_from_snapshot(snapshot)
        constraints = project["request"]["constraints"] or [{"constraint_id": "footprint_inside_parcel"}]
        candidate.update(snapshot_id=snapshot["snapshot_id"], site_id=snapshot.get("site_id"), evaluation=evaluate_site(snapshot, scene, constraints))
        project["ranking"] = self._rank(project["candidates"])
        self._save(project)
        return {"project": project, "refresh": result}

    def get_evidence(self, project_id: str, candidate_id: str, evidence_ids: list[str] | None = None) -> dict:
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        snapshot = self.sandbox.get_snapshot(candidate.get("snapshot_id", ""))
        if snapshot is None:
            raise DiligenceError("Candidate has no available SiteSnapshot evidence.")
        selected = evidence_ids or list(snapshot.get("evidence", {}))
        return {"candidate_id": candidate_id, "snapshot_id": snapshot["snapshot_id"], "evidence": {key: copy.deepcopy(snapshot["evidence"][key]) for key in selected if key in snapshot["evidence"]}}

    async def ask_mireye_site(self, project_id: str, candidate_id: str, question: str, *, confirmed_candidate_id: str | None) -> dict:
        if candidate_id != confirmed_candidate_id:
            raise ConfirmationRequired("The application has not confirmed this MIREYE site question.")
        project = self.get(project_id)
        candidate = self._candidate(project, candidate_id)
        location = candidate.get("selected_location")
        if not location:
            raise DiligenceError("Candidate must be resolved before asking MIREYE.")
        response = await self.sandbox.client.ask(lat=location["lat"], lng=location["lng"], question=question, include_trace=True)
        project.setdefault("site_questions", []).append({"candidate_id": candidate_id, "question": question, "response": response, "created_at": time.time()})
        self._save(project)
        return response

    @staticmethod
    def _rank(candidates: list[dict]) -> list[dict]:
        ranked = []
        for candidate in candidates:
            results = (candidate.get("evaluation") or {}).get("constraint_results", [])
            counts = {outcome: sum(item.get("outcome") == outcome for item in results) for outcome in ("PASS", "FAIL", "UNRESOLVED")}
            ranked.append({
                "candidate_id": candidate["candidate_id"], "snapshot_id": candidate.get("snapshot_id"),
                "status": candidate["reconciliation_status"], "overall_status": (candidate.get("evaluation") or {}).get("overall_status", "UNRESOLVED"),
                "outcome_counts": counts, "constraint_results": results, "error": candidate.get("error"),
            })
        ranked.sort(key=lambda item: (
            0 if item["status"] == "ENRICHED" else 1,
            item["outcome_counts"]["FAIL"], item["outcome_counts"]["UNRESOLVED"],
            -item["outcome_counts"]["PASS"], item["candidate_id"],
        ))
        for position, item in enumerate(ranked, 1):
            item["rank"] = position
        return ranked

    @staticmethod
    def _batch_results(payload: dict) -> list[dict]:
        for key in ("results", "items", "locations"):
            if isinstance(payload.get(key), list):
                return [item if isinstance(item, dict) else {"ok": False, "error": {"message": "Invalid MIREYE batch item."}} for item in payload[key]]
        return []

    @staticmethod
    def _quote_expiry(quote: dict, fallback: float) -> float:
        value = quote.get("expires_at") or quote.get("quote_expires_at")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
            except ValueError:
                pass
        return fallback

    @staticmethod
    def _candidate(project: dict, candidate_id: str) -> dict:
        for candidate in project["candidates"]:
            if candidate["candidate_id"] == candidate_id:
                return candidate
        raise DiligenceError("Candidate was not found in this project.")

    def _save(self, project: dict) -> dict:
        project["updated_at"] = time.time()
        self.store.save_diligence_project(project)
        return copy.deepcopy(project)
