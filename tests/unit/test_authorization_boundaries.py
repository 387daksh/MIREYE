from fastapi.testclient import TestClient

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
