"""
DuckDB-accelerated spatial screening and inverse candidate search across GeoParquet.

Enables fast multi-variable querying over millions of parcels using DuckDB's
columnar vector execution. Returns structured candidate parcels with full
per-field provenance records.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from app.config import PARQUET_DIR

PARQUET_FILE = PARQUET_DIR / "parcels.parquet"


class SpatialDiscovery:
    def __init__(self, parquet_path: Path | str | None = None):
        self.parquet_path = Path(parquet_path or PARQUET_FILE)

    def search_candidates(
        self,
        min_lat: float | None = None,
        max_lat: float | None = None,
        min_lng: float | None = None,
        max_lng: float | None = None,
        min_acreage: float | None = None,
        max_acreage: float | None = None,
        max_slope_pct: float | None = None,
        flood_zones: list[str] | None = None,
        max_flood_risk_score: float | None = None,
        min_substation_capacity_mw: float | None = None,
        max_distance_to_substation_km: float | None = None,
        max_queue_depth: int | None = None,
        zoning_renewable_only: bool = False,
        owner_types: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict]:
        if not self.parquet_path.exists():
            return []

        clauses = ["1=1"]
        if min_lat is not None:
            clauses.append(f"lat >= {min_lat}")
        if max_lat is not None:
            clauses.append(f"lat <= {max_lat}")
        if min_lng is not None:
            clauses.append(f"lon >= {min_lng}")
        if max_lng is not None:
            clauses.append(f"lon <= {max_lng}")
        if min_acreage is not None:
            clauses.append(f"acreage >= {min_acreage}")
        if max_acreage is not None:
            clauses.append(f"acreage <= {max_acreage}")
        if max_slope_pct is not None:
            clauses.append(f"slope_pct <= {max_slope_pct}")
        if flood_zones:
            zones_str = ", ".join(f"'{z}'" for z in flood_zones)
            clauses.append(f"flood_zone IN ({zones_str})")
        if max_flood_risk_score is not None:
            clauses.append(f"flood_risk_score <= {max_flood_risk_score}")
        if min_substation_capacity_mw is not None:
            clauses.append(f"substation_capacity_mw >= {min_substation_capacity_mw}")
        if max_distance_to_substation_km is not None:
            clauses.append(f"distance_to_substation_km <= {max_distance_to_substation_km}")
        if max_queue_depth is not None:
            clauses.append(f"interconnection_queue_depth <= {max_queue_depth}")
        if zoning_renewable_only:
            clauses.append("zoning_renewable_permitted = true")
        if owner_types:
            owners_str = ", ".join(f"'{o}'" for o in owner_types)
            clauses.append(f"owner_type IN ({owners_str})")

        where_clause = " AND ".join(clauses)
        query = f"""
            SELECT *
            FROM '{self.parquet_path.as_posix()}'
            WHERE {where_clause}
            ORDER BY acreage DESC
            LIMIT {limit}
        """

        con = duckdb.connect()
        try:
            df = con.execute(query).df()
            if df.empty:
                return []

            records = df.to_dict(orient="records")
            formatted = []
            for r in records:
                # Structure matching Mireye candidate dictionary with fields
                fields = {}
                for k, v in r.items():
                    if hasattr(v, "item"):
                        v = v.item()
                    fields[k] = {
                        "value": v,
                        "status": "ok",
                        "confidence": "high",
                        "source": "GeoParquet / Mireye Index",
                    }
                formatted.append({
                    "parcel_id": str(r["parcel_id"]),
                    "lat": float(r["lat"]),
                    "lng": float(r["lon"]),
                    "h3_r7": str(r["h3_r7"]),
                    "fields": fields,
                })
            return formatted
        finally:
            con.close()
