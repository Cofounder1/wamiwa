"""FD-08: editing posts and comments within 15 minutes, deleting, owners' control of agent content, paid-post locks."""
from tests.test_hiring import hire_me_setup, likeness_setup, offer, script_body


async def test_posts_can_be_edited_for_15_minutes_with_history(api):
    author = await api.signup("bisi")
    other = await api.signup("tunde")
    admin = await api.admin()
    post = await api.ok("POST", "/posts", author, json={"type": "text", "text": "Lagos traffic is wild tday"})
    assert post["edited_at"] is None

    edited = await api.ok("PATCH", f"/posts/{post['id']}", author, json={"text": "Lagos traffic is wild today"})
    assert edited["text"] == "Lagos traffic is wild today" and edited["edited_at"]
    assert (await api.ok("GET", f"/posts/{post['id']}", other))["edited_at"]
    # Only the author can edit; to anyone else the post is not theirs to find.
    await api.call("PATCH", f"/posts/{post['id']}", other, json={"text": "Hijacked"}, expect=404)
    r = await api.call("PATCH", f"/posts/{post['id']}", author, json={"text": "  "}, expect=422)
    assert r.json()["code"] == "text_required"

    # Staff can see every earlier version; saving the same text adds nothing.
    await api.ok("PATCH", f"/posts/{post['id']}", author, json={"text": "Lagos traffic is wild today"})
    history = await api.ok("GET", f"/admin/content/post/{post['id']}/edits", admin)
    assert [v["text"] for v in history["versions"]] == ["Lagos traffic is wild tday"]
    assert history["current_text"] == "Lagos traffic is wild today"
    await api.call("GET", f"/admin/content/post/{post['id']}/edits", author, expect=403)

    # After 15 minutes the post can only be deleted.
    api.clock.advance(minutes=15, seconds=1)
    r = await api.call("PATCH", f"/posts/{post['id']}", author, json={"text": "Too late"}, expect=409)
    assert r.json()["code"] == "edit_window_closed"
    await api.ok("DELETE", f"/posts/{post['id']}", author)
    await api.call("GET", f"/posts/{post['id']}", other, expect=404)
    await api.call("DELETE", f"/posts/{post['id']}", author, expect=404)


async def test_an_edit_goes_through_moderation_again(api):
    author = await api.signup("bisi")
    reader = await api.signup("tunde")
    post = await api.ok("POST", "/posts", author, json={"type": "text", "text": "Nice evening"})
    flagged = await api.ok("PATCH", f"/posts/{post['id']}", author, json={"text": "Click my scamlink now"})
    assert flagged["moderation_status"] == "flagged"
    await api.call("GET", f"/posts/{post['id']}", reader, expect=404)
    # Content under review can't be edited back to something harmless to dodge the review.
    r = await api.call("PATCH", f"/posts/{post['id']}", author, json={"text": "Nice evening"}, expect=409)
    assert r.json()["code"] == "under_review"


async def test_comment_editing_and_owners_deleting_their_agents_content(api):
    owner = await api.agent_owner()
    commenter = await api.signup("bisi")
    stranger = await api.signup("tunde")
    agent = await api.live_agent(owner)
    post = await api.ok("POST", "/posts", owner, json={"type": "text", "text": "Ask me about budgeting"})

    comment = await api.ok("POST", f"/posts/{post['id']}/comments", commenter, json={"text": "How do I strat?"})
    edited = await api.ok("PATCH", f"/comments/{comment['id']}", commenter, json={"text": "How do I start?"})
    assert edited["text"] == "How do I start?" and edited["edited_at"]
    listed = await api.ok("GET", f"/posts/{post['id']}/comments", stranger)
    assert listed[0]["text"] == "How do I start?" and listed[0]["edited_at"]
    await api.call("PATCH", f"/comments/{comment['id']}", stranger, json={"text": "Mine now"}, expect=404)
    api.clock.advance(minutes=16)
    r = await api.call("PATCH", f"/comments/{comment['id']}", commenter, json={"text": "Later"}, expect=409)
    assert r.json()["code"] == "edit_window_closed"
    await api.ok("DELETE", f"/comments/{comment['id']}", commenter)
    await api.call("PATCH", f"/comments/{comment['id']}", commenter, json={"text": "Back"}, expect=404)

    # Agents can't edit or delete (AI-03); their owner deletes for them from the app (AI-04).
    agent_comment = await api.ok("POST", f"/api/agent/v1/posts/{post['id']}/comments", headers=agent["headers"],
                                 json={"text": "Start with a monthly budget."})
    agent_post = await api.ok("POST", "/api/agent/v1/posts", headers=agent["headers"], json={"text": "Tip of the day"})
    await api.call("PATCH", f"/comments/{agent_comment['id']}", owner, json={"text": "Edited"}, expect=404)
    await api.call("DELETE", f"/comments/{agent_comment['id']}", stranger, expect=403)
    await api.call("DELETE", f"/posts/{agent_post['id']}", stranger, expect=403)
    await api.ok("DELETE", f"/comments/{agent_comment['id']}", owner)
    await api.ok("DELETE", f"/posts/{agent_post['id']}", owner)
    remaining = await api.ok("GET", f"/posts/{post['id']}/comments", stranger)
    assert {c["text"] for c in remaining} == {"[deleted]"}
    await api.call("GET", f"/posts/{agent_post['id']}", stranger, expect=404)


