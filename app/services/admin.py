"""Epic 9: admin management, permissions, account oversight and the platform analytics dashboard."""
import csv
import hashlib
import io
import json
import statistics
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select

from app.core.enums import (
    AccountStatus, AccountType, AdminRole, ContractStatus, ContractType, EscrowState, Label,
    NotificationType, PaymentPurpose, Permission, PostType, ReportReason, ReportStatus,
    SubscriptionStatus, VerificationStatus,
)
from app.core.exceptions import Conflict, Forbidden, NotFound, Unprocessable
from app.core.security import SecretFactory
from app.core.utils import Page, normalize_handle
from app.models import (
    Account, AdminInvite, AdminPermission, AgentProfile, AgentTag, AuditLog, Comment, CompanyProfile,
    Contract, Dispute, EscrowTransaction, GuardianConfirmation, IdentityVerification, Payment, Post,
    PublicFigureVerification, Report, Subscription, UserProfile,
)
from app.services.base import BaseService

# Section 1 success metrics: dashboard key -> target by month 6.
TARGETS = {
    "marketplace.completed_contracts": 500,
    "revenue.gross_escrow_value_usd": 100_000,
    "revenue.paying_subscribers": 2_000,
    "marketplace.verified_influencers_with_paid_contract_pct": 25,
    "marketplace.repeat_brand_rate_pct": 40,
    "marketplace.median_days_to_active": 3,
    "trust_and_safety.dispute_rate_pct": 5,
    "trust_and_safety.misuse_takedown_rate_pct": 1,
    "agents.helpful_rating_pct": 60,
    "growth.mau": 50_000,
}
PEOPLE = (AccountType.USER, AccountType.COMPANY)


