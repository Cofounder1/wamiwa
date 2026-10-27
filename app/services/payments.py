"""Epic 6 money path: FX, charges, escrow state machine, payouts, chargebacks and reconciliation."""
import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from app.core.enums import (
    AccountStatus, ContractStatus, EscrowState, NotificationType, PaymentPurpose, PaymentStatus,
    PayoutStatus, Permission, Queue,
)
from app.core.exceptions import Conflict, Forbidden, NotFound, PaymentRequired, ServiceUnavailable, Unprocessable
from app.core.utils import Page, add_working_days, convert_minor, new_id, pct_of
from app.models import (
    Account, AdminPermission, Contract, EscrowTransaction, GuardianConfirmation, LedgerEntry, Payment,
    Payout, QueueItem, ReconciliationRun,
)
from app.infra.providers.base import reference_prefix
from app.services.base import BaseService, ServiceContext


class FxService(BaseService):
    BASE = "USD"
    FALLBACK_TTL = 15 * 60

    async def rate(self) -> Decimal:
        """Today's USD -> local rate. Cached for the day so every quote that day uses the same rate. If the provider
        can't give one, the last rate it gave is used, and admins are told once a day while it is getting old."""
        key = f"fx:{self.BASE}:{self.s.local_currency}:{self.clock.today().isoformat()}"
        last_key = f"fx:last:{self.BASE}:{self.s.local_currency}"

        cached = await self.cache.get(key)
        if cached is not None:
            return Decimal(cached)
        try:
            rate = str(await self.providers.fx.rate(self.BASE, self.s.local_currency))
        except Exception as exc:  # noqa: BLE001 - any provider failure falls back to the last rate
            rate = await self._last_rate(last_key, exc)
            await self.cache.set(key, rate, self.FALLBACK_TTL)  # ask the provider again soon
            return Decimal(rate)
        await self.kv.set(last_key, json.dumps({"rate": rate, "on": self.clock.today().isoformat()}))
        await self.cache.set(key, rate, self.s.cache_ttl_fx)
        return Decimal(rate)

    async def _last_rate(self, last_key: str, error: Exception) -> str:
        saved = await self.kv.get(last_key)
        if saved is None:
            raise ServiceUnavailable("Prices in Naira aren't available right now. Try again shortly.",
                                     code="fx_unavailable") from error
        last = json.loads(saved)
        age = (self.clock.today() - date.fromisoformat(last["on"])).days
        if age >= self.s.fx_stale_alert_days and await self.kv.set_if_absent(
                f"fx:stale-alert:{self.clock.today().isoformat()}", "1", 24 * 3600):
            await self.svc.admin.alert_admins(
                "Exchange rate is out of date",
                f"The payment provider gave no USD rate today ({error}). Prices use the rate from {last['on']}, "
                f"{age} days old: {last['rate']} {self.s.local_currency} per dollar.")
        return last["rate"]

    async def to_local(self, amount_cents: int) -> tuple[int, Decimal]:
        rate = await self.rate()
        return convert_minor(amount_cents, rate), rate


