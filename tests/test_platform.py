"""Epics 8, 9, 13, 14: subscriptions, AI video, admin controls, queues, audit, dashboard, deletion, support."""
import pytest
from sqlalchemy import select

from app.models import AuditLog
from app.models.base import AppendOnlyError
from tests.test_hiring import PAY, hire_me_setup, kobo, offer


async def invite_admin(api, root, handle):
    email = f"{handle}@wamiwa-admin.com"
    await api.ok("POST", "/admin/invites", root, json={"email": email, "role": "admin", "reason": "Joining the team"})
    token = api.infra.providers.email.last_for(email)["body"].split("token=")[1]
    setup = await api.ok("POST", "/admin/invites/accept", json={
        "token": token, "password": "correct-horse-battery", "handle": handle, "name": handle.title()})
    # The invite link works once.
    await api.call("POST", "/admin/invites/accept", json={
        "token": token, "password": "correct-horse-battery", "handle": handle + "2", "name": "Again"}, expect=404)
    await api.ok("POST", "/admin/invites/confirm-2fa", json={
        "setup_token": setup["setup_token"], "code": api.infra.totp.now(setup["totp_secret"])})
    return await api.login(email, handle, setup["totp_secret"])


async def test_subscription_features_and_lapse(api):
    user = await api.influencer("sub", "Sub Scriber")
    owner = await api.influencer("owner", "Owner Person")
    agent = await api.live_agent(owner)
    await api.call("GET", "/post-ideas/today", user, expect=403)
    r = await api.call("PUT", f"/auto-reply?enabled=true&agent_id={agent['id']}", user, expect=403)
    assert r.json()["code"] == "subscription_required"

    plans = await api.ok("GET", "/subscriptions/plans", user)
    assert plans["monthly"]["local"] == kobo(10_00) and plans["yearly"]["usd_cents"] == 100_00
    await api.ok("POST", "/subscriptions", user, json={"period": "monthly", "payment_method": PAY})
    assert len((await api.ok("GET", "/post-ideas/today", user))["ideas"]) == 3

    # AG-05: the chosen agent replies for the influencer, labeled as such; the influencer can delete it.
    await api.ok("PUT", f"/auto-reply?enabled=true&agent_id={agent['id']}", user)
    fan = await api.signup("fan")
    post = await api.ok("POST", "/posts", user, json={"type": "text", "text": "New drop today"})
    comment = await api.ok("POST", f"/posts/{post['id']}/comments", fan, json={"text": "Where can I buy?"})
    event = api.agent_endpoint.of_type("autoreply.comment")[0]
    reply = await api.ok("POST", f"/api/agent/v1/posts/{post['id']}/comments", headers=agent["headers"], json={
        "text": "Link in bio!", "parent_id": comment["id"], "on_behalf_of": user.id, "event_id": event["id"]})
    assert reply["label_display"] == "Agent — replying for @sub"
    await api.ok("DELETE", f"/comments/{reply['id']}", user)
    # Humans rate agent comments as helpful or not.
    rated = await api.ok("PUT", f"/comments/{reply['id']}/rating", fan, json={"helpful": True})
    assert rated["helpful"] == {"yes": 1, "no": 0}

    # Cancelling keeps the plan to the end of the paid month, with notice the day before it stops.
    await api.ok("DELETE", "/subscriptions", user)
    api.clock.advance(days=29, hours=1)
    assert await api.run("subscriptions.send_lapse_notices") == 1
    api.clock.advance(days=1)
    assert (await api.run("subscriptions.process_renewals"))["lapsed"] == 1
    await api.call("GET", "/post-ideas/today", user, expect=403)
    r = await api.call("POST", f"/api/agent/v1/posts/{post['id']}/comments", headers=agent["headers"],
                       json={"text": "Still here", "on_behalf_of": user.id}, expect=403)
    assert r.json()["code"] == "not_auto_reply_agent"
    # A lapse never blocks earning: offers can still be accepted (checked via status only).
    assert (await api.ok("GET", "/subscriptions", user))["subscribed"] is False


