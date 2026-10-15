"""ON-01 email OTP signup, ON-02 age gate, login, sessions and two-factor authentication."""
from datetime import date
from typing import Any

from app.core.enums import AccountStatus, AccountType, NotificationType
from app.core.exceptions import Conflict, Forbidden, Gone, Locked, Unauthorized, Unprocessable
from app.core.security import SecretFactory
from app.core.utils import age_on, normalize_handle
from app.infra.rate_limit import HOUR, RateLimitPolicy
from app.models import Account, CompanyProfile, UserProfile
from app.services.base import BaseService


class OtpService(BaseService):
    """One-time codes kept in the key-value store: hashed, short-lived and attempt-limited."""

    def _keys(self, purpose: str, identifier: str) -> tuple[str, str, str]:
        base = f"otp:{purpose}:{identifier.lower()}"
        return f"{base}:code", f"{base}:wrong", f"{base}:lock"

    @property
    def _send_policy(self) -> RateLimitPolicy:
        # The first send plus `otp_resends_per_hour` resends.
        return RateLimitPolicy("otp_send", self.s.otp_resends_per_hour + 1, HOUR)

    async def issue(self, purpose: str, identifier: str) -> str:
        code_key, wrong_key, lock_key = self._keys(purpose, identifier)
        lock_ttl = await self.kv.ttl(lock_key)
        if lock_ttl:
            raise Locked("Too many wrong codes. Try again later.", code="otp_locked", retry_after_seconds=lock_ttl)
        await self.limiter.enforce(
            self._send_policy, f"{purpose}:{identifier.lower()}",
            "You have requested too many codes. Try again when the hour has passed.")
        code = SecretFactory.otp(self.s.otp_length)
        await self.kv.set(code_key, self.infra.hasher.digest(code), self.s.otp_ttl_seconds)
        await self.kv.delete(wrong_key)
        return code

    async def verify(self, purpose: str, identifier: str, code: str) -> None:
        code_key, wrong_key, lock_key = self._keys(purpose, identifier)
        lock_ttl = await self.kv.ttl(lock_key)
        if lock_ttl:
            raise Locked("Too many wrong codes. Try again later.", code="otp_locked", retry_after_seconds=lock_ttl)
        stored = await self.kv.get(code_key)
        if stored is None:
            raise Gone("This code has expired. Request a new one.", code="otp_expired")
        if not self.infra.hasher.matches(code, stored):
            wrong = await self.kv.incr(wrong_key, ttl=self.s.otp_ttl_seconds)
            if wrong >= self.s.otp_max_wrong_attempts:
                await self.kv.set(lock_key, "1", self.s.otp_lock_seconds)
                await self.kv.delete(code_key, wrong_key)
                raise Locked("Too many wrong codes. This email is locked for 15 minutes.", code="otp_locked",
                             retry_after_seconds=self.s.otp_lock_seconds)
            raise Unprocessable("Incorrect code", code="otp_incorrect",
                                attempts_left=self.s.otp_max_wrong_attempts - wrong)
        await self.kv.delete(code_key, wrong_key)