class PaymentService(BaseService):
    """Charges that are not escrow: subscriptions, AI video, production fees, plans and extras."""

    async def charge(self, account: Account, purpose: PaymentPurpose, amount_cents: int, payment_method: str,
                     reference_id: str | None = None) -> Payment:
        await self.limiter.enforce(self.policies.payments, account.id)
        amount_local, rate = await self.svc.fx.to_local(amount_cents)
        reference = f"{purpose}_{new_id()}"
        result = await self.providers.payments.charge(
            reference=reference, amount=amount_local, currency=self.s.local_currency,
            payment_method=payment_method, customer_id=account.id, usd_cents=amount_cents)
        if result.status == "failed":
            raise PaymentRequired(result.failure_reason or "Payment failed", code="payment_failed")
        await self.ensure_unused(result.provider_ref)
        self.svc.money.undo_charge_if_rolled_back(result.provider_ref, amount_local, self.s.local_currency, reference)
        return await self._record(account, purpose, amount_cents, amount_local, rate, result.provider_ref,
                                  result.status == "succeeded", reference_id, result.reusable_method)

    async def record_share(self, account: Account, purpose: PaymentPurpose, amount_cents: int, escrow: EscrowTransaction,
                           reference_id: str | None = None) -> Payment:
        """The part of an escrow payment that paid for something else (LK-10 video production with a likeness hire).
        It shares the escrow's provider charge, so refunding it refunds just this part of that charge."""
        contract = await self.repo(Contract).require(escrow.contract_id)
        rate = Decimal(str(contract.fx_rate))
        # Whatever the total converted to beyond the escrow's share, so the two always add up to the charge.
        amount_local = convert_minor(contract.price_cents + amount_cents, rate) - escrow.amount
        return await self._record(account, purpose, amount_cents, amount_local, rate, escrow.provider_ref,
                                  escrow.risk_cleared, reference_id, None)

    async def _record(self, account: Account, purpose: PaymentPurpose, amount_cents: int, amount_local: int, rate,
                      provider_ref: str | None, succeeded: bool, reference_id: str | None,
                      reusable_method: str | None) -> Payment:
        what = {"subscription": "your Wamiwa subscription", "ai_video": "an AI video", "video_production": "video production",
                "video_plan": "your video plan", "extra_render": "an extra final render", "arcon_filing": "ARCON filing",
                "promotion": "your Wamiwa Agent plan"}.get(str(purpose), str(purpose).replace("_", " "))
        await self.svc.notifications.notify(
            account.id, NotificationType.PAYMENT, f"Payment received: {amount_local / 100:,.2f} {self.s.local_currency}",
            f"Thank you. This payment is for {what}. It appears in your payment history.",
            {"reference_id": reference_id} if reference_id else None)
        return await self.repo(Payment).add(Payment(
            account_id=account.id, purpose=purpose, amount_cents=amount_cents, amount_local=amount_local,
            currency=self.s.local_currency, fx_rate=str(rate), provider_ref=provider_ref,
            status=PaymentStatus.SUCCEEDED if succeeded else PaymentStatus.PENDING_RISK,
            reference_id=reference_id, reusable_method=reusable_method))

    def is_international(self, account: Account) -> bool:
        """PY-05: anyone whose country isn't Wamiwa's home country pays and is paid in dollars."""
        return bool(account.region and account.region != self.s.home_region)

    def currency_for(self, account: Account) -> str:
        return "USD" if self.is_international(account) else self.s.local_currency

    async def test_mode(self) -> bool:
        """True until a Super Admin presses Go live on the live site; always true elsewhere (no real money)."""
        return not (self.s.is_production and await self.infra.launch.is_live())

    async def keys(self) -> dict[str, Any]:
        """The keys in use now: on the live site the test keys before launch and the live ones after."""
        s, test = self.s, await self.test_mode()
        if s.is_production and test:
            return {"flutterwave_secret": s.flutterwave_sandbox_secret_key, "flutterwave_public": s.flutterwave_sandbox_public_key,
                    "paypal_id": s.paypal_sandbox_client_id, "paypal_secret": s.paypal_sandbox_secret,
                    "paypal_environment": "sandbox", "test_mode": True}
        return {"flutterwave_secret": s.flutterwave_secret_key, "flutterwave_public": s.flutterwave_public_key,
                "paypal_id": s.paypal_client_id, "paypal_secret": s.paypal_secret,
                "paypal_environment": s.paypal_environment, "test_mode": test}

    async def method(self, account: Account) -> dict[str, Any]:
        """How this person can pay: Flutterwave's checkout, in Naira at home and US dollars elsewhere, or PayPal,
        always in dollars, once it is connected. `test_mode`: no real money moves (show a test card)."""
        k = await self.keys()
        connected = bool(k["flutterwave_secret"] and k["flutterwave_public"])
        paypal = bool(k["paypal_id"] and k["paypal_secret"])
        return {"provider": "flutterwave" if connected else "stand_in", "currency": self.currency_for(account),
                "public_key": k["flutterwave_public"] if connected else None, "available": connected,
                "test_mode": k["test_mode"],
                "paypal": {"available": paypal, "currency": "USD", "client_id": k["paypal_id"] if paypal else None,
                           "environment": k["paypal_environment"] if paypal else None}}

    async def start_checkout(self, account: Account, amount_cents: int, description: str) -> dict[str, Any]:
        """What the checkout window needs. The reference carries the account id, and the amount is checked again
        against the real price when the payment is used, so nothing here can be tampered with."""
        await self.limiter.enforce(self.policies.payments, account.id)
        currency = self.currency_for(account)
        amount = amount_cents if currency == "USD" else (await self.svc.fx.to_local(amount_cents))[0]
        return {"tx_ref": f"{reference_prefix(account.id)}{new_id()}", "amount": f"{amount / 100:.2f}",
                "currency": currency, "public_key": (await self.keys())["flutterwave_public"],
                "description": description[:100],
                "customer": {"email": account.email, "name": account.display_name}}

    async def start_paypal_order(self, account: Account, amount_cents: int, description: str) -> dict[str, Any]:
        """Step 1 of paying with PayPal: an order for the price in dollars, for the payer to approve. It carries the
        account id, and its amount is checked again against the real price when it is used."""
        await self.limiter.enforce(self.policies.payments, account.id)
        order_id = await self.providers.paypal.create_order(
            amount_cents=amount_cents, description=description or "Wamiwa", reference=f"{reference_prefix(account.id)}{new_id()}")
        return {"order_id": order_id, "payment_method": f"paypal:{order_id}", "amount": f"{amount_cents / 100:.2f}",
                "currency": "USD"}

    @staticmethod
    def renewal_method(payment: Payment, given: str) -> str:
        """What to charge next time: the card the provider saved, not the one-time transaction just used."""
        return payment.reusable_method or given

    async def ensure_unused(self, provider_ref: str | None) -> None:
        """One payment pays for one thing: a transaction already recorded can't be sent again for something else."""
        if provider_ref and (await self.repo(Payment).exists(Payment.provider_ref == provider_ref)
                             or await self.repo(EscrowTransaction).exists(EscrowTransaction.provider_ref == provider_ref)):
            raise Conflict("This payment has already been used", code="payment_already_used")

    async def refund(self, payment_id: str | None, amount_local: int | None = None) -> Payment | None:
        if not payment_id:
            return None
        payment = await self.repo(Payment).lock(payment_id)
        remaining = payment.amount_local - payment.refunded_local
        amount = remaining if amount_local is None else min(amount_local, remaining)
        if amount <= 0:
            return payment
        # Recorded now, sent to the provider once this transaction commits (and retried until it goes through).
        await self.svc.money.queue_refund(payment.provider_ref, amount, payment.currency, payment_id=payment.id)
        payment.refunded_local += amount
        if payment.refunded_local >= payment.amount_local:
            payment.status = PaymentStatus.REFUNDED
        await self.svc.money.notify_refund(payment.account_id, amount, payment.currency)
        return payment

    async def history(self, account: Account, cursor: str | None, limit: int) -> Page[dict]:
        page = await self.repo(Payment).page(Payment.account_id == account.id, cursor=cursor, limit=limit)
        return Page(items=[{
            "id": p.id, "purpose": p.purpose, "amount_cents": p.amount_cents, "amount_local": p.amount_local,
            "currency": p.currency, "fx_rate": p.fx_rate, "status": p.status, "refunded_local": p.refunded_local,
            "created_at": p.created_at.isoformat()} for p in page.items], next_cursor=page.next_cursor)


