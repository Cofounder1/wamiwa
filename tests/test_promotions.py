"""Epic 15: the Wamiwa Agent - smart mention cards, shared links, suggested sellers, Plus ideas, analytics."""
from app.infra.providers.base import TrendSignal
from app.models import UserProfile
from tests.test_hiring import PAY, kobo

SHOP = {"url": "https://kofishirts.example.com", "label": "Shop"}


async def comments_of(api, viewer, post_id):
    return await api.ok("GET", f"/posts/{post_id}/comments?sort=old", viewer)


async def make_minor(api, actor):
    async with api.services() as svc:
        profile = await svc.access.repo(UserProfile).first(UserProfile.account_id == actor.id)
        profile.is_minor = True


async def test_business_cards_links_rules_and_analytics(api):
    admin = await api.admin()
    brand = await api.company("kofishirts", admin, category="fashion")
    rival = await api.company("rivalwear", admin, category="fashion")
    creator, writer, reader = await api.signup("creator"), await api.signup("writer"), await api.signup("reader")
    teen = await api.signup("teen")
    await make_minor(api, teen)

    # Only verified companies and ID-verified adults can buy; the agent is in the marketplace as a plan.
    newco = await api.signup("newco", kind="company", name="NewCo")
    body = {"kind": "business", "links": [SHOP], "payment_method": PAY}
    await api.call("POST", "/promotions", newco, json=body, expect=403)
    await api.call("POST", "/promotions", writer, json=body | {"kind": "seller", "brand_name": "W", "category": "fashion"}, expect=403)
    plans = await api.ok("GET", "/promotions/plans", brand)
    assert plans["prices"]["business"]["base"] == {"usd_cents": 20_00, "local": kobo(20_00), "links": 5}
    assert plans["prices"]["business"]["plus"] == {"usd_cents": 30_00, "local": kobo(30_00), "links": 10}
    assert plans["prices"]["label"]["base"]["usd_cents"] == 80_00 and plans["prices"]["artist"]["plus"]["links"] == 6
    r = await api.call("POST", f"/marketplace/agents/{plans['agent']['id']}/orders", brand,
                       json={"task": "x", "brief": "hire it", "payment_method": PAY}, expect=409)
    assert r.json()["code"] == "use_promotions"
    await api.call("POST", "/promotions", brand, json=body | {"links": [SHOP | {"url": "http://insecure.example.com"}]}, expect=422)
    plan = await api.ok("POST", "/promotions", brand, json=body)
    assert plan["status"] == "active" and plan["names"][0]["status"] == "approved" and plan["link_limit"] == 5
    shop_id = plan["links"][0]["id"]
    assert plan["links"][0]["shared_by_agent"]

    post = await api.ok("POST", "/posts", creator, json={"type": "text", "text": "What should I wear on Friday?"})
    pid = post["id"]
    await api.ok("POST", f"/posts/{pid}/comments", writer, json={"text": "I got mine from Kofishirts Ltd last week"})

    # The name is tappable for adults, and opens a sponsored card with the links the brand chose.
    mention = (await comments_of(api, reader, pid))[0]["promotions"][0]
    assert mention["sponsored"] and mention["end"] - mention["start"] == len("Kofishirts Ltd")
    card = await api.ok("GET", mention["card_url"], reader)
    assert card["name"] == "Kofishirts Ltd" and card["sponsored"] and [l["label"] for l in card["links"]] == ["Shop"]
    assert (await api.ok("POST", card["links"][0]["click_url"], reader))["url"] == SHOP["url"]
    await api.ok("GET", mention["card_url"], reader)  # opening twice counts once
    # Nothing sponsored is shown to a 13-17 reader.
    assert "promotions" not in (await comments_of(api, teen, pid))[0]
    await api.call("GET", mention["card_url"], teen, expect=404)

    # A complaint never gets a card.
    await api.ok("POST", f"/posts/{pid}/comments", writer, json={"text": "Kofishirts Ltd scammed me, avoid them"})
    assert "promotions" not in (await comments_of(api, reader, pid))[1]

    # A question gets one sponsored reply from the Wamiwa Agent per thread, carrying one of the links.
    asked = await api.ok("POST", f"/posts/{pid}/comments", reader, json={"text": "Where can I buy from @kofishirts?"})
    await api.ok("POST", f"/posts/{pid}/comments", writer, json={"text": "Yes, who is Kofishirts Ltd?", "parent_id": asked["id"]})
    thread = (await comments_of(api, reader, pid))[2]
    sponsored = [c for c in thread["replies"] if c["sponsored"]]
    assert len(sponsored) == 1 and sponsored[0]["label_display"] == "Agent — Sponsored"
    assert sponsored[0]["text"] == "Kofishirts Ltd: Shop." and sponsored[0]["author"]["handle"] == "wamiwa"
    assert sponsored[0]["link"] == f"/promotions/links/{shop_id}/click"
    assert (await api.ok("POST", sponsored[0]["link"], writer))["url"] == SHOP["url"]

    # No competitor's card under a company's own post.
    rival_post = await api.ok("POST", "/posts", rival, json={"type": "text", "text": "New arrivals"})
    await api.ok("POST", f"/posts/{rival_post['id']}/comments", writer, json={"text": "Kofishirts Ltd is cheaper"})
    assert "promotions" not in (await comments_of(api, reader, rival_post["id"]))[0]

    # The owner sees it all under Analytics.
    stats = (await api.ok("GET", "/analytics/me", brand))["wamiwa_agent"]
    assert stats["totals"]["card_opens"] == 1 and stats["totals"]["link_clicks"] == 2  # two different people
    assert stats["totals"]["agent_replies"] == 1 and stats["totals"]["card_impressions"] >= 1
    assert stats["plan"]["tier"] == "base" and (await api.ok("GET", "/analytics/me", creator))["wamiwa_agent"] is None

    # Deleting a link: the agent stops quoting it, and the old reply no longer opens it.
    await api.ok("DELETE", f"/promotions/links/{shop_id}", brand)
    await api.call("POST", sponsored[0]["link"], reader, expect=404)
    await api.ok("POST", f"/posts/{pid}/comments", reader, json={"text": "Seriously, where can I buy Kofishirts Ltd?"})
    assert (await comments_of(api, reader, pid))[3]["replies"] == []  # nothing left to share
    assert (await api.ok("GET", mention["card_url"], writer))["links"] == []

    # The base package shares 5 links; Plus costs $10 more and shares 10.
    for n in range(5):
        added = await api.ok("POST", "/promotions/links", brand, json={"url": f"https://kofi.example.com/{n}", "label": f"Page {n}"})
    await api.call("POST", "/promotions/links", brand, json={"url": "https://kofi.example.com/0", "label": "Again"}, expect=409)
    r = await api.call("POST", "/promotions/links", brand, json={"url": "https://kofi.example.com/5", "label": "Sixth"}, expect=409)
    assert r.json()["code"] == "link_limit" and r.json()["limit"] == 5
    upgraded = await api.ok("PATCH", "/promotions", brand, json={"tier": "plus"})
    assert upgraded["link_limit"] == 10 and upgraded["price_cents"] == 30_00
    assert (await api.call("GET", "/payments", brand)).json()["data"][0]["amount_cents"] == 10_00
    for n in range(5, 10):
        await api.ok("POST", "/promotions/links", brand, json={"url": f"https://kofi.example.com/{n}", "label": f"Page {n}"})
    # Going back to base keeps every link saved, but only the first 5 are shared.
    downgraded = await api.ok("PATCH", "/promotions", brand, json={"tier": "base"})
    assert [l["shared_by_agent"] for l in downgraded["links"]] == [True] * 5 + [False] * 5
    last_id, first_id = downgraded["links"][-1]["id"], downgraded["links"][0]["id"]
    await api.call("POST", f"/promotions/links/{last_id}/click", reader, expect=404)

    # A creator can switch sponsored cards off on their own posts; a reader who blocks the brand sees none.
    await api.ok("PATCH", "/accounts/me/settings", creator, json={"sponsored_cards": False})
    await api.ok("POST", f"/posts/{pid}/comments", writer, json={"text": "Kofishirts Ltd again"})
    assert "promotions" not in (await comments_of(api, reader, pid))[4]
    await api.ok("PUT", f"/blocks/{brand.id}", reader)
    assert "promotions" not in (await comments_of(api, reader, pid))[0]

    # When the plan ends the links stop working but stay saved, and work again once the agent is bought again.
    await api.ok("DELETE", "/promotions", brand)
    api.clock.advance(days=31)
    assert await api.run("promos.renew_due") == {"renewed": 0, "lapsed": 1}
    assert "promotions" not in (await comments_of(api, writer, pid))[0]
    await api.call("POST", f"/promotions/links/{first_id}/click", writer, expect=404)
    saved = await api.ok("GET", "/promotions", brand)
    assert saved["status"] == "lapsed" and len(saved["links"]) == 10 and not any(l["shared_by_agent"] for l in saved["links"])
    await api.ok("POST", "/promotions", brand, json={"kind": "business", "payment_method": PAY})
    assert (await api.ok("POST", f"/promotions/links/{first_id}/click", writer))["url"] == "https://kofi.example.com/0"
    assert "promotions" in (await comments_of(api, writer, pid))[0]


