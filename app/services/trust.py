"""RP-01 reports, AD-02 suspend / remove / appeal, and DL-01 account deletion."""
from datetime import datetime, timedelta
from typing import Any

from app.core.enums import (
    AccountStatus, ContractStatus, ContractType, ModerationStatus, NotificationType, Permission, Queue,
    ReportReason, ReportStatus, ReportTarget,
)
from app.core.exceptions import Conflict, Forbidden, NotFound, Unprocessable
from app.models import (
    Account, AgentProfile, Comment, Contract, ConversationParticipant, LikenessAsset,
    Message, Post, QueueItem, Report, UserProfile,
)
from app.services.base import BaseService

HIGH_SEVERITY = {ReportReason.HARASSMENT, ReportReason.SCAM, ReportReason.IMPERSONATION, ReportReason.MISUSE_OF_LIKENESS}
SYSTEM_ACTOR = "system"


class ReportService(BaseService):
    async def _target_account(self, reporter: Account, target_type: ReportTarget, target_id: str) -> str | None:
        if target_type == ReportTarget.ACCOUNT:
            return (await self.repo(Account).require(target_id, "Account not found")).id
        if target_type == ReportTarget.POST:
            return (await self.repo(Post).require(target_id, "Post not found")).author_account_id
        if target_type == ReportTarget.COMMENT:
            return (await self.repo(Comment).require(target_id, "Comment not found")).author_account_id
        if target_type == ReportTarget.SCRIPT:
            contract = await self.repo(Contract).require(target_id, "Contract not found")
            if reporter.id not in (contract.hirer_id, contract.earner_id) and not await self.svc.access.is_guardian_of(
                    reporter.id, contract.earner_id):
                raise NotFound("Contract not found")
            return contract.hirer_id if reporter.id != contract.hirer_id else contract.earner_id
        if target_type == ReportTarget.MESSAGE:
            message = await self.repo(Message).require(target_id, "Message not found")
            await self._require_participant(reporter.id, message.conversation_id)
            message.reported = True  # MS-04: reported messages become visible to admins
            return message.sender_id
        await self._require_participant(reporter.id, target_id)
        for message in await self.repo(Message).list(Message.conversation_id == target_id, limit=500):
            message.reported = True
        other = await self.repo(ConversationParticipant).first(
            ConversationParticipant.conversation_id == target_id, ConversationParticipant.account_id != reporter.id)
        return other.account_id if other else None

    async def _require_participant(self, account_id: str, conversation_id: str) -> None:
        if not await self.repo(ConversationParticipant).exists(
                ConversationParticipant.conversation_id == conversation_id,
                ConversationParticipant.account_id == account_id):
            raise NotFound("Conversation not found")

    async def create(self, reporter: Account, target_type: ReportTarget, target_id: str, reason: ReportReason,
                     details: str, *, target_account_id: str | None = None, due_at: datetime | None = None) -> dict:
        """Any account type can report any other: humans, companies and agents."""
        await self.limiter.enforce(self.policies.reports, reporter.id)
        target_account_id = target_account_id or await self._target_account(reporter, target_type, target_id)
        if target_account_id == reporter.id:
            raise Unprocessable("You cannot report yourself", code="self_report")
        if await self.repo(Report).exists(
                Report.reporter_id == reporter.id, Report.target_type == target_type, Report.target_id == target_id,
                Report.status == ReportStatus.OPEN):
            raise Conflict("You have already reported this", code="already_reported")
        report = await self.repo(Report).add(Report(
            reporter_id=reporter.id, target_type=target_type, target_id=target_id,
            target_account_id=target_account_id, reason=reason, details=details.strip(),
            weight=reporter.report_weight))
        if reason == ReportReason.RACISM_OR_ETHNIC_INSULT:
            # RP-02: the investigation team decides, and the account is suspended only if they uphold it. The
            # content is hidden at once when the report is credible (an ID-verified reporter with no false reports)
            # or a second person reports it, so one bad-faith report can't silence someone.
            reporters = await self.repo(Report).count(
                Report.target_type == target_type, Report.target_id == target_id,
                Report.reason == ReportReason.RACISM_OR_ETHNIC_INSULT, Report.status == ReportStatus.OPEN)
            credible = reporter.false_reports == 0 and await self.svc.access.is_id_verified(reporter)
            if credible or reporters >= 2:
                await self._hide_pending_investigation(target_type, target_id)
            await self.svc.queues.enqueue(
                Queue.INVESTIGATIONS, "report", report.id, subject_account_id=target_account_id, severity="high",
                summary=f"Racism or insulting Yoruba people: {details[:160]}")
            return {"id": report.id, "status": report.status, "reason": report.reason}
        # Reports from accounts with a record of false reports carry less weight.
        severity = "high" if reason in HIGH_SEVERITY else "normal"
        if reporter.report_weight < 0.5 or (reason == ReportReason.OTHER and severity != "high"):
            severity = "low"
        await self.svc.queues.enqueue(Queue.REPORTS, "report", report.id, subject_account_id=target_account_id,
                                      summary=f"{reason}: {details[:160]}", severity=severity, due_at=due_at)
        if reason == ReportReason.SPAM and reporter.is_company and target_account_id:
            target = await self.repo(Account).get(target_account_id)
            if target and target.is_company:
                await self.svc.b2b.register_spam_report(target.id)
        return {"id": report.id, "status": report.status, "reason": report.reason}

    async def mine(self, reporter: Account) -> list[dict[str, Any]]:
        reports = await self.repo(Report).list(Report.reporter_id == reporter.id, order_by=Report.id.desc(), limit=100)
        return [{"id": r.id, "target_type": r.target_type, "target_id": r.target_id, "reason": r.reason,
                 "status": r.status, "created_at": r.created_at.isoformat()} for r in reports]

    async def apply_review(self, item: QueueItem, approved: bool, admin: Account, reason: str, payload: dict) -> None:
        """Reports queue. `approve` upholds a report (or an appeal); for auto-flagged content it clears it."""
        if item.target_type == "equity_cancellation":  # HM-06: a request to end an equity agreement
            await self.svc.equity.apply_cancellation(item, approved, admin, reason, payload)
            return
        if item.target_type in ("post", "comment"):  # auto-flagged by moderation
            await self._set_visibility(item.target_type, item.target_id, visible=approved)
            return
        if item.target_type == "suspension_appeal":
            await self.svc.trust.resolve_appeal(admin, item.target_id, approved, reason)
            return
        if item.target_type == "off_platform":
            if approved and payload.get("suspend"):
                await self.svc.trust.suspend(admin, item.target_id, reason)
            elif approved:
                await self.svc.notifications.notify(item.target_id, NotificationType.LEGAL,
                                                    "Warning: keep paid work on Wamiwa", reason)
            return

        report = await self.repo(Report).require(item.target_id)
        report.status = ReportStatus.UPHELD if approved else ReportStatus.DISMISSED
        reporter = await self.repo(Account).require(report.reporter_id)
        if approved:
            await self._enforce(report, admin, reason, payload)
        elif payload.get("false_report"):
            # Repeated false reports lower that account's report weight and can lead to a warning.
            reporter.false_reports += 1
            reporter.report_weight = max(0.1, round(reporter.report_weight - 0.2, 2))
            if reporter.false_reports >= self.s.false_reports_before_warning:
                await self.svc.notifications.notify(
                    reporter.id, NotificationType.LEGAL, "Warning: false reports",
                    "Several of your reports were found to be false. Further false reports may limit your account.")
        await self.svc.notifications.notify(
            reporter.id, NotificationType.LEGAL, "Your report was reviewed",
            "We took action." if approved else "We did not find a violation.", {"report_id": report.id})

    async def _hide_pending_investigation(self, target_type: ReportTarget, target_id: str) -> None:
        if target_type == ReportTarget.POST:
            post = await self.repo(Post).get(target_id)
            if post and post.moderation_status == ModerationStatus.VISIBLE:
                post.moderation_status = ModerationStatus.FLAGGED
                await self.cache.invalidate_prefix("feed:")
        elif target_type == ReportTarget.COMMENT:
            comment = await self.repo(Comment).get(target_id)
            if comment and comment.moderation_status == ModerationStatus.VISIBLE:
                comment.moderation_status = ModerationStatus.FLAGGED
                await self.svc.comments.recount(await self.repo(Post).require(comment.post_id))

    async def apply_investigation(self, item: QueueItem, approved: bool, admin: Account, reason: str,
                                  payload: dict) -> None:
        """RP-02. Upheld: the content is removed, the account suspended, and the two accounts blocked from each
        other. Rejected: hidden content comes back."""
        report = await self.repo(Report).require(item.target_id)
        report.status = ReportStatus.UPHELD if approved else ReportStatus.DISMISSED
        content = report.target_type in (ReportTarget.POST, ReportTarget.COMMENT)
        if approved:
            if content:
                await self._set_visibility(report.target_type, report.target_id, visible=False)
            target = await self.repo(Account).get(report.target_account_id) if report.target_account_id else None
            if target and not target.is_admin and target.status == AccountStatus.ACTIVE:
                why = f"Racism or insulting Yoruba people, confirmed by the investigation team. {reason}"
                await self.svc.audit.log(admin.id, "account.suspend", "account", target.id, why, investigation=report.id)
                await self.svc.trust._apply_suspension(target, why)
                await self.svc.notifications.notify(
                    target.id, NotificationType.LEGAL, "Your account has been suspended",
                    f"{why} You can appeal once within {self.s.suspension_appeal_days} days.")
            if target:
                reporter = await self.repo(Account).require(report.reporter_id)
                await self.svc.blocks.block(reporter, target.id)
        elif content:
            model = Post if report.target_type == ReportTarget.POST else Comment
            target_content = await self.repo(model).get(report.target_id)
            if target_content and target_content.moderation_status == ModerationStatus.FLAGGED:
                await self._set_visibility(report.target_type, report.target_id, visible=True)
        await self.svc.notifications.notify(
            report.reporter_id, NotificationType.LEGAL, "Your report was reviewed",
            "The investigation team confirmed it and took action." if approved else
            "The investigation team did not find racism or insults against Yoruba people.", {"report_id": report.id})

    async def _set_visibility(self, target_type: str, target_id: str, visible: bool) -> None:
        model = Post if target_type == "post" else Comment
        target = await self.repo(model).get(target_id)
        if target:
            target.moderation_status = ModerationStatus.VISIBLE if visible else ModerationStatus.REMOVED
            if model is Post:
                await self.cache.invalidate_prefix("feed:")
            else:  # a removed comment stops counting; one cleared after review starts counting
                await self.svc.comments.recount(await self.repo(Post).require(target.post_id))

    async def _enforce(self, report: Report, admin: Account, reason: str, payload: dict) -> None:
        if report.reason == ReportReason.MISUSE_OF_LIKENESS and report.target_type == ReportTarget.SCRIPT:
            # LK-08: remove the content on-platform; the brand can also be suspended.
            await self.svc.posts.take_down_contract_posts(report.target_id)
        elif report.target_type == ReportTarget.POST:
            post = await self.repo(Post).get(report.target_id)
            if post and report.reason == ReportReason.MISLABELED_AI:
                await self.svc.posts.apply_mislabel_report(post)
            elif post:
                await self._set_visibility("post", post.id, visible=False)
        elif report.target_type == ReportTarget.COMMENT:
            await self._set_visibility("comment", report.target_id, visible=False)
        if payload.get("suspend") and report.target_account_id:
            await self.svc.trust.suspend(admin, report.target_account_id, reason)
        elif report.target_account_id:
            await self.svc.notifications.notify(
                report.target_account_id, NotificationType.LEGAL, "Action was taken on your content", reason)


