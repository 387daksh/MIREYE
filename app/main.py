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

import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.discovery.confidence import rank_shortlist_by_confidence, score_site
from app.diligence import DiligenceError, DiligenceService
from app.discovery.screen import DiscoveryEngine
from app.discovery.spatial import SpatialDiscovery
from app.grid.ici import ICIEngine
from app.infrastructure.config import get_settings
from app.infrastructure.cache import RedisCache
from app.infrastructure.auth import LocalAuthProvider, RequestContext, request_context
from app.infrastructure.db import workspace_store_for
from app.infrastructure.observability import configure_observability, install_http_observability
from app.infrastructure.storage import artifact_store_for
from app.ai.evaluation import VerificationEngine
from app.ai.memory import DocumentMemoryService, EvidenceGraphRetriever, ProjectMemoryStore
from app.ai.planners import IntentInterpreter, TaskGraphPlanner
from app.ai.providers import OpenAIEmbeddingProvider, OpenAIStructuredModelProvider
from app.ai.runtime import OrchestrationEngine, OrchestrationError, build_project_tool_registry
from app.ai.schemas.orchestration import MemoryKind, OrchestrationRun
from app.application.orchestration.temporal import TemporalOrchestrationExecutor
from app.mireye_client import MireyeClient
from app.product import ProductExperienceService, ProductRequestError
from app.project_readiness import AuthoritativeSourceService
from app.sandbox_agent import ModelUnavailableError, SandboxAgent, ToolValidationError
from app.sandbox_evaluator import SceneValidationError, evaluate_site
from app.sandbox_scenarios import DEFAULT_SCENARIO_CONSTRAINTS, ScenarioError, ScenarioService
from app.sandbox import BESS_SITING_PRESET, ConfirmationRequired, MireyeUnavailableError, ParcelIdentityError, SandboxError, SiteSnapshotService
from app.workspace.engine import WorkspaceEngine
from app.world import WorldError, WorldSnapshotService


@asynccontextmanager
async def application_lifespan(_app: FastAPI):
    settings = get_settings()
    configure_observability(settings)
    workspace_store.initialize()
    try:
        await cache.ping()
    except Exception:
        if settings.app_env == "production":
            raise
    try:
        yield
    finally:
        await cache.close()

app = FastAPI(
    title="Mireye Agentic Siting & Spatial Intelligence Platform",
    description="Transforms Mireye from a single-point verification tool into an autonomous enterprise site origination engine.",
    version="1.0.0",
    lifespan=application_lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-Mireye-User-Id", "X-Mireye-Organization-Id", "X-Mireye-Workspace-Id", "X-Mireye-Roles"],
)
install_http_observability(app)
auth_provider = LocalAuthProvider()

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
_settings = get_settings()
cache = RedisCache(_settings.redis_url)
temporal_executor = TemporalOrchestrationExecutor(_settings.temporal_target, _settings.temporal_namespace, _settings.temporal_task_queue) if _settings.workflow_backend == "temporal" and _settings.temporal_target else None
workspace_store = workspace_store_for(_settings)
artifact_store = artifact_store_for(_settings)
workspace_engine = WorkspaceEngine(store=workspace_store, client=mireye_client)
world_service = WorldSnapshotService(workspace_store, artifact_store)
scenario_service = ScenarioService(workspace_store, worlds=world_service)
sandbox_service = SiteSnapshotService(store=workspace_store, client=mireye_client, scenarios=scenario_service)
diligence_service = DiligenceService(workspace_store, sandbox_service, world_service, sources=AuthoritativeSourceService())
sandbox_agent = SandboxAgent(scenarios=scenario_service, intelligence=sandbox_service, diligence=diligence_service)
orchestration_model = OpenAIStructuredModelProvider()
project_memory = ProjectMemoryStore(workspace_store)
evidence_graph = EvidenceGraphRetriever(workspace_store)
document_memory = DocumentMemoryService(evidence_graph.graph, artifact_store, OpenAIEmbeddingProvider())
orchestration_tools = build_project_tool_registry(diligence_service, scenario_service, project_memory)
orchestration_engine = OrchestrationEngine(
    diligence_service,
    IntentInterpreter(orchestration_model),
    TaskGraphPlanner(orchestration_model),
    orchestration_tools,
    project_memory,
    VerificationEngine(),
    document_memory,
)
product_service = ProductExperienceService(sandbox_service, world_service)
spatial_discovery = SpatialDiscovery()
ici_engine = ICIEngine()
discovery_engine = DiscoveryEngine(client=mireye_client)


