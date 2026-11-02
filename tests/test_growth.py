"""SB-03 Pro plan (partners and region ideas) and FD-09 "For you", matched to what people post about."""
from tests.test_hiring import PAY, offer


async def _minor(api, handle="teen"):
    minor = await api.signup(handle, dob="2010-03-01", name="Teen Ogunleye")
    guardian = await api.signup(f"{handle}mum", name="Funmilayo Ogunleye")
    await api.ok("POST", "/guardians/invite", minor, json={"contact": f"{handle}mum@example.com"})
    token = api.infra.providers.email.last_for(f"{handle}mum@example.com")["body"].split("token=")[1]
    await api.ok("POST", "/guardians/accept", guardian, json={"token": token, "relationship": "mother",
                                                              "confirm_relationship": True})
    await api.verify_id(guardian, "Funmilayo Ogunleye")  # the minor's account opens once the guardian is verified
    return minor


async def _businesses(api, admin):
    tech = await api.company("neuralworks", admin, category="tech")
    fashion = await api.company("zurihouse", admin, category="fashion")
    betting = await api.company("betnow", admin, category="betting")
    food = await api.company("jollofhub", admin, category="food & drinks")
    await api.ok("POST", "/posts", tech, json={"type": "text", "text": "We are hiring creators who explain #ml and machine learning"})
    await api.ok("POST", "/posts", fashion, json={"type": "text", "text": "New #ankara drop this Friday"})
    await api.ok("POST", "/posts", betting, json={"type": "text", "text": "Our #ml model sets the odds"})
    await api.ok("POST", "/posts", food, json={"type": "text", "text": "Jollof Friday is here"})
    return tech, fashion, betting, food


async def test_pro_tier_upgrade_mid_period_and_downgrade_at_renewal(api):
    user = await api.signup("bolu", name="Bolu Ade")
    plans = await api.ok("GET", "/subscriptions/plans", user)
    assert plans["tiers"]["standard"]["monthly"]["usd_cents"] == 10_00
    assert plans["tiers"]["pro"]["monthly"]["usd_cents"] == 16_00 and plans["tiers"]["pro"]["yearly"]["usd_cents"] == 160_00

    await api.ok("POST", "/subscriptions", user, json={"period": "monthly", "payment_method": PAY})
    r = await api.call("GET", "/growth/partners", user, expect=403)
    assert r.json()["code"] == "pro_required"

    # Halfway through the month, upgrading costs only the difference for the days left ($6 x 1/2).
    api.clock.advance(days=15)
    charges = api.infra.providers.payments.main.charges
    before = set(charges)
    assert (await api.ok("GET", "/subscriptions/upgrade-quote", user))["amount_cents"] == 3_00  # what checkout opens for
    upgraded = await api.ok("PATCH", "/subscriptions", user, json={"tier": "pro", "payment_method": PAY})
    assert upgraded["tier"] == "pro"
    [new_charge] = [charges[ref] for ref in set(charges) - before]
    assert new_charge[0] == 3_00 * 1500  # in kobo at the sandbox rate

    # Moving back to Creator waits for the renewal; Pro stays until then.
    downgraded = await api.ok("PATCH", "/subscriptions", user, json={"tier": "standard", "payment_method": PAY})
    assert downgraded["tier"] == "pro" and downgraded["next_tier"] == "standard"
    api.clock.advance(days=16)
    await api.run("subscriptions.process_renewals")
    renewed = await api.ok("GET", "/subscriptions", user)
    assert renewed["subscribed"] and renewed["tier"] == "standard" and renewed["next_tier"] is None


