# Site Sandbox Demo

## Environment

PowerShell:

```powershell
$env:MIREYE_API_KEY = "your-mireye-key"
$env:OPENAI_API_KEY = "your-openai-key" # optional; required only for live chat
$env:SANDBOX_AGENT_MODEL = "gpt-5-mini" # optional
```

The launcher reads credentials from environment variables or the existing `.env`. It never writes credentials to the demo database.

## Start

Create one real SiteSnapshot, a validated 100 MW proposal, and a temporary SQLite database, then start the server:

```powershell
python -m app.sandbox_demo --confirm --serve
```

`--confirm` authorizes the quoted fetch for one parcel. Omit it to review the quote and type `YES` interactively. The flagship address is `1 Tesla Road, Austin, TX 78725`; a prior live run resolved it to MIREYE's canonical parcel address `2025 1/2 ROBOTIC AVE`, approximately 1,698 acres. The site is an industrial-scale visualization example, not a claim of BESS suitability or export/injection interconnection. Override it with `--address "..."`. If lookup is ambiguous, re-run with the printed one-based `--candidate N` selection.

The launcher prints:

- temporary demo database path
- SiteSnapshot ID
- initial scenario ID and revision
- exact browser URL

The URL has this form:

```text
http://127.0.0.1:8000/sandbox/{snapshot_id}?scenario={scenario_id}
```

The plain-English product home is available on the same server at:

```text
http://127.0.0.1:8000/
```

## Demo Flow

1. Open the printed URL. Confirm the authoritative parcel, USGS terrain relief, hierarchical Overture roads, scale control, and restrained conceptual BESS facility are visible.
2. Review the project header, site-feasibility cards, MIREYE freshness, and the OBSERVED / DERIVED / PROPOSED legend.
3. Ask `Design a 100 MW / 400 MWh phase-1 BESS with room for a 300 MW / 1,200 MWh expansion.`
4. Confirm the proposed battery enclosures, inverter/PCS blocks, point of interconnection, internal access, service area, and expansion reserve remain explicitly conceptual.
5. Ask `Create a second layout with more expansion room.` and then `Which layout uses the least land?`
6. Compare the saved alternatives. Capacity, land envelope, evaluator outcomes, and changed constraints must come from deterministic scenario state.
7. Ask `Why is grid capacity unresolved?` Confirm proximity evidence is not presented as available MW.
8. Open View sources, then quote and confirm a MIREYE refresh if stale fields exist.
9. Confirm the observed parcel and WorldSnapshot never change when proposed layouts change.

Live chat requires `OPENAI_API_KEY`. Without it, snapshot, scene, evaluator, proposal, scenario persistence, branching, comparison, and scripted agent tools remain testable; the chat endpoint reports model unavailability explicitly.

## Stop And Reset

Press `Ctrl+C` in the server terminal. Delete the exact temporary database path printed by the launcher to reset the demo. The launcher refuses to use `app/data/workspaces.db` as its demo database.
