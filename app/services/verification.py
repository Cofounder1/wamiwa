"""ON-05 identity, ON-06 bank accounts, ON-07 creators, ON-07a popularity card, ON-08 friend import."""
import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from app.core.enums import (
    AccountStatus, DocumentType, NotificationType, PopularityLevel, PublicFigureMethod, Queue,
    SignalCategory, SocialPlatform, VerificationStatus,
)
from app.core.exceptions import Conflict, Forbidden, NotFound, Unprocessable
from app.core.utils import age_on, names_match, normalize_phone
from app.models import (
    Account, BankAccount, ExternalSocialLink, FriendInvite, IdentityVerification, PopularitySignal,
    PublicFigureVerification, QueueItem, UserProfile,
)
from app.services.base import BaseService


class IdentityService(BaseService):
    LIVE_CHECK_TTL = 30 * 60

    async def _require_can_verify(self, account: Account) -> UserProfile:
        profile = await self.svc.access.profile(account.id)
        if profile is None:
            raise Unprocessable("Companies verify with business documents", code="not_a_user",
                                hint="POST /companies/verification")
        if profile.is_minor:
            raise Forbidden("Your guardian provides the ID for your account", code="guardian_provides_id")
        if await self.repo(IdentityVerification).exists(
                IdentityVerification.account_id == account.id,
                IdentityVerification.status == VerificationStatus.MANUAL_REVIEW):
            raise Conflict("Your verification is with our team for manual review", code="manual_review")
        return profile

    async def start_live_check(self, account: Account) -> dict[str, Any]:
        """ON-05: what the browser needs for the live check. With YouVerify, its own check runs in the page under a
        session tied to this account; otherwise Wamiwa's camera check records a frame and a clip."""
        await self._require_can_verify(account)
        await self.limiter.enforce(self.policies.live_checks, account.id)
        session = await self.providers.identity.start_liveness(account.id)
        if session is None:
            return {"provider": "camera"}
        await self.kv.set(f"livecheck:{session['session_id']}", account.id, self.LIVE_CHECK_TTL)
        return session

    async def submit(self, account: Account, document_type: DocumentType, document_ref: str | None,
                     selfie_ref: str | None, liveness_ref: str | None = None, *, id_number: str | None = None,
                     liveness_session_id: str | None = None) -> dict[str, Any]:
        profile = await self._require_can_verify(account)
        if liveness_session_id:
            # Only a live check this account started counts, and only once.
            if await self.kv.get(f"livecheck:{liveness_session_id}") != account.id:
                raise Forbidden("Start the live check again", code="live_check_expired")
            await self.kv.delete(f"livecheck:{liveness_session_id}")
        await self.limiter.enforce(self.policies.id_verification, account.id)

        result = await self.providers.identity.verify(
            document_type, document_ref, selfie_ref, liveness_ref, id_number=id_number,
            liveness_session=liveness_session_id, in_home_country=not self.svc.payment.is_international(account))
        number = (id_number or "").replace(" ", "")
        record = await self.repo(IdentityVerification).add(IdentityVerification(
            account_id=account.id, document_type=document_type,
            # A looked-up number is kept masked; the full number is never stored.
            document_ref=document_ref or f"{document_type}:{'*' * max(0, len(number) - 4)}{number[-4:]}",
            selfie_ref=selfie_ref or result.face_ref or "", liveness_ref=liveness_ref or liveness_session_id,
            liveness_result=result.liveness_passed, verified_name=result.verified_name,
            verified_dob=result.date_of_birth, failure_reason=result.failure_reason,
        ))
        if result.needs_review:
            # Passports and IDs from outside Nigeria: staff read the name and date of birth from the document.
            record.status = VerificationStatus.MANUAL_REVIEW
            await self.svc.queues.enqueue(Queue.IDENTITY, "identity_document", record.id,
                                          subject_account_id=account.id,
                                          summary=f"Confirm name and date of birth from the {document_type}. "
                                                  f"{result.review_note or ''}".strip())
            return self._status(record, profile)
        if result.passed:
            await self._accept(account, profile, record, result.verified_name or profile.name, result.date_of_birth,
                               result.face_ref)
            return self._status(record, profile)

        profile.id_attempts += 1
        if profile.id_attempts >= self.s.id_max_attempts:
            record.status = VerificationStatus.MANUAL_REVIEW
            await self.svc.queues.enqueue(Queue.IDENTITY, "identity_verification", record.id,
                                          subject_account_id=account.id,
                                          summary=f"{self.s.id_max_attempts} failed attempts: {result.failure_reason}")
        else:
            record.status = VerificationStatus.FAILED
        return self._status(record, profile)

    async def _accept(self, account: Account, profile: UserProfile, record: IdentityVerification, name: str,
                      dob, face_ref: str | None) -> None:
        """The ID checks out: verify the account, unless it shows a minor or is already verified elsewhere."""
        record.verified_name, record.verified_dob = name, dob
        if dob and age_on(dob, self.clock.today()) < self.s.adult_age:
            # The ID contradicts the adult date of birth given at signup (ON-02).
            record.status = VerificationStatus.FAILED
            record.failure_reason = "ID shows a different age from the one given at signup"
            await self.svc.accounts.flag_false_age(account, record.failure_reason)
            return
        other = await self._verified_elsewhere(account, name, dob)
        if other:
            # One person, one account: stops a suspended person returning on a new account, and one person
            # running several earning accounts. Genuine cases (a lost login) are sorted out by support.
            record.status = VerificationStatus.FAILED
            record.failure_reason = ("This ID is already verified on another Wamiwa account. If that's your old "
                                     "account and you can't get into it, contact support.")
            await self.svc.queues.enqueue(
                Queue.IDENTITY, "duplicate_identity", record.id, subject_account_id=account.id, severity="high",
                summary=f"ID already verified on account {other.id} ({other.status}); possible second account")
            return
        record.status = VerificationStatus.PASSED
        await self._mark_verified(account, profile, name, dob, face_ref)

    async def _verified_elsewhere(self, account: Account, name: str | None, dob) -> Account | None:
        """Another account (not deleted) already verified with the same name and date of birth."""
        if not name or not dob:
            return None
        key = " ".join(re.findall(r"[a-z]+", name.lower()))
        rows = (await self.db.execute(
            select(IdentityVerification, Account).join(Account, Account.id == IdentityVerification.account_id).where(
                IdentityVerification.status == VerificationStatus.PASSED, IdentityVerification.verified_dob == dob,
                IdentityVerification.account_id != account.id, Account.status != AccountStatus.DELETED))).all()
        for record, other in rows:
            if " ".join(re.findall(r"[a-z]+", (record.verified_name or "").lower())) == key:
                return other
        return None

    def _status(self, record: IdentityVerification, profile: UserProfile) -> dict[str, Any]:
        return {
            "verification_id": record.id, "status": record.status, "reason": record.failure_reason,
            "retries_left": max(0, self.s.id_max_attempts - profile.id_attempts),
            "id_verified": profile.id_verified,
        }

    async def _mark_verified(self, account: Account, profile: UserProfile, name: str, dob, face_ref: str | None) -> None:
        profile.id_verified, profile.verified_name, profile.name = True, name, name
        profile.id_attempts = 0
        if dob:
            profile.date_of_birth = dob
        if face_ref:
            profile.face_ref = face_ref
        account.display_name = name
        await self.svc.bank_accounts.recheck_name(account)
        await self.svc.guardians.on_guardian_verified(account)
        await self.svc.profiles.invalidate(account)
        await self.svc.notifications.notify(account.id, NotificationType.SECURITY, "Identity verified",
                                            "You can now earn on Wamiwa.")

    async def status(self, account: Account) -> dict[str, Any]:
        profile = await self.svc.access.profile(account.id)
        latest = await self.repo(IdentityVerification).first(
            IdentityVerification.account_id == account.id, order_by=IdentityVerification.id.desc())
        return {
            "id_verified": await self.svc.access.is_id_verified(account),
            "latest": None if latest is None else {"status": latest.status, "reason": latest.failure_reason,
                                                   "submitted_at": latest.created_at.isoformat()},
            "retries_left": max(0, self.s.id_max_attempts - (profile.id_attempts if profile else 0)),
        }

    async def apply_review(self, item: QueueItem, approved: bool, admin: Account, reason: str, payload: dict) -> None:
        account = await self.repo(Account).require(item.subject_account_id)
        profile = await self.svc.access.profile(account.id)
        if item.target_type == "false_age":
            if approved:  # the stated age stands
                account.status = account.status_before_hold or AccountStatus.ACTIVE
                account.suspended_reason, account.suspended_at = None, None
            await self.svc.notifications.notify(
                account.id, NotificationType.LEGAL,
                "Your account has been restored" if approved else "Your account stays suspended", reason)
            return
        record = await self.repo(IdentityVerification).require(item.target_id)
        record.reviewed_by = admin.id
        if approved and item.target_type == "identity_document":
            # The reviewer reads the name and date of birth off the document; the usual age and one-ID checks apply.
            name, born = payload.get("verified_name"), payload.get("verified_dob")
            try:
                born = date.fromisoformat(str(born))
            except ValueError:
                born = None
            if not name or born is None:
                raise Unprocessable("Give the name and date of birth exactly as on the document",
                                    code="document_details_required", fields=["verified_name", "verified_dob"])
            await self._accept(account, profile, record, " ".join(str(name).split()), born, record.selfie_ref or None)
            if record.status != VerificationStatus.PASSED:
                await self.svc.notifications.notify(account.id, NotificationType.SECURITY,
                                                    "Identity verification was not approved", record.failure_reason)
            return
        if approved:
            record.status = VerificationStatus.PASSED
            await self._mark_verified(account, profile, payload.get("verified_name") or record.verified_name
                                      or profile.name, record.verified_dob, f"face:{record.selfie_ref}")
        else:
            record.status, record.failure_reason = VerificationStatus.REJECTED, reason
            await self.svc.notifications.notify(account.id, NotificationType.SECURITY,
                                                "Identity verification was not approved", reason)


