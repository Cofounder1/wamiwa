"""Failure handling: money is never taken or given without a record, and errors reach clients clearly."""

import httpx
import pytest

from app.core.enums import PayoutStatus
from app.models import Payout
from tests.test_hiring import PAY, hire_me_setup, kobo, likeness_setup, offer


def no_raise_client(api) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api.client._transport.app, raise_app_exceptions=False),
                             base_url="http://test")


async def test_checks_run_before_any_charge(api):
    _, brand, influencer = await likeness_setup(api)
    charges = len(api.infra.providers.payments.main.charges)
    r = await api.call("POST", "/likeness/contracts", brand, expect=409, json={
        "influencer_id": influencer.id, "script": "Hello, I use Acme to save every month.", "use_plan": True,
        "payment_method": PAY})
    assert r.json()["code"] == "plan_exhausted"
    assert len(api.infra.providers.payments.main.charges) == charges  # nobody was charged


async def test_a_charge_is_reversed_when_its_request_fails(api):
    user = await api.influencer("maker", "Maker Person")
    payments = api.infra.providers.payments.main

    async def video_service_down(_request):
        raise RuntimeError("video service unreachable")

    api.infra.providers.video.submit = video_service_down
    async with no_raise_client(api) as client:
        r = await client.post("/ai-videos", headers=user.headers, json={
            "script": "Hello there", "seconds": 60, "tier": "premium", "payment_method": PAY})
    assert r.status_code == 500 and r.json()["request_id"]
    # The card was charged, the request failed, so the same amount went straight back.
    charged = list(payments.charges.items())[-1]
    assert payments.refunds[-1]["provider_ref"] == charged[0] and payments.refunds[-1]["amount"] == charged[1][0]
    assert payments.refunds[-1]["reference"].startswith("void_")
    assert (await api.ok("GET", "/payments", user)) == []  # and no payment record was kept


async def test_refunds_happen_only_after_the_decision_is_saved_and_are_retried(api):
    _, brand, influencer = await hire_me_setup(api)
    payments = api.infra.providers.payments.main
    contract = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))

    # A refund decided inside a transaction that then fails is never sent.
    before = len(payments.refunds)
    with pytest.raises(RuntimeError):
        async with api.services() as svc:
            await svc.escrow.settle(contract["id"], 0, None, "test")
            raise RuntimeError("something later in the same request broke")
    assert len(payments.refunds) == before
    assert (await api.ok("GET", f"/contracts/{contract['id']}", brand))["escrow"]["state"] == "funded"

    # A refund the provider refuses is kept and retried, and is paid exactly once.
    payments.fail_next_refunds = 2
    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/decline", influencer)
    assert len(payments.refunds) == before  # first attempt failed
    api.clock.advance(minutes=2)
    await api.run("money.retry_due")  # second attempt fails too
    api.clock.advance(minutes=6)
    await api.run("money.retry_due")
    made = [r for r in payments.refunds if r["amount"] == kobo(100_00)]
    assert len(made) == 1
    api.clock.advance(minutes=30)
    assert await api.run("money.retry_due") == 0  # nothing left to do, nothing paid twice
    async with api.services() as svc:
        assert (await svc.reconciliation.run()).matched


async def test_payouts_are_sent_after_saving_and_never_twice(api):
    _, brand, influencer = await hire_me_setup(api)
    contract = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))
    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/accept", influencer)
    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/deliver", influencer, json={"delivery": "done"})
    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/approve", brand)
    transfers = api.infra.providers.payments.main.transfers
    assert (await api.run("payouts.run_cycle"))["sending"] == 1
    assert len(transfers) == 1 and (await api.ok("GET", "/payouts", influencer))[0]["status"] == "paid"

    # A payout left "processing" (say the server stopped after the transfer) is finished without paying twice.
    async with api.services() as svc:
        payout = await svc.payouts.repo(Payout).first(Payout.earner_id == influencer.id)
        payout.status = PayoutStatus.PROCESSING
    api.clock.advance(minutes=11)
    assert await api.run("payouts.finish_stuck") == 1
    assert len(transfers) == 1 and (await api.ok("GET", "/payouts", influencer))[0]["status"] == "paid"