class TrustService(BaseService):
    async def _target(self, admin: Account, account_id: str) -> Account:
        """AD-01: admins cannot act on their own account or another admin's account."""
        target = await self.repo(Account).require(account_id, "Account not found")
        if target.id == admin.id:
            raise Forbidden("Admins cannot act on their own account", code="self_action")
        if target.is_admin:
            raise Forbidden("Admins cannot act on another admin's account", code="admin_target",
                            hint="Super Admins manage admins from admin management")
        return target

    async def _notify_or_log_silence(self, admin: Account, target: Account, title: str, reason: str, notify: bool) -> None:
        if notify:
            await self.svc.notifications.notify(
                target.id, NotificationType.LEGAL, title,
                f"{reason} You can appeal once within {self.s.suspension_appeal_days} days.")
        elif not admin.is_super_admin:
            # AD-02: skipping notice needs a Super Admin (fraud or legal investigation only).
            raise Forbidden("Only a Super Admin can skip the notice", code="super_admin_required")

    async def _apply_suspension(self, target: Account, reason: str) -> None:
        if target.status in (AccountStatus.SUSPENDED, AccountStatus.REMOVED, AccountStatus.DELETED):
            raise Conflict(f"This account is already {target.status}", code="invalid_account_status")
        target.status_before_hold = target.status
        target.status, target.suspended_reason, target.suspended_at = AccountStatus.SUSPENDED, reason, self.now()
        target.appeal_used = False
        await self.svc.agents.cascade_owner_status(target.id, suspended=True)  # AG-01
        await self.svc.profiles.invalidate(target)
        await self.cache.invalidate_prefix("feed:")

    async def suspend(self, admin: Account, account_id: str, reason: str, notify: bool = True) -> dict[str, Any]:
        await self.svc.admin.require_permission(admin, Permission.SUSPEND)
        target = await self._target(admin, account_id)
        await self.svc.audit.log(admin.id, "account.suspend", "account", target.id, reason, notified=notify)
        await self._apply_suspension(target, reason)
        await self._notify_or_log_silence(admin, target, "Your account has been suspended", reason, notify)
        return {"id": target.id, "status": target.status}

    async def system_suspend(self, target: Account, reason: str) -> None:
        """Automatic suspension by a platform rule (e.g. repeated unlabeled AI content)."""
        if target.status != AccountStatus.ACTIVE:
            return
        await self.svc.audit.log(SYSTEM_ACTOR, "account.suspend", "account", target.id, reason, automatic=True)
        await self._apply_suspension(target, reason)
        await self.svc.notifications.notify(
            target.id, NotificationType.LEGAL, "Your account has been suspended",
            f"{reason} You can appeal once within {self.s.suspension_appeal_days} days.")

    async def reinstate(self, admin: Account, account_id: str, reason: str) -> dict[str, Any]:
        await self.svc.admin.require_permission(admin, Permission.SUSPEND)
        target = await self._target(admin, account_id)
        if target.status not in (AccountStatus.SUSPENDED, AccountStatus.FROZEN):
            raise Conflict(f"This account is {target.status}", code="invalid_account_status")
        await self.svc.audit.log(admin.id, "account.reinstate", "account", target.id, reason)
        await self._restore(target)
        await self.svc.notifications.notify(target.id, NotificationType.LEGAL, "Your account has been restored", reason)
        return {"id": target.id, "status": target.status}

    async def _restore(self, target: Account) -> None:
        previous = target.status_before_hold
        target.status = previous if previous in (AccountStatus.ACTIVE, AccountStatus.PENDING_GUARDIAN) else AccountStatus.ACTIVE
        target.suspended_reason = target.suspended_at = target.status_before_hold = None
        await self.svc.agents.cascade_owner_status(target.id, suspended=False)
        await self.svc.profiles.invalidate(target)

    async def remove(self, admin: Account, account_id: str, reason: str, notify: bool = True) -> dict[str, Any]:
        """Permanent removal: Super Admin only."""
        if not admin.is_super_admin:
            raise Forbidden("Only a Super Admin can remove accounts permanently", code="super_admin_required")
        target = await self._target(admin, account_id)
        if target.status in (AccountStatus.REMOVED, AccountStatus.DELETED):
            raise Conflict(f"This account is already {target.status}", code="invalid_account_status")
        await self.svc.audit.log(admin.id, "account.remove", "account", target.id, reason, notified=notify)
        await self.svc.likeness.end_for_account_removal(target.id, admin.id)
        # Offers that never became active are refunded in full.
        pending = await self.repo(Contract).list(
            (Contract.hirer_id == target.id) | (Contract.earner_id == target.id),
            Contract.status.in_([ContractStatus.OFFERED, ContractStatus.CHANGES_REQUESTED,
                                 ContractStatus.ADMIN_REVIEW, ContractStatus.PENDING_GUARDIAN]))
        for contract in pending:
            await self.svc.contracts.close_with_refund(contract, ContractStatus.CANCELLED, admin.id, "Account removed")
        target.status, target.suspended_reason, target.suspended_at = AccountStatus.REMOVED, reason, self.now()
        target.appeal_used = False
        await self.svc.agents.cascade_owner_status(target.id, suspended=True)
        await self.svc.profiles.invalidate(target)
        await self.cache.invalidate_prefix("feed:")
        if notify and target.email:
            self.svc.notifications.email(
                target.email, "Your Wamiwa account has been removed",
                f"{reason} You can appeal once within {self.s.suspension_appeal_days} days by replying to support.")
        return {"id": target.id, "status": target.status}

    async def appeal(self, account: Account, statement: str) -> dict[str, Any]:
        """AD-02: every suspended or removed account can appeal once within 14 days."""
        if account.status not in (AccountStatus.SUSPENDED, AccountStatus.REMOVED):
            raise Conflict("Only suspended or removed accounts can appeal", code="not_suspended")
        if account.appeal_used:
            raise Conflict("You have already used your appeal", code="appeal_used")
        deadline = (account.suspended_at or self.now()) + timedelta(days=self.s.suspension_appeal_days)
        if self.now() > deadline:
            raise Conflict("The 14-day appeal window has closed", code="appeal_window_closed")
        account.appeal_used = True
        await self.svc.queues.enqueue(Queue.REPORTS, "suspension_appeal", account.id, subject_account_id=account.id,
                                      summary=statement[:300], severity="high", escalated=True)
        return {"appealed": True, "status": account.status}

    async def resolve_appeal(self, admin: Account, account_id: str, upheld: bool, reason: str) -> None:
        target = await self.repo(Account).require(account_id)
        if upheld and target.status in (AccountStatus.SUSPENDED, AccountStatus.REMOVED):
            await self._restore(target)
        if target.email:
            self.svc.notifications.email(
                target.email, "Your appeal was " + ("successful" if upheld else "not successful"), reason)


