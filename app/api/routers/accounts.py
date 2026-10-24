"""Own account: profile, settings, guardians, notifications, blocks, verification and deletion."""
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import Active, Anonymous, Any_, Services, ok, ok_page
from app.core import regions
from app.core.enums import (
    AgentCommentPolicy, DocumentType, FeedTab, PublicFigureMethod, SignalCategory, SocialPlatform,
)

router = APIRouter(tags=["account"])
Ref = Field(min_length=1, max_length=255)


class ProfileIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    photo: str | None = Field(default=None, max_length=255)
    bio: str | None = Field(default=None, max_length=2000)
    location: str | None = Field(default=None, max_length=120)
    region: str | None = Field(default=None, max_length=64)
    primary_niche: str | None = Field(default=None, max_length=64)
    secondary_niches: list[str] | None = Field(default=None, max_length=2)
    links: list[str] | None = Field(default=None, max_length=10)


class SettingsIn(BaseModel):
    feed_preference: FeedTab | None = None
    agent_comment_policy: AgentCommentPolicy | None = None
    read_receipts: bool | None = None
    typing_indicators: bool | None = None
    sponsored_cards: bool | None = None


class PhoneIn(BaseModel):
    phone: str = Field(max_length=20)


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class GuardianInviteIn(BaseModel):
    contact: str = Field(min_length=5, max_length=255, description="The guardian's email or phone number")


class GuardianAcceptIn(BaseModel):
    token: str
    relationship: str = Field(max_length=40)
    confirm_relationship: bool


class NotificationPrefsIn(BaseModel):
    preferences: dict[str, bool] = {}
    daily_digest: bool | None = None


class MarkReadIn(BaseModel):
    notification_ids: list[str] | None = None


class IdentityIn(BaseModel):
    document_type: DocumentType
    id_number: str | None = Field(default=None, max_length=32, description=(
        "Nigerian NIN or driver's licence number, looked up with YouVerify. Not needed for a passport or an ID "
        "from outside Nigeria, which is sent as document_ref instead"))
    document_ref: str | None = Field(default=None, max_length=255, description="Photo of the passport or ID")
    liveness_session_id: str | None = Field(default=None, max_length=128, description=(
        "YouVerify's live check: the session_id from POST /verification/identity/live-check"))
    selfie_ref: str | None = Field(default=None, max_length=255, description="Wamiwa's camera check: a live frame")
    liveness_ref: str | None = Field(default=None, max_length=255, description="Wamiwa's camera check: the clip")


class BankIn(BaseModel):
    bank_code: str = Field(min_length=2, max_length=16)
    account_number: str = Field(min_length=5, max_length=40, description="Account number, or IBAN for a foreign bank")
    account_name: str = Field(min_length=2, max_length=160)
    totp_code: str | None = None
    details: dict[str, str] | None = Field(default=None, description=(
        "Outside Nigeria: bank_name, swift_code or routing_number, beneficiary_address, beneficiary_country"))


class SocialIn(BaseModel):
    platform: SocialPlatform
    auth_code: str = Field(max_length=2000, description="Code returned by the platform's login flow")


class SignalIn(BaseModel):
    category: SignalCategory
    title: str = Field(min_length=3, max_length=200)
    evidence_ref: str = Field(min_length=3, max_length=500)


class PublicFigureIn(BaseModel):
    method: PublicFigureMethod
    evidence: list[str] = Field(min_length=1, max_length=20)


class ContactsIn(BaseModel):
    phones: list[str] = Field(max_length=1000)


class AppealIn(BaseModel):
    statement: str = Field(min_length=10, max_length=4000)


# --- profile and settings ---------------------------------------------------------------------
@router.get("/regions", dependencies=[Anonymous])
async def list_regions():
    """Every country and territory that can be chosen as a region or target region."""
    return ok(regions.options())


@router.get("/accounts/me")
async def me(p: Any_, svc: Services):
    return ok(await svc.accounts.me(p.account))


@router.patch("/accounts/me/profile")
async def update_profile(body: ProfileIn, p: Active, svc: Services):
    await svc.uploads.claim(p.account, [body.photo], {"image"})
    return ok(await svc.accounts.update_profile(p.account, body.model_dump(exclude_unset=True)))


@router.patch("/accounts/me/settings")
async def update_settings(body: SettingsIn, p: Active, svc: Services):
    return ok(await svc.accounts.update_settings(p.account, body.model_dump(exclude_unset=True)))


@router.post("/accounts/me/phone")
async def request_phone_code(body: PhoneIn, p: Active, svc: Services):
    return ok(await svc.accounts.request_phone_verification(p.account, body.phone))


@router.post("/accounts/me/phone/verify")
async def verify_phone(body: CodeIn, p: Active, svc: Services):
    return ok(await svc.accounts.verify_phone(p.account, body.code))


# --- deletion, export, appeal (DL-01, AD-02) -----------------------------------------------------
@router.get("/accounts/me/deletion")
async def deletion_blockers(p: Any_, svc: Services):
    return ok({"blockers": await svc.deletion.blockers(p.account), "status": p.account.status})


