"""Audit log (AD-04) and admin review queues (Epic 13)."""
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update

from app.core.enums import AccountType, NotificationType, Queue, QueueDecision
from app.core.exceptions import Conflict, Forbidden, Unprocessable
from app.core.utils import Page, add_working_days
from app.models import Account, AuditLog, QueueItem
from app.services.base import BaseService

# AQ-01 target decision times: ("hours", n) or ("working_days", n).
QUEUE_TARGETS: dict[Queue, tuple[str, int]] = {
    Queue.IDENTITY: ("hours", 24),
    Queue.PUBLIC_FIGURE: ("working_days", 3),
    Queue.COMPANY: ("working_days", 2),
    Queue.SCRIPTS: ("hours", 24),
    Queue.REPORTS: ("hours", 24),
    Queue.DISPUTES: ("working_days", 10),
    Queue.PAYOUTS: ("working_days", 1),
    Queue.FAKE_FOLLOWERS: ("working_days", 3),
    Queue.PROMOTIONS: ("working_days", 2),
}
LOW_SEVERITY_REPORT_HOURS = 48


class AuditService(BaseService):
    async def log(self, actor_id: str, action: str, target_type: str, target_id: str, reason: str,
                  **details: Any) -> AuditLog:
        if not reason or not reason.strip():
            raise Unprocessable("A reason is required for this action", code="reason_required")
        return await self.repo(AuditLog).add(AuditLog(
            actor_id=actor_id, action=action, target_type=target_type, target_id=target_id,
            reason=reason.strip(), details=details,
        ))

    async def search(self, *, actor_id: str | None, action: str | None, target_id: str | None,
                     cursor: str | None, limit: int) -> Page[AuditLog]:
        where = []
        if actor_id:
            where.append(AuditLog.actor_id == actor_id)
        if action:
            where.append(AuditLog.action == action)
        if target_id:
            where.append(AuditLog.target_id == target_id)
        return await self.repo(AuditLog).page(*where, cursor=cursor, limit=limit)


