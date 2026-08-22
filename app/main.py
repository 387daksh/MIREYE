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

from pathlib import Path
from typing import Any, Literal
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.discovery.confidence import rank_shortlist_by_confidence, score_site
from app.config import WORLD_ASSET_DIR
from app.diligence import DiligenceError, DiligenceService
from app.discovery.screen import DiscoveryEngine, FilterRule
from app.discovery.spatial import SpatialDiscovery
from app.grid.ici import ICIEngine
from app.mireye_client import MireyeClient
from app.product import ProductExperienceService, ProductRequestError
from app.sandbox_agent import ModelUnavailableError, SandboxAgent, ToolValidationError
from app.sandbox_evaluator import SceneValidationError, evaluate_site
from app.sandbox_scenarios import ScenarioError, ScenarioService
from app.sandbox import ConfirmationRequired, MireyeUnavailableError, ParcelIdentityError, SandboxError, SiteSnapshotService
from app.workspace.engine import WorkspaceEngine
from app.workspace.store import WorkspaceStore
from app.world import ArtifactStore, WorldError, WorldSnapshotService

app = FastAPI(
    title="Mireye Agentic Siting & Spatial Intelligence Platform",
    description="Transforms Mireye from a single-point verification tool into an autonomous enterprise site origination engine.",
    version="1.0.0",
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"status": "ok", "message": "Mireye Platform API"}


@app.get("/sandbox/{snapshot_id}")
async def serve_sandbox(snapshot_id: str):
    sandbox_file = STATIC_DIR / "sandbox.html"
    if sandbox_file.exists():
        return FileResponse(sandbox_file)
    raise HTTPException(status_code=404, detail="Sandbox frontend is unavailable.")

# Global core singletons
mireye_client = MireyeClient()
workspace_store = WorkspaceStore()
workspace_engine = WorkspaceEngine(store=workspace_store, client=mireye_client)
world_service = WorldSnapshotService(workspace_store, ArtifactStore(WORLD_ASSET_DIR))
scenario_service = ScenarioService(workspace_store, worlds=world_service)
sandbox_service = SiteSnapshotService(store=workspace_store, client=mireye_client, scenarios=scenario_service)
diligence_service = DiligenceService(workspace_store, sandbox_service, world_service)
sandbox_agent = SandboxAgent(scenarios=scenario_service, intelligence=sandbox_service, diligence=diligence_service)
product_service = ProductExperienceService(sandbox_service, world_service)
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


class SandboxResolveRequest(BaseModel):
    input: str | None = None
    kind: Literal["address", "apn", "coord"] | None = None
    lat: float | None = None
    lng: float | None = None


class SandboxQuoteRequest(BaseModel):
    lat: float
    lng: float


class SandboxSnapshotRequest(BaseModel):
    workspace_id: str
    lat: float
    lng: float
    confirmed: bool = False


class SandboxEvaluationRequest(BaseModel):
    scene_state: dict[str, Any]
    requested_constraints: list[dict[str, Any]]


class SandboxChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(min_length=1, max_length=128)
    workspace_id: str | None = None
    scenario_id: str | None = None
    world_snapshot_id: str | None = None
    confirmed_refresh_plan_id: str | None = None


class SandboxRefreshConfirmRequest(BaseModel):
    confirmed: bool = False


class SandboxScenarioCreateRequest(BaseModel):
    workspace_id: str
    user_intent: str = ""
    scene_state: dict[str, Any] | None = None
    requested_constraints: list[dict[str, Any]] | None = None
    model_id: str | None = None
    world_snapshot_id: str | None = None


class WorldSnapshotCreateRequest(BaseModel):
    site_snapshot_id: str
    aoi_buffer_m: float = Field(default=1000, ge=100, le=5000)
    requested_layers: list[Literal["terrain", "roads", "transmission"]] = Field(default_factory=lambda: ["terrain", "roads"])
    prefer_1m: bool = True
    overture_release: str = "2026-08-19.0"


class SandboxScenarioBranchRequest(BaseModel):
    user_intent: str = ""
    model_id: str | None = None


class SandboxCompareRequest(BaseModel):
    left_scenario_id: str
    right_scenario_id: str
    left_revision: int | None = Field(default=None, ge=1)
    right_revision: int | None = Field(default=None, ge=1)


class ProductRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class ProductSelectionRequest(BaseModel):
    candidate_index: int = Field(ge=0)


class ProductConfirmationRequest(BaseModel):
    confirmed: bool = False


class DiligenceProjectCreateRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)
    candidates: list[Any] = Field(min_length=1, max_length=500)


class DiligencePlanRequest(BaseModel):
    confirmed_resolution: bool = False


class DiligenceEnrichmentConfirmRequest(BaseModel):
    spend_plan_id: str
    confirmed: bool = False


class DiligenceResolutionSelectionRequest(BaseModel):
    option_index: int = Field(ge=0)


class DiligenceWatchRequest(BaseModel):
    enabled: bool = True


class DiligenceCompareRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=2, max_length=100)


class DiligenceRefreshConfirmRequest(BaseModel):
    spend_plan_id: str
    confirmed: bool = False


class DiligenceChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(min_length=1, max_length=128)
    confirmed_resolution_project_id: str | None = None
    confirmed_enrichment_plan_id: str | None = None
    confirmed_refresh_plan_id: str | None = None
    confirmed_ask_candidate_id: str | None = None


# -----------------------------------------------------------------------------
# API Routes
# -----------------------------------------------------------------------------
@app.post("/v1/product/requests")
async def start_product_request(req: ProductRequest):
    try:
        return await product_service.start(req.message)
    except MireyeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SandboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/product/requests/{request_id}/select")
async def select_product_candidate(request_id: str, req: ProductSelectionRequest):
    try:
        return await product_service.select(request_id, req.candidate_index)
    except ProductRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MireyeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/v1/product/requests/{request_id}/confirm")
