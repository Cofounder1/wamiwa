"""PY-05: two ways to pay, one money path. Each charge goes to the provider the payer chose, and its refund goes
back to the same provider. Payouts and bank-name lookups are Flutterwave's alone.

The payment method says which: `paypal:<order id>` is PayPal; anything else (`flw:`, `flwcard:`, sandbox tokens)
goes to the main provider. A charge's reference starts the same way, so refunds follow it.
"""
from app.infra.providers.base import ChargeResult, PaymentGateway, PayPalCheckout, TransferResult

PAYPAL = "paypal:"


class PaymentRouter(PaymentGateway):
    def __init__(self, main: PaymentGateway, paypal: PayPalCheckout):
        self.main, self.paypal = main, paypal

    def _for(self, ref: str) -> PaymentGateway:
        return self.paypal if ref.startswith(PAYPAL) else self.main

    async def charge(self, *, reference: str, amount: int, currency: str, payment_method: str, customer_id: str,
                     usd_cents: int | None = None) -> ChargeResult:
        return await self._for(payment_method).charge(reference=reference, amount=amount, currency=currency,
                                                      payment_method=payment_method, customer_id=customer_id,
                                                      usd_cents=usd_cents)

    async def refund(self, *, provider_ref: str, amount: int, reference: str) -> TransferResult:
        return await self._for(provider_ref).refund(provider_ref=provider_ref, amount=amount, reference=reference)

    async def transfer(self, **kwargs) -> TransferResult:
        return await self.main.transfer(**kwargs)

    async def transfer_status(self, provider_ref: str) -> tuple[str, str | None]:
        return await self.main.transfer_status(provider_ref)

    async def resolve_account_name(self, bank_code: str, account_number: str, claimed_name: str | None = None) -> str | None:
        return await self.main.resolve_account_name(bank_code, account_number, claimed_name)

    async def held_balance(self, currency: str) -> int:
        """The main provider's balance. PayPal money is in dollars, kept out of the Naira reconciliation."""
        return await self.main.held_balance(currency)
