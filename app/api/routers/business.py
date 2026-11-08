"""Money (Epics 6, 8), companies and B2B (Epic 7), agent owners and the marketplace (Epics 3, 12)."""
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import Active, Services, ok, ok_page
from app.core.enums import BillingPeriod, VideoPlan, VideoTier

router = APIRouter(tags=["business"])
PaymentMethod = Field(min_length=3, max_length=64)


class PayIn(BaseModel):
    payment_method: str = PaymentMethod


class CheckoutIn(BaseModel):
    amount_cents: int = Field(gt=0, le=10_000_000, description="The price in US cents")
    description: str = Field(default="", max_length=127)


class SubscribeIn(PayIn):
    period: BillingPeriod = BillingPeriod.MONTHLY
    tier: Literal["standard", "pro"] = "standard"


class TierIn(PayIn):
    tier: Literal["standard", "pro"]


class RegionsIn(BaseModel):
    regions: list[str] = Field(max_length=5, description="Countries to get content ideas for (GET /regions)")


class PlanIn(PayIn):
    plan: VideoPlan


class ArconIn(PayIn):
    contract_id: str


class AiVideoIn(BaseModel):
    script: str = Field(min_length=3, max_length=5000)  # the most the video service reads
    seconds: int = Field(ge=5, le=600)
    tier: VideoTier = VideoTier.STANDARD
    payment_method: str | None = Field(default=None, max_length=64)
    voice: Literal["female", "male"] | None = None  # kept for their next videos and their likeness videos


class CompanyVerificationIn(BaseModel):
    registration_no: str = Field(min_length=2, max_length=64)
    document_ref: str = Field(min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=64)


class CompanySettingsIn(BaseModel):
    allow_user_messages: bool | None = None
    b2b_opt_out: bool | None = None
    region_targeting: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=64)
    bio: str | None = Field(default=None, max_length=2000)
    website: str | None = Field(default=None, max_length=500)


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class AgentIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    handle: str = Field(min_length=3, max_length=30)
    description: str = Field(default="", max_length=4000)
    skills: list[str] = Field(default_factory=list, max_length=30)
    rates: dict[str, int] = Field(description="Task name -> price in USD cents")
    free_tag_daily_cap: int = Field(default=20, ge=0, le=100000)
    accept_platform_fee: bool = Field(default=False, description="Agree to Wamiwa's percentage on this agent")


class AgentUpdateIn(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=4000)
    skills: list[str] | None = Field(default=None, max_length=30)
    rates: dict[str, int] | None = None
    free_tag_daily_cap: int | None = Field(default=None, ge=0, le=100000)


class WebhookIn(BaseModel):
    webhook_url: str = Field(min_length=8, max_length=500)


class OrderIn(PayIn):
    task: str = Field(min_length=1, max_length=80)
    brief: str = Field(min_length=3, max_length=8000)


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=4000)


class DeliveryIn(BaseModel):
    delivery: str = Field(min_length=1, max_length=20000)


# --- earnings, payouts, payments (PY-02, PY-03) ---------------------------------------------------------
@router.get("/earnings")
async def earnings(p: Active, svc: Services):
    return ok(await svc.escrow.earnings(p.id))


@router.get("/payouts")
async def payouts(p: Active, svc: Services, cursor: str | None = None, limit: int = 25):
    return ok_page(await svc.payouts.list_for(p.id, cursor, limit))


@router.get("/payments/method")
async def payment_method(p: Active, svc: Services):
    """PY-05: Flutterwave, in Naira in Nigeria and US dollars elsewhere; or PayPal, in dollars, once connected."""
    return ok(await svc.payment.method(p.account))


@router.post("/payments/checkout", status_code=201)
async def start_checkout(body: CheckoutIn, p: Active, svc: Services):
    """Step 1 of paying: what Flutterwave's checkout window needs. After paying, send `flw:<transaction id>` as the
    payment_method to the endpoint that takes the payment; Wamiwa verifies it with Flutterwave."""
    return ok(await svc.payment.start_checkout(p.account, body.amount_cents, body.description))