async def test_artists_and_labels_share_the_links_they_choose(api):
    admin = await api.admin()
    artist = await api.influencer("oshamo_music", "Osha Mo")
    fan, creator = await api.signup("fan"), await api.signup("creator")
    post = await api.ok("POST", "/posts", creator, json={"type": "text", "text": "Friday playlist"})
    pid = post["id"]

    links = [{"url": "https://open.spotify.com/artist/0shamo", "label": "Spotify"},
             {"url": "https://audiomack.com/oshamo", "label": "Audiomack"},
             {"url": "https://tix.example.com/oshamo-live", "label": "Tickets", "keywords": ["show", "concert"]}]
    body = {"kind": "artist", "brand_name": "Oshamo", "links": links, "payment_method": PAY}
    # Artists share 3 links on the base package.
    r = await api.call("POST", "/promotions", artist, json=body | {"links": links + [{"url": "https://x.example.com", "label": "More"}]}, expect=422)
    assert r.json()["code"] == "too_many_links"
    await api.call("POST", "/promotions", artist, json=body | {"brand_name": None}, expect=422)
    plan = await api.ok("POST", "/promotions", artist, json=body)
    assert plan["status"] == "active" and plan["names"][0]["status"] == "approved" and plan["link_limit"] == 3
    urls = {l["url"] for l in links}

    # Curiosity about the artist: the name is tappable and the agent shares one of the artist's links.
    for n in range(4):
        await api.ok("POST", f"/posts/{pid}/comments", fan, json={"text": f"who is this new artist called 'oshamo'? ({n})"})
    threads = await comments_of(api, creator, pid)
    card = await api.ok("GET", threads[0]["promotions"][0]["card_url"], creator)
    assert [l["label"] for l in card["links"]] == ["Spotify", "Audiomack", "Tickets"]
    for thread in threads[:4]:
        reply = thread["replies"][0]
        assert reply["text"].startswith("Oshamo: ") and (await api.ok("POST", reply["link"], creator))["url"] in urls
    # In the right context the fitting link is the one shared.
    await api.ok("POST", f"/posts/{pid}/comments", fan, json={"text": "where can I get tickets to see Oshamo?"})
    # (One person can only set the agent off 5 times a day, so the next questions come from others.)
    fan2 = await api.signup("fan2")
    await api.ok("POST", f"/posts/{pid}/comments", fan2, json={"text": "who is Oshamo and is there a concert soon?"})
    await api.ok("POST", f"/posts/{pid}/comments", fan, json={"text": "who is Oshamo again?"})
    assert (await comments_of(api, creator, pid))[6]["replies"] == []  # the sixth from the same person is ignored
    for thread in (await comments_of(api, creator, pid))[4:6]:
        assert thread["replies"][0]["text"] == "Oshamo: Tickets."

    # A name another verified account holds cannot be bought, and nothing is charged.
    copycat = await api.influencer("copycat", "Copy Cat")
    charges = len(api.infra.providers.payments.charges)
    r = await api.call("POST", "/promotions", copycat, json=body | {"brand_name": "OSHAMO"}, expect=409)
    assert r.json()["code"] == "name_taken" and len(api.infra.providers.payments.charges) == charges
    # A name that is someone else's handle waits for a person to check it.
    contested = await api.ok("POST", "/promotions", copycat, json=body | {"brand_name": "Creator"})
    assert contested["names"][0]["status"] == "pending"
    await api.decide(admin, "promotions", contested["names"][0]["id"], decision="reject", reason="That is another account's name")

    # A stage name that is an everyday word only matches with a cue word, quotes or an @.
    # (A stage name goes live at once only when it matches an account the artist has already proven.)
    added = await api.ok("POST", "/promotions/names", copycat, json={"name": "Future"})
    assert next(n for n in added["names"] if n["name"] == "Future")["status"] == "pending"
    await api.ok("DELETE", f"/promotions/names/{next(n for n in added['names'] if n['name'] == 'Future')['id']}", copycat)
    await api.ok("POST", "/verification/social", copycat, json={"platform": "instagram", "auth_code": "future_official:1200"})
    added = await api.ok("POST", "/promotions/names", copycat, json={"name": "Future"})
    future = next(n for n in added["names"] if n["name"] == "Future")
    assert future["status"] == "approved" and future["matches_in_plain_text"] is False
    await api.ok("POST", f"/posts/{pid}/comments", fan, json={"text": "the future is bright for afrobeats"})
    await api.ok("POST", f"/posts/{pid}/comments", fan, json={"text": "that new song by Future is everywhere"})
    plain, cued = (await comments_of(api, creator, pid))[7:9]
    assert "promotions" not in plain and len(cued["promotions"]) == 1

    # A label pays $80, adds each artist, and gives each artist their own links (3 each on base).
    label = await api.company("bigwave", admin, category="music")
    r = await api.call("POST", "/promotions", label, json={"kind": "label", "links": [SHOP], "payment_method": PAY}, expect=422)
    assert r.json()["code"] == "links_need_artist"
    await api.ok("POST", "/promotions", label, json={"kind": "label", "payment_method": PAY})
    assert (await api.call("GET", "/payments", label)).json()["data"][0]["amount_cents"] == 80_00
    roster = await api.ok("POST", "/promotions/names", label, json={"name": "Tidewater"})
    roster = await api.ok("POST", "/promotions/names", label, json={"name": "Ember Row"})
    tide = next(n for n in roster["names"] if n["name"] == "Tidewater")
    ember = next(n for n in roster["names"] if n["name"] == "Ember Row")
    assert tide["status"] == "approved" and roster["link_limit_applies"] == "per artist"
    await api.call("POST", "/promotions/links", label, json={"url": "https://a.example.com", "label": "Site"}, expect=422)
    for n in range(3):
        await api.ok("POST", "/promotions/links", label, json={
            "url": f"https://tide.example.com/{n}", "label": f"Tide {n}", "name_id": tide["id"]})
    r = await api.call("POST", "/promotions/links", label, json={
        "url": "https://tide.example.com/3", "label": "Tide 3", "name_id": tide["id"]}, expect=409)
    assert r.json()["code"] == "link_limit"
    await api.ok("POST", "/promotions/links", label, json={
        "url": "https://ember.example.com/album", "label": "Album", "name_id": ember["id"]})
    # Each artist's mention shares only that artist's links.
    await api.ok("POST", f"/posts/{pid}/comments", fan2, json={"text": "who is Ember Row? where can I listen?"})
    reply = (await comments_of(api, creator, pid))[9]["replies"][0]
    assert reply["text"] == "Ember Row: Album." and (await api.ok("POST", reply["link"], creator))["url"] == "https://ember.example.com/album"
    by_name = {row["name"]: row for row in (await api.ok("GET", "/analytics/me", label))["wamiwa_agent"]["by_name"]}
    assert by_name["Ember Row"]["agent_replies"] == 1 and by_name["Ember Row"]["link_clicks"] == 1
    # Removing an artist removes their links too.
    after = await api.ok("DELETE", f"/promotions/names/{tide['id']}", label)
    assert [l["label"] for l in after["links"]] == ["Album"]
    # It renews at the label price.
    api.clock.advance(days=30, minutes=1)
    assert (await api.run("promos.renew_due"))["renewed"] >= 1
    assert [p["amount_cents"] for p in (await api.call("GET", "/payments", label)).json()["data"]] == [80_00, 80_00]


