"""Provision and optionally serve one real-parcel Site Sandbox demo."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlencode


DEFAULT_ADDRESS = "1 Tesla Road, Austin, TX 78725"
DEMO_WORKSPACE_ID = "site-sandbox-demo"


class DemoSetupError(RuntimeError):
    pass


async def provision_demo(service, scenarios, *, address: str, confirmed: bool, candidate_number: int | None = None) -> dict:
    resolved = await service.resolve(input=address, kind="address")
    candidates = resolved.get("candidates", [])
    if resolved.get("requires_selection"):
        if candidate_number is None:
            choices = "\n".join(f"  {index}. {candidate}" for index, candidate in enumerate(candidates, 1))
            raise DemoSetupError(f"MIREYE returned an ambiguous address. Re-run with --candidate N:\n{choices}")
        if candidate_number < 1 or candidate_number > len(candidates):
            raise DemoSetupError("--candidate is outside the returned candidate list.")
        location = candidates[candidate_number - 1]
    elif len(candidates) == 1:
        location = candidates[0]
    else:
        raise DemoSetupError("MIREYE did not resolve the demo address to one location.")

    quote = await service.quote(lat=location["lat"], lng=location["lng"])
    if not confirmed:
        print("MIREYE quote:")
        print(json.dumps(quote["quote"], indent=2, sort_keys=True))
        confirmed = input("Type YES to create one paid SiteSnapshot: ").strip() == "YES"
    if not confirmed:
        raise DemoSetupError("Snapshot creation was not confirmed.")

    snapshot = await service.create_snapshot(
        workspace_id=DEMO_WORKSPACE_ID,
        lat=location["lat"],
        lng=location["lng"],
        confirmed=True,
    )

    from app.sandbox import scene_state_from_snapshot
    from app.sandbox_proposal import DEFAULT_MINIMUM_SETBACK_M, generate_data_center_proposal

    scene = scene_state_from_snapshot(snapshot)
    scene["proposed"] = []
    proposal = generate_data_center_proposal(
        snapshot,
        scene,
        capacity_mw=100,
        minimum_setback_m=DEFAULT_MINIMUM_SETBACK_M,
    )
    if proposal["status"] not in {"PLACED", "ADJUSTED"}:
        raise DemoSetupError(f"A valid initial proposal could not be created: {proposal['reason']}")

    scenario = scenarios.create(
        snapshot,
        workspace_id=DEMO_WORKSPACE_ID,
        user_intent="Place a conceptual 100 MW phase-1 campus with a 300 MW expansion target and a 10 m minimum setback.",
        scene_state=proposal["scene_state"],
        model_id="deterministic_demo_bootstrap",
    )
    return {"snapshot": snapshot, "scenario": scenario, "quote": quote, "proposal": proposal}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create one real MIREYE Site Sandbox demo in a temporary SQLite database.")
    parser.add_argument("--address", default=DEFAULT_ADDRESS)
    parser.add_argument("--candidate", type=int, help="One-based candidate number when MIREYE reports an ambiguous address.")
    parser.add_argument("--confirm", action="store_true", help="Confirm the quoted one-parcel MIREYE fetch without an interactive prompt.")
    parser.add_argument("--db", type=Path, help="Demo database path. Defaults to a new file in the OS temporary directory.")
    parser.add_argument("--serve", action="store_true", help="Start uvicorn after provisioning the demo.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser


def _database_path(requested: Path | None) -> Path:
    production = Path(__file__).resolve().parent / "data" / "workspaces.db"
    if requested is None:
        handle, name = tempfile.mkstemp(prefix="mireye-site-sandbox-", suffix=".db")
        os.close(handle)
        path = Path(name)
    else:
        path = requested.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
    if path.resolve() == production.resolve():
        raise DemoSetupError("The demo database cannot be app/data/workspaces.db.")
    return path


def main() -> int:
    args = _parser().parse_args()
    try:
        db_path = _database_path(args.db)
        os.environ["WORKSPACE_DB"] = str(db_path)

        from app.config import MIREYE_API_KEY

        if not MIREYE_API_KEY:
            raise DemoSetupError("MIREYE_API_KEY is required for a real-parcel demo.")

        from app.main import app, sandbox_service, scenario_service
        from app.sandbox import SandboxError

        try:
            result = asyncio.run(provision_demo(
                sandbox_service,
                scenario_service,
                address=args.address,
                confirmed=args.confirm,
                candidate_number=args.candidate,
            ))
        except SandboxError as exc:
            raise DemoSetupError(str(exc)) from exc
        snapshot, scenario = result["snapshot"], result["scenario"]
        query = urlencode({"scenario": scenario["scenario_id"]})
        url = f"http://{args.host}:{args.port}/sandbox/{snapshot['snapshot_id']}?{query}"

        print(f"Demo database: {db_path}")
        print(f"Snapshot ID: {snapshot['snapshot_id']}")
        print(f"Scenario ID: {scenario['scenario_id']} / revision {scenario['revision']}")
        print(f"Initial evaluation: {scenario['evaluation']['overall_status']}")
        print(f"Sandbox URL: {url}")
        if not os.environ.get("OPENAI_API_KEY"):
            print("Live model chat disabled: set OPENAI_API_KEY and restart the demo.")

        if args.serve:
            import uvicorn

            uvicorn.run(app, host=args.host, port=args.port)
        return 0
    except DemoSetupError as exc:
        print(f"Demo setup failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
