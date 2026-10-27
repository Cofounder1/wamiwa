"""Profiles (ON-04, SO-04), guardians (ON-03), the 18th-birthday conversion (ON-02) and access rules."""
from typing import Any

from app.core.enums import (
    MANDATORY_NOTIFICATIONS, AccountStatus, AgentCommentPolicy, FeedTab, GuardianLinkStatus, NotificationType, Queue,
)
from app.core.exceptions import Conflict, Forbidden, NotFound, Unprocessable
from app.core.security import SecretFactory
from app.core.utils import age_on, normalize_phone
from app.core import regions as core_regions
from app.models import (
    Account, CompanyProfile, GuardianCompanyApproval, GuardianLink, Subscription, UserProfile,
)
from app.services.base import BaseService


class AccessPolicy(BaseService):
    """Answers "may this account do that?" in one place so every epic applies the same rules."""

    async def profile(self, account_id: str) -> UserProfile | None:
        return await self.repo(UserProfile).first(UserProfile.account_id == account_id)

    async def company(self, account_id: str) -> CompanyProfile | None:
        return await self.repo(CompanyProfile).first(CompanyProfile.account_id == account_id)

    async def is_minor(self, account_id: str) -> bool:
        profile = await self.profile(account_id)
        return bool(profile and profile.is_minor)

    async def guardian_link(self, minor_id: str) -> GuardianLink | None:
        return await self.repo(GuardianLink).first(
            GuardianLink.minor_account_id == minor_id, GuardianLink.status == GuardianLinkStatus.ACTIVE)

    async def guardian_of(self, minor_id: str) -> Account | None:
        link = await self.guardian_link(minor_id)
        return await self.repo(Account).get(link.guardian_account_id) if link else None

    async def is_guardian_of(self, guardian_id: str, minor_id: str) -> bool:
        link = await self.guardian_link(minor_id)
        return bool(link and link.guardian_account_id == guardian_id)

    async def require_guardian_of(self, guardian_id: str, minor_id: str) -> None:
        if not await self.is_guardian_of(guardian_id, minor_id):
            raise Forbidden("You are not the guardian of this account", code="not_guardian")

    async def is_id_verified(self, account: Account) -> bool:
        """ON-05. For minors the guardian provides the ID; companies use business verification."""
        if account.is_company:
            company = await self.company(account.id)
            return bool(company and company.verified)
        profile = await self.profile(account.id)
        if profile is None:
            return False
        if profile.is_minor:
            guardian = await self.guardian_of(account.id)
            guardian_profile = await self.profile(guardian.id) if guardian else None
            return bool(guardian_profile and guardian_profile.id_verified)
        return profile.id_verified

    async def require_id_verified(self, account: Account, action: str = "do this") -> None:
        if not await self.is_id_verified(account):
            raise Forbidden(f"Verify your identity to {action}", code="id_verification_required",
                            hint="POST /verification/identity")

    async def require_verified_company(self, account: Account, action: str = "do this") -> CompanyProfile:
        """CO-01: unverified companies can browse but cannot pay, hire, DM about work or use APIs."""
        company = await self.company(account.id) if account.is_company else None
        if company is None:
            raise Forbidden(f"Only verified companies can {action}", code="company_required")
        if not company.verified:
            raise Forbidden(f"Verify your business to {action}", code="company_verification_required",
                            hint="POST /companies/verification")
        return company

    async def require_adult(self, account: Account, action: str = "do this") -> UserProfile:
        profile = await self.profile(account.id)
        if profile is None or profile.is_minor:
            raise Forbidden(f"You must be 18 or older to {action}", code="adults_only")
        return profile

    async def is_subscribed(self, account_id: str) -> bool:
        subscription = await self.repo(Subscription).first(Subscription.account_id == account_id)
        return bool(subscription and subscription.status != "lapsed" and subscription.renews_at > self.now())

    async def has_subscriber_badge(self, account_id: str, profile: UserProfile | None = None) -> bool:
        """SB-02: subscribed AND ID-verified, so the badge always means a real, checked person."""
        profile = profile or await self.profile(account_id)
        return bool(profile and profile.id_verified and await self.is_subscribed(account_id))

    async def is_pro(self, account_id: str) -> bool:
        """SB-03: subscribed now, on the Pro tier."""
        subscription = await self.repo(Subscription).first(Subscription.account_id == account_id)
        return bool(subscription and subscription.tier == "pro" and await self.is_subscribed(account_id))

    async def require_pro(self, account: Account, feature: str) -> None:
        if not await self.is_pro(account.id):
            raise Forbidden(f"{feature} come with Pro", code="pro_required", hint="POST /subscriptions with tier pro")

    async def require_subscription(self, account: Account, feature: str) -> None:
        if not await self.is_subscribed(account.id):
            raise Forbidden(f"{feature} is a subscriber feature", code="subscription_required",
                            hint="POST /subscriptions")

    async def company_approved_for_minor(self, minor_id: str, company_id: str) -> bool:
        return await self.repo(GuardianCompanyApproval).exists(
            GuardianCompanyApproval.minor_account_id == minor_id,
            GuardianCompanyApproval.company_account_id == company_id)

    def is_blocked_category_for_minors(self, category: str | None) -> bool:
        """LK-09 blocked categories for minors."""
        normalized = (category or "").lower().replace(" ", "_").replace("-", "_")
        return any(blocked in normalized for blocked in self.s.minor_blocked_categories)


