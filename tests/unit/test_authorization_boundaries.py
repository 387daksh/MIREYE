from fastapi.testclient import TestClient
from types import SimpleNamespace

from app import main


def headers(workspace: str, role: str) -> dict[str, str]:
    return {"X-Mireye-Workspace-Id": workspace, "X-Mireye-Roles": role}


def test_roles_and_workspace_ownership_are_enforced_at_api_boundary() -> None:
    with TestClient(main.app) as client:
        created = client.post(
            "/v1/diligence/projects",
            headers=headers("workspace-a", "OWNER"),
            json={"workspace_id": "workspace-a", "message": "Check one site", "candidates": [{"address": "Austin, Texas"}]},
        )
        assert created.status_code == 200
        project_id = created.json()["project_id"]

        assert client.get(f"/v1/diligence/projects/{project_id}", headers=headers("workspace-a", "VIEWER")).status_code == 200
        assert client.post(f"/v1/diligence/projects/{project_id}/next-actions", headers=headers("workspace-a", "VIEWER")).status_code == 403
        assert (
            client.post(
                f"/v1/ai/projects/{project_id}/orchestrate", headers=headers("workspace-a", "VIEWER"), json={"message": "Investigate"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/v1/diligence/projects/{project_id}/sites/site-a/sources/refresh", headers=headers("workspace-a", "MEMBER")
            ).status_code
            == 403
        )
        assert (
            client.get(f"/v1/ai/projects/{project_id}/orchestration/run-a/events", headers=headers("workspace-b", "OWNER")).status_code
            == 403
        )
        assert client.get(f"/v1/diligence/projects/{project_id}", headers=headers("workspace-b", "OWNER")).status_code == 403
        assert (
            client.post(
                "/v1/workspace/open", headers=headers("workspace-a", "VIEWER"), json={"workspace_id": "workspace-a", "label": "Denied"}
            ).status_code
            == 403
        )


def test_temporal_resume_requires_a_persisted_decision_answer(monkeypatch) -> None:
    class Executor:
        signalled = False

        async def signal_decision(self, run_id: str) -> None:
            self.signalled = True

    executor = Executor()
    monkeypatch.setattr(main, "temporal_executor", executor)
    monkeypatch.setattr(main.orchestration_engine, "get_run", lambda *_: SimpleNamespace(status="WAITING_FOR_DECISION"))
    with TestClient(main.app) as client:
        created = client.post(
            "/v1/diligence/projects",
            headers=headers("workspace-resume", "OWNER"),
            json={"workspace_id": "workspace-resume", "message": "Check one site", "candidates": [{"address": "Austin, Texas"}]},
        )
        project_id = created.json()["project_id"]
        project = main.diligence_service.get(project_id)
        project["active_decision"] = {"decision_id": "decision-pending"}
        main.diligence_service.store.save_diligence_project(project)
        response = client.post(
            f"/v1/ai/projects/{project_id}/orchestration/run-pending/resume",
            headers=headers("workspace-resume", "OWNER"),
        )

    assert response.status_code == 400
    assert "must be answered" in response.json()["detail"]
    assert executor.signalled is False
