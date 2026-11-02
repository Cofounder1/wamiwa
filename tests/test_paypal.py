"""PY-05 PayPal, the second way to pay beside Flutterwave, against a fake PayPal that behaves like its API."""
import json

import httpx

from app.infra.providers.paypal import PayPalGateway
from tests.test_hiring import hire_me_setup, kobo, offer


class FakePayPal:
    """Orders, captures, refunds, payouts and balances, with PayPal-Request-Id making repeats safe."""

    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.captures: dict[str, dict] = {}
        self.refunds: list[dict] = []
        self.payouts: list[dict] = []
        self.request_ids: dict[str, httpx.Response] = {}
        self.token_calls = 0

    def approve(self, order_id: str) -> None:
        self.orders[order_id]["status"] = "APPROVED"

    def handler(self, request: httpx.Request) -> httpx.Response:
        rid = request.headers.get("PayPal-Request-Id")
        if rid and rid in self.request_ids:
            return self.request_ids[rid]
        response = self._route(request)
        if rid:
            self.request_ids[rid] = response
        return response

    def _route(self, request: httpx.Request) -> httpx.Response:
        is_json = request.headers.get("content-type", "").startswith("application/json")
        path, body = request.url.path, json.loads(request.content) if is_json and request.content else {}
        if path == "/v1/oauth2/token":
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": f"tok{self.token_calls}", "expires_in": 32400})
        if path == "/v2/checkout/orders" and request.method == "POST":
            order_id = f"ORDER{len(self.orders) + 1}"
            self.orders[order_id] = {"id": order_id, "status": "CREATED", "purchase_units": body["purchase_units"]}
            return httpx.Response(201, json={"id": order_id, "status": "CREATED"})
        if path.startswith("/v2/checkout/orders/") and path.endswith("/capture"):
            order = self.orders[path.split("/")[4]]
            if order["status"] != "APPROVED":
                return httpx.Response(422, json={"details": [{"description": "Order not approved"}]})
            order["status"] = "COMPLETED"
            capture_id = f"CAP{len(self.captures) + 1}"
            self.captures[capture_id] = {"amount": order["purchase_units"][0]["amount"]}
            order["purchase_units"][0]["payments"] = {"captures": [{"id": capture_id, "status": "COMPLETED"}]}
            return httpx.Response(201, json=order)
        if path.startswith("/v2/checkout/orders/"):
            order = self.orders.get(path.split("/")[4])
            return httpx.Response(200, json=order) if order else httpx.Response(404, json={"message": "Not found"})
        if path.startswith("/v2/payments/captures/") and path.endswith("/refund"):
            self.refunds.append({"capture": path.split("/")[4], **body["amount"]})
            return httpx.Response(201, json={"id": f"REF{len(self.refunds)}", "status": "COMPLETED"})
        if path == "/v1/payments/payouts":
            self.payouts.append(body["items"][0])
            return httpx.Response(201, json={"batch_header": {"payout_batch_id": f"BATCH{len(self.payouts)}",
                                                              "batch_status": "PENDING"}})
        if path == "/v1/reporting/balances":
            return httpx.Response(200, json={"balances": [{"total_balance": {"currency_code": "USD", "value": "0.00"}}]})
        return httpx.Response(404)


def gateway(fake: FakePayPal) -> PayPalGateway:
    return PayPalGateway("client", "secret", "sandbox", transport=httpx.MockTransport(fake.handler), backoff_seconds=0)


async def test_paypal_checkout_checks_the_amount_and_never_charges_twice():
    fake = FakePayPal()
    paypal = gateway(fake)
    order_id = await paypal.create_order(amount_cents=16_00, description="Wamiwa Pro", reference="wmw-c-1")
    pay = dict(currency="NGN", payment_method=f"paypal:{order_id}", amount=24_000_00)
    # Not approved yet: nothing is captured.
    refused = await paypal.charge(reference="r1", customer_id="c", usd_cents=16_00, **pay)
    assert refused.status == "failed"
    fake.approve(order_id)
    # Someone else's order, or an order for $16 used for a $30 price, is refused.
    theirs = await paypal.charge(reference="r2", customer_id="d", usd_cents=16_00, **pay)
    assert theirs.status == "failed" and "another account" in theirs.failure_reason
    wrong = await paypal.charge(reference="r2", customer_id="c", usd_cents=30_00, **pay)
    assert wrong.status == "failed" and "different amount" in wrong.failure_reason
    paid = await paypal.charge(reference="r3", customer_id="c", usd_cents=16_00, **pay)
    again = await paypal.charge(reference="r3", customer_id="c", usd_cents=16_00, **pay)
    assert paid.status == "succeeded" and paid.provider_ref == again.provider_ref == "paypal:CAP1@1500"
    assert len(fake.captures) == 1 and fake.token_calls == 1  # captured once; the sign-in is reused
    # Wamiwa refunds in Naira; PayPal gets dollars at the rate of the payment.
    refund = await paypal.refund(provider_ref=paid.provider_ref, amount=12_000_00, reference="refund_1")
    assert refund.succeeded and fake.refunds == [{"capture": "CAP1", "currency_code": "USD", "value": "8.00"}]