@router.post("/accounts/me/deletion")
async def request_deletion(p: Active, svc: Services):
    return ok(await svc.deletion.request(p.account))


@router.delete("/accounts/me/deletion")
async def cancel_deletion(p: Any_, svc: Services):
    return ok(await svc.deletion.cancel(p.account))


@router.get("/accounts/me/export")
async def export_my_data(p: Any_, svc: Services):
    return ok(await svc.deletion.export(p.account))


@router.post("/accounts/me/appeal")
async def appeal_suspension(body: AppealIn, p: Any_, svc: Services):
    return ok(await svc.trust.appeal(p.account, body.statement))


# --- guardians (ON-03) -----------------------------------------------------------------------------
@router.post("/guardians/invite")
async def invite_guardian(body: GuardianInviteIn, p: Any_, svc: Services):
    return ok(await svc.guardians.invite(p.account, body.contact))


@router.post("/guardians/accept")
async def accept_guardian_invite(body: GuardianAcceptIn, p: Active, svc: Services):
    return ok(await svc.guardians.accept(p.account, body.token, body.relationship, body.confirm_relationship))


@router.get("/guardians/minors")
async def my_minors(p: Active, svc: Services):
    return ok(await svc.guardians.minors(p.account))


@router.put("/guardians/minors/{minor_id}/freeze")
async def freeze_minor(minor_id: str, p: Active, svc: Services):
    return ok(await svc.guardians.set_frozen(p.account, minor_id, True))


@router.delete("/guardians/minors/{minor_id}/freeze")
async def unfreeze_minor(minor_id: str, p: Active, svc: Services):
    return ok(await svc.guardians.set_frozen(p.account, minor_id, False))


@router.get("/guardians/minors/{minor_id}/companies")
async def approved_companies(minor_id: str, p: Active, svc: Services):
    return ok(await svc.guardians.approved_companies(p.account, minor_id))


@router.put("/guardians/minors/{minor_id}/companies/{company_id}")
async def approve_company(minor_id: str, company_id: str, p: Active, svc: Services):
    return ok(await svc.guardians.approve_company(p.account, minor_id, company_id))


@router.delete("/guardians/minors/{minor_id}/companies/{company_id}")
async def revoke_company(minor_id: str, company_id: str, p: Active, svc: Services):
    await svc.guardians.revoke_company(p.account, minor_id, company_id)
    return ok()


@router.get("/guardians/minors/{minor_id}/contracts")
async def minor_contracts(minor_id: str, p: Active, svc: Services, cursor: str | None = None, limit: int = 25):
    return ok_page(await svc.guardian_confirmations.minor_contracts(p.account, minor_id, cursor, limit))


@router.get("/guardians/minors/{minor_id}/chats")
async def minor_company_chats(minor_id: str, p: Active, svc: Services):
    return ok(await svc.messaging.guardian_view(p.account, minor_id))


@router.get("/guardians/minors/{minor_id}/earnings")
async def minor_earnings(minor_id: str, p: Active, svc: Services):
    await svc.access.require_guardian_of(p.id, minor_id)
    return ok(await svc.escrow.earnings(minor_id))


@router.get("/guardians/confirmations")
async def pending_confirmations(p: Active, svc: Services):
    return ok(await svc.guardian_confirmations.pending(p.account))


@router.get("/guardians/confirmations/{contract_id}")
async def confirmation_status(contract_id: str, p: Active, svc: Services):
    return ok(await svc.guardian_confirmations.status(p.account, contract_id))


@router.post("/guardians/confirmations/{contract_id}/approve")
async def approve_minor_contract(contract_id: str, p: Active, svc: Services):
    return ok(await svc.guardian_confirmations.app_approve(p.account, contract_id))


@router.post("/guardians/confirmations/{contract_id}/decline")
async def decline_minor_contract(contract_id: str, p: Active, svc: Services):
    return ok(await svc.guardian_confirmations.decline(p.account, contract_id))


@router.post("/guardians/confirmations/{contract_id}/request-call")
async def request_confirmation_call(contract_id: str, p: Active, svc: Services):
    return ok(await svc.guardian_confirmations.request_call(p.account, contract_id))


# --- notifications (NT-01) -----------------------------------------------------------------------------
@router.get("/notifications")
async def notifications(p: Any_, svc: Services, cursor: str | None = None, limit: int = 25, unread_only: bool = False):
    page = await svc.notifications.list_for(p.id, cursor, limit, unread_only)
    items = [{"id": n.id, "type": n.type, "title": n.title, "body": n.body, "data": n.data,
              "read": n.read_at is not None, "created_at": n.created_at.isoformat()} for n in page.items]
    return {"success": True, "data": items, **page.meta(), "unread_count": await svc.notifications.unread_count(p.id)}


@router.post("/notifications/read")
async def mark_notifications_read(body: MarkReadIn, p: Any_, svc: Services):
    return ok({"marked": await svc.notifications.mark_read(p.id, body.notification_ids)})


