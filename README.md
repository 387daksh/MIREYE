# MIREYE Site Intelligence and Sandbox

MIREYE is an evidence-grounded site-diligence and conceptual-development platform. It accepts customer-supplied addresses, coordinates, and supported APNs; resolves and enriches those candidates through MIREYE; evaluates supported constraints deterministically; and presents a selected parcel in a terrain and road-aware sandbox.

The product is deliberately conservative about what evidence proves. MIREYE distinguishes provider observations, deterministic calculations, user or agent proposals, and future generated media. It does not claim that a conceptual layout is engineering design, that proximity proves utility capacity, or that a point observation proves a parcel-wide condition.

![MIREYE request experience](docs/screenshots/mireye-home.png)

![MIREYE site sandbox](docs/screenshots/mireye-sandbox.png)

## Current product surface

The primary workflow is supplied-candidate diligence:

1. A user submits a project brief and candidate locations.
2. The application compiles typed constraints and reports unsupported semantics as unresolved.
3. Candidates are resolved and an exact MIREYE field and credit plan is shown.
4. The user explicitly confirms paid enrichment.
5. The application stores immutable site snapshots, evaluates supported predicates, and ranks candidates.
6. A selected site can be opened with USGS terrain, Overture roads, source evidence, and a conceptual proposed-development scene.
7. The user or constrained agent can create, evaluate, branch, and compare scenario revisions.

The application also contains compatibility and prototype surfaces:

- `/v1/ask` performs point lookup and fact verification with provenance.
- `/v1/screen` performs legacy prototype discovery against the bundled DuckDB/GeoParquet dataset.
- `/v1/grid` exposes the existing interconnection/capacity-intelligence surface.
- `/v1/workspace/*` provides the existing workspace observation, state, invalidation, and replay APIs.

`/v1/screen` is not statewide inverse parcel discovery and is not part of the live supplied-candidate diligence workflow.

## Evidence and safety model

| Label | Meaning | Authority |
| --- | --- | --- |
| `OBSERVED` | MIREYE or source-backed parcel facts and geometry | Authoritative input for supported calculations |
| `DERIVED` | Deterministic calculations from observations | Evaluator output |
| `PROPOSED` | User- or agent-requested conceptual objects | Scenario state only |
| `GENERATED` | Reserved for future generated visual output | Never authoritative |

The model plans and explains; typed tools perform mutations; the deterministic evaluator decides `PASS`, `FAIL`, or `UNRESOLVED`. A mutation is only reported after validation and persistence. An alternative layout requires changed proposal state plus a new deterministic evaluation.

Paid provider operations are confirmation-gated. The model cannot create a confirmation identifier or spend credits silently. Provider observations and parcel geometry are retained as immutable snapshot records; refreshes create new snapshots rather than overwriting history.

## Architecture

```text
Project request + supplied candidates
    -> typed constraint compiler
    -> candidate resolution
    -> MIREYE field plan and quote
    -> explicit confirmation
    -> confirmed batch enrichment
    -> immutable SiteSnapshots
    -> deterministic evaluation and ranking
    -> selected site and WorldSnapshot
    -> MapLibre sandbox
    -> constrained agent tools
    -> persistent scenario revisions and comparisons
    -> freshness check and confirmed refresh
```

The codebase is a modular monolith. Current boundaries are:

- `app/domain/`: dependency-light projects, sites, evidence, constraints, decisions, actions, scenarios, world state, and ports.
- `app/application/`: use cases and workflow orchestration.
- `app/infrastructure/`: settings, SQLite/PostgreSQL stores, local/S3-compatible artifacts, Redis, events, telemetry, and workflow boundaries.
- `app/adapters/`: provider-facing adapter boundaries.
- `app/ai/`: structured model providers, orchestration, evaluation, evidence graph, project memory, document retrieval, and typed contracts.
- `app/static/`: same-origin FastAPI intake and sandbox pages.
- `frontend/`: Next.js/React product frontend with generated OpenAPI types.

