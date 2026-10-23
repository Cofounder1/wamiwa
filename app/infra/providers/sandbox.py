"""Deterministic sandbox adapters used in development and tests.

Behaviour is driven by markers in the inputs (documented per class) so every PRD branch can be
exercised without a live vendor account.
"""
import hashlib
import logging
import re
from datetime import date
from decimal import Decimal

import httpx

from app.core.utils import new_id
from app.infra.providers.base import (
    ChargeResult, EmailGateway, LinkSafetyGateway, LinkVerdict, MusicCatalogGateway, TrendGateway, TrendSignal, FxRateProvider, IdentityGateway, IdentityResult, ModerationGateway,
    ModerationResult, PaymentGateway, SentimentResult, SmsGateway, SocialGateway, SocialProfile,
    TransferResult, VideoGateway, VideoJob, VideoRequest, VoiceGateway, WatermarkGateway,
    WatermarkResult, WebhookResponse, WebhookTransport,
)

log = logging.getLogger("wamiwa.sandbox")


class SandboxIdentityGateway(IdentityGateway):
    """`document_ref` format: "<name>|<YYYY-MM-DD>[|fail|blurry|noliveness]".

    e.g. "Ada Obi|1995-04-02" passes; "Ada Obi|1995-04-02|blurry" fails with a reason.
    A photo ref containing "nomatch" fails the face match.
    """

    PROTECTED_NAMES = {"burna boy", "davido", "wizkid", "tiwa savage", "beyonce"}

    async def verify(self, document_type: str, document_ref: str, selfie_ref: str,
                     liveness_ref: str | None = None) -> IdentityResult:
        parts = [p.strip() for p in document_ref.split("|")]
        flags = {p.lower() for p in parts[2:]}
        if len(parts) < 2:
            return IdentityResult(False, False, failure_reason="Document could not be read")
        try:
            dob = date.fromisoformat(parts[1])
        except ValueError:
            return IdentityResult(False, False, failure_reason="Date of birth on the document is unreadable")
        if "blurry" in flags:
            return IdentityResult(False, True, failure_reason="Document image is too blurry")
        if "noliveness" in flags or "spoof" in selfie_ref.lower():
            return IdentityResult(False, False, failure_reason="Liveness check failed")
        if "fail" in flags:
            return IdentityResult(False, True, failure_reason="Document did not pass authenticity checks")
        return IdentityResult(True, True, verified_name=parts[0], date_of_birth=dob, face_ref=f"face:{selfie_ref}")

    async def face_matches(self, face_ref: str, photo_ref: str) -> bool:
        return "nomatch" not in photo_ref.lower()

    async def is_protected_identity(self, name: str) -> bool:
        return name.strip().lower() in self.PROTECTED_NAMES


