"""Refunds and charge reversals that are safe against failed requests and retries."""
import logging
from datetime import datetime, timedelta

from app.core.enums import NotificationType
from app.models import Account, EscrowTransaction, MoneyMovement, Payment
from app.services.base import BaseService, ServiceContext

log = logging.getLogger("wamiwa.money")
MAX_ATTEMPTS = 5
BACKOFF_MINUTES = (1, 5, 15, 60)


class MoneyMovementService(BaseService):
    async def queue_refund(self, charge_ref: str, amount: int, currency: str, *, payment_id: str | None = None,
                           escrow_id: str | None = None) -> MoneyMovement:
        """Record a refund now; it is sent to the provider only after this transaction commits."""
        movement = await self.repo(MoneyMovement).add(MoneyMovement(
            kind="refund", reference="", charge_ref=charge_ref, amount=amount, currency=currency,
            payment_id=payment_id, escrow_id=escrow_id, next_attempt_at=self.now()))
        movement.reference = f"refund_{movement.id}"
        self._run_after_commit(movement.id)
        return movement

    def _run_after_commit(self, movement_id: str) -> None:
        infra = self.infra

        async def run() -> None:
            async with infra.db.unit_of_work() as uow:
                await ServiceContext(infra, uow).services.money.execute(movement_id)

        self.uow.on_commit(run)

    def undo_charge_if_rolled_back(self, charge_ref: str, amount: int, currency: str, reference: str) -> None:
        """A card was charged inside this request. If the request then fails and nothing is saved, give the
        money straight back, so a customer is never charged for something we have no record of."""
        infra = self.infra

        async def undo() -> None:
            result = await infra.providers.payments.refund(provider_ref=charge_ref, amount=amount,
                                                           reference=f"void_{reference}")
            if result.succeeded:
                log.warning("charge %s reversed because its request failed", charge_ref)
                return
            # The reversal itself failed: keep it as a pending refund so the retry job keeps trying.
            async with infra.db.unit_of_work() as uow:
                services = ServiceContext(infra, uow).services
                await services.money.repo(MoneyMovement).add(MoneyMovement(
                    kind="void", reference=f"void_{reference}", charge_ref=charge_ref, amount=amount,
                    currency=currency, next_attempt_at=services.money.now(), last_error=result.failure_reason))
                await services.admin.alert_admins(
                    "A charge could not be reversed automatically",
                    f"Charge {charge_ref} ({amount / 100:,.2f} {currency}) belongs to a failed request. Retrying.")

        self.uow.on_rollback(undo)

    async def execute(self, movement_id: str) -> MoneyMovement | None:
        movement = await self.repo(MoneyMovement).get(movement_id)
        if movement is None or movement.status != "pending":
            return movement
        movement = await self.repo(MoneyMovement).lock(movement_id)
        movement.attempts += 1
        result = await self.providers.payments.refund(provider_ref=movement.charge_ref, amount=movement.amount,
                                                      reference=movement.reference)
        if result.succeeded:
            movement.status, movement.provider_ref, movement.executed_at = "succeeded", result.provider_ref, self.now()
            movement.next_attempt_at = None
            return movement
        movement.last_error = result.failure_reason
        if movement.attempts >= MAX_ATTEMPTS:
            movement.status, movement.next_attempt_at = "failed", None
            await self.svc.admin.alert_admins(
                "A refund could not be made", f"{movement.amount / 100:,.2f} {movement.currency} on charge "
                f"{movement.charge_ref} failed {movement.attempts} times: {result.failure_reason}. Needs a person.")
        else:
            delay = BACKOFF_MINUTES[min(movement.attempts - 1, len(BACKOFF_MINUTES) - 1)]
            movement.next_attempt_at = self.now() + timedelta(minutes=delay)
        return movement

    async def retry_due(self) -> int:
        """Job: try again any refund that has not gone through yet."""
        due = await self.repo(MoneyMovement).list(
            MoneyMovement.status == "pending", MoneyMovement.next_attempt_at <= self.now(), limit=200)
        for movement in due:
            await self.execute(movement.id)
        return len(due)

    async def pending_total(self, currency: str) -> int:
        """Refunds decided but not yet made: the provider still holds that money."""
        return await self.repo(MoneyMovement).sum(
            MoneyMovement.amount, MoneyMovement.status == "pending", MoneyMovement.currency == currency,
            MoneyMovement.kind == "refund",
            ~(MoneyMovement.charge_ref.like("flwusd:%") | MoneyMovement.charge_ref.like("paypal:%")))

    async def notify_refund(self, account_id: str, amount: int, currency: str, data: dict | None = None) -> None:
        await self.svc.notifications.notify(
            account_id, NotificationType.PAYMENT, "Refund on its way",
            f"{amount / 100:,.2f} {currency} will reach you within {self.s.refund_working_days} working days.", data)


