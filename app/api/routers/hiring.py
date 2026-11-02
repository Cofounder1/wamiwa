"""Hire Me (Epic 4), Hire My Likeness (Epic 5), contracts, video generation and disputes."""
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import AfterValidator, BaseModel, Field

from app.api.deps import Active, Services, ok, ok_page
from app.core.enums import (
    ChangeResponse, ContractType, DisputeDecision, RejectionReason, VideoKind,
)

router = APIRouter(tags=["hiring"])
PaymentMethod = Field(min_length=3, max_length=64, description="Token from the payment provider's checkout")
Refs = Field(default_factory=list, max_length=20)


class QuoteIn(BaseModel):
    type: ContractType
    price_cents: int = Field(gt=0)
    video_seconds: int = Field(default=0, ge=0, le=600, description="Likeness: the video length (0 = 60 seconds)")
    use_plan: bool = Field(default=False, description="Likeness: a video plan covers the first 60 seconds")


class HireMePageIn(BaseModel):
    starting_price_cents: int = Field(gt=0)
    photos: list[str] = Refs
    videos: list[str] = Refs


class AvailabilityIn(BaseModel):
    available: bool


def _assume_utc(value: datetime) -> datetime:
    """A deadline sent without a timezone (e.g. a plain date from a date picker) is read as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


Deadline = Annotated[datetime, AfterValidator(_assume_utc)]


class EquityIn(BaseModel):
    """HM-06: shares offered alongside the cash; give a percentage or a number of shares."""
    percent: float | None = Field(default=None, gt=0, le=100)
    shares: int | None = Field(default=None, gt=0)
    share_class: str | None = Field(default=None, max_length=40)
    vesting_months: int | None = Field(default=None, ge=0, le=120)
    cliff_months: int | None = Field(default=None, ge=0, le=60)
    agreement_ref: str = Field(min_length=1, max_length=255, description="The signed-ready agreement, uploaded as a document")
    description: str | None = Field(default=None, max_length=1000)


class AcceptIn(BaseModel):
    equity_acknowledged: bool = False
    payment_method: str | None = Field(default=None, max_length=64, description="Equity-only deals: the $5 service fee")


class EquityCancelIn(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


class EquityOptInIn(BaseModel):
    open_to_equity: bool


class OfferIn(BaseModel):
    influencer_id: str
    price_cents: int = Field(ge=0, description="0 only for equity-only deals")
    deliverables: str = Field(min_length=3, max_length=5000)
    deadline: Deadline
    num_posts: int = Field(ge=1, le=1000)
    no_ai_replies: bool = False
    payment_method: str = PaymentMethod
    equity: EquityIn | None = None


class DeliverIn(BaseModel):
    delivery: str = Field(min_length=1, max_length=5000)
    partial: bool = False


class ReleaseIn(BaseModel):
    release_pct: int = Field(gt=0, lt=100)


class DeadlineIn(BaseModel):
    new_deadline: Deadline


class ReasonIn(BaseModel):
    reason: str = Field(default="", max_length=2000)


class RatingIn(BaseModel):
    stars: int = Field(ge=1, le=5)


class LikenessPageIn(BaseModel):
    minor_id: str | None = None
    price_image_cents: int = Field(gt=0)
    price_voice_cents: int | None = Field(default=None, gt=0)
    exclusivity_price_cents: int | None = Field(default=None, gt=0)


class PhotoIn(BaseModel):
    minor_id: str | None = None
    file_ref: str = Field(max_length=255)
    liveness_selfie_ref: str | None = Field(default=None, max_length=255)


class VoiceIn(BaseModel):
    minor_id: str | None = None
    file_ref: str = Field(max_length=255)
    duration_seconds: int = Field(gt=0)
    recorded_in_app: bool
    voice_consent: bool = False


class ScriptIn(BaseModel):
    influencer_id: str
    script: str = Field(min_length=10, max_length=10000)
    with_voice: bool = False
    exclusivity: bool = False
    influencer_must_post: bool = False
    takedown_days: int | None = Field(default=None, ge=1, le=3650)
    video_seconds: int = Field(default=60, ge=5, le=600)
    use_plan: bool = False
    no_ai_replies: bool = False
    payment_method: str = PaymentMethod


class AcceptScriptIn(BaseModel):
    consent_image: bool
    consent_voice: bool = False
    accept_responsibility: bool


class ChangeRequestIn(BaseModel):
    request: str = Field(min_length=3, max_length=5000)


class ChangeResponseIn(BaseModel):
    action: Literal["accepted", "modified", "rejected"]
    new_script: str | None = Field(default=None, max_length=10000)


class GenerateIn(BaseModel):
    kind: Literal["draft", "final"]
    payment_method: str | None = Field(default=None, max_length=64)


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=2000)


class VideoDecisionIn(BaseModel):
    approve: bool
    reason: RejectionReason | None = None
    note: str = Field(default="", max_length=2000)


class PostVideoIn(BaseModel):
    video_id: str
    caption: str = Field(default="", max_length=5000)


class MisuseIn(BaseModel):
    details: str = Field(min_length=10, max_length=4000)


class DisputeIn(BaseModel):
    reason: str = Field(min_length=10, max_length=4000)
    is_misuse_claim: bool = False


class EvidenceIn(BaseModel):
    statement: str = Field(min_length=3, max_length=8000)
    attachments: list[str] = Refs


class AppealIn(BaseModel):
    reason: str = Field(min_length=10, max_length=4000)


# --- contracts: shared -----------------------------------------------------------------------------
@router.post("/contracts/quote")
async def quote(body: QuoteIn, p: Active, svc: Services):
    return ok(await svc.contracts.quote(body.type, body.price_cents, body.video_seconds, body.use_plan))


@router.get("/contracts")
async def my_contracts(p: Active, svc: Services, role: Literal["hirer", "earner"] = "earner",
                       type: ContractType | None = None, status: str | None = None, cursor: str | None = None,
                       limit: int = Query(default=25, ge=1, le=100)):
    return ok_page(await svc.contracts.list_for(p.id, role=role, type=type, status=status, cursor=cursor, limit=limit))


@router.get("/contracts/{contract_id}")
async def contract_detail(contract_id: str, p: Active, svc: Services):
    return ok(await svc.contracts.detail(p.account, contract_id))


@router.post("/contracts/{contract_id}/rating")
async def rate_contract(contract_id: str, body: RatingIn, p: Active, svc: Services):
    return ok(await svc.contracts.rate(p.account, contract_id, body.stars))


# --- Hire Me (HM-01 to HM-05) -------------------------------------------------------------------------
@router.put("/hire-me/page")
async def save_hire_me_page(body: HireMePageIn, p: Active, svc: Services):
    await svc.uploads.claim(p.account, body.photos, {"image"})
    await svc.uploads.claim(p.account, body.videos, {"video"})
    return ok(await svc.hire_me.save_page(p.account, body.starting_price_cents, body.photos, body.videos))


@router.put("/hire-me/availability")
async def set_availability(body: AvailabilityIn, p: Active, svc: Services):
    return ok(await svc.hire_me.set_availability(p.account, body.available))


@router.get("/hire-me/influencers")
async def browse_influencers(p: Active, svc: Services, niche: str | None = None, cursor: str | None = None,
                             limit: int = Query(default=25, ge=1, le=50)):
    return ok_page(await svc.hire_me.browse(p.account, niche, cursor, limit))


@router.get("/hire-me/influencers/{influencer_id}")
async def hire_me_page(influencer_id: str, p: Active, svc: Services):
    return ok(await svc.hire_me.view_page(p.account, influencer_id))


@router.post("/hire-me/offers", status_code=201)
async def send_offer(body: OfferIn, p: Active, svc: Services):
    return ok(await svc.hire_me.send_offer(p.account, **body.model_dump()))


@router.post("/hire-me/contracts/{contract_id}/accept")
async def accept_offer(contract_id: str, p: Active, svc: Services, body: AcceptIn | None = None):
    return ok(await svc.hire_me.accept(p.account, contract_id, bool(body and body.equity_acknowledged),
                                       body.payment_method if body else None))


@router.post("/contracts/{contract_id}/equity/cancel")
async def request_equity_cancellation(contract_id: str, body: EquityCancelIn, p: Active, svc: Services):
    """HM-06: either side asks to end the equity agreement; Wamiwa approves or not."""
    return ok(await svc.equity.request_cancellation(p.account, contract_id, body.reason))


@router.put("/hire-me/equity")
async def set_open_to_equity(body: EquityOptInIn, p: Active, svc: Services):
    """HM-06: whether brands may offer equity alongside cash."""
    return ok(await svc.hire_me.set_open_to_equity(p.account, body.open_to_equity))


@router.post("/hire-me/contracts/{contract_id}/decline")
async def decline_offer(contract_id: str, p: Active, svc: Services):
    return ok(await svc.hire_me.decline(p.account, contract_id))


@router.post("/hire-me/contracts/{contract_id}/start")
async def start_work(contract_id: str, p: Active, svc: Services):
    return ok(await svc.hire_me.start_work(p.account, contract_id))


@router.post("/hire-me/contracts/{contract_id}/deliver")
async def deliver(contract_id: str, body: DeliverIn, p: Active, svc: Services):
    return ok(await svc.hire_me.deliver(p.account, contract_id, body.delivery, body.partial))


@router.post("/hire-me/contracts/{contract_id}/approve")
async def approve_delivery(contract_id: str, p: Active, svc: Services):
    return ok(await svc.hire_me.approve(p.account, contract_id))


@router.post("/hire-me/contracts/{contract_id}/release-partial")
async def release_partial(contract_id: str, body: ReleaseIn, p: Active, svc: Services):
    return ok(await svc.hire_me.release_partial(p.account, contract_id, body.release_pct))


@router.post("/hire-me/contracts/{contract_id}/extend-deadline")
async def extend_deadline(contract_id: str, body: DeadlineIn, p: Active, svc: Services):
    return ok(await svc.hire_me.extend_deadline(p.account, contract_id, body.new_deadline))


@router.post("/hire-me/contracts/{contract_id}/cancel")
async def cancel_contract(contract_id: str, body: ReasonIn, p: Active, svc: Services):
    return ok(await svc.hire_me.cancel(p.account, contract_id, body.reason))


# --- likeness page (LK-01, LK-02, LK-03) -----------------------------------------------------------------
@router.get("/likeness/pricing-guide")
async def likeness_pricing_guide(p: Active, svc: Services):
    return ok(await svc.likeness_pages.pricing_guide(p.account))


@router.put("/likeness/page")
async def save_likeness_page(body: LikenessPageIn, p: Active, svc: Services):
    return ok(await svc.likeness_pages.save(
        p.account, body.minor_id, price_image_cents=body.price_image_cents,
        price_voice_cents=body.price_voice_cents, exclusivity_price_cents=body.exclusivity_price_cents))


@router.get("/likeness/assets")
async def likeness_assets(p: Active, svc: Services, minor_id: str | None = None):
    return ok(await svc.likeness_pages.assets(p.account, minor_id))


@router.post("/likeness/assets/photos", status_code=201)
async def add_likeness_photo(body: PhotoIn, p: Active, svc: Services):
    await svc.uploads.claim(p.account, [body.file_ref], {"image"})
    await svc.uploads.claim(p.account, [body.liveness_selfie_ref], {"liveness", "image"})
    return ok(await svc.likeness_pages.add_photo(p.account, body.minor_id, body.file_ref, body.liveness_selfie_ref))


@router.post("/likeness/assets/voice", status_code=201)
async def add_voice_sample(body: VoiceIn, p: Active, svc: Services):
    await svc.uploads.claim(p.account, [body.file_ref], {"likeness_voice"})
    return ok(await svc.likeness_pages.add_voice(p.account, body.minor_id, body.file_ref, body.duration_seconds,
                                                 body.recorded_in_app, body.voice_consent))


@router.delete("/likeness/assets/{asset_id}")
async def delete_likeness_asset(asset_id: str, p: Active, svc: Services, minor_id: str | None = None):
    return ok(await svc.likeness_pages.delete_asset(p.account, minor_id, asset_id))


@router.get("/likeness/influencers")
async def browse_likenesses(p: Active, svc: Services, niche: str | None = None, cursor: str | None = None,
                            limit: int = Query(default=25, ge=1, le=50)):
    await svc.access.require_verified_company(p.account, "browse likenesses")
    return ok_page(await svc.likeness_pages.browse(p.account, niche, cursor, limit))


@router.get("/likeness/influencers/{influencer_id}")
async def view_likeness(influencer_id: str, p: Active, svc: Services):
    return ok(await svc.likeness_pages.view(p.account, influencer_id))


# --- likeness contracts (LK-04 to LK-08) ---------------------------------------------------------------------
@router.post("/likeness/contracts", status_code=201)
async def send_script(body: ScriptIn, p: Active, svc: Services):
    return ok(await svc.likeness.send_script(p.account, **body.model_dump()))


@router.get("/likeness/contracts/{contract_id}")
async def likeness_contract(contract_id: str, p: Active, svc: Services):
    return ok(await svc.likeness.detail(p.account, contract_id))


@router.post("/likeness/contracts/{contract_id}/accept")
async def accept_script(contract_id: str, body: AcceptScriptIn, request: Request, p: Active, svc: Services):
    return ok(await svc.likeness.accept(
        p.account, contract_id, consent_image=body.consent_image, consent_voice=body.consent_voice,
        accept_responsibility=body.accept_responsibility,
        signer_ip=request.client.host if request.client else None))


@router.post("/likeness/contracts/{contract_id}/reject")
async def reject_script(contract_id: str, body: ReasonIn, p: Active, svc: Services):
    return ok(await svc.likeness.reject(p.account, contract_id, body.reason))


@router.post("/likeness/contracts/{contract_id}/change-requests")
async def request_script_changes(contract_id: str, body: ChangeRequestIn, p: Active, svc: Services):
    return ok(await svc.likeness.request_changes(p.account, contract_id, body.request))


@router.post("/likeness/contracts/{contract_id}/change-requests/respond")
async def respond_to_script_changes(contract_id: str, body: ChangeResponseIn, p: Active, svc: Services):
    return ok(await svc.likeness.respond_to_changes(p.account, contract_id, ChangeResponse(body.action), body.new_script))


@router.post("/likeness/contracts/{contract_id}/withdraw")
async def withdraw_offer(contract_id: str, p: Active, svc: Services):
    return ok(await svc.likeness.withdraw(p.account, contract_id))


@router.post("/likeness/contracts/{contract_id}/disengage")
async def disengage(contract_id: str, p: Active, svc: Services):
    return ok(await svc.likeness.disengage(p.account, contract_id))


@router.post("/likeness/contracts/{contract_id}/guardian-end")
async def guardian_end_contract(contract_id: str, body: ReasonIn, p: Active, svc: Services):
    return ok(await svc.likeness.guardian_end(p.account, contract_id, body.reason))


@router.post("/likeness/contracts/{contract_id}/misuse")
async def report_misuse(contract_id: str, body: MisuseIn, p: Active, svc: Services):
    return ok(await svc.likeness.report_misuse(p.account, contract_id, body.details))


@router.post("/likeness/contracts/{contract_id}/posts", status_code=201)
async def post_likeness_video(contract_id: str, body: PostVideoIn, p: Active, svc: Services):
    return ok(await svc.likeness.post_video(p.account, contract_id, body.video_id, body.caption))


# --- video generation and approval (LK-06) -------------------------------------------------------------------
@router.get("/likeness/contracts/{contract_id}/videos")
async def contract_videos(contract_id: str, p: Active, svc: Services):
    return ok(await svc.videos.list_for_contract(p.account, contract_id))


@router.post("/likeness/contracts/{contract_id}/videos", status_code=201)
async def generate_video(contract_id: str, body: GenerateIn, p: Active, svc: Services):
    return ok(await svc.videos.generate(p.account, contract_id, VideoKind(body.kind), body.payment_method))


@router.get("/videos/{video_id}")
async def get_video(video_id: str, p: Active, svc: Services):
    return ok(await svc.videos.get(p.account, video_id))


@router.post("/videos/{video_id}/defect")
async def claim_video_defect(video_id: str, body: NoteIn, p: Active, svc: Services):
    return ok(await svc.videos.claim_defect(p.account, video_id, body.note))


@router.post("/videos/{video_id}/decision")
async def decide_video(video_id: str, body: VideoDecisionIn, p: Active, svc: Services):
    return ok(await svc.videos.decide(p.account, video_id, body.approve, body.reason, body.note))


# --- disputes (PY-04) ------------------------------------------------------------------------------------
@router.get("/disputes")
async def my_disputes(p: Active, svc: Services):
    return ok(await svc.disputes.list_for(p.account))


@router.post("/contracts/{contract_id}/dispute", status_code=201)
async def open_dispute(contract_id: str, body: DisputeIn, p: Active, svc: Services):
    return ok(await svc.disputes.open(p.account, contract_id, body.reason, body.is_misuse_claim))


@router.get("/disputes/{dispute_id}")
async def view_dispute(dispute_id: str, p: Active, svc: Services):
    return ok(await svc.disputes.view(p.account, dispute_id))


@router.post("/disputes/{dispute_id}/evidence")
async def add_evidence(dispute_id: str, body: EvidenceIn, p: Active, svc: Services):
    await svc.uploads.claim(p.account, body.attachments, {"image", "video", "document", "voice_note"})
    return ok(await svc.disputes.add_evidence(p.account, dispute_id, body.statement, body.attachments))


@router.post("/disputes/{dispute_id}/appeal")
async def appeal_dispute(dispute_id: str, body: AppealIn, p: Active, svc: Services):
    return ok(await svc.disputes.appeal(p.account, dispute_id, body.reason))


__all__ = ["router", "DisputeDecision"]
