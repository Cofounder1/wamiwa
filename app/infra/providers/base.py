"""Ports for every third-party dependency in PRD section 19.

Services depend only on these interfaces. Swapping a sandbox for YouVerify, Flutterwave, PayPal, HeyGen or
a chosen SMS/email/moderation vendor means adding one adapter class and wiring it in the container.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


# --- identity (YouVerify) ---------------------------------------------------------------
@dataclass(frozen=True)
class IdentityResult:
    passed: bool
    liveness_passed: bool
    verified_name: str | None = None
    date_of_birth: date | None = None
    face_ref: str | None = None
    failure_reason: str | None = None
    # The automatic checks passed but a person must confirm the name and date of birth from the document
    # (passports and IDs from outside Nigeria). `review_note` says what the checks found.
    needs_review: bool = False
    review_note: str | None = None


class IdentityGateway(ABC):
    @abstractmethod
    async def verify(self, document_type: str, document_ref: str | None, selfie_ref: str | None,
                     liveness_ref: str | None = None, *, id_number: str | None = None,
                     liveness_session: str | None = None, in_home_country: bool = True) -> IdentityResult:
        """`selfie_ref` is a frame from Wamiwa's own live camera check and `liveness_ref` its clip; or
        `liveness_session` is the provider's own live check. `id_number` is looked up in the government
        database where the provider can (Nigerian NIN and driver's licence); otherwise `document_ref` is a photo
        of the document."""

    async def start_liveness(self, account_id: str) -> dict | None:
        """What the browser needs to run the provider's own live check, or None to use Wamiwa's camera check."""
        return None

    @abstractmethod
    async def face_matches(self, face_ref: str, photo_ref: str) -> bool: ...

    @abstractmethod
    async def is_protected_identity(self, name: str) -> bool:
        """True for celebrity names that only the person themselves may use (LK-01)."""


# --- payments (Flutterwave, PayPal) -----------------------------------------------------------------
@dataclass(frozen=True)
class ChargeResult:
    status: str  # succeeded | pending_risk | failed
    provider_ref: str | None = None
    failure_reason: str | None = None
    reusable_method: str | None = None  # a saved card to charge for renewals, when the provider offers one


@dataclass(frozen=True)
class TransferResult:
    succeeded: bool
    provider_ref: str | None = None
    failure_reason: str | None = None


class PaymentGateway(ABC):
    @abstractmethod
    async def charge(self, *, reference: str, amount: int, currency: str, payment_method: str,
                     customer_id: str, usd_cents: int | None = None) -> ChargeResult:
        """`amount` is in the local currency's minor units; `usd_cents` is the same price in US cents, for
        providers that charge in dollars (PayPal)."""

    @abstractmethod
    async def refund(self, *, provider_ref: str, amount: int, reference: str) -> TransferResult: ...

    @abstractmethod
    async def transfer(self, *, reference: str, amount: int, currency: str, bank_code: str,
                       account_number: str, account_name: str, details: dict | None = None) -> TransferResult:
        """`details`: what a foreign bank needs (SWIFT or routing number, beneficiary address)."""

    @abstractmethod
    async def resolve_account_name(self, bank_code: str, account_number: str,
                                   claimed_name: str | None = None) -> str | None:
        """Name on the bank account, or None if it cannot be resolved. `claimed_name` is a sandbox aid."""

    async def transfer_status(self, provider_ref: str) -> tuple[str, str | None]:
        """Whether a transfer the provider accepted reached the bank: ("confirmed" | "failed" | "sent", reason).
        Providers that only accept final transfers report "confirmed"."""
        return "confirmed", None

    @abstractmethod
    async def held_balance(self, currency: str) -> int:
        """Funds the provider holds for Wamiwa, used by the daily reconciliation."""


def reference_prefix(customer_id: str) -> str:
    """Every checkout Wamiwa starts carries the payer's account id, so nobody can pay with someone else's payment."""
    return f"wmw-{customer_id}-"


class PayPalCheckout(PaymentGateway):
    """PayPal adds a step before `charge`: Wamiwa creates the order the payer approves in PayPal's window."""

    @abstractmethod
    async def create_order(self, *, amount_cents: int, description: str, reference: str) -> str:
        """An order for exactly `amount_cents` US cents. Returns its id."""


class FxRateProvider(ABC):
    @abstractmethod
    async def rate(self, base: str, quote: str) -> Decimal:
        """Units of `quote` per one unit of `base`."""


# --- AI video (HeyGen) and watermarking ---------------------------------------------------
@dataclass(frozen=True)
class VideoRequest:
    reference: str
    script: str
    face_ref: str
    voice_ref: str | None
    quality: str  # draft | final | standard | premium
    seconds: int
    lip_sync: bool = True  # lip movement must follow the words of the script
    expressive: bool = True  # facial expression must follow the meaning of the script
    voice: str = "female"  # female | male: which stock voice reads the script when there's no voice of their own


@dataclass(frozen=True)
class VideoJob:
    status: str  # processing | ready | failed
    file_ref: str | None = None
    failure_reason: str | None = None
    in_sync: bool | None = None  # provider's own check of lips/expression against the script; None = not reported


class VideoGateway(ABC):
    def ensure_available(self) -> None:
        """Called before any money is taken for a video; a provider that can't make videos refuses here."""

    @abstractmethod
    async def submit(self, request: VideoRequest) -> str:
        """Start a generation job and return the provider's job id."""

    @abstractmethod
    async def fetch(self, job_id: str) -> VideoJob: ...


@dataclass(frozen=True)
class WatermarkResult:
    watermark_id: str
    file_ref: str


class WatermarkGateway(ABC):
    @abstractmethod
    async def embed(self, file_ref: str, payload: str) -> WatermarkResult: ...


# --- communications ---------------------------------------------------------------------
class EmailGateway(ABC):
    @abstractmethod
    async def send(self, to: str, subject: str, body: str) -> None: ...


class SmsGateway(ABC):
    @abstractmethod
    async def send(self, to: str, body: str) -> None: ...


class VoiceGateway(ABC):
    @abstractmethod
    async def place_confirmation_call(self, *, to: str, reference: str, script: str) -> str:
        """Start an automated, recorded call. The outcome arrives on the voice webhook."""


# --- moderation and sentiment -------------------------------------------------------------
@dataclass(frozen=True)
class ModerationResult:
    flagged: bool
    vulgar: bool = False
    categories: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class SentimentResult:
    label: str  # positive | negative | neutral | uncertain
    confidence: float
    language: str


class ModerationGateway(ABC):
    @abstractmethod
    async def moderate_text(self, text: str) -> ModerationResult: ...

    @abstractmethod
    async def screen_script(self, text: str) -> ModerationResult:
        """LK-04: illegal, adult, political, defamatory or misleading content."""

    @abstractmethod
    async def detect_ai_media(self, media_ref: str) -> bool: ...

    @abstractmethod
    async def sentiment(self, text: str) -> SentimentResult: ...


# --- external social accounts -------------------------------------------------------------
@dataclass(frozen=True)
class SocialProfile:
    handle: str
    followers: int
    fake_follower_ratio: float = 0.0
    friend_ids: tuple[str, ...] = field(default_factory=tuple)
    external_id: str | None = None


class SocialGateway(ABC):
    @abstractmethod
    async def connect(self, platform: str, auth_code: str) -> SocialProfile:
        """Exchange a login/OAuth code for the account's profile. Screenshots are never accepted."""

    @abstractmethod
    async def refresh(self, platform: str, handle: str) -> SocialProfile | None:
        """Re-read a connected account; None when the connection has dropped."""


# --- trend signals ------------------------------------------------------------------------------
@dataclass(frozen=True)
class TrendSignal:
    topic: str
    score: float  # 0-1, how strongly it is rising
    source: str


class TrendGateway(ABC):
    @abstractmethod
    async def rising(self, region: str, category: str | None) -> list[TrendSignal]:
        """Topics rising in a region, from an outside trends source. Empty when nothing is known."""


# --- music catalogues and link safety -------------------------------------------------------------
class MusicCatalogGateway(ABC):
    @abstractmethod
    async def find_artist(self, platform: str, name: str) -> str | None:
        """The artist's public profile address on `platform` (spotify, youtube_music, apple_music), or None."""


@dataclass(frozen=True)
class LinkVerdict:
    suspicious: bool
    reasons: tuple[str, ...] = ()


class LinkSafetyGateway(ABC):
    @abstractmethod
    async def check(self, url: str) -> LinkVerdict:
        """Whether a link looks like a scam, phishing or malware page."""


# --- agent webhooks -----------------------------------------------------------------------
@dataclass(frozen=True)
class WebhookResponse:
    status_code: int
    body: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class WebhookTransport(ABC):
    @abstractmethod
    async def post(self, url: str, body: bytes, headers: dict[str, str]) -> WebhookResponse: ...
