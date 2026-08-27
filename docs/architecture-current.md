# MIREYE Architecture: Current State, Target, and Migration

## Current Architecture

MIREYE is a modular FastAPI monolith. It combines live physical-world evidence,
deterministic spatial evaluation, persistent project memory, and a constrained
LLM interface in one process. This is appropriate for the current product stage,
but package ownership and infrastructure boundaries were previously implicit.

```text
Vanilla browser UI (same origin)
  -> app.main FastAPI routes and Pydantic request models
  -> product / diligence / sandbox application services
  -> MIREYE, OpenAI, USGS, Overture, and public-government HTTP sources
  -> immutable SiteSnapshot and WorldSnapshot construction
  -> deterministic evaluator / readiness / change propagation
  -> SQLite WorkspaceStore + content-addressed local artifacts
  -> response, MapLibre scene, agent tools, scenarios, and project changes
```

### Existing module ownership

| Module | Current responsibility | Coupling to address |
| --- | --- | --- |
| `app/main.py` | FastAPI app, 50+ routes, request models, global service graph, static files | UI, application, providers, and infrastructure meet here |
| `app/diligence.py` | Candidate ingestion, constraint compilation, decisions, enrichment, ranking, watch/check-now | Large application service with persistence/provider knowledge |
| `app/sandbox.py` | SiteSnapshot lifecycle, evidence semantics, refresh, scene creation | MIREYE and repository details are direct dependencies |
| `app/sandbox_evaluator.py` | Deterministic geometry and evidence evaluation | Correctly independent of FastAPI and providers |
| `app/sandbox_proposal.py` | Deterministic parcel-aware proposal placement | Correctly depends on geometry/domain functions only |
| `app/sandbox_scenarios.py` | Immutable scenario revisions, comparisons, evaluation runs | Direct `WorkspaceStore` dependency |
| `app/sandbox_agent.py` | Responses API adapter, tool schemas, tool loop, session state, safety guards | Model adapter and agent runtime share one module |
| `app/project_intelligence.py` | Coverage, gaps, readiness, actions, RFIs | Mostly deterministic domain/application logic |
| `app/project_readiness.py` | Power/entitlement states plus authoritative HTTP collection | Domain calculations and source adapters share one module |
| `app/project_changes.py` | Deterministic snapshot/world change classification | Mostly deterministic domain logic |
| `app/world.py` | WorldSnapshot orchestration, USGS/Overture adapters, artifact conversion | Providers, storage, and application orchestration share one module |
| `app/mireye_client.py` | Live MIREYE HTTP adapter and legacy local simulation | Provider adapter includes legacy DuckDB fallback |
| `app/workspace/store.py` | All SQLite DDL and persistence | One concrete repository for every aggregate |
| `app/workspace/engine.py` | Legacy workspace state/replay workflows | Direct store and MIREYE dependencies |
| `app/discovery/*` | Legacy synthetic inverse screening with DuckDB/GeoParquet | Not the live supplied-candidate diligence workflow |
| `app/grid/*` | Legacy deterministic sample grid intelligence | Not utility-confirmed capacity |
| `app/static/*` | Product intake and MapLibre sandbox | Vanilla JS directly consumes REST response shapes |

### Infrastructure and state

- SQLite stores workspaces, sites, snapshots, spend plans, scenarios,
  dependencies, evaluation runs, diligence projects, and project changes.
- Local content-addressed files store source and derived WorldSnapshot artifacts.
- DuckDB/GeoParquet support the legacy prototype `/v1/screen` path.
- MIREYE and OpenAI are called with `httpx`; USGS, Overture, and government
  sources are direct adapters inside current application modules.
- `SandboxAgent` sessions are process-local. Durable scenarios and project
  decisions survive restarts; transient chat turn state does not always do so.
- Service objects are process-global in `app.main`. This prevents clean
  per-request dependency overrides and multi-process shared in-memory sessions.
- Storage initialization is explicit at FastAPI startup. Imports do not create
  the SQLite database or artifact directory.

### Current dependency direction

The deterministic evaluator is already a strong domain boundary. Other modules
still depend directly on SQLite, filesystem paths, `httpx`, FastAPI, and model
request formats. Configuration previously mutated `os.environ` through
`load_dotenv`; Phase 14 replaces that with typed, non-mutating settings while
keeping compatibility constants for existing modules.

## Target Architecture

The target remains a modular monolith until operational scale proves that a
separate deployable is necessary.