class QueueService(BaseService):
    def due_at(self, queue: Queue, severity: str = "normal") -> datetime:
        unit, amount = QUEUE_TARGETS[queue]
        if queue == Queue.REPORTS and severity == "low":
            return self.now() + timedelta(hours=LOW_SEVERITY_REPORT_HOURS)
        if unit == "hours":
            return self.now() + timedelta(hours=amount)
        return add_working_days(self.now(), amount)

    async def enqueue(self, queue: Queue, target_type: str, target_id: str, *, subject_account_id: str | None = None,
                      summary: str = "", severity: str = "normal", due_at: datetime | None = None,
                      escalated: bool = False) -> QueueItem:
        existing = await self.repo(QueueItem).first(
            QueueItem.queue == queue, QueueItem.target_type == target_type, QueueItem.target_id == target_id,
            QueueItem.decision.is_(None))
        if existing:
            return existing
        item = await self.repo(QueueItem).add(QueueItem(
            queue=queue, target_type=target_type, target_id=target_id, subject_account_id=subject_account_id,
            summary=summary, severity=severity, due_at=due_at or self.due_at(queue, severity), escalated=escalated,
        ))
        if severity == "high" or queue == Queue.DISPUTES:
            # Urgent work reaches every admin straight away; everything else is in the daily summary.
            label = str(queue).replace("_", " ")
            await self.svc.admin.alert_admins(f"New {label} item needs a decision", summary[:300],
                                              {"queue": str(queue), "queue_item_id": item.id})
        return item

    async def send_daily_summary(self) -> int:
        """Job: one email a day to each admin listing what is waiting and what is late, per queue."""
        now = self.now()
        lines = []
        for queue in Queue:
            waiting = await self.repo(QueueItem).count(QueueItem.queue == queue, QueueItem.decision.is_(None))
            if waiting:
                late = await self.repo(QueueItem).count(QueueItem.queue == queue, QueueItem.decision.is_(None),
                                                        QueueItem.due_at < now)
                lines.append(f"- {str(queue).replace('_', ' ').capitalize()}: {waiting} waiting" +
                             (f", {late} past target" if late else ""))
        if not lines:
            return 0
        admins = await self.repo(Account).list(Account.type == AccountType.ADMIN, Account.status == "active")
        for admin in admins:
            await self.svc.notifications.notify(admin.id, NotificationType.ADMIN, "Today's review queues",
                                                "\n".join(lines), {"queue": "summary"})
        return len(admins)

    async def list_items(self, queue: Queue, *, open_only: bool, mine: str | None, cursor: str | None,
                         limit: int) -> Page[QueueItem]:
        where = [QueueItem.queue == queue]
        if open_only:
            where.append(QueueItem.decision.is_(None))
        if mine:
            where.append(QueueItem.claimed_by == mine)
        return await self.repo(QueueItem).page(*where, cursor=cursor, limit=limit, newest_first=False)

    async def _guard_subject(self, admin: Account, item: QueueItem) -> None:
        """AD-01: admins cannot act on their own account or another admin's account."""
        if not item.subject_account_id:
            return
        if item.subject_account_id == admin.id:
            raise Forbidden("Admins cannot act on their own account", code="self_action")
        subject = await self.repo(Account).get(item.subject_account_id)
        if subject and subject.type == AccountType.ADMIN:
            raise Forbidden("Admins cannot act on another admin's account", code="admin_target")

    async def claim(self, admin: Account, item_id: str) -> QueueItem:
        """AQ-02: a conditional UPDATE makes the claim atomic, so two admins never share an item."""
        item = await self.repo(QueueItem).require(item_id, "Queue item not found")
        if item.decision is not None:
            raise Conflict("This item has already been decided", code="already_decided")
        if item.escalated and not admin.is_super_admin:
            raise Forbidden("This item was escalated to a Super Admin", code="escalated")
        await self._guard_subject(admin, item)
        result = await self.db.execute(
            update(QueueItem)
            .where(QueueItem.id == item_id, QueueItem.decision.is_(None),
                   (QueueItem.claimed_by.is_(None)) | (QueueItem.claimed_by == admin.id))
            .values(claimed_by=admin.id, claimed_at=self.now())
        )
        if not result.rowcount:
            raise Conflict("Another admin is already working on this item", code="already_claimed")
        await self.db.refresh(item)
        return item

    async def release(self, admin: Account, item_id: str) -> QueueItem:
        item = await self.repo(QueueItem).require(item_id, "Queue item not found")
        if item.claimed_by != admin.id and not admin.is_super_admin:
            raise Forbidden("Only the admin who claimed this item can release it")
        item.claimed_by, item.claimed_at = None, None
        return item

    async def decide(self, admin: Account, item_id: str, decision: QueueDecision, reason: str,
                     payload: dict[str, Any] | None = None) -> QueueItem:
        item = await self.repo(QueueItem).require(item_id, "Queue item not found")
        if item.decision is not None:
            raise Conflict("This item has already been decided", code="already_decided")
        if item.claimed_by != admin.id:
            raise Conflict("Claim this item before deciding it", code="not_claimed")
        await self._guard_subject(admin, item)
        await self.svc.audit.log(admin.id, f"queue.{item.queue}.{decision}", item.target_type, item.target_id,
                                 reason, queue_item_id=item.id)

        if decision == QueueDecision.ESCALATE:
            item.escalated, item.claimed_by, item.claimed_at = True, None, None
            return item
        if decision == QueueDecision.REQUEST_INFO:
            if item.subject_account_id:
                await self.svc.notifications.notify(
                    item.subject_account_id, NotificationType.LEGAL, "We need more information",
                    reason, {"queue": item.queue, "target_id": item.target_id})
            return item

        handler = self._handlers().get(Queue(item.queue))
        if handler is None:
            raise Unprocessable("Decide this item from its own screen", code="use_dedicated_endpoint",
                                hint="Disputes are decided with POST /admin/disputes/{id}/decide")
        await handler(item, decision == QueueDecision.APPROVE, admin, reason, payload or {})
        item.decision, item.reason = decision, reason
        item.decided_by, item.decided_at = admin.id, self.now()
        return item

    def _handlers(self) -> dict:
        """Each queue's decision is applied by the service that owns the underlying object."""
        return {
            Queue.IDENTITY: self.svc.identity.apply_review,
            Queue.PUBLIC_FIGURE: self.svc.popularity.apply_review,
            Queue.COMPANY: self.svc.companies.apply_review,
            Queue.SCRIPTS: self.svc.likeness.apply_script_review,
            Queue.REPORTS: self.svc.reports.apply_review,
            Queue.PAYOUTS: self.svc.payouts.apply_review,
            Queue.FAKE_FOLLOWERS: self.svc.creators.apply_review,
            Queue.PROMOTIONS: self.svc.promos.apply_review,
        }

    async def close_for_target(self, queue: Queue, target_id: str, decision: str, admin_id: str, reason: str) -> None:
        """Mark items decided when the decision was taken on a dedicated screen (disputes)."""
        items = await self.repo(QueueItem).list(
            QueueItem.queue == queue, QueueItem.target_id == target_id, QueueItem.decision.is_(None))
        for item in items:
            item.decision, item.reason = decision, reason
            item.decided_by, item.decided_at = admin_id, self.now()

    async def health(self) -> dict[str, Any]:
        """AQ-03: items waiting, average decision time and items past target, per queue and per admin."""
        now = self.now()
        items = (await self.db.execute(select(QueueItem))).scalars().all()

        def summarise(rows: list[QueueItem]) -> dict[str, Any]:
            waiting = [r for r in rows if r.decision is None]
            decided = [r for r in rows if r.decision is not None and r.decided_at]
            hours = [(r.decided_at - r.created_at).total_seconds() / 3600 for r in decided]
            return {
                "waiting": len(waiting),
                "past_target": sum(1 for r in waiting if r.due_at < now),
                "decided": len(decided),
                "average_decision_hours": round(sum(hours) / len(hours), 2) if hours else None,
            }

        per_queue = {str(q): summarise([i for i in items if i.queue == q]) for q in Queue}
        admin_ids = {i.decided_by or i.claimed_by for i in items} - {None}
        per_admin = {
            admin_id: summarise([i for i in items if (i.decided_by or i.claimed_by) == admin_id])
            for admin_id in admin_ids
        }
        return {"per_queue": per_queue, "per_admin": per_admin, "generated_at": now.isoformat()}

    async def open_count(self, queue: Queue) -> int:
        stmt = select(func.count()).select_from(QueueItem).where(QueueItem.queue == queue, QueueItem.decision.is_(None))
        return int((await self.db.execute(stmt)).scalar_one())