@router.post("/payments/paypal/order", status_code=201)
async def start_paypal_order(body: CheckoutIn, p: Active, svc: Services):
    """Step 1 of paying with PayPal: an order for the payer to approve in PayPal's window. After approving, send
    `paypal:<order id>` as the payment_method to the endpoint that takes the payment; Wamiwa captures it there.
    Answers 503 `paypal_unavailable` until PayPal is connected."""
    return ok(await svc.payment.start_paypal_order(p.account, body.amount_cents, body.description))


@router.get("/payments")
async def payment_history(p: Active, svc: Services, cursor: str | None = None, limit: int = 25):
    return ok_page(await svc.payment.history(p.account, cursor, limit))


# --- subscriptions (SB-01) and AI video (AV-01) ------------------------------------------------------------
@router.get("/subscriptions/plans")
async def subscription_plans(p: Active, svc: Services):
    return ok(await svc.subscriptions.plans())


@router.get("/subscriptions")
async def subscription_status(p: Active, svc: Services):
    return ok(await svc.subscriptions.status(p.account))


@router.post("/subscriptions", status_code=201)
async def subscribe(body: SubscribeIn, p: Active, svc: Services):
    return ok(await svc.subscriptions.subscribe(p.account, body.period, body.payment_method, body.tier))


@router.get("/subscriptions/upgrade-quote")
async def subscription_upgrade_quote(p: Active, svc: Services):
    """What moving up to Pro costs right now (the difference for the days left)."""
    return ok(await svc.subscriptions.upgrade_quote(p.account))


@router.patch("/subscriptions")
async def change_subscription_tier(body: TierIn, p: Active, svc: Services):
    """Upgrade to Pro now (pay the difference for the days left), or move back to Creator at renewal."""
    return ok(await svc.subscriptions.change_tier(p.account, body.tier, body.payment_method))


# --- Pro growth tools (SB-03) ----------------------------------------------------------------------------------
@router.get("/growth/partners")
async def growth_partners(p: Active, svc: Services):
    """Up to 50 verified businesses that fit what you post about, with the reasons."""
    return ok(await svc.growth.partners(p.account))


@router.put("/growth/regions")
async def growth_regions(body: RegionsIn, p: Active, svc: Services):
    return ok(await svc.growth.set_regions(p.account, body.regions))


@router.get("/growth/ideas")
async def growth_ideas(p: Active, svc: Services, region: str | None = Query(default=None, max_length=64)):
    return ok(await svc.growth.ideas(p.account, region))


@router.delete("/subscriptions")
async def cancel_subscription(p: Active, svc: Services):
    return ok(await svc.subscriptions.cancel(p.account))


@router.get("/ai-videos/quote")
async def ai_video_quote(p: Active, svc: Services, seconds: int = Query(ge=5, le=600),
                         tier: VideoTier = VideoTier.STANDARD):
    return ok(await svc.videos.personal_quote(p.account, seconds, tier))


@router.get("/ai-videos/plan")
async def ai_video_plan(p: Active, svc: Services):
    """AV-02: $30 a month for 15 standard videos of up to 2 minutes (longer ones use one per started 2 minutes)."""
    return ok(await svc.videos.personal_plan(p.account))


@router.post("/ai-videos/plan", status_code=201)
async def subscribe_ai_video_plan(body: PayIn, p: Active, svc: Services):
    return ok(await svc.videos.subscribe_personal_plan(p.account, body.payment_method))


@router.delete("/ai-videos/plan")
async def cancel_ai_video_plan(p: Active, svc: Services):
    """Stops at the end of the month already paid for."""
    return ok(await svc.videos.cancel_personal_plan(p.account))


@router.get("/ai-videos")
async def my_ai_videos(p: Active, svc: Services):
    return ok(await svc.videos.list_personal(p.account))


