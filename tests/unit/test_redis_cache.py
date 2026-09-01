import asyncio

from redis.exceptions import ConnectionError as RedisConnectionError

from app.infrastructure.cache import RedisCache


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttl = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex):
        self.values[key] = value
        self.ttl[key] = ex

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
        return len(keys)

    async def ping(self):
        return True

    async def aclose(self):
        return None


def test_cache_has_explicit_ttl_and_invalidation():
    async def check():
        redis = FakeRedis()
        cache = RedisCache("redis://unused", client=redis)
        calls = 0

        async def load():
            nonlocal calls
            calls += 1
            return {"real": True}

        assert await cache.get_or_set("catalog", 3600, load) == {"real": True}
        assert await cache.get_or_set("catalog", 3600, load) == {"real": True}
        assert calls == 1 and redis.ttl["catalog"] == 3600
        await cache.invalidate("catalog")
        assert await cache.get_or_set("catalog", 60, load) == {"real": True}
        assert calls == 2

    asyncio.run(check())


def test_cache_falls_back_to_loader_when_redis_is_down():
    class UnavailableRedis(FakeRedis):
        async def get(self, key):
            raise RedisConnectionError("redis unavailable")

    async def check():
        cache = RedisCache("redis://unused", client=UnavailableRedis())

        async def load():
            return {"real": True}

        assert await cache.get_or_set("catalog", 3600, load) == {"real": True}

    asyncio.run(check())
