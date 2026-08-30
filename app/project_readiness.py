"""Deterministic power and entitlement views over existing project evidence."""
from __future__ import annotations

import hashlib
import json
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode

import httpx

from app.infrastructure.observability import traced_async


AUSTIN_JURISDICTION_URL = "https://services.arcgis.com/0L95CJ0VTaxqcmED/arcgis/rest/services/BOUNDARIES_jurisdictions/FeatureServer/0/query"
AUSTIN_ZONING_URL = "https://services.arcgis.com/0L95CJ0VTaxqcmED/ArcGIS/rest/services/DDB_Phase_1_Web_Layers/FeatureServer/5/query"
TRAVIS_DEVELOPMENT_URL = "https://www.traviscountytx.gov/tnr/environmental-quality/stormwater/professionals/environmental-review-faqs"
READINESS_SCHEMA_VERSION = "project_readiness_v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _stable_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}_{hashlib.sha256(_canonical(parts).encode('utf-8')).hexdigest()[:24]}"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if value:
            self.parts.append(value)


def _html_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    return " ".join(parser.parts)


def _excerpt(text: str, phrase: str, *, radius: int = 280) -> str | None:
    index = text.casefold().find(phrase.casefold())
    if index < 0:
        return None
    return text[max(0, index - radius):min(len(text), index + len(phrase) + radius)].strip()


def _record(
    *, site_id: str, field: str, value: Any, provider: str, dataset: str, source_url: str,
    scope: str, semantic_strength: str, requirement_ids: list[str], retrieved_at: float,
    document_title: str | None = None, document_type: str | None = None,
    section_reference: str | None = None, excerpt: str | None = None,
    jurisdiction: str | None = None, confidence: str = "HIGH", human_review_required: bool = True,
) -> dict:
    payload = {
        "field": field, "value": value, "provider": provider, "dataset": dataset,
        "source_url": source_url, "scope": scope, "semantic_strength": semantic_strength,
        "requirement_ids": requirement_ids, "document_title": document_title,
        "document_type": document_type, "section_reference": section_reference,
        "excerpt": excerpt, "jurisdiction": jurisdiction,
    }
    return {
        "evidence_id": _stable_id("external_evidence", site_id, payload),
        **payload,
        "status": "ok", "source": provider, "unit": None,
        "retrieved_at": retrieved_at, "observed_at": retrieved_at,
        "expires_at": retrieved_at + 86400, "freshness": "CURRENT",
        "confidence": confidence, "human_review_required": human_review_required,
        "source_hash": hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest(),
    }


