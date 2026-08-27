from __future__ import annotations

import inspect
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

from app.ai.schemas.orchestration import AgentRole
from app.infrastructure.observability import span


class ToolEffect(str, Enum):
    READ_ONLY = "READ_ONLY"
    MUTATION = "MUTATION"


@dataclass(frozen=True)
class ToolPolicy:
    name: str
    description: str
    input_schema: dict[str, Any]
    required_scopes: frozenset[str]
    effect: ToolEffect
    metered: bool
    confirmation_required: bool
    allowed_agents: frozenset[AgentRole]
    postcondition: str | None = None
    evidence_requirements: tuple[str, ...] = ()


class ToolPolicyError(ValueError):
    pass


ToolHandler = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]


class PolicyToolRegistry:
    def __init__(self):
        self._tools: dict[str, tuple[ToolPolicy, ToolHandler]] = {}

    def register(self, policy: ToolPolicy, handler: ToolHandler) -> None:
        if policy.name in self._tools:
            raise ValueError(f"Tool already registered: {policy.name}")
        self._tools[policy.name] = (policy, handler)

    def schemas(self, role: AgentRole | None = None) -> list[dict[str, Any]]:
        return [
            {"name": policy.name, "description": policy.description, "parameters": policy.input_schema}
            for policy, _handler in self._tools.values()
            if role is None or role in policy.allowed_agents
        ]

    def policy(self, name: str) -> ToolPolicy:
        if name not in self._tools:
            raise ToolPolicyError(f"Unknown tool: {name}")
        return self._tools[name][0]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        role: AgentRole,
        granted_scopes: set[str],
        application_confirmation: bool = False,
        before_state_hash: str | None = None,
        available_evidence: set[str] | None = None,
    ) -> dict[str, Any]:
        if name not in self._tools:
            raise ToolPolicyError(f"Unknown tool: {name}")
        policy, handler = self._tools[name]
        if role not in policy.allowed_agents:
            raise ToolPolicyError(f"{role.value} is not allowed to use {name}")
        if not policy.required_scopes <= granted_scopes:
            raise ToolPolicyError(f"Missing application-granted scopes for {name}")
        if (policy.metered or policy.confirmation_required) and not application_confirmation:
            raise ToolPolicyError(f"{name} requires application confirmation")
        if not set(policy.evidence_requirements) <= (available_evidence or set()):
            raise ToolPolicyError(f"{name} is missing required evidence")
        _validate_object(arguments, policy.input_schema)
        with span("ai.tool", **{"tool.name": name, "agent.role": role.value, "tool.metered": policy.metered}):
            result = handler(arguments)
            if inspect.isawaitable(result):
                result = await result
        if not isinstance(result, dict):
            raise ToolPolicyError(f"{name} returned an invalid result")
        if policy.effect == ToolEffect.MUTATION and policy.postcondition == "STATE_HASH_CHANGED":
            after = result.get("state_hash")
            if not before_state_hash or not isinstance(after, str) or after == before_state_hash:
                raise ToolPolicyError(f"{name} did not satisfy STATE_HASH_CHANGED")
        return result


def _validate_object(value: dict[str, Any], schema: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise ToolPolicyError("Tool arguments must be an object")
    properties = schema.get("properties", {})
    missing = set(schema.get("required", [])) - set(value)
    extra = set(value) - set(properties)
    if missing or extra:
        raise ToolPolicyError(f"Invalid tool arguments; missing={sorted(missing)}, extra={sorted(extra)}")
    for key, item in value.items():
        expected = properties[key].get("type")
        if expected == "string" and not isinstance(item, str):
            raise ToolPolicyError(f"{key} must be a string")
        if expected == "array" and not isinstance(item, list):
            raise ToolPolicyError(f"{key} must be an array")
        if expected == "boolean" and not isinstance(item, bool):
            raise ToolPolicyError(f"{key} must be boolean")
        if expected == "number" and (isinstance(item, bool) or not isinstance(item, (int, float))):
            raise ToolPolicyError(f"{key} must be numeric")
