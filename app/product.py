"""User-facing orchestration for the existing site sandbox services."""
from __future__ import annotations

import re
import uuid
from typing import Any

from app.sandbox_evaluator import evaluate_site


class ProductRequestError(ValueError):
    pass


_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "district of columbia",
}


def compile_request(message: str) -> dict[str, Any]:
    """Extract only semantics the current product can represent without guessing."""
    text = " ".join(message.strip().split())
    lower = text.lower()
    capacity = _number_before(lower, "mw")
    acreage = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*acres?", lower)
    region = next((name.title() for name in _STATES if re.search(rf"\b{re.escape(name)}\b", lower)), None)
    project = next((label for key, label in (
        ("data center", "Data center"), ("solar", "Solar project"),
        ("industrial", "Industrial site"), ("diligence", "Property diligence"),
    ) if key in lower), "Site analysis")

    understanding = []
    if capacity is not None:
        understanding.append(f"{capacity:g} MW {project.lower()}")
    else:
        understanding.append(project)
    if region:
        understanding.append(region)
    if acreage:
        understanding.append(f"{float(acreage.group(1)):g}-{float(acreage.group(2)):g} acres")

    constraints: list[dict[str, Any]] = []
    labels: dict[str, str] = {}
    if acreage:
        constraints.append({
            "constraint_id": "parcel_acreage_range",
            "min_acres": float(acreage.group(1)), "max_acres": float(acreage.group(2)),
        })
        labels["parcel_acreage_range"] = "Parcel area"
    if "flood" in lower:
        constraints.append({"constraint_id": "parcel_outside_fema_sfha"})
        labels["parcel_outside_fema_sfha"] = "Flood"
        understanding.append("Flood requirement")
    transmission = _distance_threshold(lower, "transmission")
    if "transmission" in lower:
        constraint = ({"constraint_id": "max_resolution_point_transmission_distance_m", "max_distance_m": transmission}
                      if transmission is not None else {"constraint_id": "transmission_proximity"})
        constraints.append(constraint)
        labels[constraint["constraint_id"]] = "Transmission"
        understanding.append("Transmission proximity")
    road = _distance_threshold(lower, "road")
    if "road" in lower or "access" in lower:
        constraint = ({"constraint_id": "max_resolution_point_major_road_distance_m", "max_distance_m": road}
                      if road is not None and "access" not in lower else {"constraint_id": "legal_access"})
        constraints.append(constraint)
        labels[constraint["constraint_id"]] = "Road access"
        understanding.append("Road requirement")
    if "industrial zoning" in lower or "industrial-zoned" in lower:
        constraints.append({"constraint_id": "industrial_zoning"})
        labels["industrial_zoning"] = "Zoning"
        understanding.append("Industrial zoning")
    if "grid capacity" in lower or "sufficient grid" in lower:
        constraints.append({"constraint_id": "sufficient_grid_capacity"})
        labels["sufficient_grid_capacity"] = "Grid capacity"
        understanding.append("Grid capacity")

    return {
        "message": text,
        "project": project,
        "capacity_mw": capacity,
        "acreage": [float(acreage.group(1)), float(acreage.group(2))] if acreage else None,
        "region": region,
        "requires_discovery": bool(re.search(r"\b(find|compare)\b", lower) or "land for" in lower),
        "location": _location(text),
        "understanding": list(dict.fromkeys(understanding)),
        "constraints": constraints,
        "constraint_labels": labels,
    }


