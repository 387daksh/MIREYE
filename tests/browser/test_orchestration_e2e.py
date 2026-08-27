from __future__ import annotations

import json
import os

import pytest
from playwright.sync_api import Route, expect, sync_playwright


pytestmark = pytest.mark.runtime


def test_project_orchestration_decision_resume_and_refresh() -> None:
    if os.getenv("MIREYE_RUNTIME_INTEGRATION") != "1":
        pytest.skip("requires the containerized frontend")
    state = {"started": False, "answered": False, "resumed": False}

    def api_fixture(route: Route) -> None:
        request = route.request
        url = request.url
        headers = {"Access-Control-Allow-Origin": "http://localhost:3000", "Access-Control-Allow-Headers": "content-type"}
        if request.method == "OPTIONS":
            return route.fulfill(status=204, headers=headers)
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
            return route.fulfill(json={"world_snapshot_id": "world-e2e", "query_aoi": {"bbox": [-97.01, 31.99, -96.99, 32.01]}, "layers": []}, headers=headers)
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
        page = browser.new_page()
        page.route("http://localhost:8000/**", api_fixture)
        page.goto("http://localhost:3000")
        page.get_by_label("Workspace").fill("workspace-e2e")
        page.get_by_placeholder("Describe the project and constraints").fill("Validate a Texas site")
        page.get_by_placeholder("One real candidate address or coordinate per line").fill("Austin, Texas")
        page.get_by_role("button", name="Create project").click()
        expect(page.get_by_label("Physical-world workspace")).to_be_visible()
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
        page.reload()
        expect(page.get_by_text("MIREYE · Completed", exact=True)).to_be_visible()
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
        "derived": [],
        "proposed": [],
    }