async def test_paypal_is_off_until_connected(api):
    _, brand, influencer = await hire_me_setup(api)
    method = await api.ok("GET", "/payments/method", brand)
    assert method["paypal"] == {"available": False, "currency": "USD", "client_id": None, "environment": None}
    r = await api.call("POST", "/payments/paypal/order", brand, json={"amount_cents": 100_00}, expect=503)
    assert r.json()["code"] == "paypal_unavailable"
    r = await api.call("POST", "/hire-me/offers", brand, json=offer(api, influencer) | {"payment_method": "paypal:X"},
                       expect=503)
    assert r.json()["code"] == "paypal_unavailable"
    # Flutterwave (here its stand-in) still takes the payment.
    await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))


async def test_paying_with_paypal_beside_flutterwave(api):
    api.infra.settings.paypal_client_id, api.infra.settings.paypal_secret = "client", "secret"
    fake = FakePayPal()
    api.infra.providers.paypal = api.infra.providers.payments.paypal = gateway(fake)
    _, brand, influencer = await hire_me_setup(api)
    method = await api.ok("GET", "/payments/method", brand)
    assert method["paypal"] == {"available": True, "currency": "USD", "client_id": "client", "environment": "sandbox"}

    # The brand funds escrow with PayPal; the order carries the brand's account and is captured once.
    order = await api.ok("POST", "/payments/paypal/order", brand, json={"amount_cents": 100_00, "description": "Offer"})
    assert order["amount"] == "100.00" and order["currency"] == "USD"
    fake.approve(order["order_id"])
    contract = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer) | {
        "payment_method": order["payment_method"]})
    assert (await api.ok("GET", f"/contracts/{contract['id']}", brand))["escrow"]["amount"] == kobo(100_00)
    r = await api.call("POST", "/hire-me/offers", brand, json=offer(api, influencer) | {
        "payment_method": order["payment_method"]}, expect=402)
    assert r.json()["code"] == "payment_failed" and len(fake.captures) == 1

    # Declined: the refund goes back to PayPal, in dollars.
    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/decline", influencer)
    assert fake.refunds == [{"capture": "CAP1", "currency_code": "USD", "value": "100.00"}]

    # Another payer's PayPal order can't be used, and other payments still go to Flutterwave.
    other = await api.signup("ben")
    theirs = await api.ok("POST", "/payments/paypal/order", other, json={"amount_cents": 100_00})
    fake.approve(theirs["order_id"])
    r = await api.call("POST", "/hire-me/offers", brand, json=offer(api, influencer) | {
        "payment_method": theirs["payment_method"]}, expect=402)
    assert "another account" in r.json()["error"]
    await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))

    # A subscription paid with PayPal stays held at PayPal, in dollars, so the Naira reconciliation (Flutterwave's
    # balance) leaves it out and still matches.
    pro = await api.ok("POST", "/payments/paypal/order", other, json={"amount_cents": 16_00})
    fake.approve(pro["order_id"])
    await api.ok("POST", "/subscriptions", other, json={"period": "monthly", "tier": "pro",
                                                         "payment_method": pro["payment_method"]})
    run = await api.run("reconciliation.run")
    assert run.matched


def test_test_keys_are_used_outside_production_and_never_on_the_live_site():
    from app.core.config import Settings

    keys = dict(flutterwave_secret_key="FLWSECK-live", flutterwave_sandbox_secret_key="FLWSECK_TEST-x",
                paypal_client_id="live-id", paypal_secret="live-secret", paypal_environment="live",
                paypal_sandbox_client_id="sb-id", paypal_sandbox_secret="sb-secret")
    local = Settings(_env_file=None, env="development", **keys)
    assert local.flutterwave_secret_key == "FLWSECK_TEST-x"
    assert (local.paypal_client_id, local.paypal_secret, local.paypal_environment) == ("sb-id", "sb-secret", "sandbox")
    live = Settings(_env_file=None, env="production", **keys)
    assert live.flutterwave_secret_key == "FLWSECK-live"
    assert (live.paypal_client_id, live.paypal_environment) == ("live-id", "live")
