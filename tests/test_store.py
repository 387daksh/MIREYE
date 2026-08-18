import time
import pytest
from pathlib import Path
from app.workspace.store import WorkspaceStore


@pytest.fixture
def temp_store(tmp_path: Path):
    db_file = tmp_path / "test_workspaces.db"
    return WorkspaceStore(db_path=db_file)


def test_workspace_creation_and_observe(temp_store: WorkspaceStore):
    ws_id = "ws_test_01"
    temp_store.create_workspace(ws_id, label="Texas Solar Pipeline")

    snap = {
        "lat": 32.5,
        "lng": -97.0,
        "fields": {
            "acreage": {"value": 250.0, "status": "ok", "confidence": "high"},
            "slope_pct": {"value": 2.1, "status": "ok", "confidence": "high"},
        },
    }

    # First observation
    v1 = temp_store.observe(ws_id, "PCL-0001", "candidate", "Initial screen passed", snap)
    assert v1 == 1

    # Second observation on same site
    v2 = temp_store.observe(ws_id, "PCL-0001", "shortlisted", "Grid interconnection looks clear", snap)
    assert v2 == 2

    # Another site
    v_other = temp_store.observe(ws_id, "PCL-0002", "rejected", "Wetlands > 30%", snap)
    assert v_other == 1

    # Check state
    state = temp_store.state(ws_id)
    assert len(state) == 2
    pcl1 = next(r for r in state if r["local_key"] == "PCL-0001")
    assert pcl1["version"] == 2
    assert pcl1["status"] == "shortlisted"

    # Check history
    hist = temp_store.history(ws_id, "PCL-0001")
    assert len(hist) == 2
    assert hist[0]["version"] == 1
    assert hist[1]["version"] == 2


def test_workspace_replay(temp_store: WorkspaceStore):
    ws_id = "ws_replay_test"
    snap = {"lat": 33.0, "lng": -96.0, "fields": {}}

    t0 = time.time()
    time.sleep(0.01)
    temp_store.observe(ws_id, "PCL-100", "candidate", "Step 1", snap)
    time.sleep(0.05)
    t1 = time.time()
    time.sleep(0.05)
    temp_store.observe(ws_id, "PCL-100", "rejected", "Step 2", snap)

    # Replay at t0 -> empty
    s0 = temp_store.replay(ws_id, t0)
    assert len(s0) == 0

    # Replay at t1 -> candidate
    s1 = temp_store.replay(ws_id, t1)
    assert len(s1) == 1
    assert s1[0]["status"] == "candidate"

    # Latest state -> rejected
    cur = temp_store.state(ws_id)
    assert cur[0]["status"] == "rejected"


def test_site_id_registration(temp_store: WorkspaceStore):
    ws_id = "ws_site_id_test"
    temp_store.register_site_id(ws_id, "PCL-999", "mireye_site_abc123")
    assert temp_store.get_site_id(ws_id, "PCL-999") == "mireye_site_abc123"
    assert temp_store.get_site_id(ws_id, "PCL-NONEXISTENT") is None
