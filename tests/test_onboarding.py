"""Epic 1: signup, age gate, guardians, verification, bank accounts, and general rate limiting."""
from tests.conftest import ADULT_DOB, PASSWORD, build_api


async def test_signup_otp_rules(api):
    email = "ada@example.com"
    first = await api.ok("POST", "/auth/signup/request-otp", json={"email": email})
    assert first["expires_in_seconds"] == 600 and first["resend_available_after_seconds"] == 60

    # 5 wrong codes lock the email for 15 minutes.
    for attempt in range(4):
        r = await api.call("POST", "/auth/signup/verify-otp", json={"email": email, "code": "000000"}, expect=422)
        assert r.json()["attempts_left"] == 4 - attempt
    await api.call("POST", "/auth/signup/verify-otp", json={"email": email, "code": "000000"}, expect=423)
    await api.call("POST", "/auth/signup/request-otp", json={"email": email}, expect=423)
    api.clock.advance(minutes=16)

    # The first send plus 3 resends are allowed in an hour; the next is blocked.
    for _ in range(3):
        await api.call("POST", "/auth/signup/request-otp", json={"email": email}, expect=200)
    blocked = await api.call("POST", "/auth/signup/request-otp", json={"email": email}, expect=429)
    assert "Retry-After" in blocked.headers

    # A code is valid for 10 minutes.
    code = api.email_code(email)
    api.clock.advance(minutes=11)
    await api.call("POST", "/auth/signup/verify-otp", json={"email": email, "code": code}, expect=410)


async def test_registered_email_is_told_to_log_in(api):
    await api.signup("ada")
    r = await api.call("POST", "/auth/signup/request-otp", json={"email": "ada@example.com"}, expect=409)
    assert "Log in instead" in r.json()["error"] and "password_reset_url" in r.json()


async def test_age_gate_refuses_under_13(api):
    email = "kid@example.com"
    await api.ok("POST", "/auth/signup/request-otp", json={"email": email})
    token = (await api.ok("POST", "/auth/signup/verify-otp", json={"email": email, "code": api.email_code(email)}))["signup_token"]
    body = {"signup_token": token, "account_type": "user", "password": PASSWORD, "handle": "kid", "name": "Kid",
            "date_of_birth": "2016-01-01"}
    await api.call("POST", "/auth/signup/complete", json=body, expect=403)
    # Resubmitting with an adult date of birth is refused too.
    await api.call("POST", "/auth/signup/complete", json=body | {"date_of_birth": ADULT_DOB}, expect=403)


async def test_minor_needs_a_verified_guardian(api):
    minor = await api.signup("teen", dob="2011-06-01")
    me = await api.ok("GET", "/accounts/me", minor)
    assert me["status"] == "pending_guardian" and me["profile"]["is_minor"]
    # Signup is not finished: the app is closed until a guardian is linked.
    r = await api.call("GET", "/feed", minor, expect=403)
    assert r.json()["code"] == "guardian_required"

    await api.ok("POST", "/guardians/invite", minor, json={"contact": "mum@example.com"})
    token = api.infra.providers.email.last_for("mum@example.com")["body"].split("token=")[1]
    guardian = await api.signup("mum", name="Funmilayo Ogunleye")
    accepted = await api.ok("POST", "/guardians/accept", guardian,
                            json={"token": token, "relationship": "mother", "confirm_relationship": True})
    assert accepted["status"] == "pending_verification"
    assert (await api.ok("GET", "/accounts/me", minor))["status"] == "pending_guardian"

    await api.verify_id(guardian, "Funmilayo Ogunleye")
    assert (await api.ok("GET", "/accounts/me", minor))["status"] == "active"
    minors = await api.ok("GET", "/guardians/minors", guardian)
    assert minors[0]["minor_id"] == minor.id

    # The guardian can freeze the account.
    await api.ok("PUT", f"/guardians/minors/{minor.id}/freeze", guardian)
    await api.call("GET", "/feed", minor, expect=403)
    await api.ok("DELETE", f"/guardians/minors/{minor.id}/freeze", guardian)
    await api.call("GET", "/feed", minor, expect=200)

    # A minor's bank account must be the guardian's.
    r = await api.call("PUT", "/verification/bank-account", minor,
                       json={"bank_code": "058", "account_number": "0123456789", "account_name": "Teen"}, expect=403)
    assert r.json()["code"] == "guardian_account_required"


