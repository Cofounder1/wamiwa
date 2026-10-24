"""PY-05 Flutterwave for all payments and payouts, against a fake Flutterwave that behaves like its v3 API."""
import json

import httpx

from app.infra.providers.flutterwave import FlutterwaveGateway
from tests.test_hiring import hire_me_setup, kobo, offer


class FakeFlutterwave:
    def __init__(self) -> None:
        self.transactions: dict[int, dict] = {}
        self.refunds: list[dict] = []
        self.transfers: list[dict] = []
        self.token_charges: list[dict] = []
        self.references: set[str] = set()
        self.rate: float | None = 1534.25  # Naira per US dollar

    def pay(self, tx_ref: str, amount: str, currency: str, email: str = "payer@example.com") -> int:
        """What happens in the checkout window: a successful card payment, with a token saved for renewals."""
        transaction_id = 1000 + len(self.transactions)
        self.transactions[transaction_id] = {
            "id": transaction_id, "tx_ref": tx_ref, "status": "successful", "amount": float(amount), "currency": currency,
            "customer": {"email": email}, "card": {"token": f"flw-t1nf-{transaction_id}"}}
        return transaction_id

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v3")
        body = json.loads(request.content) if request.content else {}

        def ok(data):
            return httpx.Response(200, json={"status": "success", "message": "ok", "data": data})

        if path.endswith("/verify"):
            tx = self.transactions.get(int(path.split("/")[2]))
            return ok(tx) if tx else httpx.Response(404, json={"status": "error", "message": "No transaction"})
        if path == "/tokenized-charges":
            if body["tx_ref"] in self.references:
                return httpx.Response(400, json={"status": "error", "message": "Duplicate reference"})
            self.references.add(body["tx_ref"])
            self.token_charges.append(body)
            return ok(self.transactions.setdefault(9000 + len(self.token_charges), {
                "id": 9000 + len(self.token_charges), "tx_ref": body["tx_ref"], "status": "successful",
                "amount": float(body["amount"]), "currency": body["currency"], "customer": {"email": body["email"]},
                "card": {"token": body["token"]}}))
        if path.endswith("/refund"):
            self.refunds.append({"transaction": int(path.split("/")[2]), **body})
            return ok({"id": 70 + len(self.refunds), "status": "completed"})
        if path == "/transfers":
            self.transfers.append(body)
            return ok({"id": 500 + len(self.transfers), "status": "NEW"})
        if path == "/accounts/resolve":
            return ok({"account_name": "TOLA BELLO"})
        if path == "/transfers/rates":
            if self.rate is None:
                return httpx.Response(400, json={"status": "error", "message": "Unable to fetch rates at the moment",
                                                 "data": None})
            q = request.url.params  # as the real one answers: "rate" is the inverse; the amounts are what count
            amount = float(q["amount"])
            return ok({"rate": round(1 / self.rate, 9), "source": {"currency": q["source_currency"],
                       "amount": round(amount * self.rate, 4)},
                       "destination": {"currency": q["destination_currency"], "amount": amount}})
        if path.startswith("/balances/"):
            return ok({"currency": path.split("/")[2], "available_balance": 0, "ledger_balance": 0})
        return httpx.Response(404, json={"status": "error", "message": "Not found"})


def gateway(fake: FakeFlutterwave) -> FlutterwaveGateway:
    return FlutterwaveGateway("FLWSECK_TEST-x", transport=httpx.MockTransport(fake.handler), backoff_seconds=0)


async def test_payments_are_verified_with_flutterwave_and_used_once():
    fake = FakeFlutterwave()
    flw = gateway(fake)
    mine = fake.pay("wmw-acc1-abc", "150000.00", "NGN")
    ok = await flw.charge(reference="r1", amount=150_000_00, currency="NGN", payment_method=f"flw:{mine}",
                          customer_id="acc1", usd_cents=100_00)
    assert ok.status == "succeeded" and ok.provider_ref == f"flw:{mine}" and ok.reusable_method.startswith("flwcard:")
    # Someone else's payment, or one for a different amount, is refused.
    theirs = await flw.charge(reference="r2", amount=150_000_00, currency="NGN", payment_method=f"flw:{mine}",
                              customer_id="acc2", usd_cents=100_00)
    assert theirs.status == "failed" and "another account" in theirs.failure_reason
    short = await flw.charge(reference="r3", amount=300_000_00, currency="NGN", payment_method=f"flw:{mine}",
                             customer_id="acc1", usd_cents=200_00)
    assert short.status == "failed" and "different amount" in short.failure_reason
    # A dollar payment keeps its rate, and its refund goes back in dollars at that rate.
    usd = fake.pay("wmw-acc3-def", "16.00", "USD")
    paid = await flw.charge(reference="r4", amount=24_000_00, currency="NGN", payment_method=f"flw:{usd}",
                            customer_id="acc3", usd_cents=16_00)
    assert paid.provider_ref == f"flwusd:{usd}@1500"
    await flw.refund(provider_ref=paid.provider_ref, amount=12_000_00, reference="refund_1")
    assert fake.refunds == [{"transaction": usd, "amount": "8.00"}]


