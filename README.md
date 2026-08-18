# Mireye: Agentic Siting & Spatial Intelligence Platform

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![DuckDB](https://img.shields.io/badge/DuckDB-Spatial-yellow.svg)](https://duckdb.org/)
[![Tests](https://img.shields.io/badge/tests-20%20passed-brightgreen.svg)]()

Transforms Mireye from a single-point verification tool into an autonomous enterprise site origination platform for renewable energy, data center siting, and infrastructure development.

---

## Architecture Overview

```
                    ┌──────────────────────────────────────────────┐
                    │            Client / Agent / MCP              │
                    └──────┬──────────────┬──────────────┬─────────┘
                           │              │              │
                    POST /v1/screen  POST /v1/ask    GET /v1/grid
                    (Constraints     (Point Facts &  (Substation &
                     to Parcels)      Provenance)     Queue Dynamics)
                           │              │              │
                    ┌──────┴──────────────┴──────────────┴─────────┐
                    │                 Unified API                  │
                    │               (FastAPI Engine)               │
                    └──────┬─────────────────────────────┬─────────┘
                           │                             │
                    ┌──────▼────────────────┐     ┌──────▼────────────────┐
                    │   Discovery Engine    │     │   Workspace Memory    │
                    │ (DuckDB + Parquet+H3) │     │ (SQLite Store/Replay) │
                    └──────┬────────────────┘     └──────┬────────────────┘
                           │                             │
                    ┌──────▼─────────────────────────────▼────────────────┐
                    │                    Mireye Client                    │
                    │   (Live HTTP API / Local DuckDB Synthetic Engine)   │
                    └─────────────────────────────────────────────────────┘
```

---

## Key Features

1. **Discovery Engine (`POST /v1/screen`)**:
   - Inverse candidate search across millions of parcels.
   - DuckDB columnar acceleration on GeoParquet with H3 coarse grid support.
   - Multi-variable filtering: Acreage, slope, FEMA flood zones, substation distance/capacity, and renewable zoning.

2. **Agentic Memory Architecture (`/v1/workspace/*`)**:
   - Stateful persistence for multi-epoch autonomous AI agents.
   - **Observe**: Immutable snapshot binding with sighting justifications.
   - **State**: Shortlist retrieval and proprietary Rejection Moat ledger.
   - **Invalidate**: Automated staleness detection against live strata.
   - **Replay**: Time-travel reconstruction of intelligence state at historical timestamps.

3. **Interconnection Capacity Intelligence (ICI) (`GET /v1/grid`)**:
   - Substation Capacity Dynamics (SCD): Firm Headroom vs. Contested Headroom.
   - FERC Order 2023 Queue Attrition Velocity modeling.
   - Right-of-Way (ROW) Feasibility Filter: Physical barrier detection (wetlands, highways, steep topography, EPA Superfunds).
   - 80% token compression for LLM agent execution loops.

4. **Confidence Scoring & Calibration**:
   - Heuristic ranking based on per-field `status` and `confidence`.
   - Weakest field identification and audit breakdowns.

5. **Dual Operation Modes**:
   - **Live Mode**: Direct connection to Mireye API (`api.mireye.com`) across 306 fields and 15 presets.
   - **Local Mode**: Zero-credential offline development using local DuckDB and 2,500 synthetic Texas siting parcels.

6. **Native Model Context Protocol (MCP)**:
   - 7 agent tools ready for LLMs (`screen_parcels`, `get_grid_capacity`, `verify_parcel`, `workspace_observe`, `workspace_state`, `workspace_invalidate`, `workspace_replay`).

---

## Getting Started

### Installation

```bash
# Clone repository
git clone https://github.com/387daksh/MIREYE.git
cd MIREYE

# Install dependencies
pip install fastapi uvicorn duckdb h3 httpx pydantic pytest python-dotenv
```

### Configuration

Copy `.env.example` to `.env` and configure your credentials:

```bash
cp .env.example .env
```

```ini
MIREYE_API_KEY=your_mireye_api_key_here
MIREYE_BASE_URL=https://api.mireye.com
```

### Seed Synthetic Dataset (Optional for Local Mode)

```bash
python -m app.data.seed_parcels
```

### Run the Unified API Server

```bash
python -m uvicorn app.main:app --port 8000 --reload
```

Interactive API documentation available at `http://localhost:8000/docs`.

### Run Automated Tests

```bash
python -m pytest -v
```

---

## API Reference

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/v1/screen` | `POST` | Multi-parcel constraint search (inverse search) |
| `/v1/ask` | `POST` | Deep fact verification for a single parcel |
| `/v1/grid` | `GET` | Interconnection capacity & substation dynamics |
| `/v1/meta/fields` | `GET` | Live/local metadata field catalog |
| `/v1/workspace/open` | `POST` | Open/create an agentic workspace |
| `/v1/workspace/observe` | `POST` | Bind sighting, status & justification |
| `/v1/workspace/{id}/state` | `GET` | Get shortlisted candidates and rejection ledger |
| `/v1/workspace/{id}/invalidate` | `POST` | Detect staleness against underlying data |
| `/v1/workspace/{id}/replay` | `GET` | Time-travel replay state at `as_of_ts` |
| `/v1/workspace/{id}/history/{key}` | `GET` | Full audit trail for a single parcel |
| `/health` | `GET` | Service health status |

---

## License

MIT License.