class BankAccountService(BaseService):
    async def _expected_name(self, account: Account) -> str | None:
        if account.is_company:
            company = await self.svc.access.company(account.id)
            return company.business_name if company else None
        profile = await self.svc.access.profile(account.id)
        return profile.verified_name if profile else None

    async def save(self, account: Account, bank_code: str, account_number: str, account_name: str,
                   totp_code: str | None, details: dict[str, str] | None = None) -> dict[str, Any]:
        if await self.svc.access.is_minor(account.id):
            raise Forbidden("Payouts for 13-17 accounts go to the guardian's bank account",
                            code="guardian_account_required")
        await self.svc.access.require_id_verified(account, "add a bank account")
        existing = await self.repo(BankAccount).first(BankAccount.account_id == account.id)
        if existing is not None:
            self.svc.auth.require_totp(account, totp_code)  # section 20: 2FA required for payout changes

        # PY-05: creators in Nigeria are paid to a Nigerian bank in Naira; everyone else to their own bank in dollars.
        international = self.svc.payment.is_international(account)
        if international:
            bank_code, account_number = "intl", account_number.replace(" ", "").upper()
            missing = [k for k in ("bank_name", "beneficiary_address", "beneficiary_country") if not (details or {}).get(k)]
            if not ((details or {}).get("swift_code") or (details or {}).get("routing_number")):
                missing.append("swift_code or routing_number")
            if missing:
                raise Unprocessable("For a bank outside Nigeria, add: " + ", ".join(missing), code="bank_details_required",
                                    missing=missing)
            if not (5 <= len(account_number) <= 34 and account_number.isalnum()):
                raise Unprocessable("Enter the account number or IBAN", code="invalid_account_number")
        elif not (account_number.isdigit() and len(account_number) == 10):
            raise Unprocessable("A Nigerian account number is 10 digits", code="invalid_account_number")
        resolved = await self.providers.payments.resolve_account_name(bank_code, account_number, account_name)
        if resolved is None:
            raise Unprocessable("We could not find that bank account. Check the details.", code="account_not_found")
        matches = names_match(resolved, await self._expected_name(account) or "")

        if existing is None:
            bank = await self.repo(BankAccount).add(BankAccount(
                account_id=account.id, bank_code=bank_code, account_number=account_number,
                account_name=resolved, verified=matches, details=details if bank_code == "intl" else None))
        else:
            bank = existing
            bank.bank_code, bank.account_number, bank.account_name = bank_code, account_number, resolved
            bank.details = details if bank_code == "intl" else None
            bank.verified = matches
            bank.hold_until = self.now() + timedelta(hours=self.s.bank_change_hold_hours)
            alert = f"Your payout bank details were changed. Payouts are held for {self.s.bank_change_hold_hours} hours."
            await self.svc.notifications.notify(account.id, NotificationType.SECURITY, "Bank details changed", alert)
            if account.phone and account.phone_verified_at:
                self.svc.notifications.sms(account.phone, f"Wamiwa: {alert}")
        return self.present(bank)

    def present(self, bank: BankAccount) -> dict[str, Any]:
        on_hold = bool(bank.hold_until and bank.hold_until > self.now())
        return {
            "bank_code": bank.bank_code,
            "account_number": f"******{bank.account_number[-4:]}",
            "bank_name": (bank.details or {}).get("bank_name"), "currency": "USD" if bank.bank_code == "intl" else "NGN",
            "account_name": bank.account_name, "name_matches_id": bank.verified,
            "hold_until": bank.hold_until.isoformat() if on_hold else None,
            "payouts_blocked": not bank.verified or on_hold,
            "blocked_reason": (None if bank.verified else "Account name does not match your verified ID name")
            or ("Recently changed: held for 48 hours" if on_hold else None),
        }

    async def get(self, account: Account) -> dict[str, Any] | None:
        bank = await self.repo(BankAccount).first(BankAccount.account_id == account.id)
        return self.present(bank) if bank else None

    async def recheck_name(self, account: Account) -> None:
        bank = await self.repo(BankAccount).first(BankAccount.account_id == account.id)
        if bank:
            bank.verified = names_match(bank.account_name, await self._expected_name(account) or "")

    async def payable(self, account_id: str) -> tuple[BankAccount | None, str | None]:
        """The bank account to pay, or the reason payouts are blocked."""
        bank = await self.repo(BankAccount).first(BankAccount.account_id == account_id)
        if bank is None:
            return None, "No bank account on file"
        if not bank.verified:
            return None, "Bank account name does not match the verified ID name"
        if bank.hold_until and bank.hold_until > self.now():
            return None, "Bank details changed recently; payouts are on a 48-hour hold"
        return bank, None


