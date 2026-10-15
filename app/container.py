"""Composition root: builds the process-wide infrastructure and wires provider adapters."""
from app.core.config import Settings
from app.core.security import Hasher, PasswordHasher, TokenService, Totp
from app.core.utils import Clock, set_default_clock
from app.infra.cache import CacheService
from app.infra.database import Database
from app.infra.kv import InMemoryKeyValueStore, KeyValueStore, RedisKeyValueStore
from app.infra.providers.base import EmailGateway
from app.infra.providers.resend import ResendEmailGateway
from app.infra.providers.sandbox import (
    HttpxWebhookTransport, SandboxEmailGateway, SandboxLinkSafetyGateway, SandboxMusicCatalogGateway, SandboxTrendGateway, SandboxIdentityGateway, SandboxModerationGateway,
    SandboxPaymentGateway, SandboxSmsGateway, SandboxSocialGateway, SandboxVideoGateway,
    SandboxVoiceGateway, SandboxWatermarkGateway, StaticFxRateProvider,
)
from app.infra.rate_limit import RateLimiter, RateLimitPolicies
from app.services.base import Infrastructure, Providers


def build_email(settings: Settings) -> EmailGateway:
    """Real email only when it is asked for by name and a key is present; otherwise nothing leaves the machine."""
    if settings.email_provider == "resend":
        if not settings.email_api_key:
            raise RuntimeError("WAMIWA_EMAIL_PROVIDER=resend needs WAMIWA_EMAIL_API_KEY")
        return ResendEmailGateway(settings.email_api_key, settings.email_from_address, settings.email_from_name,
                                  reply_to=settings.email_reply_to)
    return SandboxEmailGateway()


def build_providers(settings: Settings) -> Providers:
    """Sandbox adapters until the vendors in PRD section 19 are chosen and credentialed.

    To go live, implement the matching port in app/infra/providers/base.py (e.g. a
    KorapayPaymentGateway) and return it here based on settings.
    """
    return Providers(
        identity=SandboxIdentityGateway(),
        payments=SandboxPaymentGateway(),
        fx=StaticFxRateProvider({("USD", settings.local_currency): settings.sandbox_fx_rate}),
        video=SandboxVideoGateway(),
        watermark=SandboxWatermarkGateway(),
        email=build_email(settings),
        sms=SandboxSmsGateway(),
        voice=SandboxVoiceGateway(),
        moderation=SandboxModerationGateway(),
        social=SandboxSocialGateway(),
        webhooks=HttpxWebhookTransport(settings.webhook_timeout_seconds),
        trends=SandboxTrendGateway(),
        music=SandboxMusicCatalogGateway(),
        link_safety=SandboxLinkSafetyGateway(),
    )


def build_infrastructure(settings: Settings, *, clock: Clock | None = None, kv: KeyValueStore | None = None,
                         providers: Providers | None = None) -> Infrastructure:
    clock = clock or Clock()
    set_default_clock(clock)
    if kv is None:
        kv = RedisKeyValueStore(settings.redis_url) if settings.redis_url else InMemoryKeyValueStore(clock)
    return Infrastructure(
        settings=settings,
        clock=clock,
        db=Database(settings.database_url, echo=False, pool_size=settings.database_pool_size,
                    max_overflow=settings.database_max_overflow, schema=settings.database_schema),
        kv=kv,
        cache=CacheService(kv),
        limiter=RateLimiter(kv, clock),
        policies=RateLimitPolicies(settings),
        hasher=Hasher(settings.hash_pepper),
        passwords=PasswordHasher(),
        tokens=TokenService(settings, clock),
        totp=Totp(clock),
        providers=providers or build_providers(settings),
    )
