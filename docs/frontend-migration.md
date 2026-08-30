# Frontend Migration Plan

## Current state

The product uses same-origin vanilla HTML, CSS, and JavaScript served by FastAPI.
MapLibre renders the parcel, WorldSnapshot layers, and conceptual scenarios. This
surface remains functional and is not being rewritten in Phase 14.

Current surfaces:

- project intake and supplied-candidate diligence
- dynamic DecisionRequest interruptions
- candidate resolution and spend confirmation
- evidence, readiness, power, entitlement, actions, and changes
- MapLibre sandbox and layer controls
- scenario creation/comparison
- agent chat and MIREYE refresh

## Target

- Next.js with React and TypeScript
- Tailwind and shadcn/ui for accessible product primitives
- TanStack Query for server state and refresh/invalidation
- Zustand only for transient map/scene interaction state
- MapLibre for the base spatial canvas
- deck.gl only for layer volume or GPU visualization MapLibre cannot handle
- generated client from the exported FastAPI OpenAPI contract

The browser must not become the authority for evidence, constraint outcomes,
scenario hashes, spend confirmation, or geometry validation.

## Migration sequence

1. Export `build/openapi.json` with `python scripts/export_openapi.py` and generate
   a checked TypeScript client in CI. Do not hand-maintain duplicate API types.
2. Add a Next.js application beside the current static UI and proxy `/v1` to the
   existing FastAPI process during development.
3. Migrate project intake and dynamic DecisionRequest first; these are mostly
   typed forms and server state.
4. Migrate diligence workspace, evidence, readiness, actions, and changes.
5. Move the MapLibre sandbox last, preserving exact SceneState serialization and
   deterministic backend evaluation contracts.
6. Remove a vanilla surface only after browser and API-contract parity tests pass.

## Parity checks

Contract parity runs in the frontend test suite (`npm test`, in `frontend/`):

- `src/lib/api-contract.test.ts` scans the app for every `api.GET/POST(...)`
  call and asserts the path and verb exist in `build/openapi.json`.
- `src/lib/product-status.test.ts` reads the statuses `app/product.py` can
  emit and asserts the intake still renders a branch for each, in both
  directions. The product response is an untyped dict, so the generated client
  cannot catch this on its own.

Both skip with a warning when `build/openapi.json` is absent. Export it first:

```
uv run python scripts/export_openapi.py
npm --prefix frontend run generate:api
```

Browser-level parity (rendering the migrated surfaces against a running API)
is not yet covered.

## State ownership

| State | Owner |
| --- | --- |
| Projects, evidence, snapshots, scenarios, decisions | FastAPI application |
| Query cache and loading/error state | TanStack Query |
| Temporary map camera, selected layer, open panel | Local React state or Zustand |
| PASS/FAIL/UNRESOLVED, hashes, confirmation | Backend only |

## Non-goals

No React migration, visual redesign, deck.gl adoption, frontend microservice, or
world-model UI is part of the current phase.
