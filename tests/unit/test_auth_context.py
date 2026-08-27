import asyncio

from app.infrastructure.auth import local_request_context


def test_local_context_maps_roles_to_permissions():
    context = asyncio.run(local_request_context(x_mireye_roles="MEMBER"))
    assert context.allows("orchestration:run")
    assert not context.allows("workspace:admin")
    viewer = asyncio.run(local_request_context(x_mireye_roles="VIEWER"))
    assert viewer.allows("project:read")
    assert not viewer.allows("orchestration:run")
