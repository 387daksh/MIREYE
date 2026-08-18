"""
Discovery Engine — multi-parcel site screening.

Mireye's real API answers questions about ONE place at a time (or up to 25
in a /v1/fetch/batch call). It has no "find me every candidate across a
region matching these constraints" endpoint — that's the actual gap the
ideation doc identified, and it's still open. This module fills it as a
layer on top of the real client, not a reimplementation of anything Mireye
already does.

Two things the ideation doc worried about turned out to be non-issues on
the real API and are NOT reimplemented here:
  - "Silent field-cap truncation" — /v1/fetch already hard-refuses
    (400 fields_too_many) past 50 fields rather than dropping silently.
  - "Hidden match guessing" — /v1/geocode and /v1/lookup already refuse
    centroid-grade matches and return an explicit `clarify` disposition
    for genuinely ambiguous input, rather than silently picking one.

Pipeline:
  1. generate_grid(): coarse H3 candidate points across a bounding region
     (cheap — no API calls).
  2. screen(): batches those points through /v1/fetch/batch (25 at a time,
     respecting Mireye's own rate limits) with a field/preset selection,
     then applies user filters client-side.
  3. Results are ranked and returned with full per-field provenance intact
     — every value keeps its `source`, `confidence`, and `status` from
     Mireye, so a shortlist is auditable back to the federal source.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field as dc_field
from typing import Any, Callable

import h3

from app.mireye_client import MireyeClient

BATCH_SIZE = 25  # Mireye's /v1/fetch/batch hard limit


@dataclass
class FilterRule:
    field: str
    op: str          # one of: <, <=, >, >=, ==, !=, in, not_in
    value: Any

    def matches(self, field_record: dict | None) -> bool:
        if field_record is None or field_record.get("status") != "ok":
            return False  # never filter on a failed/absent field as if it passed
        v = field_record["value"]
        if self.op == "<":
            return v < self.value
        if self.op == "<=":
            return v <= self.value
        if self.op == ">":
            return v > self.value
        if self.op == ">=":
            return v >= self.value
        if self.op == "==":
            return v == self.value
        if self.op == "!=":
            return v != self.value
        if self.op == "in":
            return v in self.value
        if self.op == "not_in":
            return v not in self.value
        raise ValueError(f"Unknown filter op: {self.op}")


def generate_grid(min_lat: float, min_lng: float, max_lat: float, max_lng: float,
                   h3_resolution: int = 7) -> list[dict]:
    """
    Coarse candidate-point generator over a bounding box using H3. Resolution 7
    cells are ~5.2 km across — dense enough for siting-scale screening without
    burning a fetch call per square meter. No Mireye calls happen here.
    """
    poly = [
        [min_lat, min_lng], [min_lat, max_lng],
        [max_lat, max_lng], [max_lat, min_lng],
    ]
    cells = h3.polygon_to_cells(h3.LatLngPoly(poly), h3_resolution)
    points = []
    for cell in cells:
        lat, lng = h3.cell_to_latlng(cell)
        points.append({"h3_cell": cell, "lat": lat, "lng": lng})
    return points


def _extract_batch_entries(result: dict) -> list[dict]:
    """
    Pull the index-aligned array out of a /v1/fetch/batch response.

    NOTE: Mireye's OpenAPI spec leaves this endpoint's response schema
    untyped (`schema: {}`) — only the prose doc confirms entries are
    index-aligned and per-fetch-shaped. The wrapping key name itself isn't
    confirmed. This tries the plausible candidates and fails loudly rather
    than silently misaligning results with locations if none match.

    TODO(you): run one real fetch_batch call, inspect the raw JSON, and
    hardcode the correct key here — this is a 30-second fix once you have
    a live response in front of you.
    """
    if isinstance(result, list):
        return result
    for key in ("results", "locations", "items", "data"):
        if key in result and isinstance(result[key], list):
            return result[key]
    raise ValueError(
        "Could not find the results array in /v1/fetch/batch response. "
        f"Top-level keys were: {list(result.keys())}. "
        "Update _extract_batch_entries() in app/discovery/screen.py with the real key."
    )


class DiscoveryEngine:
    def __init__(self, client: MireyeClient | None = None):
        self.client = client or MireyeClient()

    async def screen(
        self,
        candidates: list[dict],               # [{"lat":.., "lng":..} or {"address":..}, ...]
        filters: list[FilterRule] | None = None,
        fields: list[str] | None = None,
        preset: str | None = None,
        rank_by: str | None = None,            # field name to sort surviving candidates by
        rank_desc: bool = False,
        max_results: int | None = None,
        concurrency: int = 4,
    ) -> dict:
        filters = filters or []
        chunks = [candidates[i:i + BATCH_SIZE] for i in range(0, len(candidates), BATCH_SIZE)]

        sem = asyncio.Semaphore(concurrency)

        async def run_chunk(chunk):
            async with sem:
                locs = [{k: v for k, v in c.items() if k in ("lat", "lng", "address")} for c in chunk]
                return await self.client.fetch_batch(locations=locs, fields=fields, preset=preset)

        chunk_results = await asyncio.gather(*[run_chunk(c) for c in chunks])

        all_results = []
        for chunk, result in zip(chunks, chunk_results):
            entries = _extract_batch_entries(result)
            for original, entry in zip(chunk, entries):
                if entry.get("ok") is False:
                    # Per-location resolution failure (docs: "resolves to an
                    # ok:false entry with the same error object /v1/fetch would
                    # have returned"). Treat as screened-out, not a crash.
                    all_results.append({**original, "fields": {}, "location_error": entry})
                else:
                    all_results.append({**original, **entry})

        passed, rejected = [], []
        for r in all_results:
            fields_map = r.get("fields", {})
            failed_rules = [f.field for f in filters if not f.matches(fields_map.get(f.field))]
            if failed_rules:
                rejected.append({**r, "rejected_on": failed_rules})
            else:
                passed.append(r)

        if rank_by:
            passed.sort(
                key=lambda r: r.get("fields", {}).get(rank_by, {}).get("value", 0),
                reverse=rank_desc,
            )

        if max_results:
            passed = passed[:max_results]

        return {
            "candidates_screened": len(all_results),
            "shortlisted": passed,
            "rejected_count": len(rejected),
            "rejected": rejected,
        }