@app.middleware("http")
async def authorize_application_request(request, call_next):
    try:
        context = await auth_provider.authenticate(request.headers)
    except (PermissionError, ValueError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    request.state.context = context
    permission = _permission_for(request.method, request.url.path)
    if permission and not context.allows(permission):
        return JSONResponse({"detail": f"Missing permission: {permission}"}, status_code=403)
    owner = await _request_workspace(request)
    if context.workspace_id is not None and owner is not None and owner != context.workspace_id:
        return JSONResponse({"detail": "Resource does not belong to the request workspace."}, status_code=403)
    return await call_next(request)


def _permission_for(method: str, path: str) -> str | None:
    if not path.startswith("/v1/"):
        return None
    if "/orchestration" in path:
        return "project:read" if method == "GET" else "orchestration:run"
    if "/rfis" in path:
        if path.endswith("/approve"):
            return "rfi:approve"
        if path.endswith("/send"):
            return "rfi:send"
        return "rfi:create"
    if "/sources/refresh" in path or "/refresh" in path or path.endswith("/enrich") or path.endswith("/check-now"):
        return "evidence:refresh"
    if "/evidence" in path or "/intelligence" in path or "/power-readiness" in path or "/entitlement" in path:
        return "evidence:read"
    if path.endswith("/compare"):
        return "scenario:read"
    if "/scenarios" in path:
        return "scenario:read" if method == "GET" else "scenario:mutate"
    if "/world-snapshots" in path:
        return "evidence:read" if method == "GET" else "evidence:refresh"
    if path.startswith("/v1/diligence/projects"):
        return "project:read" if method == "GET" else "project:write"
    if path.startswith("/v1/workspace"):
        return "project:read" if method == "GET" else "workspace:admin"
    if path.startswith("/v1/sandbox"):
        return "evidence:read" if method == "GET" else "scenario:mutate"
    return None


def _resource_workspace(path: str) -> str | None:
    match = re.search(r"/diligence/projects/([^/]+)", path)
    if match:
        project = diligence_service.store.get_diligence_project(match.group(1))
        return project.get("workspace_id") if project else None
    match = re.search(r"/workspace/([^/]+)", path)
    if match and match.group(1) not in {"open", "observe"}:
        return match.group(1)
    match = re.search(r"/world-snapshots/([^/]+)", path)
    if match and (world := world_service.store.get_world_snapshot(match.group(1))):
        snapshot = world_service.store.get_site_snapshot(world["site_snapshot_id"])
        return snapshot.get("workspace_id") if snapshot else None
    match = re.search(r"/scenarios/([^/]+)", path)
    if match and (scenario := scenario_service.store.get_scenario_version(match.group(1))):
        return scenario.get("workspace_id")
    match = re.search(r"/site/(?:snapshots/)?([^/]+)", path)
    if match and (snapshot := sandbox_service.store.get_site_snapshot(match.group(1))):
        return snapshot.get("workspace_id")
    match = re.search(r"/sandbox/([^/]+)", path)
    if match and match.group(1) not in {"compare", "world-snapshots", "scenarios"}:
        snapshot = sandbox_service.store.get_site_snapshot(match.group(1))
        return snapshot.get("workspace_id") if snapshot else None
    return None


async def _request_workspace(request) -> str | None:
    owner = _resource_workspace(request.url.path)
    if owner is not None or request.method == "GET":
        return owner
    try:
        body = await request.json()
    except Exception:
        return None
    if isinstance(body, dict) and body.get("workspace_id"):
        return str(body["workspace_id"])
    if isinstance(body, dict) and body.get("site_snapshot_id"):
        snapshot = sandbox_service.store.get_site_snapshot(str(body["site_snapshot_id"]))
        return snapshot.get("workspace_id") if snapshot else None
    if isinstance(body, dict) and body.get("left_scenario_id"):
        scenario = scenario_service.store.get_scenario_version(str(body["left_scenario_id"]))
        return scenario.get("workspace_id") if scenario else None
    return None


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


class OrchestrationRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)


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
    requested_layers: list[Literal["terrain", "roads", "buildings", "water", "land_cover", "transmission"]] = Field(default_factory=lambda: ["terrain", "roads"])
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
    retry_reason: str | None = Field(default=None, min_length=8, max_length=500)


