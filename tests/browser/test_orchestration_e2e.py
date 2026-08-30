from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import Route, expect, sync_playwright


pytestmark = pytest.mark.runtime


def test_project_orchestration_decision_resume_and_refresh() -> None:
    if os.getenv("MIREYE_RUNTIME_INTEGRATION") != "1":
        pytest.skip("requires the containerized frontend")
    state = {"started": False, "answered": False, "resumed": False}
    frontend_url = os.getenv("MIREYE_FRONTEND_URL", "http://localhost:3000")
    width, height = (int(value) for value in os.getenv("MIREYE_VIEWPORT", "1280x720").split("x", 1))
    root = Path(__file__).resolve().parents[2]

    def api_fixture(route: Route) -> None:
        request = route.request
        url = request.url
        headers = {"Access-Control-Allow-Origin": frontend_url, "Access-Control-Allow-Headers": "content-type"}
        if request.method == "OPTIONS":
            return route.fulfill(status=204, headers=headers)
        if url.endswith("/static/vendor/maplibre-gl.js"):
            return route.fulfill(path=root / "app" / "static" / "vendor" / "maplibre-gl.js", content_type="application/javascript", headers=headers)
        if url.endswith("/static/vendor/maplibre-gl.css"):
            return route.fulfill(path=root / "app" / "static" / "vendor" / "maplibre-gl.css", content_type="text/css", headers=headers)
        if url.endswith("/v1/diligence/projects") and request.method == "POST":
            return route.fulfill(json={"project_id": "project-e2e"}, headers=headers)
        if url.endswith("/v1/diligence/projects/project-e2e") and request.method == "GET":
            runs = []
            if state["started"]:
                runs = [{"run_id": "run-e2e", "status": "COMPLETED" if state["resumed"] else "WAITING_FOR_DECISION"}]
            decision = None if state["answered"] else _decision() if state["started"] else None
            return route.fulfill(
                json={
                    "project_id": "project-e2e",
                    "workspace_id": "workspace-e2e",
                    "orchestration_runs": runs,
                    "active_decision": decision,
                    "decision_history": [{"decision_id": "decision-history", "status": "UNRESOLVED", "rationale": "Utility confirmation remained required.", "evidence_ids": ["transmission-distance"]}],
                    "project_intelligence": {"evidence_items": [{"evidence_id": "transmission-distance", "source": "MIREYE"}]},
                    "candidates": [{
                        "candidate_id": "candidate-e2e", "snapshot_id": "site-e2e",
                        "summary": {"sandbox_url": "/sandbox/site-e2e?world=world-e2e"},
                    }],
                },
                headers=headers,
            )
        if url.endswith("/v1/sandbox/site/snapshots/site-e2e/scene"):
            return route.fulfill(json=_scene(), headers=headers)
        if url.endswith("/v1/sandbox/world-snapshots/world-e2e"):
            return route.fulfill(json=_world(), headers=headers)
        if url.endswith("/v1/sandbox/world-snapshots/world-e2e/layers/roads"):
            return route.fulfill(json=_roads(), headers=headers)
        if url.endswith("/v1/sandbox/world-snapshots/world-e2e/layers/buildings"):
            return route.fulfill(json=_buildings(), headers=headers)
        if url.endswith("/v1/sandbox/world-snapshots/world-e2e/layers/water"):
            return route.fulfill(json=_water(), headers=headers)
        if url.endswith("/v1/sandbox/world-snapshots/world-e2e/layers/land_cover"):
            return route.fulfill(json=_land_cover(), headers=headers)
        if url.endswith("/v1/ai/projects/project-e2e/orchestrate"):
            state["started"] = True
            return route.fulfill(json={"run": {"run_id": "run-e2e", "status": "RUNNING"}, "decision_request": None}, headers=headers)
        if url.endswith("/v1/diligence/projects/project-e2e/decisions/decision-e2e/answer"):
            state["answered"] = True
            return route.fulfill(json={"status": "ANSWERED"}, headers=headers)
        if url.endswith("/v1/ai/projects/project-e2e/orchestration/run-e2e/resume"):
            state["resumed"] = True
            return route.fulfill(json={"run_id": "run-e2e", "status": "RESUMED"}, headers=headers)
        if url.endswith("/v1/ai/projects/project-e2e/orchestration/run-e2e/events"):
            events = _completed_events() if state["resumed"] else _waiting_events()
            return route.fulfill(
                status=200, body=events, headers={**headers, "Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
            )
        return route.fallback()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        browser_errors: list[str] = []
        failed_requests: list[str] = []
        page.on("console", lambda message: browser_errors.append(message.text) if message.type == "error" else None)
        # Next.js cancels its in-flight RSC navigation request when the test deliberately reloads.
        page.on("requestfailed", lambda request: failed_requests.append(request.url) if "?_rsc=" not in request.url else None)
        page.route("http://localhost:8000/**", api_fixture)
        page.goto(frontend_url)
        page.get_by_label("Workspace").fill("workspace-e2e")
        page.get_by_placeholder("Describe the project and constraints").fill("Validate a Texas site")
        page.get_by_placeholder("One real candidate address or coordinate per line").fill("Austin, Texas")
        page.get_by_role("button", name="Create project").click()
        expect(page.get_by_label("Physical-world workspace")).to_be_visible()
        expect(page.get_by_role("heading", name="Context")).to_be_visible()
        expect(page.get_by_text("History", exact=True)).to_be_visible()
        page.get_by_label("Ask MIREYE about this site").fill("Check power and entitlement")
        page.get_by_role("button", name="Ask MIREYE").click()
        expect(page.get_by_text("MIREYE · Waiting for you", exact=True)).to_be_visible()
        expect(page.get_by_text("Continue with verified evidence?", exact=True)).to_be_visible()
        if screenshot := os.getenv("MIREYE_UI_SCREENSHOT"):
            page.screenshot(path=screenshot)
        page.get_by_label("Continue").check()
        page.get_by_label("Custom answer").fill("Continue screening without upgrading unsupported evidence.")
        page.get_by_role("button", name="Continue", exact=True).click()
        expect(page.get_by_text("MIREYE · Completed", exact=True)).to_be_visible()
        expect(page.get_by_text("Current scene", exact=True)).to_be_visible()
        expect(page.get_by_test_id("react-sandbox-map")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        expect(page.get_by_role("button", name="Terrain")).to_be_visible()
        if screenshot := os.getenv("MIREYE_UI_FINAL_SCREENSHOT"):
            page.screenshot(path=screenshot)
        page.get_by_role("button", name="Proposed").click()
        expect(page.get_by_role("button", name="Proposed")).to_have_attribute("aria-pressed", "false")
        page.reload()
        expect(page.get_by_text("MIREYE · Completed", exact=True)).to_be_visible()
        assert not browser_errors
        assert not failed_requests
        browser.close()


def _decision() -> dict:
    return {
        "decision_id": "decision-e2e",
        "resume_token": "resume-e2e",
        "question": "Continue with verified evidence?",
        "options": [{"id": "continue", "label": "Continue", "description": "Resume the durable workflow."}],
        "allow_custom": True,
    }


def _event(event_type: str, sequence: int, **payload) -> str:
    return f"event: {event_type}\ndata: {json.dumps({'type': event_type, 'sequence': sequence, **payload})}\n\n"


def _waiting_events() -> str:
    return _event("RUN_STARTED", 1) + _event("PLANNING", 2) + _event("NEEDS_USER_DECISION", 3, decision_request=_decision())


def _completed_events() -> str:
    return _event("RESUMED", 4) + _event("TASK_COMPLETED", 5, task_id="task-e2e") + _event("COMPLETED", 6)


def _scene() -> dict:
    parcel = {"type": "Polygon", "coordinates": [[[-97.001, 31.999], [-96.999, 31.999], [-96.999, 32.001], [-97.001, 32.001], [-97.001, 31.999]]]}
    return {
        "site_snapshot_id": "site-e2e",
        "frame": {"origin": {"lat": 32.0, "lng": -97.0}},
        "camera": {"center": {"lat": 32.0, "lng": -97.0}, "zoom": 15},
        "observed": [
            {"id": "parcel_boundary", "geometry": parcel},
            {"id": "resolution_point", "geometry": {"type": "Point", "coordinates": [-97.0, 32.0]}},
        ],
        "derived": [{"id": "parcel_centroid", "geometry": {"type": "Point", "coordinates": [-97.0, 32.0]}}],
        "proposed": [{"id": "bess_1", "geometry_local": {"center_xy_m": [0, 0], "width_m": 120, "length_m": 150, "height_m": 4, "rotation_deg": 10}, "components": [{"id": "battery_enclosure_a", "render_class": "building", "geometry_relative": {"center_uv": [0, 0], "width_ratio": .45, "length_ratio": .52, "height_m": 4, "rotation_offset_deg": 0}}, {"id": "reserve", "render_class": "reserve", "geometry_relative": {"center_uv": [.3, .15], "width_ratio": .25, "length_ratio": .3, "height_m": 0, "rotation_offset_deg": 0}}]}],
    }


def _world() -> dict:
    root = "/v1/sandbox/world-snapshots/world-e2e/layers"
    return {"world_snapshot_id": "world-e2e", "query_aoi": {"bbox": [-97.01, 31.99, -96.99, 32.01]}, "layers": [{"layer": name, "availability": "AVAILABLE", "render": {"type": "geojson", "url": f"{root}/{name}"}} for name in ("roads", "buildings", "water", "land_cover")]}


def _feature(geometry: dict, **properties) -> dict:
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def _roads() -> dict:
    return {"type": "FeatureCollection", "features": [_feature({"type": "LineString", "coordinates": [[-97.01, 31.993], [-96.99, 32.007]]}, **{"class": "primary"}), _feature({"type": "LineString", "coordinates": [[-97.007, 32.009], [-96.994, 31.991]]}, **{"class": "secondary"})]}


def _buildings() -> dict:
    return {"type": "FeatureCollection", "features": [_feature({"type": "Polygon", "coordinates": [[[-97.005, 32.004], [-97.003, 32.004], [-97.003, 32.006], [-97.005, 32.006], [-97.005, 32.004]]]}, height_m=12)]}


def _water() -> dict:
    return {"type": "FeatureCollection", "features": [_feature({"type": "Polygon", "coordinates": [[[-97.009, 31.998], [-97.006, 31.998], [-97.006, 32.001], [-97.009, 32.001], [-97.009, 31.998]]]})]}


def _land_cover() -> dict:
    return {"type": "FeatureCollection", "features": [_feature({"type": "Polygon", "coordinates": [[[-97.01, 31.99], [-96.99, 31.99], [-96.99, 32.01], [-97.01, 32.01], [-97.01, 31.99]]]}, subtype="grass")]}
