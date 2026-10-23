"""HM-06 equity agreements after the offer: the $5 service fees on equity-only deals, the deal summary email,
vesting reminders, and cancellation through Wamiwa.

The equity's own state lives in the contract's `equity_terms`: "active" once accepted, "cancellation_requested"
while an admin reviews a request to end it, and "cancelled" once approved. Reminders go only to active agreements.
"""
import calendar
from datetime import datetime
from typing import Any

from app.core.enums import ContractStatus, NotificationType, PaymentPurpose, PaymentStatus, Queue
from app.core.exceptions import Conflict, NotFound, PaymentRequired, Unprocessable
from app.models import Account, Contract, Payment, QueueItem
from app.services.base import BaseService

ENDED = (ContractStatus.CANCELLED, ContractStatus.DECLINED, ContractStatus.EXPIRED)


def add_months(moment: datetime, months: int) -> datetime:
    """The same day `months` later, or the month's last day when it's shorter (31 Jan + 1 month = 28/29 Feb)."""
    month_index = moment.month - 1 + months
    year, month = moment.year + month_index // 12, month_index % 12 + 1
    return moment.replace(year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1]))


class EquityService(BaseService):
    # --- fees on equity-only deals ------------------------------------------------------------------
    async def charge_fee(self, account: Account, contract: Contract, payment_method: str | None) -> None:
        """Equity-only deals (no cash): each side pays Wamiwa a flat service fee."""
        if not payment_method:
            raise PaymentRequired(f"Equity-only deals have a ${self.s.equity_service_fee_cents / 100:.0f} service fee",
                                  code="payment_required", price_cents=self.s.equity_service_fee_cents)
        await self.svc.payment.charge(account, PaymentPurpose.EQUITY_FEE, self.s.equity_service_fee_cents,
                                      payment_method, reference_id=contract.id)

    async def refund_offer_fee(self, contract: Contract) -> None:
        """The brand's fee comes back if the offer is declined, withdrawn or expires before it's accepted."""
        payment = await self.repo(Payment).first(
            Payment.reference_id == contract.id, Payment.account_id == contract.hirer_id,
            Payment.purpose == PaymentPurpose.EQUITY_FEE, Payment.status == PaymentStatus.SUCCEEDED)
        if payment:
            await self.svc.payment.refund(payment.id)

    # --- the agreement ------------------------------------------------------------------------------
    def _set(self, contract: Contract, **changes: Any) -> dict[str, Any]:
        contract.equity_terms = {**contract.equity_terms, **changes}  # a new dict, so the change is saved
        return contract.equity_terms

    @staticmethod
    def _summary(contract: Contract) -> str:
        e = contract.equity_terms
        amount = f"{e['percent']}% equity" if e.get("percent") else f"{e['shares']:,} {e['share_class']} shares"
        vesting = (f"vesting over {e['vesting_months']} months" + (f" with a {e['cliff_months']}-month cliff"
                   if e.get("cliff_months") else "")) if e.get("vesting_months") else "no vesting period"
        cash = f"${contract.price_cents / 100:,.2f} cash (held in escrow)" if contract.price_cents else "no cash"
        return f"{amount}, {vesting}, plus {cash}"

    def schedule(self, contract: Contract) -> list[tuple[str, datetime]]:
        """When reminders are due: the end of the cliff (first shares vest) and the end of vesting (all vest)."""
        e = contract.equity_terms or {}
        if not contract.start or not e.get("vesting_months"):
            return []
        points = []
        if e.get("cliff_months") and e["cliff_months"] < e["vesting_months"]:
            points.append(("cliff", add_months(contract.start, e["cliff_months"])))
        points.append(("vested", add_months(contract.start, e["vesting_months"])))
        return points

    async def on_accepted(self, contract: Contract) -> None:
        """Both sides get an email summarising the deal, with the vesting dates."""
        terms = self._set(contract, status="active", reminders_sent=[])
        dates = "; ".join(f"{'first shares vest' if k == 'cliff' else 'fully vested'} on {d:%d %b %Y}"
                          for k, d in self.schedule(contract))
        body = (f"Deal agreed: {self._summary(contract)}. Share class: {terms['share_class']}. "
                + (f"Vesting: {dates}. We'll remind you both when shares vest. " if dates else "")
                + "Issuing and registering the shares is between you, under the agreement attached to the contract. "
                "Wamiwa doesn't hold, value or guarantee the equity.")
        for party in (contract.hirer_id, contract.earner_id):
            await self.svc.notifications.notify(party, NotificationType.CONTRACT, "Your equity deal is agreed", body,
                                                {"contract_id": contract.id})

    def is_active(self, contract: Contract) -> bool:
        return bool(contract.equity_terms and contract.start and contract.status not in ENDED
                    and contract.equity_terms.get("status") == "active")

    async def send_vesting_reminders(self) -> int:
        """Job: when shares are due to vest on an active agreement, remind the company and the creator. Cancelled
        or ended agreements, and ones waiting on a cancellation decision, get none."""
        sent = 0
        contracts = await self.repo(Contract).list(Contract.equity_terms.is_not(None), Contract.start.is_not(None),
                                                   limit=5000)
        for contract in contracts:
            if not self.is_active(contract):
                continue
            done = set(contract.equity_terms.get("reminders_sent") or [])
            for kind, due in self.schedule(contract):
                if kind in done or due > self.now():
                    continue
                what = "The first shares vest today" if kind == "cliff" else "All the shares have now vested"
                for party in (contract.hirer_id, contract.earner_id):
                    await self.svc.notifications.notify(
                        party, NotificationType.CONTRACT, f"Equity reminder: {what.lower()}",
                        f"{what} under your deal: {self._summary(contract)}. The company issues and registers the "
                        f"shares under the agreement.", {"contract_id": contract.id})
                done.add(kind)
                sent += 1
            self._set(contract, reminders_sent=sorted(done))
        return sent

    # --- cancelling the equity, approved by Wamiwa ---------------------------------------------------
    async def request_cancellation(self, account: Account, contract_id: str, reason: str) -> dict[str, Any]:
        contract = await self.svc.contracts.require_party(account, contract_id)
        if account.id not in (contract.hirer_id, contract.earner_id):
            raise NotFound("Contract not found")
        if not self.is_active(contract):
            if contract.equity_terms and contract.equity_terms.get("status") == "cancellation_requested":
                raise Conflict("A cancellation request is already with Wamiwa", code="cancellation_pending")
            raise Unprocessable("There's no active equity agreement on this contract", code="no_active_equity")
        self._set(contract, status="cancellation_requested", cancellation_requested_by=account.id,
                  cancellation_reason=reason.strip())
        await self.svc.queues.enqueue(Queue.REPORTS, "equity_cancellation", contract.id, subject_account_id=account.id,
                                      summary=f"Equity cancellation requested: {reason.strip()[:160]}")
        other = contract.earner_id if account.id == contract.hirer_id else contract.hirer_id
        await self.svc.notifications.notify(
            other, NotificationType.CONTRACT, "The other side asked to cancel your equity agreement",
            f"Reason given: {reason.strip()}. Wamiwa will review it and tell you both the outcome.",
            {"contract_id": contract.id})
        return self.svc.contracts.present(contract)

    async def apply_cancellation(self, item: QueueItem, approved: bool, admin: Account, reason: str,
                                 payload: dict) -> None:
        contract = await self.repo(Contract).require(item.target_id)
        self._set(contract, status="cancelled" if approved else "active", cancellation_decided_at=self.now().isoformat())
        title = "Your equity agreement is cancelled" if approved else "Your equity agreement continues"
        body = (f"Wamiwa approved the request to cancel the equity agreement. Vesting reminders stop. {reason}"
                if approved else f"Wamiwa didn't approve the request to cancel the equity agreement. {reason}")
        for party in (contract.hirer_id, contract.earner_id):
            await self.svc.notifications.notify(party, NotificationType.CONTRACT, title, body, {"contract_id": contract.id})
