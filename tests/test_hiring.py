"""Epics 4, 5 and 6: Hire Me, likeness, minors' confirmation calls, agent orders, escrow and disputes."""
from datetime import timedelta

RATE = 1500  # sandbox NGN per USD
PAY = "tok_ok"


def kobo(cents: int) -> int:
    return cents * RATE


async def hire_me_setup(api, influencer_handle="tola"):
    admin = await api.admin()
    brand = await api.company("acme", admin)
    influencer = await api.influencer(influencer_handle, "Tola Bello")
    await api.ok("PUT", "/hire-me/page", influencer, json={"starting_price_cents": 50_00, "photos": ["p/1.jpg"]})
    await api.bank(influencer, "Tola Bello")
    return admin, brand, influencer


def offer(api, influencer, price=100_00, **extra):
    deadline = (api.clock.now() + timedelta(days=14)).isoformat()
    return {"influencer_id": influencer.id, "price_cents": price, "deliverables": "Two reels and a story",
            "deadline": deadline, "num_posts": 3, "payment_method": PAY, **extra}


async def test_hire_me_happy_path_fees_and_payout(api):
    admin, brand, influencer = await hire_me_setup(api)
    quote = await api.ok("POST", "/contracts/quote", brand, json={"type": "hire_me", "price_cents": 100_00})
    assert quote["usd_cents"] == {"price": 100_00, "fee_pct": 20, "fee": 20_00, "net": 80_00}

    # Only verified companies can send offers.
    newco = await api.signup("newco", kind="company", name="NewCo")
    await api.call("POST", "/hire-me/offers", newco, json=offer(api, influencer), expect=403)

    contract = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))
    assert contract["status"] == "offered" and contract["price"]["local"]["price"] == kobo(100_00)
    detail = await api.ok("GET", f"/contracts/{contract['id']}", influencer)
    assert detail["escrow"]["state"] == "funded" and detail["price"]["usd_cents"]["net"] == 80_00

    assert (await api.ok("POST", f"/hire-me/contracts/{contract['id']}/accept", influencer))["status"] == "active"
    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/start", influencer)
    delivered = await api.ok("POST", f"/hire-me/contracts/{contract['id']}/deliver", influencer, json={"delivery": "links"})
    assert delivered["status"] == "delivered" and delivered["review_ends_at"]
    assert (await api.ok("GET", "/earnings", influencer))["pending"] == kobo(80_00)

    completed = await api.ok("POST", f"/hire-me/contracts/{contract['id']}/approve", brand)
    assert completed["status"] == "completed"
    assert (await api.ok("GET", "/earnings", influencer))["cleared"] == kobo(80_00)

    summary = await api.run("payouts.run_cycle")
    assert summary["paid"] == 1
    earnings = await api.ok("GET", "/earnings", influencer)
    assert earnings["paid"] == kobo(80_00) and earnings["cleared"] == 0
    assert api.infra.providers.payments.transfers[0]["amount"] == kobo(80_00)
    # The next cycle is two weeks away.
    assert (await api.run("payouts.run_cycle"))["ran"] is False

    # Escrow balances reconcile with the payment provider: Wamiwa's 20% is what remains.
    async with api.services() as svc:
        run = await svc.reconciliation.run()
    assert run.matched and run.actual == kobo(20_00)

    await api.ok("POST", f"/contracts/{contract['id']}/rating", brand, json={"stars": 5})
    page = await api.ok("GET", f"/hire-me/influencers/{influencer.id}", brand)
    assert page["past_hires"] == 1 and page["rating"] == 5


