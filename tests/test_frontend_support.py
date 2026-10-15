"""What the web frontend relies on: CORS, file uploads, the development sign-in code, demo data."""
import pytest

from app.core.config import Settings
from app.main import create_app
from tests.conftest import build_api

ORIGIN = "http://localhost:5173"


async def test_cors_lets_the_frontend_call_the_api(api):
    # The browser's preflight for a write with a login token and an Idempotency-Key.
    r = await api.call("OPTIONS", "/posts", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type,idempotency-key"}, expect=200)
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert "idempotency-key" in r.headers["access-control-allow-headers"].lower()
    # Real responses, including errors, carry the header and expose the rate-limit headers to the page.
    user = await api.signup("ada")
    ok = await api.call("GET", "/accounts/me", user, headers={"Origin": ORIGIN}, expect=200)
    assert ok.headers["access-control-allow-origin"] == ORIGIN
    assert "x-ratelimit-remaining" in ok.headers["access-control-expose-headers"].lower()
    denied = await api.call("GET", "/accounts/me", headers={"Origin": ORIGIN}, expect=401)
    assert denied.headers["access-control-allow-origin"] == ORIGIN
    # Sites that are not on the list get no permission.
    other = await api.call("GET", "/health", headers={"Origin": "https://evil.example.com"}, expect=200)
    assert "access-control-allow-origin" not in other.headers


async def test_uploads_by_kind(api, tmp_path):
    api.infra.settings.upload_dir = str(tmp_path)
    api.client._transport.app.state.__dict__.pop("storage", None)
    user = await api.signup("ada")
    files = {"file": ("look.jpg", b"\xff\xd8\xff" + b"0" * 2048, "image/jpeg")}
    photo = await api.ok("POST", "/uploads", user, data={"kind": "image"}, files=files)
    assert photo["ref"].startswith("image/") and photo["url"] == f"/media/{photo['ref']}" and photo["size_bytes"] == 2051
    served = await api.call("GET", photo["url"], expect=200)
    assert served.content.startswith(b"\xff\xd8\xff") and "immutable" in served.headers["cache-control"]
    # The returned ref is what other endpoints take.
    post = await api.ok("POST", "/posts", user, json={"type": "video", "media_ref": photo["ref"]})
    assert post["media"] == photo["ref"]
    # Wrong type, wrong kind, too big.
    r = await api.call("POST", "/uploads", user, data={"kind": "image"}, files={"file": ("x.exe", b"MZ", "application/x-msdownload")}, expect=422)
    assert r.json()["code"] == "unsupported_file_type"
    await api.call("POST", "/uploads", user, data={"kind": "selfie"}, files=files, expect=422)
    api.infra.settings.upload_max_bytes["image"] = 1000
    r = await api.call("POST", "/uploads", user, data={"kind": "image"}, files=files, expect=422)
    assert r.json()["code"] == "file_too_large" and not any(tmp_path.rglob("*.part"))
    # ID documents are stored but never served back.
    doc = await api.ok("POST", "/uploads", user, data={"kind": "id_document"}, files={"file": ("id.png", b"\x89PNG" + b"0" * 10, "image/png")})
    assert doc["url"] is None
    await api.call("GET", f"/media/{doc['ref']}", expect=404)
    await api.call("GET", "/media/image/../../etc/passwd", expect=404)
    # Uploading needs a login.
    await api.call("POST", "/uploads", data={"kind": "image"}, files=files, expect=401)


async def test_development_sign_in_code():
    api = await build_api(dev_fixed_otp="123456")
    try:
        await api.ok("POST", "/auth/signup/request-otp", json={"email": "dev@example.com"})
        await api.ok("POST", "/auth/signup/verify-otp", json={"email": "dev@example.com", "code": "123456"})
    finally:
        await api.client.aclose()
        await api.infra.db.dispose()
    # Never in production.
    with pytest.raises(RuntimeError, match="DEV_FIXED_OTP"):
        create_app(Settings(env="production", redis_url="redis://x", dev_fixed_otp="123456", jwt_secret="a" * 40,
                            hash_pepper="b", payment_webhook_secret="c", voice_webhook_secret="d", video_webhook_secret="e"))


async def test_demo_seed_gives_every_persona_working_data(api):
    from app.demo_seed import DEMO_PASSWORD, DemoSeeder

    result = await DemoSeeder(api.infra).run()
    assert {role for role, _, _ in result.accounts} == {"Influencer", "Minor (16)", "Guardian", "Company", "Agent owner"}
    kemi = await api.ok("POST", "/auth/login", json={"email": "kemi@example.com", "password": DEMO_PASSWORD})
    me = await api.call("GET", "/accounts/me", headers={"Authorization": f"Bearer {kemi['access_token']}"}, expect=200)
    assert me.json()["data"]["profile"]["verified_creator"] and me.json()["data"]["profile"]["id_verified"]
    kiki = await api.ok("POST", "/auth/login", json={"email": "kiki@example.com", "password": DEMO_PASSWORD})
    assert kiki["account"]["status"] == "active"
    feed = await api.call("GET", "/feed", headers={"Authorization": f"Bearer {kemi['access_token']}"}, expect=200)
    assert len(feed.json()["data"]) == 2
    market = await api.call("GET", "/marketplace/agents", headers={"Authorization": f"Bearer {kemi['access_token']}"}, expect=200)
    assert {a["handle"] for a in market.json()["data"]} >= {"scribe", "lens", "wamiwa"}
    root = await api.ok("POST", "/auth/login", json={"email": "dami@example.com", "password": DEMO_PASSWORD,
                                                      "totp_code": api.infra.totp.now(result.admin_totp["dami@example.com"])})
    counts = await api.call("GET", "/admin/accounts/count", headers={"Authorization": f"Bearer {root['access_token']}"}, expect=200)
    assert counts.json()["data"]["users"] == 4
    with pytest.raises(RuntimeError, match="already there"):
        await DemoSeeder(api.infra).run()