class ProductExperienceService:
    """Small process-local coordinator; durable state begins at SiteSnapshot creation."""

    def __init__(self, sandbox: Any, worlds: Any):
        self.sandbox = sandbox
        self.worlds = worlds
        self._requests: dict[str, dict[str, Any]] = {}

    async def start(self, message: str) -> dict:
        compiled = compile_request(message)
        request_id = uuid.uuid4().hex
        state = {"request_id": request_id, "compiled": compiled}
        self._requests[request_id] = state
        stages = self._stages("complete", "pending", "pending", "pending")
        if not compiled["location"]:
            discovery_unavailable = compiled["requires_discovery"]
            stages[1]["status"] = "unavailable" if discovery_unavailable else "needs_input"
            return {
                "request_id": request_id,
                "status": "DISCOVERY_UNAVAILABLE" if discovery_unavailable else "CLARIFICATION_REQUIRED",
                "understanding": compiled["understanding"], "stages": stages,
                "message": (
                    f"I can analyze a specific property, but I don't currently have a parcel source that can search all of {compiled['region']}."
                    if compiled["region"] else "I can analyze a specific property, but I don't currently have a parcel source for broad parcel search."
                    if discovery_unavailable else "Which property should I analyze? Send an address or latitude and longitude."
                ),
            }
        if getattr(self.sandbox.client, "mode", None) != "live":
            stages[1]["status"] = "unavailable"
            return {
                "request_id": request_id, "status": "MIREYE_UNAVAILABLE",
                "understanding": compiled["understanding"], "stages": stages,
                "message": "MIREYE site intelligence is not configured. Add a MIREYE API key to analyze this property.",
            }
        location = compiled["location"]
        resolved = await self.sandbox.resolve(**location)
        if resolved["status"] == "ambiguous":
            state["candidates"] = resolved["candidates"]
            stages[1]["status"] = "needs_input"
            return {
                "request_id": request_id, "status": "CLARIFICATION_REQUIRED",
                "understanding": compiled["understanding"], "stages": stages,
                "message": "I found more than one matching property. Which one should I use?",
                "choices": [self._choice(index, item) for index, item in enumerate(resolved["candidates"])],
            }
        if resolved["status"] != "resolved":
            stages[1]["status"] = "unavailable"
            return {"request_id": request_id, "status": "NOT_FOUND", "understanding": compiled["understanding"], "stages": stages, "message": "I couldn't resolve that property."}
        return await self._quote(state, resolved["candidates"][0])

    async def select(self, request_id: str, candidate_index: int) -> dict:
        state = self._state(request_id)
        candidates = state.get("candidates") or []
        if candidate_index < 0 or candidate_index >= len(candidates):
            raise ProductRequestError("That property choice is no longer available.")
        return await self._quote(state, candidates[candidate_index])

    async def confirm(self, request_id: str, confirmed: bool) -> dict:
        state = self._state(request_id)
        if not confirmed:
            raise ProductRequestError("MIREYE analysis requires explicit confirmation.")
        location = state.get("selected_location")
        if not location:
            raise ProductRequestError("Resolve and quote a property before confirming analysis.")
        workspace_id = f"site-{request_id}"
        snapshot = await self.sandbox.create_snapshot(
            workspace_id=workspace_id, lat=location["lat"], lng=location["lng"], confirmed=True,
        )
        scene = self.sandbox.scene_state(snapshot["snapshot_id"])
        constraints = state["compiled"]["constraints"]
        evaluation = evaluate_site(snapshot, scene, constraints) if constraints else None
        world = self.worlds.latest_for_site_snapshot(snapshot["snapshot_id"])
        world_id = world.get("world_snapshot_id") if world else None
        candidate = self._candidate(snapshot, evaluation, state["compiled"], world_id)
        state["snapshot_id"] = snapshot["snapshot_id"]
        return {
            "request_id": request_id, "status": "COMPLETE",
            "understanding": state["compiled"]["understanding"],
            "stages": self._stages("complete", "complete", "complete", "complete"),
            "message": "MIREYE verified the property and the available constraints have been evaluated.",
            "candidates": [candidate],
        }

    async def _quote(self, state: dict, location: dict) -> dict:
        selected = {"lat": float(location["lat"]), "lng": float(location["lng"])}
        state["selected_location"] = selected
        quote = await self.sandbox.quote(**selected)
        state["quote"] = quote
        return {
            "request_id": state["request_id"], "status": "CONFIRMATION_REQUIRED",
            "understanding": state["compiled"]["understanding"],
            "stages": self._stages("complete", "complete", "confirmation_required", "pending"),
            "message": "The property is resolved. Confirm before MIREYE retrieves its site intelligence.",
            "confirmation": {
                "field_count": len(quote["fields"]),
                "estimated_credits": self.sandbox._estimated_credits(quote["quote"]),
                "location_label": location.get("address") or location.get("label") or "Selected property",
            },
        }

    def _state(self, request_id: str) -> dict:
        try:
            return self._requests[request_id]
        except KeyError as exc:
            raise ProductRequestError("This request has expired. Start a new search.") from exc

    @staticmethod
    def _choice(index: int, candidate: dict) -> dict:
        return {
            "index": index,
            "label": candidate.get("address") or candidate.get("label") or f"Property near {candidate['lat']:.5f}, {candidate['lng']:.5f}",
        }

    @staticmethod
    def _candidate(snapshot: dict, evaluation: dict | None, compiled: dict, world_id: str | None) -> dict:
        evidence = snapshot.get("evidence", {})
        value = lambda name: (evidence.get(name) or {}).get("value")
        area_m2 = value("parcel_area_m2")
        outcomes = {item["constraint_id"]: item for item in (evaluation or {}).get("constraint_results", [])}
        checks = []
        for constraint in compiled["constraints"]:
            item = outcomes.get(constraint["constraint_id"])
            checks.append({
                "label": compiled["constraint_labels"].get(constraint["constraint_id"], constraint["constraint_id"]),
                "outcome": item["outcome"] if item else "UNRESOLVED",
                "reason": item["explanation"] if item else "The available evidence cannot prove this requirement.",
            })
        query = f"?world={world_id}" if world_id else ""
        return {
            "title": snapshot["parcel_identity"].get("parcel_address") or "Verified property",
            "region": compiled.get("region"),
            "area_acres": round(float(area_m2) / 4046.8564224, 2) if isinstance(area_m2, (int, float)) else None,
            "transmission_distance_m": value("nearest_transmission_line_distance_m"),
            "road_distance_m": value("nearest_major_road_distance_m"),
            "zoning_code": value("parcel_zoning"),
            "overall_status": (evaluation or {}).get("overall_status", "UNRESOLVED"),
            "checks": checks,
            "sandbox_url": f"/sandbox/{snapshot['snapshot_id']}{query}",
        }

    @staticmethod
    def _stages(understand: str, discover: str, enrich: str, evaluate: str) -> list[dict[str, str]]:
        return [
            {"id": "understand", "label": "Understanding request", "status": understand},
            {"id": "discover", "label": "Resolving candidate locations", "status": discover},
            {"id": "enrich", "label": "Checking MIREYE site intelligence", "status": enrich},
            {"id": "evaluate", "label": "Evaluating constraints", "status": evaluate},
        ]