async def test_individual_sellers_and_suggested_sellers(api):
    admin = await api.admin()
    brand = await api.company("kofishirts", admin, category="fashion", region="nigeria")
    nolinks = await api.company("emptyco", admin, category="fashion")
    seller = await api.influencer("tola", "Tola Bello")
    creator, writer = await api.signup("creator"), await api.signup("writer")
    reader = await api.signup("reader", region="nigeria")
    await api.ok("PUT", "/admin/promotions/keywords", admin, json={
        "category": "fashion", "keywords": ["shirt", "dress", "sneaker"], "reason": "Launch list"})
    await api.ok("POST", "/promotions", brand, json={"kind": "business", "links": [SHOP], "payment_method": PAY})
    await api.ok("POST", "/promotions", nolinks, json={"kind": "business", "payment_method": PAY})

    # An individual seller's shop name and links are reviewed before going live.
    body = {"kind": "seller", "brand_name": "Tola Thrift", "category": "Fashion", "payment_method": PAY,
            "links": [{"url": "https://tolathrift.example.com", "label": "Shop"}]}
    await api.call("POST", "/promotions", seller, json=body | {"category": None}, expect=422)
    pending = await api.ok("POST", "/promotions", seller, json=body)
    assert pending["status"] == "pending_review" and pending["names"][0]["status"] == "pending"
    # Only sellers who are live and have a link to share are listed.
    assert [s["name"] for s in (await api.ok("GET", "/promotions/suggested?category=fashion", reader))["sellers"]] == ["Kofishirts Ltd"]
    await api.decide(admin, "promotions", pending["id"])
    live = await api.ok("GET", "/promotions", seller)
    assert live["status"] == "active" and live["names"][0]["status"] == "approved"

    post = await api.ok("POST", "/posts", creator, json={"type": "text", "text": "Outfit of the day"})
    pid = post["id"]
    # A wish to buy with no brand named gets the "Suggested sellers" link, once per thread.
    want = await api.ok("POST", f"/posts/{pid}/comments", writer, json={"text": "I love plain white shirts a lot"})
    await api.ok("POST", f"/posts/{pid}/comments", writer, json={"text": "I need a dress too", "parent_id": want["id"]})
    for text in ("my ex loved plain white shirts", "I don't like shirts at all",
                 "I need a shirt for my father's funeral", "shirts are everywhere this season"):
        await api.ok("POST", f"/posts/{pid}/comments", writer, json={"text": text})
    shown = await comments_of(api, reader, pid)
    link = shown[0]["suggested_sellers"]
    assert link["label"] == "Suggested sellers" and link["sponsored"] and link["category"] == "fashion"
    assert "suggested_sellers" not in shown[0]["replies"][0]
    assert all("suggested_sellers" not in c for c in shown[1:])

    # The list: the reader's region first, capped, labeled sponsored; taps are counted for the seller.
    listed = await api.ok("GET", link["url"], reader)
    assert [s["name"] for s in listed["sellers"]][0] == "Kofishirts Ltd" and listed["sellers"][0]["in_your_region"]
    assert {s["name"] for s in listed["sellers"]} == {"Kofishirts Ltd", "Tola Thrift"} and listed["sponsored"]
    tola = next(s for s in listed["sellers"] if s["name"] == "Tola Thrift")
    assert (await api.ok("POST", tola["click_url"], reader))["url"] == "https://tolathrift.example.com"
    stats = (await api.ok("GET", "/analytics/me", seller))["wamiwa_agent"]
    assert stats["totals"]["list_impressions"] == 1 and stats["totals"]["list_clicks"] == 1
    assert stats["rates"]["list_click_rate_pct"] == 100.0

    # A seller's new link is reviewed too, and a rejected first application is refunded.
    changed = await api.ok("POST", "/promotions/links", seller, json={"url": "https://new-shop.example.com", "label": "New shop"})
    assert changed["status"] == "pending_review"
    other = await api.influencer("bisi", "Bisi Ade")
    applied = await api.ok("POST", "/promotions", other, json=body | {"brand_name": "Bisi Bags"})
    refunds = api.infra.providers.payments.refunds
    before = len(refunds)
    await api.decide(admin, "promotions", applied["id"], decision="reject", reason="Link leads to a betting site")
    assert refunds[before]["amount"] == kobo(20_00) and (await api.ok("GET", "/promotions", other))["status"] == "rejected"

    # A seller cannot take a name that is a verified company's, and is not charged for trying.
    clash = await api.influencer("clash", "Clash Person")
    charges = len(api.infra.providers.payments.charges)
    taken = await api.call("POST", "/promotions", clash, json=body | {"brand_name": "Kofishirts Ltd"}, expect=409)
    assert taken.json()["code"] == "name_taken" and len(api.infra.providers.payments.charges) == charges