class AdminService(BaseService):
    # --- permissions -----------------------------------------------------------------------------
    async def permissions(self, admin: Account) -> dict[str, Any]:
        if admin.is_super_admin:
            return {str(p): True for p in Permission}
        grants = await self.repo(AdminPermission).list(AdminPermission.admin_id == admin.id)
        granted = {g.permission: g for g in grants}
        return {str(p): str(p) in granted for p in Permission} | {
            "payout_limit": granted[Permission.APPROVE_PAYOUTS].payout_limit if Permission.APPROVE_PAYOUTS in granted else None}

    async def has_permission(self, admin: Account, permission: Permission) -> bool:
        if not admin.is_admin:
            return False
        if admin.is_super_admin:
            return True
        return await self.repo(AdminPermission).exists(
            AdminPermission.admin_id == admin.id, AdminPermission.permission == permission)

    async def require_permission(self, admin: Account, permission: Permission) -> None:
        if not await self.has_permission(admin, permission):
            raise Forbidden(f"You have not been granted the '{permission}' permission", code="permission_denied")

    @staticmethod
    def require_super(admin: Account) -> None:
        if not admin.is_super_admin:
            raise Forbidden("Only a Super Admin can do this", code="super_admin_required")

    # --- AD-01 manage admins ------------------------------------------------------------------------
    async def bootstrap_super_admin(self, email: str, password: str, handle: str) -> dict[str, str]:
        """CLI only: create a Super Admin. Run twice; the platform needs at least two."""
        email = email.lower()
        if await self.repo(Account).exists(Account.email == email):
            raise Conflict("This email is already registered", code="email_registered")
        secret = self.infra.totp.generate_secret()
        account = await self.repo(Account).add(Account(
            type=AccountType.ADMIN, admin_role=AdminRole.SUPER_ADMIN, email=email, handle=normalize_handle(handle),
            display_name=handle, password_hash=self.infra.passwords.hash(password), totp_secret=secret,
            totp_enabled=True))
        return {"id": account.id, "totp_secret": secret,
                "otpauth_url": self.infra.totp.provisioning_uri(secret, email)}

    async def invite(self, admin: Account, email: str, role: AdminRole, reason: str) -> dict[str, Any]:
        """New admins get a one-time link by email. Credentials are never emailed."""
        self.require_super(admin)
        email = email.lower()
        if await self.repo(Account).exists(Account.email == email):
            raise Conflict("This email already has an account", code="email_registered")
        token = SecretFactory.token()
        invite = await self.repo(AdminInvite).add(AdminInvite(
            email=email, role=role, token_hash=self.infra.hasher.digest(token), invited_by=admin.id,
            expires_at=self.now() + timedelta(hours=self.s.admin_invite_ttl_hours)))
        await self.svc.audit.log(admin.id, "admin.invite", "admin_invite", invite.id, reason, email=email, role=role)
        self.svc.notifications.email(
            email, "You have been invited to Wamiwa admin",
            f"Set up your account with this one-time link: {self.s.public_base_url}/admin/invite?token={token}")
        return {"invite_id": invite.id, "email": email, "role": role, "expires_at": invite.expires_at.isoformat()}

    async def accept_invite(self, token: str, password: str, handle: str, name: str) -> dict[str, Any]:
        invite = await self.repo(AdminInvite).first(AdminInvite.token_hash == self.infra.hasher.digest(token))
        if invite is None or invite.used_at is not None or invite.expires_at <= self.now():
            raise NotFound("This invite link is invalid, used or expired", code="invalid_invite")
        handle = normalize_handle(handle)
        if await self.repo(Account).exists(or_(Account.handle == handle, Account.email == invite.email)):
            raise Conflict("That handle or email is taken", code="handle_taken")
        invite.used_at = self.now()
        secret = self.infra.totp.generate_secret()
        account = await self.repo(Account).add(Account(
            type=AccountType.ADMIN, admin_role=invite.role, email=invite.email, handle=handle,
            display_name=name.strip(), password_hash=self.infra.passwords.hash(password), totp_secret=secret))
        setup_token = self.infra.tokens.issue("admin_2fa", account.id, 30 * 60)
        return {"setup_token": setup_token, "totp_secret": secret,
                "otpauth_url": self.infra.totp.provisioning_uri(secret, invite.email),
                "next_step": "Confirm a code from your authenticator app to finish. Two-factor login is required."}

    async def confirm_invite_2fa(self, setup_token: str, code: str) -> dict[str, Any]:
        account = await self.repo(Account).require(self.infra.tokens.decode(setup_token, "admin_2fa")["sub"])
        if not self.infra.totp.verify(account.totp_secret, code):
            raise Unprocessable("Incorrect two-factor code", code="2fa_invalid")
        account.totp_enabled = True
        return {"ready": True, "login": "POST /auth/login with your email, password and a two-factor code"}

    async def list_admins(self, admin: Account) -> list[dict[str, Any]]:
        self.require_super(admin)
        admins = await self.repo(Account).list(Account.type == AccountType.ADMIN)
        grants = await self.repo(AdminPermission).list()
        return [{"id": a.id, "email": a.email, "handle": a.handle, "role": a.admin_role, "status": a.status,
                 "two_factor_enabled": a.totp_enabled,
                 "permissions": [{"permission": g.permission, "payout_limit": g.payout_limit}
                                 for g in grants if g.admin_id == a.id]} for a in admins]

    async def _admin_target(self, actor: Account, admin_id: str) -> Account:
        self.require_super(actor)
        target = await self.repo(Account).get(admin_id)
        if target is None or not target.is_admin or target.status == AccountStatus.REMOVED:
            raise NotFound("Admin not found")
        if target.id == actor.id:
            raise Forbidden("Admins cannot act on their own account", code="self_action")
        return target

    async def remove_admin(self, actor: Account, admin_id: str, reason: str) -> dict[str, Any]:
        """There must always be at least two Super Admins; the last two cannot be removed."""
        target = await self._admin_target(actor, admin_id)
        if target.is_super_admin:
            supers = await self.repo(Account).count(
                Account.type == AccountType.ADMIN, Account.admin_role == AdminRole.SUPER_ADMIN,
                Account.status == AccountStatus.ACTIVE)
            if supers <= self.s.min_super_admins:
                raise Conflict("There must always be at least two Super Admins", code="min_super_admins")
        await self.svc.audit.log(actor.id, "admin.remove", "account", target.id, reason)
        target.status, target.password_hash, target.totp_enabled = AccountStatus.REMOVED, None, False
        for grant in await self.repo(AdminPermission).list(AdminPermission.admin_id == target.id):
            await self.repo(AdminPermission).delete(grant)
        return {"id": target.id, "status": target.status}

    async def grant(self, actor: Account, admin_id: str, permission: Permission, payout_limit: int | None,
                    reason: str) -> dict[str, Any]:
        """Admins get only the permissions a Super Admin grants them."""
        target = await self._admin_target(actor, admin_id)
        if permission == Permission.APPROVE_PAYOUTS and (payout_limit is None or payout_limit <= 0):
            raise Unprocessable("Payout approval needs a limit", code="payout_limit_required")
        grant = await self.repo(AdminPermission).first(
            AdminPermission.admin_id == target.id, AdminPermission.permission == permission)
        if grant is None:
            grant = await self.repo(AdminPermission).add(AdminPermission(
                admin_id=target.id, permission=permission, granted_by=actor.id))
        grant.payout_limit = payout_limit if permission == Permission.APPROVE_PAYOUTS else None
        grant.granted_by = actor.id
        await self.svc.audit.log(actor.id, "admin.grant", "account", target.id, reason,
                                 permission=permission, payout_limit=payout_limit)
        return {"admin_id": target.id, "permission": permission, "payout_limit": grant.payout_limit}

    async def revoke(self, actor: Account, admin_id: str, permission: Permission, reason: str) -> None:
        target = await self._admin_target(actor, admin_id)
        grant = await self.repo(AdminPermission).first(
            AdminPermission.admin_id == target.id, AdminPermission.permission == permission)
        if grant:
            await self.repo(AdminPermission).delete(grant)
            await self.svc.audit.log(actor.id, "admin.revoke", "account", target.id, reason, permission=permission)

    async def alert_admins(self, title: str, body: str, data: dict | None = None) -> None:
        """Tell every active Admin and Super Admin."""
        for admin in await self.repo(Account).list(Account.type == AccountType.ADMIN, Account.status == AccountStatus.ACTIVE):
            await self.svc.notifications.notify(admin.id, NotificationType.ADMIN, title, body, data)

    async def alert_super_admins(self, title: str, body: str) -> None:
        supers = await self.repo(Account).list(
            Account.type == AccountType.ADMIN, Account.admin_role == AdminRole.SUPER_ADMIN,
            Account.status == AccountStatus.ACTIVE)
        for admin in supers:
            await self.svc.notifications.notify(admin.id, NotificationType.LEGAL, title, body)

    # --- oversight ---------------------------------------------------------------------------------
    async def accounts(self, *, type: AccountType | None, status: str | None, q: str | None, cursor: str | None,
                       limit: int) -> Page[dict]:
        where = [Account.type != AccountType.ADMIN]
        if type:
            where.append(Account.type == type)
        if status:
            where.append(Account.status == status)
        if q:
            pattern = f"%{q.lower()}%"
            where.append(or_(Account.handle.ilike(pattern), Account.email.ilike(pattern),
                             Account.display_name.ilike(pattern)))
        page = await self.repo(Account).page(*where, cursor=cursor, limit=limit)
        return Page(items=[{"id": a.id, "type": a.type, "handle": a.handle, "email": a.email, "name": a.display_name,
                            "status": a.status, "created_at": a.created_at.isoformat()} for a in page.items],
                    next_cursor=page.next_cursor)

    async def total_user_count(self) -> dict[str, int]:
        """Each person is counted once: one login covers every role they hold (guardian, agent owner...)."""
        gone = (AccountStatus.DELETED, AccountStatus.REMOVED)
        rows = (await self.db.execute(
            select(Account.type, func.count()).where(Account.status.not_in(gone)).group_by(Account.type))).all()
        counts = {str(t): int(n) for t, n in rows}
        return {"total_people": counts.get("user", 0) + counts.get("company", 0), "users": counts.get("user", 0),
                "companies": counts.get("company", 0), "agents": counts.get("agent", 0)}

    async def account_detail(self, account_id: str) -> dict[str, Any]:
        """AQ-02 context: account history, prior reports and contracts."""
        account = await self.repo(Account).require(account_id, "Account not found")
        reports = await self.repo(Report).list(Report.target_account_id == account.id, order_by=Report.id.desc(), limit=50)
        history = await self.repo(AuditLog).list(AuditLog.target_id == account.id, order_by=AuditLog.id.desc(), limit=50)
        contracts = await self.repo(Contract).count((Contract.hirer_id == account.id) | (Contract.earner_id == account.id))
        return {
            "account": await self.svc.accounts.me(account), "suspended_reason": account.suspended_reason,
            "warnings": account.warnings, "false_reports": account.false_reports, "contracts": contracts,
            "reports_against": [{"id": r.id, "reason": r.reason, "status": r.status, "details": r.details,
                                 "created_at": r.created_at.isoformat()} for r in reports],
            "admin_history": [{"action": h.action, "actor_id": h.actor_id, "reason": h.reason,
                               "at": h.created_at.isoformat()} for h in history],
        }

    async def agent_content_stats(self) -> dict[str, Any]:
        """Agent content: posts count, video hours and text post count."""
        rows = (await self.db.execute(
            select(Post.type, func.count(), func.coalesce(func.sum(Post.duration_seconds), 0))
            .where(Post.label == Label.AGENT).group_by(Post.type))).all()
        by_type = {str(t): (int(n), int(seconds)) for t, n, seconds in rows}
        return {"posts": sum(n for n, _ in by_type.values()),
                "video_hours": round(by_type.get(PostType.VIDEO, (0, 0))[1] / 3600, 2),
                "text_posts": by_type.get(PostType.TEXT, (0, 0))[0]}

    async def minor_contracts(self, cursor: str | None, limit: int) -> Page[dict]:
        """Minor contracts with their confirmation-call recordings."""
        page = await self.repo(GuardianConfirmation).page(cursor=cursor, limit=limit)
        return Page(items=[{
            "contract_id": c.contract_id, "minor_id": c.minor_id, "guardian_id": c.guardian_id, "result": c.result,
            "call_attempts": c.call_attempts, "recording_ref": c.recording_ref,
            "decided_at": c.decided_at.isoformat() if c.decided_at else None} for c in page.items],
            next_cursor=page.next_cursor)