def _number_before(text: str, unit: str) -> float | None:
    match = re.search(rf"(\d+(?:\.\d+)?)\s*{re.escape(unit)}\b", text)
    return float(match.group(1)) if match else None


def _distance_threshold(text: str, subject: str) -> float | None:
    patterns = (
        rf"(?:within|under|less than|max(?:imum)?)\s*(\d+(?:\.\d+)?)\s*(km|kilometers?|m|meters?)\s+(?:of|to|from)\s+(?:a\s+)?{subject}",
        rf"{subject}[^.]*?(?:within|under|less than|max(?:imum)?)\s*(\d+(?:\.\d+)?)\s*(km|kilometers?|m|meters?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = float(match.group(1))
            return value * 1000 if match.group(2).startswith("k") else value
    return None


def _location(text: str) -> dict[str, Any] | None:
    coordinate = re.search(r"(-?\d{1,2}(?:\.\d+))\s*[, ]\s*(-?\d{1,3}(?:\.\d+))", text)
    if coordinate:
        lat, lng = float(coordinate.group(1)), float(coordinate.group(2))
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return {"lat": lat, "lng": lng}
    address = re.search(r"(?:\bat\b|\baddress(?: is)?\b|\bproperty at\b|^diligence)\s+(.+)$", text, re.IGNORECASE)
    if not address and re.match(r"^\d{1,6}\s+\S+", text):
        return {"input": text, "kind": "address"}
    if address and re.search(r"\d", address.group(1)):
        return {"input": address.group(1).strip(" ."), "kind": "address"}
    return None