class DiligenceEnrichmentConfirmRequest(BaseModel):
    spend_plan_id: str
    confirmed: bool = False


class DiligenceSnapshotLinkRequest(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=128)


class DiligenceResolutionSelectionRequest(BaseModel):
    option_index: int = Field(ge=0)


class DiligenceWatchRequest(BaseModel):
    enabled: bool = True


class DiligenceCheckNowRequest(BaseModel):
    candidate_id: str | None = None
    spend_plan_id: str | None = None
    confirmed: bool = False


class DiligenceCompareRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=2, max_length=100)


class DiligenceRefreshConfirmRequest(BaseModel):
    spend_plan_id: str
    confirmed: bool = False


class DiligenceWorldSnapshotRequest(BaseModel):
    requested_layers: list[str] | None = None


class DiligenceChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(min_length=1, max_length=128)
    confirmed_resolution_project_id: str | None = None
    confirmed_enrichment_plan_id: str | None = None
    confirmed_refresh_plan_id: str | None = None
    confirmed_ask_candidate_id: str | None = None


class DiligenceRfiDraftRequest(BaseModel):
    action_id: str = Field(min_length=1, max_length=128)
    generated_request: str = Field(min_length=40, max_length=8000)


class DiligenceRfiUpdateRequest(BaseModel):
    generated_request: str | None = Field(default=None, min_length=40, max_length=8000)
    recipient_name: str | None = Field(default=None, max_length=200)
    recipient_contact: str | None = Field(default=None, max_length=300)
    internal_notes: str | None = Field(default=None, max_length=2000)


class DiligenceRfiApprovalRequest(BaseModel):
    approved_by: str = Field(min_length=2, max_length=200)


class DiligenceRfiSentRequest(BaseModel):
    sent_by: str = Field(min_length=2, max_length=200)
    delivery_reference: str | None = Field(default=None, max_length=500)


class DiligenceRfiResponseRequest(BaseModel):
    details: str = Field(min_length=1, max_length=8000)
    provider: str = Field(min_length=2, max_length=200)
    source_url: str | None = Field(default=None, max_length=2048)
    source_type: Literal["document", "email", "study", "note"] = "email"


class DiligenceUserEvidenceRequest(BaseModel):
    requirement_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=3, max_length=200)
    details: str = Field(min_length=1, max_length=8000)
    provider: str = Field(min_length=2, max_length=200)
    source_url: str | None = Field(default=None, max_length=2048)
    source_type: Literal["document", "email", "study", "note"] = "document"


class AgentDecisionAnswerRequest(BaseModel):
    resume_token: str = Field(min_length=1, max_length=128)
    option_id: str | None = Field(default=None, max_length=128)
    option_ids: list[str] | None = None
    value: Any = None
    text: str | None = Field(default=None, max_length=4000)
    cancelled: bool = False


def require_orchestration_workspace(project_id: str, context: RequestContext, permission: str) -> None:
    project = diligence_service.get(project_id)
    if not context.allows(permission):
        raise HTTPException(status_code=403, detail=f"Missing permission: {permission}")
    if context.workspace_id is not None and project["workspace_id"] != context.workspace_id:
        raise HTTPException(status_code=403, detail="Project does not belong to the request workspace.")


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
        return await diligence_service.resolve_and_quote(
            project_id, confirmed_resolution=req.confirmed_resolution, retry_reason=req.retry_reason,
        )
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


