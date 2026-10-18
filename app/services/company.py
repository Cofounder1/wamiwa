"""Epic 7: company verification and settings, B2B discovery, API keys and the company API."""
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.core.enums import (
    AccountStatus, AccountType, Label, ModerationStatus, NotificationType, Permission, Queue,
    VerificationStatus,
)
from app.core.exceptions import Conflict, Forbidden, NotFound, Unauthorized, Unprocessable
from app.core.security import SecretFactory
from app.core.utils import Cursor, Page
from app.core import regions as core_regions
from app.models import (
    Account, ApiKey, B2BCategoryLink, Comment, CompanyProfile, Contract, Post, QueueItem, UserProfile,
)
from app.services.base import BaseService, Principal


def normalize_category(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


COMPANY_SUFFIXES = {"ltd", "limited", "plc", "inc", "llc", "co", "company", "nig", "nigeria"}


def registration_key(value: str) -> tuple[str, str]:
    """("RC", "123456") for "RC 123456", "rc-123456" or "RC123456"; ("", "123456") when no prefix is given.
    RC (companies) and BN (business names) are different registries, so the prefix is kept."""
    compact = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
    match = re.fullmatch(r"([A-Z]*)(\d+)", compact)
    return (match.group(1), match.group(2)) if match else ("", compact)


def same_registration(a: str, b: str) -> bool:
    (pa, da), (pb, db) = registration_key(a), registration_key(b)
    return bool(da) and da == db and (pa == pb or not pa or not pb)


def name_key(value: str | None) -> str:
    words = re.findall(r"[a-z0-9]+", (value or "").lower())
    return " ".join(w for w in words if w not in COMPANY_SUFFIXES)


class CompanyService(BaseService):
    async def _company(self, account: Account) -> CompanyProfile:
        company = await self.svc.access.company(account.id) if account.is_company else None
        if company is None:
            raise Forbidden("This is for company accounts", code="company_required")
        return company

    async def submit_verification(self, account: Account, registration_no: str, document_ref: str,
                                  category: str | None) -> dict[str, Any]:
        """CO-01: business registration number or equivalent document, reviewed by an admin."""
        company = await self._company(account)
        if company.verified:
            raise Conflict("Your business is already verified", code="already_verified")
        if company.verification_status == VerificationStatus.PENDING:
            raise Conflict("Your documents are already under review", code="review_pending")
        await self._check_not_registered(account, company, registration_no)
        company.registration_no, company.document_ref = registration_no.strip(), document_ref
        if category:
            company.category = normalize_category(category)
        company.verification_status = VerificationStatus.PENDING
        await self.svc.queues.enqueue(Queue.COMPANY, "company", account.id, subject_account_id=account.id,
                                      summary=f"{company.business_name} ({registration_no})")
        return {"verification_status": company.verification_status, "verified": False}

    async def _check_not_registered(self, account: Account, company: CompanyProfile, registration_no: str) -> None:
        """CO-01: one business, one account. A registration number already verified or under review on another
        account is refused; support can sort out genuine cases (a lost login, a new owner)."""
        _, digits = registration_key(registration_no)
        if not digits:
            raise Unprocessable("Enter the registration number, e.g. RC 123456", code="invalid_registration_no")
        rows = (await self.db.execute(
            select(CompanyProfile, Account).join(Account, Account.id == CompanyProfile.account_id).where(
                CompanyProfile.account_id != account.id, CompanyProfile.registration_no.is_not(None),
                CompanyProfile.registration_no.contains(digits),
                CompanyProfile.verification_status.in_([VerificationStatus.PENDING, VerificationStatus.PASSED]),
                Account.status != AccountStatus.REMOVED))).all()
        for other, _ in rows:
            if not same_registration(other.registration_no, registration_no):
                continue
            if name_key(other.business_name) == name_key(company.business_name):
                raise Conflict("This business is already registered on Wamiwa. If you think this is a mistake, "
                               "contact support.", code="company_already_registered", hint="POST /support/tickets")
            raise Conflict("This registration number is already used by another business on Wamiwa. If it's yours, "
                           "contact support.", code="registration_number_in_use", hint="POST /support/tickets")

    async def apply_review(self, item: QueueItem, approved: bool, admin: Account, reason: str, payload: dict) -> None:
        account = await self.repo(Account).require(item.target_id)
        company = await self.svc.access.company(account.id)
        company.verified = approved
        company.verification_status = VerificationStatus.PASSED if approved else VerificationStatus.REJECTED
        await self.svc.bank_accounts.recheck_name(account)
        await self.svc.profiles.invalidate(account)
        await self.cache.invalidate_prefix("b2b:")
        await self.svc.notifications.notify(
            account.id, NotificationType.SECURITY,
            "Your business is verified" if approved else "Business verification was not approved", reason)

    async def update_settings(self, account: Account, changes: dict[str, Any]) -> dict[str, Any]:
        """CO-04 and MS-01 settings: region targeting, allow user messages, opt out of buyer lists."""
        company = await self._company(account)
        for field in ("allow_user_messages", "b2b_opt_out"):
            if changes.get(field) is not None:
                setattr(company, field, bool(changes[field]))
        if changes.get("region_targeting") is not None:
            company.region_targeting = core_regions.require(changes["region_targeting"])
        if changes.get("bio") is not None:
            company.bio = changes["bio"]
        if changes.get("website") is not None:
            company.website = await self.svc.promos.vet_website(account, changes["website"]) if changes["website"] else None
        if changes.get("category") is not None:
            company.category = normalize_category(changes["category"])
        await self.svc.profiles.invalidate(account)
        await self.cache.invalidate_prefix("b2b:")
        return await self.svc.accounts.me(account)


class B2BService(BaseService):
    """CO-03: likely buyers come from a category map maintained by admins."""

    async def category_map(self) -> dict[str, list[str]]:
        async def load() -> dict[str, list[str]]:
            mapping: dict[str, list[str]] = {}
            for link in await self.repo(B2BCategoryLink).list():
                mapping.setdefault(link.seller_category, []).append(link.buyer_category)
            return mapping

        return await self.cache.get_or_set("b2b:map", self.s.cache_ttl_category_map, load) or {}

    async def set_buyers(self, admin: Account, seller_category: str, buyer_categories: list[str], reason: str) -> dict:
        """e.g. farming -> [fertilizer]; used_cars -> [spare_parts, drivers]."""
        await self.svc.admin.require_permission(admin, Permission.B2B_CATEGORY_MAP)
        seller = normalize_category(seller_category)
        buyers = sorted({normalize_category(b) for b in buyer_categories if b.strip()})
        for link in await self.repo(B2BCategoryLink).list(B2BCategoryLink.seller_category == seller):
            await self.repo(B2BCategoryLink).delete(link)
        for buyer in buyers:
            await self.repo(B2BCategoryLink).add(B2BCategoryLink(
                seller_category=seller, buyer_category=buyer, updated_by=admin.id))
        await self.svc.audit.log(admin.id, "b2b.category_map", "category", seller[:32], reason, buyers=buyers)
        await self.cache.invalidate_prefix("b2b:")
        return {seller: buyers}

    async def likely_buyers(self, account: Account, q: str | None, limit: int) -> list[dict[str, Any]]:
        """Also backs the Find box: typing @ lists likely buyer companies (optionally narrowed by text)."""
        company = await self.svc.access.require_verified_company(account, "use B2B discovery")
        categories = (await self.category_map()).get(company.category or "", [])
        if not categories:
            return []
        stmt = (select(Account, CompanyProfile).join(CompanyProfile, CompanyProfile.account_id == Account.id)
                .where(Account.type == AccountType.COMPANY, Account.status == AccountStatus.ACTIVE,
                       Account.id != account.id, CompanyProfile.verified.is_(True),
                       CompanyProfile.b2b_opt_out.is_(False),  # companies can opt out of buyer lists
                       CompanyProfile.category.in_(categories)))
        if q and q.lstrip("@").strip():
            pattern = f"%{q.lstrip('@').strip().lower()}%"
            stmt = stmt.where(CompanyProfile.business_name.ilike(pattern) | Account.handle.ilike(pattern))
        hidden = await self.svc.blocks.hidden_ids(account.id)
        rows = (await self.db.execute(stmt.order_by(Account.id.desc()).limit(max(1, min(limit, 50))))).all()
        return [{"id": a.id, "handle": a.handle, "name": c.business_name, "category": c.category}
                for a, c in rows if a.id not in hidden]

    async def feed(self, account: Account, cursor: str | None, limit: int) -> Page[dict]:
        """Posts from companies whose likely buyers include this company's category.
        Competitors only ever see each other's public posts here, never hires or prices."""
        company = await self.svc.access.require_verified_company(account, "use B2B discovery")
        sellers = [seller for seller, buyers in (await self.category_map()).items() if company.category in buyers]
        if not sellers or company.b2b_opt_out:
            return Page(items=[])
        limit = max(1, min(limit, 50))
        stmt = (select(Post, Account).join(Account, Account.id == Post.author_account_id)
                .join(CompanyProfile, CompanyProfile.account_id == Account.id)
                .where(CompanyProfile.category.in_(sellers), CompanyProfile.verified.is_(True),
                       Account.status == AccountStatus.ACTIVE, Post.moderation_status == ModerationStatus.VISIBLE))
        last_id = Cursor.decode(cursor)
        if last_id:
            stmt = stmt.where(Post.id < last_id)
        hidden = await self.svc.blocks.hidden_ids(account.id)
        rows = (await self.db.execute(stmt.order_by(Post.id.desc()).limit(limit + 1))).all()
        items = [self.svc.posts.present(p, a) for p, a in rows[:limit] if a.id not in hidden]
        return Page(items=items, next_cursor=Cursor.encode(rows[limit - 1][0].id) if len(rows) > limit else None)

    async def guard_new_conversation(self, sender: Account) -> None:
        """Max 20 new B2B conversations per day; 3 spam reports remove B2B DM access for 30 days."""
        company = await self.svc.access.require_verified_company(sender, "message companies")
        if company.b2b_banned_until and company.b2b_banned_until > self.now():
            raise Forbidden("B2B messaging is paused on your account after spam reports", code="b2b_dm_removed",
                            until=company.b2b_banned_until.isoformat())
        await self.limiter.enforce(self.policies.b2b_conversations, sender.id,
                                   "You can start 20 new B2B conversations per day")

    async def register_spam_report(self, company_account_id: str) -> None:
        company = await self.svc.access.company(company_account_id)
        if company is None:
            return
        company.b2b_spam_reports += 1
        if company.b2b_spam_reports >= self.s.b2b_spam_reports_for_ban:
            company.b2b_spam_reports = 0
            company.b2b_banned_until = self.now() + timedelta(days=self.s.b2b_ban_days)
            await self.svc.notifications.notify(
                company_account_id, NotificationType.LEGAL, "B2B messaging removed for 30 days",
                "Your company received 3 spam reports.")


class ApiKeyService(BaseService):
    def present(self, key: ApiKey) -> dict[str, Any]:
        return {"id": key.id, "name": key.name, "prefix": key.key_prefix, "created_at": key.created_at.isoformat(),
                "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
                "revoked": key.revoked_at is not None,
                "limits": {"posts_per_hour": self.s.api_posts_per_hour, "replies_per_hour": self.s.api_replies_per_hour}}

    async def create(self, account: Account, name: str) -> dict[str, Any]:
        await self.svc.access.require_verified_company(account, "use APIs")
        active = await self.repo(ApiKey).count(ApiKey.company_account_id == account.id, ApiKey.revoked_at.is_(None))
        if active >= 10:
            raise Conflict("Revoke an existing key before creating another", code="too_many_keys")
        raw = SecretFactory.company_key()
        key = await self.repo(ApiKey).add(ApiKey(
            company_account_id=account.id, name=name.strip(), key_hash=self.infra.hasher.digest(raw), key_prefix=raw[:10]))
        return {**self.present(key), "api_key": raw, "notice": "Store this key now. It is shown once."}

    async def list_keys(self, account: Account) -> list[dict[str, Any]]:
        keys = await self.repo(ApiKey).list(ApiKey.company_account_id == account.id, order_by=ApiKey.id.desc())
        return [self.present(k) for k in keys]

    async def revoke(self, account: Account, key_id: str) -> dict[str, Any]:
        """Revoking a key fails all its calls immediately: the cached lookup is dropped in the same step."""
        key = await self.repo(ApiKey).require(key_id, "API key not found")
        if key.company_account_id != account.id:
            raise NotFound("API key not found")
        key.revoked_at = key.revoked_at or self.now()
        await self.cache.delete(f"apikey:{key.key_hash}")
        cache, key_hash = self.cache, key.key_hash

        async def drop_again() -> None:  # closes the race with a lookup that was cached mid-transaction
            await cache.delete(f"apikey:{key_hash}")

        self.uow.on_commit(drop_again)
        return self.present(key)

    async def resolve_key(self, raw_key: str) -> Principal:
        if not raw_key.startswith(SecretFactory.COMPANY_KEY_PREFIX):
            raise Unauthorized("Invalid API key", code="invalid_api_key")
        key_hash = self.infra.hasher.digest(raw_key)

        async def load() -> dict[str, str] | None:
            row = await self.repo(ApiKey).first(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
            return {"id": row.id, "company": row.company_account_id} if row else None

        found = await self.cache.get_or_set(f"apikey:{key_hash}", self.s.cache_ttl_api_key, load)
        if not found:
            raise Unauthorized("Invalid or revoked API key", code="invalid_api_key")
        account = await self.repo(Account).get(found["company"])
        if account is None or account.status != AccountStatus.ACTIVE:
            raise Forbidden("This company account is not active", code="account_unavailable")
        await self.svc.access.require_verified_company(account, "use APIs")
        if await self.kv.set_if_absent(f"apikey_used:{found['id']}", "1", 300):
            key = await self.repo(ApiKey).get(found["id"])
            if key:
                key.last_used_at = self.now()
        return Principal(account=account, kind="company_key", api_key_id=found["id"])


class CompanyApiService(BaseService):
    """API-02 reading comments and API-03 sentiment, on the company's posts and posts made on its behalf."""

    async def _own_post(self, company_id: str, post_id: str) -> Post:
        post = await self.repo(Post).get(post_id)
        owns = bool(post) and post.author_account_id == company_id
        if post and not owns and post.sponsored_contract_id:
            contract = await self.repo(Contract).get(post.sponsored_contract_id)
            owns = bool(contract and contract.hirer_id == company_id)
        if not owns:
            raise NotFound("Post not found")
        return post

    async def _audience_filter(self, company: Account) -> list:
        """Companies in categories restricted for minors (alcohol, betting, loans...) never receive
        comments written by 13-17 accounts. Every other company receives all comments."""
        profile = await self.svc.access.company(company.id)
        if profile and self.svc.access.is_blocked_category_for_minors(profile.category):
            return [Comment.author_account_id.not_in(
                select(UserProfile.account_id).where(UserProfile.is_minor.is_(True)))]
        return []

    async def comments(self, company: Account, post_id: str, cursor: str | None, limit: int) -> Page[dict]:
        """Vulgar incoming comments are flagged to the company, never auto-deleted."""
        await self._own_post(company.id, post_id)
        page = await self.repo(Comment).page(
            Comment.post_id == post_id, Comment.moderation_status == ModerationStatus.VISIBLE,
            Comment.deleted_at.is_(None), *await self._audience_filter(company), cursor=cursor, limit=limit)
        authors = await self.repo(Account).by_ids([c.author_account_id for c in page.items])
        items = [{
            "id": c.id, "parent_id": c.parent_id, "label": c.label, "flagged_vulgar": c.flagged_vulgar,
            "text": self.svc.webhooks.untrusted(c.text),
            "author": {"id": c.author_account_id, "handle": authors[c.author_account_id].handle
                       if c.author_account_id in authors else None},
            "created_at": c.created_at.isoformat()} for c in page.items]
        return Page(items=items, next_cursor=page.next_cursor)

    async def sentiment(self, company: Account, post_id: str) -> dict[str, Any]:
        """Counts only human comments. Low-confidence results are marked "uncertain"."""
        await self.svc.access.require_verified_company(company, "use sentiment analysis")
        await self._own_post(company.id, post_id)
        audience = await self._audience_filter(company)

        async def load() -> dict[str, Any]:
            comments = await self.repo(Comment).list(
                Comment.post_id == post_id, Comment.label == Label.HUMAN, Comment.deleted_at.is_(None),
                Comment.moderation_status == ModerationStatus.VISIBLE, *audience, limit=2000)
            totals = {"positive": 0, "negative": 0, "neutral": 0, "uncertain": 0}
            languages: dict[str, int] = {}
            for comment in comments:
                result = await self.providers.moderation.sentiment(comment.text)
                totals[result.label] = totals.get(result.label, 0) + 1
                languages[result.language] = languages.get(result.language, 0) + 1
            return {"post_id": post_id, "human_comments_analysed": len(comments), "sentiment": totals,
                    "languages": languages, "supported_languages": ["en", "pcm", "yo", "ig", "ha"],
                    "generated_at": self.now().isoformat()}

        return await self.cache.get_or_set(f"sentiment:{post_id}:{company.id}", self.s.cache_ttl_sentiment, load)
