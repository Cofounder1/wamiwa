"""PY-04 disputes and appeals, and Epic 8 subscriptions."""
from datetime import timedelta
from typing import Any

from app.core.enums import (
    BillingPeriod, ContractStatus, DisputeDecision, DisputeStatus, EscrowState, NotificationType,
    PaymentPurpose, Queue, SubscriptionStatus,
)
from app.core.exceptions import Conflict, Forbidden, NotFound, PaymentRequired, Unprocessable
from app.core.utils import add_working_days, pct_of
from app.models import Account, Contract, Dispute, DisputeEvidence, EscrowTransaction, LedgerEntry, Subscription
from app.services.base import BaseService

FREEZABLE = (EscrowState.ACTIVE, EscrowState.DELIVERED, EscrowState.CLEARED)


class DisputeService(BaseService):
    def present(self, dispute: Dispute, evidence: list[DisputeEvidence] | None = None) -> dict[str, Any]:
        data = {
            "id": dispute.id, "contract_id": dispute.contract_id, "raised_by": dispute.raised_by,
            "reason": dispute.reason, "is_misuse_claim": dispute.is_misuse_claim, "status": dispute.status,
            "evidence_due_at": dispute.evidence_due_at.isoformat(),
            "decision_due_at": dispute.decision_due_at.isoformat(), "decision": dispute.decision,
            "release_pct": dispute.release_pct, "decision_reason": dispute.decision_reason,
            "decided_at": dispute.decided_at.isoformat() if dispute.decided_at else None,
            "appealed_by": dispute.appealed_by, "appeal_reason": dispute.appeal_reason,
            "post_payout_adjustment": dispute.post_payout_adjustment,
        }
        if evidence is not None:
            data["evidence"] = [{"submitted_by": e.submitted_by, "statement": e.statement,
                                 "attachments": e.attachments, "at": e.created_at.isoformat()} for e in evidence]
        return data

    async def _party(self, account: Account, dispute_id: str) -> tuple[Dispute, Contract]:
        dispute = await self.repo(Dispute).require(dispute_id, "Dispute not found")
        contract = await self.repo(Contract).require(dispute.contract_id)
        if account.id not in (contract.hirer_id, contract.earner_id) and not account.is_admin \
                and not await self.svc.access.is_guardian_of(account.id, contract.earner_id):
            raise NotFound("Dispute not found")
        return dispute, contract

    async def open(self, account: Account, contract_id: str, reason: str, is_misuse_claim: bool) -> dict[str, Any]:
        contract = await self.repo(Contract).get(contract_id)
        if contract is None or account.id not in (contract.hirer_id, contract.earner_id):
            raise NotFound("Contract not found")
        if await self.repo(Dispute).exists(Dispute.contract_id == contract.id):
            raise Conflict("This contract already has a dispute", code="dispute_exists")
        escrow = await self.svc.escrow.for_contract(contract.id)
        in_review = contract.status == ContractStatus.DELIVERED
        misuse_ok = False
        if is_misuse_claim and contract.status in (ContractStatus.COMPLETED, ContractStatus.CANCELLED,
                                                   ContractStatus.ACTIVE):
            # Misuse claims stay open for 30 days after payout (or after clearing, while payout is pending).
            reference = escrow.paid_at or escrow.cleared_at or self.now()
            misuse_ok = self.now() <= reference + timedelta(days=self.s.dispute_misuse_window_days)
        if not (in_review or misuse_ok):
            raise Conflict("A dispute can be opened during the 7-day review window, or within 30 days of payout "
                           "for misuse claims", code="dispute_window_closed")
        dispute = await self.repo(Dispute).add(Dispute(
            contract_id=contract.id, raised_by=account.id, reason=reason.strip(), is_misuse_claim=is_misuse_claim,
            evidence_due_at=self.now() + timedelta(days=self.s.dispute_evidence_days),
            decision_due_at=add_working_days(self.now(), self.s.dispute_decision_working_days),
            contract_status_before=contract.status))
        if escrow.state in FREEZABLE:
            await self.svc.escrow.freeze(contract.id, account.id, f"Dispute opened: {reason[:120]}")
        contract.status = ContractStatus.DISPUTED
        await self.svc.queues.enqueue(Queue.DISPUTES, "dispute", dispute.id, summary=reason[:200],
                                      due_at=dispute.decision_due_at)
        other = contract.earner_id if account.id == contract.hirer_id else contract.hirer_id
        await self.svc.notifications.notify(
            other, NotificationType.DISPUTE, "A dispute was opened on your contract",
            f"Submit your evidence within {self.s.dispute_evidence_days} days. Missing evidence counts against you.",
            {"dispute_id": dispute.id, "contract_id": contract.id}, include_guardian=other == contract.earner_id)
        return self.present(dispute)

    async def add_evidence(self, account: Account, dispute_id: str, statement: str, attachments: list[str]) -> dict:
        dispute, contract = await self._party(account, dispute_id)
        if account.id not in (contract.hirer_id, contract.earner_id):
            raise Forbidden("Only the two sides of the contract submit evidence")
        if dispute.status != DisputeStatus.EVIDENCE or self.now() > dispute.evidence_due_at:
            raise Conflict("The evidence window has closed", code="evidence_closed")
        await self.repo(DisputeEvidence).add(DisputeEvidence(
            dispute_id=dispute.id, submitted_by=account.id, statement=statement.strip(), attachments=attachments))
        return await self.view(account, dispute_id)

    async def view(self, account: Account, dispute_id: str) -> dict[str, Any]:
        dispute, contract = await self._party(account, dispute_id)
        evidence = list(await self.repo(DisputeEvidence).list(DisputeEvidence.dispute_id == dispute.id))
        data = self.present(dispute, evidence)
        if account.is_admin:
            submitted = {e.submitted_by for e in evidence}
            closed = dispute.status != DisputeStatus.EVIDENCE
            # Missing evidence counts against that side.
            data["missing_evidence_from"] = [side for side, party in (("hirer", contract.hirer_id),
                                                                      ("earner", contract.earner_id))
                                             if closed and party not in submitted]
            data["contract"] = self.svc.contracts.present(contract)
        return data

    async def list_for(self, account: Account) -> list[dict[str, Any]]:
        contracts = await self.repo(Contract).list(
            (Contract.hirer_id == account.id) | (Contract.earner_id == account.id))
        disputes = await self.repo(Dispute).list(Dispute.contract_id.in_([c.id for c in contracts] or [""]))
        return [self.present(d) for d in disputes]

    async def close_evidence_windows(self) -> int:
        due = await self.repo(Dispute).list(Dispute.status == DisputeStatus.EVIDENCE, Dispute.evidence_due_at <= self.now())
        for dispute in due:
            dispute.status = DisputeStatus.UNDER_REVIEW
        return len(due)

    @staticmethod
    def _release_pct(decision: DisputeDecision, release_pct: int | None) -> int:
        if decision == DisputeDecision.RELEASE:
            return 100
        if decision == DisputeDecision.REFUND:
            return 0
        if release_pct is None or not 0 < release_pct < 100:
            raise Unprocessable("A split needs a release share between 1 and 99 percent", code="invalid_split")
        return release_pct

    async def _guard_admin(self, admin: Account, contract: Contract) -> None:
        parties = await self.repo(Account).by_ids([contract.hirer_id, contract.earner_id])
        if admin.id in parties or any(p.is_admin for p in parties.values()):
            raise Forbidden("Admins cannot act on their own or another admin's account", code="admin_target")

    async def decide(self, admin: Account, dispute_id: str, decision: DisputeDecision, release_pct: int | None,
                     reason: str) -> dict[str, Any]:
        """An admin decides: release, refund or split, with a logged reason. Funds stay frozen for the
        appeal window, then the decision is applied."""
        dispute = await self.repo(Dispute).lock(dispute_id, "Dispute not found")
        contract = await self.repo(Contract).require(dispute.contract_id)
        await self._guard_admin(admin, contract)
        if dispute.status not in (DisputeStatus.EVIDENCE, DisputeStatus.UNDER_REVIEW):
            raise Conflict(f"This dispute is {dispute.status}", code="invalid_dispute_status")
        dispute.release_pct = self._release_pct(decision, release_pct)
        dispute.decision, dispute.decision_reason = decision, reason
        dispute.decided_by, dispute.decided_at, dispute.status = admin.id, self.now(), DisputeStatus.DECIDED
        await self.svc.audit.log(admin.id, "dispute.decide", "dispute", dispute.id, reason,
                                 decision=decision, release_pct=dispute.release_pct)
        await self.svc.queues.close_for_target(Queue.DISPUTES, dispute.id, decision, admin.id, reason)
        for party in (contract.hirer_id, contract.earner_id):
            await self.svc.notifications.notify(
                party, NotificationType.DISPUTE, f"Dispute decided: {decision}",
                f"{reason} You can appeal once within {self.s.dispute_appeal_days} days.",
                {"dispute_id": dispute.id}, include_guardian=party == contract.earner_id)
        return self.present(dispute)

    async def appeal(self, account: Account, dispute_id: str, reason: str) -> dict[str, Any]:
        """Each side can appeal once; a Super Admin decides and that decision is final on-platform."""
        dispute, contract = await self._party(account, dispute_id)
        if account.id not in (contract.hirer_id, contract.earner_id):
            raise Forbidden("Only the two sides of the contract can appeal")
        if account.id in (dispute.appealed_by or []):
            raise Conflict("You have already appealed this dispute", code="appeal_used")
        if dispute.status != DisputeStatus.DECIDED:
            raise Conflict("Only a decided dispute that is not yet final can be appealed", code="invalid_dispute_status")
        dispute.appealed_by = [*(dispute.appealed_by or []), account.id]
        dispute.appeal_reason, dispute.status = reason.strip(), DisputeStatus.APPEALED
        await self.svc.queues.enqueue(Queue.DISPUTES, "dispute_appeal", dispute.id, summary=reason[:200], escalated=True)
        return self.present(dispute)

    async def decide_appeal(self, admin: Account, dispute_id: str, decision: DisputeDecision,
                            release_pct: int | None, reason: str) -> dict[str, Any]:
        if not admin.is_super_admin:
            raise Forbidden("Only a Super Admin decides dispute appeals", code="super_admin_required")
        dispute = await self.repo(Dispute).lock(dispute_id, "Dispute not found")
        contract = await self.repo(Contract).require(dispute.contract_id)
        await self._guard_admin(admin, contract)
        if dispute.status != DisputeStatus.APPEALED:
            raise Conflict(f"This dispute is {dispute.status}", code="invalid_dispute_status")
        dispute.release_pct = self._release_pct(decision, release_pct)
        dispute.decision, dispute.decision_reason, dispute.appeal_decided_by = decision, reason, admin.id
        await self.svc.audit.log(admin.id, "dispute.appeal_decide", "dispute", dispute.id, reason,
                                 decision=decision, release_pct=dispute.release_pct)
        await self.svc.queues.close_for_target(Queue.DISPUTES, dispute.id, decision, admin.id, reason)
        await self._apply(dispute, contract, admin.id)
        return self.present(dispute)

    async def _apply(self, dispute: Dispute, contract: Contract, actor_id: str | None) -> None:
        escrow = await self.svc.escrow.for_contract(contract.id)
        reason = f"Dispute {dispute.decision}: {dispute.decision_reason}"
        if escrow.state == EscrowState.FROZEN:
            await self.svc.escrow.settle(contract.id, dispute.release_pct, actor_id, reason)
        elif escrow.state == EscrowState.PAID and dispute.release_pct < 100:
            # Decided after payout (misuse claim): record what is owed back; nothing moves automatically.
            dispute.post_payout_adjustment = escrow.amount - pct_of(escrow.amount, dispute.release_pct)
            await self.repo(LedgerEntry).add(LedgerEntry(
                escrow_id=escrow.id, contract_id=contract.id, from_state=escrow.state, to_state=escrow.state,
                amount=dispute.post_payout_adjustment, actor_id=actor_id, reason=f"{reason} (post-payout recovery due)"))
            await self.svc.admin.alert_super_admins(
                "Post-payout dispute refund due", f"Contract {contract.id}: recover and refund "
                f"{dispute.post_payout_adjustment / 100:,.2f} {escrow.currency}.")
        dispute.status = DisputeStatus.FINAL
        contract.status = ContractStatus.CANCELLED if dispute.release_pct == 0 else ContractStatus.COMPLETED
        contract.completed_at = contract.completed_at or self.now()
        for party in (contract.hirer_id, contract.earner_id):
            await self.svc.notifications.notify(
                party, NotificationType.DISPUTE, "Dispute closed", f"Final outcome: {dispute.decision}.",
                {"dispute_id": dispute.id}, include_guardian=party == contract.earner_id)

    async def apply_unappealed(self) -> int:
        """Job: once the appeal window passes with no appeal, the decision takes effect."""
        cutoff = self.now() - timedelta(days=self.s.dispute_appeal_days)
        due = await self.repo(Dispute).list(Dispute.status == DisputeStatus.DECIDED, Dispute.decided_at <= cutoff)
        for dispute in due:
            await self._apply(dispute, await self.repo(Contract).require(dispute.contract_id), None)
        return len(due)

    async def has_open(self, account_id: str) -> bool:
        contracts = await self.repo(Contract).list(
            ((Contract.hirer_id == account_id) | (Contract.earner_id == account_id)),
            Contract.status == ContractStatus.DISPUTED)
        return bool(contracts)


