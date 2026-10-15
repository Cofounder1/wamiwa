"""Every role hears about what concerns them by email, with a link to the right screen."""
from datetime import timedelta

from tests.test_hiring import PAY, hire_me_setup, offer
from tests.test_platform import invite_admin


def inbox(api, address):
    return [m for m in api.infra.providers.email.outbox if m["to"] == address]


def subjects(api, address):
    return [m["subject"] for m in inbox(api, address)]


async def test_agent_orders_email_the_owner_and_the_hirer(api):
    owner = await api.influencer("owner", "Owner Person")
    hirer = await api.signup("hirer", name="Tunde Bakare")
    agent = await api.live_agent(owner, rates={"research": 50_00})
    order = await api.ok("POST", f"/marketplace/agents/{agent['id']}/orders", hirer,
                         json={"task": "research", "brief": "Market scan", "payment_method": PAY})
    new_order = next(m for m in inbox(api, owner.email) if m["subject"].startswith("New order for"))
    assert f"/app/orders/{order['id']}" in new_order["body"] and new_order["body"].startswith("Hi Owner,")
    assert "concern your money, contracts or account security" in new_order["body"]
    hirer_mail = subjects(api, hirer.email)
    assert any(s.startswith("Payment received:") for s in hirer_mail) and "Order received: research" in hirer_mail

    await api.ok("POST", f"/api/agent/v1/orders/{order['id']}/deliver", headers=agent["headers"], json={"delivery": "Done"})
    assert "Work delivered" in subjects(api, hirer.email)
    await api.ok("POST", f"/orders/{order['id']}/approve", hirer)
    assert "Order completed" in subjects(api, owner.email) and "Funds cleared" in subjects(api, owner.email)


async def test_influencers_brands_and_guardians_hear_about_contracts(api):
    _, brand, influencer = await hire_me_setup(api)
    contract = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))
    assert any(s.startswith("New offer from") for s in subjects(api, influencer.email))
    receipt = next(m for m in inbox(api, brand.email) if m["subject"].startswith("Payment received:"))
    assert "held in escrow" not in receipt["subject"] and "escrow" in receipt["body"]
    assert receipt["body"].startswith("Hi Acme Ltd team,")

    # A reminder a day before the offer expires.
    api.clock.advance(days=6, hours=1)
    await api.run("reminders.send_due")
    assert "An offer expires in less than a day" in subjects(api, influencer.email)
    await api.run("reminders.send_due")
    assert subjects(api, influencer.email).count("An offer expires in less than a day") == 1  # only once

    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/accept", influencer)
    assert subjects(api, influencer.email).count("Your contract is active") == 1
    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/deliver", influencer, json={"delivery": "links"})
    api.clock.advance(days=6, hours=1)
    await api.run("reminders.send_due")
    assert "One day left to review delivered work" in subjects(api, brand.email)


async def test_messages_and_followers_with_preferences(api):
    ada, bola = await api.signup("ada", name="Ada Bello"), await api.signup("bola", name="Bola Ade")
    for text in ("Hi", "Are you there?", "Hello?"):
        await api.ok("POST", "/messages", ada, json={"recipient_id": bola.id, "body": text})
    notice = [m for m in inbox(api, bola.email) if m["subject"] == "New message from @ada"]
    assert len(notice) == 1 and "/app/messages/" in notice[0]["body"]  # a busy chat is one email an hour
    assert "Settings > Notifications" in notice[0]["body"]  # optional emails say how to turn them off

    await api.ok("PUT", f"/follows/{bola.id}", ada)
    assert "@ada started following you" in subjects(api, bola.email)
    # Turned off: no more follower emails.
    await api.ok("PUT", "/notifications/preferences", bola, json={"preferences": {"follow": False}})
    cara = await api.signup("cara")
    await api.ok("PUT", f"/follows/{bola.id}", cara)
    assert "@cara started following you" not in subjects(api, bola.email)


async def test_staff_hear_about_urgent_work_and_get_a_daily_summary(api):
    root, brand, influencer = await hire_me_setup(api)
    mod = await invite_admin(api, root, "mod")
    contract = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))
    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/accept", influencer)
    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/deliver", influencer, json={"delivery": "x"})
    await api.ok("POST", f"/contracts/{contract['id']}/dispute", brand, json={"reason": "Nothing like the brief"})
    for admin in (root, mod):
        mail = next(m for m in inbox(api, admin.email) if m["subject"] == "New disputes item needs a decision")
        assert "/admin/" in mail["body"]
    await api.ok("POST", "/support/tickets", influencer, json={
        "category": "payments", "subject": "Payout missing", "body": "Not paid"})
    assert any(s.startswith("Urgent support ticket WM-") for s in subjects(api, mod.email))

    company = await api.signup("newco", kind="company", name="NewCo")
    await api.ok("POST", "/companies/verification", company, json={"registration_no": "RC9", "document_ref": "d.pdf"})
    await api.run("queues.send_daily_summary")
    summary = next(m for m in inbox(api, mod.email) if m["subject"] == "Today's review queues")
    assert "Company verification: 1 waiting" in summary["body"] and "Disputes: 1 waiting" in summary["body"]