async def test_hire_me_expiry_cancellation_and_auto_clear(api):
    _, brand, influencer = await hire_me_setup(api)
    refunds = api.infra.providers.payments.refunds

    # No response in 7 days: the offer expires and the brand is refunded in full.
    ignored = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))
    api.clock.advance(days=7, minutes=1)
    assert await api.run("contracts.expire_offers") == 1
    assert (await api.ok("GET", f"/contracts/{ignored['id']}", brand))["status"] == "expired"
    assert refunds[-1]["amount"] == kobo(100_00)

    # Brand cancels after work started: the influencer keeps 25% of the net amount.
    started = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))
    await api.ok("POST", f"/hire-me/contracts/{started['id']}/accept", influencer)
    await api.ok("POST", f"/hire-me/contracts/{started['id']}/start", influencer)
    await api.ok("POST", f"/hire-me/contracts/{started['id']}/cancel", brand, json={"reason": "Campaign pulled"})
    escrow = (await api.ok("GET", f"/contracts/{started['id']}", brand))["escrow"]
    assert escrow["net_amount"] == kobo(80_00) // 4 and escrow["refunded_amount"] == kobo(75_00)

    # Missed deadline: the brand can extend once, or cancel for a full refund.
    late = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))
    await api.ok("POST", f"/hire-me/contracts/{late['id']}/accept", influencer)
    await api.ok("POST", f"/hire-me/contracts/{late['id']}/start", influencer)
    api.clock.advance(days=15)
    new_deadline = (api.clock.now() + timedelta(days=3)).isoformat()
    await api.ok("POST", f"/hire-me/contracts/{late['id']}/extend-deadline", brand, json={"new_deadline": new_deadline})
    await api.call("POST", f"/hire-me/contracts/{late['id']}/extend-deadline", brand,
                   json={"new_deadline": (api.clock.now() + timedelta(days=9)).isoformat()}, expect=409)
    api.clock.advance(days=4)
    await api.ok("POST", f"/hire-me/contracts/{late['id']}/cancel", brand, json={"reason": ""})
    assert (await api.ok("GET", f"/contracts/{late['id']}", brand))["escrow"]["state"] == "refunded"

    # Delivered and not disputed: funds clear after the 7-day review window.
    quiet = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))
    await api.ok("POST", f"/hire-me/contracts/{quiet['id']}/accept", influencer)
    await api.ok("POST", f"/hire-me/contracts/{quiet['id']}/deliver", influencer, json={"delivery": "done"})
    api.clock.advance(days=6)
    assert await api.run("contracts.auto_clear") == 0
    api.clock.advance(days=1, minutes=1)
    assert await api.run("contracts.auto_clear") == 1
    assert (await api.ok("GET", f"/contracts/{quiet['id']}", brand))["escrow"]["state"] == "cleared"

    # "Not available to work as human" hides the page and blocks new offers.
    await api.ok("PUT", "/hire-me/availability", influencer, json={"available": False})
    await api.call("POST", "/hire-me/offers", brand, json=offer(api, influencer), expect=404)


async def likeness_setup(api, followers=12_000):
    admin = await api.admin()
    brand = await api.company("acme", admin, category="fintech")
    influencer = await api.influencer("zee", "Zee Musa", followers=followers)
    await api.ok("PUT", "/likeness/page", influencer, json={
        "price_image_cents": 100_00, "price_voice_cents": 150_00, "exclusivity_price_cents": 180_00})
    await api.ok("POST", "/likeness/assets/photos", influencer, json={"file_ref": "p/zee.jpg"})
    return admin, brand, influencer


def script_body(influencer, text="Hello, I use Acme to save every month.", **extra):
    return {"influencer_id": influencer.id, "script": text, "payment_method": PAY, **extra}


async def test_likeness_page_eligibility_and_pricing(api):
    small = await api.influencer("small", "Small Fry", followers=120)
    r = await api.call("PUT", "/likeness/page", small, json={"price_image_cents": 20_00}, expect=403)
    assert r.json()["code"] == "likeness_not_eligible"

    admin, brand, influencer = await likeness_setup(api)
    guide = await api.ok("GET", "/likeness/pricing-guide", influencer)
    assert guide["type"] == "follower_based" and guide["max_price_cents"] == 105_00
    r = await api.call("PUT", "/likeness/page", influencer, json={"price_image_cents": 150_00}, expect=422)
    assert r.json()["code"] == "price_above_ceiling"
    # Voice and exclusivity are charged on top with no cap; only the image-only price has a ceiling.
    await api.ok("PUT", "/likeness/page", influencer, json={
        "price_image_cents": 100_00, "price_voice_cents": 900_00, "exclusivity_price_cents": 2_500_00})
    await api.ok("PUT", "/likeness/page", influencer, json={
        "price_image_cents": 100_00, "price_voice_cents": 150_00, "exclusivity_price_cents": 180_00})
    # Photos must match the verified face.
    await api.call("POST", "/likeness/assets/photos", influencer, json={"file_ref": "p/nomatch.jpg"}, expect=422)
    # Voice: at least 3 in-app clips of 30-60 seconds and a separate consent box.
    await api.call("POST", "/likeness/assets/voice", influencer, json={
        "file_ref": "v/1.wav", "duration_seconds": 45, "recorded_in_app": True}, expect=422)
    for n in range(3):
        voice = await api.ok("POST", "/likeness/assets/voice", influencer, json={
            "file_ref": f"v/{n}.wav", "duration_seconds": 45, "recorded_in_app": True, "voice_consent": True})
    assert voice["voice_enabled"]

    # The brand sees a niche warning and a potential rating, and the influencer an anonymous view notice.
    seen = await api.ok("GET", f"/likeness/influencers/{influencer.id}", brand)
    assert "fashion" in seen["warnings"][0] and seen["potential_rating"] > 0
    notes = await api.ok("GET", "/notifications", influencer)
    assert any(n["title"] == "A company in fintech viewed your likeness page" for n in notes)

    # Followers dropping below 500 pauses the listing.
    await api.set_followers(influencer, 300)
    await api.call("POST", "/likeness/contracts", brand, json=script_body(influencer), expect=409)


