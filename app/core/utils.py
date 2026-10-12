"""Small shared building blocks: clock, ids, cursor pagination, money maths, working days."""
import base64
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Generic, Sequence, TypeVar

from app.core.exceptions import Unprocessable

T = TypeVar("T")


class Clock:
    """Source of time. Injected everywhere so tests can travel in time."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def today(self) -> date:
        return self.now().date()

    def timestamp(self) -> float:
        return self.now().timestamp()


class FrozenClock(Clock):
    def __init__(self, start: datetime | None = None):
        self._now = start or datetime.now(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: float) -> datetime:
        self._now += timedelta(**delta)
        return self._now


def new_id() -> str:
    """Time-ordered 32-char id (ms timestamp + randomness), so `ORDER BY id` is creation order."""
    return f"{time.time_ns() // 1_000_000:012x}{secrets.token_hex(10)}"


_default_clock: Clock = Clock()


def set_default_clock(clock: Clock) -> None:
    """Make row timestamps (created_at / updated_at) follow the application's clock."""
    global _default_clock
    _default_clock = clock


def utcnow() -> datetime:
    return _default_clock.now()


HANDLE_RE = re.compile(r"^[a-z0-9_]{3,30}$")
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{3,30})")


def normalize_handle(handle: str) -> str:
    handle = handle.strip().lstrip("@").lower()
    if not HANDLE_RE.match(handle):
        raise Unprocessable("Handle must be 3-30 characters: letters, numbers or underscores", code="invalid_handle")
    return handle


def extract_mentions(text: str) -> list[str]:
    """Distinct @handles in the order they appear."""
    seen: dict[str, None] = {}
    for match in MENTION_RE.finditer(text or ""):
        seen.setdefault(match.group(1).lower(), None)
    return list(seen)


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"[^\d+]", "", phone or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not re.fullmatch(r"\+?\d{7,15}", digits):
        raise Unprocessable("Enter a valid phone number in international format", code="invalid_phone")
    return digits


def normalize_name(name: str) -> str:
    return " ".join(sorted(re.sub(r"[^a-z ]", "", (name or "").lower()).split()))


def names_match(a: str, b: str) -> bool:
    """Order-insensitive comparison; a bank's "ADE BOLA" matches an ID's "Bola Ade"."""
    na, nb = normalize_name(a), normalize_name(b)
    return bool(na) and (na == nb or set(na.split()) <= set(nb.split()) or set(nb.split()) <= set(na.split()))


def age_on(date_of_birth: date, today: date) -> int:
    years = today.year - date_of_birth.year
    if (today.month, today.day) < (date_of_birth.month, date_of_birth.day):
        years -= 1
    return years


def add_working_days(start: datetime, days: int) -> datetime:
    current = start
    while days > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            days -= 1
    return current


def pct_of(amount: int, pct: int | float | Decimal) -> int:
    """Percentage of an integer amount of minor units, rounded half up."""
    return int((Decimal(amount) * Decimal(str(pct)) / Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def convert_minor(amount_cents: int, rate: Decimal) -> int:
    """USD cents -> local minor units at `rate` local units per USD."""
    return int((Decimal(amount_cents) * rate).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class FeeBreakdown:
    """PY-01: the fee comes out of the price, never on top."""

    price: int
    fee_pct: int
    fee: int
    net: int

    @classmethod
    def of(cls, price: int, fee_pct: int) -> "FeeBreakdown":
        fee = pct_of(price, fee_pct)
        return cls(price=price, fee_pct=fee_pct, fee=fee, net=price - fee)

    def to_dict(self) -> dict[str, int]:
        return {"price": self.price, "fee_pct": self.fee_pct, "fee": self.fee, "net": self.net}


@dataclass
class Page(Generic[T]):
    items: Sequence[T]
    next_cursor: str | None = None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None

    def meta(self) -> dict[str, Any]:
        return {"count": len(self.items), "has_more": self.has_more, "next_cursor": self.next_cursor}


class Cursor:
    """Opaque keyset cursor: base64url JSON holding the last seen sort key."""

    @staticmethod
    def encode(value: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps({"k": value}).encode()).decode().rstrip("=")

    @staticmethod
    def decode(cursor: str | None) -> Any:
        if not cursor:
            return None
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            return json.loads(base64.urlsafe_b64decode(padded.encode()))["k"]
        except Exception as exc:  # noqa: BLE001 - any malformed cursor is a client error
            raise Unprocessable("Invalid pagination cursor", code="invalid_cursor") from exc
