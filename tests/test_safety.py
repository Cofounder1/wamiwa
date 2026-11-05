"""AI-05 no personal information from agents, MS-05 chat summaries by agents, RP-02 racism and Yoruba insult reports."""
from tests.test_growth import _minor
from tests.test_hiring import PAY
from tests.test_platform import invite_admin

SECRET_CHAT = ["Hi, can we talk about the shoot?", "Sure. Call me on 08031234567 or email bisi@example.com",
               "Pay the deposit to 0123456789 GTB", "Great, Friday works for the shoot."]


async def _chat(api, a, b, lines):
    """A two-person chat; returns its id."""
    first = await api.ok("POST", "/messages", a, json={"recipient_id": b.id, "body": lines[0]})
    conversation_id = first["conversation_id"]
    for n, body in enumerate(lines[1:]):
        sender = b if n % 2 == 0 else a
        await api.ok("POST", f"/conversations/{conversation_id}/messages", sender, json={"body": body})
    return conversation_id


async def test_agents_never_give_out_personal_information(api):
    owner = await api.agent_owner()
    agent = await api.live_agent(owner)
    post = await api.ok("POST", "/posts", owner, json={"type": "text", "text": "Ask me anything"})
    for text in ("Her number is 0803 123 4567", "Write to kemi@example.com", "Send it to account 0123456789"):
        r = await api.call("POST", f"/api/agent/v1/posts/{post['id']}/comments", headers=agent["headers"],
                           json={"text": text}, expect=422)
        assert r.json()["code"] == "personal_information"
    r = await api.call("POST", "/api/agent/v1/posts", headers=agent["headers"], json={"text": "DM me on +234 803 123 4567"}, expect=422)
    assert r.json()["found"] == ["phone number"]
    # Ordinary numbers are fine.
    await api.ok("POST", f"/api/agent/v1/posts/{post['id']}/comments", headers=agent["headers"],
                 json={"text": "Prices rose 38% to 150000 naira in 2026."})


async def test_pro_subscribers_hire_agents_to_summarize_chats_without_personal_details(api):
    owner = await api.agent_owner()
    agent = await api.live_agent(owner, "digest", rates={"chat_summary": 20_00, "research": 50_00})
    await api.live_agent(owner, "plainbot")  # offers no chat summaries
    me, other = await api.signup("tola", name="Tola Ade"), await api.signup("bisi", name="Bisi Ade")
    conversation_id = await _chat(api, me, other, SECRET_CHAT)
    body = {"conversation_id": conversation_id, "agent_id": agent["id"], "kind": "summary", "payment_method": PAY}

    r = await api.call("POST", "/chat-insights", me, json=body, expect=403)
    assert r.json()["code"] == "pro_required"
    await api.ok("POST", "/subscriptions", me, json={"period": "monthly", "tier": "pro", "payment_method": PAY})
    offers = await api.ok("GET", "/chat-insights/agents", me)
    assert [o["handle"] for o in offers] == ["digest"] and offers[0]["price_cents"] == 20_00

    order = await api.ok("POST", "/chat-insights", me, json=body | {"question": "What did we agree?"})
    assert order["shared_messages"] == 4 and order["personal_details_removed"] == 3
    brief = api.agent_endpoint.of_type("order.created")[-1]["data"]["brief"]["value"]
    assert "08031234567" not in brief and "bisi@example.com" not in brief and "0123456789" not in brief
    assert "[phone number removed]" in brief and "Focus on: What did we agree?" in brief and "You: Hi, can we talk" in brief

    # The agent's answer can't put personal details back.
    r = await api.call("POST", f"/api/agent/v1/orders/{order['id']}/deliver", headers=agent["headers"],
                       json={"delivery": "You agreed Friday. Bisi's number is 08031234567."}, expect=422)
    assert r.json()["code"] == "personal_information"
    await api.ok("POST", f"/api/agent/v1/orders/{order['id']}/deliver", headers=agent["headers"],
                 json={"delivery": "You agreed on a Friday shoot and a deposit before it."})

    # Someone who isn't in the chat can't share it.
    outsider = await api.signup("kola")
    await api.ok("POST", "/subscriptions", outsider, json={"period": "monthly", "tier": "pro", "payment_method": PAY})
    await api.call("POST", "/chat-insights", outsider, json=body, expect=404)


async def test_businesses_need_a_payment_in_the_last_90_days_and_minors_chats_stay_private(api):
    admin = await api.admin()
    owner = await api.agent_owner()
    agent = await api.live_agent(owner, "digest", rates={"chat_summary": 20_00})
    brand = await api.company("acme", admin)
    buyer = await api.company("shopco", admin)
    conversation_id = await _chat(api, brand, buyer, ["Can you stock our sneakers?", "Yes, send the price list"])
    body = {"conversation_id": conversation_id, "agent_id": agent["id"], "payment_method": PAY}
    r = await api.call("POST", "/chat-insights", brand, json=body, expect=403)
    assert r.json()["code"] == "paying_business_required"
    await api.ok("POST", "/video-plans", brand, json={"plan": "growth", "payment_method": PAY})
    await api.ok("POST", "/chat-insights", brand, json=body)
    api.clock.advance(days=91)
    await api.call("POST", "/chat-insights", brand, json=body, expect=403)

    # A chat with an under-18 is never shared with an agent.
    adult = await api.signup("tobi")
    await api.ok("POST", "/subscriptions", adult, json={"period": "monthly", "tier": "pro", "payment_method": PAY})
    minor = await _minor(api)
    minor_chat = await _chat(api, adult, minor, ["Hello", "Hi"])
    r = await api.call("POST", "/chat-insights", adult, json=body | {"conversation_id": minor_chat}, expect=403)
    assert r.json()["code"] == "minor_chat"


