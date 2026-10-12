"""Generic async repository with keyset pagination."""
from typing import Any, Generic, Sequence, TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound
from app.core.utils import Cursor, Page
from app.models.base import Model

M = TypeVar("M", bound=Model)


class Repository(Generic[M]):
    def __init__(self, session: AsyncSession, model: type[M]):
        self.session = session
        self.model = model

    async def get(self, entity_id: str | None) -> M | None:
        if not entity_id:
            return None
        return await self.session.get(self.model, entity_id)

    async def require(self, entity_id: str | None, message: str | None = None) -> M:
        entity = await self.get(entity_id)
        if entity is None:
            raise NotFound(message or f"{self.model.__name__} not found")
        return entity

    async def lock(self, entity_id: str, message: str | None = None) -> M:
        """Load a fresh copy under a row lock (SELECT ... FOR UPDATE; a no-op on SQLite).

        Used wherever money or a claim is at stake so two requests cannot both win.
        """
        stmt = select(self.model).where(self.model.id == entity_id).with_for_update()
        entity = (await self.session.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()
        if entity is None:
            raise NotFound(message or f"{self.model.__name__} not found")
        return entity

    async def add(self, entity: M) -> M:
        """Insert and flush immediately so rows hit the database in program order."""
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def delete(self, entity: M) -> None:
        await self.session.delete(entity)
        await self.session.flush()

    async def first(self, *where: Any, order_by: Any = None) -> M | None:
        stmt = select(self.model).where(*where).limit(1)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(self, *where: Any, order_by: Any = None, limit: int | None = None) -> Sequence[M]:
        stmt = select(self.model).where(*where)
        stmt = stmt.order_by(order_by if order_by is not None else self.model.id)
        if limit:
            stmt = stmt.limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def by_ids(self, ids: Sequence[str]) -> dict[str, M]:
        ids = [i for i in set(ids) if i]
        if not ids:
            return {}
        rows = (await self.session.execute(select(self.model).where(self.model.id.in_(ids)))).scalars().all()
        return {row.id: row for row in rows}

    async def count(self, *where: Any) -> int:
        stmt = select(func.count()).select_from(self.model).where(*where)
        return int((await self.session.execute(stmt)).scalar_one())

    async def sum(self, column: Any, *where: Any) -> int:
        stmt = select(func.coalesce(func.sum(column), 0)).where(*where)
        return int((await self.session.execute(stmt)).scalar_one())

    async def exists(self, *where: Any) -> bool:
        return await self.first(*where) is not None

    async def page(self, *where: Any, cursor: str | None = None, limit: int = 25, newest_first: bool = True) -> Page[M]:
        """Keyset pagination on the time-ordered id: constant cost at any depth."""
        limit = max(1, min(limit, 100))
        last_id = Cursor.decode(cursor)
        stmt = select(self.model).where(*where)
        if last_id:
            stmt = stmt.where(self.model.id < last_id if newest_first else self.model.id > last_id)
        stmt = stmt.order_by(self.model.id.desc() if newest_first else self.model.id.asc()).limit(limit + 1)
        rows = list((await self.session.execute(stmt)).scalars().all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        return Page(items=rows, next_cursor=Cursor.encode(rows[-1].id) if has_more and rows else None)