class EscrowService(BaseService):
    """The escrow state machine. Every change goes through `_transition`, which writes the ledger."""

    TRANSITIONS: dict[EscrowState, tuple[EscrowState, ...]] = {
        EscrowState.FUNDED: (EscrowState.ACTIVE, EscrowState.REFUNDED),
        EscrowState.ACTIVE: (EscrowState.DELIVERED, EscrowState.CLEARED, EscrowState.REFUNDED, EscrowState.FROZEN),
        EscrowState.DELIVERED: (EscrowState.CLEARED, EscrowState.FROZEN, EscrowState.REFUNDED, EscrowState.ACTIVE),
        EscrowState.CLEARED: (EscrowState.PAID, EscrowState.FROZEN),
        EscrowState.FROZEN: (EscrowState.CLEARED, EscrowState.REFUNDED, EscrowState.ACTIVE, EscrowState.DELIVERED),
        EscrowState.PAID: (),
        EscrowState.REFUNDED: (),
    }

    def present(self, escrow: EscrowTransaction) -> dict[str, Any]:
        return {
            "id": escrow.id, "contract_id": escrow.contract_id, "state": escrow.state, "amount": escrow.amount,
            "currency": escrow.currency, "fee_pct": escrow.fee_pct, "released_gross": escrow.released_gross,
            "fee_amount": escrow.fee_amount, "net_amount": escrow.net_amount,
            "refunded_amount": escrow.refunded_amount, "risk_cleared": escrow.risk_cleared,
            "cleared_at": escrow.cleared_at.isoformat() if escrow.cleared_at else None,
            "paid_at": escrow.paid_at.isoformat() if escrow.paid_at else None,
            "refund_due_by": escrow.refund_due_by.isoformat() if escrow.refund_due_by else None,
        }

    async def for_contract(self, contract_id: str) -> EscrowTransaction:
        escrow = await self.repo(EscrowTransaction).first(EscrowTransaction.contract_id == contract_id)
        if escrow is None:
            raise NotFound("Escrow not found")
        return await self.repo(EscrowTransaction).lock(escrow.id)

    async def _transition(self, escrow: EscrowTransaction, to_state: EscrowState, actor_id: str | None,
                          reason: str, amount: int = 0) -> EscrowTransaction:
        current = EscrowState(escrow.state)
        if to_state not in self.TRANSITIONS[current]:
            raise Conflict(f"Escrow cannot move from {current} to {to_state}", code="invalid_escrow_transition")
        await self.repo(LedgerEntry).add(LedgerEntry(
            escrow_id=escrow.id, contract_id=escrow.contract_id, from_state=current, to_state=to_state,
            amount=amount, actor_id=actor_id, reason=reason))
        escrow.state = to_state
        return escrow

    async def fund(self, contract: Contract, hirer: Account, payment_method: str, *,
                   also_charge_cents: int = 0) -> EscrowTransaction:
        """The hirer pays the full price when sending the offer; the day's FX rate is locked on the contract.
        `also_charge_cents` is charged in the same payment, since a checkout pays one exact amount; the escrow holds
        only the contract's share and the rest is recorded by `PaymentService.record_share`."""
        await self.limiter.enforce(self.policies.payments, hirer.id)
        rate = Decimal(str(contract.fx_rate))
        amount = convert_minor(contract.price_cents, rate)
        total = convert_minor(contract.price_cents + also_charge_cents, rate)
        result = await self.providers.payments.charge(
            reference=f"escrow_{contract.id}", amount=total, currency=contract.currency,
            payment_method=payment_method, customer_id=hirer.id, usd_cents=contract.price_cents + also_charge_cents)
        if result.status == "failed":
            raise PaymentRequired(result.failure_reason or "Payment failed", code="payment_failed")
        await self.svc.payment.ensure_unused(result.provider_ref)
        self.svc.money.undo_charge_if_rolled_back(result.provider_ref, total, contract.currency, f"escrow_{contract.id}")
        escrow = await self.repo(EscrowTransaction).add(EscrowTransaction(
            contract_id=contract.id, hirer_id=contract.hirer_id, earner_id=contract.earner_id, amount=amount,
            currency=contract.currency, fee_pct=contract.fee_pct, provider_ref=result.provider_ref,
            # PY-03: card payments clear only after the provider's risk check.
            risk_cleared=result.status == "succeeded"))
        await self.repo(LedgerEntry).add(LedgerEntry(
            escrow_id=escrow.id, contract_id=contract.id, from_state=None, to_state=EscrowState.FUNDED,
            amount=amount, actor_id=hirer.id, reason="Hirer funded escrow"))
        await self.svc.notifications.notify(
            hirer.id, NotificationType.PAYMENT, f"Payment received: {amount / 100:,.2f} {contract.currency}",
            "Wamiwa holds this in escrow. It is released only when the work is approved, and refunded if the offer "
            "is declined or expires.", {"contract_id": contract.id})
        return escrow

    async def activate(self, contract_id: str, actor_id: str | None) -> EscrowTransaction:
        return await self._transition(await self.for_contract(contract_id), EscrowState.ACTIVE, actor_id, "Contract active")

    async def mark_delivered(self, contract_id: str, actor_id: str | None) -> EscrowTransaction:
        return await self._transition(await self.for_contract(contract_id), EscrowState.DELIVERED, actor_id, "Work delivered")

    async def reopen(self, contract_id: str, actor_id: str | None, reason: str) -> EscrowTransaction:
        return await self._transition(await self.for_contract(contract_id), EscrowState.ACTIVE, actor_id, reason)

    async def freeze(self, contract_id: str, actor_id: str, reason: str) -> EscrowTransaction:
        escrow = await self.for_contract(contract_id)
        escrow.state_before_freeze = escrow.state
        return await self._transition(escrow, EscrowState.FROZEN, actor_id, reason)

    async def settle(self, contract_id: str, release_pct: int | float, actor_id: str | None, reason: str) -> EscrowTransaction:
        """Release `release_pct` of the price to the earner (less Wamiwa's fee) and refund the rest.

        100 = full release, 0 = full refund, anything between is a split.
        """
        if not 0 <= release_pct <= 100:
            raise Unprocessable("Release percentage must be between 0 and 100", code="invalid_split")
        escrow = await self.for_contract(contract_id)
        released = pct_of(escrow.amount, release_pct)
        refund = escrow.amount - released
        if refund > 0:
            await self.svc.money.queue_refund(escrow.provider_ref, refund, escrow.currency, escrow_id=escrow.id)
            escrow.refund_due_by = add_working_days(self.now(), self.s.refund_working_days)  # PY-03
        escrow.released_gross = released
        escrow.fee_amount = pct_of(released, escrow.fee_pct)  # PY-01: the fee comes out of the price
        escrow.net_amount = released - escrow.fee_amount
        escrow.refunded_amount = refund
        if released == 0:
            await self._transition(escrow, EscrowState.REFUNDED, actor_id, reason, refund)
        else:
            escrow.cleared_at = self.now()
            await self._transition(escrow, EscrowState.CLEARED, actor_id, reason, released)
        if refund > 0:
            await self.svc.notifications.notify(
                escrow.hirer_id, NotificationType.PAYMENT, "Refund on its way",
                f"{refund / 100:,.2f} {escrow.currency} will reach you within {self.s.refund_working_days} working days.",
                {"contract_id": contract_id})
        if released > 0:
            await self.svc.notifications.notify(
                escrow.earner_id, NotificationType.PAYMENT, "Funds cleared",
                f"{escrow.net_amount / 100:,.2f} {escrow.currency} will be paid in the next payout cycle.",
                {"contract_id": contract_id}, include_guardian=True)
        return escrow

    async def earnings(self, earner_id: str) -> dict[str, Any]:
        """PY-02 dashboard: pending, cleared and paid amounts."""
        rows = (await self.db.execute(
            select(EscrowTransaction.state, EscrowTransaction.currency, func.sum(EscrowTransaction.amount),
                   func.sum(EscrowTransaction.net_amount), func.max(EscrowTransaction.fee_pct))
            .where(EscrowTransaction.earner_id == earner_id)
            .group_by(EscrowTransaction.state, EscrowTransaction.currency))).all()
        totals = {"pending": 0, "cleared": 0, "paid": 0, "in_dispute": 0}
        fees = await self.db.execute(
            select(EscrowTransaction.amount, EscrowTransaction.fee_pct, EscrowTransaction.state)
            .where(EscrowTransaction.earner_id == earner_id,
                   EscrowTransaction.state.in_([EscrowState.FUNDED, EscrowState.ACTIVE, EscrowState.DELIVERED,
                                                EscrowState.FROZEN])))
        for amount, fee_pct, state in fees.all():
            net = amount - pct_of(amount, fee_pct)
            totals["in_dispute" if state == EscrowState.FROZEN else "pending"] += net
        for state, _currency, _amount, net, _fee in rows:
            if state == EscrowState.CLEARED:
                totals["cleared"] += int(net or 0)
            elif state == EscrowState.PAID:
                totals["paid"] += int(net or 0)
        return {"currency": self.s.local_currency, **totals,
                "next_payout_cycle": (await self.svc.payouts.next_cycle_date()).isoformat()}

    async def has_open_funds(self, account_id: str) -> bool:
        """DL-01 blocker: money still in escrow for this account on either side."""
        open_states = [EscrowState.FUNDED, EscrowState.ACTIVE, EscrowState.DELIVERED, EscrowState.FROZEN, EscrowState.CLEARED]
        return await self.repo(EscrowTransaction).exists(
            (EscrowTransaction.hirer_id == account_id) | (EscrowTransaction.earner_id == account_id),
            EscrowTransaction.state.in_(open_states))