async def test_likeness_script_review_changes_consent_video_and_term(api):
    admin, brand, influencer = await likeness_setup(api)
    email = api.infra.providers.email

    # A flagged script goes to admin review before the influencer sees it.
    contract = await api.ok("POST", "/likeness/contracts", brand,
                            json=script_body(influencer, "Acme gives guaranteed returns on every deposit."))
    assert contract["status"] == "admin_review"
    assert (await api.ok("GET", f"/likeness/contracts/{contract['id']}", influencer))["scripts"] == []
    script_item = (await api.ok("GET", "/admin/queues/scripts", admin))[0]
    await api.decide(admin, "scripts", script_item["target_id"])
    cid = contract["id"]
    assert (await api.ok("GET", f"/likeness/contracts/{cid}", influencer))["status"] == "offered"

    # Up to 3 change rounds; after the last the brand must accept or withdraw.
    for round_no in (1, 2, 3):
        await api.ok("POST", f"/likeness/contracts/{cid}/change-requests", influencer, json={"request": "Drop the promise"})
        if round_no == 3:
            r = await api.call("POST", f"/likeness/contracts/{cid}/change-requests/respond", brand,
                               json={"action": "rejected"}, expect=409)
            assert r.json()["code"] == "final_round"
        current = await api.ok("POST", f"/likeness/contracts/{cid}/change-requests/respond", brand,
                               json={"action": "accepted", "new_script": f"I save with Acme. Take {round_no}."})
    assert current["status"] == "offered" and current["change_rounds_left"] == 0
    await api.call("POST", f"/likeness/contracts/{cid}/change-requests", influencer, json={"request": "Again"}, expect=409)

    # Accepting means signing consent for that exact script and taking responsibility for it.
    await api.call("POST", f"/likeness/contracts/{cid}/accept", influencer,
                   json={"consent_image": True, "accept_responsibility": False}, expect=422)
    active = await api.ok("POST", f"/likeness/contracts/{cid}/accept", influencer,
                          json={"consent_image": True, "accept_responsibility": True})
    assert active["status"] == "active" and active["scripts"][-1]["status"] == "approved"
    assert email.last_for(brand.email)["subject"] == "Your script was accepted"

    # Drafts first, then a final render; a visible counter shows what is left.
    draft = await api.ok("POST", f"/likeness/contracts/{cid}/videos", brand, json={"kind": "draft"})
    assert draft["status"] == "ready" and draft["counter"]["display"] == "3 drafts, 2 final renders left"
    final = await api.ok("POST", f"/likeness/contracts/{cid}/videos", brand, json={"kind": "final"})
    assert final["influencer_decision"] == "pending" and final["watermark_id"]
    r = await api.call("POST", f"/likeness/contracts/{cid}/posts", brand, json={"video_id": final["id"]}, expect=403)
    assert r.json()["code"] == "video_not_approved"

    # Rejected for not matching the signed script: the re-render is free.
    await api.ok("POST", f"/videos/{final['id']}/decision", influencer,
                 json={"approve": False, "reason": "script_mismatch", "note": "Line two was changed"})
    redo = await api.ok("POST", f"/likeness/contracts/{cid}/videos", brand, json={"kind": "final"})
    assert redo["counter"]["finals_left"] == 1
    # No answer in 72 hours: warned, then approved automatically.
    api.clock.advance(hours=49)
    assert (await api.run("videos.auto_approve")) == {"warned": 1, "approved": 0}
    api.clock.advance(hours=24)
    assert (await api.run("videos.auto_approve"))["approved"] == 1

    post = await api.ok("POST", f"/likeness/contracts/{cid}/posts", brand, json={"video_id": redo["id"], "caption": "New!"})
    assert post["label_display"] == "Sponsored + AI likeness" and post["sponsored_contract_id"] == cid

    # Extra final renders beyond the included 2 are charged.
    await api.ok("POST", f"/likeness/contracts/{cid}/videos", brand, json={"kind": "final"})
    r = await api.call("POST", f"/likeness/contracts/{cid}/videos", brand, json={"kind": "final"}, expect=402)
    assert r.json()["price_cents"] == 15_00
    await api.ok("POST", f"/likeness/contracts/{cid}/videos", brand, json={"kind": "final", "payment_method": PAY})

    # Early disengagement on day 10 of 30: unused days refunded pro rata (under the 75% ceiling).
    api.clock.advance(days=7)
    ended = await api.ok("POST", f"/likeness/contracts/{cid}/disengage", brand)
    escrow = (await api.ok("GET", f"/contracts/{cid}", brand))["escrow"]
    assert ended["status"] == "cancelled"
    assert 0.65 < escrow["refunded_amount"] / escrow["amount"] < 0.68
    # Posted videos stay up for 60 days, then come down.
    api.clock.advance(days=59)
    assert await api.run("posts.take_down_expired") == 0
    api.clock.advance(days=2)
    assert await api.run("posts.take_down_expired") == 1


