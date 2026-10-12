"""Database engine, sessions and the unit of work shared by a request."""
from typing import Awaitable, Callable

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base


class Database:
    def __init__(self, url: str, echo: bool = False):
        kwargs: dict = {"echo": echo}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if ":memory:" in url:
                kwargs["poolclass"] = StaticPool  # one shared connection keeps the in-memory DB alive
        else:
            kwargs.update(pool_size=10, max_overflow=20, pool_pre_ping=True)
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", self._sqlite_pragmas)
        self._sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    def session(self) -> AsyncSession:
        return self._sessionmaker()

    def unit_of_work(self) -> "UnitOfWork":
        return UnitOfWork(self.session())

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        await self.engine.dispose()


class UnitOfWork:
    """One transaction per request or job run.

    Side effects that must not happen for a rolled-back transaction (emails, SMS, calls, cache
    invalidation) are registered with `on_commit` and run only after the commit succeeds.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self._after_commit: list[Callable[[], Awaitable[None]]] = []

    def on_commit(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._after_commit.append(callback)

    async def commit(self) -> None:
        await self.session.commit()
        callbacks, self._after_commit = self._after_commit, []
        for callback in callbacks:
            try:
                await callback()
            except Exception:  # noqa: BLE001 - a failed notification must not fail a committed request
                import logging

                logging.getLogger("wamiwa.uow").exception("after-commit hook failed")

    async def rollback(self) -> None:
        self._after_commit.clear()
        await self.session.rollback()

    async def close(self) -> None:
        await self.session.close()

    async def __aenter__(self) -> "UnitOfWork":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                await self.commit()
            else:
                await self.rollback()
        finally:
            await self.close()