async def test_plus_package_content_ideas_for_target_regions(api):
    admin = await api.admin()
    brand = await api.company("kofishirts", admin, category="fashion", region="nigeria")
    plan = await api.ok("POST", "/promotions", brand, json={"kind": "business", "links": [SHOP], "payment_method": PAY})
    r = await api.call("GET", "/promotions/content-ideas", brand, expect=403)
    assert r.json()["code"] == "plus_required"
    await api.ok("PATCH", "/promotions", brand, json={"tier": "plus"})

    # A brand in Nigeria can aim at China and the US.
    await api.call("PATCH", "/promotions", brand, json={"target_regions": ["ghana", "kenya", "india", "japan", "peru", "chile"]}, expect=422)
    r = await api.call("PATCH", "/promotions", brand, json={"target_regions": ["Atlantis"]}, expect=422)
    assert r.json()["code"] == "unknown_region"
    updated = await api.ok("PATCH", "/promotions", brand, json={"target_regions": ["China", "US"]})
    assert updated["target_regions"] == ["china", "united_states"]  # "US" is stored under its full name and plan["id"] == updated["id"]

    # Science: what people in China engaged with on Wamiwa this week...
    poster, liker = await api.signup("meili", region="China"), await api.signup("chen", region="China")
    hot = await api.ok("POST", "/posts", poster, json={"type": "text", "text": "Street looks today #hanfu #citywalk"})
    await api.ok("POST", "/posts", poster, json={"type": "text", "text": "Quiet day #teatime"})
    await api.ok("PUT", f"/posts/{hot['id']}/like", liker)
    await api.ok("POST", f"/posts/{hot['id']}/comments", liker, json={"text": "Love this"})
    # ...and an outside trends source for the US.
    api.infra.providers.trends.signals["united_states"] = [TrendSignal("thrift flips", 0.9, "search trends"),
                                                TrendSignal("quiet luxury", 0.5, "search trends")]

    ideas = await api.ok("GET", "/promotions/content-ideas", brand)
    china, us = ideas["regions"]
    assert china["region"] == "china" and us["region"] == "united_states"
    assert {i["topic"] for i in china["ideas"]} >= {"hanfu", "citywalk"} and china["based_on"]["posts_analysed"] == 2
    top = china["ideas"][0]
    assert top["topic"] in ("hanfu", "citywalk") and "engagement on Wamiwa" in top["why"]
    # Art: the region's own upcoming moment is worked into the idea.
    assert "Singles' Day" in china["based_on"]["upcoming_moments"] and any("Singles' Day" in i["idea"] for i in china["ideas"])
    assert us["ideas"][0]["topic"] == "thrift flips" and "search trends" in us["ideas"][0]["why"]
    assert "Halloween" in us["based_on"]["upcoming_moments"]
    # One region at a time, including one that is not on the saved list.
    only = await api.ok("GET", "/promotions/content-ideas?region=US", brand)
    assert [r["region"] for r in only["regions"]] == ["united_states"]
    quiet = await api.ok("GET", "/promotions/content-ideas?region=brazil", brand)
    assert quiet["regions"][0]["based_on"]["posts_analysed"] == 0  # nothing measurable yet: says so
    # Dropping back to base turns ideas off.
    await api.ok("PATCH", "/promotions", brand, json={"tier": "base"})
    await api.call("GET", "/promotions/content-ideas", brand, expect=403)