async def test_likeness_term_end_misuse_and_rejected_offer_refund(api):
    admin, brand, influencer = await likeness_setup(api)
    payments = api.infra.providers.payments

    # A rejected offer is refunded in full, production fee included.
    rejected = await api.ok("POST", "/likeness/contracts", brand, json=script_body(influencer))
    before = len(payments.refunds)
    await api.ok("POST", f"/likeness/contracts/{rejected['id']}/reject", influencer, json={"reason": "Not for me"})
    assert sorted(r["amount"] for r in payments.refunds[before:]) == [kobo(45_00), kobo(100_00)]

    contract = await api.ok("POST", "/likeness/contracts", brand, json=script_body(influencer))
    cid = contract["id"]
    await api.ok("POST", f"/likeness/contracts/{cid}/accept", influencer,
                 json={"consent_image": True, "accept_responsibility": True})
    final = await api.ok("POST", f"/likeness/contracts/{cid}/videos", brand, json={"kind": "final"})
    await api.ok("POST", f"/videos/{final['id']}/decision", influencer, json={"approve": True})
    post = await api.ok("POST", f"/likeness/contracts/{cid}/posts", brand, json={"video_id": final["id"]})

    # Each hire runs for 1 month; then the review window opens and funds clear.
    api.clock.advance(days=30, minutes=1)
    assert await api.run("likeness.end_terms") == 1
    await api.call("POST", f"/likeness/contracts/{cid}/videos", brand, json={"kind": "draft"}, expect=409)
    api.clock.advance(days=7, minutes=1)
    assert await api.run("contracts.auto_clear") == 1
    assert (await api.ok("GET", "/earnings", influencer))["cleared"] == kobo(80_00)

    # Misuse reported: Wamiwa removes the content on-platform and can suspend the brand.
    reported = await api.ok("POST", f"/likeness/contracts/{cid}/misuse", influencer,
                            json={"details": "They re-cut the video with new claims."})
    assert reported["consent_withdrawn"]
    await api.decide(admin, "reports", reported["report_id"], payload={"suspend": True})
    await api.call("GET", f"/posts/{post['id']}", influencer, expect=404)
    assert (await api.ok("GET", "/accounts/me", brand))["status"] == "suspended"