class PayoutService(BaseService):
    def present(self, payout: Payout) -> dict[str, Any]:
        return {
            "id": payout.id, "account_id": payout.account_id, "earner_id": payout.earner_id,
            "cycle_date": payout.cycle_date.isoformat(), "amount": payout.amount, "currency": payout.currency,
            "status": payout.status, "manual": payout.manual, "needs_second_approval": payout.needs_second_approval,
            "approved_by": payout.approved_by, "second_approved_by": payout.second_approved_by,
            "failure_reason": payout.failure_reason,
            "paid_at": payout.paid_at.isoformat() if payout.paid_at else None,
        }

    async def _last_cycle(self):
        return (await self.db.execute(
            select(func.max(Payout.cycle_date)).where(Payout.manual.is_(False)))).scalar_one_or_none()

    async def next_cycle_date(self):
        last = await self._last_cycle()
        today = self.clock.today()
        if last is None:
            return today
        return max(today, last + timedelta(days=self.s.payout_cycle_days))

    async def recipient_for(self, escrow: EscrowTransaction) -> str:
        """PY-02: a minor's payouts go to the guardian who confirmed the contract."""
        contract = await self.repo(Contract).require(escrow.contract_id)
        if contract.requires_guardian:
            confirmation = await self.repo(GuardianConfirmation).first(GuardianConfirmation.contract_id == contract.id)
            if confirmation:
                return confirmation.guardian_id
        return escrow.earner_id

    async def _cleared(self, earner_id: str | None = None) -> list[EscrowTransaction]:
        where = [EscrowTransaction.state == EscrowState.CLEARED, EscrowTransaction.payout_id.is_(None),
                 EscrowTransaction.risk_cleared.is_(True), EscrowTransaction.charged_back.is_(False),
                 EscrowTransaction.net_amount > 0]
        if earner_id:
            where.append(EscrowTransaction.earner_id == earner_id)
        return list(await self.repo(EscrowTransaction).list(*where))

    async def run_cycle(self, force: bool = False) -> dict[str, Any]:
        """Job: every 2 weeks, pay cleared funds to each earner's verified bank account."""
        today = self.clock.today()
        last = await self._last_cycle()
        if not force and last is not None and (today - last).days < self.s.payout_cycle_days:
            return {"ran": False, "next_cycle_date": (last + timedelta(days=self.s.payout_cycle_days)).isoformat()}
        groups: dict[tuple[str, str], list[EscrowTransaction]] = {}
        for escrow in await self._cleared():
            groups.setdefault((await self.recipient_for(escrow), escrow.earner_id), []).append(escrow)
        summary = {"ran": True, "sending": 0, "held": 0, "cycle_date": today.isoformat()}
        for (recipient_id, earner_id), escrows in groups.items():
            payout = await self.repo(Payout).add(Payout(
                account_id=recipient_id, earner_id=earner_id, cycle_date=today,
                amount=sum(e.net_amount for e in escrows), currency=escrows[0].currency))
            await self._execute(payout, escrows)
            summary["sending" if payout.status == PayoutStatus.PROCESSING else "held"] += 1
        if not groups:
            # Record the cycle so the fortnightly schedule keeps its anchor even with nothing to pay.
            summary["note"] = "No cleared funds to pay"
        return summary

    async def _execute(self, payout: Payout, escrows: list[EscrowTransaction]) -> Payout:
        """Decide and record the payout. The bank transfer itself happens only after this is committed,
        so a failed request can never have sent money that the records do not show."""
        recipient = await self.repo(Account).require(payout.account_id)
        bank, blocked_reason = await self.svc.bank_accounts.payable(payout.account_id)
        if recipient.status != AccountStatus.ACTIVE:
            bank, blocked_reason = None, "Account is not active"
        if bank is None:
            payout.status, payout.failure_reason = PayoutStatus.HELD, blocked_reason
            for escrow in escrows:
                escrow.payout_id = None
            await self.svc.notifications.notify(payout.account_id, NotificationType.PAYMENT, "Payout on hold",
                                                blocked_reason or "")
            return payout
        payout.status = PayoutStatus.PROCESSING
        for escrow in escrows:
            escrow.payout_id = payout.id  # reserved for this payout
        infra, payout_id = self.infra, payout.id

        async def send() -> None:
            async with infra.db.unit_of_work() as uow:
                await ServiceContext(infra, uow).services.payouts.complete(payout_id)

        self.uow.on_commit(send)
        return payout

    async def complete(self, payout_id: str) -> Payout | None:
        """Make the transfer for a recorded payout. Safe to repeat: the provider sees the same reference."""
        payout = await self.repo(Payout).get(payout_id)
        if payout is None or payout.status != PayoutStatus.PROCESSING:
            return payout
        payout = await self.repo(Payout).lock(payout_id)
        bank, blocked_reason = await self.svc.bank_accounts.payable(payout.account_id)
        escrows = list(await self.repo(EscrowTransaction).list(EscrowTransaction.payout_id == payout.id))
        result = None
        if bank is not None:
            amount, currency = payout.amount, payout.currency
            if bank.bank_code == "intl":  # PY-05: foreign banks are paid in dollars at the day's rate
                rate = await self.svc.fx.rate()  # the day's rate, as for every other conversion
                amount, currency = int((Decimal(payout.amount) / rate).quantize(Decimal("1"))), "USD"
            result = await self.providers.payments.transfer(
                reference=f"payout_{payout.id}", amount=amount, currency=currency, bank_code=bank.bank_code,
                account_number=bank.account_number, account_name=bank.account_name, details=bank.details)
        if result is None or not result.succeeded:
            payout.status = PayoutStatus.HELD if bank is None else PayoutStatus.FAILED
            payout.failure_reason = blocked_reason if bank is None else result.failure_reason
            for escrow in escrows:
                escrow.payout_id = None  # stays cleared and is paid in the next cycle
            return payout
        payout.status, payout.provider_ref, payout.paid_at = PayoutStatus.PAID, result.provider_ref, self.now()
        for escrow in escrows:
            locked = await self.repo(EscrowTransaction).lock(escrow.id)
            if locked.state == EscrowState.PAID:
                continue  # already recorded on an earlier attempt
            locked.paid_at = self.now()
            await self.svc.escrow._transition(locked, EscrowState.PAID, payout.approved_by, f"Payout {payout.id}",
                                              locked.net_amount)
        await self.svc.notifications.notify(
            payout.account_id, NotificationType.PAYMENT, "Payout sent",
            f"{payout.amount / 100:,.2f} {payout.currency} was sent to your bank account.", {"payout_id": payout.id})
        if payout.earner_id != payout.account_id:
            await self.svc.notifications.notify(
                payout.earner_id, NotificationType.PAYMENT, "Your earnings were paid to your guardian",
                f"{payout.amount / 100:,.2f} {payout.currency}", {"payout_id": payout.id})
        return payout

    async def finish_stuck(self) -> int:
        """Job: a payout recorded but not confirmed (the server stopped mid-way) is completed. The
        provider recognises the reference, so a transfer that did go through is not sent twice."""
        stuck = await self.repo(Payout).list(
            Payout.status == PayoutStatus.PROCESSING, Payout.updated_at <= self.now() - timedelta(minutes=10))
        for payout in stuck:
            await self.complete(payout.id)
        return len(stuck)

    # --- manual payouts (PY-02, AD-03) -------------------------------------------------------
    async def _payout_limit(self, admin: Account) -> int | None:
        """None = unlimited (Super Admin); 0 = not allowed."""
        if admin.is_super_admin:
            return None
        grant = await self.repo(AdminPermission).first(
            AdminPermission.admin_id == admin.id, AdminPermission.permission == Permission.APPROVE_PAYOUTS)
        return (grant.payout_limit or 0) if grant else 0

    async def request_manual(self, admin: Account, earner_id: str, reason: str) -> dict[str, Any]:
        limit = await self._payout_limit(admin)
        if limit == 0:
            raise Forbidden("You have not been granted payout approval", code="permission_denied")
        escrows = await self._cleared(earner_id)
        if not escrows:
            raise Unprocessable("This account has no cleared funds", code="nothing_to_pay")
        amount = sum(e.net_amount for e in escrows)
        over_admin_limit = limit is not None and amount > limit
        dual = amount > self.s.manual_payout_dual_approval_minor_units
        payout = await self.repo(Payout).add(Payout(
            account_id=await self.recipient_for(escrows[0]), earner_id=earner_id, cycle_date=self.clock.today(),
            amount=amount, currency=escrows[0].currency, manual=True, requested_by=admin.id, reason=reason,
            approved_by=None if over_admin_limit else admin.id, needs_second_approval=dual or over_admin_limit,
            status=PayoutStatus.PENDING_APPROVAL if (dual or over_admin_limit) else PayoutStatus.APPROVED))
        for escrow in escrows:
            escrow.payout_id = payout.id  # reserve the funds while approval is pending
        await self.svc.audit.log(admin.id, "payout.manual_request", "payout", payout.id, reason, amount=amount)
        if payout.status == PayoutStatus.PENDING_APPROVAL:
            await self.svc.queues.enqueue(Queue.PAYOUTS, "payout", payout.id, subject_account_id=earner_id,
                                          summary=f"Manual payout of {amount / 100:,.2f} {payout.currency} needs approval",
                                          escalated=over_admin_limit)
        else:
            await self._execute(payout, escrows)
        return self.present(payout)

    async def apply_review(self, item: QueueItem, approved: bool, admin: Account, reason: str, payload: dict) -> None:
        """A manual payout above the set amount needs approval from two different admins."""
        payout = await self.repo(Payout).lock(item.target_id)
        if payout.status != PayoutStatus.PENDING_APPROVAL:
            raise Conflict("This payout is no longer awaiting approval", code="not_pending")
        escrows = list(await self.repo(EscrowTransaction).list(EscrowTransaction.payout_id == payout.id))
        if not approved:
            payout.status, payout.failure_reason = PayoutStatus.REJECTED, reason
            for escrow in escrows:
                escrow.payout_id = None
            return
        if admin.id in (payout.requested_by, payout.approved_by):
            raise Forbidden("A second, different admin must approve this payout", code="same_approver")
        limit = await self._payout_limit(admin)
        if limit == 0 or (limit is not None and payout.amount > limit):
            raise Forbidden("This payout is above your approval limit", code="over_limit")
        if payout.approved_by is None:
            payout.approved_by = admin.id
        else:
            payout.second_approved_by = admin.id
        payout.status = PayoutStatus.APPROVED
        await self._execute(payout, escrows)

    async def list_for(self, account_id: str, cursor: str | None, limit: int) -> Page[dict]:
        page = await self.repo(Payout).page(
            (Payout.account_id == account_id) | (Payout.earner_id == account_id), cursor=cursor, limit=limit)
        return Page(items=[self.present(p) for p in page.items], next_cursor=page.next_cursor)