class AdminAnalyticsService(BaseService):
    """AD-05 dashboard. Results are cached for a few minutes: the queries scan wide ranges."""

    async def dashboard(self, admin: Account, *, start: datetime, end: datetime, role: str | None,
                        niche: str | None, region: str | None, compare: bool = True) -> dict[str, Any]:
        admin_service = self.svc.admin
        if not await admin_service.has_permission(admin, Permission.VIEW_DASHBOARD):
            raise Forbidden("You have not been granted the dashboard", code="permission_denied")
        if end <= start:
            raise Unprocessable("The end of the range must be after its start", code="invalid_range")
        fingerprint = hashlib.sha256(json.dumps(
            [start.isoformat(), end.isoformat(), role, niche, region, compare]).encode()).hexdigest()[:24]

        async def load() -> dict[str, Any]:
            current = await self._metrics(start, end, role, niche, region)
            data: dict[str, Any] = {"range": {"start": start.isoformat(), "end": end.isoformat()},
                                    "filters": {"role": role, "niche": niche, "region": region}, "metrics": current}
            if compare:
                span = end - start
                data["previous_period"] = await self._metrics(start - span, start, role, niche, region)
            data["targets"] = self._targets(current)
            return data

        data = dict(await self.cache.get_or_set(f"dashboard:{fingerprint}", self.s.cache_ttl_dashboard, load))
        if not await admin_service.has_permission(admin, Permission.VIEW_REVENUE):
            # Admins never see revenue figures unless that is granted separately.
            data["metrics"] = {k: v for k, v in data["metrics"].items() if k != "revenue"}
            if "previous_period" in data:
                data["previous_period"] = {k: v for k, v in data["previous_period"].items() if k != "revenue"}
            data["targets"] = [t for t in data["targets"] if not t["metric"].startswith("revenue.")]
        return data

    @staticmethod
    def _targets(metrics: dict[str, Any]) -> list[dict[str, Any]]:
        """Each success metric shows its target beside the live value."""
        rows = []
        for key, target in TARGETS.items():
            area, name = key.split(".")
            rows.append({"metric": key, "value": metrics.get(area, {}).get(name), "target": target})
        return rows

    async def _scalar(self, stmt) -> int:
        return int((await self.db.execute(stmt)).scalar_one() or 0)

    @staticmethod
    def _pct(part: float, whole: float) -> float:
        return round(part / whole * 100, 2) if whole else 0.0

    async def _metrics(self, start: datetime, end: datetime, role: str | None, niche: str | None,
                       region: str | None) -> dict[str, Any]:
        account_filter = [Account.type.in_([role] if role else PEOPLE)]
        if region:
            account_filter.append(Account.region == region)
        count = lambda model, *where: self._scalar(select(func.count()).select_from(model).where(*where))  # noqa: E731

        # Growth
        dau = await count(Account, *account_filter, Account.last_active_at >= end - timedelta(days=1),
                          Account.last_active_at < end)
        mau = await count(Account, *account_filter, Account.last_active_at >= end - timedelta(days=30),
                          Account.last_active_at < end)
        signups = dict((await self.db.execute(
            select(Account.type, func.count()).where(Account.created_at >= start, Account.created_at < end,
                                                     *([Account.region == region] if region else []))
            .group_by(Account.type))).all())

        # Retention (churn definitions from AD-05)
        established = await count(Account, *account_filter, Account.created_at < end - timedelta(days=self.s.user_churn_days))
        churned = await count(Account, *account_filter, Account.created_at < end - timedelta(days=self.s.user_churn_days),
                              or_(Account.last_active_at.is_(None),
                                  Account.last_active_at < end - timedelta(days=self.s.user_churn_days)))
        subscribers = await count(Subscription, Subscription.started_at < end)
        lost_subscribers = await count(
            Subscription, Subscription.status.in_([SubscriptionStatus.CANCELLED, SubscriptionStatus.LAPSED]),
            Subscription.updated_at >= start, Subscription.updated_at < end)
        brand_rows = (await self.db.execute(
            select(Contract.hirer_id, func.max(Contract.created_at), func.count()).where(Contract.created_at < end)
            .group_by(Contract.hirer_id))).all()
        brands = len(brand_rows)
        churned_brands = sum(1 for _, last, _n in brand_rows
                             if self._aware(last) < end - timedelta(days=self.s.brand_churn_days))
        cohorts = await self._cohorts(end, account_filter)

        # Marketplace
        contract_filter = [Contract.type.in_([ContractType.HIRE_ME, ContractType.LIKENESS])]
        if niche:
            contract_filter.append(Contract.earner_id.in_(
                select(UserProfile.account_id).where(UserProfile.primary_niche == niche.lower())))
        completed = await count(Contract, *contract_filter, Contract.status == ContractStatus.COMPLETED,
                                Contract.completed_at >= start, Contract.completed_at < end)
        started = (await self.db.execute(
            select(Contract.created_at, Contract.start).where(
                *contract_filter, Contract.start.is_not(None), Contract.start >= start, Contract.start < end))).all()
        days_to_active = [(self._aware(s) - self._aware(c)).total_seconds() / 86400 for c, s in started]
        window = timedelta(days=90)
        hirers = (await self.db.execute(
            select(Contract.hirer_id, func.count()).where(*contract_filter, Contract.created_at >= end - window,
                                                          Contract.created_at < end)
            .group_by(Contract.hirer_id))).all()
        verified_influencers = await count(UserProfile, UserProfile.id_verified.is_(True))
        paid_influencers = await self._scalar(
            select(func.count(func.distinct(Contract.earner_id))).where(
                *contract_filter, Contract.status == ContractStatus.COMPLETED, Contract.completed_at < end))

        # Trust and safety
        disputes = await count(Dispute, Dispute.created_at >= start, Dispute.created_at < end)
        reports = dict((await self.db.execute(
            select(Report.reason, func.count()).where(Report.created_at >= start, Report.created_at < end)
            .group_by(Report.reason))).all())
        misuse_takedowns = await count(
            Report, Report.reason == ReportReason.MISUSE_OF_LIKENESS, Report.status == ReportStatus.UPHELD,
            Report.updated_at >= start, Report.updated_at < end)
        likeness_posts = await count(Post, Post.sponsored_contract_id.is_not(None), Post.created_at < end)
        suspensions = await count(Account, Account.suspended_at >= start, Account.suspended_at < end)

        # Agents
        agent_stats = await self.svc.admin.agent_content_stats()
        helpful = (await self.db.execute(
            select(func.coalesce(func.sum(Comment.helpful_count), 0), func.coalesce(func.sum(Comment.unhelpful_count), 0))
            .where(Comment.label == Label.AGENT, Comment.created_at >= start, Comment.created_at < end))).one()

        return {
            "growth": {"dau": dau, "mau": mau, "dau_mau_ratio": round(dau / mau, 3) if mau else 0.0,
                       "new_signups_by_role": {str(k): int(v) for k, v in signups.items()}},
            "retention": {"user_churn_pct": self._pct(churned, established),
                          "subscriber_churn_pct": self._pct(lost_subscribers, subscribers),
                          "brand_churn_pct": self._pct(churned_brands, brands), "cohorts": cohorts},
            "revenue": await self._revenue(start, end),
            "marketplace": {
                "completed_contracts": completed,
                "repeat_brand_rate_pct": self._pct(sum(1 for _, n in hirers if n > 1), len(hirers)),
                "median_days_to_active": round(statistics.median(days_to_active), 2) if days_to_active else None,
                "verified_influencers_with_paid_contract_pct": self._pct(paid_influencers, verified_influencers)},
            "trust_and_safety": {
                "dispute_rate_pct": self._pct(disputes, completed), "misuse_takedowns": misuse_takedowns,
                "misuse_takedown_rate_pct": self._pct(misuse_takedowns, likeness_posts),
                "reports_by_reason": {str(k): int(v) for k, v in reports.items()}, "suspensions": suspensions},
            "verification": await self._funnels(start, end),
            "wamiwa_agent": await self._wamiwa_agent(start, end),
            "agents": {
                "active_agents": await count(Account, Account.type == AccountType.AGENT,
                                             Account.status == AccountStatus.ACTIVE),
                "content_count": agent_stats["posts"], "video_hours": agent_stats["video_hours"],
                "free_tags": await count(AgentTag, AgentTag.created_at >= start, AgentTag.created_at < end),
                "helpful_rating_pct": self._pct(int(helpful[0]), int(helpful[0]) + int(helpful[1]))},
        }

    @staticmethod
    def _aware(value: datetime) -> datetime:
        from datetime import UTC

        return value if value.tzinfo else value.replace(tzinfo=UTC)

    async def _cohorts(self, end: datetime, account_filter: list) -> list[dict[str, Any]]:
        """Retention cohorts by signup month: how many of each month's signups were active in the last 30 days."""
        rows = (await self.db.execute(
            select(Account.created_at, Account.last_active_at).where(
                *account_filter, Account.created_at >= end - timedelta(days=365), Account.created_at < end))).all()
        cohorts: dict[str, list[int]] = {}
        for created, last_active in rows:
            bucket = cohorts.setdefault(self._aware(created).strftime("%Y-%m"), [0, 0])
            bucket[0] += 1
            if last_active and self._aware(last_active) >= end - timedelta(days=30):
                bucket[1] += 1
        return [{"signup_month": month, "signed_up": total, "retained": kept, "retention_pct": self._pct(kept, total)}
                for month, (total, kept) in sorted(cohorts.items())]

    async def _revenue(self, start: datetime, end: datetime) -> dict[str, Any]:
        in_range = (EscrowTransaction.created_at >= start, EscrowTransaction.created_at < end)
        gross_local = await self._scalar(select(func.coalesce(func.sum(EscrowTransaction.amount), 0)).where(*in_range))
        gross_cents = await self._scalar(
            select(func.coalesce(func.sum(Contract.price_cents), 0)).where(Contract.created_at >= start,
                                                                          Contract.created_at < end))
        fees = dict((await self.db.execute(
            select(Contract.type, func.coalesce(func.sum(EscrowTransaction.fee_amount), 0))
            .join(Contract, Contract.id == EscrowTransaction.contract_id)
            .where(EscrowTransaction.state.in_([EscrowState.CLEARED, EscrowState.PAID]),
                   EscrowTransaction.cleared_at >= start, EscrowTransaction.cleared_at < end)
            .group_by(Contract.type))).all())
        refunds = await self._scalar(select(func.coalesce(func.sum(EscrowTransaction.refunded_amount), 0)).where(*in_range))
        subscription_revenue = await self._scalar(
            select(func.coalesce(func.sum(Payment.amount_local), 0)).where(
                Payment.purpose == PaymentPurpose.SUBSCRIPTION, Payment.created_at >= start, Payment.created_at < end))
        active = (await self.db.execute(
            select(Subscription.period, func.count()).where(
                Subscription.status != SubscriptionStatus.LAPSED, Subscription.renews_at > end)
            .group_by(Subscription.period))).all()
        by_period = {str(p): int(n) for p, n in active}
        mrr_cents = by_period.get("monthly", 0) * self.s.subscription_monthly_cents + round(
            by_period.get("yearly", 0) * self.s.subscription_yearly_cents / 12)
        return {
            "currency": self.s.local_currency, "gross_escrow_value": gross_local,
            "gross_escrow_value_usd": round(gross_cents / 100, 2),
            "fees_by_type": {str(k): int(v) for k, v in fees.items()},
            "subscription_revenue": subscription_revenue, "mrr_usd": round(mrr_cents / 100, 2),
            "paying_subscribers": sum(by_period.values()), "refunds": refunds,
        }

    async def _wamiwa_agent(self, start: datetime, end: datetime) -> dict[str, Any]:
        from app.models import PromoDailyStat, PromoLink, PromoListing

        plans = dict((await self.db.execute(
            select(PromoListing.status, func.count()).group_by(PromoListing.status))).all())
        fields = ("card_impressions", "card_opens", "link_clicks", "agent_replies", "list_impressions", "list_clicks")
        totals = (await self.db.execute(
            select(*[func.coalesce(func.sum(getattr(PromoDailyStat, f)), 0) for f in fields])
            .where(PromoDailyStat.day >= start.date(), PromoDailyStat.day <= end.date()))).one()
        return {"active_plans": int(plans.get("active", 0)), "lapsed_plans": int(plans.get("lapsed", 0)),
                "links_under_review": await self._scalar(
                    select(func.count()).select_from(PromoLink).where(PromoLink.flagged.is_(True))),
                **dict(zip(fields, (int(v) for v in totals)))}

    async def _funnels(self, start: datetime, end: datetime) -> dict[str, Any]:
        """ID, company and public-figure funnels: started, passed, failed, waiting."""
        def shape(rows: dict[str, int]) -> dict[str, int]:
            passed = rows.get(VerificationStatus.PASSED, 0)
            failed = rows.get(VerificationStatus.FAILED, 0) + rows.get(VerificationStatus.REJECTED, 0)
            waiting = rows.get(VerificationStatus.PENDING, 0) + rows.get(VerificationStatus.MANUAL_REVIEW, 0)
            return {"started": sum(rows.values()), "passed": passed, "failed": failed, "waiting": waiting}

        async def grouped(model, column) -> dict[str, int]:
            rows = (await self.db.execute(
                select(column, func.count()).where(model.created_at >= start, model.created_at < end,
                                                   column.is_not(None)).group_by(column))).all()
            return {str(k): int(v) for k, v in rows}

        return {"identity": shape(await grouped(IdentityVerification, IdentityVerification.status)),
                "company": shape(await grouped(CompanyProfile, CompanyProfile.verification_status)),
                "public_figure": shape(await grouped(PublicFigureVerification, PublicFigureVerification.status))}

    async def export_csv(self, admin: Account, **params: Any) -> str:
        """Charts and tables can be exported as CSV: one row per metric, with the previous period and target."""
        await self.limiter.enforce(self.policies.exports, admin.id)
        data = await self.dashboard(admin, **params)
        targets = {t["metric"]: t["target"] for t in data["targets"]}

        def flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
            if isinstance(value, dict):
                for key, inner in value.items():
                    flatten(f"{prefix}.{key}" if prefix else str(key), inner, out)
            elif not isinstance(value, list):
                out[prefix] = value

        current, previous = {}, {}
        flatten("", data["metrics"], current)
        flatten("", data.get("previous_period", {}), previous)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["metric", "value", "previous_period", "target"])
        for metric, value in current.items():
            writer.writerow([metric, value, previous.get(metric, ""), targets.get(metric, "")])
        for cohort in data["metrics"].get("retention", {}).get("cohorts", []):
            writer.writerow([f"retention.cohort.{cohort['signup_month']}", cohort["retention_pct"], "", ""])
        return buffer.getvalue()
