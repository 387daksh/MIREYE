import pytest
from fastapi.testclient import TestClient
from app.main import app, diligence_service


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as value:
        yield value


def test_root_dashboard(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Mireye" in response.text


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_meta_fields(client):
    response = client.get("/v1/meta/fields")
    assert response.status_code == 200
    data = response.json()
    assert "total_fields" in data
    assert len(data["fields"]) > 0


def test_screen_parcels(client):
    payload = {
        "min_acreage": 20.0,
        "max_slope_pct": 15.0,
        "limit": 5,
        "apply_confidence_scoring": True,
    }
    response = client.post("/v1/screen", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "candidates_found" in data
    assert "shortlist" in data
    if data["candidates_found"] > 0:
        assert "confidence_score" in data["shortlist"][0]


def test_ask_parcel(client):
    payload = {
        "lat": 32.5,
        "lng": -97.0,
        "preset": "site_selection",
    }
    response = client.post("/v1/ask", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "dossier" in data
    assert "confidence" in data
    assert data["dossier"]["ok"] is True


def test_grid_capacity(client):
    response = client.get("/v1/grid?lat=32.5&lng=-97.0&target_capacity_mw=75.0")
    assert response.status_code == 200
    data = response.json()
    assert "substation" in data
    assert "feasibility" in data


def test_workspace_lifecycle(client):
    import time
    ws_id = f"ws_api_test_{int(time.time() * 1000)}"

    # 1. Open
    r1 = client.post("/v1/workspace/open", json={"workspace_id": ws_id, "label": "API Test"})
    assert r1.status_code == 200

    # 2. Observe
    obs_payload = {
        "workspace_id": ws_id,
        "local_key": "PCL-000005",
        "status": "shortlisted",
        "justification": "Optimal wind speed and flat slope",
        "lat": 32.5,
        "lng": -97.0,
    }
    r2 = client.post("/v1/workspace/observe", json=obs_payload)
    assert r2.status_code == 200
    assert r2.json()["version"] == 1

    # 3. State
    r3 = client.get(f"/v1/workspace/{ws_id}/state")
    assert r3.status_code == 200
    state = r3.json()
    assert len(state["shortlisted"]) == 1

    # 4. Invalidate
    r4 = client.post(f"/v1/workspace/{ws_id}/invalidate")
    assert r4.status_code == 200
    assert "stale_fields" in r4.json()

    # 5. History
    r5 = client.get(f"/v1/workspace/{ws_id}/history/PCL-000005")
    assert r5.status_code == 200
    assert len(r5.json()["history"]) == 1


def test_memory_retrieval_enforces_project_workspace(client):
    project = diligence_service.create_project(
        workspace_id="workspace-memory-api", message="Compare a 100 MW data center site.", candidates=["1032 Robotic Ave"]
    )
    path = f"/v1/diligence/projects/{project['project_id']}/memory/search?query=power"
    denied = client.get(path, headers={"X-Mireye-Workspace-Id": "another-workspace"})
    allowed = client.get(path, headers={"X-Mireye-Workspace-Id": "workspace-memory-api"})

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["project_id"] == project["project_id"]
