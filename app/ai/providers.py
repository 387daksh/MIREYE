from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, MODEL_PRICING, OPENAI_API_KEY, SANDBOX_AGENT_MODEL, SANDBOX_AGENT_REASONING_EFFORT
from app.ai.accounting import record_model
from app.infrastructure.observability import record_model_usage, span
from app.sandbox_agent import ModelUnavailableError


class OpenAIStructuredModelProvider:
    """Responses API adapter for strict typed orchestration modules."""

    def __init__(
        self,
        api_key: str = OPENAI_API_KEY,
        model: str = SANDBOX_AGENT_MODEL,
        reasoning_effort: str = SANDBOX_AGENT_REASONING_EFFORT,
    ):
        self.api_key, self.model, self.reasoning_effort = api_key, model, reasoning_effort
        self.pricing = MODEL_PRICING.get(model)

    async def generate(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise ModelUnavailableError("AI orchestration requires OPENAI_API_KEY configuration.")
        payload = {
            "model": self.model,
            "reasoning": {"effort": self.reasoning_effort},
            "instructions": request["instructions"],
            "input": json.dumps(request["input"], sort_keys=True, default=str),
            "text": {"format": {"type": "json_schema", "name": request["schema_name"], "strict": False, "schema": request["schema"]}},
            "store": False,
        }
        with span("ai.model", **{"ai.module": request["module"], "gen_ai.request.model": self.model}):
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
                try:
                    response = await client.post(
                        "https://api.openai.com/v1/responses",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    detail = response.json().get("error", {}).get("message", "OpenAI rejected the orchestration request.")
                    raise ModelUnavailableError(detail) from exc
                except httpx.RequestError as exc:
                    raise ModelUnavailableError("OpenAI orchestration is temporarily unavailable.") from exc
        body = response.json()
        record_model_usage(self.model, body.get("usage") or {})
        usage = body.get("usage") or {}
        cost = body.get("cost_usd")
        if not isinstance(cost, (int, float)) and self.pricing:
            cost = (
                float(usage.get("input_tokens") or 0) * self.pricing.get("input_per_million_usd", 0)
                + float(usage.get("output_tokens") or 0) * self.pricing.get("output_per_million_usd", 0)
            ) / 1_000_000
        record_model(usage, cost, module=request["module"])
        text = body.get("output_text") or "".join(
            part.get("text", "") for item in body.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text"
        )
        try:
            return json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ModelUnavailableError("OpenAI returned invalid structured orchestration output.") from exc


class OpenAIEmbeddingProvider:
    """OpenAI embeddings adapter used only to rank already-authoritative records."""

    def __init__(
        self,
        api_key: str = OPENAI_API_KEY,
        model: str = EMBEDDING_MODEL,
        dimensions: int = EMBEDDING_DIMENSIONS,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key, self.model, self.dimensions, self.transport = api_key, model, dimensions, transport

    async def embed(self, texts: list[str]) -> dict[str, Any]:
        if not self.api_key:
            raise ModelUnavailableError("Document retrieval requires OPENAI_API_KEY configuration.")
        if not texts or any(not item.strip() for item in texts):
            raise ValueError("Embedding input must contain non-empty text.")
        payload: dict[str, Any] = {"model": self.model, "input": texts, "dimensions": self.dimensions}
        with span("ai.embedding", **{"gen_ai.request.model": self.model, "ai.embedding.inputs": len(texts)}):
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), transport=self.transport) as client:
                try:
                    response = await client.post(
                        "https://api.openai.com/v1/embeddings",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    detail = response.json().get("error", {}).get("message", "OpenAI rejected the embedding request.")
                    raise ModelUnavailableError(detail) from exc
                except httpx.RequestError as exc:
                    raise ModelUnavailableError("OpenAI embeddings are temporarily unavailable.") from exc
        body = response.json()
        vectors = [item.get("embedding") for item in body.get("data", [])]
        if len(vectors) != len(texts) or any(not isinstance(vector, list) or len(vector) != self.dimensions for vector in vectors):
            raise ModelUnavailableError("OpenAI returned embeddings with an unexpected dimension.")
        return {"model": body.get("model", self.model), "dimensions": self.dimensions, "vectors": vectors, "usage": body.get("usage") or {}, "cost": "UNKNOWN"}