async def confirm_product_request(request_id: str, req: ProductConfirmationRequest):
    try:
        return await product_service.confirm(request_id, req.confirmed)
    except ProductRequestError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ConfirmationRequired, ParcelIdentityError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MireyeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (SandboxError, SceneValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/diligence/projects")
async def create_diligence_project(req: DiligenceProjectCreateRequest):
    try:
        return diligence_service.create_project(workspace_id=req.workspace_id, message=req.message, candidates=req.candidates)
    except DiligenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/diligence/projects/{project_id}")
async def get_diligence_project(project_id: str):
    try:
        return diligence_service.get(project_id)
    except DiligenceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/v1/diligence/projects/{project_id}/candidates")
async def list_diligence_candidates(project_id: str, cursor: str | None = None, limit: int = Query(default=25, ge=1, le=100)):
    try:
        return diligence_service.candidate_page(project_id, cursor=cursor, limit=limit)
    except DiligenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/plan")
async def plan_diligence_project(project_id: str, req: DiligencePlanRequest):
    try:
        return await diligence_service.resolve_and_quote(project_id, confirmed_resolution=req.confirmed_resolution)
    except ConfirmationRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (DiligenceError, SandboxError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/enrich")
async def enrich_diligence_project(project_id: str, req: DiligenceEnrichmentConfirmRequest):
    try:
        return await diligence_service.confirm_and_fetch(project_id, req.spend_plan_id, confirmed=req.confirmed)
    except ConfirmationRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (DiligenceError, SandboxError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/candidates/{candidate_id}/select")
async def select_diligence_candidate_resolution(project_id: str, candidate_id: str, req: DiligenceResolutionSelectionRequest):
    try:
        return await diligence_service.select_resolution(project_id, candidate_id, req.option_index)
    except (DiligenceError, SandboxError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/diligence/projects/{project_id}/candidates/{candidate_id}/open")
async def open_diligence_candidate(project_id: str, candidate_id: str):
    try:
        return diligence_service.open_candidate(project_id, candidate_id)
    except DiligenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/watch")
async def watch_diligence_project(project_id: str, req: DiligenceWatchRequest):
    try:
        return diligence_service.set_watch(project_id, enabled=req.enabled)
    except DiligenceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/compare")
async def compare_diligence_candidates(project_id: str, req: DiligenceCompareRequest):
    try:
        return diligence_service.compare_candidates(project_id, req.candidate_ids)
    except DiligenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/check-now")
async def check_diligence_project(project_id: str):
    try:
        return diligence_service.check_now(project_id)
    except (DiligenceError, SandboxError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/candidates/{candidate_id}/refresh/quote")
async def quote_diligence_candidate_refresh(project_id: str, candidate_id: str):
    try:
        return await diligence_service.quote_candidate_refresh(project_id, candidate_id)
    except (DiligenceError, SandboxError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/candidates/{candidate_id}/refresh")
async def refresh_diligence_candidate(project_id: str, candidate_id: str, req: DiligenceRefreshConfirmRequest):
    try:
        return await diligence_service.confirm_candidate_refresh(project_id, candidate_id, req.spend_plan_id, confirmed=req.confirmed)
    except ConfirmationRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (DiligenceError, SandboxError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/chat")
async def chat_diligence_project(project_id: str, req: DiligenceChatRequest):
    try:
        return await sandbox_agent.chat_project(
            project_id, req.session_id, req.message,
            confirmed_resolution_project_id=req.confirmed_resolution_project_id,
            confirmed_enrichment_plan_id=req.confirmed_enrichment_plan_id,
            confirmed_refresh_plan_id=req.confirmed_refresh_plan_id,
            confirmed_ask_candidate_id=req.confirmed_ask_candidate_id,
        )
    except ModelUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (DiligenceError, ToolValidationError, ConfirmationRequired) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


@app.post("/v1/sandbox/site/resolve")
async def resolve_sandbox_site(req: SandboxResolveRequest):
    try:
        result = await sandbox_service.resolve(
            input=req.input,
            kind=req.kind,
            lat=req.lat,
            lng=req.lng,
        )
    except MireyeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SandboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result["status"] == "ambiguous":
        raise HTTPException(status_code=409, detail=result)
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail=result)
    return result


@app.post("/v1/sandbox/site/quote")
async def quote_sandbox_site(req: SandboxQuoteRequest):
    try:
        return await sandbox_service.quote(lat=req.lat, lng=req.lng)
    except MireyeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SandboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/sandbox/site/snapshots")
async def create_sandbox_snapshot(req: SandboxSnapshotRequest):
    try:
        return await sandbox_service.create_snapshot(
            workspace_id=req.workspace_id,
            lat=req.lat,
            lng=req.lng,
            confirmed=req.confirmed,
        )
    except ConfirmationRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ParcelIdentityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MireyeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SandboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/sandbox/site/snapshots/{snapshot_id}/scene")
async def get_sandbox_scene(snapshot_id: str):
    try:
        return sandbox_service.scene_state(snapshot_id)
    except SandboxError as exc:
        status_code = 404 if str(exc) == "SiteSnapshot not found." else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.get("/v1/sandbox/site/snapshots/{snapshot_id}")
async def get_sandbox_snapshot(snapshot_id: str):
    snapshot = sandbox_service.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="SiteSnapshot not found.")
    return snapshot


@app.get("/v1/sandbox/site/{snapshot_id}/freshness")
async def get_sandbox_freshness(snapshot_id: str):
    try:
        return sandbox_service.freshness_status(snapshot_id)
    except SandboxError as exc:
        raise HTTPException(status_code=404 if str(exc) == "SiteSnapshot not found." else 400, detail=str(exc)) from exc


@app.post("/v1/sandbox/site/{snapshot_id}/refresh/quote")
async def quote_sandbox_refresh(snapshot_id: str):
    try:
        return await sandbox_service.quote_refresh(snapshot_id)
    except MireyeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SandboxError as exc:
        raise HTTPException(status_code=404 if str(exc) == "SiteSnapshot not found." else 400, detail=str(exc)) from exc


@app.post("/v1/sandbox/site/refresh/{spend_plan_id}/confirm")
async def confirm_sandbox_refresh(spend_plan_id: str, req: SandboxRefreshConfirmRequest):
    try:
        return await sandbox_service.confirm_and_refresh(spend_plan_id, confirmed_by_application=req.confirmed)
    except ConfirmationRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ParcelIdentityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MireyeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SandboxError as exc:
        raise HTTPException(status_code=404 if str(exc) == "SiteSnapshot not found." else 400, detail=str(exc)) from exc


@app.post("/v1/sandbox/site/{snapshot_id}/evaluate")
async def evaluate_sandbox_site(snapshot_id: str, req: SandboxEvaluationRequest):
    snapshot = sandbox_service.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="SiteSnapshot not found.")
    try:
        return evaluate_site(snapshot, req.scene_state, req.requested_constraints)
    except SceneValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/sandbox/{snapshot_id}/chat")
async def chat_with_sandbox(snapshot_id: str, req: SandboxChatRequest):
    snapshot = sandbox_service.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="SiteSnapshot not found.")
    try:
        return await sandbox_agent.chat(
            snapshot, req.session_id, req.message, workspace_id=req.workspace_id,
            scenario_id=req.scenario_id, world_snapshot_id=req.world_snapshot_id,
            confirmed_refresh_plan_id=req.confirmed_refresh_plan_id,
        )
    except ModelUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ToolValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ScenarioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/sandbox/{snapshot_id}/scenarios")
async def create_sandbox_scenario(snapshot_id: str, req: SandboxScenarioCreateRequest):
    snapshot = sandbox_service.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="SiteSnapshot not found.")
    try:
        scene_state = req.scene_state or sandbox_service.scene_state(snapshot_id)
        if req.world_snapshot_id is not None:
            scene_state = dict(scene_state)
            scene_state["world_snapshot_id"] = req.world_snapshot_id
        return scenario_service.create(
            snapshot, workspace_id=req.workspace_id, user_intent=req.user_intent,
            scene_state=scene_state, requested_constraints=req.requested_constraints, model_id=req.model_id,
        )
    except ScenarioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/sandbox/world-snapshots")
async def create_world_snapshot(req: WorldSnapshotCreateRequest):
    try:
        snapshot = await world_service.create(
            site_snapshot_id=req.site_snapshot_id, buffer_m=req.aoi_buffer_m,
            requested_layers=req.requested_layers,
            options={"prefer_1m": req.prefer_1m, "overture_release": req.overture_release},
        )
        return world_service.public(snapshot)
    except WorldError as exc:
        raise HTTPException(status_code=404 if str(exc) == "SiteSnapshot not found." else 400, detail=str(exc)) from exc


@app.get("/v1/sandbox/world-snapshots/{world_snapshot_id}")
async def get_world_snapshot(world_snapshot_id: str):
    snapshot = world_service.get(world_snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="WorldSnapshot not found.")
    return world_service.public(snapshot)


@app.get("/v1/sandbox/world-snapshots/{world_snapshot_id}/terrain/{z}/{x}/{y}")
async def get_world_terrain_tile(world_snapshot_id: str, z: int, x: int, y: int):
    snapshot = world_service.get(world_snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="WorldSnapshot not found.")
    try:
        return FileResponse(world_service.artifact_for_tile(snapshot, z, x, y), media_type="image/png")
    except WorldError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/v1/sandbox/world-snapshots/{world_snapshot_id}/roads")
async def get_world_roads(world_snapshot_id: str):
    snapshot = world_service.get(world_snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="WorldSnapshot not found.")
    try:
        return FileResponse(world_service.road_artifact(snapshot), media_type="application/geo+json")
    except WorldError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/v1/sandbox/scenarios/{scenario_id}/branch")
async def branch_sandbox_scenario(scenario_id: str, req: SandboxScenarioBranchRequest):
    try:
        return scenario_service.branch(scenario_id, user_intent=req.user_intent, model_id=req.model_id)
    except ScenarioError as exc:
        raise HTTPException(status_code=404 if str(exc) == "Scenario was not found." else 400, detail=str(exc)) from exc


@app.get("/v1/sandbox/scenarios/{scenario_id}")
async def get_sandbox_scenario(scenario_id: str, revision: int | None = Query(default=None, ge=1)):
    try:
        return scenario_service.get(scenario_id, revision)
    except ScenarioError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/v1/sandbox/scenarios/{scenario_id}/revisions")
async def list_sandbox_scenario_revisions(scenario_id: str):
    try:
        return scenario_service.list_revisions(scenario_id)
    except ScenarioError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/v1/sandbox/compare")
async def compare_sandbox_scenarios(req: SandboxCompareRequest):
    try:
        return scenario_service.compare(
            req.left_scenario_id, req.right_scenario_id,
            left_revision=req.left_revision, right_revision=req.right_revision,
        )
    except ScenarioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
        error = dossier.get("error", {"code": "not_found", "message": "Parcel not found"})
        status_code = {"invalid_location": 400, "ambiguous_address": 409}.get(error.get("code"), 404)
        raise HTTPException(status_code=status_code, detail=error)

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
