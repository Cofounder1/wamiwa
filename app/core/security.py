"""Cryptographic helpers: passwords, tokens, API keys, OTP/TOTP and webhook signatures."""
import base64
import hashlib
import hmac
import secrets
import struct
from typing import Any

import jwt

from app.core.config import Settings
from app.core.exceptions import Unauthorized
from app.core.utils import Clock


class PasswordHasher:
    """scrypt (stdlib) with a per-password salt. Format: scrypt$n$r$p$salt$hash."""

    N, R, P, KEY_LEN = 2**14, 8, 1, 32

    def hash(self, password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=self.N, r=self.R, p=self.P, dklen=self.KEY_LEN)
        return f"scrypt${self.N}${self.R}${self.P}${salt.hex()}${digest.hex()}"

    def verify(self, password: str, stored: str | None) -> bool:
        if not stored:
            # Spend the same time as a real check so a missing account cannot be told apart by timing.
            hashlib.scrypt(password.encode(), salt=b"wamiwa-dummy-salt", n=self.N, r=self.R, p=self.P, dklen=self.KEY_LEN)
            return False
        try:
            _, n, r, p, salt, expected = stored.split("$")
            digest = hashlib.scrypt(
                password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected) // 2
            )
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(digest.hex(), expected)


class Hasher:
    """Keyed hashing for secrets we only need to look up or compare (API keys, OTPs, phone numbers)."""

    def __init__(self, pepper: str):
        self._pepper = pepper.encode()

    def digest(self, value: str) -> str:
        return hmac.new(self._pepper, value.encode(), hashlib.sha256).hexdigest()

    def matches(self, value: str, digest: str | None) -> bool:
        return bool(digest) and hmac.compare_digest(self.digest(value), digest)


class SecretFactory:
    AGENT_KEY_PREFIX = "wma_"
    COMPANY_KEY_PREFIX = "wmk_"

    @staticmethod
    def otp(length: int = 6) -> str:
        return "".join(str(secrets.randbelow(10)) for _ in range(length))

    @staticmethod
    def token(nbytes: int = 32) -> str:
        return secrets.token_urlsafe(nbytes)

    @classmethod
    def agent_key(cls) -> str:
        return cls.AGENT_KEY_PREFIX + secrets.token_urlsafe(32)

    @classmethod
    def company_key(cls) -> str:
        return cls.COMPANY_KEY_PREFIX + secrets.token_urlsafe(32)

    @staticmethod
    def webhook_secret() -> str:
        return "whsec_" + secrets.token_urlsafe(32)


class TokenService:
    """Signed, purpose-bound JWTs. A token minted for one purpose is rejected for any other."""

    ALGORITHM = "HS256"

    def __init__(self, settings: Settings, clock: Clock):
        self._settings = settings
        self._clock = clock

    def issue(self, purpose: str, subject: str, ttl_seconds: int, **claims: Any) -> str:
        now = int(self._clock.timestamp())
        payload = {"sub": subject, "purpose": purpose, "iat": now, "exp": now + ttl_seconds,
                   "jti": secrets.token_hex(8), **claims}
        return jwt.encode(payload, self._settings.jwt_secret, algorithm=self.ALGORITHM)

    def decode(self, token: str, purpose: str) -> dict[str, Any]:
        try:
            # Expiry is checked against the injected clock, not the wall clock.
            payload = jwt.decode(token, self._settings.jwt_secret, algorithms=[self.ALGORITHM],
                                 options={"verify_exp": False, "verify_iat": False})
        except jwt.PyJWTError as exc:
            raise Unauthorized("Invalid token", code="invalid_token") from exc
        if payload.get("purpose") != purpose:
            raise Unauthorized("Invalid token", code="invalid_token")
        if payload.get("exp", 0) <= self._clock.timestamp():
            raise Unauthorized("Token expired", code="token_expired", hint="Refresh your session and retry")
        return payload


class Totp:
    """RFC 6238 time-based one-time passwords (SHA-1, 30 second step, 6 digits)."""

    STEP, DIGITS = 30, 6

    def __init__(self, clock: Clock):
        self._clock = clock

    @staticmethod
    def generate_secret() -> str:
        return base64.b32encode(secrets.token_bytes(20)).decode()

    def code_at(self, secret: str, counter: int) -> str:
        key = base64.b32decode(secret)
        mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = mac[-1] & 0x0F
        number = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
        return str(number % 10**self.DIGITS).zfill(self.DIGITS)

    def now(self, secret: str) -> str:
        return self.code_at(secret, int(self._clock.timestamp()) // self.STEP)

    def verify(self, secret: str | None, code: str | None) -> bool:
        if not secret or not code:
            return False
        counter = int(self._clock.timestamp()) // self.STEP
        return any(hmac.compare_digest(self.code_at(secret, counter + drift), code) for drift in (-1, 0, 1))

    def provisioning_uri(self, secret: str, account: str, issuer: str = "Wamiwa") -> str:
        return f"otpauth://totp/{issuer}:{account}?secret={secret}&issuer={issuer}"


class WebhookSigner:
    """HMAC-SHA256 over "<timestamp>.<body>", sent as `X-Wamiwa-Signature: t=<ts>,v1=<hex>` (AI-02)."""

    HEADER = "X-Wamiwa-Signature"
    TOLERANCE_SECONDS = 300

    @staticmethod
    def sign(secret: str, timestamp: int, body: bytes) -> str:
        mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
        return f"t={timestamp},v1={mac}"

    @classmethod
    def verify(cls, secret: str, header: str | None, body: bytes, now: float) -> bool:
        try:
            parts = dict(item.split("=", 1) for item in (header or "").split(","))
            timestamp = int(parts["t"])
        except (ValueError, KeyError):
            return False
        if abs(now - timestamp) > cls.TOLERANCE_SECONDS:
            return False
        return hmac.compare_digest(cls.sign(secret, timestamp, body), header or "")
