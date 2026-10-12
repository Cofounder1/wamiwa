"""NT-01 notifications: in-app rows plus email, respecting per-type preferences and digests."""
from typing import Any

from sqlalchemy import update

from app.core.enums import MANDATORY_NOTIFICATIONS, GuardianLinkStatus, NotificationType
from app.core.utils import Page
from app.models import Account, AgentProfile, GuardianLink, Notification, UserProfile
from app.services.base import BaseService


class NotificationService(BaseService):
    async def notify(self, account_id: str, type: NotificationType, title: str, body: str = "",
                     data: dict[str, Any] | None = None, *, include_guardian: bool = False) -> Notification | None:
        """Create an in-app notification and email it after commit.

        Agent notifications are redirected to the owner. `include_guardian` copies a minor's
        contract notification to the guardian.
        """
        account = await self.repo(Account).get(account_id)
        if account is None:
            return None
        if account.is_agent:
            agent = await self.repo(AgentProfile).first(AgentProfile.account_id == account.id)
            if agent is None:
                return None
            data = {**(data or {}), "agent_id": account.id}
            account = await self.repo(Account).require(agent.owner_account_id)

        profile = await self.repo(UserProfile).first(UserProfile.account_id == account.id)
        mandatory = type in MANDATORY_NOTIFICATIONS
        prefs = (profile.notification_prefs if profile else None) or {}
        if not mandatory and prefs.get(str(type), True) is False:
            return None

        digest = bool(profile and profile.daily_digest and not mandatory)
        notification = await self.repo(Notification).add(Notification(
            account_id=account.id, type=type, title=title, body=body, data=data or {},
            digest_pending=digest, emailed=not digest and bool(account.email),
        ))
        if not digest and account.email:
            self._email_after_commit(account.email, title, body)

        if include_guardian and profile and profile.is_minor:
            link = await self.repo(GuardianLink).first(
                GuardianLink.minor_account_id == account.id, GuardianLink.status == GuardianLinkStatus.ACTIVE)
            if link and link.guardian_account_id:
                await self.notify(link.guardian_account_id, type, f"[For @{account.handle}] {title}", body,
                                  {**(data or {}), "minor_id": account.id})
        return notification

    def _email_after_commit(self, to: str, subject: str, body: str) -> None:
        async def send() -> None:
            await self.providers.email.send(to, subject, body)

        self.uow.on_commit(send)

    def email(self, to: str, subject: str, body: str) -> None:
        """Queue a plain email (invites, OTPs for non-accounts) for after commit."""
        self._email_after_commit(to, subject, body)

    def sms(self, to: str, body: str) -> None:
        async def send() -> None:
            await self.providers.sms.send(to, body)

        self.uow.on_commit(send)

    async def list_for(self, account_id: str, cursor: str | None, limit: int, unread_only: bool = False) -> Page[Notification]:
        where = [Notification.account_id == account_id]
        if unread_only:
            where.append(Notification.read_at.is_(None))
        return await self.repo(Notification).page(*where, cursor=cursor, limit=limit)

    async def unread_count(self, account_id: str) -> int:
        return await self.repo(Notification).count(
            Notification.account_id == account_id, Notification.read_at.is_(None))

    async def mark_read(self, account_id: str, notification_ids: list[str] | None = None) -> int:
        stmt = update(Notification).where(
            Notification.account_id == account_id, Notification.read_at.is_(None))
        if notification_ids:
            stmt = stmt.where(Notification.id.in_(notification_ids))
        result = await self.db.execute(stmt.values(read_at=self.now()))
        return result.rowcount or 0

    async def set_preferences(self, account_id: str, prefs: dict[str, bool], daily_digest: bool | None) -> dict:
        profile = await self.repo(UserProfile).first(UserProfile.account_id == account_id)
        optional = {str(t) for t in NotificationType} - {str(t) for t in MANDATORY_NOTIFICATIONS}
        if profile is None:
            return {"optional": {}, "daily_digest": False, "always_on": sorted(str(t) for t in MANDATORY_NOTIFICATIONS)}
        merged = dict(profile.notification_prefs or {})
        merged.update({k: bool(v) for k, v in prefs.items() if k in optional})
        profile.notification_prefs = merged
        if daily_digest is not None:
            profile.daily_digest = daily_digest
        return {
            "optional": {name: merged.get(name, True) for name in sorted(optional)},
            "daily_digest": profile.daily_digest,
            "always_on": sorted(str(t) for t in MANDATORY_NOTIFICATIONS),
        }

    async def send_daily_digests(self) -> int:
        """Job: roll each account's pending optional notifications into one email."""
        pending = await self.repo(Notification).list(Notification.digest_pending.is_(True), limit=5000)
        by_account: dict[str, list[Notification]] = {}
        for notification in pending:
            by_account.setdefault(notification.account_id, []).append(notification)
        accounts = await self.repo(Account).by_ids(list(by_account))
        for account_id, items in by_account.items():
            account = accounts.get(account_id)
            if account and account.email:
                lines = "\n".join(f"- {n.title}" for n in items)
                self._email_after_commit(account.email, f"Your Wamiwa daily digest ({len(items)})", lines)
            for item in items:
                item.digest_pending = False
                item.emailed = True
        return len(by_account)
