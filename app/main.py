"""
Unified Mireye Platform API Server (FastAPI).

Exposes the unified spatial core:
  - POST /v1/screen: Multi-parcel inverse candidate search & discovery
  - POST /v1/ask: Point lookup & deep fact verification with provenance
  - GET  /v1/grid: Interconnection Capacity Intelligence (ICI) & Substation Dynamics
  - POST /v1/workspace/open: Workspace session management
  - POST /v1/workspace/observe: Agentic memory observation binding
  - GET  /v1/workspace/{id}/state: Active shortlist and rejection ledger
  - POST /v1/workspace/{id}/invalidate: Automated staleness detection
  - GET  /v1/workspace/{id}/replay: Time-travel state reconstruction
  - GET  /v1/meta/fields: Live/local field metadata catalog
"""
from __future__ import annotations

from typing import Any
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from app.discovery.confidence import rank_shortlist_by_confidence, score_site
from app.discovery.screen import DiscoveryEngine, FilterRule
from app.discovery.spatial import SpatialDiscovery
from app.grid.ici import ICIEngine
from app.mireye_client import MireyeClient
from app.workspace.engine import WorkspaceEngine
from app.workspace.store import WorkspaceStore

app = FastAPI(
    title="Mireye Agentic Siting & Spatial Intelligence Platform",
    description="Transforms Mireye from a single-point verification tool into an autonomous enterprise site origination engine.",
    version="1.0.0",
)

# Global core singletons
mireye_client = MireyeClient()
workspace_store = WorkspaceStore()
workspace_engine = WorkspaceEngine(store=workspace_store, client=mireye_client)
spatial_discovery = SpatialDiscovery()
ici_engine = ICIEngine()
discovery_engine = DiscoveryEngine(client=mireye_client)


# -----------------------------------------------------------------------------
# Request / Response Schemas
# -----------------------------------------------------------------------------
class ScreenRequest(BaseModel):
    min_lat: float | None = None
    max_lat: float | None = None
    min_lng: float | None = None
    max_lng: float | None = None
    min_acreage: float | None = None
    max_acreage: float | None = None
    max_slope_pct: float | None = None
    flood_zones: list[str] | None = None
    max_flood_risk_score: float | None = None
    min_substation_capacity_mw: float | None = None
    max_distance_to_substation_km: float | None = None
    max_queue_depth: int | None = None
    zoning_renewable_only: bool = False
    owner_types: list[str] | None = None
    limit: int = Field(default=50, ge=1, le=500)
    apply_confidence_scoring: bool = True


class AskRequest(BaseModel):
    lat: float | None = None
    lng: float | None = None
    address: str | None = None
    fields: list[str] | None = None
    preset: str | None = "site_selection"


class WorkspaceOpenRequest(BaseModel):
    workspace_id: str
    label: str = ""


class WorkspaceObserveRequest(BaseModel):
    workspace_id: str
    local_key: str
    status: str  # "shortlisted", "candidate", "rejected"
    justification: str
    lat: float | None = None
    lng: float | None = None
    address: str | None = None


# -----------------------------------------------------------------------------
# API Routes
# -----------------------------------------------------------------------------
@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "mireye-platform",
        "mode": mireye_client.mode,
    }


@app.get("/v1/usage")
async def get_usage():
    return await mireye_client.usage()


@app.get("/v1/meta/fields")
async def get_meta_fields():
    return await mireye_client.meta_fields()


@app.post("/v1/screen")
async def screen_parcels(req: ScreenRequest):
    """
    Inverse Candidate Search: Scan parcels matching spatial, terrain, environmental,
    and electrical grid constraints.
    """
    raw_candidates = spatial_discovery.search_candidates(
        min_lat=req.min_lat,
        max_lat=req.max_lat,
        min_lng=req.min_lng,
        max_lng=req.max_lng,
        min_acreage=req.min_acreage,
        max_acreage=req.max_acreage,
        max_slope_pct=req.max_slope_pct,
        flood_zones=req.flood_zones,
        max_flood_risk_score=req.max_flood_risk_score,
        min_substation_capacity_mw=req.min_substation_capacity_mw,
        max_distance_to_substation_km=req.max_distance_to_substation_km,
        max_queue_depth=req.max_queue_depth,
        zoning_renewable_only=req.zoning_renewable_only,
        owner_types=req.owner_types,
        limit=req.limit,
    )

    if req.apply_confidence_scoring:
        ranked = rank_shortlist_by_confidence(raw_candidates)
    else:
        ranked = raw_candidates

    return {
        "candidates_found": len(ranked),
        "shortlist": ranked,
    }