async def test_default_links_when_none_are_added(api):
    admin = await api.admin()
    music = api.infra.providers.music
    creator, fan = await api.signup("creator"), await api.signup("fan")
    reader = await api.signup("reader")
    post = await api.ok("POST", "/posts", creator, json={"type": "text", "text": "New music Friday"})
    pid = post["id"]

    # An artist who adds no links: we look on Spotify, then YouTube Music, then Audiomack, and use the first found.
    yt = "https://music.youtube.com/channel/UCzino123"
    music.artists[("youtube_music", "zinolee")] = yt
    music.artists[("audiomack", "zinolee")] = "https://audiomack.com/zinolee"
    artist = await api.influencer("zinolee_ng", "Zino Lee")
    plan = await api.ok("POST", "/promotions", artist, json={"kind": "artist", "brand_name": "Zinolee", "payment_method": PAY})
    assert plan["links"] == [] and plan["names"][0]["default_link"] == {"url": yt, "label": "YouTube Music"}
    await api.ok("POST", f"/posts/{pid}/comments", fan, json={"text": "who is Zinolee? where can I listen?"})
    asked = (await comments_of(api, creator, pid))[0]
    assert asked["replies"][0]["text"] == "Zinolee: YouTube Music."
    assert (await api.ok("POST", asked["replies"][0]["link"], creator))["url"] == yt
    # Once the artist turns up on Spotify, the weekly check moves the default there.
    sp = "https://open.spotify.com/artist/zino456"
    music.artists[("spotify", "zinolee")] = sp
    api.clock.advance(days=8)
    assert await api.run("promos.refresh_defaults") >= 1
    card = await api.ok("GET", asked["promotions"][0]["card_url"], reader)
    assert [l["label"] for l in card["links"]] == ["Spotify"] and card["links"][0]["report_url"] is None
    # A link the artist adds themselves replaces the default; deleting it brings the default back.
    own = await api.ok("POST", "/promotions/links", artist, json={"url": "https://zinolee.example.com/ep", "label": "New EP"})
    assert [l["label"] for l in (await api.ok("GET", asked["promotions"][0]["card_url"], reader))["links"]] == ["New EP"]
    await api.ok("DELETE", f"/promotions/links/{own['links'][0]['id']}", artist)
    card = await api.ok("GET", asked["promotions"][0]["card_url"], reader)
    assert (await api.ok("POST", card["links"][0]["click_url"], reader))["url"] == sp

    # A label's artist with no links gets the same treatment; one found nowhere has nothing to share.
    label = await api.company("bigwave", admin, category="music")
    await api.ok("POST", "/promotions", label, json={"kind": "label", "payment_method": PAY})
    music.artists[("audiomack", "tidewater")] = "https://audiomack.com/tidewater"
    roster = await api.ok("POST", "/promotions/names", label, json={"name": "Tidewater"})
    roster = await api.ok("POST", "/promotions/names", label, json={"name": "Nobody Knows Them"})
    defaults = {n["name"]: n["default_link"] for n in roster["names"]}
    assert defaults["Tidewater"]["label"] == "Audiomack" and defaults["Nobody Knows Them"] is None

    # A business that adds no links: the website on its company profile is shared.
    brand = await api.company("kofishirts", admin, category="fashion")
    await api.ok("POST", "/promotions", brand, json={"kind": "business", "payment_method": PAY})
    assert (await api.ok("GET", "/promotions/suggested?category=fashion", reader))["sellers"] == []
    await api.ok("PATCH", "/companies/settings", brand, json={"website": "https://kofishirts.example.com"})
    listed = (await api.ok("GET", "/promotions/suggested?category=fashion", reader))["sellers"]
    assert [s["name"] for s in listed] == ["Kofishirts Ltd"]
    assert (await api.ok("POST", listed[0]["click_url"], reader)) == {"url": "https://kofishirts.example.com", "label": "Website"}
    await api.ok("POST", f"/posts/{pid}/comments", fan, json={"text": "where can I buy from Kofishirts Ltd?"})
    assert (await comments_of(api, creator, pid))[1]["replies"][0]["text"] == "Kofishirts Ltd: Website."