class SandboxPaymentGateway(PaymentGateway):
    """Payment method tokens: "tok_fail" declines, "tok_risk" stays pending the risk check.

    Keeps a running balance so the reconciliation job has something real to compare against.
    """

    def __init__(self) -> None:
        self.balance: dict[str, int] = {}
        self.charges: dict[str, tuple[int, str]] = {}
        self.bank_accounts: dict[str, str] = {}
        self.transfers: list[dict] = []
        self.refunds: list[dict] = []
        self._done: dict[str, TransferResult] = {}
        self.fail_next_refunds = 0  # tests: make the next N refunds fail

    async def charge(self, *, reference, amount, currency, payment_method, customer_id, usd_cents=None) -> ChargeResult:
        if payment_method == "tok_fail":
            return ChargeResult("failed", failure_reason="Card declined")
        provider_ref = f"ch_{new_id()}"
        self.charges[provider_ref] = (amount, currency)
        self.balance[currency] = self.balance.get(currency, 0) + amount
        return ChargeResult("pending_risk" if payment_method == "tok_risk" else "succeeded", provider_ref)

    async def refund(self, *, provider_ref, amount, reference) -> TransferResult:
        if reference in self._done:
            return self._done[reference]  # same reference: already done, never paid twice
        if provider_ref not in self.charges:
            return TransferResult(False, failure_reason="Unknown charge")
        if self.fail_next_refunds > 0:
            self.fail_next_refunds -= 1
            return TransferResult(False, failure_reason="Provider temporarily unavailable")
        currency = self.charges[provider_ref][1]
        self.balance[currency] = self.balance.get(currency, 0) - amount
        self.refunds.append({"provider_ref": provider_ref, "amount": amount, "reference": reference})
        self._done[reference] = TransferResult(True, f"rf_{new_id()}")
        return self._done[reference]

    async def transfer(self, *, reference, amount, currency, bank_code, account_number, account_name, details=None) -> TransferResult:
        if reference in self._done:
            return self._done[reference]
        if account_number.endswith("0000"):
            return TransferResult(False, failure_reason="Beneficiary bank rejected the transfer")
        self.balance[currency] = self.balance.get(currency, 0) - amount
        self.transfers.append({"reference": reference, "amount": amount, "account_number": account_number})
        self._done[reference] = TransferResult(True, f"tr_{new_id()}")
        return self._done[reference]

    async def resolve_account_name(self, bank_code, account_number, claimed_name=None) -> str | None:
        if account_number.startswith("000"):
            return None
        return self.bank_accounts.get(account_number, claimed_name)

    async def held_balance(self, currency: str) -> int:
        return self.balance.get(currency, 0)


class StaticFxRateProvider(FxRateProvider):
    def __init__(self, rates: dict[tuple[str, str], Decimal]):
        self._rates = rates

    async def rate(self, base: str, quote: str) -> Decimal:
        if base == quote:
            return Decimal(1)
        return self._rates[(base, quote)]


class SandboxVideoGateway(VideoGateway):
    """Jobs finish on the first status fetch. A script containing "[fail]" fails; "[slow]" stays
    processing until `complete(job_id)` is called; "[desync]" renders out of sync once, and
    "[desync-always]" every time."""

    def __init__(self) -> None:
        self.jobs: dict[str, VideoJob] = {}
        self.requests: dict[str, VideoRequest] = {}
        self._desynced: set[str] = set()

    async def submit(self, request: VideoRequest) -> str:
        job_id = f"vid_{new_id()}"
        self.requests[job_id] = request
        self.jobs[job_id] = VideoJob("processing")
        return job_id

    def complete(self, job_id: str) -> None:
        request = self.requests[job_id]
        out_of_sync = "[desync-always]" in request.script or (
            "[desync]" in request.script and request.reference not in self._desynced)
        self._desynced.add(request.reference)
        self.jobs[job_id] = VideoJob("ready", file_ref=f"videos/{job_id}.mp4", in_sync=not out_of_sync)

    async def fetch(self, job_id: str) -> VideoJob:
        request = self.requests.get(job_id)
        if request is None:
            return VideoJob("failed", failure_reason="Unknown job")
        if self.jobs[job_id].status == "processing":
            if "[fail]" in request.script:
                self.jobs[job_id] = VideoJob("failed", failure_reason="Rendering failed")
            elif "[slow]" not in request.script:
                self.complete(job_id)
        return self.jobs[job_id]


class SandboxWatermarkGateway(WatermarkGateway):
    async def embed(self, file_ref: str, payload: str) -> WatermarkResult:
        watermark_id = "wm_" + hashlib.sha256(f"{file_ref}:{payload}".encode()).hexdigest()[:24]
        return WatermarkResult(watermark_id=watermark_id, file_ref=f"{file_ref}?wm={watermark_id}")


class SandboxEmailGateway(EmailGateway):
    def __init__(self) -> None:
        self.outbox: list[dict] = []

    async def send(self, to: str, subject: str, body: str) -> None:
        self.outbox.append({"to": to, "subject": subject, "body": body})
        log.info("email to=%s subject=%s", to, subject)

    def last_for(self, to: str) -> dict | None:
        return next((m for m in reversed(self.outbox) if m["to"] == to), None)


