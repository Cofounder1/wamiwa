"""Declarative base, shared column types and append-only enforcement."""
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.core.utils import new_id, utcnow


class UTCDateTime(TypeDecorator):
    """Always timezone-aware UTC in Python, whatever the database driver returns."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class Model(Base):
    __abstract__ = True

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class AppendOnlyError(RuntimeError):
    pass


def append_only(cls):
    """Class decorator: rows can be inserted but never updated or deleted (AD-04, section 20)."""

    def _refuse(_mapper, _connection, target):
        raise AppendOnlyError(f"{type(target).__name__} is append-only")

    event.listen(cls, "before_update", _refuse)
    event.listen(cls, "before_delete", _refuse)
    return cls