class DeletionService(BaseService):
    async def blockers(self, account: Account) -> list[dict[str, Any]]:
        """DL-01: money in escrow, an open dispute or an active likeness contract block deletion."""
        blockers: list[dict[str, Any]] = []
        if await self.svc.escrow.has_open_funds(account.id):
            blockers.append({"code": "money_in_escrow", "message": "You have money in escrow or awaiting payout"})
        if await self.svc.disputes.has_open(account.id):
            blockers.append({"code": "open_dispute", "message": "You have an open dispute"})
        likeness = await self.repo(Contract).list(
            (Contract.hirer_id == account.id) | (Contract.earner_id == account.id),
            Contract.type == ContractType.LIKENESS,
            Contract.status.in_([ContractStatus.ACTIVE, ContractStatus.PENDING_GUARDIAN]))
        if likeness:
            blockers.append({"code": "active_likeness_contract", "message": "You have an active likeness contract",
                             "contract_ids": [c.id for c in likeness]})
        return blockers

    async def request(self, account: Account) -> dict[str, Any]:
        if account.status == AccountStatus.PENDING_DELETION:
            raise Conflict("Deletion is already scheduled", code="deletion_pending")
        blockers = await self.blockers(account)
        if blockers:
            raise Conflict("Your account cannot be deleted yet", code="deletion_blocked", blockers=blockers)
        account.status_before_hold, account.status = account.status, AccountStatus.PENDING_DELETION
        account.deletion_requested_at = self.now()
        delete_on = self.now() + timedelta(days=self.s.deletion_grace_days)
        await self.svc.profiles.invalidate(account)
        await self.svc.notifications.notify(
            account.id, NotificationType.SECURITY, "Your account is scheduled for deletion",
            f"It will be deleted on {delete_on.date().isoformat()}. You can cancel any time before then.")
        return {"status": account.status, "delete_on": delete_on.isoformat()}

    async def cancel(self, account: Account) -> dict[str, Any]:
        if account.status != AccountStatus.PENDING_DELETION:
            raise Conflict("No deletion is scheduled", code="no_deletion_pending")
        account.status = account.status_before_hold or AccountStatus.ACTIVE
        account.deletion_requested_at = account.status_before_hold = None
        return {"status": account.status}

    async def purge_due(self) -> int:
        """Job: after the 30-day grace period, delete personal data, photos and voice samples.
        Financial records (contracts, escrow, payouts, ledger) are kept as the law requires."""
        cutoff = self.now() - timedelta(days=self.s.deletion_grace_days)
        due = await self.repo(Account).list(
            Account.status == AccountStatus.PENDING_DELETION, Account.deletion_requested_at <= cutoff)
        for account in due:
            await self._purge(account)
        return len(due)

    async def _purge(self, account: Account) -> None:
        await self.svc.profiles.invalidate(account)
        account.email, account.phone, account.phone_hash, account.password_hash = None, None, None, None
        account.totp_secret, account.totp_enabled = None, False
        account.handle, account.display_name = f"deleted_{account.id[-16:]}", "Deleted account"
        account.status = AccountStatus.DELETED
        profile = await self.repo(UserProfile).first(UserProfile.account_id == account.id)
        if profile:
            profile.name, profile.verified_name, profile.bio, profile.photo_ref = "", None, "", None
            profile.links, profile.location, profile.face_ref = [], None, None
        for asset in await self.repo(LikenessAsset).list(LikenessAsset.account_id == account.id):
            asset.deleted_at, asset.file_ref = asset.deleted_at or self.now(), "deleted"
        for post in await self.repo(Post).list(Post.author_account_id == account.id):
            post.moderation_status, post.text, post.media_ref = ModerationStatus.REMOVED, "", None
        await self.svc.uploads.delete_for_account(account.id)  # the files themselves, not just the references
        for agent in await self.repo(AgentProfile).list(AgentProfile.owner_account_id == account.id):
            agent_account = await self.repo(Account).get(agent.account_id)
            if agent_account:
                agent_account.status, agent.paused = AccountStatus.DELETED, True
        await self.cache.invalidate_prefix("feed:")
        await self.cache.invalidate_prefix("marketplace:")

    async def export(self, account: Account) -> dict[str, Any]:
        """NDPA 2023: users can download their data."""
        await self.limiter.enforce(self.policies.exports, account.id)
        posts = await self.repo(Post).list(Post.author_account_id == account.id, limit=5000)
        comments = await self.repo(Comment).list(Comment.author_account_id == account.id, limit=5000)
        contracts = await self.repo(Contract).list(
            (Contract.hirer_id == account.id) | (Contract.earner_id == account.id), limit=5000)
        conversations = await self.repo(ConversationParticipant).list(ConversationParticipant.account_id == account.id)
        messages = await self.repo(Message).list(Message.sender_id == account.id, limit=10000)
        return {
            "account": await self.svc.accounts.me(account),
            "posts": [self.svc.posts.present(p) for p in posts],
            "comments": [{"id": c.id, "post_id": c.post_id, "text": c.text, "created_at": c.created_at.isoformat()}
                         for c in comments],
            "contracts": [self.svc.contracts.present(c) for c in contracts],
            "conversation_ids": [c.conversation_id for c in conversations],
            "messages_sent": [{"conversation_id": m.conversation_id, "body": m.body,
                               "created_at": m.created_at.isoformat()} for m in messages],
            "exported_at": self.now().isoformat(),
        }