class AuthService(BaseService):
    SIGNUP, RESET, ACCESS, REFRESH = "signup", "password_reset", "access", "refresh"

    # --- signup (ON-01, ON-02) ---------------------------------------------------------
    async def request_signup_otp(self, email: str, ip: str) -> dict[str, Any]:
        email = email.lower()
        await self.limiter.enforce(self.policies.otp_ip, ip)
        if await self.repo(Account).exists(Account.email == email):
            raise Conflict("This email is already registered. Log in instead.", code="email_registered",
                           login_url=f"{self.s.public_base_url}/login",
                           password_reset_url=f"{self.s.public_base_url}/reset-password")
        code = await self.svc.otp.issue(self.SIGNUP, email)
        self.svc.notifications.email(email, "Your Wamiwa code",
                                     f"Your code is {code}. It is valid for {self.s.otp_ttl_seconds // 60} minutes.")
        return {
            "expires_in_seconds": self.s.otp_ttl_seconds,
            "resend_available_after_seconds": self.s.otp_resend_hint_after_seconds,
            "hint": "No email after 60 seconds? Check your spam folder or resend the code.",
        }

    async def verify_signup_otp(self, email: str, code: str) -> dict[str, Any]:
        email = email.lower()
        await self.svc.otp.verify(self.SIGNUP, email, code)
        token = self.infra.tokens.issue(self.SIGNUP, email, self.s.signup_token_ttl_seconds)
        return {"signup_token": token, "expires_in_seconds": self.s.signup_token_ttl_seconds}

    async def complete_signup(self, *, signup_token: str, account_type: AccountType, password: str, handle: str,
                              name: str, date_of_birth: date | None, category: str | None,
                              region: str | None) -> dict[str, Any]:
        email = self.infra.tokens.decode(signup_token, self.SIGNUP)["sub"]
        if account_type not in (AccountType.USER, AccountType.COMPANY):
            raise Unprocessable("Sign up as a user or a company", code="invalid_account_type")
        if await self.repo(Account).exists(Account.email == email):
            raise Conflict("This email is already registered. Log in instead.", code="email_registered")
        handle = normalize_handle(handle)
        if await self.repo(Account).exists(Account.handle == handle):
            raise Conflict("That handle is taken", code="handle_taken")

        status, is_minor = AccountStatus.ACTIVE, False
        if account_type == AccountType.USER:
            if date_of_birth is None:
                raise Unprocessable("Date of birth is required", code="dob_required")
            if await self.kv.get(f"agegate:{email}"):
                raise Forbidden("You cannot sign up for Wamiwa yet", code="under_age")
            age = age_on(date_of_birth, self.clock.today())
            if age < self.s.min_age:
                # Remember the refusal so the form cannot simply be resubmitted with another date.
                await self.kv.set(f"agegate:{email}", "1", 24 * 3600)
                raise Forbidden("You must be at least 13 to use Wamiwa", code="under_age")
            if age < self.s.adult_age:
                status, is_minor = AccountStatus.PENDING_GUARDIAN, True

        account = await self.repo(Account).add(Account(
            type=account_type, email=email, handle=handle, display_name=name.strip(),
            password_hash=self.infra.passwords.hash(password), status=status, region=region,
            last_active_at=self.now(),
        ))
        if account_type == AccountType.USER:
            await self.repo(UserProfile).add(UserProfile(
                account_id=account.id, name=name.strip(), date_of_birth=date_of_birth, is_minor=is_minor))
        else:
            await self.repo(CompanyProfile).add(CompanyProfile(
                account_id=account.id, business_name=name.strip(), category=category))

        result = await self._session(account)
        if not is_minor:  # a minor's welcome waits until the guardian is linked
            await self.svc.welcome.send(account)
        if is_minor:
            result["next_step"] = "link_guardian"
            result["hint"] = "Signup finishes once a parent or guardian links and verifies their account."
        return result

    # --- sessions ------------------------------------------------------------------------
    async def _session(self, account: Account) -> dict[str, Any]:
        refresh = self.infra.tokens.issue(self.REFRESH, account.id, self.s.refresh_token_ttl_seconds)
        jti = self.infra.tokens.decode(refresh, self.REFRESH)["jti"]
        await self.kv.set(f"refresh:{jti}", account.id, self.s.refresh_token_ttl_seconds)
        return {
            "access_token": self.infra.tokens.issue(self.ACCESS, account.id, self.s.access_token_ttl_seconds),
            "refresh_token": refresh,
            "token_type": "bearer",
            "expires_in_seconds": self.s.access_token_ttl_seconds,
            "account": {"id": account.id, "type": account.type, "handle": account.handle, "status": account.status},
        }

    async def login(self, email: str, password: str, totp_code: str | None, ip: str) -> dict[str, Any]:
        email = email.lower()
        await self.limiter.enforce(self.policies.login_ip, ip)
        await self.limiter.enforce(self.policies.login, email, "Too many login attempts. Try again later.")
        account = await self.repo(Account).first(Account.email == email)
        # verify() burns the same time when the account is missing, so timing does not leak existence.
        valid = self.infra.passwords.verify(password, account.password_hash if account else None)
        if account is None or not valid:
            raise Unauthorized("Incorrect email or password", code="invalid_credentials")
        if account.status in (AccountStatus.DELETED, AccountStatus.REMOVED):
            raise Unauthorized("Incorrect email or password", code="invalid_credentials")
        if account.is_admin and not account.totp_enabled:
            raise Forbidden("Finish two-factor setup from your invite link first", code="2fa_setup_required")
        if account.totp_enabled:
            if not totp_code:
                raise Unauthorized("Two-factor code required", code="2fa_required")
            if not self.infra.totp.verify(account.totp_secret, totp_code):
                raise Unauthorized("Incorrect two-factor code", code="2fa_invalid")
        await self.limiter.reset(self.policies.login, email)
        account.last_active_at = self.now()
        return await self._session(account)

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        payload = self.infra.tokens.decode(refresh_token, self.REFRESH)
        key = f"refresh:{payload['jti']}"
        if await self.kv.get(key) != payload["sub"]:
            raise Unauthorized("Session expired. Log in again.", code="session_revoked")
        await self.kv.delete(key)  # rotation: each refresh token works once
        account = await self.repo(Account).get(payload["sub"])
        if account is None or account.status in (AccountStatus.DELETED, AccountStatus.REMOVED):
            raise Unauthorized("Session expired. Log in again.", code="session_revoked")
        return await self._session(account)

    async def logout(self, refresh_token: str) -> None:
        try:
            payload = self.infra.tokens.decode(refresh_token, self.REFRESH)
        except Unauthorized:
            return
        await self.kv.delete(f"refresh:{payload['jti']}")

    async def resolve_access_token(self, token: str) -> Account:
        payload = self.infra.tokens.decode(token, self.ACCESS)
        account = await self.repo(Account).get(payload["sub"])
        if account is None or account.status in (AccountStatus.DELETED, AccountStatus.REMOVED):
            raise Unauthorized("Account not available", code="account_unavailable")
        return account

    # --- password reset ------------------------------------------------------------------
    async def request_password_reset(self, email: str, ip: str) -> dict[str, Any]:
        email = email.lower()
        await self.limiter.enforce(self.policies.otp_ip, ip)
        await self.limiter.enforce(self.policies.password_reset, email)
        account = await self.repo(Account).first(Account.email == email)
        if account is not None:
            code = await self.svc.otp.issue(self.RESET, email)
            self.svc.notifications.email(email, "Reset your Wamiwa password", f"Your reset code is {code}.")
        # Same answer either way: the reset form must not reveal which emails exist.
        return {"message": "If that email is registered, a reset code has been sent."}

    async def reset_password(self, email: str, code: str, new_password: str) -> None:
        email = email.lower()
        await self.svc.otp.verify(self.RESET, email, code)
        account = await self.repo(Account).first(Account.email == email)
        if account is None:
            raise Unprocessable("Incorrect code", code="otp_incorrect")
        account.password_hash = self.infra.passwords.hash(new_password)
        await self.svc.notifications.notify(account.id, NotificationType.SECURITY, "Your password was changed",
                                            "If this was not you, contact support immediately.")

    # --- two-factor (section 20) ---------------------------------------------------------
    async def begin_totp_setup(self, account: Account) -> dict[str, str]:
        if account.totp_enabled:
            raise Conflict("Two-factor login is already on", code="2fa_enabled")
        account.totp_secret = self.infra.totp.generate_secret()
        return {"secret": account.totp_secret,
                "otpauth_url": self.infra.totp.provisioning_uri(account.totp_secret, account.email or account.handle)}

    async def enable_totp(self, account: Account, code: str) -> None:
        if not self.infra.totp.verify(account.totp_secret, code):
            raise Unprocessable("Incorrect two-factor code", code="2fa_invalid")
        account.totp_enabled = True
        await self.svc.notifications.notify(account.id, NotificationType.SECURITY, "Two-factor login is now on")

    async def disable_totp(self, account: Account, code: str) -> None:
        if account.is_admin:
            raise Forbidden("Two-factor login is required for admins", code="2fa_required_for_admins")
        self.require_totp(account, code)
        account.totp_enabled, account.totp_secret = False, None
        await self.svc.notifications.notify(account.id, NotificationType.SECURITY, "Two-factor login was turned off")

    def require_totp(self, account: Account, code: str | None) -> None:
        """Gate for sensitive changes such as payout details."""
        if not account.totp_enabled:
            raise Forbidden("Turn on two-factor login before making this change", code="2fa_setup_required",
                            hint="POST /auth/2fa/setup")
        if not self.infra.totp.verify(account.totp_secret, code):
            raise Unauthorized("Incorrect two-factor code", code="2fa_invalid")
