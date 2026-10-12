"""Sliding-window rate limiter and the catalogue of limits used across the API."""
from dataclasses import dataclass

from app.core.config import Settings
from app.core.exceptions import RateLimited
from app.core.utils import Clock
from app.infra.kv import KeyValueStore


@dataclass(frozen=True)
class RateLimitPolicy:
    name: str
    limit: int
    window_seconds: int


@dataclass(frozen=True)
class RateLimitResult:
    policy: RateLimitPolicy
    allowed: bool
    remaining: int
    reset_at: int
    retry_after: int

    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.policy.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_at),
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after)
        return headers

    def raise_if_blocked(self, message: str | None = None, **extra) -> "RateLimitResult":
        if not self.allowed:
            raise RateLimited(
                message or "Rate limit exceeded",
                retry_after=self.retry_after,
                limit=self.policy.limit,
                reset_at=self.reset_at,
                policy=self.policy.name,
                **extra,
            )
        return self


class RateLimiter:
    """Sliding-window counter: the previous window is weighted by how much of it still overlaps.

    Smoother than a fixed window (no double burst at the boundary) and needs only two counters
    per key, so it works identically on Redis and in memory.
    """

    def __init__(self, kv: KeyValueStore, clock: Clock):
        self._kv = kv
        self._clock = clock

    def _keys(self, policy: RateLimitPolicy, identifier: str, window: int) -> tuple[str, str]:
        base = f"rl:{policy.name}:{identifier}"
        return f"{base}:{window}", f"{base}:{window - 1}"

    async def _estimate(self, policy: RateLimitPolicy, identifier: str) -> tuple[float, int, float, str]:
        now = self._clock.timestamp()
        window = int(now // policy.window_seconds)
        elapsed = (now % policy.window_seconds) / policy.window_seconds
        current_key, previous_key = self._keys(policy, identifier, window)
        current = int(await self._kv.get(current_key) or 0)
        previous = int(await self._kv.get(previous_key) or 0)
        return previous * (1 - elapsed) + current, window, now, current_key

    def _result(self, policy: RateLimitPolicy, used: float, window: int, now: float, allowed: bool) -> RateLimitResult:
        reset_at = (window + 1) * policy.window_seconds
        return RateLimitResult(
            policy=policy,
            allowed=allowed,
            remaining=max(0, int(policy.limit - used)),
            reset_at=reset_at,
            retry_after=0 if allowed else max(1, int(reset_at - now + 0.999)),
        )

    async def peek(self, policy: RateLimitPolicy, identifier: str) -> RateLimitResult:
        used, window, now, _ = await self._estimate(policy, identifier)
        return self._result(policy, used, window, now, used < policy.limit)

    async def hit(self, policy: RateLimitPolicy, identifier: str, cost: int = 1) -> RateLimitResult:
        used, window, now, current_key = await self._estimate(policy, identifier)
        if used + cost > policy.limit:
            return self._result(policy, used, window, now, False)
        # Counters live for two windows so the next window can still weigh this one.
        await self._kv.incr(current_key, cost, ttl=policy.window_seconds * 2)
        return self._result(policy, used + cost, window, now, True)

    async def enforce(self, policy: RateLimitPolicy, identifier: str, message: str | None = None, **extra) -> RateLimitResult:
        return (await self.hit(policy, identifier)).raise_if_blocked(message, **extra)

    async def refund(self, policy: RateLimitPolicy, identifier: str, cost: int = 1) -> None:
        """Give a hit back when the guarded action failed for an unrelated reason."""
        _, _, _, current_key = await self._estimate(policy, identifier)
        if int(await self._kv.get(current_key) or 0) >= cost:
            await self._kv.incr(current_key, -cost)

    async def reset(self, policy: RateLimitPolicy, identifier: str) -> None:
        await self._kv.delete_prefix(f"rl:{policy.name}:{identifier}:")


MINUTE, HOUR, DAY = 60, 3600, 86400


class RateLimitPolicies:
    """Every limit in one place. Values marked PRD come from the PRD's section 20 table."""

    def __init__(self, s: Settings):
        # General request budgets, per principal (or per IP when anonymous).
        self.human_read = RateLimitPolicy("human_read", s.human_reads_per_minute, MINUTE)
        self.human_write = RateLimitPolicy("human_write", s.human_writes_per_minute, MINUTE)
        self.machine_read = RateLimitPolicy("machine_read", s.machine_reads_per_minute, MINUTE)
        self.machine_write = RateLimitPolicy("machine_write", s.machine_writes_per_minute, MINUTE)
        self.anonymous = RateLimitPolicy("anonymous", s.anonymous_requests_per_minute, MINUTE)
        # Auth abuse.
        self.otp_resend = RateLimitPolicy("otp_resend", s.otp_resends_per_hour, HOUR)  # PRD
        self.otp_ip = RateLimitPolicy("otp_ip", 20, HOUR)
        self.login = RateLimitPolicy("login", s.logins_per_hour, HOUR)
        self.login_ip = RateLimitPolicy("login_ip", 50, HOUR)
        self.password_reset = RateLimitPolicy("password_reset", 3, HOUR)
        # Agents and company APIs.
        self.agent_comments = RateLimitPolicy("agent_comments", s.agent_comments_per_hour, HOUR)  # PRD
        self.agent_posts = RateLimitPolicy("agent_posts", 30, HOUR)
        self.api_posts = RateLimitPolicy("api_posts", s.api_posts_per_hour, HOUR)  # PRD
        self.api_replies = RateLimitPolicy("api_replies", s.api_replies_per_hour, HOUR)  # PRD
        self.b2b_conversations = RateLimitPolicy("b2b_conversations", s.b2b_new_conversations_per_day, DAY)  # PRD
        # Abuse-prone human actions.
        self.posts = RateLimitPolicy("posts", 30, HOUR)
        self.comments = RateLimitPolicy("comments", 120, HOUR)
        self.follows = RateLimitPolicy("follows", 200, HOUR)
        self.messages = RateLimitPolicy("messages", 120, MINUTE)
        self.new_conversations = RateLimitPolicy("new_conversations", 40, DAY)
        self.reports = RateLimitPolicy("reports", 20, HOUR)
        self.support_tickets = RateLimitPolicy("support_tickets", 5, HOUR)
        self.search = RateLimitPolicy("search", 30, MINUTE)
        self.guardian_invites = RateLimitPolicy("guardian_invites", 5, DAY)
        self.friend_invites = RateLimitPolicy("friend_invites", 50, DAY)
        self.id_verification = RateLimitPolicy("id_verification", 5, DAY)
        self.offers = RateLimitPolicy("offers", 30, HOUR)
        self.video_generation = RateLimitPolicy("video_generation", 20, HOUR)
        self.payments = RateLimitPolicy("payments", 20, HOUR)
        self.exports = RateLimitPolicy("exports", 10, HOUR)
        self.webhook_tests = RateLimitPolicy("webhook_tests", 10, HOUR)
        self.key_rotations = RateLimitPolicy("key_rotations", 10, DAY)