async def test_racism_and_yoruba_insult_reports_go_to_the_investigation_team(api):
    root = await api.admin()
    mod = await invite_admin(api, root, "mod")
    offender, reporter, viewer = await api.signup("offender"), await api.signup("ife", name="Ife Ade"), await api.signup("seyi")
    post = await api.ok("POST", "/posts", offender, json={"type": "text", "text": "An insulting post"})

    # One report from an unverified account doesn't hide anything on its own; a second reporter does.
    first, second_reporter = await api.signup("kayode"), await api.signup("bimpe")
    await api.ok("POST", "/reports", first, json={
        "target_type": "post", "target_id": post["id"], "reason": "racism_ethnic_insult", "details": "Insults"})
    await api.ok("GET", f"/posts/{post['id']}", viewer)
    await api.ok("POST", "/reports", second_reporter, json={
        "target_type": "post", "target_id": post["id"], "reason": "racism_ethnic_insult", "details": "Insults"})
    await api.call("GET", f"/posts/{post['id']}", viewer, expect=404)
    await api.verify_id(reporter, "Ife Ade")  # a verified reporter with a clean record is trusted straight away

    report = await api.ok("POST", "/reports", reporter, json={
        "target_type": "post", "target_id": post["id"], "reason": "racism_ethnic_insult", "details": "Insults Yoruba people"})
    # Hidden at once, but the account is not suspended until the team confirms.
    await api.call("GET", f"/posts/{post['id']}", viewer, expect=404)
    assert (await api.ok("GET", "/accounts/me", offender))["status"] == "active"
    items = await api.ok("GET", "/admin/queues/investigations", root)
    item = next(i for i in items if i["target_id"] == report["id"])
    assert item["severity"] == "high"

    # Only admins on the investigation team can take the case.
    r = await api.call("POST", f"/admin/queue-items/{item['id']}/claim", mod, expect=403)
    assert r.json()["code"] == "permission_denied"
    await api.ok("PUT", f"/admin/admins/{mod.id}/permissions", root, json={"permission": "investigate", "reason": "Investigation team"})
    await api.decide(mod, "investigations", report["id"], "approve", reason="Slur against Yoruba people confirmed")
    assert (await api.ok("GET", "/accounts/me", offender))["status"] == "suspended"
    await api.call("GET", f"/posts/{post['id']}", viewer, expect=404)
    assert offender.id in {b["id"] for b in await api.ok("GET", "/blocks", reporter)}

    # A report the team doesn't uphold brings the content back and suspends no one.
    other = await api.signup("dele")
    fine = await api.ok("POST", "/posts", other, json={"type": "text", "text": "Jollof debate: Ghana or Nigeria?"})
    second = await api.ok("POST", "/reports", reporter, json={
        "target_type": "post", "target_id": fine["id"], "reason": "racism_ethnic_insult", "details": "Offensive"})
    await api.call("GET", f"/posts/{fine['id']}", viewer, expect=404)
    await api.decide(mod, "investigations", second["id"], "reject", reason="No insult found")
    await api.ok("GET", f"/posts/{fine['id']}", viewer)
    assert (await api.ok("GET", "/accounts/me", other))["status"] == "active"


async def test_live_check_files_are_private_and_reach_verification(api, tmp_path):
    api.infra.settings.upload_dir = str(tmp_path)
    api.client._transport.app.state.__dict__.pop("storage", None)
    user = await api.signup("ada", name="Ada Obi")
    frame = await api.ok("POST", "/uploads", user, data={"kind": "liveness"},
                         files={"file": ("live-frame.jpg", b"\xff\xd8\xff" + b"0" * 512, "image/jpeg")})
    clip = await api.ok("POST", "/uploads", user, data={"kind": "liveness"},
                        files={"file": ("live-check.webm", b"\x1a\x45\xdf\xa3" + b"0" * 512, "video/webm")})
    assert frame["url"] is None and clip["url"] is None  # never served back, like ID documents
    await api.call("GET", f"/media/{frame['ref']}", expect=404)
    result = await api.ok("POST", "/verification/identity", user, json={
        "document_type": "passport", "document_ref": "Ada Obi|1990-05-01", "selfie_ref": frame["ref"],
        "liveness_ref": clip["ref"]})
    assert result["status"] == "passed"


