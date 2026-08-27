# MIREYE Site Sandbox: Future Technology Stack and Differentiation

## Product thesis

MIREYE Site Sandbox is an evidence-grounded workspace for physical-site diligence and conceptual pre-development planning.

It is not a generic GIS viewer, a parcel-data warehouse, an architecture application, or a generative 3D demo. A user supplies a real property or a candidate list, describes a project in normal language, and the product plans evidence collection, asks for approval before metered MIREYE work, preserves immutable evidence, evaluates only what can be proven, and supports conceptual alternatives.

```text
Plain-English project brief
        -> candidate list or selected property
        -> MIREYE identity, field planning, quote, enrichment
        -> immutable SiteSnapshot
        -> WorldSnapshot with source-backed spatial context
        -> deterministic evaluation and conceptual scenarios
        -> comparison, refresh, invalidation, replay
```

## Stack today

| Concern | Technology | Why it exists |
| --- | --- | --- |
| Application | Python, FastAPI, Uvicorn | One deployable application with typed HTTP APIs and no unnecessary service boundaries. |
| Real-world intelligence | Live MIREYE REST API | Parcel identity, field catalog, quote/fetch, batch enrichment, evidence provenance, refresh, and field-request lifecycle. |
| Agent | OpenAI strict function calling, `gpt-5.6-sol` | Natural-language planning and explanations; never the authority for evidence or geometry. |
| Truth engine | Shapely, local-meter spatial frame | Deterministic containment, setback, area, coverage, transform validation, and PASS/FAIL/UNRESOLVED results. |
| Persistence | SQLite | Local/demo-safe storage for immutable SiteSnapshots, WorldSnapshots, scenarios, revisions, evidence links, and spend plans. |
| Spatial artifacts | GeoJSON, GeoParquet, content hashes | Source-preserving observed layers and reproducible snapshots. |
| Terrain | USGS 3DEP | Source-backed DEM used for visual terrain and controlled placement anchoring. |
| Roads | Release-pinned Overture Transportation | Source-backed mapped-road geometry and contextual rendering. |
| Frontend | Vanilla HTML/CSS/JavaScript, MapLibre GL JS | Lightweight same-origin product UI without a client framework requirement. |
| Verification | pytest, temporary SQLite databases, Playwright | Offline deterministic tests plus opt-in live MIREYE and browser checks. |

## Trust boundary

```text
OBSERVED  = MIREYE and attributed-source facts/geometry
DERIVED   = deterministic calculations from observed data
PROPOSED  = user/agent conceptual scenario geometry
GENERATED = future visual output only; never factual site state
```

The LLM can decide what information is needed, ask a user for clarification, select validated tools, and explain tool results. It cannot invent parcel facts, write arbitrary geometry, spend MIREYE credits, mutate observed geometry, or decide PASS/FAIL itself.

## Production evolution

The product should evolve infrastructure without replacing its core model.

| Stage | Add | Keep unchanged |
| --- | --- | --- |
| Demo and design partners | Current FastAPI monolith, SQLite, local content-addressed assets | MIREYE integration, immutable snapshots, deterministic evaluator, MapLibre UI. |
| First shared production | Postgres + PostGIS, object storage, Redis-backed session/cache, background worker queue | API contracts, evidence model, scenario lineage, spend approval. |
| Portfolio scale | Worker autoscaling, durable job status, rate limiting, tenant budgets, vector/raster tile cache, observability | Customer-supplied candidate flow and MIREYE-first enrichment. |
| Enterprise platform | SSO/RBAC, audit export, retention controls, webhooks, data residency options, model evaluations | Evidence provenance and deterministic decision semantics. |

Recommended infrastructure shape:

```text
Web/API instances
      |
Postgres/PostGIS <-> object storage
      |
Redis / durable work queue
      |
workers for enrichment, WorldSnapshot builds, refresh, exports
      |
MIREYE + USGS + Overture + approved source primitives
```

Do not split into microservices before throughput or ownership boundaries require it. The current modular monolith is the correct operating model while product behavior is still moving.

## What makes MIREYE Site Sandbox different

### 1. Evidence is a product object, not a hidden backend detail

Every claim can carry source, field, timestamp, freshness, scope, confidence, hash, and snapshot lineage. The product can say exactly what supports a result and what does not.

### 2. Uncertainty is first-class

`UNRESOLVED` is not a soft pass. Point FEMA evidence does not prove parcel-wide flood exclusion. Transmission proximity does not prove deliverable MW. A raw zoning code does not prove industrial entitlement. This is a defensible diligence workflow rather than a confidence theater dashboard.

### 3. MIREYE remains active after the first lookup

The system plans minimal field requests, quotes them, requires explicit confirmation, refreshes only stale or affected evidence, creates a new immutable snapshot, diffs the result, and re-evaluates affected scenarios. MIREYE becomes the live intelligence layer throughout the project lifecycle.

### 4. The agent is constrained but useful

The agent translates plain English into a typed project/evidence plan, generates dynamic clarification requests, selects approved tools, and creates conceptual alternatives. Deterministic code remains responsible for geometry, cost confirmation, identity safety, and outcomes.

### 5. It links diligence to a grounded pre-development workspace

Most site-selection products stop at a report. Most visualization products start from unverified geometry. This product connects real parcel intelligence, source-backed physical context, conceptual layout, scenario comparison, and revalidation in one workflow.

### 6. It creates an audit trail for decisions, not only data

A scenario records intent, accepted tool calls, state hash, evaluator version, evidence dependencies, model identifier, and result. Teams can replay why a site was rejected or why a conclusion changed after refresh.

## Strategic opportunities to pitch with MIREYE

### Living Diligence Room

Customer-supplied candidate lists become persistent portfolios. MIREYE refreshes evidence on demand, the system identifies which constraints and scenarios changed, and the team receives an explainable “what changed and what to do next” view.

### Controlled discovery run API

If MIREYE chooses to expose its internal screening capability, a controlled asynchronous endpoint could accept a geography, typed constraints, evidence profile, candidate limit, and credit ceiling. It would return ranked candidates with evidence and uncertainty without exposing a raw parcel universe.

### Footprint-aware evidence API

The high-value missing primitive is an authoritative geometry request: evaluate a proposed footprint against flood, wetlands, protected areas, land cover, and other source-backed geometries. This would allow MIREYE to answer footprint-scope questions without the Sandbox copying MIREYE's underlying data stack.

### Next-best-evidence planning

Given a project brief and current snapshot, return the smallest additional evidence request, field request, survey, utility confirmation, or jurisdictional mapping needed to reduce the most important uncertainty.

### Decision-grade export

Produce a cited diligence packet containing exact evidence, scopes, source snapshots, scenario geometry, deterministic outcomes, and unresolved risks. This is useful for investment committees, development teams, and external diligence partners.

## What we should not build

- A local nationwide replication of MIREYE's parcel, hazard, utility, or zoning stack.
- Synthetic parcel boundaries, terrain, roads, or infrastructure to make the scene look better.
- A model that decides physical facts or PASS/FAIL outcomes.
- Uncontrolled spending or autonomous MIREYE calls.
- Nationwide inverse discovery unless MIREYE exposes an approved capability or a separate licensed candidate source is selected.
- Engineering-grade power-flow, grading, entitlement, or construction claims.
- A foundation world model before the deterministic scene and evidence coverage justify it.

## North-star pitch

> Describe the physical site problem in plain English. MIREYE plans and refreshes the evidence, the sandbox makes the real place legible, and deterministic evaluation keeps every recommendation tied to what can actually be proven.
