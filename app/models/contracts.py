"""Hire Me, likeness and agent contracts with their scripts, videos and guardian confirmations."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, BigInteger, Boolean, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import (
    ChangeResponse, ContractStatus, GuardianResult, ScriptStatus, VideoDecision, VideoStatus,
)
from app.models.accounts import FK_ACCOUNT
from app.models.base import Model, UTCDateTime

FK_CONTRACT = "contracts.id"


class HireMePage(Model):
    __tablename__ = "hire_me_pages"

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), unique=True)
    starting_price_cents: Mapped[int] = mapped_column(Integer)
    photos: Mapped[list] = mapped_column(JSON, default=list)
    videos: Mapped[list] = mapped_column(JSON, default=list)
    available: Mapped[bool] = mapped_column(Boolean, default=True, index=True)  # "Not available to work as human"
    open_to_equity: Mapped[bool] = mapped_column(Boolean, default=False)  # HM-06: accepts cash + equity offers
    past_hires: Mapped[int] = mapped_column(Integer, default=0)
    rating_sum: Mapped[int] = mapped_column(Integer, default=0)
    rating_count: Mapped[int] = mapped_column(Integer, default=0)


class LikenessPage(Model):
    __tablename__ = "likeness_pages"

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), unique=True)
    price_image_cents: Mapped[int] = mapped_column(Integer)
    price_voice_cents: Mapped[int | None] = mapped_column(Integer)  # image + voice
    exclusivity_price_cents: Mapped[int | None] = mapped_column(Integer)
    voice_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    voice_consent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    paused: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    past_hires: Mapped[int] = mapped_column(Integer, default=0)


class LikenessAsset(Model):
    __tablename__ = "likeness_assets"

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    kind: Mapped[str] = mapped_column(String(8))
    file_ref: Mapped[str] = mapped_column(String(255))
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    face_match: Mapped[bool | None] = mapped_column(Boolean)
    consent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    uploaded_by: Mapped[str] = mapped_column(String(32))  # the guardian, for minors
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Contract(Model):
    """One row for all three hire types; type-specific rules live in the services."""

    __tablename__ = "contracts"

    type: Mapped[str] = mapped_column(String(10), index=True)
    hirer_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    earner_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)  # agent owner for agent hires
    agent_account_id: Mapped[str | None] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(20), default=ContractStatus.OFFERED, index=True)
    # Money: price is quoted in USD cents and locked into local currency on the day of payment.
    price_cents: Mapped[int] = mapped_column(Integer)
    fee_pct: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    # Terms
    title: Mapped[str] = mapped_column(String(200), default="")
    deliverables: Mapped[str] = mapped_column(Text, default="")
    num_posts: Mapped[int] = mapped_column(Integer, default=0)
    deadline: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deadline_extended: Mapped[bool] = mapped_column(Boolean, default=False)
    exclusivity: Mapped[bool] = mapped_column(Boolean, default=False)
    no_ai_replies: Mapped[bool] = mapped_column(Boolean, default=False)
    # HM-06: shares offered alongside the cash. Recorded only; Wamiwa doesn't hold, value or transfer them.
    equity_terms: Mapped[dict | None] = mapped_column(JSON)
    equity_acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    with_voice: Mapped[bool] = mapped_column(Boolean, default=False)
    influencer_must_post: Mapped[bool] = mapped_column(Boolean, default=False)
    takedown_days: Mapped[int | None] = mapped_column(Integer)
    hirer_category: Mapped[str | None] = mapped_column(String(64))
    requires_guardian: Mapped[bool] = mapped_column(Boolean, default=False)
    # Lifecycle
    offer_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    end: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    work_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    review_ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    partial_delivery: Mapped[bool] = mapped_column(Boolean, default=False)
    revision_used: Mapped[bool] = mapped_column(Boolean, default=False)
    delivery_ref: Mapped[str | None] = mapped_column(Text)
    change_rounds: Mapped[int] = mapped_column(Integer, default=0)
    video_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    cancelled_by: Mapped[str | None] = mapped_column(String(32))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[int | None] = mapped_column(Integer)
    conversation_id: Mapped[str | None] = mapped_column(String(32))


class Script(Model):
    __tablename__ = "scripts"
    __table_args__ = (UniqueConstraint("contract_id", "version"),)

    contract_id: Mapped[str] = mapped_column(ForeignKey(FK_CONTRACT), index=True)
    version: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    screening_result: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(14), default=ScriptStatus.PENDING)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ScriptChangeRequest(Model):
    __tablename__ = "script_change_requests"

    contract_id: Mapped[str] = mapped_column(ForeignKey(FK_CONTRACT), index=True)
    round: Mapped[int] = mapped_column(Integer)
    request_text: Mapped[str] = mapped_column(Text)
    response: Mapped[str] = mapped_column(String(10), default=ChangeResponse.PENDING)
    resulting_version: Mapped[int | None] = mapped_column(Integer)


class LikenessConsent(Model):
    """Signed consent for one exact script (LK-05). The hash pins the wording that was approved."""

    __tablename__ = "likeness_consents"

    contract_id: Mapped[str] = mapped_column(ForeignKey(FK_CONTRACT), unique=True)
    signed_by: Mapped[str] = mapped_column(String(32))
    script_version: Mapped[int] = mapped_column(Integer)
    script_sha256: Mapped[str] = mapped_column(String(64))
    includes_voice: Mapped[bool] = mapped_column(Boolean, default=False)
    accepted_responsibility: Mapped[bool] = mapped_column(Boolean, default=False)
    signed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    signer_ip: Mapped[str | None] = mapped_column(String(64))
    withdrawn_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class VideoProduction(Model):
    """Render allowance for one likeness video (LK-06 counter: "3 drafts, 1 final render left")."""

    __tablename__ = "video_productions"

    contract_id: Mapped[str] = mapped_column(ForeignKey(FK_CONTRACT), unique=True)
    seconds: Mapped[int] = mapped_column(Integer)
    drafts_left: Mapped[int] = mapped_column(Integer)
    finals_left: Mapped[int] = mapped_column(Integer)
    defect_claims: Mapped[int] = mapped_column(Integer, default=0)
    free_rerender_credit: Mapped[int] = mapped_column(Integer, default=0)
    funded_by_plan: Mapped[bool] = mapped_column(Boolean, default=False)
    payment_id: Mapped[str | None] = mapped_column(String(32))


class GeneratedVideo(Model):
    __tablename__ = "generated_videos"

    contract_id: Mapped[str | None] = mapped_column(String(32), index=True)  # None for AV-01 personal videos
    owner_account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    kind: Mapped[str] = mapped_column(String(10))
    tier: Mapped[str | None] = mapped_column(String(10))
    version: Mapped[int] = mapped_column(Integer, default=1)
    script_text: Mapped[str] = mapped_column(Text, default="")
    seconds: Mapped[int] = mapped_column(Integer, default=60)
    provider_job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(12), default=VideoStatus.PROCESSING, index=True)
    file_ref: Mapped[str | None] = mapped_column(String(255))
    watermark_id: Mapped[str | None] = mapped_column(String(40), index=True)
    influencer_decision: Mapped[str] = mapped_column(String(14), default=VideoDecision.NOT_REQUIRED)
    rejection_reason: Mapped[str | None] = mapped_column(String(20))
    rejection_note: Mapped[str | None] = mapped_column(Text)
    decision_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    warning_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    slow_notice_due: Mapped[bool] = mapped_column(Boolean, default=False)
    payment_id: Mapped[str | None] = mapped_column(String(32))
    plan_videos_used: Mapped[int] = mapped_column(Integer, default=0)  # AV-02: given back if generation fails
    retries: Mapped[int] = mapped_column(Integer, default=0)
    posted_post_id: Mapped[str | None] = mapped_column(String(32))
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # by its owner; kept for watermark tracing


class GuardianConfirmation(Model):
    __tablename__ = "guardian_confirmations"

    contract_id: Mapped[str] = mapped_column(ForeignKey(FK_CONTRACT), unique=True)
    guardian_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    minor_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT))
    result: Mapped[str] = mapped_column(String(24), default=GuardianResult.AWAITING_APP_APPROVAL, index=True)
    app_approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    otp_hash: Mapped[str | None] = mapped_column(String(64))
    call_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_call_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_call_id: Mapped[str | None] = mapped_column(String(64))
    recording_ref: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class CreatorVideoPlan(Model):
    """AV-02: an individual's monthly AI video plan. Each video of up to 2 minutes uses one of the month's videos;
    longer ones use one per started 2 minutes. Standard quality only."""

    __tablename__ = "creator_video_plans"

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), unique=True)
    videos_left: Mapped[int] = mapped_column(Integer, default=0)
    renews_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    payment_method: Mapped[str] = mapped_column(String(64))


class BrandVideoPlan(Model):
    """LK-10 Growth / Scale plan with one month of rollover."""

    __tablename__ = "brand_video_plans"

    company_account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), unique=True)
    plan: Mapped[str] = mapped_column(String(10))
    videos_left: Mapped[int] = mapped_column(Integer, default=0)
    rollover_left: Mapped[int] = mapped_column(Integer, default=0)
    renews_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    payment_method: Mapped[str] = mapped_column(String(64))
    amount_local: Mapped[int] = mapped_column(BigInteger, default=0)