async def test_minor_turning_18_converts(api):
    minor = await api.signup("almost", dob="2008-10-20")
    assert (await api.ok("GET", "/accounts/me", minor))["profile"]["is_minor"]
    api.clock.advance(days=9)
    assert await api.run("accounts.convert_minors_turning_18") == 1
    me = await api.ok("GET", "/accounts/me", minor)
    assert me["status"] == "active" and not me["profile"]["is_minor"]


async def test_identity_retries_then_manual_review(api):
    admin = await api.admin()
    user = await api.signup("bola", name="Bola Ade")
    for retries_left in (2, 1):
        result = await api.ok("POST", "/verification/identity", user, json={
            "document_type": "national_id", "document_ref": "Bola Ade|1990-05-01|blurry", "selfie_ref": "s"})
        assert result["status"] == "failed" and result["retries_left"] == retries_left and "blurry" in result["reason"]
    result = await api.ok("POST", "/verification/identity", user, json={
        "document_type": "national_id", "document_ref": "Bola Ade|1990-05-01|blurry", "selfie_ref": "s"})
    assert result["status"] == "manual_review"
    await api.decide(admin, "identity", result["verification_id"], payload={"verified_name": "Bola Ade"})
    assert (await api.ok("GET", "/verification/identity", user))["id_verified"]
    # A verified name only changes through a new ID check.
    await api.call("PATCH", "/accounts/me/profile", user, json={"name": "Someone Else"}, expect=403)


async def test_id_showing_a_minor_suspends_pending_review(api):
    user = await api.signup("fibber", name="Fib Ber")
    await api.verify_id(user, "Fib Ber", dob="2012-01-01")
    assert (await api.ok("GET", "/accounts/me", user))["status"] == "suspended"


async def test_bank_account_rules(api):
    user = await api.signup("chidi", name="Chidi Okeke")
    await api.call("PUT", "/verification/bank-account", user,
                   json={"bank_code": "058", "account_number": "0123456789", "account_name": "Chidi Okeke"}, expect=403)
    await api.verify_id(user, "Chidi Okeke")
    # Name mismatch: saved, but payouts are blocked until fixed.
    mismatch = await api.bank(user, "Another Person")
    assert not mismatch["name_matches_id"] and mismatch["payouts_blocked"]
    # Changing details needs two-factor login.
    r = await api.call("PUT", "/verification/bank-account", user,
                       json={"bank_code": "058", "account_number": "0123456789", "account_name": "OKEKE CHIDI"}, expect=403)
    assert r.json()["code"] == "2fa_setup_required"
    secret = (await api.ok("POST", "/auth/2fa/setup", user))["secret"]
    await api.ok("POST", "/auth/2fa/enable", user, json={"code": api.infra.totp.now(secret)})
    fixed = await api.ok("PUT", "/verification/bank-account", user, json={
        "bank_code": "058", "account_number": "0123456789", "account_name": "OKEKE CHIDI",
        "totp_code": api.infra.totp.now(secret)})
    # The name now matches, but a change holds payouts for 48 hours and alerts the user.
    assert fixed["name_matches_id"] and fixed["payouts_blocked"] and fixed["hold_until"]
    assert any("Bank details changed" in m["subject"] for m in api.infra.providers.email.outbox)
    api.clock.advance(hours=49)
    assert not (await api.ok("GET", "/verification/bank-account", user))["payouts_blocked"]


async def test_verified_creator_and_popularity_card(api):
    admin, second_admin = await api.admin(), await api.admin("root2")
    user = await api.influencer("star", "Star Person")
    small = await api.ok("POST", "/verification/social", user, json={"platform": "instagram", "auth_code": "star:3000"})
    assert not small["verified_creator"]
    both = await api.ok("POST", "/verification/social", user, json={"platform": "tiktok", "auth_code": "star:2500"})
    assert both["verified_creator"] and both["combined_followers"] == 5500

    # A flagged account goes to admin review and does not count.
    flagged = await api.ok("POST", "/verification/social", user, json={"platform": "x", "auth_code": "star:900000:fake"})
    assert flagged["combined_followers"] == 5500 and flagged["accounts"][-1]["under_review"]

    # Popularity card: Notable = 1 signal, Prominent = 2, Major = 3+ from at least two categories.
    levels = []
    for category, title in (("speaking", "TEDx Lagos"), ("speaking", "Keynote, Web Summit"), ("position", "CEO, Acme")):
        signal = await api.ok("POST", "/verification/popularity/signals", user,
                              json={"category": category, "title": title, "evidence_ref": "https://example.com/proof"})
        await api.decide(admin, "public_figure", signal["signal_id"])
        levels.append((await api.ok("GET", "/profiles/star/popularity-card", user))["level"])
    assert levels == ["notable", "prominent", "major"]
    card = await api.ok("GET", "/profiles/star/popularity-card", user)
    assert card["verified_major_person"] and all("evidence" not in str(s) for s in card["signals"])

    # Appeal: a second review by a different admin, once per 30 days.
    rejected = await api.ok("POST", "/verification/popularity/signals", user,
                            json={"category": "recognition", "title": "Award", "evidence_ref": "https://x.test/a"})
    await api.decide(admin, "public_figure", rejected["signal_id"], decision="reject")
    await api.ok("POST", f"/verification/popularity/signals/{rejected['signal_id']}/appeal", user)
    items = await api.ok("GET", "/admin/queues/public_figure", admin)
    item = next(i for i in items if i["target_id"] == rejected["signal_id"])
    await api.ok("POST", f"/admin/queue-items/{item['id']}/claim", admin)
    r = await api.call("POST", f"/admin/queue-items/{item['id']}/decide", admin,
                       json={"decision": "approve", "reason": "Looks right", "payload": {}}, expect=403)
    assert r.json()["code"] == "same_reviewer"
    await api.ok("POST", f"/admin/queue-items/{item['id']}/release", admin)
    await api.decide(second_admin, "public_figure", rejected["signal_id"])
    await api.call("POST", f"/verification/popularity/signals/{rejected['signal_id']}/appeal", user, expect=422)


