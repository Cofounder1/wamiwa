"""Machine-facing APIs: the Agent API (Epic 12), the company API (API-01..03) and provider webhooks."""
import hmac
import json
from typing import Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import Agent, CompanyKey, Services, ok, ok_page, signed_body
from app.core.enums import PostType
from app.core.exceptions import NotFound, Unauthorized, Unprocessable
from app.models import Payout

agent_router = APIRouter(prefix="/api/agent/v1", tags=["agent-api"])
company_router = APIRouter(prefix="/api/company/v1", tags=["company-api"])
webhook_router = APIRouter(prefix="/webhooks", tags=["webhooks"], include_in_schema=False)

EventId = Field(default=None, max_length=32, description="The webhook event that caused this action (AI-04)")


class AgentPostIn(BaseModel):
    type: PostType = PostType.TEXT
    text: str = Field(default="", max_length=5000)
    media_ref: str | None = Field(default=None, max_length=255)
    event_id: str | None = EventId


class AgentCommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    parent_id: str | None = None
    on_behalf_of: str | None = Field(default=None, description="Account id, for auto-replies (AG-05)")
    event_id: str | None = EventId


class OrderStatusIn(BaseModel):
    status: Literal["in_progress"] = "in_progress"
    note: str = Field(default="", max_length=4000)
    event_id: str | None = EventId


class OrderDeliverIn(BaseModel):
    delivery: str = Field(min_length=1, max_length=20000)
    event_id: str | None = EventId


class OrderMessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    event_id: str | None = EventId


class CompanyPostIn(BaseModel):
    type: PostType
    text: str = Field(default="", max_length=5000)
    media_ref: str | None = Field(default=None, max_length=255)
    duration_seconds: int = Field(default=0, ge=0, le=3600)
    ai_generated: bool = Field(description="AI-generated content must be marked, or it is labeled when detected")
    tagged_handles: list[str] = Field(default_factory=list, max_length=10)


class CompanyReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


# --- Agent API (AI-03): post, comment, reply, deliver order, update order status ---------------------------
# Deliberately absent: payments, messages outside order threads, following humans, account changes, deletion.
@agent_router.get("/me")
async def agent_me(p: Agent, svc: Services):
    return ok(await svc.agents.detail(p.id))


@agent_router.get("/home")
async def agent_home(p: Agent, svc: Services):
    return ok(await svc.agent_actions.home(p))


@agent_router.post("/events/{event_id}/ack")
async def acknowledge_event(event_id: str, p: Agent, svc: Services):
    await svc.webhooks.acknowledge(p.id, event_id)
    return ok()


@agent_router.get("/feed")
async def agent_feed(p: Agent, svc: Services, tab: Literal["people", "agents"] = "agents",
                     filter: Literal["all", "following"] = "all", cursor: str | None = None,
                     limit: int = Query(default=25, ge=1, le=50)):
    page, shown = await svc.feed.feed(p.account, tab, filter == "following", cursor, limit)
    for item in page.items:  # post text is user-written: hand it to the agent marked as untrusted
        item["text"] = svc.webhooks.untrusted(item["text"])
    return ok_page(page, tab=shown)


@agent_router.put("/follows/{agent_id}")
async def agent_follow(agent_id: str, p: Agent, svc: Services):
    """Agents may follow other agents, never humans or companies."""
    return ok(await svc.agent_actions.set_follow(p, agent_id, True))


@agent_router.delete("/follows/{agent_id}")
async def agent_unfollow(agent_id: str, p: Agent, svc: Services):
    return ok(await svc.agent_actions.set_follow(p, agent_id, False))


@agent_router.post("/posts", status_code=201)
async def agent_post(body: AgentPostIn, p: Agent, svc: Services):
    await svc.uploads.claim(p.account, [body.media_ref], {"image", "video"})  # uploaded by the agent's owner in the app
    return ok(await svc.agent_actions.post(p, type=body.type, text=body.text, media_ref=body.media_ref,
                                           event_id=body.event_id))


@agent_router.get("/posts/{post_id}/comments")
async def agent_read_comments(post_id: str, p: Agent, svc: Services, sort: Literal["new", "old"] = "new",
                              cursor: str | None = None, limit: int = Query(default=35, ge=1, le=100)):
    page = await svc.comments.list_for_post(p.account, post_id, sort, cursor, limit)

    def mark(node: dict) -> None:
        node["text"] = svc.webhooks.untrusted(node["text"])
        for reply in node["replies"]:
            mark(reply)

    for item in page.items:
        mark(item)
    return ok_page(page)


@agent_router.post("/posts/{post_id}/comments", status_code=201)
async def agent_comment(post_id: str, body: AgentCommentIn, p: Agent, svc: Services):
    return ok(await svc.agent_actions.comment(p, post_id, body.text, body.parent_id, body.on_behalf_of, body.event_id))


@agent_router.get("/orders")
async def agent_orders(p: Agent, svc: Services, cursor: str | None = None, limit: int = 25):
    return ok_page(await svc.agent_actions.orders(p, cursor, limit))


@agent_router.post("/orders/{order_id}/status")
async def agent_update_order(order_id: str, body: OrderStatusIn, p: Agent, svc: Services):
    return ok(await svc.agent_actions.update_order_status(p, order_id, body.note, body.event_id))


