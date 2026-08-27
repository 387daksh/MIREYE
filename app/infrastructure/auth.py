from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Protocol

from fastapi import Header, Request


class Role(str, Enum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"
    VIEWER = "VIEWER"


_PERMISSIONS = {
    Role.OWNER: frozenset(
        {
            "project:read",
            "project:write",
            "evidence:read",
            "evidence:refresh",
            "scenario:read",
            "scenario:mutate",
            "orchestration:run",
            "rfi:create",
            "rfi:approve",
            "rfi:send",
            "workspace:admin",
        }
    ),
    Role.ADMIN: frozenset(
        {
            "project:read",
            "project:write",
            "evidence:read",
            "evidence:refresh",
            "scenario:read",
            "scenario:mutate",
            "orchestration:run",
            "rfi:create",
            "rfi:approve",
            "rfi:send",
        }
    ),
    Role.MEMBER: frozenset(
        {"project:read", "project:write", "evidence:read", "scenario:read", "scenario:mutate", "orchestration:run", "rfi:create"}
    ),
    Role.VIEWER: frozenset({"project:read", "evidence:read", "scenario:read"}),
}


@dataclass(frozen=True)
class RequestContext:
    user_id: str
    organization_id: str
    workspace_id: str | None
    roles: frozenset[Role]

    def allows(self, permission: str) -> bool:
        return any(permission in _PERMISSIONS[role] for role in self.roles)


class AuthProvider(Protocol):
    async def authenticate(self, headers: Mapping[str, str]) -> RequestContext: ...


class LocalAuthProvider:
    async def authenticate(self, headers: Mapping[str, str]) -> RequestContext:
        return context_from_values(
            user_id=headers.get("x-mireye-user-id"),
            organization_id=headers.get("x-mireye-organization-id"),
            workspace_id=headers.get("x-mireye-workspace-id"),
            roles=headers.get("x-mireye-roles"),
        )


class OIDCAuthProvider:
    """Provider-neutral OIDC boundary; token verification is injected by deployment."""

    def __init__(self, verify: Callable[[str], Awaitable[dict[str, Any]]]):
        self.verify = verify

    async def authenticate(self, headers: Mapping[str, str]) -> RequestContext:
        authorization = headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise PermissionError("Bearer authentication is required.")
        claims = await self.verify(authorization.removeprefix("Bearer "))
        return context_from_values(
            user_id=str(claims["sub"]),
            organization_id=str(claims["organization_id"]),
            workspace_id=str(claims["workspace_id"]),
            roles=",".join(claims.get("roles", [])),
        )


def context_from_values(*, user_id: str | None, organization_id: str | None, workspace_id: str | None, roles: str | None) -> RequestContext:
    return RequestContext(
        user_id=user_id or "local-user",
        organization_id=organization_id or "local-org",
        workspace_id=workspace_id,
        roles=frozenset(Role(item.strip().upper()) for item in (roles or "OWNER").split(",")),
    )


async def local_request_context(
    x_mireye_user_id: str | None = Header(default=None),
    x_mireye_organization_id: str | None = Header(default=None),
    x_mireye_workspace_id: str | None = Header(default=None),
    x_mireye_roles: str | None = Header(default=None),
) -> RequestContext:
    return context_from_values(
        user_id=x_mireye_user_id, organization_id=x_mireye_organization_id, workspace_id=x_mireye_workspace_id, roles=x_mireye_roles
    )


def request_context(request: Request) -> RequestContext:
    return request.state.context