class CreatorVerificationService(BaseService):
    async def connect(self, account: Account, platform: SocialPlatform, auth_code: str) -> dict[str, Any]:
        profile = await self.svc.access.profile(account.id)
        if profile is None:
            raise Unprocessable("Only user accounts connect social accounts", code="not_a_user")
        try:
            social = await self.providers.social.connect(platform, auth_code)
        except ValueError as exc:
            raise Unprocessable(str(exc), code="social_login_failed",
                                hint="Log in to the platform; screenshots are not accepted") from exc
        if social.external_id and await self.repo(ExternalSocialLink).exists(
                ExternalSocialLink.external_id == social.external_id, ExternalSocialLink.account_id != account.id,
                ExternalSocialLink.connected.is_(True)):
            raise Conflict("That account is already connected to another Wamiwa user", code="social_already_linked")
        flagged = social.fake_follower_ratio >= self.s.fake_follower_threshold
        link = await self.repo(ExternalSocialLink).first(
            ExternalSocialLink.account_id == account.id, ExternalSocialLink.platform == platform)
        if link is None:
            link = await self.repo(ExternalSocialLink).add(ExternalSocialLink(
                account_id=account.id, platform=platform, handle=social.handle, last_checked_at=self.now()))
        link.handle, link.external_id, link.follower_count = social.handle, social.external_id, social.followers
        link.flagged, link.connected, link.last_checked_at = flagged, True, self.now()
        link.friend_ids = list(social.friend_ids)
        if flagged:
            await self.svc.queues.enqueue(Queue.FAKE_FOLLOWERS, "external_social_link", link.id,
                                          subject_account_id=account.id,
                                          summary=f"{platform} @{social.handle}: suspected fake followers")
        await self.recompute(account, profile)
        return await self.summary(account)

    async def combined_followers(self, account_id: str) -> int:
        return await self.repo(ExternalSocialLink).sum(
            ExternalSocialLink.follower_count, ExternalSocialLink.account_id == account_id,
            ExternalSocialLink.connected.is_(True), ExternalSocialLink.flagged.is_(False))

    async def recompute(self, account: Account, profile: UserProfile | None = None) -> None:
        profile = profile or await self.svc.access.profile(account.id)
        if profile is None:
            return
        qualifies = await self.combined_followers(account.id) >= self.s.verified_creator_followers
        if qualifies and not profile.is_verified_creator:
            profile.is_verified_creator, profile.new_orders_paused = True, False
            await self.svc.notifications.notify(account.id, NotificationType.SECURITY, "You are a Verified creator",
                                                "You can receive orders immediately.")
        elif qualifies:
            profile.new_orders_paused = False
        elif profile.is_verified_creator:
            # ON-07: new orders pause; active contracts continue.
            profile.is_verified_creator, profile.new_orders_paused = False, True
            await self.svc.notifications.notify(
                account.id, NotificationType.CONTRACT, "New orders are paused",
                "A connected account dropped or fell below 5,000 followers. Active contracts continue.")
        await self.svc.likeness_pages.refresh_eligibility(account)
        await self.svc.profiles.invalidate(account)

    async def effective_human_followers(self, account: Account) -> int:
        """Human followers on Wamiwa, plus external followers for Verified creators (ON-07)."""
        profile = await self.svc.access.profile(account.id)
        external = await self.combined_followers(account.id) if profile and profile.is_verified_creator else 0
        return account.followers_human + external

    async def summary(self, account: Account) -> dict[str, Any]:
        links = await self.repo(ExternalSocialLink).list(ExternalSocialLink.account_id == account.id)
        profile = await self.svc.access.profile(account.id)
        return {
            "verified_creator": bool(profile and profile.is_verified_creator),
            "combined_followers": await self.combined_followers(account.id),
            "threshold": self.s.verified_creator_followers,
            "accounts": [{"platform": l.platform, "handle": l.handle, "followers": l.follower_count,
                          "connected": l.connected, "under_review": l.flagged,
                          "last_checked_at": l.last_checked_at.isoformat()} for l in links],
        }

    async def recheck_due(self) -> int:
        """Job: connected accounts are rechecked every 90 days."""
        due = await self.repo(ExternalSocialLink).list(
            ExternalSocialLink.connected.is_(True),
            ExternalSocialLink.last_checked_at < self.now() - timedelta(days=self.s.social_recheck_days))
        touched: set[str] = set()
        for link in due:
            social = await self.providers.social.refresh(link.platform, link.handle)
            link.last_checked_at = self.now()
            if social is None:
                link.connected = False
            else:
                link.follower_count = social.followers
                link.flagged = link.flagged or social.fake_follower_ratio >= self.s.fake_follower_threshold
            touched.add(link.account_id)
        for account in (await self.repo(Account).by_ids(list(touched))).values():
            await self.recompute(account)
        return len(due)

    async def apply_review(self, item: QueueItem, approved: bool, admin: Account, reason: str, payload: dict) -> None:
        link = await self.repo(ExternalSocialLink).require(item.target_id)
        link.flagged = not approved  # approved = followers judged genuine
        account = await self.repo(Account).require(link.account_id)
        await self.recompute(account)
        await self.svc.notifications.notify(
            account.id, NotificationType.SECURITY,
            f"Your {link.platform} account was {'cleared' if approved else 'not accepted'}", reason)