class AccountService(BaseService):
    async def me(self, account: Account) -> dict[str, Any]:
        profile = await self.svc.access.profile(account.id)
        company = await self.svc.access.company(account.id)
        data: dict[str, Any] = {
            "id": account.id, "type": account.type, "handle": account.handle, "email": account.email,
            "status": account.status, "phone": account.phone, "phone_verified": bool(account.phone_verified_at),
            # Why a suspended or frozen account is on hold, for the banner that explains it and the appeal.
            "status_reason": account.suspended_reason if account.status in ("suspended", "frozen") else None,
            "two_factor_enabled": account.totp_enabled, "feed_preference": account.feed_preference,
            "region": account.region, "admin_role": account.admin_role,
            "sponsored_cards": not account.promo_opt_out,
            "followers": {"human": account.followers_human, "agent": account.followers_agent},
            "following": account.following_count,
            "subscribed": await self.svc.access.is_subscribed(account.id),
            "subscriber_badge": await self.svc.access.has_subscriber_badge(account.id, profile),
        }
        if profile:
            data["profile"] = {
                "name": profile.name, "verified_name": profile.verified_name,
                "date_of_birth": profile.date_of_birth.isoformat(), "is_minor": profile.is_minor,
                "photo": profile.photo_ref, "bio": profile.bio, "location": profile.location,
                "primary_niche": profile.primary_niche, "secondary_niches": profile.secondary_niches,
                "links": profile.links, "id_verified": profile.id_verified,
                "verified_creator": profile.is_verified_creator, "verified_major_person": profile.is_major_person,
                "popularity_level": profile.popularity_level,
                "agent_comment_policy": profile.agent_comment_policy,
                "read_receipts": profile.read_receipts, "typing_indicators": profile.typing_indicators,
                # NT-01: the optional notification types and whether each is on; always-on ones can't be turned off.
                "notification_prefs": {t: (profile.notification_prefs or {}).get(t, True) for t in sorted(
                    {str(t) for t in NotificationType} - {str(t) for t in MANDATORY_NOTIFICATIONS})},
                "daily_digest": profile.daily_digest,
            }
        if company:
            data["company"] = {
                "business_name": company.business_name, "category": company.category, "verified": company.verified,
                "website": company.website,
                "verification_status": company.verification_status,
                "allow_user_messages": company.allow_user_messages, "b2b_opt_out": company.b2b_opt_out,
                "region_targeting": company.region_targeting,
            }
        return data

    async def update_profile(self, account: Account, changes: dict[str, Any]) -> dict[str, Any]:
        """ON-04 / SO-04. Niche changes only affect new pricing and warnings; contracts keep their terms."""
        profile = await self.svc.access.profile(account.id)
        if profile is None:
            raise Unprocessable("Only user accounts have a personal profile", code="not_a_user")
        if "name" in changes and changes["name"] is not None:
            if profile.id_verified and changes["name"].strip() != (profile.verified_name or ""):
                raise Forbidden("A verified name can only change through a new ID check",
                                code="verified_name_locked", hint="POST /verification/identity")
            profile.name = changes["name"].strip()
            account.display_name = profile.name
        secondary = changes.get("secondary_niches")
        if secondary is not None:
            secondary = [n.strip().lower() for n in secondary if n.strip()]
            if len(set(secondary)) > 2:
                raise Unprocessable("Choose up to 2 secondary niches", code="too_many_niches")
            profile.secondary_niches = list(dict.fromkeys(secondary))
        if changes.get("primary_niche") is not None:
            profile.primary_niche = changes["primary_niche"].strip().lower()
        if profile.primary_niche and profile.primary_niche in (profile.secondary_niches or []):
            raise Unprocessable("A secondary niche cannot repeat the primary niche", code="duplicate_niche")
        for field, column in (("photo", "photo_ref"), ("bio", "bio"), ("location", "location"), ("links", "links")):
            if changes.get(field) is not None:
                setattr(profile, column, changes[field])
        if "primary_niche" in changes or "secondary_niches" in changes:
            await self.cache.delete(f"interests:{account.id}")
        if changes.get("region") is not None:
            account.region = core_regions.require(changes["region"])
        await self.svc.profiles.invalidate(account)
        return await self.me(account)

    async def update_settings(self, account: Account, changes: dict[str, Any]) -> dict[str, Any]:
        if changes.get("feed_preference") is not None:
            account.feed_preference = FeedTab(changes["feed_preference"])  # FD-02: the choice is remembered
        if changes.get("sponsored_cards") is not None:
            account.promo_opt_out = not changes["sponsored_cards"]  # off = nothing sponsored on my posts or comments
        profile = await self.svc.access.profile(account.id)
        if profile:
            if changes.get("agent_comment_policy") is not None:
                profile.agent_comment_policy = AgentCommentPolicy(changes["agent_comment_policy"])  # FD-03
            for field in ("read_receipts", "typing_indicators"):
                if changes.get(field) is not None:
                    setattr(profile, field, bool(changes[field]))
        return await self.me(account)

    async def request_phone_verification(self, account: Account, phone: str) -> dict[str, Any]:
        phone = normalize_phone(phone)
        code = await self.svc.otp.issue("phone", f"{account.id}:{phone}")
        self.svc.notifications.sms(phone, f"Your Wamiwa code is {code}")
        await self.kv.set(f"phone_pending:{account.id}", phone, self.s.otp_ttl_seconds)
        return {"expires_in_seconds": self.s.otp_ttl_seconds}

    async def verify_phone(self, account: Account, code: str) -> dict[str, Any]:
        """ON-04: a number is only usable for payouts and recovery once verified by SMS OTP."""
        phone = await self.kv.get(f"phone_pending:{account.id}")
        if phone is None:
            raise Unprocessable("Request a code first", code="no_pending_phone")
        await self.svc.otp.verify("phone", f"{account.id}:{phone}", code)
        account.phone = phone
        account.phone_hash = self.infra.hasher.digest(phone)
        account.phone_verified_at = self.now()
        await self.kv.delete(f"phone_pending:{account.id}")
        await self.svc.notifications.notify(account.id, NotificationType.SECURITY, "Phone number verified")
        return {"phone": phone, "verified": True}

    async def touch(self, account: Account) -> None:
        """Record activity at most once every 10 minutes (feeds DAU/MAU and churn)."""
        if await self.kv.set_if_absent(f"active:{account.id}", "1", 600):
            account.last_active_at = self.now()

    async def flag_false_age(self, account: Account, reason: str) -> None:
        """ON-02: ID or a report shows a false age -> suspended pending review."""
        if account.status == AccountStatus.SUSPENDED:
            return
        account.status_before_hold = account.status
        account.status = AccountStatus.SUSPENDED
        account.suspended_reason, account.suspended_at = f"Age check: {reason}", self.now()
        await self.svc.queues.enqueue(Queue.IDENTITY, "false_age", account.id, subject_account_id=account.id,
                                      summary=reason, severity="high")
        await self.svc.notifications.notify(account.id, NotificationType.LEGAL, "Your account is under review",
                                            "We could not confirm your age. Our team will review your account.")

    async def convert_minors_turning_18(self) -> int:
        """Job (ON-02): convert to a full account, notify both sides; contracts keep their terms."""
        today = self.clock.today()
        converted = 0
        for profile in await self.repo(UserProfile).list(UserProfile.is_minor.is_(True)):
            if age_on(profile.date_of_birth, today) < self.s.adult_age:
                continue
            profile.is_minor, profile.converted_to_adult_at = False, self.now()
            account = await self.repo(Account).require(profile.account_id)
            if account.status == AccountStatus.PENDING_GUARDIAN:
                account.status = AccountStatus.ACTIVE
            for link in await self.repo(GuardianLink).list(GuardianLink.minor_account_id == account.id):
                if link.status == GuardianLinkStatus.ACTIVE and link.guardian_account_id:
                    await self.svc.notifications.notify(
                        link.guardian_account_id, NotificationType.LEGAL, f"@{account.handle} is now 18",
                        "Their account is now a full account. Active contracts finish under their original terms.")
                link.status = GuardianLinkStatus.ENDED
            await self.svc.notifications.notify(
                account.id, NotificationType.LEGAL, "Your account is now a full account",
                "You are 18. Active contracts finish under their original terms.")
            converted += 1
        return converted


