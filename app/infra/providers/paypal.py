"""PayPal, through its REST API: a second way to pay, beside Flutterwave (PY-05). Always in US dollars.

How a PayPal payment works:
  1. The app asks Wamiwa for a PayPal order for the exact price (`POST /payments/paypal/order`).
  2. The payer approves it in PayPal's window.
  3. The app sends `paypal:<order id>` as the payment method to the normal endpoint (subscribe, send offer...),
     and `charge` captures it, after checking the order is this account's and for exactly the expected price.

Wamiwa's records stay in Naira, so a capture keeps the rate it was recorded at (`paypal:<capture id>@<rate>`)
and a refund converts back at exactly that rate. Every write carries a PayPal-Request-Id, so retrying never
charges, refunds or pays twice. PayPal saves no card here, so a renewal paid by PayPal is paid again by hand.
"""
import asyncio
import time
from decimal import ROUND_HALF_UP, Decimal

import httpx

from app.infra.providers.base import ChargeResult, PayPalCheckout, TransferResult, reference_prefix

BASE_URLS = {"sandbox": "https://api-m.sandbox.paypal.com", "live": "https://api-m.paypal.com"}
CURRENCY = "USD"


def to_value(cents: int) -> str:
    return str((Decimal(cents) / 100).quantize(Decimal("0.01")))