async def test_posts_named_in_a_hire_me_delivery_are_locked_until_the_contract_is_over(api):
    _admin, brand, influencer = await hire_me_setup(api)
    approved_post = await api.ok("POST", "/posts", influencer, json={"type": "text", "text": "Loving my Acme sneakers"})
    disputed_post = await api.ok("POST", "/posts", influencer, json={"type": "text", "text": "Acme week, day one"})

    first = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))
    await api.ok("POST", f"/hire-me/contracts/{first['id']}/accept", influencer)
    await api.ok("POST", f"/hire-me/contracts/{first['id']}/deliver", influencer,
                 json={"delivery": f"https://wamiwa.site/app/post/{approved_post['id']}"})
    r = await api.call("PATCH", f"/posts/{approved_post['id']}", influencer, json={"text": "Changed"}, expect=409)
    assert r.json()["code"] == "paid_post_locked" and "reviewing" in r.json()["error"]
    await api.call("DELETE", f"/posts/{approved_post['id']}", influencer, expect=409)
    await api.ok("POST", f"/hire-me/contracts/{first['id']}/approve", brand)
    await api.ok("PATCH", f"/posts/{approved_post['id']}", influencer, json={"text": "Still loving my Acme sneakers"})
    await api.ok("DELETE", f"/posts/{approved_post['id']}", influencer)

    second = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, influencer))
    await api.ok("POST", f"/hire-me/contracts/{second['id']}/accept", influencer)
    await api.ok("POST", f"/hire-me/contracts/{second['id']}/deliver", influencer,
                 json={"delivery": f"Posted: https://wamiwa.site/app/post/{disputed_post['id']}", "partial": True})
    await api.ok("POST", f"/contracts/{second['id']}/dispute", brand, json={"reason": "Only one of three posts delivered"})
    r = await api.call("DELETE", f"/posts/{disputed_post['id']}", influencer, expect=409)
    assert "dispute" in r.json()["error"]


async def test_likeness_videos_are_locked_for_the_term_and_review_window(api):
    _admin, brand, influencer = await likeness_setup(api)
    contract = await api.ok("POST", "/likeness/contracts", brand, json=script_body(influencer))
    cid = contract["id"]
    await api.ok("POST", f"/likeness/contracts/{cid}/accept", influencer,
                 json={"consent_image": True, "accept_responsibility": True})
    final = await api.ok("POST", f"/likeness/contracts/{cid}/videos", brand, json={"kind": "final"})
    await api.ok("POST", f"/videos/{final['id']}/decision", influencer, json={"approve": True})
    post = await api.ok("POST", f"/likeness/contracts/{cid}/posts", brand, json={"video_id": final["id"], "caption": "New"})

    r = await api.call("PATCH", f"/posts/{post['id']}", brand, json={"text": "Different caption"}, expect=409)
    assert r.json()["code"] == "paid_post_locked"
    await api.call("DELETE", f"/posts/{post['id']}", brand, expect=409)
    # The month ends and the review window opens: still locked.
    api.clock.advance(days=30, minutes=1)
    await api.run("likeness.end_terms")
    await api.call("DELETE", f"/posts/{post['id']}", brand, expect=409)
    # Funds clear after 7 quiet days; the contract is over and the brand may take its post down.
    api.clock.advance(days=7, minutes=1)
    await api.run("contracts.auto_clear")
    assert (await api.ok("GET", f"/contracts/{cid}", brand))["status"] == "completed"
    await api.ok("DELETE", f"/posts/{post['id']}", brand)


async def test_comment_counts_are_the_comments_readers_can_see(api):
    author = await api.signup("bisi")
    reader = await api.signup("tunde")
    admin = await api.admin()
    post = await api.ok("POST", "/posts", author, json={"type": "text", "text": "Rate my jollof"})
    pid = post["id"]
    counts = lambda: api.ok("GET", f"/posts/{pid}", reader)  # noqa: E731

    first = await api.ok("POST", f"/posts/{pid}/comments", reader, json={"text": "Looks great"})
    second = await api.ok("POST", f"/posts/{pid}/comments", reader, json={"text": "Needs more pepper"})
    assert (await counts())["comments"] == {"human": 2, "agent": 0}

    # Deleting a comment takes it off the count.
    await api.ok("DELETE", f"/comments/{first['id']}", reader)
    assert (await counts())["comments"]["human"] == 1
    await api.ok("DELETE", f"/comments/{first['id']}", reader)  # deleting twice changes nothing
    assert (await counts())["comments"]["human"] == 1

    # A comment hidden for review isn't counted; it counts once a moderator clears it.
    flagged = await api.ok("POST", f"/posts/{pid}/comments", reader, json={"text": "Click my scamlink"})
    assert flagged["moderation_status"] == "flagged" and (await counts())["comments"]["human"] == 1
    await api.decide(admin, "reports", flagged["id"], "approve")
    assert (await counts())["comments"]["human"] == 2

    # A comment a moderator removes after a report stops counting.
    report = await api.ok("POST", "/reports", author, json={"target_type": "comment", "target_id": second["id"], "reason": "harassment"})
    await api.decide(admin, "reports", report["id"], "approve")
    assert (await counts())["comments"]["human"] == 1
