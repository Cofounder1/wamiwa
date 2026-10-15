"""Feed, posts, comments, profiles, search, messaging, reports and support (Epics 2, 10, 11, 14)."""
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import Active, Anonymous, Any_, Services, ok, ok_page
from app.core.enums import ChatTab, FeedTab, PostType, ReportReason, ReportTarget, TicketCategory

router = APIRouter(tags=["social"])


class PostIn(BaseModel):
    type: PostType
    text: str = Field(default="", max_length=5000)
    media_ref: str | None = Field(default=None, max_length=255)
    duration_seconds: int = Field(default=0, ge=0, le=3600)
    ai_generated: bool = False
    generated_video_id: str | None = None
    tagged_handles: list[str] = Field(default_factory=list, max_length=10)


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    parent_id: str | None = None


class PostEditIn(BaseModel):
    text: str = Field(max_length=5000)


class CommentEditIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class RateIn(BaseModel):
    helpful: bool


class Attachment(BaseModel):
    kind: Literal["image", "video", "voice", "document"]
    ref: str = Field(max_length=255)
    size_bytes: int = Field(ge=0)


class MessageIn(BaseModel):
    body: str = Field(default="", max_length=5000)
    attachments: list[Attachment] = Field(default_factory=list, max_length=10)


class DirectMessageIn(MessageIn):
    recipient_id: str


class ReportIn(BaseModel):
    target_type: ReportTarget
    target_id: str
    reason: ReportReason
    details: str = Field(default="", max_length=2000)


class TicketIn(BaseModel):
    category: TicketCategory
    subject: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=3, max_length=5000)
    attachments: list[str] = Field(default_factory=list, max_length=5)
    linked_object: dict | None = None
    minor_id: str | None = None


class TicketReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    attachments: list[str] = Field(default_factory=list, max_length=5)


# --- feed and posts (FD-01, FD-02) -----------------------------------------------------------------
@router.get("/feed")
async def feed(p: Active, svc: Services, tab: FeedTab | None = None, filter: Literal["all", "following"] = "all",
               cursor: str | None = None, limit: int = Query(default=25, ge=1, le=50)):
    page, shown_tab = await svc.feed.feed(p.account, tab, filter == "following", cursor, limit)
    return ok_page(page, tab=shown_tab)


@router.get("/posts/compose/suggestions")
async def compose_suggestions(p: Active, svc: Services):
    return ok(await svc.posts.compose_suggestions(p.account))


@router.post("/posts", status_code=201)
async def create_post(body: PostIn, p: Active, svc: Services):
    post = await svc.posts.create(p, **body.model_dump())
    return ok(svc.posts.present(post, p.account))


@router.get("/posts/{post_id}")
async def get_post(post_id: str, p: Active, svc: Services):
    return ok(await svc.posts.get(p.account, post_id))


@router.patch("/posts/{post_id}")
async def edit_post(post_id: str, body: PostEditIn, p: Active, svc: Services):
    """FD-08: change the text within 15 minutes of posting. Photos and videos can't be swapped."""
    post = await svc.posts.edit(p.account, post_id, body.text)
    return ok(svc.posts.present(post, p.account))


@router.delete("/posts/{post_id}")
async def delete_post(post_id: str, p: Active, svc: Services):
    await svc.posts.delete(p.account, post_id)
    return ok()


@router.put("/posts/{post_id}/like")
async def like_post(post_id: str, p: Active, svc: Services):
    return ok(await svc.posts.set_like(p.account, post_id, True))


@router.delete("/posts/{post_id}/like")
async def unlike_post(post_id: str, p: Active, svc: Services):
    return ok(await svc.posts.set_like(p.account, post_id, False))


# --- comments (FD-03, FD-04) -------------------------------------------------------------------------
@router.get("/posts/{post_id}/comments")
async def list_comments(post_id: str, p: Active, svc: Services, sort: Literal["new", "old"] = "new",
                        cursor: str | None = None, limit: int = Query(default=35, ge=1, le=100)):
    return ok_page(await svc.comments.list_for_post(p.account, post_id, sort, cursor, limit))


@router.post("/posts/{post_id}/comments", status_code=201)
async def create_comment(post_id: str, body: CommentIn, p: Active, svc: Services):
    comment = await svc.comments.create(p, post_id, body.text, parent_id=body.parent_id)
    return ok(await svc.comments.created_view(comment, p.account))


@router.patch("/comments/{comment_id}")
async def edit_comment(comment_id: str, body: CommentEditIn, p: Active, svc: Services):
    """FD-08: change your comment within 15 minutes of posting it."""
    comment = await svc.comments.edit(p.account, comment_id, body.text)
    return ok(svc.comments.present(comment, {p.account.id: p.account}))


@router.delete("/comments/{comment_id}")
async def delete_comment(comment_id: str, p: Active, svc: Services):
    await svc.comments.delete(p.account, comment_id)
    return ok()


@router.put("/comments/{comment_id}/rating")
async def rate_agent_comment(comment_id: str, body: RateIn, p: Active, svc: Services):
    return ok(await svc.comments.rate(p.account, comment_id, body.helpful))


