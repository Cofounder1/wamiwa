"""Flutterwave for payments and all payouts (PY-05): Naira in Nigeria, US dollars elsewhere, through its v3 API.
People can also pay with PayPal instead (paypal.py).

How a payment works:
  1. Wamiwa gives the app a reference (`wmw-<account id>-<unique>`), the amount and the currency.
  2. The payer pays in Flutterwave's checkout window.
  3. The app sends `flw:<transaction id>` as the payment method, and `charge` verifies the transaction with
     Flutterwave: successful, this account's reference, exactly the expected amount and currency.
  4. Flutterwave returns a card token. It's offered back as a reusable payment method (`flwcard:...`), so
     subscriptions and plans renew without the person paying again by hand.

Dollar payments keep the rate used in their reference (`flwusd:<id>@<rate>`), so a refund converts back at
exactly the same rate. Wamiwa's own records stay in Naira.
"""
import asyncio
from decimal import ROUND_HALF_UP, Decimal

import httpx

from app.infra.providers.base import ChargeResult, FxRateProvider, PaymentGateway, TransferResult, reference_prefix

BASE_URL = "https://api.flutterwave.com/v3"
LOCAL, USD = "NGN", "USD"


def major(minor: int) -> str:
    return str((Decimal(minor) / 100).quantize(Decimal("0.01")))