```text
API / browser / future TypeScript client
  -> application use cases and workflow orchestration
  -> domain state and deterministic policies
  -> ports
       repositories | artifacts | sources | models | workflows | events
  -> adapters
       MIREYE | USGS | Overture | government docs | OpenAI
       PostgreSQL/PostGIS | S3 | Temporal | Redis
```

### Package boundaries

```text
app/domain/          projects, sites, evidence, constraints, decisions,
                     actions, scenarios, world, and source semantics
app/application/     diligence, intelligence, workflows, orchestration
app/infrastructure/  config, database, storage, queue, events, observability
app/adapters/        MIREYE, USGS, Overture, government, documents
app/ai/              model/runtime/planner/tool/verifier/memory contracts
```

Existing files remain authoritative while behavior is moved incrementally.
No domain package may import FastAPI, SQLite, OpenAI, HTTPX, filesystem paths,
or a cloud SDK. The initial ports live in `app/domain/ports.py`; current concrete
classes are adapters even before their files are relocated.

### Physical-World OS capability boundaries

- **World state:** immutable SiteSnapshots and WorldSnapshots.
- **Evidence graph:** evidence, provenance, scope, freshness, claims, and
  deterministic constraint dependencies.
- **Project state:** requirements, decisions, assumptions, actions, readiness,
  and ProjectChanges.
- **Memory:** durable evidence/project/scenario history plus future separately
  governed episodic, semantic, and procedural agent memory.
- **Orchestration:** workflow ports support execute/observe/verify/replan without
  embedding a workflow engine in domain code.
- **Simulation:** deterministic evaluator and immutable scenario revisions.
- **Monitoring:** source watches, refresh plans, snapshot diffs, and selective
  propagation.
- **World model:** a future downstream visualization adapter; never evidence or
  evaluation authority.

## Migration Plan

### 1. Foundation (current phase)

- Typed `pydantic-settings` configuration with explicit environment profiles.
- Domain/infrastructure/application/adapter/AI package boundaries and ports.
- Local and S3-compatible content-addressed artifact adapters.
- Alembic baseline, local async workflow executor, typed in-process events,
  correlation IDs, traces, structured logs, and provider/model usage metrics.
- `pyproject.toml`, `uv.lock`, Docker development services, OpenAPI export, and
  CI quality gates.

### 2. Repository extraction

- Split `WorkspaceStore` behind project, snapshot, scenario, and evidence ports.
- Convert current SQLite DDL to declarative migrations.
- Implement PostgreSQL/PostGIS adapters and migrate a copy of data.
- Validate row counts, immutable JSON hashes, geometry hashes, and lineage before
  any production cutover. SQLite remains for isolated tests and local demos.

### 3. Provider extraction

- Move MIREYE, USGS, Overture, government, and document HTTP implementations
  behind source ports without changing response semantics.
- Inject shared HTTP clients, retry policies, tracing, and rate/cost controls.
- Remove the legacy local mode from the live MIREYE adapter only after explicit
  demo and discovery behavior is separated.

### 4. Application decomposition

- Extract route schemas from `app.main` and inject an application service graph.
- Separate deterministic project/readiness calculations from provider fetching.
- Replace process-local chat sessions with an explicit `MemoryStore` adapter when
  multi-process deployment is required.

### 5. Durable workflows and events

- Replace `LocalAsyncWorkflowExecutor` with a Temporal adapter for long-running,
  retryable, human-interrupted workflows.
- Add a transactional outbox in the same database transaction as aggregate
  changes, then publish typed events. Kafka is not justified yet.

### 6. Frontend migration

- Export OpenAPI and generate a typed client.
- Migrate one surface at a time to Next.js/React, starting with project intake
  and DecisionRequest, while retaining same-origin FastAPI compatibility.

## Risks and controls

| Risk | Control |
| --- | --- |
| Baseline migration does not create the legacy schema | Stamp only validated existing databases; convert DDL before PostgreSQL cutover |
| Global service graph complicates tests/multi-process execution | Introduce a composition root before moving routes |
| Process-local agent sessions are not horizontally scalable | Keep durable scenario/project state authoritative; add shared memory only when multi-process deployment starts |
| S3 and PostgreSQL adapters are not active application paths | Treat configuration as preparatory; do not claim production cutover |
| Direct provider calls have uneven retry/timeout policy | Extract adapters behind shared HTTP policy incrementally |
| Existing large modules invite risky rewrites | Move one use case behind a port at a time with contract/regression tests |
| Legacy `/v1/screen` and `/v1/grid` can be mistaken for live proof | Keep them outside the live diligence orchestration and label their provenance |