async def test_contacts_are_matched_by_hash_and_invited_once(api):
    ada, bola = await api.signup("ada"), await api.signup("bola")
    await api.verify_phone(bola, "+2348012345678")
    matched = await api.ok("POST", "/friends/contacts/match", ada, json={"phones": ["+234 801 234 5678", "+2348099999999"]})
    assert [u["handle"] for u in matched["on_wamiwa"]] == ["bola"] and matched["not_on_wamiwa"] == 1
    await api.ok("POST", "/friends/contacts/invite", ada, json={"phone": "+2348099999999"})
    await api.call("POST", "/friends/contacts/invite", ada, json={"phone": "+2348099999999"}, expect=409)


async def test_general_rate_limit_headers_and_429():
    api = await build_api(human_reads_per_minute=3)
    try:
        user = await api.signup("ada")
        first = await api.call("GET", "/accounts/me", user, expect=200)
        assert first.headers["X-RateLimit-Limit"] == "3" and first.headers["X-RateLimit-Remaining"] == "2"
        await api.call("GET", "/accounts/me", user, expect=200)
        await api.call("GET", "/accounts/me", user, expect=200)
        blocked = await api.call("GET", "/accounts/me", user, expect=429)
        assert blocked.json()["retry_after_seconds"] > 0 and blocked.headers["Retry-After"]
        api.clock.advance(minutes=2)
        await api.call("GET", "/accounts/me", user, expect=200)
    finally:
        await api.client.aclose()
        await api.infra.db.dispose()


async def test_welcome_email_from_the_founder(api):
    outbox = api.infra.providers.email.outbox

    def welcome_for(address):
        return [m for m in outbox if m["to"] == address and m["subject"] == "Welcome to Wamiwa"]

    await api.signup("ada", name="Bisi Adewale")
    [mail] = welcome_for("ada@example.com")
    assert mail["body"].startswith("Hi Bisi,") and "Damilola Oladimeji\nFounder & CEO, Wamiwa" in mail["body"]
    assert "just reply" in mail["body"]

    admin = await api.admin()
    await api.company("acme", admin)
    [company_mail] = welcome_for("acme@example.com")
    assert company_mail["body"].startswith("Hi Acme Ltd team,") and "verify your business" in company_mail["body"]

    # A 13-17 account is only welcomed once a guardian has linked and been verified.
    minor = await api.signup("teen", dob="2011-06-01", name="Teen Ogunleye")
    assert welcome_for("teen@example.com") == []
    await api.ok("POST", "/guardians/invite", minor, json={"contact": "mum@example.com"})
    token = api.infra.providers.email.last_for("mum@example.com")["body"].split("token=")[1]
    guardian = await api.signup("mum", name="Funmilayo Ogunleye")
    await api.ok("POST", "/guardians/accept", guardian, json={"token": token, "relationship": "mother", "confirm_relationship": True})
    assert welcome_for("teen@example.com") == []
    await api.verify_id(guardian, "Funmilayo Ogunleye")
    [teen_mail] = welcome_for("teen@example.com")
    assert teen_mail["body"].startswith("Hi Teen,") and "guardian has linked" in teen_mail["body"]
    assert len(welcome_for("mum@example.com")) == 1  # the guardian got their own, once