class PaymentEventService(BaseService):
    """Provider webhooks: risk-check results and chargebacks (PY-03)."""

    async def handle(self, event_type: str, provider_ref: str) -> dict[str, Any]:
        escrow = await self.repo(EscrowTransaction).first(EscrowTransaction.provider_ref == provider_ref)
        payment = await self.repo(Payment).first(Payment.provider_ref == provider_ref)
        if escrow is None and payment is None:
            raise NotFound("Unknown charge")
        if event_type == "charge.risk_cleared":
            if escrow:
                escrow.risk_cleared = True
            if payment and payment.status == PaymentStatus.PENDING_RISK:
                payment.status = PaymentStatus.SUCCEEDED
        elif event_type == "charge.chargeback":
            await self._chargeback(escrow, payment)
        else:
            raise Unprocessable("Unsupported event type", code="unsupported_event")
        return {"handled": event_type}

    async def _chargeback(self, escrow: EscrowTransaction | None, payment: Payment | None) -> None:
        """Freeze the brand; the amount is pursued from the brand, never clawed back from the earner."""
        brand_id = escrow.hirer_id if escrow else payment.account_id
        brand = await self.repo(Account).require(brand_id)
        if brand.status == AccountStatus.ACTIVE:
            brand.status_before_hold, brand.status = brand.status, AccountStatus.FROZEN
            brand.suspended_reason = "Chargeback under investigation"
        if payment:
            payment.status = PaymentStatus.CHARGED_BACK
        if escrow:
            escrow = await self.repo(EscrowTransaction).lock(escrow.id)
            escrow.charged_back = True
            contract = await self.repo(Contract).require(escrow.contract_id)
            if escrow.state == EscrowState.PAID:
                await self.repo(LedgerEntry).add(LedgerEntry(
                    escrow_id=escrow.id, contract_id=escrow.contract_id, from_state=escrow.state,
                    to_state=escrow.state, amount=escrow.amount, actor_id=None,
                    reason="Chargeback after payout: amount is owed by the brand"))
            elif escrow.state != EscrowState.REFUNDED:
                # The provider already returned the money to the cardholder, so no refund call is made.
                if escrow.state == EscrowState.CLEARED:
                    escrow.state = EscrowState.FROZEN
                    escrow.released_gross = escrow.fee_amount = escrow.net_amount = 0
                    escrow.cleared_at = None
                escrow.refunded_amount = escrow.amount
                await self.svc.escrow._transition(escrow, EscrowState.REFUNDED, None, "Chargeback", escrow.amount)
                if contract.status not in (ContractStatus.COMPLETED, ContractStatus.CANCELLED):
                    contract.status, contract.cancel_reason = ContractStatus.CANCELLED, "Chargeback by hirer"
                await self.svc.notifications.notify(
                    escrow.earner_id, NotificationType.PAYMENT, "A contract was cancelled",
                    "The hirer's payment was reversed by their bank.", {"contract_id": contract.id},
                    include_guardian=True)
        await self.svc.notifications.notify(
            brand.id, NotificationType.PAYMENT, "Your account is frozen",
            "A payment was charged back. Contact support to resolve it.")
        await self.svc.admin.alert_super_admins(
            "Chargeback received", f"Brand @{brand.handle} is frozen pending investigation.")


