"""Service plumbing: shared infrastructure, per-request context and the lazy service registry."""
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import Hasher, PasswordHasher, TokenService, Totp
from app.core.utils import Clock
from app.infra.cache import CacheService
from app.infra.database import Database, UnitOfWork
from app.infra.kv import KeyValueStore
from app.infra.providers.base import (
    EmailGateway, FxRateProvider, IdentityGateway, LinkSafetyGateway, ModerationGateway, MusicCatalogGateway, PaymentGateway, SmsGateway,
    SocialGateway, TrendGateway, VideoGateway, VoiceGateway, WatermarkGateway, WebhookTransport,
)
from app.infra.rate_limit import RateLimiter, RateLimitPolicies
from app.models import Account
from app.models.base import Model
from app.repositories.base import Repository

if TYPE_CHECKING:
    from app.services.registry import ServiceRegistry

M = TypeVar("M", bound=Model)


@dataclass
class Providers:
    identity: IdentityGateway
    payments: PaymentGateway
    fx: FxRateProvider
    video: VideoGateway
    watermark: WatermarkGateway
    email: EmailGateway
    sms: SmsGateway
    voice: VoiceGateway
    moderation: ModerationGateway
    social: SocialGateway
    webhooks: WebhookTransport
    trends: TrendGateway
    music: MusicCatalogGateway
    link_safety: LinkSafetyGateway


@dataclass
class Infrastructure:
    """Process-wide singletons, built once by the container."""

    settings: Settings
    clock: Clock
    db: Database
    kv: KeyValueStore
    cache: CacheService
    limiter: RateLimiter
    policies: RateLimitPolicies
    hasher: Hasher
    passwords: PasswordHasher
    tokens: TokenService
    totp: Totp
    providers: Providers


@dataclass(frozen=True)
class Principal:
    """Who is making the request and how they authenticated."""

    account: Account
    kind: str = "session"  # session | agent_key | company_key
    api_key_id: str | None = None

    @property
    def id(self) -> str:
        return self.account.id

    @property
    def is_machine(self) -> bool:
        return self.kind != "session"

    @property
    def rate_limit_identity(self) -> str:
        return self.api_key_id or self.account.id


class ServiceContext:
    """Everything a service needs for one unit of work."""

    def __init__(self, infra: Infrastructure, uow: UnitOfWork):
        self.infra = infra
        self.uow = uow
        self._repos: dict[type, Repository] = {}

    @property
    def session(self) -> AsyncSession:
        return self.uow.session

    def repo(self, model: type[M]) -> Repository[M]:
        if model not in self._repos:
            self._repos[model] = Repository(self.session, model)
        return self._repos[model]

    @cached_property
    def services(self) -> "ServiceRegistry":
        from app.services.registry import ServiceRegistry

        return ServiceRegistry(self)


class BaseService:
    def __init__(self, ctx: ServiceContext):
        self.ctx = ctx
        self.infra = ctx.infra
        self.s = ctx.infra.settings
        self.clock = ctx.infra.clock
        self.kv = ctx.infra.kv
        self.cache = ctx.infra.cache
        self.limiter = ctx.infra.limiter
        self.policies = ctx.infra.policies
        self.providers = ctx.infra.providers
        self.db = ctx.session
        self.uow = ctx.uow
        self.repo = ctx.repo

    @property
    def svc(self) -> "ServiceRegistry":
        return self.ctx.services

    def now(self):
        return self.clock.now()