async def test_personal_ai_video_pricing_and_refund(api):
    user = await api.influencer("maker", "Maker Person")
    stranger = await api.signup("stranger")
    await api.call("POST", "/ai-videos", stranger, json={"script": "Hello there", "seconds": 30}, expect=403)

    quote = await api.ok("GET", "/ai-videos/quote?seconds=90&tier=standard", user)
    assert quote["price_cents"] == 4_50  # $3 per minute
    assert (await api.ok("GET", "/ai-videos/quote?seconds=60&tier=premium", user))["price_cents"] == 25_00
    # Payment is taken before generation.
    await api.call("POST", "/ai-videos", user, json={"script": "Hello there", "seconds": 90}, expect=402)
    await api.call("POST", "/ai-videos", user, json={"script": "Hello", "seconds": 90, "payment_method": "tok_fail"}, expect=402)

    # Subscribers get 1 standard minute free each month.
    await api.ok("POST", "/subscriptions", user, json={"period": "yearly", "payment_method": PAY})
    video = await api.ok("POST", "/ai-videos", user, json={"script": "Hello there", "seconds": 90, "payment_method": PAY})
    assert video["status"] == "ready" and video["charged_cents"] == 1_50 and video["ai_label"] == "AI-generated"
    download = await api.ok("GET", f"/ai-videos/{video['id']}/download", user)
    assert download["watermarked"] and download["watermark_id"]
    assert (await api.ok("GET", "/ai-videos/quote?seconds=60&tier=standard", user))["price_cents"] == 3_00
    posted = await api.ok("POST", "/posts", user, json={"type": "video", "generated_video_id": video["id"]})
    assert posted["label_display"] == "Human + AI-generated"

    # Generation fails after payment: one retry, then an automatic refund.
    refunds = api.infra.providers.payments.main.refunds
    before = len(refunds)
    failed = await api.ok("POST", "/ai-videos", user, json={"script": "[fail] broken", "seconds": 60, "tier": "premium",
                                                           "payment_method": PAY})
    assert failed["status"] == "failed" and refunds[before]["amount"] == kobo(25_00)


async def test_admin_management_permissions_and_audit(api):
    root, root2 = await api.admin(), await api.admin("root2")
    mod = await invite_admin(api, root, "mod")
    target = await api.signup("target")

    # Admins get only the permissions a Super Admin grants them.
    r = await api.call("POST", f"/admin/accounts/{target.id}/suspend", mod, json={"reason": "Spam posting"}, expect=403)
    assert r.json()["code"] == "permission_denied"
    await api.call("PUT", f"/admin/admins/{mod.id}/permissions", mod,
                   json={"permission": "suspend", "reason": "Self service"}, expect=403)
    await api.ok("PUT", f"/admin/admins/{mod.id}/permissions", root, json={"permission": "suspend", "reason": "Trusted"})
    # Skipping the notice needs a Super Admin.
    await api.call("POST", f"/admin/accounts/{target.id}/suspend", mod,
                   json={"reason": "Fraud investigation", "notify": False}, expect=403)
    await api.ok("POST", f"/admin/accounts/{target.id}/suspend", mod, json={"reason": "Spam posting"})
    assert (await api.call("GET", "/feed", target)).json()["code"] == "account_suspended"
    # Admins cannot act on their own account or another admin's; only Super Admins remove accounts.
    await api.call("POST", f"/admin/accounts/{root.id}/suspend", mod, json={"reason": "Coup attempt"}, expect=403)
    await api.call("POST", f"/admin/accounts/{target.id}/remove", mod, json={"reason": "Gone for good"}, expect=403)
    # Every action needs a reason.
    await api.call("POST", f"/admin/accounts/{target.id}/reinstate", mod, json={"reason": ""}, expect=422)

    # A successful appeal restores the account.
    await api.ok("POST", "/accounts/me/appeal", target, json={"statement": "Those posts were not spam at all."})
    await api.decide(root, "reports", target.id)
    assert (await api.ok("GET", "/accounts/me", target))["status"] == "active"

    # There must always be at least two Super Admins.
    r = await api.call("DELETE", f"/admin/admins/{root2.id}", root, json={"reason": "Downsizing"}, expect=409)
    assert r.json()["code"] == "min_super_admins"
    await api.ok("DELETE", f"/admin/admins/{mod.id}", root, json={"reason": "Left the company"})
    await api.call("GET", "/admin/accounts", mod, expect=401)

    # The audit log records who, what, when and why, and cannot be edited or deleted.
    log = await api.ok("GET", "/admin/audit-log", root)
    assert {"admin.invite", "admin.grant", "account.suspend", "admin.remove"} <= {e["action"] for e in log}
    assert all(e["reason"] for e in log)
    async with api.infra.db.unit_of_work() as uow:
        entry = (await uow.session.execute(select(AuditLog).limit(1))).scalar_one()
        entry.reason = "rewritten history"
        with pytest.raises(AppendOnlyError):
            await uow.session.flush()
        await uow.rollback()