@router.post("/ai-videos", status_code=201)
async def generate_ai_video(body: AiVideoIn, p: Active, svc: Services):
    return ok(await svc.videos.generate_personal(p.account, body.script, body.seconds, body.tier, body.payment_method,
                                                body.voice))


@router.delete("/ai-videos/{video_id}")
async def delete_ai_video(video_id: str, p: Active, svc: Services):
    await svc.videos.delete_personal(p.account, video_id)
    return ok()


@router.get("/ai-videos/{video_id}/download")
async def download_ai_video(video_id: str, p: Active, svc: Services):
    return ok(await svc.videos.download(p.account, video_id))


# --- brand video plans (LK-10) ---------------------------------------------------------------------------------
@router.get("/video-plans/prices")
async def video_prices(p: Active, svc: Services):
    return ok(svc.video_plans.price_list())


@router.get("/video-plans")
async def video_plan_status(p: Active, svc: Services):
    return ok(await svc.video_plans.status(p.account))


@router.post("/video-plans", status_code=201)
async def buy_video_plan(body: PlanIn, p: Active, svc: Services):
    return ok(await svc.video_plans.subscribe(p.account, body.plan, body.payment_method))


@router.delete("/video-plans")
async def cancel_video_plan(p: Active, svc: Services):
    return ok(await svc.video_plans.cancel(p.account))


@router.post("/video-plans/arcon-filing", status_code=201)
async def request_arcon_filing(body: ArconIn, p: Active, svc: Services):
    return ok(await svc.video_plans.request_arcon_filing(p.account, body.contract_id, body.payment_method))


# --- companies (CO-01, CO-03, CO-04) and API keys ----------------------------------------------------------------
@router.post("/companies/verification")
async def submit_company_verification(body: CompanyVerificationIn, p: Active, svc: Services):
    await svc.uploads.claim(p.account, [body.document_ref], {"document", "id_document"})
    return ok(await svc.companies.submit_verification(p.account, body.registration_no, body.document_ref, body.category))


@router.patch("/companies/settings")
async def company_settings(body: CompanySettingsIn, p: Active, svc: Services):
    return ok(await svc.companies.update_settings(p.account, body.model_dump(exclude_unset=True)))


@router.get("/b2b/likely-buyers")
async def likely_buyers(p: Active, svc: Services, q: str | None = Query(default=None, max_length=100),
                        limit: int = Query(default=20, ge=1, le=50)):
    return ok(await svc.b2b.likely_buyers(p.account, q, limit))


@router.get("/b2b/feed")
async def b2b_feed(p: Active, svc: Services, cursor: str | None = None, limit: int = 25):
    return ok_page(await svc.b2b.feed(p.account, cursor, limit))


@router.get("/api-keys")
async def list_api_keys(p: Active, svc: Services):
    return ok(await svc.api_keys.list_keys(p.account))


@router.post("/api-keys", status_code=201)
async def create_api_key(body: ApiKeyIn, p: Active, svc: Services):
    return ok(await svc.api_keys.create(p.account, body.name))


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(key_id: str, p: Active, svc: Services):
    return ok(await svc.api_keys.revoke(p.account, key_id))


@router.get("/companies/posts/{post_id}/sentiment")
async def sentiment_report(post_id: str, p: Active, svc: Services):
    return ok(await svc.company_api.sentiment(p.account, post_id))


# --- agent owners (AG-01, AG-06, AI-01, AI-04) ---------------------------------------------------------------------
@router.get("/agents")
async def my_agents(p: Active, svc: Services):
    return ok(await svc.agents.list_mine(p.account))


@router.post("/agents", status_code=201)
async def deploy_agent(body: AgentIn, p: Active, svc: Services):
    return ok(await svc.agents.deploy(p.account, **body.model_dump()))


@router.patch("/agents/{agent_id}")
async def update_agent(agent_id: str, body: AgentUpdateIn, p: Active, svc: Services):
    return ok(await svc.agents.update(p.account, agent_id, body.model_dump(exclude_unset=True)))


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, p: Active, svc: Services):
    await svc.agents.delete(p.account, agent_id)
    return ok()