async def test_minor_contract_needs_guardian_approval_and_call(api):
    admin = await api.admin()
    brand = await api.company("acme", admin, category="fashion")
    betting = await api.company("betnow", admin, category="betting")
    minor = await api.signup("teen", dob="2010-03-01", name="Teen Obi")
    guardian = await api.signup("mum", name="Ngozi Obi")
    await api.ok("POST", "/guardians/invite", minor, json={"contact": "mum@example.com"})
    token = api.infra.providers.email.last_for("mum@example.com")["body"].split("token=")[1]
    await api.ok("POST", "/guardians/accept", guardian, json={"token": token, "relationship": "mother",
                                                              "confirm_relationship": True})
    await api.verify_id(guardian, "Ngozi Obi")
    await api.bank(guardian, "Ngozi Obi")
    await api.ok("PUT", "/hire-me/page", minor, json={"starting_price_cents": 20_00})

    # Blocked categories for minors, and DMs only after the guardian approves the company.
    r = await api.call("POST", "/hire-me/offers", betting, json=offer(api, minor, 40_00), expect=403)
    assert r.json()["code"] == "blocked_category"
    await api.call("POST", "/messages", brand, json={"recipient_id": minor.id, "body": "Hi"}, expect=403)
    await api.ok("PUT", f"/guardians/minors/{minor.id}/companies/{brand.id}", guardian)
    await api.ok("POST", "/messages", brand, json={"recipient_id": minor.id, "body": "Hi"})
    assert len(await api.ok("GET", f"/guardians/minors/{minor.id}/chats", guardian)) == 1

    contract = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, minor, 40_00))
    cid = contract["id"]
    pending = await api.ok("POST", f"/hire-me/contracts/{cid}/accept", minor)
    assert pending["status"] == "pending_guardian" and pending["display_status"] == "Awaiting guardian"

    # Step 2: the guardian approves in the app (a verified phone is needed for the call).
    r = await api.call("POST", f"/guardians/confirmations/{cid}/approve", guardian, expect=422)
    assert r.json()["code"] == "phone_required"
    await api.verify_phone(guardian, "+2348011112222")
    status = await api.ok("POST", f"/guardians/confirmations/{cid}/approve", guardian)
    assert status["result"] == "awaiting_call" and status["call_attempts"] == 1
    call = api.infra.providers.voice.calls[-1]
    assert "Acme Ltd" in call["script"] and "40.00" in call["script"]

    # Step 3: a missed call is retried; a wrong code does nothing; code + "1" activates the contract.
    secret = api.infra.settings.voice_webhook_secret
    await api.call("POST", "/webhooks/voice", expect=401, json={"reference": call["reference"]})
    await api.call("POST", "/webhooks/voice", expect=200, **api.signed(secret, {"reference": call["reference"], "answered": False}))
    api.clock.advance(hours=24, minutes=1)
    assert await api.run("guardian.retry_calls") == 1
    code = api.sms_code("+2348011112222")
    await api.call("POST", "/webhooks/voice", expect=200, **api.signed(
        secret, {"reference": call["reference"], "answered": True, "otp": "000000", "digit": "1"}))
    assert (await api.ok("GET", f"/contracts/{cid}", brand))["status"] == "pending_guardian"
    done = await api.call("POST", "/webhooks/voice", expect=200, **api.signed(
        secret, {"reference": call["reference"], "answered": True, "otp": code, "digit": "1",
                 "recording_ref": "rec/123.mp3", "call_id": "c1"}))
    assert done.json()["data"]["result"] == "approved" and done.json()["data"]["recorded"]
    assert (await api.ok("GET", f"/contracts/{cid}", brand))["status"] == "active"

    # The minor sees the earnings; the payout goes to the guardian's bank account.
    await api.ok("POST", f"/hire-me/contracts/{cid}/deliver", minor, json={"delivery": "done"})
    await api.ok("POST", f"/hire-me/contracts/{cid}/approve", brand)
    assert (await api.ok("GET", "/earnings", minor))["cleared"] == kobo(32_00)
    await api.run("payouts.run_cycle")
    payout = (await api.ok("GET", "/payouts", guardian))[0]
    assert payout["status"] == "paid" and payout["earner_id"] == minor.id and payout["account_id"] == guardian.id
    recordings = await api.ok("GET", "/admin/minor-contracts", admin)
    assert recordings[0]["recording_ref"] == "rec/123.mp3"

    # No confirmation within 7 days: the offer expires and escrow is refunded.
    second = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, minor, 40_00))
    await api.ok("POST", f"/hire-me/contracts/{second['id']}/accept", minor)
    api.clock.advance(days=7, minutes=1)
    assert await api.run("guardian.expire") == 1
    assert (await api.ok("GET", f"/contracts/{second['id']}", brand))["escrow"]["state"] == "refunded"