class SandboxSmsGateway(SmsGateway):
    def __init__(self) -> None:
        self.outbox: list[dict] = []

    async def send(self, to: str, body: str) -> None:
        self.outbox.append({"to": to, "body": body})
        log.info("sms to=%s", to)

    def last_for(self, to: str) -> dict | None:
        return next((m for m in reversed(self.outbox) if m["to"] == to), None)


class SandboxVoiceGateway(VoiceGateway):
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def place_confirmation_call(self, *, to: str, reference: str, script: str) -> str:
        call_id = f"call_{new_id()}"
        self.calls.append({"call_id": call_id, "to": to, "reference": reference, "script": script})
        log.info("voice call to=%s reference=%s", to, reference)
        return call_id


class SandboxModerationGateway(ModerationGateway):
    """Keyword lexicons standing in for a real moderation/sentiment vendor."""

    ABUSE = {"kill yourself", "i will hurt you", "scamlink"}
    VULGAR = {"fuck", "shit", "bastard", "mumu", "ode", "oloshi", "wawa"}
    SCRIPT_RULES = {
        "illegal": {"cocaine", "counterfeit", "stolen"},
        "adult": {"xxx", "porn", "nude"},
        "political": {"vote for", "election", "candidate", "campaign rally"},
        "defamatory": {"is a thief", "is a fraud", "is a criminal"},
        "misleading": {"guaranteed returns", "cures cancer", "miracle cure", "double your money"},
    }
    LEXICON = {
        "en": ({"love", "great", "good", "amazing", "nice", "best", "excellent"},
               {"bad", "hate", "terrible", "worst", "awful", "poor", "scam"}),
        "pcm": ({"sweet", "correct", "gbam", "e choke", "dey burst", "na fire"},
                {"wack", "yeye", "nonsense", "e no make sense", "rubbish"}),
        "yo": ({"dara", "o dun", "mo feran", "e se"}, {"buru", "ko dara", "ko da"}),
        "ig": ({"mma", "o di mma", "daalu", "amaka"}, {"ojoo", "o di njo", "adighi mma"}),
        "ha": ({"kyau", "na gode", "madalla", "yana da kyau"}, {"mummuna", "ba kyau", "banza"}),
    }

    @staticmethod
    def _contains(text: str, terms: set[str]) -> list[str]:
        return [t for t in terms if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", text)]

    async def moderate_text(self, text: str) -> ModerationResult:
        lowered = text.lower()
        vulgar = bool(self._contains(lowered, self.VULGAR))
        abusive = self._contains(lowered, self.ABUSE)
        if abusive:
            return ModerationResult(True, vulgar, ("abuse",), "Contains abusive or unsafe content")
        return ModerationResult(False, vulgar)

    async def screen_script(self, text: str) -> ModerationResult:
        lowered = text.lower()
        hits = tuple(category for category, terms in self.SCRIPT_RULES.items() if self._contains(lowered, terms))
        return ModerationResult(bool(hits), categories=hits, reason=f"Flagged: {', '.join(hits)}" if hits else None)

    async def detect_ai_media(self, media_ref: str) -> bool:
        return "ai-" in media_ref.lower() or "generated" in media_ref.lower()

    async def sentiment(self, text: str) -> SentimentResult:
        lowered = text.lower()
        best = ("en", 0, 0)
        for language, (positive, negative) in self.LEXICON.items():
            pos, neg = len(self._contains(lowered, positive)), len(self._contains(lowered, negative))
            if pos + neg > best[1] + best[2]:
                best = (language, pos, neg)
        language, pos, neg = best
        total = pos + neg
        if total == 0:
            return SentimentResult("uncertain", 0.3, language)
        confidence = round(abs(pos - neg) / total * min(1.0, 0.6 + 0.2 * total), 2)
        if confidence < 0.5:
            return SentimentResult("uncertain", confidence, language)
        return SentimentResult("positive" if pos > neg else "negative", confidence, language)


class SandboxSocialGateway(SocialGateway):
    """`auth_code` format: "<handle>:<followers>[:fake][:friends=a,b,c]"."""

    def __init__(self) -> None:
        self.profiles: dict[tuple[str, str], SocialProfile | None] = {}

    async def connect(self, platform: str, auth_code: str) -> SocialProfile:
        parts = auth_code.split(":")
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError("Login was cancelled or the code is invalid")
        friends: tuple[str, ...] = ()
        for part in parts[2:]:
            if part.startswith("friends="):
                friends = tuple(f for f in part[8:].split(",") if f)
        profile = SocialProfile(
            handle=parts[0], followers=int(parts[1]),
            fake_follower_ratio=0.8 if "fake" in parts[2:] else 0.05,
            friend_ids=friends, external_id=f"{platform}:{parts[0]}",
        )
        self.profiles[(platform, parts[0])] = profile
        return profile

    async def refresh(self, platform: str, handle: str) -> SocialProfile | None:
        return self.profiles.get((platform, handle))


class SandboxTrendGateway(TrendGateway):
    """Signals are registered by hand: `gateway.signals["nigeria"] = [TrendSignal("amapiano", 0.9, "search")]`."""

    def __init__(self) -> None:
        self.signals: dict[str, list[TrendSignal]] = {}

    async def rising(self, region: str, category: str | None) -> list[TrendSignal]:
        return list(self.signals.get(region, []))


class SandboxMusicCatalogGateway(MusicCatalogGateway):
    """Profiles are registered by hand: `gateway.artists[("spotify", "oshamo")] = "https://open.spotify.com/artist/..."`."""

    def __init__(self) -> None:
        self.artists: dict[tuple[str, str], str] = {}

    async def find_artist(self, platform: str, name: str) -> str | None:
        return self.artists.get((platform, name.strip().lower()))


class SandboxLinkSafetyGateway(LinkSafetyGateway):
    """Rule-of-thumb checks standing in for a real reputation service. `blocklist` holds known-bad hosts."""

    SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "cutt.ly", "is.gd", "rb.gy", "shorturl.at"}
    RISKY_TLDS = (".tk", ".ml", ".ga", ".cf", ".gq", ".zip", ".mov")
    BAIT = ("free-money", "double-your", "giveaway-claim", "airdrop", "wallet-connect", "verify-account",
            "login-secure", "claim-prize", "investment-returns")
    LOOKALIKES = ("paypa1", "g00gle", "faceb00k", "amaz0n", "micros0ft", "app1e", "spot1fy", "binance-")

    def __init__(self) -> None:
        self.blocklist: set[str] = set()

    async def check(self, url: str) -> LinkVerdict:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host, whole = (parsed.hostname or "").lower(), url.lower()
        reasons = []
        if host in self.blocklist:
            reasons.append("on a list of known scam sites")
        if host in self.SHORTENERS:
            reasons.append("hides its destination behind a link shortener")
        if host.startswith("xn--") or ".xn--" in host:
            reasons.append("uses look-alike characters in its address")
        if host.endswith(self.RISKY_TLDS):
            reasons.append("uses a domain ending often used for scams")
        if "@" in parsed.netloc or host.count(".") > 3:
            reasons.append("has a misleading address")
        if any(word in whole for word in self.BAIT):
            reasons.append("uses wording typical of scams")
        if any(word in host for word in self.LOOKALIKES):
            reasons.append("imitates a well-known brand")
        return LinkVerdict(bool(reasons), tuple(reasons))


class HttpxWebhookTransport(WebhookTransport):
    def __init__(self, timeout: float):
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def post(self, url: str, body: bytes, headers: dict[str, str]) -> WebhookResponse:
        try:
            response = await self._client.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            return WebhookResponse(0, str(exc))
        return WebhookResponse(response.status_code, response.text[:2000])

    async def close(self) -> None:
        await self._client.aclose()
