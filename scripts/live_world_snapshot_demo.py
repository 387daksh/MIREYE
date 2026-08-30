"""Build and optionally serve one real grounded Site Sandbox world."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Create one real MIREYE + USGS 3DEP + Overture WorldSnapshot demo.")
    result.add_argument("--address", default="1 Tesla Road, Austin, TX 78725")
    result.add_argument("--candidate", type=int)
    result.add_argument("--confirm", action="store_true", help="Confirm the one-location MIREYE fetch after quoting it.")
    result.add_argument("--serve", action="store_true")
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=8000)
    result.add_argument("--db", type=Path)
    result.add_argument("--assets", type=Path)
    return result


def temporary_path(prefix: str, suffix: str = "") -> Path:
    if suffix:
        handle, name = tempfile.mkstemp(prefix=prefix, suffix=suffix)
        os.close(handle)
        return Path(name)
    return Path(tempfile.mkdtemp(prefix=prefix))


def safe_path(requested: Path | None, *, production: Path, prefix: str, suffix: str = "") -> Path:
    path = requested.expanduser().resolve() if requested else temporary_path(prefix, suffix)
    if path.resolve() == production.resolve():
        raise RuntimeError(f"Demo path cannot be {production}.")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def build(args) -> dict:
    from app.main import sandbox_service, scenario_service, world_service
    from app.sandbox_demo import provision_demo

    provisioned = await provision_demo(
        sandbox_service, scenario_service, address=args.address,
        confirmed=args.confirm, candidate_number=args.candidate,
    )
    snapshot = provisioned["snapshot"]
    world = await world_service.create(
        site_snapshot_id=snapshot["snapshot_id"], buffer_m=1000,
        requested_layers=["terrain", "roads", "transmission"],
        options={"prefer_1m": True, "overture_release": "2026-08-19.0"},
    )
    scene = provisioned["proposal"]["scene_state"]
    scene["world_snapshot_id"] = world["world_snapshot_id"]
    scenario = scenario_service.create(
        snapshot, workspace_id=snapshot["workspace_id"],
        user_intent="Grounded 100 MW / 400 MWh phase-1 BESS with a 300 MW / 1,200 MWh expansion target on the pinned physical world.",
        scene_state=scene, model_id="deterministic_world_demo_bootstrap",
    )
    return {"snapshot": snapshot, "world": world, "scenario": scenario, "quote": provisioned["quote"]}


def main() -> int:
    args = parser().parse_args()
    load_dotenv(ROOT / ".env")
    if not os.environ.get("MIREYE_API_KEY"):
        print("MIREYE_API_KEY is required for the opt-in real-world demo.")
        return 2
    production_db = ROOT / "app" / "data" / "workspaces.db"
    production_assets = ROOT / "app" / "data" / "world-assets"
    try:
        db = safe_path(args.db, production=production_db, prefix="mireye-world-demo-", suffix=".db")
        assets = safe_path(args.assets, production=production_assets, prefix="mireye-world-assets-")
        os.environ["WORKSPACE_DB"] = str(db)
        os.environ["WORLD_ASSET_DIR"] = str(assets)
        result = asyncio.run(build(args))
    except (RuntimeError, ValueError) as exc:
        print(f"World demo failed: {exc}")
        return 2

    snapshot, world, scenario = result["snapshot"], result["world"], result["scenario"]
    query = urlencode({"world": world["world_snapshot_id"], "scenario": scenario["scenario_id"]})
    url = f"http://{args.host}:{args.port}/sandbox/{snapshot['snapshot_id']}?{query}"
    terrain = next(layer for layer in world["layers"] if layer["layer"] == "terrain")
    roads = next(layer for layer in world["layers"] if layer["layer"] == "roads")
    transmission = next(layer for layer in world["layers"] if layer["layer"] == "transmission")
    print(f"Temporary database: {db}")
    print(f"Content-addressed assets: {assets}")
    print(f"SiteSnapshot: {snapshot['snapshot_id']}")
    print(f"WorldSnapshot: {world['world_snapshot_id']}")
    print(f"Terrain: {terrain['source']['dataset']} / {terrain['terrain']['actual_resolution_m']} m / {terrain['terrain']['vertical_reference']}")
    print(f"Roads: Overture {roads['source']['release']} / {roads['roads']['feature_count']} features")
    print(f"Transmission geometry: {transmission['availability']}")
    print(f"Scenario: {scenario['scenario_id']} / revision {scenario['revision']} / {scenario['evaluation']['overall_status']}")
    print(f"Sandbox URL: {url}")
    if args.serve:
        import uvicorn
        from app.main import app

        uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