@router.post("/agents/{agent_id}/{action}")
async def pause_or_resume_agent(agent_id: str, action: Literal["pause", "resume"], p: Active, svc: Services):
    return ok(await svc.agents.set_paused(p.account, agent_id, action == "pause"))


@router.get("/agents/{agent_id}/activity")
async def agent_activity(agent_id: str, p: Active, svc: Services, cursor: str | None = None, limit: int = 50):
    return ok_page(await svc.agents.activity(p.account, agent_id, cursor, limit))


@router.get("/agents/{agent_id}/integration")
async def integration_status(agent_id: str, p: Active, svc: Services):
    return ok(await svc.integrations.status(p.account, agent_id))


@router.put("/agents/{agent_id}/integration/webhook")
async def set_webhook(agent_id: str, body: WebhookIn, p: Active, svc: Services):
    return ok(await svc.integrations.configure(p.account, agent_id, body.webhook_url))


@router.post("/agents/{agent_id}/integration/key")
async def rotate_agent_key(agent_id: str, p: Active, svc: Services):
    return ok(await svc.integrations.rotate_key(p.account, agent_id))


@router.delete("/agents/{agent_id}/integration/key")
async def revoke_agent_key(agent_id: str, p: Active, svc: Services):
    await svc.integrations.revoke_key(p.account, agent_id)
    return ok()


@router.post("/agents/{agent_id}/integration/test")
async def send_test_event(agent_id: str, p: Active, svc: Services):
    return ok(await svc.integrations.send_test(p.account, agent_id))


@router.get("/agent-orders")
async def owner_orders(p: Active, svc: Services, agent_id: str | None = None, cursor: str | None = None,
                       limit: int = 25):
    return ok_page(await svc.agent_orders.list_for_owner(p.account, agent_id, cursor, limit))


# --- marketplace and hiring an agent (AG-03, AG-04) ------------------------------------------------------------------
@router.get("/marketplace/agents")
async def marketplace(p: Active, svc: Services, q: str | None = Query(default=None, max_length=100),
                      skill: str | None = None, max_price_cents: int | None = Query(default=None, ge=0),
                      min_rating: float | None = Query(default=None, ge=0, le=5), similar_to: str | None = None,
                      cursor: str | None = None, limit: int = Query(default=25, ge=1, le=50)):
    data = await svc.agents.marketplace(q=q, skill=skill, max_price_cents=max_price_cents, min_rating=min_rating,
                                        similar_to=similar_to, cursor=cursor, limit=limit)
    return {"success": True, "data": data["items"], "count": len(data["items"]),
            "has_more": data["next_cursor"] is not None, "next_cursor": data["next_cursor"]}


@router.get("/marketplace/agents/{agent_id}")
async def marketplace_agent(agent_id: str, p: Active, svc: Services):
    return ok(await svc.agents.detail(agent_id))


@router.post("/marketplace/agents/{agent_id}/orders", status_code=201)
async def place_order(agent_id: str, body: OrderIn, p: Active, svc: Services):
    return ok(await svc.agent_orders.place(p.account, agent_id, body.task, body.brief, body.payment_method))


@router.post("/orders/{order_id}/approve")
async def approve_order(order_id: str, p: Active, svc: Services):
    return ok(await svc.agent_orders.approve(p.account, order_id))


@router.post("/orders/{order_id}/revision")
async def request_revision(order_id: str, body: NoteIn, p: Active, svc: Services):
    return ok(await svc.agent_orders.request_revision(p.account, order_id, body.note))


@router.post("/orders/{order_id}/refund")
async def refund_order(order_id: str, body: NoteIn, p: Active, svc: Services):
    return ok(await svc.agent_orders.refund(p.account, order_id, body.note))


@router.post("/orders/{order_id}/deliver")
async def owner_deliver_order(order_id: str, body: DeliveryIn, p: Active, svc: Services):
    return ok(await svc.agent_orders.deliver(p.account, order_id, body.delivery))
