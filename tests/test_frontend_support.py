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
    doc = await api.ok("POST", "/uploads", user, data={"kind": "id_document"}, files={"file": ("id.png", b"\x89PNG\r\n\x1a\n" + b"0" * 10, "image/png")})
    assert doc["url"] is None
    await api.call("GET", f"/media/{doc['ref']}", expect=404)
    await api.call("GET", "/media/image/../../etc/passwd", expect=404)
    # Uploading needs a login.
    await api.call("POST", "/uploads", data={"kind": "image"}, files=files, expect=401)


async def test_uploads_are_checked_owned_and_cleaned_up(api, tmp_path):
    api.infra.settings.upload_dir = str(tmp_path)
    owner, other = await api.signup("ada", name="Ada Obi"), await api.signup("bola")
    jpeg = {"file": ("look.jpg", b"\xff\xd8\xff" + b"0" * 64, "image/jpeg")}
    # The file's own bytes must match the type it claims, and an empty file is refused.
    page = {"file": ("look.jpg", b"<html><script>alert(1)</script></html>", "image/jpeg")}
    r = await api.call("POST", "/uploads", owner, data={"kind": "image"}, files=page, expect=422)
    assert r.json()["code"] == "file_content_mismatch"
    r = await api.call("POST", "/uploads", owner, data={"kind": "image"}, files={"file": ("e.jpg", b"", "image/jpeg")}, expect=422)
    assert r.json()["code"] == "empty_file"

    photo = await api.ok("POST", "/uploads", owner, data={"kind": "image"}, files=jpeg)
    served = await api.call("GET", photo["url"], expect=200)
    assert served.headers["x-content-type-options"] == "nosniff" and "sandbox" in served.headers["content-security-policy"]

    # Only the uploader can use a file, and only where that kind of file belongs.
    r = await api.call("POST", "/posts", other, json={"type": "video", "media_ref": photo["ref"]}, expect=404)
    assert r.json()["code"] == "file_not_found"
    r = await api.call("PATCH", "/accounts/me/profile", owner, json={"photo": "image/" + "0" * 32 + ".jpg"}, expect=404)
    r = await api.call("POST", "/verification/identity", owner, json={
        "document_type": "passport", "document_ref": "Ada Obi|1990-05-01", "selfie_ref": photo["ref"]}, expect=422)
    assert r.json()["code"] == "wrong_file_kind"  # a saved photo is not a live check
    await api.ok("PATCH", "/accounts/me/profile", owner, json={"photo": photo["ref"]})

    # A file nobody used is deleted after a day; a used one stays.
    unused = await api.ok("POST", "/uploads", owner, data={"kind": "image"}, files=jpeg)
    api.clock.advance(hours=25)
    assert await api.run("uploads.purge_unused") == 1
    assert not (tmp_path / unused["ref"]).exists() and (tmp_path / photo["ref"]).exists()

    # Closing an account deletes its files, not just the references to them.
    await api.ok("POST", "/accounts/me/deletion", owner, json={})
    api.clock.advance(days=31)
    await api.run("deletion.purge_due")
    assert not (tmp_path / photo["ref"]).exists()


async def test_the_live_site_refuses_files_that_were_not_uploaded(api):
    user = await api.signup("ada")
    api.infra.settings.env = "production"  # only the file rule is being checked here
    try:
        r = await api.call("POST", "/posts", user, json={"type": "video", "media_ref": "v/made-up.mp4"}, expect=422)
        assert r.json()["code"] == "invalid_file_ref"
        await api.ok("POST", "/verification/popularity/signals", user, json={  # a link is fine where proof can be one
            "category": "recognition", "title": "Headies 2025", "evidence_ref": "https://headies.example.com/winners"})
    finally:
        api.infra.settings.env = "development"


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
    assert len(feed.json()["data"]) == 4  # human posts only; the agent post is in the Agents feed
    market = await api.call("GET", "/marketplace/agents", headers={"Authorization": f"Bearer {kemi['access_token']}"}, expect=200)
    assert {a["handle"] for a in market.json()["data"]} >= {"scribe", "lens", "wamiwa"}
    root = await api.ok("POST", "/auth/login", json={"email": "dami@example.com", "password": DEMO_PASSWORD,
                                                      "totp_code": api.infra.totp.now(result.admin_totp["dami@example.com"])})
    counts = await api.call("GET", "/admin/accounts/count", headers={"Authorization": f"Bearer {root['access_token']}"}, expect=200)
    assert counts.json()["data"]["users"] == 4
    with pytest.raises(RuntimeError, match="already there"):
        await DemoSeeder(api.infra).run()