@app.post("/v1/ask")
async def ask_parcel(req: AskRequest):
    """
    Deep Fact Verification: Fetch complete dossier facts and provenance citations
    for a specific site.
    """
    if req.lat is None and req.lng is None and not req.address:
        raise HTTPException(status_code=400, detail="Must provide lat/lng coordinates or address.")

    dossier = await mireye_client.fetch(
        lat=req.lat,
        lng=req.lng,
        address=req.address,
        fields=req.fields,
        preset=req.preset,
    )

    if not dossier.get("ok", True):
        raise HTTPException(status_code=404, detail=dossier.get("error", "Parcel not found"))

    confidence_breakdown = score_site(dossier.get("fields", {}))
    return {
        "dossier": dossier,
        "confidence": confidence_breakdown,
    }


@app.get("/v1/grid")
async def get_grid_capacity(
    lat: float = Query(..., description="Latitude of target parcel"),
    lng: float = Query(..., description="Longitude of target parcel"),
    target_capacity_mw: float = Query(50.0, description="Target generation or load size in MW"),
    slope_pct: float = Query(0.0, description="Slope percentage"),
    epa_wetlands_pct: float = Query(0.0, description="Wetlands percentage"),
    superfund_nearby: bool = Query(False, description="Whether EPA superfund site is nearby"),
    compress_tokens: bool = Query(False, description="Compress payload by 80% for LLM context windows"),
):
    """
    Interconnection Capacity Intelligence: Substation headroom modeling, FERC queue
    attrition velocity, and ROW feasibility analysis.
    """
    analysis = ici_engine.analyze_interconnection(
        lat=lat,
        lng=lng,
        target_capacity_mw=target_capacity_mw,
        slope_pct=slope_pct,
        epa_wetlands_pct=epa_wetlands_pct,
        superfund_nearby=superfund_nearby,
    )

    if compress_tokens:
        return ici_engine.compress_for_llm(analysis)
    return analysis


# -----------------------------------------------------------------------------
# Workspace Execution Primitives (Agentic Memory)
# -----------------------------------------------------------------------------
@app.post("/v1/workspace/open")
async def open_workspace(req: WorkspaceOpenRequest):
    workspace_engine.open(req.workspace_id, req.label)
    return {"workspace_id": req.workspace_id, "status": "opened"}


@app.post("/v1/workspace/observe")
async def observe_site(req: WorkspaceObserveRequest):
    """
    Observe: Bind a sighting + justification to a site with an immutable dossier snapshot.
    """
    res = await workspace_engine.observe(
        workspace_id=req.workspace_id,
        local_key=req.local_key,
        status=req.status,
        justification=req.justification,
        lat=req.lat,
        lng=req.lng,
        address=req.address,
    )
    return res


@app.get("/v1/workspace/{workspace_id}/state")
async def get_workspace_state(workspace_id: str):
    """
    State: Retrieve current shortlist, candidate set, and rejection ledger.
    """
    return workspace_engine.state(workspace_id)


@app.post("/v1/workspace/{workspace_id}/invalidate")
async def check_invalidation(workspace_id: str):
    """
    Invalidate: Check all stored snapshots against live data strata and log staleness diffs.
    """
    stale_diffs = await workspace_engine.invalidate_check(workspace_id)
    return {
        "workspace_id": workspace_id,
        "stale_fields_count": len(stale_diffs),
        "stale_fields": stale_diffs,
    }


@app.get("/v1/workspace/{workspace_id}/replay")
async def replay_workspace_state(
    workspace_id: str,
    as_of_ts: float = Query(..., description="Epoch timestamp to replay state at"),
):
    """
    Replay: Reconstruct exact intelligence state at any prior timestamp.
    """
    return workspace_engine.replay(workspace_id, as_of_ts)


@app.get("/v1/workspace/{workspace_id}/history/{local_key}")
async def get_site_history(workspace_id: str, local_key: str):
    """
    History: Full chronological audit trail for a single site.
    """
    return {
        "workspace_id": workspace_id,
        "local_key": local_key,
        "history": workspace_engine.history(workspace_id, local_key),
    }