async def test_agent_order_escrow_revision_and_fee(api):
    owner = await api.influencer("owner", "Owner Person")
    hirer = await api.signup("hirer")
    agent = await api.live_agent(owner, rates={"research": 50_00})
    listing = await api.call("GET", "/marketplace/agents?skill=research&max_price_cents=6000", hirer, expect=200)
    assert listing.json()["data"][0]["owner"]["handle"] == "owner"

    order = await api.ok("POST", f"/marketplace/agents/{agent['id']}/orders", hirer,
                         json={"task": "research", "brief": "Summarise the fintech market", "payment_method": PAY})
    oid = order["id"]
    assert order["display_status"] == "Order received" and order["price"]["usd_cents"]["fee_pct"] == 10
    created = api.agent_endpoint.of_type("order.created")[0]
    assert created["data"]["brief"]["untrusted"] is True

    # The agent cannot be deleted while a paid order is open.
    await api.call("DELETE", f"/agents/{agent['id']}", owner, expect=409)

    await api.ok("POST", f"/api/agent/v1/orders/{oid}/status", headers=agent["headers"], json={"note": "On it"})
    delivered = await api.ok("POST", f"/api/agent/v1/orders/{oid}/deliver", headers=agent["headers"],
                             json={"delivery": "Report v1", "event_id": created["id"]})
    assert delivered["display_status"] == "Delivered"
    # One revision, then approve or dispute.
    await api.ok("POST", f"/orders/{oid}/revision", hirer, json={"note": "Add sources"})
    assert api.agent_endpoint.of_type("order.revision_requested")
    await api.ok("POST", f"/api/agent/v1/orders/{oid}/deliver", headers=agent["headers"], json={"delivery": "Report v2"})
    await api.call("POST", f"/orders/{oid}/revision", hirer, json={"note": "More"}, expect=409)
    await api.ok("POST", f"/orders/{oid}/approve", hirer)

    escrow = (await api.ok("GET", f"/contracts/{oid}", hirer))["escrow"]
    assert escrow["fee_amount"] == kobo(5_00) and escrow["net_amount"] == kobo(45_00)
    assert (await api.ok("GET", f"/marketplace/agents/{agent['id']}", hirer))["completed_orders"] == 1
    # The order thread is the only place an agent talks to its hirer.
    thread = (await api.ok("GET", f"/contracts/{oid}", hirer))["conversation_id"]
    assert len((await api.ok("GET", f"/conversations/{thread}/messages", hirer))["messages"]) == 1
    await api.ok("DELETE", f"/agents/{agent['id']}", owner)

    # If the agent cannot complete the task, the owner refunds in full.
    agent2 = await api.live_agent(owner, "secondbot", rates={"research": 50_00})
    order2 = await api.ok("POST", f"/marketplace/agents/{agent2['id']}/orders", hirer,
                          json={"task": "research", "brief": "Another brief", "payment_method": PAY})
    await api.ok("POST", f"/orders/{order2['id']}/refund", owner, json={"note": "Out of scope"})
    assert (await api.ok("GET", f"/contracts/{order2['id']}", hirer))["escrow"]["state"] == "refunded"
    assert api.agent_endpoint.of_type("order.cancelled")