class AuthoritativeSourceService:
    """Fetch only explicit authoritative sources; failures remain visible."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client

    @traced_async("source.collect")
    async def collect(self, project: dict, snapshot: dict) -> dict:
        site_id = snapshot.get("site_id") or "unknown_site"
        point = snapshot.get("parcel_identity", {}).get("selected_point") or {}
        lat, lng = point.get("lat"), point.get("lng")
        evidence = snapshot.get("evidence", {})
        iso = (evidence.get("iso_rto") or {}).get("value")
        county = str((evidence.get("political_county") or {}).get("value") or "")
        collected_at = time.time()
        records: list[dict] = []
        sources: list[dict] = []
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=httpx.Timeout(25, connect=10), follow_redirects=True)
        try:
            sources.append({
                "provider": str(iso) if iso else "Interconnection authority",
                "dataset": "BESS export / injection interconnection pathway",
                "availability": "UNRESOLVED",
                "reason": "No approved generation or storage interconnection source is configured.",
            })
            if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
                await self._austin_layers(client, site_id, float(lat), float(lng), collected_at, records, sources)
            else:
                sources.append({"provider": "City of Austin", "dataset": "Jurisdiction and zoning GIS", "availability": "UNAVAILABLE", "reason": "The SiteSnapshot has no validated resolution point."})
            if "travis" in county.casefold():
                await self._travis(client, site_id, collected_at, records, sources)
            else:
                sources.append({"provider": "Travis County", "dataset": "Development requirements", "source_url": TRAVIS_DEVELOPMENT_URL, "availability": "NOT_APPLICABLE_OR_UNCONFIRMED", "reason": "MIREYE evidence does not identify Travis County."})
        finally:
            if owns_client:
                await client.aclose()
        return {"site_id": site_id, "collected_at": collected_at, "records": records, "sources": sources}

    async def _austin_layers(self, client: httpx.AsyncClient, site_id: str, lat: float, lng: float, now: float, records: list[dict], sources: list[dict]) -> None:
        params = {
            "geometry": f"{lng},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outFields": "*", "returnGeometry": "false", "f": "json",
        }
        for field, dataset, url, requirements in (
            ("austin_jurisdiction", "City of Austin jurisdiction boundaries", AUSTIN_JURISDICTION_URL, ["industrial_zoning", "energy_storage_entitlement"]),
            ("austin_base_zoning", "City of Austin base zoning districts", AUSTIN_ZONING_URL, ["industrial_zoning", "energy_storage_entitlement"]),
        ):
            query_url = f"{url}?{urlencode(params)}"
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
                features = payload.get("features") or []
                if features:
                    attributes = features[0].get("attributes") or {}
                    records.append(_record(
                        site_id=site_id, field=field, value=attributes,
                        provider="City of Austin", dataset=dataset, source_url=query_url,
                        scope="POINT_IN_POLYGON", semantic_strength="DIRECTLY_VERIFIED",
                        requirement_ids=requirements, retrieved_at=now,
                        document_title=dataset, document_type="official_gis",
                        section_reference="Intersecting feature attributes", jurisdiction="Austin, Texas",
                        human_review_required=True,
                    ))
                    availability, reason = "AVAILABLE", None
                else:
                    availability, reason = "NO_INTERSECTING_FEATURE", "The official layer returned no feature at the validated resolution point; this does not establish a legal conclusion."
                sources.append({"provider": "City of Austin", "dataset": dataset, "source_url": query_url, "availability": availability, "reason": reason, "retrieved_at": now})
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                sources.append({"provider": "City of Austin", "dataset": dataset, "source_url": query_url, "availability": "UNAVAILABLE", "reason": str(exc), "retrieved_at": now})

    async def _travis(self, client: httpx.AsyncClient, site_id: str, now: float, records: list[dict], sources: list[dict]) -> None:
        try:
            response = await client.get(TRAVIS_DEVELOPMENT_URL)
            response.raise_for_status()
            text = _html_text(response.text)
            excerpt = _excerpt(text, "Basic Development Permit is required")
            if not excerpt:
                raise ValueError("The current page did not expose the development-permit applicability text.")
            records.append(_record(
                site_id=site_id, field="travis_county_development_permit_context",
                value={"development_permit_described": True, "site_applicability": "REQUIRES_CONFIRMATION"},
                provider="Travis County", dataset="Environmental Review of Development Proposals",
                source_url=TRAVIS_DEVELOPMENT_URL, scope="COUNTY_RULE_CONTEXT",
                semantic_strength="SOURCE_BACKED_SIGNAL", requirement_ids=["industrial_zoning", "energy_storage_entitlement"], retrieved_at=now,
                document_title="Environmental Review of Development Proposals", document_type="official_guidance",
                section_reference="Geographical areas requiring development authorization", excerpt=excerpt,
                jurisdiction="Travis County, Texas", human_review_required=True,
            ))
            sources.append({"provider": "Travis County", "dataset": "Environmental Review of Development Proposals", "source_url": TRAVIS_DEVELOPMENT_URL, "availability": "AVAILABLE", "retrieved_at": now})
        except (httpx.HTTPError, ValueError) as exc:
            sources.append({"provider": "Travis County", "dataset": "Environmental Review of Development Proposals", "source_url": TRAVIS_DEVELOPMENT_URL, "availability": "UNAVAILABLE", "reason": str(exc), "retrieved_at": now})


def _usable(record: Any, now: float) -> bool:
    if not isinstance(record, dict) or record.get("status") != "ok" or record.get("value") is None:
        return False
    try:
        return float(record.get("expires_at")) > now
    except (TypeError, ValueError):
        return False


def _item(snapshot: dict, key: str, label: str, fields: list[str], *, now: float, limitation: str) -> dict:
    evidence = snapshot.get("evidence", {})
    records = [(field, evidence.get(field)) for field in fields if _usable(evidence.get(field), now)]
    if not records:
        return {"key": key, "label": label, "state": "UNRESOLVED", "value": None, "unit": None, "evidence_ids": [], "explanation": f"No current source-backed evidence is available. {limitation}"}
    field, record = records[0]
    strength = record.get("semantic_strength")
    state = "VERIFIED" if strength == "DIRECTLY_VERIFIED" else "SOURCE_BACKED"
    return {
        "key": key, "label": label, "state": state, "value": record.get("value"), "unit": record.get("unit"),
        "evidence_ids": [name for name, _record_value in records], "explanation": limitation,
        "source": record.get("source"), "freshness": "CURRENT", "scope": record.get("scope"),
    }


def _project_storage_requirements(project: dict) -> dict:
    request = project.get("request", {})
    supplied = request.get("storage_requirements") or {}
    return {
        "phase_1_power_mw": supplied.get("phase_1_power_mw"),
        "phase_1_energy_mwh": supplied.get("phase_1_energy_mwh"),
        "duration_hours": supplied.get("duration_hours"),
        "expansion_power_mw": supplied.get("expansion_power_mw"),
        "expansion_energy_mwh": supplied.get("expansion_energy_mwh"),
        "target_energization_date": supplied.get("target_energization_date"),
    }


def _evidence_details(snapshot: dict, external_records: list[dict], items: list[dict]) -> list[dict]:
    wanted = {evidence_id for item in items for evidence_id in item.get("evidence_ids", [])}
    details = [record for record in external_records if record.get("evidence_id") in wanted]
    for evidence_id in sorted(wanted):
        record = snapshot.get("evidence", {}).get(evidence_id)
        if isinstance(record, dict):
            details.append({
                "evidence_id": evidence_id, "provider": "MIREYE", "dataset": record.get("source"),
                "source_url": record.get("source_url"), "scope": record.get("scope"),
                "semantic_strength": record.get("semantic_strength"), "freshness": "CURRENT" if _usable(record, time.time()) else "STALE",
                "observed_at": record.get("observed_at"), "expires_at": record.get("expires_at"),
                "human_review_required": False, "section_reference": evidence_id,
            })
    return details


def build_power_readiness(project: dict, candidate: dict, snapshot: dict, intelligence: dict, external: dict | None = None, *, now: float | None = None) -> dict:
    evaluated_at = time.time() if now is None else float(now)
    external_records = (external or {}).get("records", [])
    items = [
        _item(snapshot, "serving_utility", "Serving utility", ["electric_utility_service_territory"], now=evaluated_at, limitation="A mapped service territory is a routing signal, not a service commitment."),
        _item(snapshot, "iso_market", "ISO / market", ["iso_rto"], now=evaluated_at, limitation="Market membership does not establish project interconnection status."),
        _item(snapshot, "nearest_transmission", "Nearest transmission", ["nearest_transmission_line_distance_m", "nearest_osm_transmission_line_distance_m"], now=evaluated_at, limitation="Proximity does not prove export or injection interconnection capability."),
        _item(snapshot, "transmission_voltage", "Transmission voltage", ["nearest_transmission_line_voltage_kv", "nearest_osm_transmission_line_voltage_kv"], now=evaluated_at, limitation="Published voltage does not prove available MW."),
        _item(snapshot, "nearest_substation", "Nearest substation", ["nearest_substation_distance_m", "nearest_osm_substation_distance_m"], now=evaluated_at, limitation="A mapped substation does not prove an available interconnection position."),
        _item(snapshot, "substation_voltage", "Substation voltage", ["nearest_substation_max_voltage_kv", "nearest_osm_substation_max_voltage_kv"], now=evaluated_at, limitation="Voltage does not prove export or injection interconnection capability."),
        _item(snapshot, "nearby_generation", "Nearby generation", ["nearest_power_plant_capacity_mw"], now=evaluated_at, limitation="Nearby generation nameplate capacity is contextual and is not available export or injection capacity."),
        _item(snapshot, "queue_context", "Interconnection queue context", ["interconnection_queue_active_capacity_ercot_mw", "interconnection_queue_active_capacity_county_mw"], now=evaluated_at, limitation="Generation/storage queue totals do not establish site-specific export or injection capacity."),
    ]
    items.append({
        "key": "interconnection_pathway", "label": "Interconnection pathway",
        "state": "UNRESOLVED", "value": None, "unit": None, "evidence_ids": [],
        "explanation": "No approved generation or storage interconnection source is configured, and no site-specific application or completed study is evidenced.",
    })
    capacity_fields = ["utility_or_iso_confirmed_export_injection_capacity_mw"]
    capacity = _item(snapshot, "confirmed_capacity", "Confirmed export / injection capability", capacity_fields, now=evaluated_at, limitation="Only explicit utility- or ISO-confirmed evidence can support an export or injection interconnection claim.")
    items.append(capacity)
    requirements = _project_storage_requirements(project)
    for key, label, required in (
        ("phase_1_export_interconnection", "Phase 1 export / injection interconnection", requirements.get("phase_1_power_mw")),
        ("expansion_export_interconnection", "Expansion export / injection interconnection", requirements.get("expansion_power_mw")),
    ):
        proven = capacity["state"] == "VERIFIED" and isinstance(capacity.get("value"), (int, float)) and isinstance(required, (int, float)) and capacity["value"] >= required
        items.append({
            "key": key, "label": label, "state": "VERIFIED" if proven else "UNRESOLVED",
            "value": required, "unit": "MW", "evidence_ids": capacity["evidence_ids"] if proven else [],
            "explanation": "The requested export/injection power is covered by explicit utility- or ISO-confirmed interconnection evidence." if proven else "No current evidence proves export or injection interconnection capability for this project phase.",
        })
    gaps = [item for item in intelligence.get("unresolved_issues", []) if item.get("domain") == "Power" or item.get("requirement_id") == "bess_export_interconnection"]
    actions = [item for item in intelligence.get("recommended_actions", []) if item.get("requirement_id") == "bess_export_interconnection"]
    has_context = any(item["state"] in {"VERIFIED", "SOURCE_BACKED", "PARTIAL"} for item in items)
    readiness = "VERIFIED" if next(item for item in items if item["key"] == "phase_1_export_interconnection")["state"] == "VERIFIED" else "PARTIAL" if has_context else "UNAVAILABLE"
    return {
        "schema_version": READINESS_SCHEMA_VERSION, "project_id": project["project_id"],
        "site_id": candidate.get("site_id"), "site_snapshot_id": snapshot["snapshot_id"],
        "project_requirements": requirements, "items": items, "readiness_state": readiness,
        "critical_blockers": gaps, "unresolved_evidence": sorted({field for gap in gaps for field in gap.get("missing_evidence", [])}),
        "affected_constraints": sorted({item for gap in gaps for item in gap.get("affected_constraints", [])}),
        "affected_scenarios": [item for gap in gaps for item in gap.get("affected_scenarios", [])],
        "next_best_actions": actions, "existing_rfis": [rfi for rfi in project.get("rfis", []) if rfi.get("type") == "BESS_EXPORT_INTERCONNECTION_RFI"],
        "external_evidence": external_records, "evidence_details": _evidence_details(snapshot, external_records, items),
        "source_status": (external or {}).get("sources", []),
        "freshness": "CURRENT" if all(not item.get("stale_evidence") for item in gaps) else "STALE",
        "conflicts": [], "evaluated_at": evaluated_at,
    }


def build_entitlement_state(project: dict, candidate: dict, snapshot: dict, intelligence: dict, external: dict | None = None, *, now: float | None = None) -> dict:
    evaluated_at = time.time() if now is None else float(now)
    external_records = (external or {}).get("records", [])
    jurisdiction = next((item for item in external_records if item.get("field") == "austin_jurisdiction" and _usable(item, evaluated_at)), None)
    external_zoning = next((item for item in external_records if item.get("field") == "austin_base_zoning" and _usable(item, evaluated_at)), None)
    county_path = next((item for item in external_records if item.get("field") == "travis_county_development_permit_context" and _usable(item, evaluated_at)), None)
    zoning = _item(snapshot, "zoning_code", "Raw zoning code", ["parcel_zoning"], now=evaluated_at, limitation="A raw code does not establish a permitted energy-storage use or legal entitlement.")
    if zoning["state"] == "UNRESOLVED" and external_zoning:
        zoning.update(state="SOURCE_BACKED", value=external_zoning["value"], evidence_ids=[external_zoning["evidence_id"]], source="City of Austin", scope="POINT_IN_POLYGON")
    items = [
        {"key": "jurisdiction", "label": "Jurisdiction", "state": "VERIFIED" if jurisdiction else "UNRESOLVED", "value": jurisdiction.get("value") if jurisdiction else None, "evidence_ids": [jurisdiction["evidence_id"]] if jurisdiction else [], "explanation": "Official jurisdiction-layer intersection." if jurisdiction else "The authoritative jurisdiction for entitlement decisions still requires confirmation."},
        zoning,
        {"key": "permitted_use", "label": "Energy-storage permitted use", "state": "UNRESOLVED", "value": None, "evidence_ids": [], "explanation": "No current jurisdiction-specific permitted-use determination is attached."},
        {"key": "conditional_or_special_use", "label": "Conditional / special use", "state": "UNRESOLVED", "value": None, "evidence_ids": [], "explanation": "No authoritative determination identifies whether a conditional or special-use process applies."},
        {"key": "moratorium", "label": "Moratorium", "state": "UNRESOLVED", "value": None, "evidence_ids": [], "explanation": "No bounded authoritative moratorium search result is attached; absence is not established."},
        {"key": "site_plan_path", "label": "Site-plan / development path", "state": "PARTIAL" if county_path else "UNRESOLVED", "value": county_path.get("value") if county_path else None, "evidence_ids": [county_path["evidence_id"]] if county_path else [], "explanation": "County guidance identifies a potential development-permit path, but site applicability requires jurisdictional confirmation." if county_path else "No authoritative site-plan pathway evidence is attached."},
    ]
    dependencies = [
        {"step_id": "jurisdiction_confirmation", "title": "Confirm controlling jurisdiction", "state": "VERIFIED" if jurisdiction else "REQUIRES_CONFIRMATION", "depends_on": [], "evidence_ids": [jurisdiction["evidence_id"]] if jurisdiction else []},
        {"step_id": "zoning_determination", "title": "Obtain current zoning determination", "state": "VERIFIED" if zoning["state"] in {"VERIFIED", "SOURCE_BACKED"} else "REQUIRES_CONFIRMATION", "depends_on": ["jurisdiction_confirmation"], "evidence_ids": zoning["evidence_ids"]},
        {"step_id": "permitted_use_determination", "title": "Confirm energy-storage use classification", "state": "REQUIRES_CONFIRMATION", "depends_on": ["zoning_determination"], "evidence_ids": []},
    ]
    if county_path:
        dependencies.append({"step_id": "development_permit_path", "title": "Confirm county development-permit requirements", "state": "REQUIRES_CONFIRMATION", "depends_on": ["jurisdiction_confirmation"], "evidence_ids": [county_path["evidence_id"]]})
    gaps = [item for item in intelligence.get("unresolved_issues", []) if item.get("domain") == "Entitlement" or item.get("requirement_id") in {"industrial_zoning", "parcel_zoning_code_in"}]
    actions = [item for item in intelligence.get("recommended_actions", []) if item.get("requirement_id") in {"industrial_zoning", "energy_storage_entitlement", "parcel_zoning_code_in"}]
    return {
        "schema_version": READINESS_SCHEMA_VERSION, "project_id": project["project_id"],
        "site_id": candidate.get("site_id"), "site_snapshot_id": snapshot["snapshot_id"],
        "readiness_state": "PARTIAL" if any(item["state"] in {"VERIFIED", "SOURCE_BACKED", "PARTIAL"} for item in items) else "UNAVAILABLE",
        "items": items, "dependency_graph": dependencies, "timeline": {"state": "UNKNOWN", "reason": "No authoritative site-specific approval durations are attached."},
        "critical_blockers": gaps, "next_best_actions": actions,
        "existing_rfis": [rfi for rfi in project.get("rfis", []) if rfi.get("type") == "ENERGY_STORAGE_ENTITLEMENT_RFI"],
        "external_evidence": external_records, "evidence_details": _evidence_details(snapshot, external_records, items),
        "source_status": (external or {}).get("sources", []),
        "human_review_required": True, "legal_advice": False, "evaluated_at": evaluated_at,
    }
