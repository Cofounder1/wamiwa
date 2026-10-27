"""Test mode until a Super Admin presses Go live; live payment keys after, for good.

`LaunchState` answers "has Wamiwa gone live?" from the database. Each process remembers the answer briefly, and the
shared cache spreads a launch to every worker within a minute. Once live, it is never asked again.

`LaunchSwitch` stands in for a provider: every call goes to the test provider before launch, the live one after.
"""
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from app.infra.database import Database
from app.infra.kv import KeyValueStore

KEY = "platform:launched"


class LaunchState:
    def __init__(self, db: Database, kv: KeyValueStore, *, recheck_seconds: float = 30.0):
        self._db, self._kv, self._recheck = db, kv, recheck_seconds
        self._live: bool | None = None
        self._until = 0.0

    async def is_live(self) -> bool:
        if self._live or (self._live is not None and time.monotonic() < self._until):
            return bool(self._live)
        shared = await self._kv.get(KEY)
        if shared is None:
            from app.models import PlatformState

            uow = self._db.unit_of_work()
            try:
                launched = (await uow.session.execute(
                    select(PlatformState.id).where(PlatformState.launched_at.is_not(None)).limit(1))).first()
            finally:
                await uow.close()
            shared = "1" if launched else "0"
            await self._kv.set(KEY, shared, None if launched else 60)
        self._live, self._until = shared == "1", time.monotonic() + self._recheck
        return self._live

    async def mark_live(self) -> None:
        """Called once the launch is saved: this process switches at once, the others within a minute."""
        await self._kv.set(KEY, "1")
        self._live = True


class LaunchSwitch:
    """Every method call goes to `test` before launch and to `live` after."""

    def __init__(self, test: Any, live: Any, is_live: Callable[[], Awaitable[bool]]):
        self.test, self.live, self._is_live = test, live, is_live

    async def current(self) -> Any:
        return self.live if await self._is_live() else self.test

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(*args, **kwargs):
            return await getattr(await self.current(), name)(*args, **kwargs)

        return call