async def test_dispute_split_appeal_and_final_decision(api):
    admin, brand, influencer = await hire_me_setup(api)
    invite = await api.ok("POST", "/admin/invites", admin, json={
        "email": "mod@wamiwa-admin.com", "role": "admin", "reason": "New moderator"})
    token = api.infra.providers.email.last_for("mod@wamiwa-admin.com")["body"].split("token=")[1]
    setup = await api.ok("POST", "/admin/invites/accept", json={
        "token": token, "password": "correct-horse-battery", "handle": "mod", "name": "Mod"})
    # Two-factor login is mandatory for admins.
    await api.call("POST", "/auth/login", json={"email": "mod@wamiwa-admin.com", "password": "correct-horse-battery"}, expect=403)
    await api.ok("POST", "/admin/invites/confirm-2fa", json={
        "setup_token": setup["setup_token"], "code": api.infra.totp.now(setup["totp_secret"])})
    mod = await api.login("mod@wamiwa-admin.com", "mod", setup["totp_secret"])
    assert invite["role"] == "admin"

    contract = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))
    cid = contract["id"]
    await api.ok("POST", f"/hire-me/contracts/{cid}/accept", influencer)
    # A dispute can only be opened in the review window.
    await api.call("POST", f"/contracts/{cid}/dispute", brand, json={"reason": "Too early to say"}, expect=409)
    await api.ok("POST", f"/hire-me/contracts/{cid}/deliver", influencer, json={"delivery": "one reel", "partial": True})
    dispute = await api.ok("POST", f"/contracts/{cid}/dispute", brand, json={"reason": "Only one of three posts delivered"})
    did = dispute["id"]
    assert (await api.ok("GET", f"/contracts/{cid}", brand))["escrow"]["state"] == "frozen"
    await api.run("contracts.auto_clear")  # frozen money never auto-clears
    await api.ok("POST", f"/disputes/{did}/evidence", brand, json={"statement": "See the brief"})

    api.clock.advance(days=5, minutes=1)
    await api.run("disputes.close_evidence_windows")
    view = await api.ok("GET", f"/admin/disputes/{did}", mod)
    assert view["missing_evidence_from"] == ["earner"]  # missing evidence counts against that side
    decided = await api.ok("POST", f"/admin/disputes/{did}/decide", mod,
                           json={"decision": "split", "release_pct": 40, "reason": "One of three posts was delivered"})
    assert decided["status"] == "decided" and decided["release_pct"] == 40
    assert (await api.ok("GET", f"/contracts/{cid}", brand))["escrow"]["state"] == "frozen"

    # Each side can appeal once; only a Super Admin decides, and that is final.
    await api.ok("POST", f"/disputes/{did}/appeal", influencer, json={"reason": "The other two were agreed verbally"})
    await api.call("POST", f"/admin/disputes/{did}/decide-appeal", mod,
                   json={"decision": "release", "reason": "Not allowed"}, expect=403)
    final = await api.ok("POST", f"/admin/disputes/{did}/decide-appeal", admin,
                         json={"decision": "split", "release_pct": 50, "reason": "Half the work was usable"})
    assert final["status"] == "final"
    escrow = (await api.ok("GET", f"/contracts/{cid}", brand))["escrow"]
    assert escrow["state"] == "cleared" and escrow["refunded_amount"] == kobo(50_00) and escrow["net_amount"] == kobo(40_00)
    await api.call("POST", f"/disputes/{did}/appeal", brand, json={"reason": "We want a full refund"}, expect=409)
    log = await api.ok("GET", f"/admin/audit-log?target_id={did}", admin)
    assert {e["action"] for e in log} == {"dispute.decide", "dispute.appeal_decide"}


async def test_out_of_sync_renders_are_redone_free_and_never_delivered(api):
    admin, brand, influencer = await likeness_setup(api)

    async def active_contract(script):
        contract = await api.ok("POST", "/likeness/contracts", brand, json=script_body(influencer, script))
        await api.ok("POST", f"/likeness/contracts/{contract['id']}/accept", influencer,
                     json={"consent_image": True, "accept_responsibility": True})
        return contract["id"]

    # Every request asks the provider for lip sync and matching facial expression.
    cid = await active_contract("Hello, [desync] I save with Acme every month.")
    final = await api.ok("POST", f"/likeness/contracts/{cid}/videos", brand, json={"kind": "final"})
    requests = list(api.infra.providers.video.requests.values())
    assert all(r.lip_sync and r.expressive for r in requests)
    # The first render came back out of sync, so it was redone automatically and counted once.
    assert len(requests) == 2 and final["status"] == "ready" and final["counter"]["finals_left"] == 1

    # A render that stays out of sync is never shown to anyone, and the allowance is given back.
    cid = await active_contract("Hello, [desync-always] I save with Acme every month.")
    failed = await api.ok("POST", f"/likeness/contracts/{cid}/videos", brand, json={"kind": "final"})
    assert failed["status"] == "failed" and failed["file"] is None and failed["counter"]["finals_left"] == 2
    assert (await api.ok("GET", f"/likeness/contracts/{cid}/videos", influencer))["videos"] == [] or all(
        v["status"] == "failed" for v in (await api.ok("GET", f"/likeness/contracts/{cid}/videos", influencer))["videos"])