class PopularityCardService(BaseService):
    LEVELS = {PopularityLevel.NONE: 0, PopularityLevel.NOTABLE: 1, PopularityLevel.PROMINENT: 2, PopularityLevel.MAJOR: 3}

    async def submit_signal(self, account: Account, category: SignalCategory, title: str, evidence_ref: str) -> dict[str, Any]:
        if await self.svc.access.profile(account.id) is None:
            raise Unprocessable("Only user accounts have a popularity card", code="not_a_user")
        signal = await self.repo(PopularitySignal).add(PopularitySignal(
            account_id=account.id, category=category, title=title.strip(), evidence_ref=evidence_ref))
        await self.svc.queues.enqueue(Queue.PUBLIC_FIGURE, "popularity_signal", signal.id,
                                      subject_account_id=account.id, summary=f"{category}: {title}")
        return {"signal_id": signal.id, "status": signal.status}

    async def request_major_person(self, account: Account, method: PublicFigureMethod, evidence: list[str]) -> dict[str, Any]:
        """ON-07: verified well-known name online, or proof sent to support. Admin approves."""
        if method == PublicFigureMethod.POPULARITY_CARD:
            raise Unprocessable("Submit popularity card evidence as signals", code="use_signals",
                                hint="POST /verification/popularity/signals")
        if await self.svc.access.profile(account.id) is None:
            raise Unprocessable("Only user accounts can be verified as a public figure", code="not_a_user")
        if await self.repo(PublicFigureVerification).exists(
                PublicFigureVerification.account_id == account.id,
                PublicFigureVerification.status == VerificationStatus.PENDING):
            raise Conflict("You already have a request under review", code="request_pending")
        request = await self.repo(PublicFigureVerification).add(PublicFigureVerification(
            account_id=account.id, method=method, evidence=evidence))
        await self.svc.queues.enqueue(Queue.PUBLIC_FIGURE, "public_figure", request.id,
                                      subject_account_id=account.id, summary=f"Verified major person via {method}")
        return {"request_id": request.id, "status": request.status}

    async def recompute(self, account: Account) -> PopularityLevel:
        """Notable = 1 verified signal, Prominent = 2, Major = 3+ from at least two categories."""
        profile = await self.svc.access.profile(account.id)
        verified = await self.repo(PopularitySignal).list(
            PopularitySignal.account_id == account.id, PopularitySignal.status == VerificationStatus.PASSED)
        categories = {s.category for s in verified}
        if len(verified) >= 3 and len(categories) >= 2:
            level = PopularityLevel.MAJOR
        elif len(verified) >= 2:
            level = PopularityLevel.PROMINENT
        elif len(verified) == 1:
            level = PopularityLevel.NOTABLE
        else:
            level = PopularityLevel.NONE
        approved_otherwise = await self.repo(PublicFigureVerification).exists(
            PublicFigureVerification.account_id == account.id,
            PublicFigureVerification.status == VerificationStatus.PASSED)
        profile.popularity_level = level
        profile.is_major_person = approved_otherwise or self.LEVELS[level] >= self.LEVELS[PopularityLevel.PROMINENT]
        if verified and profile.popularity_review_at is None:
            profile.popularity_review_at = self.now() + timedelta(days=30 * self.s.popularity_review_months)
        await self.svc.likeness_pages.refresh_eligibility(account)
        await self.svc.profiles.invalidate(account)
        return level

    async def card(self, account_id: str) -> dict[str, Any]:
        """Public view: the level and the verified signals behind it, never the evidence documents."""
        profile = await self.svc.access.profile(account_id)
        if profile is None:
            raise NotFound("Account not found")
        verified = await self.repo(PopularitySignal).list(
            PopularitySignal.account_id == account_id, PopularitySignal.status == VerificationStatus.PASSED)
        return {"level": profile.popularity_level, "verified_major_person": profile.is_major_person,
                "signals": [{"category": s.category, "title": s.title} for s in verified]}

    async def my_signals(self, account: Account) -> list[dict[str, Any]]:
        signals = await self.repo(PopularitySignal).list(PopularitySignal.account_id == account.id)
        return [{"id": s.id, "category": s.category, "title": s.title, "status": s.status} for s in signals]

    async def appeal(self, account: Account, signal_id: str) -> dict[str, Any]:
        """ON-07a: a second admin review, once per 30 days (new evidence goes through submit_signal)."""
        profile = await self.svc.access.profile(account.id)
        signal = await self.repo(PopularitySignal).require(signal_id, "Signal not found")
        if signal.account_id != account.id:
            raise NotFound("Signal not found")
        if signal.status != VerificationStatus.REJECTED:
            raise Unprocessable("Only a rejected signal can be appealed", code="not_rejected")
        window = timedelta(days=self.s.popularity_appeal_days)
        if profile.popularity_last_appeal_at and profile.popularity_last_appeal_at + window > self.now():
            raise Conflict("You can appeal once every 30 days", code="appeal_too_soon",
                           next_appeal_at=(profile.popularity_last_appeal_at + window).isoformat())
        profile.popularity_last_appeal_at = self.now()
        signal.status, signal.is_second_review = VerificationStatus.PENDING, True
        await self.svc.queues.enqueue(Queue.PUBLIC_FIGURE, "popularity_signal", signal.id,
                                      subject_account_id=account.id,
                                      summary=f"Second review requested (first reviewer {signal.reviewed_by})")
        return {"signal_id": signal.id, "status": signal.status}

    async def apply_review(self, item: QueueItem, approved: bool, admin: Account, reason: str, payload: dict) -> None:
        account = await self.repo(Account).require(item.subject_account_id)
        if item.target_type == "public_figure":
            request = await self.repo(PublicFigureVerification).require(item.target_id)
            request.status = VerificationStatus.PASSED if approved else VerificationStatus.REJECTED
            request.reviewed_by, request.reason = admin.id, reason
            await self.recompute(account)
            await self.svc.notifications.notify(
                account.id, NotificationType.SECURITY,
                "You are a Verified major person" if approved else "Public figure request not approved", reason)
            return
        if item.target_type == "popularity_review":  # 12-month review: reject clears the stale signals
            if not approved:
                for signal in await self.repo(PopularitySignal).list(
                        PopularitySignal.account_id == account.id, PopularitySignal.status == VerificationStatus.PASSED):
                    signal.status = VerificationStatus.REJECTED
            profile = await self.svc.access.profile(account.id)
            profile.popularity_review_at = self.now() + timedelta(days=30 * self.s.popularity_review_months)
            await self.recompute(account)
            return
        signal = await self.repo(PopularitySignal).require(item.target_id)
        if signal.is_second_review and signal.reviewed_by == admin.id:
            raise Forbidden("A second review must be done by a different admin", code="same_reviewer")
        signal.status = VerificationStatus.PASSED if approved else VerificationStatus.REJECTED
        signal.reviewed_by, signal.reviewed_at = admin.id, self.now()
        level = await self.recompute(account)
        await self.svc.notifications.notify(
            account.id, NotificationType.SECURITY,
            f"Popularity card updated: {level}" if approved else "A popularity signal was not verified", reason)

    async def revoke_signal(self, admin: Account, signal_id: str, reason: str) -> PopularityLevel:
        """A signal changed (e.g. the person left a CEO role): re-score sooner than the yearly review."""
        signal = await self.repo(PopularitySignal).require(signal_id, "Signal not found")
        await self.svc.audit.log(admin.id, "popularity.revoke_signal", "popularity_signal", signal.id, reason)
        signal.status, signal.reviewed_by, signal.reviewed_at = VerificationStatus.REJECTED, admin.id, self.now()
        return await self.recompute(await self.repo(Account).require(signal.account_id))

    async def queue_due_reviews(self) -> int:
        """Job: cards are reviewed every 12 months."""
        due = await self.repo(UserProfile).list(
            UserProfile.popularity_review_at.is_not(None), UserProfile.popularity_review_at < self.now())
        for profile in due:
            await self.svc.queues.enqueue(Queue.PUBLIC_FIGURE, "popularity_review", profile.account_id,
                                          subject_account_id=profile.account_id, summary="12-month card review")
        return len(due)