async def test_racing_for_the_same_handle_gives_a_clear_409(api):
    from app.models import Account
    from app.repositories.base import Repository

    tokens = []
    for email in ("one@example.com", "two@example.com"):
        await api.ok("POST", "/auth/signup/request-otp", json={"email": email})
        tokens.append((await api.ok("POST", "/auth/signup/verify-otp",
                                    json={"email": email, "code": api.email_code(email)}))["signup_token"])
    body = {"account_type": "user", "password": "correct-horse-battery", "handle": "samehandle", "name": "X",
            "date_of_birth": "1990-01-01"}
    await api.ok("POST", "/auth/signup/complete", json=body | {"signup_token": tokens[0]})
    real_exists = Repository.exists

    async def blind(self, *where):  # the normal "is it taken?" check misses, as it can in a race
        return False if self.model is Account else await real_exists(self, *where)

    Repository.exists = blind
    try:
        r = await api.call("POST", "/auth/signup/complete", json=body | {"signup_token": tokens[1]}, expect=409)
    finally:
        Repository.exists = real_exists
    assert r.json()["code"] == "handle_taken"


async def test_a_redis_outage_degrades_instead_of_failing(api):
    user = await api.signup("ada")

    async def down(*_a, **_k):
        raise ConnectionError("redis down")

    strict = api.infra.kv.strict
    originals = {name: getattr(strict, name) for name in ("get", "set", "set_if_absent", "delete", "incr", "ttl", "delete_prefix")}
    for name in originals:
        setattr(strict, name, down)
    try:
        # Everyday use keeps working: rate limits and the cache are skipped, not fatal.
        await api.call("GET", "/feed", user, expect=200)
        await api.call("POST", "/posts", user, json={"type": "text", "text": "still here"}, expect=201)
        # Sign-in codes cannot be checked safely, so they say so.
        r = await api.call("POST", "/auth/signup/request-otp", json={"email": "new@example.com"}, expect=503)
        assert r.json()["code"] == "temporarily_unavailable" and r.headers["Retry-After"]
        # Health tells the truth.
        health = await api.call("GET", "/health", expect=503)
        assert {k: health.json()[k] for k in ("status", "database", "cache")} == {"status": "degraded", "database": "ok", "cache": "unavailable"}
        await api.call("GET", "/health/live", expect=200)
        # No scheduled job runs while its lock cannot be taken (money jobs must never run twice).
        assert await api.jobs.run_due() == []
    finally:
        for name, method in originals.items():
            setattr(strict, name, method)
    assert (await api.call("GET", "/health", expect=200)).json()["status"] == "ok"


async def test_every_error_has_the_same_shape_a_request_id_and_cors(api):
    origin = {"Origin": "http://localhost:5173"}
    r = await api.call("GET", "/no-such-endpoint", headers=origin, expect=404)
    assert r.json()["code"] == "not_found" and r.json()["request_id"] == r.headers["X-Request-ID"]
    assert (await api.call("DELETE", "/health", expect=405)).json()["code"] == "method_not_allowed"
    # The caller's own request ID is kept, so frontend and server logs line up.
    r = await api.call("GET", "/health", headers={"X-Request-ID": "web-1234abcd"}, expect=200)
    assert r.headers["X-Request-ID"] == "web-1234abcd"

    app = api.client._transport.app

    @app.get("/boom-test")
    async def boom():
        raise RuntimeError("kaboom")

    async with no_raise_client(api) as client:
        r = await client.get("/boom-test", headers=origin)
    assert r.status_code == 500 and r.json()["code"] == "internal_error" and "kaboom" not in r.text
    assert r.headers["access-control-allow-origin"] == origin["Origin"]  # the browser can read it
    assert r.json()["request_id"] == r.headers["X-Request-ID"]