async def test_queue_claims_are_exclusive_and_health_is_tracked(api):
    root, root2 = await api.admin(), await api.admin("root2")
    company = await api.signup("acme", kind="company", name="Acme Ltd")
    await api.ok("POST", "/companies/verification", company, json={"registration_no": "RC1", "document_ref": "d.pdf"})
    item = (await api.ok("GET", "/admin/queues/company_verification", root))[0]

    claimed = await api.ok("POST", f"/admin/queue-items/{item['id']}/claim", root)
    assert claimed["context"]["account"]["handle"] == "acme"  # full context comes with the item
    r = await api.call("POST", f"/admin/queue-items/{item['id']}/claim", root2, expect=409)
    assert r.json()["code"] == "already_claimed"
    await api.call("POST", f"/admin/queue-items/{item['id']}/decide", root2,
                   json={"decision": "approve", "reason": "Looks fine"}, expect=409)
    # Past the 2-working-day target, the item shows as late.
    api.clock.advance(days=5)
    health = await api.ok("GET", "/admin/queues/health", root)
    assert health["per_queue"]["company_verification"] == {
        "waiting": 1, "past_target": 1, "decided": 0, "average_decision_hours": None}
    await api.ok("POST", f"/admin/queue-items/{item['id']}/decide", root, json={"decision": "reject", "reason": "Document unreadable"})
    assert (await api.ok("GET", "/accounts/me", company))["company"]["verified"] is False
    assert (await api.ok("GET", "/admin/queues/health", root))["per_admin"][root.id]["decided"] == 1
    # Unverified companies can browse but cannot pay or hire.
    await api.call("GET", "/feed", company, expect=200)
    await api.call("POST", "/video-plans", company, json={"plan": "growth", "payment_method": PAY}, expect=403)


async def test_manual_payout_needs_two_admins_above_the_limit():
    from tests.conftest import build_api

    api = await build_api(manual_payout_dual_approval_minor_units=kobo(50_00))
    try:
        root, brand, influencer = await hire_me_setup(api)
        root2 = await api.admin("root2")
        contract = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))
        await api.ok("POST", f"/hire-me/contracts/{contract['id']}/accept", influencer)
        await api.ok("POST", f"/hire-me/contracts/{contract['id']}/deliver", influencer, json={"delivery": "done"})
        await api.ok("POST", f"/hire-me/contracts/{contract['id']}/approve", brand)

        payout = await api.ok("POST", "/admin/payouts/manual", root, json={"earner_id": influencer.id, "reason": "Hardship request"})
        assert payout["status"] == "pending_approval" and payout["needs_second_approval"]
        items = await api.ok("GET", "/admin/queues/payouts", root)
        await api.ok("POST", f"/admin/queue-items/{items[0]['id']}/claim", root)
        r = await api.call("POST", f"/admin/queue-items/{items[0]['id']}/decide", root,
                           json={"decision": "approve", "reason": "Approving my own request"}, expect=403)
        assert r.json()["code"] == "same_approver"
        await api.ok("POST", f"/admin/queue-items/{items[0]['id']}/release", root)
        await api.decide(root2, "payouts", payout["id"])
        assert (await api.ok("GET", "/payouts", influencer))[0]["status"] == "paid"
        assert (await api.ok("GET", "/earnings", influencer))["paid"] == kobo(80_00)
    finally:
        await api.client.aclose()
        await api.infra.db.dispose()