class GuardianService(BaseService):
    async def invite(self, minor: Account, contact: str) -> dict[str, Any]:
        profile = await self.svc.access.profile(minor.id)
        if profile is None or not profile.is_minor:
            raise Unprocessable("Only 13-17 accounts link a guardian", code="not_a_minor")
        if await self.svc.access.guardian_link(minor.id):
            raise Conflict("A guardian is already linked", code="guardian_linked")
        await self.limiter.enforce(self.policies.guardian_invites, minor.id)
        is_email = "@" in contact
        contact = contact.strip().lower() if is_email else normalize_phone(contact)
        for stale in await self.repo(GuardianLink).list(
                GuardianLink.minor_account_id == minor.id, GuardianLink.status == GuardianLinkStatus.INVITED):
            stale.status = GuardianLinkStatus.ENDED
        token = SecretFactory.token()
        link = await self.repo(GuardianLink).add(GuardianLink(
            minor_account_id=minor.id, invite_contact=contact, invite_token_hash=self.infra.hasher.digest(token)))
        url = f"{self.s.public_base_url}/guardian/accept?token={token}"
        message = (f"{profile.name or minor.handle} asked you to be their parent or guardian on Wamiwa. "
                   f"Create or link a verified adult account: {url}")
        if is_email:
            self.svc.notifications.email(contact, "Guardian invite from Wamiwa", message)
        else:
            self.svc.notifications.sms(contact, message)
        return {"link_id": link.id, "status": link.status, "sent_to": contact}

    async def accept(self, guardian: Account, token: str, relationship: str, confirm_relationship: bool) -> dict[str, Any]:
        if not confirm_relationship:
            raise Unprocessable("Confirm that you are this child's parent or guardian", code="relationship_unconfirmed")
        await self.svc.access.require_adult(guardian, "be a guardian")
        link = await self.repo(GuardianLink).first(
            GuardianLink.invite_token_hash == self.infra.hasher.digest(token),
            GuardianLink.status == GuardianLinkStatus.INVITED)
        if link is None:
            raise NotFound("This invite is invalid or has already been used", code="invalid_invite")
        if link.minor_account_id == guardian.id:
            raise Unprocessable("You cannot be your own guardian", code="self_guardian")
        link.guardian_account_id, link.relationship = guardian.id, relationship
        link.status = GuardianLinkStatus.PENDING_VERIFICATION
        if await self.svc.access.is_id_verified(guardian):
            await self._activate(link)
        return {"link_id": link.id, "status": link.status,
                "next_step": None if link.status == GuardianLinkStatus.ACTIVE else "verify_identity"}

    async def _activate(self, link: GuardianLink) -> None:
        link.status, link.verified_at = GuardianLinkStatus.ACTIVE, self.now()
        minor = await self.repo(Account).require(link.minor_account_id)
        if minor.status == AccountStatus.PENDING_GUARDIAN:
            minor.status = AccountStatus.ACTIVE
            await self.svc.welcome.send(minor, minor=True)
        await self.svc.notifications.notify(minor.id, NotificationType.SECURITY, "Your guardian is linked",
                                            "Your account is ready to use.")

    async def on_guardian_verified(self, guardian: Account) -> None:
        """Called by identity verification: finish any links waiting on this guardian's ID."""
        for link in await self.repo(GuardianLink).list(
                GuardianLink.guardian_account_id == guardian.id,
                GuardianLink.status == GuardianLinkStatus.PENDING_VERIFICATION):
            await self._activate(link)

    async def minors(self, guardian: Account) -> list[dict[str, Any]]:
        links = await self.repo(GuardianLink).list(
            GuardianLink.guardian_account_id == guardian.id,
            GuardianLink.status.in_([GuardianLinkStatus.ACTIVE, GuardianLinkStatus.PENDING_VERIFICATION]))
        accounts = await self.repo(Account).by_ids([link.minor_account_id for link in links])
        return [
            {"minor_id": link.minor_account_id, "handle": accounts[link.minor_account_id].handle,
             "name": accounts[link.minor_account_id].display_name, "link_status": link.status,
             "account_status": accounts[link.minor_account_id].status,
             "frozen": accounts[link.minor_account_id].status == AccountStatus.FROZEN}
            for link in links if link.minor_account_id in accounts
        ]

    async def set_frozen(self, guardian: Account, minor_id: str, frozen: bool) -> dict[str, Any]:
        """ON-03: the guardian can freeze the minor's account."""
        await self.svc.access.require_guardian_of(guardian.id, minor_id)
        minor = await self.repo(Account).require(minor_id)
        if frozen and minor.status == AccountStatus.ACTIVE:
            minor.status_before_hold, minor.status = minor.status, AccountStatus.FROZEN
            minor.suspended_reason = "Frozen by guardian"
        elif not frozen and minor.status == AccountStatus.FROZEN and minor.suspended_reason == "Frozen by guardian":
            minor.status, minor.suspended_reason = AccountStatus.ACTIVE, None
        await self.svc.notifications.notify(
            minor.id, NotificationType.SECURITY,
            "Your guardian froze your account" if frozen else "Your guardian unfroze your account")
        return {"minor_id": minor.id, "status": minor.status}

    async def approve_company(self, guardian: Account, minor_id: str, company_id: str) -> dict[str, Any]:
        """HM-02 / MS-01: a company may DM a minor only after the guardian approves that company."""
        await self.svc.access.require_guardian_of(guardian.id, minor_id)
        company = await self.svc.access.company(company_id)
        if company is None or not company.verified:
            raise Unprocessable("Only verified companies can be approved", code="company_not_verified")
        if self.svc.access.is_blocked_category_for_minors(company.category):
            raise Forbidden("Companies in this category cannot work with minors", code="blocked_category")
        if not await self.svc.access.company_approved_for_minor(minor_id, company_id):
            await self.repo(GuardianCompanyApproval).add(GuardianCompanyApproval(
                guardian_account_id=guardian.id, minor_account_id=minor_id, company_account_id=company_id))
        return {"minor_id": minor_id, "company_id": company_id, "approved": True}

    async def revoke_company(self, guardian: Account, minor_id: str, company_id: str) -> None:
        await self.svc.access.require_guardian_of(guardian.id, minor_id)
        approval = await self.repo(GuardianCompanyApproval).first(
            GuardianCompanyApproval.minor_account_id == minor_id,
            GuardianCompanyApproval.company_account_id == company_id)
        if approval:
            await self.repo(GuardianCompanyApproval).delete(approval)

    async def approved_companies(self, guardian: Account, minor_id: str) -> list[dict[str, Any]]:
        await self.svc.access.require_guardian_of(guardian.id, minor_id)
        approvals = await self.repo(GuardianCompanyApproval).list(GuardianCompanyApproval.minor_account_id == minor_id)
        accounts = await self.repo(Account).by_ids([a.company_account_id for a in approvals])
        return [{"company_id": a.company_account_id,
                 "handle": accounts[a.company_account_id].handle if a.company_account_id in accounts else None,
                 "approved_at": a.created_at.isoformat()} for a in approvals]
