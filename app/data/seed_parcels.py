"""
Generates a small synthetic GeoParquet-style dataset so the platform is fully
runnable on a laptop with zero external credentials. Mirrors the field shape
described in the ideation doc (319 fields / 23+ sources / 7 layers) but with
a representative ~20-field subset across the layers that matter for siting:
elevation (USGS), flood (FEMA), climate (NOAA), environmental (EPA),
zoning, interconnection queue, and ownership.

Run: python -m app.data.seed_parcels
"""
import json
from pathlib import Path

import duckdb
import numpy as np
import h3

PARQUET_DIR = Path(__file__).resolve().parent / "parquet"
PARQUET_DIR.mkdir(parents=True, exist_ok=True)

N = 2500
rng = np.random.default_rng(42)

lat = rng.uniform(31.5, 34.5, N)
lon = rng.uniform(-98.5, -95.0, N)


def make_rows():
    for i in range(N):
        h3_cell = h3.latlng_to_cell(lat[i], lon[i], 7)
        yield {
            "parcel_id": f"PCL-{i:06d}",
            "lat": float(lat[i]),
            "lon": float(lon[i]),
            "h3_r7": h3_cell,
            "acreage": float(np.round(rng.uniform(2, 850), 1)),
            "elevation_m": float(np.round(rng.uniform(150, 900), 1)),
            "slope_pct": float(np.round(rng.uniform(0, 22), 2)),
            "flood_zone": str(rng.choice(["X", "AE", "A", "VE"], p=[0.7, 0.15, 0.1, 0.05])),
            "flood_risk_score": float(np.round(rng.uniform(0, 1), 3)),
            "avg_wind_speed_mps": float(np.round(rng.uniform(3.5, 9.5), 2)),
            "annual_precip_mm": float(np.round(rng.uniform(600, 1400), 0)),
            "epa_superfund_nearby": bool(rng.random() < 0.04),
            "epa_wetlands_pct": float(np.round(rng.uniform(0, 0.4), 3)),
            "zoning_code": str(rng.choice(["AG", "IND-L", "IND-H", "RES", "COMM"], p=[0.4, 0.2, 0.1, 0.2, 0.1])),
            "zoning_renewable_permitted": bool(rng.random() < 0.55),
            "distance_to_substation_km": float(np.round(rng.uniform(0.2, 40), 2)),
            "interconnection_queue_depth": int(rng.integers(0, 60)),
            "substation_capacity_mw": float(np.round(rng.uniform(5, 400), 1)),
            "owner_type": str(rng.choice(["individual", "trust", "LLC", "municipal", "unknown"])),
            "parcel_price_usd": float(np.round(rng.uniform(15000, 3_500_000), 0)),
            "last_sale_year": int(rng.integers(1985, 2025)),
            "source_snapshot_ts": "2026-06-01T00:00:00Z",
        }


if __name__ == "__main__":
    rows = list(make_rows())
    tmp = PARQUET_DIR / "_rows.jsonl"
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    con = duckdb.connect()
    con.execute(f"""
        COPY (SELECT * FROM read_json_auto('{tmp}'))
        TO '{PARQUET_DIR / "parcels.parquet"}' (FORMAT PARQUET)
    """)
    con.close()
    tmp.unlink()
    print(f"Wrote {N} synthetic parcels -> {PARQUET_DIR / 'parcels.parquet'}")