async def test_chargeback_freezes_brand_and_deletion_blockers(api):
    root, brand, influencer = await hire_me_setup(api)
    contract = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))
    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/accept", influencer)

    # DL-01: money in escrow blocks deletion and the blockers are listed.
    r = await api.call("POST", "/accounts/me/deletion", influencer, expect=409)
    assert r.json()["blockers"][0]["code"] == "money_in_escrow"

    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/deliver", influencer, json={"delivery": "done"})
    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/approve", brand)
    await api.run("payouts.run_cycle")

    # Chargeback after payout: the brand is frozen and the earner keeps the money.
    async with api.services() as svc:
        provider_ref = (await svc.escrow.for_contract(contract["id"])).provider_ref
    secret = api.infra.settings.payment_webhook_secret
    await api.call("POST", "/webhooks/payments", expect=200,
                   **api.signed(secret, {"type": "charge.chargeback", "provider_ref": provider_ref}))
    assert (await api.ok("GET", "/accounts/me", brand))["status"] == "frozen"
    assert (await api.ok("GET", "/earnings", influencer))["paid"] == kobo(80_00)

    # With nothing outstanding the account can be deleted: 30-day grace, cancellable, then purged.
    scheduled = await api.ok("POST", "/accounts/me/deletion", influencer)
    assert scheduled["status"] == "pending_deletion"
    await api.ok("DELETE", "/accounts/me/deletion", influencer)
    await api.ok("POST", "/accounts/me/deletion", influencer)
    api.clock.advance(days=29)
    assert await api.run("deletion.purge_due") == 0
    api.clock.advance(days=2)
    assert await api.run("deletion.purge_due") == 1
    await api.call("GET", "/accounts/me", influencer, expect=401)
    await api.call("GET", "/profiles/tola", brand, expect=403)  # brand is frozen
    # Financial records are kept.
    assert (await api.ok("GET", f"/admin/accounts/{influencer.id}", root))["contracts"] == 1


async def test_reports_support_and_dashboard(api):
    root, brand, influencer = await hire_me_setup(api)
    troll = await api.signup("troll")
    post = await api.ok("POST", "/posts", influencer, json={"type": "text", "text": "My honest review"})

    # Any account can report; repeated false reports lower the reporter's weight and earn a warning.
    for n in range(3):
        target = await api.ok("POST", "/posts", influencer, json={"type": "text", "text": f"Post {n}"})
        report = await api.ok("POST", "/reports", troll, json={"target_type": "post", "target_id": target["id"], "reason": "scam"})
        await api.decide(root, "reports", report["id"], decision="reject", payload={"false_report": True})
    detail = await api.ok("GET", f"/admin/accounts/{troll.id}", root)
    assert detail["false_reports"] == 3
    assert any(n["title"] == "Warning: false reports" for n in await api.ok("GET", "/notifications", troll))
    # An upheld report removes the content.
    upheld = await api.ok("POST", "/reports", brand, json={"target_type": "post", "target_id": post["id"], "reason": "harassment"})
    await api.decide(root, "reports", upheld["id"])
    await api.call("GET", f"/posts/{post['id']}", troll, expect=404)

    # Support: a tracked ticket with a number, email confirmation and a 4-hour target for payment issues.
    ticket = await api.ok("POST", "/support/tickets", influencer, json={
        "category": "payments", "subject": "Payout missing", "body": "I have not been paid.",
        "linked_object": {"type": "contract", "id": "abc"}})
    assert ticket["number"].startswith("WM-") and ticket["number"] in api.infra.providers.email.last_for(influencer.email)["subject"]
    replied = await api.ok("POST", f"/admin/support/tickets/{ticket['id']}/replies", root, json={"body": "Looking into it"})
    assert replied["status"] == "waiting_on_me"
    assert (await api.ok("POST", f"/support/tickets/{ticket['id']}/replies", influencer, json={"body": "Thanks"}))["status"] == "open"

    # Dashboard: live values beside targets, previous period, CSV export, and revenue gated separately.
    contract = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))
    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/accept", influencer)
    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/deliver", influencer, json={"delivery": "done"})
    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/approve", brand)
    api.clock.advance(hours=1)
    dashboard = await api.ok("GET", "/admin/dashboard", root)
    metrics = dashboard["metrics"]
    assert metrics["marketplace"]["completed_contracts"] == 1 and metrics["revenue"]["gross_escrow_value_usd"] == 100.0
    assert metrics["revenue"]["fees_by_type"] == {"hire_me": kobo(20_00)}
    assert metrics["trust_and_safety"]["reports_by_reason"] == {"scam": 3, "harassment": 1}
    assert metrics["verification"]["company"]["passed"] == 1 and "previous_period" in dashboard
    assert {"metric": "marketplace.completed_contracts", "value": 1, "target": 500} in dashboard["targets"]
    csv = (await api.call("GET", "/admin/dashboard/export.csv", root, expect=200)).text
    assert csv.splitlines()[0] == "metric,value,previous_period,target" and "marketplace.completed_contracts,1" in csv

    mod = await invite_admin(api, root, "mod")
    await api.call("GET", "/admin/dashboard", mod, expect=403)
    await api.ok("PUT", f"/admin/admins/{mod.id}/permissions", root, json={"permission": "view_dashboard", "reason": "Ops"})
    assert "revenue" not in (await api.ok("GET", "/admin/dashboard", mod))["metrics"]
    counts = await api.ok("GET", "/admin/accounts/count", mod)
    assert counts["total_people"] == 3 and counts["companies"] == 1


