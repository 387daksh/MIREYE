"""
Async Mireye API Client.

Supports two operational modes:
  - Live Mode (DATA_MODE="live" with MIREYE_API_KEY):
    Calls the official Mireye REST endpoints at https://api.mireye.com.
  - Local Mode (DATA_MODE="local" or offline fallback):
    Simulates Mireye's multi-layer federal data stack (USGS, FEMA, NOAA, EPA,
    NREL, EIA, County Tax Rolls) using the local DuckDB GeoParquet dataset.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import duckdb
import httpx

from app.config import DATA_MODE, MIREYE_API_KEY, MIREYE_BASE_URL, PARQUET_DIR

logger = logging.getLogger(__name__)

PARQUET_FILE = PARQUET_DIR / "parcels.parquet"

# Standard presets
PRESET_FIELDS = {
    "site_selection": [
        "acreage", "elevation_m", "slope_pct", "flood_zone", "flood_risk_score",
        "avg_wind_speed_mps", "annual_precip_mm", "epa_superfund_nearby", "epa_wetlands_pct",
        "zoning_code", "zoning_renewable_permitted", "distance_to_substation_km",
        "interconnection_queue_depth", "substation_capacity_mw", "owner_type",
        "parcel_price_usd", "last_sale_year",
    ],
    "terrain": ["elevation_m", "slope_pct", "flood_zone", "flood_risk_score"],
    "grid": ["distance_to_substation_km", "interconnection_queue_depth", "substation_capacity_mw"],
    "environmental": ["epa_superfund_nearby", "epa_wetlands_pct", "flood_zone"],
    "zoning": ["zoning_code", "zoning_renewable_permitted", "owner_type"],
}

FIELD_METADATA = {
    "acreage": {"source": "County Assessor / Parcel Boundary", "layer": "Cadastral", "confidence": "high"},
    "elevation_m": {"source": "USGS 3DEP 10m DEM", "layer": "Topography", "confidence": "high"},
    "slope_pct": {"source": "USGS 3DEP Derived Slope", "layer": "Topography", "confidence": "high"},
    "flood_zone": {"source": "FEMA National Flood Hazard Layer (NFHL)", "layer": "Hazard", "confidence": "high"},
    "flood_risk_score": {"source": "NOAA / First Street Flood Model", "layer": "Climate", "confidence": "medium"},
    "avg_wind_speed_mps": {"source": "NREL WIND Toolkit 100m", "layer": "Resource", "confidence": "high"},
    "annual_precip_mm": {"source": "NOAA NCEI Climate Normals", "layer": "Climate", "confidence": "high"},
    "epa_superfund_nearby": {"source": "EPA SEMS Active Superfund Sites", "layer": "Environmental", "confidence": "high"},
    "epa_wetlands_pct": {"source": "USFWS National Wetlands Inventory", "layer": "Environmental", "confidence": "high"},
    "zoning_code": {"source": "County / Municipal Planning Ordinances", "layer": "Zoning", "confidence": "high"},
    "zoning_renewable_permitted": {"source": "NREL Siting Ordinances / Zoneomics", "layer": "Zoning", "confidence": "high"},
    "distance_to_substation_km": {"source": "HIFLD Electric Power Substations", "layer": "Grid", "confidence": "high"},
    "interconnection_queue_depth": {"source": "FERC / ISO Interconnection Queue", "layer": "Grid", "confidence": "medium"},
    "substation_capacity_mw": {"source": "EIA-860 Generator / Substation Database", "layer": "Grid", "confidence": "high"},
    "owner_type": {"source": "County Deed & Assessor Roll", "layer": "Cadastral", "confidence": "high"},
    "parcel_price_usd": {"source": "County Recorded Assessment", "layer": "Valuation", "confidence": "medium"},
    "last_sale_year": {"source": "County Registry of Deeds", "layer": "Cadastral", "confidence": "high"},
}


class MireyeClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        mode: str | None = None,
        timeout: float = 30.0,
    ):
        self.api_key = api_key if api_key is not None else MIREYE_API_KEY
        self.base_url = (base_url or MIREYE_BASE_URL).rstrip("/")
        self.mode = mode or ("live" if self.api_key else "local")
        self.timeout = timeout
        self._registered_sites: dict[str, dict] = {}

    async def _request(self, method: str, path: str, json_body: dict | None = None, params: dict | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mireye-Agent-Platform/1.0",
        }
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.request(method, url, json=json_body, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()

    # -------------------------------------------------------------------------
    # Local Simulation Engine (DuckDB + Parquet)
    # -------------------------------------------------------------------------
    def _local_find_parcel(self, lat: float | None = None, lng: float | None = None, address: str | None = None) -> dict | None:
        if not PARQUET_FILE.exists():
            return None

        con = duckdb.connect()
        try:
            if lat is not None and lng is not None:
                # Find nearest parcel by euclidean distance
                query = f"""
                    SELECT *,
                           ((lat - {lat})*(lat - {lat}) + (lon - {lng})*(lon - {lng})) as dist_sq
                    FROM '{PARQUET_FILE.as_posix()}'
                    ORDER BY dist_sq ASC
                    LIMIT 1
                """
            elif address:
                # If address contains PCL-XXXXXX
                df = con.execute(
                    f"""
                    SELECT * FROM '{PARQUET_FILE.as_posix()}'
                    WHERE parcel_id ILIKE ?
                    LIMIT 2
                    """,
                    [f"%{address.strip()}%"],
                ).df()
                if len(df) > 1:
                    return {
                        "ok": False,
                        "error": {
                            "code": "ambiguous_address",
                            "message": "Address matched multiple parcels. Choose one candidate and retry.",
                            "candidates": [
                                {
                                    "parcel_id": str(r["parcel_id"]),
                                    "lat": float(r["lat"]),
                                    "lng": float(r["lon"]),
                                }
                                for r in df.to_dict(orient="records")
                            ],
                        },
                    }
                if df.empty:
                    return None
                return df.to_dict(orient="records")[0]
            else:
                return None

            df = con.execute(query).df()
            if df.empty:
                return None
            return df.to_dict(orient="records")[0]
        finally:
            con.close()

    def _build_dossier_from_row(self, row: dict | None, lat: float | None = None, lng: float | None = None,
                                fields: list[str] | None = None, preset: str | None = None) -> dict:
        if not row:
            return {
                "ok": False,
                "lat": lat,
                "lng": lng,
                "error": {"code": "not_found", "message": "No parcel matched the coordinates or identifier."},
            }
        if row.get("ok") is False:
            return row

        target_fields = fields or (PRESET_FIELDS.get(preset or "site_selection", PRESET_FIELDS["site_selection"]))
        field_records = {}
        for f in target_fields:
            if f in row:
                meta = FIELD_METADATA.get(f, {"source": "Mireye Index", "layer": "General", "confidence": "high"})
                val = row[f]
                # Cast numpy types to python native types
                if hasattr(val, "item"):
                    val = val.item()
                field_records[f] = {
                    "value": val,
                    "status": "ok",
                    "confidence": meta["confidence"],
                    "source": meta["source"],
                    "layer": meta["layer"],
                }
            else:
                field_records[f] = {
                    "value": None,
                    "status": "absent",
                    "confidence": "unknown",
                    "source": "None",
                    "layer": "None",
                }

        p_lat = float(row.get("lat", lat or 0.0))
        p_lng = float(row.get("lon", lng or 0.0))

        return {
            "ok": True,
            "parcel_id": str(row.get("parcel_id", "PCL-UNKNOWN")),
            "lat": p_lat,
            "lng": p_lng,
            "h3_r7": str(row.get("h3_r7", "")),
            "fields": field_records,
            "snapshot_ts": str(row.get("source_snapshot_ts", "2026-06-01T00:00:00Z")),
        }

    # -------------------------------------------------------------------------
    # Public API Methods
    # -------------------------------------------------------------------------
    async def fetch(
        self,
        lat: float | None = None,
        lng: float | None = None,
        address: str | None = None,
        fields: list[str] | None = None,
        preset: str | None = None,
    ) -> dict:
        """Fetch dossier facts for a single parcel."""
        if (lat is None) != (lng is None):
            return {
                "ok": False,
                "lat": lat,
                "lng": lng,
                "error": {"code": "invalid_location", "message": "Latitude and longitude must be provided together."},
            }
        if lat is None and lng is None and not address:
            return {
                "ok": False,
                "lat": lat,
                "lng": lng,
                "error": {"code": "invalid_location", "message": "Must provide lat/lng coordinates or address."},
            }

        if self.mode == "live":
            payload = {"lat": lat, "lng": lng, "address": address, "fields": fields, "preset": preset}
            res = await self._request("POST", "/v1/fetch", json_body={k: v for k, v in payload.items() if v is not None})
            if isinstance(res, dict) and "ok" not in res:
                res["ok"] = True
            return res

        # Local mode
        row = self._local_find_parcel(lat, lng, address)
        return self._build_dossier_from_row(row, lat=lat, lng=lng, fields=fields, preset=preset)

    async def fetch_batch(
        self,
        locations: list[dict],
        fields: list[str] | None = None,
        preset: str | None = None,
    ) -> dict:
        """Batch fetch up to 25 locations at once."""
        if self.mode == "live":
            payload = {"locations": locations, "fields": fields, "preset": preset}
            return await self._request("POST", "/v1/fetch/batch", json_body={k: v for k, v in payload.items() if v is not None})

        # Local mode
        results = []
        for loc in locations:
            lat = loc.get("lat")
            lng = loc.get("lng")
            address = loc.get("address")
            row = self._local_find_parcel(lat, lng, address)
            results.append(self._build_dossier_from_row(row, lat=lat, lng=lng, fields=fields, preset=preset))

        return {"results": results, "count": len(results)}

    async def fetch_quote(
        self,
        locations: list[dict],
        fields: list[str] | None = None,
        preset: str | None = None,
    ) -> dict:
        """Quote pricing and credit cost for a proposed batch fetch."""
        if self.mode == "live":
            payload = {"locations": locations, "fields": fields, "preset": preset}
            return await self._request("POST", "/v1/fetch/quote", json_body=payload)

        n_locs = len(locations)
        n_fields = len(fields) if fields else len(PRESET_FIELDS.get(preset or "site_selection", []))
        return {
            "locations_count": n_locs,
            "fields_requested": n_fields,
            "estimated_credits": max(1, n_locs * (1 if n_fields <= 15 else 2)),
            "cost_usd": round(n_locs * 0.05, 2),
            "tier": "standard",
        }

    async def meta_fields(self) -> dict:
        """Return the live/local catalog of supported geospatial and zoning fields."""
        if self.mode == "live":
            res = await self._request("GET", "/v1/meta/fields")
            if isinstance(res, dict) and "total_fields" not in res and "fields" in res:
                res["total_fields"] = len(res["fields"])
            return res

        return {
            "total_fields": len(FIELD_METADATA),
            "layers": sorted(list({v["layer"] for v in FIELD_METADATA.values()})),
            "fields": [
                {"name": name, "layer": meta["layer"], "source": meta["source"], "default_confidence": meta["confidence"]}
                for name, meta in FIELD_METADATA.items()
            ],
            "presets": list(PRESET_FIELDS.keys()),
        }

    async def lookup(self, address: str) -> dict:
        """Verify address clarity without silent guessing."""
        if self.mode == "live":
            return await self._request("POST", "/v1/lookup", json_body={"address": address})

        return {
            "query": address,
            "disposition": "exact_match",
            "candidates": [
                {"address": address, "lat": 32.7767, "lng": -96.7970, "confidence": 0.98, "parcel_id": "PCL-000100"}
            ],
        }

    async def geocode(self, address: str) -> dict:
        """Resolve address to lat/lng coordinates."""
        if self.mode == "live":
            return await self._request("POST", "/v1/geocode", json_body={"address": address})

        return {"address": address, "lat": 32.7767, "lng": -96.7970, "match_quality": "rooftop"}

    async def proximity(self, lat: float, lng: float, targets: list[str] | None = None) -> dict:
        """Calculate straight-line and network distances to infrastructure targets."""
        if self.mode == "live":
            return await self._request("POST", "/v1/proximity", json_body={"lat": lat, "lng": lng, "targets": targets})

        return {
            "lat": lat,
            "lng": lng,
            "distances_km": {
                "substation": 2.4,
                "transmission_line": 0.8,
                "highway": 3.1,
                "rail": 5.2,
                "fiber_backbone": 1.2,
            },
        }

    async def register_site(self, payload: dict) -> dict:
        """Register a site polygon or point (/v1/sites)."""
        if self.mode == "live":
            return await self._request("POST", "/v1/sites", json_body=payload)

        site_id = f"site_{len(self._registered_sites) + 1:05d}"
        self._registered_sites[site_id] = {"id": site_id, **payload}
        return {"site_id": site_id, "status": "registered"}

    async def get_site(self, site_id: str) -> dict:
        """Retrieve registered site details."""
        if self.mode == "live":
            return await self._request("GET", f"/v1/sites/{site_id}")

        site_data = self._registered_sites.get(site_id, {"id": site_id, "geometry": None})
        return {
            "site_id": site_id,
            "fields": {k: {"value": 1.0, "status": "ok", "confidence": "high", "source": "mock"} for k in PRESET_FIELDS["site_selection"]},
            "metadata": site_data,
        }

    async def runs(self) -> dict:
        """List active/recent bulk download runs."""
        if self.mode == "live":
            return await self._request("GET", "/v1/runs")
        return {"runs": [], "total": 0}

    async def usage(self) -> dict:
        """Check API key validity and credit balance."""
        if self.mode == "live":
            return await self._request("GET", "/v1/usage")

        return {
            "status": "ok",
            "mode": "local",
            "credits_remaining": 999999,
            "tier": "enterprise_local",
        }
