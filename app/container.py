"""Composition root: builds the process-wide infrastructure and wires provider adapters."""
from app.core.config import Settings
from app.core.security import Hasher, PasswordHasher, TokenService, Totp
from app.core.utils import Clock, set_default_clock
from app.infra.cache import CacheService
from app.infra.database import Database
from app.infra.kv import InMemoryKeyValueStore, KeyValueStore, RedisKeyValueStore, ResilientKeyValueStore
from app.infra.providers.base import EmailGateway
from app.infra.providers.resend import ResendEmailGateway
from app.infra.providers.africastalking import AfricasTalkingSmsGateway, AfricasTalkingVoiceGateway
from app.infra.providers.base import SmsGateway, VoiceGateway
from app.infra.providers.flutterwave import FlutterwaveGateway
from app.infra.providers.payment_router import PaymentRouter
from app.infra.providers.paypal import PayPalGateway
from app.infra.providers.youverify import YouVerifyIdentityGateway
from app.infra.storage import LocalFileStorage
from app.infra.providers.unavailable import IdentityNotConnected, PaymentsNotConnected, PayPalNotConnected
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


def build_sms_and_voice(settings: Settings) -> tuple[SmsGateway, VoiceGateway]:
    """Africa's Talking once its username and key are set; calls also need its voice number and callback token."""
    at = settings.africastalking_username and settings.africastalking_api_key
    sms = (AfricasTalkingSmsGateway(settings.africastalking_username, settings.africastalking_api_key,
                                    settings.africastalking_sender_id) if at else SandboxSmsGateway())
    calls = at and settings.africastalking_voice_number and settings.africastalking_callback_token
    voice = (AfricasTalkingVoiceGateway(settings.africastalking_username, settings.africastalking_api_key,
                                        settings.africastalking_voice_number) if calls else SandboxVoiceGateway())
    return sms, voice


def build_providers(settings: Settings) -> Providers:
    """Sandbox adapters until the vendors in PRD section 19 are chosen and credentialed.

    To go live, implement the matching port in app/infra/providers/base.py (as FlutterwaveGateway does)
    and return it here based on settings.
    """
    # On the live site a stand-in would approve every payment and ID check, so they refuse until connected.
    stand_ins_allowed = not settings.is_production or settings.allow_stand_in_providers
    if settings.flutterwave_secret_key:
        payments = FlutterwaveGateway(settings.flutterwave_secret_key)
    else:
        payments = SandboxPaymentGateway() if stand_ins_allowed else PaymentsNotConnected()
    # PY-05: PayPal is a second way to pay, off until its credentials are set. It has no stand-in.
    if settings.paypal_client_id and settings.paypal_secret:
        paypal = PayPalGateway(settings.paypal_client_id, settings.paypal_secret, settings.paypal_environment)
    else:
        paypal = PayPalNotConnected()
    sms, voice = build_sms_and_voice(settings)
    if settings.youverify_api_key and settings.youverify_public_merchant_key:
        identity = YouVerifyIdentityGateway(
            settings.youverify_api_key, settings.youverify_public_merchant_key, settings.youverify_environment,
            storage=LocalFileStorage(settings.upload_dir), protected_names=set(settings.protected_identity_names))
    else:
        identity = SandboxIdentityGateway() if stand_ins_allowed else IdentityNotConnected()
    return Providers(
        identity=identity,
        payments=PaymentRouter(payments, paypal),
        # The day's rate comes from Flutterwave once it is connected, so conversions match the money it moves.
        fx=payments if isinstance(payments, FlutterwaveGateway)
        else StaticFxRateProvider({("USD", settings.local_currency): settings.sandbox_fx_rate}),
        video=SandboxVideoGateway(),
        watermark=SandboxWatermarkGateway(),
        email=build_email(settings),
        sms=sms,
        voice=voice,
        moderation=SandboxModerationGateway(),
        social=SandboxSocialGateway(),
        webhooks=HttpxWebhookTransport(settings.webhook_timeout_seconds),
        trends=SandboxTrendGateway(),
        music=SandboxMusicCatalogGateway(),
        link_safety=SandboxLinkSafetyGateway(),
        paypal=paypal,
    )


def build_infrastructure(settings: Settings, *, clock: Clock | None = None, kv: KeyValueStore | None = None,
                         providers: Providers | None = None) -> Infrastructure:
    clock = clock or Clock()
    set_default_clock(clock)
    if kv is None:
        kv = RedisKeyValueStore(settings.redis_url) if settings.redis_url else InMemoryKeyValueStore(clock)
    if not isinstance(kv, ResilientKeyValueStore):
        kv = ResilientKeyValueStore(kv)
    return Infrastructure(
        settings=settings,
        clock=clock,
        db=Database(settings.database_url, echo=False, pool_size=settings.database_pool_size,
                    max_overflow=settings.database_max_overflow, schema=settings.database_schema,
                    query_timeout_seconds=settings.database_query_timeout_seconds),
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