async def test_flutterwave_end_to_end_naira_dollars_renewals_and_payouts(api):
    api.infra.settings.flutterwave_public_key = "FLWPUBK_TEST-x"
    api.infra.settings.flutterwave_secret_key = "FLWSECK_TEST-x"
    fake = FakeFlutterwave()
    api.infra.providers.payments = gateway(fake)
    admin, brand, influencer = await hire_me_setup(api)

    # A Nigerian brand pays in Naira; the same payment can't be used twice.
    method = await api.ok("GET", "/payments/method", brand)
    assert method == {"provider": "flutterwave", "currency": "NGN", "public_key": "FLWPUBK_TEST-x", "available": True,
                      "paypal": {"available": False, "currency": "USD", "client_id": None, "environment": None}}
    checkout = await api.ok("POST", "/payments/checkout", brand, json={"amount_cents": 100_00, "description": "Offer"})
    assert checkout["currency"] == "NGN" and checkout["amount"] == "150000.00" and checkout["tx_ref"].startswith(f"wmw-{brand.id}-")
    paid = fake.pay(checkout["tx_ref"], checkout["amount"], "NGN")
    contract = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer) | {"payment_method": f"flw:{paid}"})
    assert (await api.ok("GET", f"/contracts/{contract['id']}", brand))["escrow"]["amount"] == kobo(100_00)
    r = await api.call("POST", "/hire-me/offers", brand, json=offer(api, influencer) | {"payment_method": f"flw:{paid}"}, expect=409)
    assert r.json()["code"] == "payment_already_used"

    # Someone in the UK pays in dollars, and the subscription renews on the saved card.
    uk = await api.signup("ben", region="United Kingdom")
    checkout = await api.ok("POST", "/payments/checkout", uk, json={"amount_cents": 16_00})
    assert checkout["currency"] == "USD" and checkout["amount"] == "16.00"
    usd = fake.pay(checkout["tx_ref"], "16.00", "USD", email="ben@example.com")
    await api.ok("POST", "/subscriptions", uk, json={"period": "monthly", "tier": "pro", "payment_method": f"flw:{usd}"})
    api.clock.advance(days=30, minutes=1)
    await api.run("subscriptions.process_renewals")
    assert fake.token_charges[-1] | {"tx_ref": "x"} == {"token": f"flw-t1nf-{usd}", "email": "ben@example.com",
                                                         "currency": "USD", "amount": "16.00", "tx_ref": "x"}
    assert (await api.ok("GET", "/subscriptions", uk))["subscribed"]

    # A creator abroad is paid to their own bank in dollars; the bank's details are required.
    abroad = await api.influencer("amaka", "Amaka Obi")
    await api.ok("PATCH", "/accounts/me/profile", abroad, json={"region": "Canada"})
    r = await api.call("PUT", "/verification/bank-account", abroad, json={
        "bank_code": "intl", "account_number": "12345678", "account_name": "Amaka Obi"}, expect=422)
    assert r.json()["code"] == "bank_details_required"
    saved = await api.ok("PUT", "/verification/bank-account", abroad, json={
        "bank_code": "intl", "account_number": "12345678", "account_name": "Amaka Obi", "details": {
            "bank_name": "RBC", "swift_code": "ROYCCAT2", "beneficiary_address": "1 King St, Toronto",
            "beneficiary_country": "CA"}})
    assert saved["currency"] == "USD" and saved["bank_name"] == "RBC" and not saved["payouts_blocked"]
    result = await api.infra.providers.payments.transfer(
        reference="payout_x", amount=80_00, currency="USD", bank_code="intl", account_number="12345678",
        account_name="Amaka Obi", details={"bank_name": "RBC", "swift_code": "ROYCCAT2"})
    assert result.provider_ref.startswith("flwusd:") and fake.transfers[-1]["meta"][0]["swift_code"] == "ROYCCAT2"