async def test_suspected_scam_links_are_held_and_reported_to_admins(api):
    from tests.test_platform import invite_admin

    root = await api.admin()
    mod = await invite_admin(api, root, "mod")
    brand = await api.company("kofishirts", root, category="fashion")
    creator = await api.signup("creator")
    post = await api.ok("POST", "/posts", creator, json={"type": "text", "text": "Shopping thread"})
    pid = post["id"]
    plan = await api.ok("POST", "/promotions", brand, json={"kind": "business", "links": [SHOP], "payment_method": PAY})
    good_id = plan["links"][0]["id"]

    # A suspect link is saved but never shared; the owner is warned and every Admin and Super Admin is told.
    added = await api.ok("POST", "/promotions/links", brand, json={"url": "https://bit.ly/3xYzAbc", "label": "Deal"})
    bad = added["links"][1]
    assert "looks unsafe" in added["warning"] and bad["under_review"] and not bad["shared_by_agent"]
    assert "link shortener" in bad["review_reason"]
    for admin in (root, mod):
        assert any(n["title"] == "Suspected scam link on a Wamiwa Agent plan" for n in await api.ok("GET", "/notifications", admin))
    assert any(n["title"] == "One of your links is being checked" for n in await api.ok("GET", "/notifications", brand))
    await api.call("POST", f"/promotions/links/{bad['id']}/click", creator, expect=410)
    await api.ok("POST", f"/posts/{pid}/comments", creator, json={"text": "Kofishirts Ltd has nice things"})
    card_url = (await comments_of(api, creator, pid))[0]["promotions"][0]["card_url"]
    assert [l["label"] for l in (await api.ok("GET", card_url, creator))["links"]] == ["Shop"]
    # An unsafe website is refused outright.
    r = await api.call("PATCH", "/companies/settings", brand, json={"website": "https://paypa1-login-secure.example.com"}, expect=422)
    assert r.json()["code"] == "unsafe_link"

    # An admin clears a link that turns out to be fine, and the daily check does not flag it again for the same reason.
    item = next(i for i in await api.ok("GET", "/admin/queues/promotions", mod) if i["target_id"] == bad["id"])
    assert item["severity"] == "high"
    await api.decide(mod, "promotions", bad["id"], reason="Checked: it is their own campaign page")
    assert (await api.ok("POST", f"/promotions/links/{bad['id']}/click", creator))["url"] == "https://bit.ly/3xYzAbc"
    assert await api.run("promos.recheck_links") == 0

    # Readers can report a link. Three different people switch it off until it is reviewed.
    await api.call("POST", f"/promotions/links/{good_id}/report", brand, json={"details": "x"}, expect=422)
    reporters = [await api.signup(f"reader{n}") for n in range(3)]
    results = [await api.ok("POST", f"/promotions/links/{good_id}/report", r, json={"details": "Asked for my card PIN"})
               for r in reporters]
    assert [r["switched_off"] for r in results] == [False, False, True]
    await api.call("POST", f"/promotions/links/{good_id}/report", reporters[0], json={"details": "again"}, expect=409)
    r = await api.call("POST", f"/promotions/links/{good_id}/click", creator, expect=410)
    assert r.json()["code"] == "link_under_review"
    # Confirmed as a scam: the link is removed and the plan can be suspended in the same decision.
    await api.decide(root, "promotions", good_id, decision="reject", payload={"suspend_plan": True},
                     reason="Phishing page collecting card details")
    after = await api.ok("GET", "/promotions", brand)
    assert after["status"] == "suspended" and [l["label"] for l in after["links"]] == ["Deal"]
    assert "promotions" not in (await comments_of(api, creator, pid))[0]

    # The daily check catches a page that turns bad later, even one an admin cleared before.
    api.infra.providers.link_safety.blocklist.add("bit.ly")
    assert await api.run("promos.recheck_links") == 1
    assert (await api.ok("GET", "/promotions", brand))["links"][0]["under_review"]