class FriendImportService(BaseService):
    async def facebook_friends(self, account: Account) -> dict[str, Any]:
        link = await self.repo(ExternalSocialLink).first(
            ExternalSocialLink.account_id == account.id, ExternalSocialLink.platform == SocialPlatform.FACEBOOK,
            ExternalSocialLink.connected.is_(True))
        if link is None:
            raise Unprocessable("Connect Facebook first", code="facebook_not_connected",
                                hint="POST /verification/social")
        friend_keys = [f"{SocialPlatform.FACEBOOK}:{friend}" for friend in link.friend_ids or []]
        matches = await self.repo(ExternalSocialLink).list(
            ExternalSocialLink.external_id.in_(friend_keys or [""]), ExternalSocialLink.connected.is_(True))
        accounts = await self.repo(Account).by_ids([m.account_id for m in matches])
        hidden = await self.svc.blocks.hidden_ids(account.id)
        return {
            "notice": "Only friends who have also connected Wamiwa are shown.",
            "friends": [{"id": a.id, "handle": a.handle, "name": a.display_name}
                        for a in accounts.values() if a.is_visible and a.id not in hidden and a.id != account.id],
        }

    async def match_contacts(self, account: Account, phones: list[str]) -> dict[str, Any]:
        """Contacts are matched by hashed number; nothing about non-users is stored."""
        hashes: dict[str, None] = {}
        for phone in phones[:1000]:
            try:
                hashes[self.infra.hasher.digest(normalize_phone(phone))] = None
            except Unprocessable:
                continue
        matched = await self.repo(Account).list(
            Account.phone_hash.in_(list(hashes) or [""]), Account.phone_verified_at.is_not(None))
        hidden = await self.svc.blocks.hidden_ids(account.id)
        users = [a for a in matched if a.is_visible and a.id != account.id and a.id not in hidden]
        return {"on_wamiwa": [{"id": a.id, "handle": a.handle, "name": a.display_name} for a in users],
                "not_on_wamiwa": max(0, len(hashes) - len(matched))}

    async def invite(self, account: Account, phone: str) -> dict[str, Any]:
        """One invite per contact, never repeated automatically."""
        phone = normalize_phone(phone)
        contact_hash = self.infra.hasher.digest(phone)
        if await self.repo(Account).exists(Account.phone_hash == contact_hash, Account.phone_verified_at.is_not(None)):
            raise Conflict("This contact is already on Wamiwa", code="already_user")
        if await self.repo(FriendInvite).exists(
                FriendInvite.inviter_account_id == account.id, FriendInvite.contact_hash == contact_hash):
            raise Conflict("You have already invited this contact", code="already_invited")
        await self.limiter.enforce(self.policies.friend_invites, account.id)
        await self.repo(FriendInvite).add(FriendInvite(inviter_account_id=account.id, contact_hash=contact_hash))
        self.svc.notifications.sms(
            phone, f"{account.display_name or account.handle} invited you to Wamiwa: {self.s.public_base_url}/join")
        return {"invited": True}