async def test_a_registered_business_cannot_register_again(api):
    admin = await api.admin()
    first = await api.signup("zuri", kind="company", name="Zuri Fashion House Ltd", category="fashion")
    await api.ok("POST", "/companies/verification", first, json={"registration_no": "RC 123456", "document_ref": "docs/cac.pdf"})
    await api.decide(admin, "company_verification", first.id)

    same = await api.signup("zuri2", kind="company", name="ZURI FASHION HOUSE LIMITED", category="fashion")
    r = await api.call("POST", "/companies/verification", same, json={"registration_no": "rc-123456", "document_ref": "docs/cac.pdf"}, expect=409)
    assert r.json()["code"] == "company_already_registered" and "contact support" in r.json()["error"]
    other_name = await api.signup("acme", kind="company", name="Acme Ltd", category="fashion")
    r = await api.call("POST", "/companies/verification", other_name, json={"registration_no": "123456", "document_ref": "docs/cac.pdf"}, expect=409)
    assert r.json()["code"] == "registration_number_in_use"
    # BN (business name) numbers are a different registry, so the same digits are a different business.
    await api.ok("POST", "/companies/verification", other_name, json={"registration_no": "BN 123456", "document_ref": "docs/bn.pdf"})


async def test_live_site_refuses_stand_in_payments_and_id_checks():
    """Until Flutterwave and YouVerify are connected, nothing on the live site can be 'paid' for or 'verified' free."""
    import pytest
    from app.container import build_providers
    from app.core.config import Settings
    from app.core.exceptions import ServiceUnavailable

    live = build_providers(Settings(_env_file=None, env="production"))
    with pytest.raises(ServiceUnavailable) as paid:
        await live.payments.charge(reference="r", amount=100, currency="NGN", payment_method="tok_visa", customer_id="c")
    assert paid.value.code == "payments_unavailable"
    with pytest.raises(ServiceUnavailable) as checked:
        await live.identity.verify("passport", "Ada Obi|1990-05-01", "liveness/x.jpg")
    assert checked.value.code == "identity_unavailable"
    local = build_providers(Settings(_env_file=None, env="development"))
    assert type(local.payments.main).__name__ == "SandboxPaymentGateway"
    # PayPal has no stand-in anywhere: until it's connected it refuses, even in development.
    with pytest.raises(ServiceUnavailable) as paypal:
        await local.payments.charge(reference="r", amount=100, currency="NGN", payment_method="paypal:X", customer_id="c")
    assert paypal.value.code == "paypal_unavailable"


async def test_one_id_one_account(api):
    first, second = await api.signup("ada", name="Ada Obi"), await api.signup("ada2", name="Ada Obi")
    await api.verify_id(first, "Ada Obi")
    r = await api.ok("POST", "/verification/identity", second, json={
        "document_type": "passport", "document_ref": "ADA  OBI|1990-05-01", "selfie_ref": "selfie-2"})
    assert r["status"] == "failed" and "already verified on another Wamiwa account" in r["reason"]
    assert not (await api.ok("GET", "/accounts/me", second))["profile"]["id_verified"]
    admin = await api.admin()
    assert any(i["target_type"] == "duplicate_identity" for i in await api.ok("GET", "/admin/queues/identity", admin))


async def test_photo_location_is_removed_on_upload(api, tmp_path):
    import struct
    api.infra.settings.upload_dir = str(tmp_path)
    user = await api.signup("ada")
    exif = b"Exif\x00\x00GPS 6.4531N 3.3958E"
    jpeg = b"\xff\xd8\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif + b"\xff\xda\x00\x02PIXELS\xff\xd9"
    photo = await api.ok("POST", "/uploads", user, data={"kind": "image"}, files={"file": ("p.jpg", jpeg, "image/jpeg")})
    served = (await api.call("GET", photo["url"], expect=200)).content
    assert b"GPS" not in served and b"PIXELS" in served and photo["size_bytes"] == len(served)


async def test_scam_links_in_posts_and_spelled_numbers_from_agents_are_caught(api):
    author, reader = await api.signup("ada"), await api.signup("bola")
    held = await api.ok("POST", "/posts", author, json={"type": "text", "text": "Free money here https://bit.ly/claim-now"})
    assert held["moderation_status"] == "flagged"
    await api.call("GET", f"/posts/{held['id']}", reader, expect=404)
    fine = await api.ok("POST", "/posts", author, json={"type": "text", "text": "My shop: https://adaobi.example.com"})
    edited = await api.ok("PATCH", f"/posts/{fine['id']}", author, json={"text": "My shop: https://bit.ly/x1"})
    assert edited["moderation_status"] == "flagged"  # added after posting, still caught

    owner = await api.agent_owner()
    agent = await api.live_agent(owner)
    post = await api.ok("POST", "/posts", owner, json={"type": "text", "text": "Ask me"})
    r = await api.call("POST", f"/api/agent/v1/posts/{post['id']}/comments", headers=agent["headers"],
                       json={"text": "Her line is zero eight zero three one two three four five six seven"}, expect=422)
    assert r.json()["code"] == "personal_information"