async def test_subscriber_badge_needs_both_a_subscription_and_a_verified_id(api):
    """SB-02: a paid badge that always means a real, checked person."""
    user = await api.signup("bolu", name="Bolu Ade")
    viewer = await api.signup("seyi")
    await api.ok("POST", "/subscriptions", user, json={"period": "monthly", "payment_method": PAY})
    me = await api.ok("GET", "/accounts/me", user)
    assert me["subscribed"] is True and me["subscriber_badge"] is False  # paying alone is not enough

    await api.verify_id(user, "Bolu Ade")
    assert (await api.ok("GET", "/accounts/me", user))["subscriber_badge"] is True
    profile = await api.ok("GET", "/profiles/bolu", viewer)
    assert "subscriber" in profile["badges"] and "id_verified" in profile["badges"]

    # Cancelling keeps the badge until the paid month ends; then it goes with the subscription.
    await api.ok("DELETE", "/subscriptions", user)
    assert "subscriber" in (await api.ok("GET", "/profiles/bolu", viewer))["badges"]
    api.clock.advance(days=31)
    await api.run("subscriptions.process_renewals")
    assert "subscriber" not in (await api.ok("GET", "/profiles/bolu", viewer))["badges"]
    assert (await api.ok("GET", "/accounts/me", user))["subscriber_badge"] is False


async def test_monthly_ai_video_plan_counts_each_started_two_minutes(api):
    user = await api.influencer("maker", "Maker Person")
    plan = await api.ok("GET", "/ai-videos/plan", user)
    assert not plan["active"] and plan["terms"] == {"price_cents": 30_00, "videos": 15, "minutes_per_video": 2, "quality": "standard"}
    charges = api.infra.providers.payments.main.charges
    before = set(charges)
    plan = await api.ok("POST", "/ai-videos/plan", user, json={"payment_method": PAY})
    assert plan["active"] and plan["videos_left"] == 15
    assert [charges[r][0] for r in set(charges) - before] == [kobo(30_00)]

    # Up to 2 minutes is one video; a 5-minute video uses 3 (2 + 2 + 1). Nothing is charged.
    quote = await api.ok("GET", "/ai-videos/quote?seconds=300&tier=standard", user)
    assert quote["plan_videos_applied"] == 3 and quote["price_cents"] == 0
    five = await api.ok("POST", "/ai-videos", user, json={"script": "A five minute story", "seconds": 300})
    assert five["charged_cents"] == 0 and five["plan_videos_used"] == 3
    two = await api.ok("POST", "/ai-videos", user, json={"script": "Quick tip", "seconds": 120})
    assert two["plan_videos_used"] == 1
    assert (await api.ok("GET", "/ai-videos/plan", user))["videos_left"] == 11

    # Premium isn't on the plan: always per minute.
    assert (await api.ok("GET", "/ai-videos/quote?seconds=60&tier=premium", user))["price_cents"] == 25_00
    # A video that fails gives its plan videos back.
    failed = await api.ok("POST", "/ai-videos", user, json={"script": "[fail] broken", "seconds": 200})
    assert failed["status"] == "failed" and (await api.ok("GET", "/ai-videos/plan", user))["videos_left"] == 11

    # When the plan can't cover a video, it's priced per minute as usual.
    for _ in range(2):
        await api.ok("POST", "/ai-videos", user, json={"script": "Long story", "seconds": 600})  # 5 videos each
    assert (await api.ok("GET", "/ai-videos/plan", user))["videos_left"] == 1
    over = await api.ok("GET", "/ai-videos/quote?seconds=180&tier=standard", user)
    assert over["plan_videos_applied"] == 0 and over["price_cents"] == 9_00

    # The month renews with 15 again (unused ones don't carry over); cancelling stops it at the month's end.
    api.clock.advance(days=30, minutes=1)
    await api.run("ai_video_plans.renew_due")
    assert (await api.ok("GET", "/ai-videos/plan", user))["videos_left"] == 15
    cancelled = await api.ok("DELETE", "/ai-videos/plan", user)
    assert cancelled["active"] and cancelled["cancel_at_period_end"]
    api.clock.advance(days=30, minutes=1)
    await api.run("ai_video_plans.renew_due")
    assert not (await api.ok("GET", "/ai-videos/plan", user))["active"]