@app.post("/v1/diligence/projects/{project_id}/candidates/{candidate_id}/snapshot-link")
async def link_diligence_candidate_snapshot(project_id: str, candidate_id: str, req: DiligenceSnapshotLinkRequest):
    try:
        return diligence_service.link_existing_snapshot(project_id, candidate_id, req.snapshot_id)
    except (DiligenceError, SandboxError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/decisions/{decision_id}/answer")
async def answer_diligence_decision(project_id: str, decision_id: str, req: AgentDecisionAnswerRequest):
    try:
        if req.text is not None and not req.cancelled:
            return await sandbox_agent.interpret_project_decision_answer(
                project_id, decision_id, resume_token=req.resume_token, text=req.text,
            )
        return await diligence_service.answer_decision(
            project_id, decision_id, resume_token=req.resume_token, option_id=req.option_id,
            option_ids=req.option_ids, value=req.value, cancelled=req.cancelled,
        )
    except (DiligenceError, ConfirmationRequired, SandboxError, ToolValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/diligence/projects/{project_id}/candidates/{candidate_id}/open")
async def open_diligence_candidate(project_id: str, candidate_id: str):
    try:
        return diligence_service.open_candidate(project_id, candidate_id)
    except DiligenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/candidates/{candidate_id}/world-snapshot")
async def build_diligence_candidate_world(
    project_id: str, candidate_id: str, req: DiligenceWorldSnapshotRequest,
):
    try:
        return await diligence_service.build_world_snapshot(
            project_id, candidate_id, requested_layers=req.requested_layers,
        )
    except (DiligenceError, WorldError) as exc:
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
async def check_diligence_project(project_id: str, req: DiligenceCheckNowRequest | None = None):
    try:
        req = req or DiligenceCheckNowRequest()
        return await diligence_service.check_now_workflow(
            project_id, candidate_id=req.candidate_id, spend_plan_id=req.spend_plan_id, confirmed=req.confirmed,
        )
    except ConfirmationRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (DiligenceError, SandboxError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/diligence/projects/{project_id}/changes")
async def get_diligence_project_changes(
    project_id: str, site: str | None = None, severity: str | None = None,
    source: str | None = None, change_type: str | None = None,
    affected_requirement: str | None = None, since: float | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    try:
        return diligence_service.changes(
            project_id, site_id=site, significance=severity, source=source, change_type=change_type,
            requirement_id=affected_requirement, since=since, limit=limit,
        )
    except DiligenceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/v1/diligence/projects/{project_id}/intelligence")
async def get_diligence_project_intelligence(project_id: str):
    try:
        return diligence_service.evaluate_evidence_coverage(project_id)
    except DiligenceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/v1/diligence/projects/{project_id}/memory/search")
async def search_project_memory(
    project_id: str, query: str = Query(min_length=1, max_length=1000), limit: int = Query(default=12, ge=1, le=50), as_of: float | None = Query(default=None),
    context: RequestContext = Depends(request_context),
):
    """Hybrid structured/semantic recall that always returns provenance references."""
    try:
        require_orchestration_workspace(project_id, context, "project:read")
        result = await document_memory.retrieve(project_id, query, limit=limit, as_of=as_of)
        return {"project_id": project_id, "records": result["graph_records"], "documents": result["document_chunks"], "vector_queries": result["vector_queries"], "as_of": as_of}
    except (DiligenceError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/v1/diligence/projects/{project_id}/memory/requirements/{requirement_id}")
async def trace_project_claim(
    project_id: str, requirement_id: str, as_of: float | None = Query(default=None),
    context: RequestContext = Depends(request_context),
):
    try:
        require_orchestration_workspace(project_id, context, "evidence:read")
        claims = evidence_graph.find_claims_for_requirement(project_id, requirement_id, as_of=as_of)
        return {"project_id": project_id, "requirement_id": requirement_id, "claims": claims, "evidence": [
            item for claim in claims for item in evidence_graph.find_supporting_evidence(project_id, claim["claim_id"])
        ]}
    except (DiligenceError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/v1/diligence/projects/{project_id}/evidence-plan")
async def get_diligence_project_evidence_plan(project_id: str):
    try:
        return await diligence_service.plan_project_evidence(project_id)
    except (DiligenceError, SandboxError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/sites/{site_id}/sources/refresh")
async def refresh_diligence_site_sources(project_id: str, site_id: str):
    try:
        return await diligence_service.refresh_authoritative_sources(project_id, site_id)
    except DiligenceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/sites/{site_id}/user-evidence")
async def add_diligence_user_evidence(
    project_id: str,
    site_id: str,
    req: DiligenceUserEvidenceRequest,
    context: RequestContext = Depends(request_context),
):
    try:
        require_orchestration_workspace(project_id, context, "project:write")
        result = diligence_service.add_user_evidence(project_id, site_id, **req.model_dump())
        evidence = result["evidence"]
        memory = project_memory.put_record(
            project_id,
            MemoryKind.EVIDENCE,
            evidence,
            {
                "source": "user",
                "provider": evidence["provider"],
                "source_url": evidence.get("source_url"),
                "human_review_required": True,
            },
        )
        return {**result, "memory_id": memory.memory_id}
    except (DiligenceError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/diligence/projects/{project_id}/sites/{site_id}/power-readiness")
async def get_diligence_power_readiness(project_id: str, site_id: str):
    try:
        return diligence_service.power_readiness(project_id, site_id)
    except DiligenceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/v1/diligence/projects/{project_id}/sites/{site_id}/entitlement")
async def get_diligence_entitlement(project_id: str, site_id: str):
    try:
        return diligence_service.entitlement_state(project_id, site_id)
    except DiligenceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/next-actions")
async def prioritize_diligence_project_actions(project_id: str):
    try:
        return diligence_service.next_actions(project_id)
    except DiligenceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/rfis")
async def create_diligence_project_rfi(project_id: str, req: DiligenceRfiDraftRequest):
    try:
        return diligence_service.create_rfi_draft(project_id, req.action_id, req.generated_request)
    except DiligenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/v1/diligence/projects/{project_id}/rfis/{rfi_id}")
async def update_diligence_project_rfi(project_id: str, rfi_id: str, req: DiligenceRfiUpdateRequest):
    try:
        return diligence_service.update_rfi_draft(project_id, rfi_id, **req.model_dump())
    except DiligenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/rfis/{rfi_id}/approve")
async def approve_diligence_project_rfi(project_id: str, rfi_id: str, req: DiligenceRfiApprovalRequest):
    try:
        return diligence_service.approve_rfi(project_id, rfi_id, req.approved_by)
    except DiligenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/rfis/{rfi_id}/sent")
async def mark_diligence_project_rfi_sent(project_id: str, rfi_id: str, req: DiligenceRfiSentRequest):
    try:
        return diligence_service.mark_rfi_sent(project_id, rfi_id, req.sent_by, req.delivery_reference)
    except DiligenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/diligence/projects/{project_id}/rfis/{rfi_id}/response")
async def record_diligence_project_rfi_response(
    project_id: str,
    rfi_id: str,
    req: DiligenceRfiResponseRequest,
    context: RequestContext = Depends(request_context),
):
    try:
        require_orchestration_workspace(project_id, context, "project:write")
        result = diligence_service.record_rfi_response(project_id, rfi_id, **req.model_dump())
        evidence = result["evidence"]
        memory = project_memory.put_record(
            project_id,
            MemoryKind.EVIDENCE,
            evidence,
            {
                "source": "user",
                "provider": evidence["provider"],
                "source_url": evidence.get("source_url"),
                "rfi_id": rfi_id,
                "human_review_required": True,
            },
        )
        return {**result, "memory_id": memory.memory_id}
    except (DiligenceError, ValueError) as exc:
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


@app.post("/v1/ai/projects/{project_id}/orchestrate")
async def orchestrate_project(project_id: str, req: OrchestrationRequest, context: RequestContext = Depends(request_context)):
    try:
        require_orchestration_workspace(project_id, context, "orchestration:run")
        if temporal_executor:
            import uuid
            run_id = f"run_{uuid.uuid4().hex}"
            await temporal_executor.start(project_id, req.message, run_id)
            return {"run": {"run_id": run_id, "project_id": project_id, "status": "RUNNING"}, "decision_request": None}
        return await orchestration_engine.run(project_id, req.message)
    except ModelUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (DiligenceError, OrchestrationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/ai/projects/{project_id}/orchestration/{run_id}", response_model=OrchestrationRun)
async def get_orchestration_run(project_id: str, run_id: str, context: RequestContext = Depends(request_context)):
    try:
        require_orchestration_workspace(project_id, context, "project:read")
        return orchestration_engine.get_run(project_id, run_id).model_dump(mode="json")
    except (DiligenceError, OrchestrationError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/v1/ai/projects/{project_id}/orchestration/{run_id}/resume")
async def resume_orchestration_run(project_id: str, run_id: str, context: RequestContext = Depends(request_context)):
    try:
        require_orchestration_workspace(project_id, context, "orchestration:run")
        if temporal_executor:
            run = orchestration_engine.get_run(project_id, run_id)
            if run.status != "WAITING_FOR_DECISION":
                raise OrchestrationError("Only a run waiting for a user decision can resume.")
            if diligence_service.get(project_id).get("active_decision"):
                raise OrchestrationError("The active user decision must be answered before resume.")
            await temporal_executor.signal_decision(run_id)
            return {"run_id": run_id, "status": "RESUMED"}
        return await orchestration_engine.resume(project_id, run_id)
    except (DiligenceError, OrchestrationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/ai/projects/{project_id}/orchestration/{run_id}/events")
async def stream_orchestration_events(project_id: str, run_id: str, context: RequestContext = Depends(request_context)):
    require_orchestration_workspace(project_id, context, "project:read")
    async def events():
        sequence = 0
        while True:
            try:
                run = orchestration_engine.get_run(project_id, run_id)
            except OrchestrationError:
                yield "event: error\ndata: {\"message\":\"Run not available yet\"}\n\n"
                await __import__("asyncio").sleep(1)
                continue
            for item in run.events[sequence:]:
                sequence = item["sequence"]
                yield f"event: {item['type']}\ndata: {__import__('json').dumps(item)}\n\n"
            if run.status in {"COMPLETED", "FAILED", "CANCELLED"}:
                break
            await __import__("asyncio").sleep(1)
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "mireye-platform",
        "mode": mireye_client.mode,
    }


@app.get("/health/live")
async def health_live():
    return {"status": "live", "service": "mireye-platform"}


@app.get("/health/ready")
async def health_ready():
    dependencies: dict[str, bool] = {}
    try:
        await asyncio.to_thread(_database_ready)
        dependencies["database"] = True
    except Exception:
        dependencies["database"] = False
    try:
        await cache.ping()
        dependencies["redis"] = True
    except Exception:
        dependencies["redis"] = False
    if _settings.artifact_store_backend == "s3":
        try:
            await asyncio.to_thread(artifact_store.client.head_bucket, Bucket=artifact_store.bucket)
            dependencies["artifact_store"] = True
        except Exception:
            dependencies["artifact_store"] = False
    if _settings.workflow_backend == "temporal" and _settings.temporal_target:
        dependencies["temporal"] = await _tcp_ready(_settings.temporal_target, 7233)
    ready = all(dependencies.values())
    return JSONResponse({"status": "ready" if ready else "unavailable", "dependencies": dependencies}, status_code=200 if ready else 503)


def _database_ready() -> None:
    with workspace_store._get_conn() as connection:
        connection.execute("SELECT 1").fetchone()


async def _tcp_ready(target: str, default_port: int) -> bool:
    host, separator, port = target.rpartition(":")
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host if separator else target, int(port) if separator else default_port), timeout=1)
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, TimeoutError):
        return False


@app.get("/v1/usage")
async def get_usage():
    return await mireye_client.usage()


@app.get("/v1/meta/fields")
async def get_meta_fields():
    return await cache.get_or_set("mireye:meta:fields:v1", 3600, mireye_client.meta_fields)


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


@app.get("/v1/sandbox/site/{snapshot_id}/intelligence-plan")
async def get_sandbox_intelligence_plan(snapshot_id: str):
    try:
        return await sandbox_service.project_intelligence_plan(snapshot_id)
    except MireyeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SandboxError as exc:
        raise HTTPException(status_code=404 if str(exc) == "SiteSnapshot not found." else 400, detail=str(exc)) from exc


@app.post("/v1/sandbox/site/{snapshot_id}/refresh/quote")
async def quote_sandbox_refresh(
    snapshot_id: str,
    profile: Literal["bess_siting"] | None = Query(default=None),
):
    try:
        return await sandbox_service.quote_refresh(snapshot_id, project_profile=BESS_SITING_PRESET if profile else None)
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
        return evaluate_site(snapshot, req.scene_state, req.requested_constraints or DEFAULT_SCENARIO_CONSTRAINTS)
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


@app.get("/v1/sandbox/world-snapshots/{world_snapshot_id}/layers/{layer_name}")
async def get_world_vector_layer(world_snapshot_id: str, layer_name: str):
    snapshot = world_service.get(world_snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="WorldSnapshot not found.")
    try:
        return FileResponse(world_service.vector_artifact(snapshot, layer_name), media_type="application/geo+json")
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