def minor(value) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class FlutterwaveGateway(PaymentGateway, FxRateProvider):
    def __init__(self, secret_key: str, *, timeout: float = 20.0, retries: int = 2, backoff_seconds: float = 0.5,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._retries, self._backoff = retries, backoff_seconds
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout, transport=transport,
                                         headers={"Authorization": f"Bearer {secret_key}"})

    async def _call(self, method: str, path: str, *, json: dict | None = None) -> httpx.Response:
        """Retries network errors, 429 and 5xx. Writes carry Wamiwa's own reference, which Flutterwave won't
        accept twice, so a retry can't charge, refund or pay twice."""
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.request(method, path, json=json)
            except httpx.TransportError:
                if attempt == self._retries:
                    raise
            else:
                if response.status_code < 500 and response.status_code != 429 or attempt == self._retries:
                    return response
            await asyncio.sleep(self._backoff * 2 ** attempt)
        raise RuntimeError("unreachable")

    @staticmethod
    def _data(response: httpx.Response) -> dict | None:
        try:
            body = response.json()
        except ValueError:
            return None
        return body.get("data") if body.get("status") == "success" else None

    @staticmethod
    def _message(response: httpx.Response) -> str:
        try:
            return response.json().get("message") or f"Flutterwave error {response.status_code}"
        except ValueError:
            return f"Flutterwave error {response.status_code}"

    def _settled(self, data: dict, amount: int, usd_cents: int | None) -> ChargeResult:
        """A successful transaction for exactly the expected amount, as a result with the right reference."""
        currency, paid = data.get("currency"), minor(data.get("amount", 0))
        if data.get("status") != "successful":
            return ChargeResult("failed", failure_reason="The payment wasn't completed")
        if currency == LOCAL and paid == amount:
            ref = f"flw:{data['id']}"
        elif currency == USD and usd_cents and paid == usd_cents:
            rate = (Decimal(amount) / Decimal(usd_cents)).quantize(Decimal("0.000001")).normalize()
            ref = f"flwusd:{data['id']}@{rate:f}"  # plain digits, never 1.5E+3
        else:
            return ChargeResult("failed", failure_reason="The payment is for a different amount. Try again.")
        card = data.get("card") or {}
        customer = data.get("customer") or {}
        reusable = (f"flwcard:{card['token']}:{customer.get('email', '')}:{currency}"
                    if card.get("token") and customer.get("email") else None)
        return ChargeResult("succeeded", ref, reusable_method=reusable)

    # --- charges ---------------------------------------------------------------------------------------
    async def charge(self, *, reference: str, amount: int, currency: str, payment_method: str, customer_id: str,
                     usd_cents: int | None = None) -> ChargeResult:
        if payment_method.startswith("flwcard:"):
            return await self._charge_saved_card(reference, amount, payment_method, usd_cents)
        transaction_id = payment_method.removeprefix("flw:")
        if transaction_id == payment_method or not transaction_id.isdigit():
            return ChargeResult("failed", failure_reason="Pay through the checkout to continue")
        response = await self._call("GET", f"/transactions/{transaction_id}/verify")
        data = self._data(response)
        if data is None:
            return ChargeResult("failed", failure_reason="We couldn't find that payment")
        if not str(data.get("tx_ref", "")).startswith(reference_prefix(customer_id)):
            return ChargeResult("failed", failure_reason="This payment belongs to another account")
        return self._settled(data, amount, usd_cents)

    async def _charge_saved_card(self, reference: str, amount: int, method: str, usd_cents: int | None) -> ChargeResult:
        """Renewals: charge the card saved at the first payment, in the currency it was first paid in."""
        _, token, email, currency = method.split(":", 3)
        value = major(amount) if currency == LOCAL else major(usd_cents or 0)
        response = await self._call("POST", "/tokenized-charges", json={
            "token": token, "email": email, "currency": currency, "amount": value, "tx_ref": reference})
        data = self._data(response)
        if data is None:
            return ChargeResult("failed", failure_reason=self._message(response))
        result = self._settled(data, amount, usd_cents)
        return ChargeResult(result.status, result.provider_ref, result.failure_reason, reusable_method=method)

    async def refund(self, *, provider_ref: str, amount: int, reference: str) -> TransferResult:
        """`amount` is in Naira minor units; a dollar payment is refunded in dollars at its original rate."""
        if provider_ref.startswith("flwusd:"):
            transaction_id, _, rate = provider_ref.removeprefix("flwusd:").partition("@")
            value = major(int((Decimal(amount) / Decimal(rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
        else:
            transaction_id, value = provider_ref.removeprefix("flw:"), major(amount)
        response = await self._call("POST", f"/transactions/{transaction_id}/refund", json={"amount": value})
        data = self._data(response)
        if data is None:
            return TransferResult(False, failure_reason=self._message(response))
        return TransferResult(True, f"flwrefund:{data.get('id')}")

    # --- payouts ---------------------------------------------------------------------------------------
    async def transfer(self, *, reference: str, amount: int, currency: str, bank_code: str, account_number: str,
                       account_name: str, details: dict | None = None) -> TransferResult:
        """Naira to a Nigerian bank, or dollars to a foreign bank (`details` carries what that bank needs, such as
        SWIFT or routing number and the beneficiary's address). `amount` is in the transfer currency's minor units."""
        body = {"account_bank": bank_code, "account_number": account_number, "amount": major(amount),
                "currency": currency, "debit_currency": currency, "narration": "Wamiwa earnings",
                "reference": reference, "beneficiary_name": account_name}
        if details:
            body["meta"] = [{**details, "AccountNumber": account_number, "BeneficiaryName": account_name}]
        response = await self._call("POST", "/transfers", json=body)
        data = self._data(response)
        if data is None or data.get("status") in ("FAILED",):
            return TransferResult(False, failure_reason=self._message(response))
        # Dollar payouts leave the dollar balance, so they're marked like dollar payments and kept out of the
        # Naira reconciliation.
        return TransferResult(True, f"{'flwusd' if currency == USD else 'flw'}:transfer:{data.get('id')}")

    async def resolve_account_name(self, bank_code: str, account_number: str, claimed_name: str | None = None) -> str | None:
        """Nigerian accounts are looked up; for foreign banks Flutterwave can't, so the name given is used."""
        if bank_code == "intl":
            return claimed_name or None
        response = await self._call("POST", "/accounts/resolve", json={"account_number": account_number,
                                                                      "account_bank": bank_code})
        data = self._data(response)
        return data.get("account_name") if data else None

    # --- exchange rate --------------------------------------------------------------------------------
    async def rate(self, base: str, quote: str) -> Decimal:
        """Units of `quote` per one `base` (e.g. Naira per US dollar), at Flutterwave's own rate, so Wamiwa's
        conversions match the money Flutterwave moves. Flutterwave updates it a few times a day."""
        if base == quote:
            return Decimal(1)
        response = await self._call("GET", f"/transfers/rates?amount=100&destination_currency={base}&source_currency={quote}")
        data = self._data(response) or {}
        # Worked out from the amounts: what 100 `base` cost in `quote`. Flutterwave's own "rate" field is the other
        # way round (e.g. dollars per Naira), whatever its documentation says.
        try:
            rate = Decimal(str(data["source"]["amount"])) / Decimal(str(data["destination"]["amount"]))
        except (KeyError, TypeError, ArithmeticError, ValueError):
            rate = None
        if rate is None or rate <= 0:
            raise RuntimeError(f"Flutterwave rate unavailable: {self._message(response)}")
        return rate.quantize(Decimal("0.0001"))

    async def held_balance(self, currency: str) -> int:
        data = self._data(await self._call("GET", f"/balances/{currency}"))
        if data is None:
            raise RuntimeError("Flutterwave balance unavailable")
        # The ledger balance includes money still settling, which is what escrow records count as held.
        return minor(data.get("ledger_balance") if data.get("ledger_balance") is not None else data.get("available_balance", 0))