async def test_the_days_rate_comes_from_flutterwave_with_the_last_rate_as_a_fallback(api):
    from decimal import Decimal

    fake = FakeFlutterwave()
    api.infra.providers.fx = gateway(fake)
    async with api.services() as svc:
        assert await svc.fx.rate() == Decimal("1534.25")
        assert await svc.fx.to_local(10_00) == (1_534_250, Decimal("1534.25"))  # $10 in kobo
    # Fetched once a day: a change at Flutterwave later the same day doesn't move today's prices.
    fake.rate = 1600
    async with api.services() as svc:
        assert await svc.fx.rate() == Decimal("1534.25")

    # Flutterwave can't give a rate: the last one is used, and the admins hear once it's 2 days old.
    fake.rate = None
    api.clock.advance(days=1)
    async with api.services() as svc:
        assert await svc.fx.rate() == Decimal("1534.25")
    admin = await api.admin()
    api.clock.advance(days=1, minutes=20)
    async with api.services() as svc:
        assert await svc.fx.rate() == Decimal("1534.25")
    alerts = await api.ok("GET", "/notifications", admin)
    assert sum("Exchange rate is out of date" in n["title"] for n in alerts) == 1
    # Back again: the fresh rate is used within minutes, not the next day.
    fake.rate = 1550
    api.clock.advance(minutes=20)
    async with api.services() as svc:
        assert await svc.fx.rate() == Decimal("1550")


async def test_no_rate_ever_saved_refuses_clearly(api):
    import pytest

    from app.core.exceptions import ServiceUnavailable

    fake = FakeFlutterwave()
    fake.rate = None
    api.infra.providers.fx = gateway(fake)
    with pytest.raises(ServiceUnavailable) as refused:
        async with api.services() as svc:
            await svc.fx.rate()
    assert refused.value.code == "fx_unavailable"


async def test_a_likeness_hire_and_its_video_production_are_one_payment(api):
    """LK-10: the checkout charges one exact amount, so the likeness price and the production fee are paid together
    and refunded separately against that one payment."""
    from tests.test_hiring import likeness_setup, script_body

    api.infra.settings.flutterwave_public_key = "FLWPUBK_TEST-x"
    api.infra.settings.flutterwave_secret_key = "FLWSECK_TEST-x"
    fake = FakeFlutterwave()
    api.infra.providers.payments = gateway(fake)
    admin, brand, influencer = await likeness_setup(api)

    # The quote says what to pay: $100 likeness + $45 production.
    quote = await api.ok("POST", "/contracts/quote", brand, json={"type": "likeness", "price_cents": 100_00})
    assert quote["production_usd_cents"] == 45_00 and quote["pay_now"] == {
        "usd_cents": 145_00, "local": kobo(145_00), "currency": "NGN"}
    checkout = await api.ok("POST", "/payments/checkout", brand, json={"amount_cents": quote["pay_now"]["usd_cents"]})
    paid = fake.pay(checkout["tx_ref"], checkout["amount"], "NGN")
    contract = await api.ok("POST", "/likeness/contracts", brand, json=script_body(influencer) | {
        "payment_method": f"flw:{paid}"})
    assert contract["escrow"]["amount"] == kobo(100_00)
    history = await api.ok("GET", "/payments", brand)
    assert [(p["purpose"], p["amount_local"]) for p in history] == [("video_production", kobo(45_00))]

    # A payment for only the likeness price isn't enough.
    short = fake.pay((await api.ok("POST", "/payments/checkout", brand, json={"amount_cents": 100_00}))["tx_ref"],
                     f"{kobo(100_00) / 100:.2f}", "NGN")
    r = await api.call("POST", "/likeness/contracts", brand, json=script_body(influencer) | {
        "payment_method": f"flw:{short}"}, expect=402)
    assert "different amount" in r.json()["error"]

    # Rejected: both parts go back, each refunded against the same Flutterwave payment.
    await api.ok("POST", f"/likeness/contracts/{contract['id']}/reject", influencer, json={"reason": "Not for me"})
    assert {(r["transaction"], r["amount"]) for r in fake.refunds} == {
        (paid, f"{kobo(45_00) / 100:.2f}"), (paid, f"{kobo(100_00) / 100:.2f}")}

    # With a video plan covering the video, only the likeness price is paid.
    await api.ok("POST", "/video-plans", brand, json={"plan": "growth", "payment_method": f"flw:{fake.pay(
        (await api.ok('POST', '/payments/checkout', brand, json={'amount_cents': 199_00}))['tx_ref'],
        f'{kobo(199_00) / 100:.2f}', 'NGN')}"})
    planned = fake.pay((await api.ok("POST", "/payments/checkout", brand, json={"amount_cents": 100_00}))["tx_ref"],
                       f"{kobo(100_00) / 100:.2f}", "NGN")
    await api.ok("POST", "/likeness/contracts", brand, json=script_body(influencer, use_plan=True) | {
        "payment_method": f"flw:{planned}"})