### State and storage

- SQLite is the default local workspace store.
- PostgreSQL is selected when `DATABASE_URL` is configured; production settings reject SQLite.
- Local content-addressed files are the default world-artifact store.
- S3-compatible artifact storage is selected with `ARTIFACT_STORE_BACKEND=s3`.
- DuckDB and GeoParquet support the legacy `/v1/screen` prototype path.
- Redis provides cache and ephemeral coordination.
- Temporal provides durable workflow execution when `WORKFLOW_BACKEND=temporal`.
- NATS JetStream carries committed domain events in the containerized runtime.
- Project memory and evidence-graph records retain provenance and bounded context. Document retrieval uses stored artifacts and the configured embedding provider.

Durable project, evidence, snapshot, scenario, orchestration, and memory state is persisted by the configured workspace store. Sandbox chat turn state is process-local; accepted scenario revisions remain the durable authority.

## Requirements

- Python 3.10–3.13
- `uv` 0.8.15 or newer
- Node.js and npm for the Next.js frontend
- A MIREYE API key for live parcel intelligence
- An OpenAI API key for live agent and embedding operations
- Network access for MIREYE, OpenAI, USGS, Overture, and basemap assets

The dependency source of truth is `pyproject.toml` plus `uv.lock`.

## Local setup

Install the locked Python environment:

```powershell
uv sync --frozen --extra world --group dev --group test
```

Create `.env` from `.env.example` and provide at least:

```ini
MIREYE_API_KEY=your_mireye_api_key
OPENAI_API_KEY=your_openai_api_key
```

For a safe temporary local run:

```powershell
.\start.ps1
```

The launcher creates a unique temporary database and world-asset directory and starts the FastAPI server at `http://127.0.0.1:8000/`. It will not use `app/data/workspaces.db` unless a caller explicitly starts the application with another configuration.

Use a different port or enable reload with:

```powershell
.\start.ps1 -Port 8001 -Reload
```

The API serves the legacy same-origin pages at `/` and `/sandbox/{snapshot_id}`. Interactive API documentation is at `/docs`.

### Next.js frontend

The current product frontend is in `frontend/` and uses Next.js, React, React Query, `openapi-fetch`, TypeScript, and generated OpenAPI types.

```powershell
cd frontend
npm install
npm run dev
```

Open `http://localhost:3000/`. Set `NEXT_PUBLIC_API_URL` when the API is not at `http://localhost:8000`:

```powershell
$env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:8000"
npm run dev
```

Useful frontend commands:

```powershell
npm run typecheck
npm run lint
npm test
npm run build
```

Regenerate the typed API client after exporting OpenAPI:

```powershell
uv run python scripts/export_openapi.py
cd frontend
npm run generate:api
```

## Containerized runtime

The base Compose file runs the API, Next.js frontend, Temporal worker, event publisher, event consumer, PostgreSQL, Redis, NATS JetStream, Temporal, Temporal UI, and MinIO. The API uses PostgreSQL, S3-compatible artifacts, and Temporal in this configuration.

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

Endpoints and local service ports include:

| Service | URL or port |
| --- | --- |
| Next.js frontend | `http://localhost:3000` |
| FastAPI API | `http://localhost:8000` |
| FastAPI OpenAPI | `http://localhost:8000/docs` |
| Temporal UI | `http://localhost:8080` |
| PostgreSQL | `localhost:5432` |
| Redis | `localhost:6379` |
| NATS client/monitor | `localhost:4222` / `localhost:8222` |
| Temporal gRPC | `localhost:7233` |
| MinIO API/console | `localhost:9000` / `localhost:9001` |

The API readiness endpoint checks the configured database, Redis, artifact store, and Temporal dependency. Worker and event-service readiness checks are separate. No health endpoint calls a paid provider.

## Live demos

Run the one-parcel flow with temporary storage:

```powershell
python -m app.sandbox_demo --serve
```

Use `--confirm` only for intentional non-interactive paid confirmation:

```powershell
python -m app.sandbox_demo --confirm --serve
```

Use another input or port with:

```powershell
python -m app.sandbox_demo --address "1510 E Lookout Drive, Richardson, TX 75082" --serve --port 8001
```

For the terrain and roads demonstration:

```powershell
python scripts/live_world_snapshot_demo.py --confirm --serve
```

This performs one confirmed parcel lookup, downloads a bounded USGS 3DEP DEM, extracts release-pinned Overture road geometry for the parcel area of interest, and prints the sandbox URL. It does not establish site suitability or data-center deliverability. See [docs/sandbox-demo.md](docs/sandbox-demo.md) for the runbook.

For the full manual product path, see [docs/demo-today.md](docs/demo-today.md). For provider-specific live checks, see [docs/live-mireye.md](docs/live-mireye.md).

## Supported deterministic evidence

When matching and fresh evidence exists, the evaluator can assess:

- parcel containment, setback, footprint area, and parcel coverage;
- blocked-geometry collision;
- parcel NWI wetland acres or fraction against caller-supplied thresholds;
- resolution-point FEMA flood status;
- resolution-point slope against a supplied threshold;
- resolution-point distance to the nearest substation, transmission line, or mapped major road; and
- exact normalized raw zoning-code membership in a caller-supplied allow-list.

These remain unresolved without stronger evidence or explicit semantics:

- available grid or interconnection capacity;
- parcel-wide or footprint-wide flood exclusion from point evidence;
- parcel-wide or footprint-wide slope from point evidence;
- generic “industrial zoning” without jurisdiction-aware mapping;
- legal road access, frontage, easements, or heavy-haul suitability; and
- engineering-grade grading, foundations, power flow, water, construction, or entitlement feasibility.

MIREYE proximity fields are screening evidence, not capacity proof. Overture roads provide spatial context, not legal access proof.

## API overview

| Area | Main routes |
| --- | --- |
| Product intake | `/v1/product/requests`, `/select`, `/confirm` |
| Candidate diligence | `/v1/diligence/projects/...` |
| Project memory and evidence | `/v1/diligence/projects/{project_id}/memory/search`, evidence-plan, intelligence, RFI, and next-actions routes |
| AI orchestration | `/v1/ai/projects/{project_id}/orchestrate`, status, resume, and event stream |
| Site lifecycle | `/v1/sandbox/site/resolve`, quote, snapshots, freshness, refresh, and evaluate |
| World lifecycle | `/v1/sandbox/world-snapshots`, terrain tiles, roads, and layers |
| Sandbox agent | `/v1/sandbox/{snapshot_id}/chat` |
| Scenarios | create, branch, revisions, and compare under `/v1/sandbox` |
| Compatibility APIs | `/v1/ask`, `/v1/screen`, `/v1/grid`, `/v1/workspace/*` |
| Operations | `/health`, `/health/live`, `/health/ready`, `/v1/usage`, `/v1/meta/fields` |

Authentication is local/header-based in the current application. Requests may carry `X-Mireye-User-Id`, `X-Mireye-Organization-Id`, `X-Mireye-Workspace-Id`, and `X-Mireye-Roles`; production deployment must provide an appropriate external identity boundary.

## Configuration reference

