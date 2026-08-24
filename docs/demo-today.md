# MIREYE Site Sandbox - Demo Day Runbook

## Open the prepared live demo

The server is already running with a temporary workspace database.

Open:

```text
http://127.0.0.1:8000/sandbox/site_9ba24af6d8bc4b2ab0b862081e480efc?scenario=scn_66de6cc61c7f49668b06f5778c4987d3&world=world_557e4f0da8cd9655846d86933f4fa181
```

If the server must be recreated, run this from the repository root:

```powershell
python -m app.sandbox_demo --confirm --serve
```

This performs a real one-parcel MIREYE lookup, quote, confirmed fetch, and immutable snapshot creation in a new temporary SQLite database. Open the URL printed by the command. It does not use `app/data/workspaces.db`.

## Six-minute demo

### 1. Establish that this is a real place

Show the map before touching the chat.

Say:

> This is a real MIREYE-resolved parcel near Austin, not synthetic demo geometry. The parcel boundary and site intelligence come from MIREYE. Terrain comes from USGS 3DEP, and the 1,777 mapped roads in this WorldSnapshot come from release-pinned Overture data.

Point to:

1. `OBSERVED / DERIVED / PROPOSED` in the upper-left.
2. The teal authoritative parcel boundary.
3. The terrain and road toggles. Turn each off and on once.
4. The scale bar and surrounding road/geographic context.
5. `Exact parcel match` and `1698.4 acres` in the right panel.

### 2. Explain the trust boundary

Say:

> MIREYE and the external source layers describe observed reality. Geometry calculations and PASS/FAIL outcomes are deterministic. The campus is only a proposed pre-development concept.

Point out that land currently passes, while flood, grid, zoning, legal road access, water capacity, and expansion feasibility remain explicitly unresolved where the evidence cannot prove them.

### 3. Ask the live agent to evaluate the current concept

Type:

```text
Does the current 100 MW phase-1 campus fit inside the parcel with the current setback? Explain only from deterministic evaluation.
```

Expected behavior:

- Activity says the site was inspected and re-evaluated, without exposing tool names.
- Overall result is `PASS`.
- The answer cites deterministic containment, area, coverage, and boundary-distance results.

Say:

> The model planned the work, but it did not decide PASS. The deterministic evaluator did.

### 4. Create a real alternative

In `Design options`:

1. Enter `Expansion-oriented option` as the option name.
2. Click `Try another`.
3. Type:

```text
Create an alternative 100 MW layout with more room reserved for future expansion, then evaluate whether it fits.
```

Expected behavior:

- The proposed scene changes while the observed parcel, terrain, and roads remain fixed.
- A new scenario revision and state hash are persisted.
- The agent reports success only after geometry validation and deterministic evaluation.

### 5. Compare alternatives

1. In `Compare with`, select the original option.
2. Click `Compare options`.

Show the deterministic changes in capacity, footprint, parcel coverage, constraint outcomes, evidence references, and calculation versions.

Then type:

```text
Which option uses less land, and what is still unresolved?
```

The explanation may narrate the deterministic comparison but must not invent a suitability score.

### 6. Show evidence and active MIREYE refresh

1. Click `View sources` under `MIREYE Site Intelligence`.
2. Show source, freshness, confidence, and timestamps.
3. Click `Refresh` to display the live quote.
4. Point out that no credits are charged until confirmation.
5. Click `Not now` during the normal demo. Confirm only when demonstrating immutable snapshot refresh end to end.

Type:

```text
Why is grid capacity unresolved even though transmission is nearby?
```

Expected answer: proximity, voltage, status, and queue evidence are useful screening signals, but they do not prove utility-deliverable MW.

## Closing line

> MIREYE turns a real location into a cited, refreshable site-intelligence record. The sandbox adds real physical context, deterministic pre-development evaluation, and agent-directed alternatives without confusing a conceptual proposal with observed reality or engineering approval.

## Do not claim

- The parcel is approved for a data center.
- Nearby transmission proves 100 MW or 300 MW deliverability.
- Point FEMA or slope evidence proves the whole parcel or footprint.
- Mapped roads prove legal access, frontage, or heavy-haul suitability.
- Raw zoning proves industrial entitlement.
- Proposed massing is engineering or construction design.

## Fast recovery

- Blank map: refresh once and wait up to 15 seconds for terrain/basemap tiles.
- Chat failure: verify `OPENAI_API_KEY` and restart the server.
- MIREYE failure: verify `MIREYE_API_KEY`; the prior immutable snapshot remains available.
- Port already in use: stop the existing server, then rerun the startup command with `--port 8001`.
