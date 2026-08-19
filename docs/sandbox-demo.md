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

`--confirm` authorizes the quoted fetch for one parcel. Omit it to review the quote and type `YES` interactively. The default address is `1 Tesla Road, Austin, TX 78725`; override it with `--address "..."`. If lookup is ambiguous, re-run with the printed one-based `--candidate N` selection.

The launcher prints:

- temporary demo database path
- SiteSnapshot ID
- initial scenario ID and revision
- exact browser URL

The URL has this form:

```text
http://127.0.0.1:8000/sandbox/{snapshot_id}?scenario={scenario_id}
```

## Demo Flow

1. Open the printed URL. Confirm the real parcel boundary and resolution point are visible and the camera fits the parcel.
2. Confirm the orange conceptual 100 MW data center appears inside the observed parcel.
3. Review REAL PARCEL, CURRENT SCENARIO, and the OBSERVED / DERIVED / PROPOSED legend.
4. Click an example prompt to place it in the chat input, then send it.
5. Try `Move it 200 meters north.`, `Make it 120 m by 140 m.`, `Rotate it 30 degrees.`, and `Does it fit?`.
6. Confirm every evaluation shows PASS, FAIL, or UNRESOLVED with the deterministic reason and evidence IDs.
7. Use Branch to create another scenario, change its capacity or geometry, select both scenarios, and use Compare.
8. Confirm the parcel geometry never changes while the orange proposed object does.

Live chat requires `OPENAI_API_KEY`. Without it, snapshot, scene, evaluator, proposal, scenario persistence, branching, comparison, and scripted agent tools remain testable; the chat endpoint reports model unavailability explicitly.

## Stop And Reset

Press `Ctrl+C` in the server terminal. Delete the exact temporary database path printed by the launcher to reset the demo. The launcher refuses to use `app/data/workspaces.db` as its demo database.