async def test_partners_follow_what_you_actually_post_about(api):
    admin = await api.admin()
    tech, fashion, betting, food = await _businesses(api, admin)
    creator = await api.influencer("ada", "Ada Obi", niche="fashion")
    await api.ok("PUT", "/hire-me/page", creator, json={"starting_price_cents": 50_00, "photos": ["p/1.jpg"]})
    await api.ok("POST", "/hire-me/offers", fashion, json=offer(api, creator))  # Zuri is hiring creators
    user = await api.signup("tobi", name="Tobi Ade")
    await api.ok("PATCH", "/accounts/me/profile", user, json={"primary_niche": "fashion"})
    for text in ("Training my first #ml model tonight", "Notes on machine learning for beginners", "More #ml tips"):
        await api.ok("POST", "/posts", user, json={"type": "text", "text": text})
    await api.ok("POST", "/subscriptions", user, json={"period": "monthly", "tier": "pro", "payment_method": PAY})

    result = await api.ok("GET", "/growth/partners", user)
    names = [p["handle"] for p in result["partners"]]
    # Posting about ML puts the ML business first; the chosen niche still brings in fashion businesses.
    assert names[0] == "neuralworks" and "zurihouse" in names and "jollofhub" not in names
    top = result["partners"][0]
    assert any("#ml" in why for why in top["why"]) and "tech" in result["based_on"]["categories"]
    zuri = next(p for p in result["partners"] if p["handle"] == "zurihouse")
    assert zuri["hiring"] and "Hiring creators now" in zuri["why"]
    assert "betnow" in names  # adults may see any category

    # Minors get the same tool, without categories restricted for under-18s.
    minor = await _minor(api)
    await api.ok("POST", "/posts", minor, json={"type": "text", "text": "Learning #ml at school"})
    await api.ok("POST", "/subscriptions", minor, json={"period": "monthly", "tier": "pro", "payment_method": PAY})
    minor_names = [p["handle"] for p in (await api.ok("GET", "/growth/partners", minor))["partners"]]
    assert "neuralworks" in minor_names and "betnow" not in minor_names


async def test_for_you_shows_posts_in_your_topics_to_everyone(api):
    admin = await api.admin()
    await _businesses(api, admin)
    person = await api.signup("kunle", name="Kunle Ade")
    other = await api.signup("ife", name="Ife Ade")
    await api.ok("POST", "/posts", other, json={"type": "text", "text": "Fine-tuning an #llm this weekend"})
    empty = await api.ok("GET", "/feed/for-you", person)
    assert empty["items"] == [] and "hint" in empty

    await api.ok("POST", "/posts", person, json={"type": "text", "text": "Why #ml engineers should learn statistics"})
    await api.ok("POST", "/posts", person, json={"type": "text", "text": "My #llm reading list"})
    feed = await api.ok("GET", "/feed/for-you", person)
    handles = [i["author"]["handle"] for i in feed["items"]]
    assert handles[0] == "neuralworks" and feed["items"][0]["from_business"]  # businesses in your topics first
    assert "ife" in handles and "jollofhub" not in handles and "zurihouse" not in handles
    assert feed["items"][0]["why"].startswith("Because you post about #")
    assert all(i["author"]["handle"] != "kunle" for i in feed["items"])  # never your own posts


async def test_pro_content_ideas_for_any_countries(api):
    user = await api.signup("dami", name="Dami Ade")
    await api.ok("PATCH", "/accounts/me/profile", user, json={"primary_niche": "tech"})
    await api.call("GET", "/growth/ideas", user, expect=403)
    await api.ok("POST", "/subscriptions", user, json={"period": "monthly", "tier": "pro", "payment_method": PAY})
    saved = await api.ok("PUT", "/growth/regions", user, json={"regions": ["Brazil", "UK"]})
    assert saved["target_regions"] == ["brazil", "united_kingdom"]
    ideas = await api.ok("GET", "/growth/ideas", user)
    assert [r["region"] for r in ideas["regions"]] == ["brazil", "united_kingdom"] and ideas["topic_area"] == "tech"
    r = await api.call("PUT", "/growth/regions", user, json={"regions": ["Atlantis"]}, expect=422)
    assert r.json()["code"] == "unknown_region"


async def test_admin_mrr_counts_each_tier_at_its_own_price(api):
    root = await api.admin()
    creator, pro = await api.signup("sade"), await api.signup("femi")
    await api.ok("POST", "/subscriptions", creator, json={"period": "monthly", "payment_method": PAY})
    await api.ok("POST", "/subscriptions", pro, json={"period": "monthly", "tier": "pro", "payment_method": PAY})
    revenue = (await api.ok("GET", "/admin/dashboard", root))["metrics"]["revenue"]
    assert revenue["mrr_usd"] == 26.0 and revenue["subscribers_by_tier"] == {"creator": 1, "pro": 1}