async def test_admins_monitor_wamiwa_agent_plans(api):
    from tests.test_platform import invite_admin

    root = await api.admin()
    mod = await invite_admin(api, root, "mod")
    brand = await api.company("kofishirts", root, category="fashion")
    artist = await api.influencer("oshamo_music", "Osha Mo")
    plan = await api.ok("POST", "/promotions", brand, json={"kind": "business", "tier": "plus", "links": [SHOP], "payment_method": PAY})
    await api.ok("POST", "/promotions", artist, json={"kind": "artist", "brand_name": "Oshamo", "payment_method": PAY})
    reader = await api.signup("reader")
    post = await api.ok("POST", "/posts", reader, json={"type": "text", "text": "I bought from Kofishirts Ltd"})
    card = (await api.ok("GET", f"/posts/{post['id']}", artist))["promotions"][0]  # cards work on posts too
    await api.ok("GET", card["card_url"], artist)

    # Every admin sees the Wamiwa Agent dashboard; money figures need the revenue permission.
    seen = await api.ok("GET", "/admin/promotions/overview", mod)
    assert seen["plans_by_status"] == {"active": 2} and seen["active_plans"] == {"business:plus": 1, "artist:base": 1}
    assert seen["activity"]["card_impressions"] == 1 and seen["activity"]["card_opens"] == 1 and "revenue" not in seen
    full = await api.ok("GET", "/admin/promotions/overview", root)
    assert full["revenue"]["monthly_recurring_usd"] == 50.0 and full["revenue"]["collected_in_period"] == kobo(50_00)
    plans = await api.ok("GET", "/admin/promotions?kind=business", mod)
    assert [p["handle"] for p in plans] == ["kofishirts"]
    detail = await api.ok("GET", f"/admin/promotions/{plan['id']}", mod)
    assert detail["plan"]["links"][0]["label"] == "Shop" and detail["results"]["totals"]["card_opens"] == 1

    # Suspending a plan needs the suspend permission and a reason, and is written to the audit log.
    await api.call("POST", f"/admin/promotions/{plan['id']}/suspend", mod, json={"reason": "Misleading name"}, expect=403)
    await api.ok("POST", f"/admin/promotions/{plan['id']}/suspend", root, json={"reason": "Misleading name"})
    assert "promotions" not in await api.ok("GET", f"/posts/{post['id']}", artist)
    await api.ok("POST", f"/admin/promotions/{plan['id']}/restore", root, json={"reason": "Name corrected"})
    await api.call("DELETE", f"/admin/promotions/links/{plan['links'][0]['id']}", root, json={"reason": "Dead page"}, expect=200)
    actions = [e["action"] for e in await api.ok("GET", "/admin/audit-log", root)]
    assert {"promotions.suspend", "promotions.restore", "promotions.remove_link"} <= set(actions)
    # The main platform dashboard carries a Wamiwa Agent section as well.
    assert (await api.ok("GET", "/admin/dashboard", root))["metrics"]["wamiwa_agent"]["active_plans"] == 2