def to_cents(value: str) -> int:
    return int((Decimal(value) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class PayPalError(RuntimeError):
    pass


class PayPalGateway(PayPalCheckout):
    def __init__(self, client_id: str, secret: str, environment: str = "sandbox", *, timeout: float = 15.0,
                 retries: int = 2, backoff_seconds: float = 0.5, transport: httpx.AsyncBaseTransport | None = None):
        if environment not in BASE_URLS:
            raise ValueError("PayPal environment must be 'sandbox' or 'live'")
        self.environment = environment
        self._auth = (client_id, secret)
        self._retries, self._backoff = retries, backoff_seconds
        self._client = httpx.AsyncClient(base_url=BASE_URLS[environment], timeout=timeout, transport=transport)
        self._token: str | None = None
        self._token_expires = 0.0

    # --- plumbing -------------------------------------------------------------------------------------
    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires:
            return self._token
        response = await self._client.post("/v1/oauth2/token", auth=self._auth, data={"grant_type": "client_credentials"})
        if response.status_code != 200:
            raise PayPalError(f"PayPal sign-in failed ({response.status_code})")
        body = response.json()
        self._token = body["access_token"]
        self._token_expires = time.monotonic() + max(60, int(body.get("expires_in", 3600)) - 120)
        return self._token

    async def _call(self, method: str, path: str, *, json: dict | None = None, request_id: str | None = None,
                    params: dict | None = None) -> httpx.Response:
        """Retries network errors and PayPal's 5xx/429 with backoff; the request id makes retries safe."""
        for attempt in range(self._retries + 1):
            headers = {"Authorization": f"Bearer {await self._access_token()}", "Content-Type": "application/json",
                       "Prefer": "return=representation"}
            if request_id:
                headers["PayPal-Request-Id"] = request_id
            try:
                response = await self._client.request(method, path, json=json, params=params, headers=headers)
            except httpx.TransportError:
                if attempt == self._retries:
                    raise
            else:
                if response.status_code == 401:
                    self._token = None  # expired early: sign in again on the next try
                elif response.status_code < 500 and response.status_code != 429:
                    return response
                elif attempt == self._retries:
                    return response
            await asyncio.sleep(self._backoff * 2 ** attempt)
        raise PayPalError("unreachable")

    @staticmethod
    def _reason(response: httpx.Response) -> str:
        try:
            body = response.json()
            details = body.get("details") or [{}]
            return details[0].get("description") or body.get("message") or f"PayPal error {response.status_code}"
        except ValueError:
            return f"PayPal error {response.status_code}"

    # --- checkout ------------------------------------------------------------------------------------
    async def create_order(self, *, amount_cents: int, description: str, reference: str) -> str:
        """A PayPal order for the payer to approve. Returns its id."""
        response = await self._call("POST", "/v2/checkout/orders", request_id=f"order-{reference}", json={
            "intent": "CAPTURE",
            "purchase_units": [{"amount": {"currency_code": CURRENCY, "value": to_value(amount_cents)},
                                "description": description[:127], "custom_id": reference[:127]}],
            "application_context": {"brand_name": "Wamiwa", "shipping_preference": "NO_SHIPPING",
                                    "user_action": "PAY_NOW"},
        })
        if response.status_code not in (200, 201):
            raise PayPalError(self._reason(response))
        return response.json()["id"]

    async def charge(self, *, reference: str, amount: int, currency: str, payment_method: str,
                     customer_id: str, usd_cents: int | None = None) -> ChargeResult:
        """Capture an order the payer approved. `usd_cents` is the price Wamiwa expects; any other amount is refused,
        so an order created for less can't be used to pay for more. `amount` is the same price in Naira."""
        order_id = payment_method.removeprefix("paypal:")
        if not order_id or order_id == payment_method or usd_cents is None:
            return ChargeResult("failed", failure_reason="Pay with PayPal to continue")
        order = await self._call("GET", f"/v2/checkout/orders/{order_id}")
        if order.status_code != 200:
            return ChargeResult("failed", failure_reason="PayPal order not found")
        unit = (order.json().get("purchase_units") or [{}])[0]
        if not str(unit.get("custom_id", "")).startswith(reference_prefix(customer_id)):
            return ChargeResult("failed", failure_reason="This payment belongs to another account")
        amount_value = unit.get("amount", {})
        if amount_value.get("currency_code") != CURRENCY or to_cents(amount_value.get("value", "0")) != usd_cents:
            return ChargeResult("failed", failure_reason="The PayPal payment is for a different amount. Try again.")
        if order.json().get("status") not in ("APPROVED", "COMPLETED"):
            return ChargeResult("failed", failure_reason="The PayPal payment wasn't approved")
        captured = await self._call("POST", f"/v2/checkout/orders/{order_id}/capture", request_id=f"capture-{reference}")
        if captured.status_code not in (200, 201):
            return ChargeResult("failed", failure_reason=self._reason(captured))
        captures = ((captured.json().get("purchase_units") or [{}])[0].get("payments") or {}).get("captures") or [{}]
        capture = captures[0]
        status = capture.get("status")
        if status not in ("COMPLETED", "PENDING"):
            return ChargeResult("failed", failure_reason=f"PayPal payment {str(status).lower()}")
        rate = (Decimal(amount) / Decimal(usd_cents)).quantize(Decimal("0.000001")).normalize()
        return ChargeResult("succeeded" if status == "COMPLETED" else "pending_risk", f"paypal:{capture['id']}@{rate:f}")

    async def refund(self, *, provider_ref: str, amount: int, reference: str) -> TransferResult:
        """`amount` is in Naira minor units, refunded in dollars at the capture's own rate."""
        capture_id, _, rate = provider_ref.removeprefix("paypal:").partition("@")
        cents = int((Decimal(amount) / Decimal(rate or "1")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        response = await self._call("POST", f"/v2/payments/captures/{capture_id}/refund", request_id=f"refund-{reference}",
                                    json={"amount": {"currency_code": CURRENCY, "value": to_value(cents)}})
        if response.status_code not in (200, 201) or response.json().get("status") not in ("COMPLETED", "PENDING"):
            return TransferResult(False, failure_reason=self._reason(response))
        return TransferResult(True, f"paypalrefund:{response.json().get('id')}")

    # --- payouts -------------------------------------------------------------------------------------
    async def transfer(self, *, reference: str, amount: int, currency: str, bank_code: str, account_number: str,
                       account_name: str, details: dict | None = None) -> TransferResult:
        """Pay a creator's PayPal account. `amount` is in US cents; `account_number` is their PayPal email.
        PayPal sends the money in a batch and confirms later; a submitted batch counts as paid.
        Not used yet: payouts go to bank accounts through Flutterwave (PY-05)."""
        response = await self._call("POST", "/v1/payments/payouts", request_id=f"payout-{reference}", json={
            "sender_batch_header": {"sender_batch_id": reference[:127], "email_subject": "You have a payout from Wamiwa"},
            "items": [{"recipient_type": "EMAIL", "receiver": account_number, "sender_item_id": reference[:63],
                       "amount": {"currency": CURRENCY, "value": to_value(amount)}, "note": "Wamiwa earnings"}],
        })
        if response.status_code not in (200, 201):
            return TransferResult(False, failure_reason=self._reason(response))
        header = response.json().get("batch_header", {})
        if header.get("batch_status") in ("DENIED", "CANCELED"):
            return TransferResult(False, failure_reason=f"PayPal payout {header['batch_status'].lower()}")
        return TransferResult(True, header.get("payout_batch_id"))

    async def resolve_account_name(self, bank_code: str, account_number: str, claimed_name: str | None = None) -> str | None:
        """PayPal doesn't reveal whose account an email is, so the name the creator gives is used for the ID check."""
        return claimed_name or None

    async def held_balance(self, currency: str) -> int:
        response = await self._call("GET", "/v1/reporting/balances", params={"currency_code": CURRENCY})
        if response.status_code != 200:
            raise PayPalError(self._reason(response))
        balances = response.json().get("balances") or [{}]
        return to_cents(balances[0].get("total_balance", {}).get("value", "0"))