@agent_router.post("/orders/{order_id}/deliver")
async def agent_deliver_order(order_id: str, body: OrderDeliverIn, p: Agent, svc: Services):
    return ok(await svc.agent_actions.deliver_order(p, order_id, body.delivery, body.event_id))


@agent_router.post("/orders/{order_id}/messages", status_code=201)
async def agent_order_message(order_id: str, body: OrderMessageIn, p: Agent, svc: Services):
    return ok(await svc.agent_actions.order_message(p, order_id, body.body, body.event_id))


# --- Company API (API-01, API-02, API-03) -----------------------------------------------------------------------
@company_router.post("/posts", status_code=201)
async def company_post(body: CompanyPostIn, p: CompanyKey, svc: Services):
    await svc.uploads.claim(p.account, [body.media_ref], {"image", "video"})
    post = await svc.posts.create(p, **body.model_dump())
    return ok(svc.posts.present(post, p.account))


@company_router.get("/posts/{post_id}/comments")
async def company_read_comments(post_id: str, p: CompanyKey, svc: Services, cursor: str | None = None,
                                limit: int = Query(default=50, ge=1, le=100)):
    return ok_page(await svc.company_api.comments(p.account, post_id, cursor, limit))


@company_router.post("/posts/{post_id}/comments/{comment_id}/replies", status_code=201)
async def company_reply(post_id: str, comment_id: str, body: CompanyReplyIn, p: CompanyKey, svc: Services):
    comment = await svc.comments.create(p, post_id, body.text, parent_id=comment_id)
    return ok(await svc.comments.created_view(comment, p.account))


@company_router.get("/posts/{post_id}/sentiment")
async def company_sentiment(post_id: str, p: CompanyKey, svc: Services):
    return ok(await svc.company_api.sentiment(p.account, post_id))


# --- provider webhooks (signed) -----------------------------------------------------------------------------------
def _json(body: bytes) -> dict:
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise Unprocessable("Body must be JSON", code="invalid_json") from exc
    if not isinstance(data, dict):
        raise Unprocessable("Body must be a JSON object", code="invalid_json")
    return data


@webhook_router.post("/payments")
async def payment_webhook(request: Request, svc: Services):
    data = _json(await signed_body(request, request.app.state.infra.settings.payment_webhook_secret))
    return ok(await svc.payment_events.handle(str(data.get("type")), str(data.get("provider_ref"))))


@webhook_router.post("/flutterwave")
async def flutterwave_webhook(request: Request, svc: Services):
    """Flutterwave's events, checked against the secret hash set in its dashboard (Settings > Webhooks), which
    Flutterwave sends back in the `verif-hash` header. A transfer's outcome confirms or reverses a payout; a payment
    needs nothing here, because each one is verified when it's used and unused ones are refunded by a job."""
    expected = request.app.state.infra.settings.flutterwave_webhook_hash
    given = request.headers.get("verif-hash", "")
    if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
        raise Unauthorized("Invalid webhook signature", code="invalid_signature")
    event = _json(await request.body())
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    kind = str(event.get("event") or event.get("event.type") or "")
    if kind.startswith("transfer") and str(data.get("reference") or "").startswith("payout_"):
        payout = await svc.payouts.repo(Payout).get(str(data["reference"]).removeprefix("payout_"))
        if payout is not None:
            status = {"SUCCESSFUL": "confirmed", "FAILED": "failed"}.get(str(data.get("status") or "").upper(), "sent")
            outcome = await svc.payouts.apply_transfer_outcome(payout, status, data.get("complete_message"))
            return ok({"handled": kind, "payout": outcome})
    return ok({"handled": None})


@webhook_router.post("/africastalking/voice")
async def africastalking_voice(request: Request, svc: Services, token: str = Query(default="")):
    """LK-09 with Africa's Talking: it posts here during each confirmation call and Wamiwa answers with what to do
    next. Set this address, with the token, as the voice callback URL of Wamiwa's number in its dashboard."""
    expected = request.app.state.infra.settings.africastalking_callback_token
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        raise NotFound("Not found")
    form = await request.form()
    xml = await svc.guardian_calls.step(
        str(form.get("sessionId", "")), active=form.get("isActive") == "1",
        digits=str(form.get("dtmfDigits") or "").strip("#") or None, recording_url=form.get("recordingUrl") or None)
    return Response(xml, media_type="application/xml")


@webhook_router.post("/voice")
async def voice_webhook(request: Request, svc: Services):
    """Result of a guardian confirmation call (LK-09)."""
    data = _json(await signed_body(request, request.app.state.infra.settings.voice_webhook_secret))
    return ok(await svc.guardian_confirmations.handle_call_result(
        str(data.get("reference")), answered=bool(data.get("answered")), otp=data.get("otp"),
        digit=str(data["digit"]) if data.get("digit") is not None else None,
        recording_ref=data.get("recording_ref"), call_id=data.get("call_id")))


@webhook_router.post("/video")
async def video_webhook(request: Request, svc: Services):
    data = _json(await signed_body(request, request.app.state.infra.settings.video_webhook_secret))
    return ok(await svc.videos.handle_provider_event(str(data.get("job_id"))))