@router.put("/notifications/preferences")
async def notification_preferences(body: NotificationPrefsIn, p: Active, svc: Services):
    return ok(await svc.notifications.set_preferences(p.id, body.preferences, body.daily_digest))


# --- blocks (MS-03) --------------------------------------------------------------------------------------
@router.get("/blocks")
async def my_blocks(p: Active, svc: Services):
    return ok(await svc.blocks.list_blocked(p.account))


@router.put("/blocks/{account_id}")
async def block(account_id: str, p: Active, svc: Services):
    await svc.blocks.block(p.account, account_id)
    return ok({"blocked": True})


@router.delete("/blocks/{account_id}")
async def unblock(account_id: str, p: Active, svc: Services):
    await svc.blocks.unblock(p.account, account_id)
    return ok({"blocked": False})


# --- verification (ON-05 to ON-08) ---------------------------------------------------------------------------
@router.post("/verification/identity/live-check")
async def start_live_check(p: Active, svc: Services):
    """ON-05 step 1. `provider: "youverify"`: run YouVerify's liveness SDK with the session_id, session_token and
    public_merchant_key given, then send the session_id with the ID. `provider: "camera"`: use Wamiwa's own camera
    check and send selfie_ref and liveness_ref."""
    return ok(await svc.identity.start_live_check(p.account))


@router.post("/verification/identity")
async def submit_identity(body: IdentityIn, p: Active, svc: Services):
    await svc.uploads.claim(p.account, [body.document_ref], {"id_document"})
    await svc.uploads.claim(p.account, [body.selfie_ref, body.liveness_ref], {"liveness"})  # taken live, never a saved photo
    return ok(await svc.identity.submit(p.account, body.document_type, body.document_ref, body.selfie_ref,
                                        body.liveness_ref, id_number=body.id_number,
                                        liveness_session_id=body.liveness_session_id))


@router.get("/verification/identity")
async def identity_status(p: Any_, svc: Services):
    return ok(await svc.identity.status(p.account))


@router.put("/verification/bank-account")
async def save_bank_account(body: BankIn, p: Active, svc: Services):
    return ok(await svc.bank_accounts.save(p.account, body.bank_code, body.account_number, body.account_name,
                                           body.totp_code, body.details))


@router.get("/verification/bank-account")
async def get_bank_account(p: Active, svc: Services):
    return ok(await svc.bank_accounts.get(p.account))


@router.post("/verification/social")
async def connect_social(body: SocialIn, p: Active, svc: Services):
    return ok(await svc.creators.connect(p.account, body.platform, body.auth_code))


@router.get("/verification/social")
async def social_accounts(p: Active, svc: Services):
    return ok(await svc.creators.summary(p.account))


@router.post("/verification/public-figure")
async def request_public_figure(body: PublicFigureIn, p: Active, svc: Services):
    await svc.uploads.claim(p.account, body.evidence, {"document", "image", "video"}, allow_links=True)
    return ok(await svc.popularity.request_major_person(p.account, body.method, body.evidence))


@router.post("/verification/popularity/signals", status_code=201)
async def submit_popularity_signal(body: SignalIn, p: Active, svc: Services):
    await svc.uploads.claim(p.account, [body.evidence_ref], {"document", "image"}, allow_links=True)
    return ok(await svc.popularity.submit_signal(p.account, body.category, body.title, body.evidence_ref))


@router.get("/verification/popularity/signals")
async def my_popularity_signals(p: Active, svc: Services):
    return ok(await svc.popularity.my_signals(p.account))


@router.post("/verification/popularity/signals/{signal_id}/appeal")
async def appeal_popularity_signal(signal_id: str, p: Active, svc: Services):
    return ok(await svc.popularity.appeal(p.account, signal_id))


@router.get("/friends/facebook")
async def facebook_friends(p: Active, svc: Services):
    return ok(await svc.friends.facebook_friends(p.account))


@router.post("/friends/contacts/match")
async def match_contacts(body: ContactsIn, p: Active, svc: Services):
    return ok(await svc.friends.match_contacts(p.account, body.phones))


@router.post("/friends/contacts/invite")
async def invite_contact(body: PhoneIn, p: Active, svc: Services):
    return ok(await svc.friends.invite(p.account, body.phone))


@router.get("/analytics/me")
async def my_analytics(p: Active, svc: Services, days: int = Query(default=30, ge=1, le=365)):
    return ok(await svc.analytics.creator(p.account, days))


@router.get("/post-ideas/today")
async def post_ideas(p: Active, svc: Services):
    return ok(await svc.post_ideas.today(p.account))


@router.put("/auto-reply")
async def configure_auto_reply(p: Active, svc: Services, enabled: bool, agent_id: str | None = None):
    return ok(await svc.agents.configure_auto_reply(p.account, agent_id, enabled))


@router.get("/follows/{direction}")
async def my_follows(direction: Literal["followers", "following"], p: Active, svc: Services,
                     cursor: str | None = None, limit: int = 25):
    return ok_page(await svc.follows.list_follows(p.id, direction, cursor, limit))