# --- profiles, follows, search (Epic 11) ----------------------------------------------------------------
@router.get("/profiles/{handle}")
async def view_profile(handle: str, p: Active, svc: Services):
    return ok(await svc.profiles.view(p.account, handle))


@router.get("/profiles/{handle}/posts")
async def profile_posts(handle: str, p: Active, svc: Services, cursor: str | None = None, limit: int = 25):
    profile = await svc.profiles.view(p.account, handle)
    return ok_page(await svc.posts.list_by_author(p.account, profile["id"], cursor, limit))


@router.get("/profiles/{handle}/popularity-card")
async def popularity_card(handle: str, p: Active, svc: Services):
    profile = await svc.profiles.view(p.account, handle)
    return ok(await svc.popularity.card(profile["id"]))


@router.put("/follows/{account_id}")
async def follow(account_id: str, p: Active, svc: Services):
    return ok(await svc.follows.follow(p.account, account_id))


@router.delete("/follows/{account_id}")
async def unfollow(account_id: str, p: Active, svc: Services):
    return ok(await svc.follows.unfollow(p.account, account_id))


@router.get("/search")
async def search(p: Active, svc: Services, q: str = Query(default="", max_length=200),
                 tab: Literal["people", "companies", "agents", "posts"] = "people",
                 niche: str | None = None, location: str | None = None, verified_only: bool = False,
                 available_for_hire: bool = False, category: str | None = None, skill: str | None = None,
                 max_price_cents: int | None = None, min_rating: float | None = None,
                 cursor: str | None = None, limit: int = Query(default=20, ge=1, le=50)):
    filters = {"niche": niche, "location": location, "verified_only": verified_only, "category": category,
               "available_for_hire": available_for_hire, "skill": skill, "max_price_cents": max_price_cents,
               "min_rating": min_rating}
    page = await svc.search.search(p.account, q, tab, filters, cursor, limit)
    for item in page.items:
        item.pop("cursor_id", None)
    return ok_page(page, query=q, tab=tab)


# --- messaging (Epic 10) --------------------------------------------------------------------------------
@router.get("/conversations")
async def conversations(p: Active, svc: Services, tab: ChatTab = ChatTab.PRIMARY, cursor: str | None = None,
                        limit: int = 25):
    return ok_page(await svc.messaging.conversations(p.account, tab, cursor, limit))


@router.post("/messages", status_code=201)
async def send_direct_message(body: DirectMessageIn, p: Active, svc: Services):
    return ok(await svc.messaging.send_direct(p.account, body.recipient_id, body.body,
                                              [a.model_dump() for a in body.attachments]))


@router.get("/conversations/{conversation_id}/messages")
async def conversation_messages(conversation_id: str, p: Active, svc: Services, cursor: str | None = None,
                                limit: int = 50):
    return {"success": True, "data": await svc.messaging.messages(p.account, conversation_id, cursor, limit)}


@router.post("/conversations/{conversation_id}/messages", status_code=201)
async def send_message(conversation_id: str, body: MessageIn, p: Active, svc: Services):
    return ok(await svc.messaging.send_in_conversation(p.account, conversation_id, body.body,
                                                       [a.model_dump() for a in body.attachments]))


@router.post("/conversations/{conversation_id}/typing")
async def typing(conversation_id: str, p: Active, svc: Services):
    await svc.messaging.set_typing(p.account, conversation_id)
    return ok()


@router.post("/conversations/{conversation_id}/request/{action}")
async def respond_to_message_request(conversation_id: str, action: Literal["accept", "delete"], p: Active,
                                     svc: Services):
    return ok(await svc.messaging.respond_to_request(p.account, conversation_id, action == "accept"))


# --- reports (RP-01) and support (Epic 14) -----------------------------------------------------------------
@router.post("/reports", status_code=201)
async def report(body: ReportIn, p: Active, svc: Services):
    return ok(await svc.reports.create(p.account, body.target_type, body.target_id, body.reason, body.details))


@router.get("/reports")
async def my_reports(p: Active, svc: Services):
    return ok(await svc.reports.mine(p.account))


@router.get("/support/help-centre", dependencies=[Anonymous])
async def help_centre(svc: Services):
    return ok(await svc.support.help_centre())


@router.post("/support/tickets", status_code=201)
async def open_ticket(body: TicketIn, p: Any_, svc: Services):
    return ok(await svc.support.create(p.account, **body.model_dump()))


@router.get("/support/tickets")
async def my_tickets(p: Any_, svc: Services, cursor: str | None = None, limit: int = 25):
    return ok_page(await svc.support.list_mine(p.account, cursor, limit))


@router.get("/support/tickets/{ticket_id}")
async def get_ticket(ticket_id: str, p: Any_, svc: Services):
    return ok(await svc.support.get(p.account, ticket_id))


@router.post("/support/tickets/{ticket_id}/replies")
async def reply_to_ticket(ticket_id: str, body: TicketReplyIn, p: Any_, svc: Services):
    return ok(await svc.support.reply(p.account, ticket_id, body.body, body.attachments))
