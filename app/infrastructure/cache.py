from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError


class RedisCache:
    """Ephemeral JSON cache; evidence and project state never live here."""

    def __init__(self, url: str, *, client: Redis | None = None):
        self.client = client or Redis.from_url(url, decode_responses=True)

    async def ping(self) -> None:
        await cast(Awaitable[Any], self.client.ping())

    async def close(self) -> None:
        await self.client.aclose()

    async def get_or_set(self, key: str, ttl_seconds: int, loader: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        try:
            cached = await self.client.get(key)
        except RedisError:
            return await loader()
        if cached is not None:
            return json.loads(cached)
        value = await loader()
        try:
            await self.client.set(key, json.dumps(value, sort_keys=True), ex=ttl_seconds)
        except RedisError:
            pass
        return value

    async def invalidate(self, *keys: str) -> int:
        return await self.client.delete(*keys) if keys else 0
