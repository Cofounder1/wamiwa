"""JSON read-through cache on top of the key-value store."""
import json
from typing import Any, Awaitable, Callable

from app.infra.kv import KeyValueStore


class CacheService:
    """Namespaced cache. Keys are grouped by prefix so a write can drop a whole family at once.

    Only derived, re-computable data is cached; money and permission checks always read the database.
    """

    def __init__(self, kv: KeyValueStore, namespace: str = "cache"):
        self._kv = kv
        self._namespace = namespace

    def _key(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    async def get(self, key: str) -> Any | None:
        raw = await self._kv.get(self._key(key))
        return json.loads(raw) if raw is not None else None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        await self._kv.set(self._key(key), json.dumps(value, default=str), ttl)

    async def delete(self, *keys: str) -> None:
        await self._kv.delete(*(self._key(k) for k in keys))

    async def invalidate_prefix(self, prefix: str) -> None:
        await self._kv.delete_prefix(self._key(prefix))

    async def get_or_set(self, key: str, ttl: int, loader: Callable[[], Awaitable[Any]]) -> Any:
        cached = await self.get(key)
        if cached is not None:
            return cached
        value = await loader()
        if value is not None:
            await self.set(key, value, ttl)
        return value
