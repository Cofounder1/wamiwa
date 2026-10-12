"""Key-value store abstraction backing OTPs, rate limits, counters and the cache.

Redis in production; an in-process store for development and tests (single worker only).
"""
from abc import ABC, abstractmethod

from app.core.utils import Clock


class KeyValueStore(ABC):
    @abstractmethod
    async def get(self, key: str) -> str | None: ...

    @abstractmethod
    async def set(self, key: str, value: str, ttl: int | None = None) -> None: ...

    @abstractmethod
    async def set_if_absent(self, key: str, value: str, ttl: int | None = None) -> bool:
        """Atomically set `key` only when it does not exist. Returns True when it was set."""

    @abstractmethod
    async def delete(self, *keys: str) -> None: ...

    @abstractmethod
    async def incr(self, key: str, amount: int = 1, ttl: int | None = None) -> int:
        """Atomically add `amount`; `ttl` is applied only when the key is created."""

    @abstractmethod
    async def ttl(self, key: str) -> int:
        """Seconds until expiry; 0 when the key is missing or has no expiry."""

    @abstractmethod
    async def delete_prefix(self, prefix: str) -> None: ...

    async def close(self) -> None:  # noqa: B027 - optional hook
        return None


class InMemoryKeyValueStore(KeyValueStore):
    def __init__(self, clock: Clock):
        self._clock = clock
        self._data: dict[str, tuple[str, float | None]] = {}

    def _live(self, key: str) -> tuple[str, float | None] | None:
        entry = self._data.get(key)
        if entry and entry[1] is not None and entry[1] <= self._clock.timestamp():
            del self._data[key]
            return None
        return entry

    def _expiry(self, ttl: int | None) -> float | None:
        return self._clock.timestamp() + ttl if ttl else None

    async def get(self, key: str) -> str | None:
        entry = self._live(key)
        return entry[0] if entry else None

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self._data[key] = (value, self._expiry(ttl))

    async def set_if_absent(self, key: str, value: str, ttl: int | None = None) -> bool:
        if self._live(key):
            return False
        self._data[key] = (value, self._expiry(ttl))
        return True

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._data.pop(key, None)

    async def incr(self, key: str, amount: int = 1, ttl: int | None = None) -> int:
        entry = self._live(key)
        if entry is None:
            self._data[key] = (str(amount), self._expiry(ttl))
            return amount
        value = int(entry[0]) + amount
        self._data[key] = (str(value), entry[1])
        return value

    async def ttl(self, key: str) -> int:
        entry = self._live(key)
        if not entry or entry[1] is None:
            return 0
        return max(0, int(entry[1] - self._clock.timestamp() + 0.999))

    async def delete_prefix(self, prefix: str) -> None:
        for key in [k for k in self._data if k.startswith(prefix)]:
            del self._data[key]


class RedisKeyValueStore(KeyValueStore):
    _INCR_SCRIPT = """
    local value = redis.call('INCRBY', KEYS[1], ARGV[1])
    if tonumber(ARGV[2]) > 0 and redis.call('TTL', KEYS[1]) < 0 then
        redis.call('EXPIRE', KEYS[1], ARGV[2])
    end
    return value
    """

    def __init__(self, url: str):
        from redis.asyncio import Redis

        self._redis = Redis.from_url(url, decode_responses=True)
        self._incr = self._redis.register_script(self._INCR_SCRIPT)

    async def get(self, key: str) -> str | None:
        return await self._redis.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        await self._redis.set(key, value, ex=ttl or None)

    async def set_if_absent(self, key: str, value: str, ttl: int | None = None) -> bool:
        return bool(await self._redis.set(key, value, ex=ttl or None, nx=True))

    async def delete(self, *keys: str) -> None:
        if keys:
            await self._redis.delete(*keys)

    async def incr(self, key: str, amount: int = 1, ttl: int | None = None) -> int:
        return int(await self._incr(keys=[key], args=[amount, ttl or 0]))

    async def ttl(self, key: str) -> int:
        return max(0, int(await self._redis.ttl(key)))

    async def delete_prefix(self, prefix: str) -> None:
        batch: list[str] = []
        async for key in self._redis.scan_iter(match=f"{prefix}*", count=500):
            batch.append(key)
            if len(batch) >= 500:
                await self._redis.delete(*batch)
                batch.clear()
        if batch:
            await self._redis.delete(*batch)

    async def close(self) -> None:
        await self._redis.aclose()
