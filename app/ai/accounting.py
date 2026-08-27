from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any


_usage: ContextVar[dict[str, Any] | None] = ContextVar("orchestration_usage", default=None)


def start(model: str) -> Token:
    return _usage.set({"model": model, "input_tokens": 0, "output_tokens": 0, "model_cost": "UNKNOWN", "mireye_charged_credits": 0.0, "model_usage_by_module": {}})


def record_model(usage: dict[str, Any], cost: Any = None, *, module: str = "unknown") -> None:
    current = _usage.get()
    if current is None:
        return
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    current["input_tokens"] += input_tokens
    current["output_tokens"] += output_tokens
    bucket = current["model_usage_by_module"].setdefault(module, {"input_tokens": 0, "output_tokens": 0})
    bucket["input_tokens"] += input_tokens
    bucket["output_tokens"] += output_tokens
    if isinstance(cost, (int, float)):
        current["model_cost"] = float(cost) + (current["model_cost"] if isinstance(current["model_cost"], float) else 0.0)


def record_mireye(*, quoted: Any = None, charged: Any = None) -> None:
    current = _usage.get()
    if current is None:
        return
    if isinstance(quoted, (int, float)):
        current["mireye_quoted_credits"] = float(quoted) + float(current.get("mireye_quoted_credits", 0))
    if isinstance(charged, (int, float)):
        current["mireye_charged_credits"] += float(charged)


def finish(token: Token) -> dict[str, Any]:
    current = dict(_usage.get() or {})
    _usage.reset(token)
    return current