async def test_claiming_another_artists_name_is_stopped(api):
    admin = await api.admin()
    creator, fan = await api.signup("creator"), await api.signup("fan")
    post = await api.ok("POST", "/posts", creator, json={"type": "text", "text": "Who is everyone listening to?"})
    pid = post["id"]
    body = {"kind": "artist", "payment_method": PAY, "links": [{"url": "https://squat.example.com", "label": "Listen"}]}

    # 1. A stage name the account has not proven does not go live: a person checks it first.
    squatter = await api.influencer("randomguy", "Random Guy")
    held = await api.ok("POST", "/promotions", squatter, json=body | {"brand_name": "Ayra Bloom"})
    assert held["names"][0]["status"] == "pending"
    await api.ok("POST", f"/posts/{pid}/comments", fan, json={"text": "Ayra Bloom is brilliant"})
    assert "promotions" not in (await comments_of(api, creator, pid))[0]
    # Suppose an admin lets it through by mistake.
    await api.decide(admin, "promotions", held["names"][0]["id"], reason="Looked plausible")
    await api.ok("POST", f"/posts/{pid}/comments", fan, json={"text": "Ayra Bloom again"})
    assert "promotions" in (await comments_of(api, creator, pid))[1]

    # 2. A name that matches an account the artist proved by logging in goes live at once.
    real = await api.influencer("ayra", "Ayra Bello")
    await api.ok("POST", "/verification/social", real, json={"platform": "instagram", "auth_code": "ayrabloom_official:250000"})
    # 3. The real artist claims the name. Because they can show it is theirs, its cards pause straight away.
    r = await api.call("POST", "/promotions", real, json=body | {"brand_name": "Ayra Bloom"}, expect=409)
    assert r.json()["code"] == "name_taken"
    claim = await api.ok("POST", "/promotions/names/claim", real, json={"name": "ayra bloom", "evidence": "My Instagram is ayrabloom_official"})
    assert claim["paused_while_reviewed"] is True
    assert "promotions" not in (await comments_of(api, creator, pid))[1]
    await api.call("POST", "/promotions/names/claim", real, json={"name": "Ayra Bloom"}, expect=409)
    # A claimant who cannot show the name is theirs does not get to switch anyone off.
    rival = await api.influencer("rival", "Rival Person")
    assert (await api.ok("POST", "/promotions/names/claim", rival, json={"name": "Ayra Bloom"}))["paused_while_reviewed"] is False

    # An admin upholds the real artist's claim: the squatter loses the name and it is kept for its owner.
    await api.decide(admin, "promotions", claim["claim_id"], reason="Instagram account matches and is verified")
    assert (await api.ok("GET", "/promotions", squatter))["names"][0]["status"] == "rejected"
    mine = await api.ok("POST", "/promotions", real, json=body | {
        "brand_name": "Ayra Bloom", "links": [{"url": "https://ayrabloom.example.com", "label": "Official site"}]})
    assert mine["names"][0]["status"] == "approved"
    await api.ok("POST", f"/posts/{pid}/comments", fan, json={"text": "who is Ayra Bloom?"})
    reply = (await comments_of(api, creator, pid))[2]["replies"][0]
    assert reply["text"] == "Ayra Bloom: Official site."
    # Labels are verified companies, so their roster still goes live without a check.
    label = await api.company("bigwave", admin, category="music")
    await api.ok("POST", "/promotions", label, json={"kind": "label", "payment_method": PAY})
    assert (await api.ok("POST", "/promotions/names", label, json={"name": "Tidewater"}))["names"][-1]["status"] == "approved"


async def test_regions_cover_every_country_and_accept_common_names(api):
    listed = await api.ok("GET", "/regions")
    ids = {r["id"] for r in listed}
    assert len(listed) == 200 and {"nigeria", "united_kingdom", "brazil", "japan", "canada", "germany"} <= ids
    user = await api.signup("ama", region="Ghana")
    assert (await api.ok("GET", "/accounts/me", user))["region"] == "ghana"
    await api.ok("PATCH", "/accounts/me/profile", user, json={"region": "UK"})
    assert (await api.ok("GET", "/accounts/me", user))["region"] == "united_kingdom"
    r = await api.call("PATCH", "/accounts/me/profile", user, json={"region": "Narnia"}, expect=422)
    assert r.json()["code"] == "unknown_region"