| Variable | Purpose | Local default |
| --- | --- | --- |
| `APP_ENV` | `development`, `test`, `demo`, or `production` | `development` |
| `DATABASE_URL` | PostgreSQL connection; selects PostgreSQL storage | unset, SQLite fallback |
| `WORKSPACE_DB` | SQLite workspace path | `app/data/workspaces.db` |
| `ARTIFACT_STORE_BACKEND` | `local` or `s3` | `local` |
| `WORLD_ASSET_DIR` | World-artifact directory | `app/data/world-assets` |
| `MIREYE_API_KEY` / `MIREYE_BASE_URL` | MIREYE provider access | key unset / `https://api.mireye.com` |
| `MIREYE_ENRICHMENT_BATCH_SIZE` | Confirmed enrichment chunk size | `2` |
| `OPENAI_API_KEY` | Agent and embedding access | unset |
| `SANDBOX_AGENT_MODEL` | Sandbox agent model | `gpt-5.6-sol` |
| `SANDBOX_AGENT_REASONING_EFFORT` | Agent reasoning setting | `high` |
| `REDIS_URL` | Cache and ephemeral coordination | `redis://localhost:6379/0` |
| `WORKFLOW_BACKEND` | `local` or `temporal` | `local` |
| `TEMPORAL_TARGET` | Temporal endpoint | unset |
| `NATS_URL` / `NATS_STREAM` | Event transport | `nats://localhost:4222` / `MIREYE` |
| `CORS_ORIGINS` | Allowed browser origins | `["http://localhost:3000"]` |
| `NEXT_PUBLIC_API_URL` | Frontend API base URL | `http://localhost:8000` |

Production validation rejects missing provider credentials, SQLite authority, local artifact storage, and implicit local workflow execution. Container configuration is a development/integration foundation and should not be treated as proof of a production cutover.

## Tests and validation

Offline Python tests use temporary SQLite storage and do not call live MIREYE:

```powershell
uv run pytest -q
```

Targeted suites:

```powershell
uv run pytest -q tests/test_sandbox_agent.py tests/test_sandbox_scenarios.py
uv run pytest -q tests/unit/test_memory_graph.py tests/unit/test_document_memory.py
```

Opt-in provider validation:

```powershell
python scripts/live_mireye_demo.py
```

Container-dependent checks are marked `runtime` and require the actual PostgreSQL, Redis, NATS, Temporal, and MinIO stack. Live provider checks and browser-active behavior must be reported separately from offline/unit results.

## Known limits and roadmap boundary

- Candidate enumeration is customer-supplied; no public statewide inverse-search capability is assumed.
- The legacy `/v1/screen` dataset is prototype data and remains separate from live diligence.
- Enrichment is conservatively chunked to two locations because larger provider batches showed inconsistent failures during validation.
- Terrain may fall back from bounded 1 m acquisition to real 10 m USGS coverage.
- Proposed facilities are conceptual rectangular extrusions, not construction models.
- Chat sessions are process-local even though accepted scenarios and project state are durable.
- Continuous watch scheduling is deferred; watch state and explicit check-now are available.
- PostgreSQL, S3, Temporal, NATS, Redis, and the Next.js frontend are implemented paths in the repository, but deployment readiness still requires environment-specific integration and operational validation.

See [docs/architecture-current.md](docs/architecture-current.md), [docs/runtime-topology.md](docs/runtime-topology.md), [docs/frontend-migration.md](docs/frontend-migration.md), and [docs/future-tech-stack.md](docs/future-tech-stack.md) for architecture decisions and migration boundaries.

## Repository map

```text
app/main.py                 FastAPI routes, service composition, and legacy pages
app/mireye_client.py        MIREYE provider adapter
app/product.py              Product intake and confirmation workflow
app/diligence.py            Supplied-candidate orchestration and ranking
app/sandbox.py              SiteSnapshot lifecycle and scene construction
app/sandbox_evaluator.py    Deterministic geometry/evidence evaluation
app/sandbox_agent.py        Constrained sandbox agent tool loop
app/sandbox_scenarios.py    Scenario persistence, branching, and comparison
app/world.py                WorldSnapshot, USGS terrain, and Overture roads
app/ai/                     Orchestration, evaluation, memory, and model contracts
app/domain/                 Domain state and ports
app/application/            Use-case boundaries
app/infrastructure/         Storage, config, events, cache, and telemetry
app/static/                 Same-origin HTML/CSS/JavaScript pages
frontend/                   Next.js/React frontend and generated API client
migrations/                 Alembic migrations
scripts/                    Live demos, OpenAPI export, workers, and migration tools
tests/                      Offline, integration, and browser coverage
docs/                       Runbooks, architecture, and migration notes
```

## License

MIT License.
