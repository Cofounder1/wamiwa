"""Test mode until a Super Admin presses Go live: test payment keys and a reset for the test data before launch, live
keys after, with no way back."""
import pytest

from app.container import build_providers
from app.core.config import Settings
from app.infra.launch import LaunchState
from app.models import Account
from app.reset import AlreadyLive, reset
from tests.test_hiring import hire_me_setup, offer

from tests.conftest import PASSWORD  # noqa: E402


async def super_admin(api, handle="boss"):
    """A Super Admin and their authenticator secret, for the fresh code Go live needs."""
    async with api.services() as svc:
        created = await svc.admin.bootstrap_super_admin(f"{handle}@wamiwa-admin.com", PASSWORD, handle)
    return await api.login(f"{handle}@wamiwa-admin.com", handle, created["totp_secret"]), created["totp_secret"]


async def test_reset_then_go_live_once_and_for_good(api):
    admin, secret = await super_admin(api)
    await hire_me_setup(api)
    brand = await api.company("kofi", admin)
    influencer = await api.influencer("ada", "Ada Obi")
    await api.ok("PUT", "/hire-me/page", influencer, json={"starting_price_cents": 50_00})
    await api.bank(influencer, "Ada Obi")
    await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))
    assert (await api.ok("GET", "/platform/status"))["test_mode"]

    # Only a Super Admin, with a fresh code and "GO LIVE" typed out; and never while test money is recorded.
    code = lambda: api.infra.totp.now(secret)  # noqa: E731
    r = await api.call("POST", "/admin/platform/go-live", admin, json={"totp_code": "000000", "confirm": "GO LIVE"},
                       expect=401)
    assert r.json()["code"] == "2fa_invalid"
    r = await api.call("POST", "/admin/platform/go-live", admin, json={"totp_code": code(), "confirm": "go"}, expect=422)
    assert r.json()["code"] == "confirmation_required"
    r = await api.call("POST", "/admin/platform/go-live", admin, json={"totp_code": code(), "confirm": "GO LIVE"},
                       expect=409)
    assert r.json()["code"] == "reset_required" and r.json()["left"]["escrow"] == 1
    assert (await api.ok("GET", "/admin/platform", admin))["test_money_left"]["escrow"] == 1

    # The reset shows what goes, then deletes it: only the admins and the Wamiwa Agent remain.
    async with api.services() as svc:
        await svc.promos.ensure_agent()
    async with api.services() as svc:
        dry = await reset(svc.admin.ctx.uow, api.infra.settings, execute=False)
        assert dry.deletions["escrow_transactions"] == 1 and dry.deletions["accounts"] >= 4
    async with api.services() as svc:
        await reset(svc.admin.ctx.uow, api.infra.settings, execute=True)
    async with api.services() as svc:
        left = await svc.admin.repo(Account).list()
        agent = api.infra.settings.wamiwa_agent_handle
        assert {a.handle for a in left} == {"boss", "root", agent, f"{agent}_official"}

    # Go live: once, recorded, and after it neither the reset nor a second Go live works.
    live = await api.ok("POST", "/admin/platform/go-live", admin, json={"totp_code": code(), "confirm": "GO LIVE"})
    assert live["launched_at"]
    r = await api.call("POST", "/admin/platform/go-live", admin, json={"totp_code": code(), "confirm": "GO LIVE"},
                       expect=409)
    assert r.json()["code"] == "already_live"
    with pytest.raises(AlreadyLive):
        async with api.services() as svc:
            await reset(svc.admin.ctx.uow, api.infra.settings, execute=True)
    assert (await api.ok("GET", "/platform/status"))["launched_at"]


async def test_the_live_site_uses_test_keys_until_go_live_then_live_keys(api):
    """On the live site, both key sets are loaded and the launch state picks one."""
    settings = Settings(_env_file=None, env="production", flutterwave_secret_key="FLWSECK-live",
                        flutterwave_sandbox_secret_key="FLWSECK_TEST-x", paypal_client_id="live-id",
                        paypal_secret="live-secret", paypal_environment="live", paypal_sandbox_client_id="sb-id",
                        paypal_sandbox_secret="sb-secret")
    launch = LaunchState(api.infra.db, api.infra.kv)
    providers = build_providers(settings, launch)
    test = await providers.payments.current()
    assert test.main._client.headers["Authorization"] == "Bearer FLWSECK_TEST-x"
    assert (await providers.paypal.current()).environment == "sandbox"

    admin, secret = await super_admin(api)
    async with api.services() as svc:
        await svc.launch.go_live(await svc.admin.repo(Account).require(admin.id), api.infra.totp.now(secret), "GO LIVE")
    assert not launch._live  # another process: it learns of the launch from the database
    launch._until = 0
    await api.infra.kv.delete("platform:launched")
    live = await providers.payments.current()
    assert live.main._client.headers["Authorization"] == "Bearer FLWSECK-live"
    assert (await providers.paypal.current()).environment == "live"