class ReconciliationService(BaseService):
    async def run(self) -> ReconciliationRun:
        """Job (section 20): escrow balances reconcile with the provider daily; mismatches alert Super Admins."""
        currency = self.s.local_currency

        def local(column):  # dollar payments (Flutterwave's dollar balance, PayPal) are reconciled separately
            return column.is_(None) | ~(column.like("flwusd:%") | column.like("paypal:%"))

        held = await self.repo(EscrowTransaction).sum(EscrowTransaction.amount, EscrowTransaction.currency == currency,
                                                      local(EscrowTransaction.provider_ref))
        refunded = await self.repo(EscrowTransaction).sum(
            EscrowTransaction.refunded_amount, EscrowTransaction.currency == currency,
            EscrowTransaction.charged_back.is_(False), local(EscrowTransaction.provider_ref))
        charged_back = await self.repo(EscrowTransaction).sum(
            EscrowTransaction.amount, EscrowTransaction.currency == currency, EscrowTransaction.charged_back.is_(True),
            local(EscrowTransaction.provider_ref))
        paid_out = await self.repo(Payout).sum(Payout.amount, Payout.status == PayoutStatus.PAID, Payout.currency == currency,
                                               local(Payout.provider_ref))
        charges = await self.repo(Payment).sum(
            Payment.amount_local - Payment.refunded_local, Payment.currency == currency, local(Payment.provider_ref),
            Payment.status.in_([PaymentStatus.SUCCEEDED, PaymentStatus.PENDING_RISK, PaymentStatus.REFUNDED]))
        # Refunds already decided but not yet made at the provider are still in its balance.
        expected = held - refunded - charged_back - paid_out + charges + await self.svc.money.pending_total(currency)
        actual = await self.providers.payments.held_balance(currency)
        run = await self.repo(ReconciliationRun).add(ReconciliationRun(
            currency=currency, expected=expected, actual=actual, matched=expected == actual,
            difference=actual - expected))
        if not run.matched:
            await self.svc.admin.alert_super_admins(
                "Escrow reconciliation mismatch",
                f"Expected {expected / 100:,.2f} {currency}, provider holds {actual / 100:,.2f} {currency}.")
        return run