class SubscriptionService(BaseService):
    """One plan: $10 per month or $100 per year. A lapse only switches off extras (SB-01)."""

    def _terms(self, period: BillingPeriod) -> tuple[int, int]:
        return (self.s.subscription_yearly_cents, 365) if period == BillingPeriod.YEARLY else (
            self.s.subscription_monthly_cents, 30)

    async def plans(self) -> dict[str, Any]:
        monthly, _ = await self.svc.fx.to_local(self.s.subscription_monthly_cents)
        yearly, rate = await self.svc.fx.to_local(self.s.subscription_yearly_cents)
        return {
            "monthly": {"usd_cents": self.s.subscription_monthly_cents, "local": monthly},
            "yearly": {"usd_cents": self.s.subscription_yearly_cents, "local": yearly, "note": "Two months free"},
            "currency": self.s.local_currency, "fx_rate": str(rate),
            "features": ["Daily post ideas", "Contact companies first about likeness", "Agent auto-replies",
                         "1 standard AI video minute free each month"],
        }

    def present(self, subscription: Subscription | None) -> dict[str, Any]:
        if subscription is None:
            return {"subscribed": False, "status": None}
        active = subscription.status != SubscriptionStatus.LAPSED and subscription.renews_at > self.now()
        return {"subscribed": active, "status": subscription.status, "period": subscription.period,
                "renews_at": subscription.renews_at.isoformat(),
                "ends_at": subscription.renews_at.isoformat() if subscription.status == SubscriptionStatus.CANCELLED else None}

    async def status(self, account: Account) -> dict[str, Any]:
        return self.present(await self.repo(Subscription).first(Subscription.account_id == account.id))

    async def subscribe(self, account: Account, period: BillingPeriod, payment_method: str) -> dict[str, Any]:
        if not account.is_user:
            raise Unprocessable("Subscriptions are for user accounts", code="not_a_user")
        subscription = await self.repo(Subscription).first(Subscription.account_id == account.id)
        if subscription and subscription.status == SubscriptionStatus.ACTIVE and subscription.renews_at > self.now():
            raise Conflict("You are already subscribed", code="already_subscribed")
        price, days = self._terms(period)
        await self.svc.payment.charge(account, PaymentPurpose.SUBSCRIPTION, price, payment_method)
        if subscription is None:
            subscription = await self.repo(Subscription).add(Subscription(
                account_id=account.id, period=period, started_at=self.now(), renews_at=self.now(),
                payment_method=payment_method))
        still_paid = subscription.renews_at if subscription.renews_at > self.now() else self.now()
        subscription.period, subscription.status = period, SubscriptionStatus.ACTIVE
        subscription.renews_at = still_paid + timedelta(days=days)
        subscription.payment_method, subscription.cancelled_at, subscription.lapse_notice_sent = payment_method, None, False
        await self.svc.profiles.invalidate(account)  # the subscriber badge shows at once
        return self.present(subscription)

    async def cancel(self, account: Account) -> dict[str, Any]:
        subscription = await self.repo(Subscription).first(Subscription.account_id == account.id)
        if subscription is None or subscription.status != SubscriptionStatus.ACTIVE:
            raise NotFound("You have no active subscription")
        subscription.status, subscription.cancelled_at = SubscriptionStatus.CANCELLED, self.now()
        return self.present(subscription)

    async def _lapse(self, subscription: Subscription) -> None:
        """Extras stop; paid contracts and earnings are never affected."""
        subscription.status = SubscriptionStatus.LAPSED
        await self.svc.profiles.invalidate(await self.repo(Account).require(subscription.account_id))  # badge goes
        profile = await self.svc.access.profile(subscription.account_id)
        if profile:
            profile.auto_reply_enabled = False
        await self.svc.notifications.notify(
            subscription.account_id, NotificationType.PAYMENT, "Your subscription has ended",
            "Daily ideas and agent auto-replies are off. Your contracts and earnings are unaffected.")

    async def process_renewals(self) -> dict[str, int]:
        """Job: renew active subscriptions, lapse cancelled or unpaid ones."""
        due = await self.repo(Subscription).list(
            Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELLED]),
            Subscription.renews_at <= self.now())
        renewed = lapsed = 0
        for subscription in due:
            account = await self.repo(Account).require(subscription.account_id)
            price, days = self._terms(BillingPeriod(subscription.period))
            if subscription.status == SubscriptionStatus.ACTIVE and account.is_active:
                try:
                    await self.svc.payment.charge(account, PaymentPurpose.SUBSCRIPTION, price, subscription.payment_method)
                    subscription.renews_at += timedelta(days=days)
                    subscription.lapse_notice_sent = False
                    renewed += 1
                    continue
                except PaymentRequired:
                    pass
            await self._lapse(subscription)
            lapsed += 1
        return {"renewed": renewed, "lapsed": lapsed}

    async def send_lapse_notices(self) -> int:
        """Job (AG-05): auto-replies stop on lapse, with notice the day before."""
        soon = await self.repo(Subscription).list(
            Subscription.status == SubscriptionStatus.CANCELLED, Subscription.lapse_notice_sent.is_(False),
            Subscription.renews_at <= self.now() + timedelta(days=1), Subscription.renews_at > self.now())
        for subscription in soon:
            subscription.lapse_notice_sent = True
            await self.svc.notifications.notify(
                subscription.account_id, NotificationType.PAYMENT, "Your subscription ends tomorrow",
                "Agent auto-replies and daily post ideas will stop. Renew to keep them.")
        return len(soon)