class StrayPaymentService(BaseService):
    """With Flutterwave the person pays first and the app then asks Wamiwa for what they paid for. If that request
    is refused, or never arrives (a closed browser), the payment would sit at Flutterwave with no record and no
    refund. This job finds such payments and gives them back in full (PayPal needs none: nothing is taken until
    Wamiwa captures it)."""

    GRACE = timedelta(minutes=30)  # long enough for a slow checkout to reach Wamiwa
    LOOKBACK_DAYS = 3

    async def _flutterwave(self):
        payments = self.providers.payments
        if hasattr(payments, "current"):  # the live site's test/live switch
            payments = await payments.current()
        main = getattr(payments, "main", payments)
        return main if hasattr(main, "successful_transactions") else None

    async def refund_unrecorded(self) -> int:
        flutterwave = await self._flutterwave()
        if flutterwave is None:
            return 0
        now = self.now()
        transactions = await flutterwave.successful_transactions(
            (now - timedelta(days=self.LOOKBACK_DAYS)).date().isoformat(), now.date().isoformat())
        refunded = 0
        for t in transactions:
            if not t["tx_ref"].startswith("wmw-") or t["id"] is None:
                continue  # not a Wamiwa checkout (e.g. a payment made in Flutterwave's dashboard)
            try:
                paid_at = datetime.fromisoformat(str(t["created_at"]).replace("Z", "+00:00"))
            except ValueError:
                continue
            if now - paid_at < self.GRACE:
                continue
            refs = (f"flw:{t['id']}", f"flwusd:{t['id']}@%")
            recorded = any([
                await self.repo(Payment).exists(Payment.provider_ref.like(refs[1]) | (Payment.provider_ref == refs[0])),
                await self.repo(EscrowTransaction).exists(
                    EscrowTransaction.provider_ref.like(refs[1]) | (EscrowTransaction.provider_ref == refs[0])),
                await self.repo(MoneyMovement).exists(MoneyMovement.reference == f"stray_{t['id']}"),
            ])
            if recorded:
                continue
            result = await flutterwave.refund_in_full(t["id"])
            amount = int(round(float(t["amount"] or 0) * 100))
            await self.repo(MoneyMovement).add(MoneyMovement(
                kind="stray", reference=f"stray_{t['id']}", charge_ref=refs[0], amount=amount,
                currency=t["currency"] or self.s.local_currency, status="succeeded" if result.succeeded else "failed",
                provider_ref=result.provider_ref, attempts=1, executed_at=now if result.succeeded else None,
                last_error=result.failure_reason))
            account_id = t["tx_ref"].removeprefix("wmw-").split("-", 1)[0]
            if result.succeeded:
                refunded += 1
                if await self.repo(Account).exists(Account.id == account_id):
                    await self.svc.notifications.notify(
                        account_id, NotificationType.PAYMENT, "Payment refunded",
                        f"We couldn't complete what your payment of {amount / 100:,.2f} {t['currency']} was for, so it "
                        "has been refunded in full. It reaches you within 5 working days.")
            else:
                await self.svc.admin.alert_admins(
                    "A payment could not be refunded automatically",
                    f"Flutterwave transaction {t['id']} ({amount / 100:,.2f} {t['currency']}) paid for nothing on "
                    f"Wamiwa, and the refund failed: {result.failure_reason}. Refund it from the Flutterwave dashboard.")
        return refunded
