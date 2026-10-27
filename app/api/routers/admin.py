"""Admin and Super Admin (Epics 9 and 13). Every mutating action is audit-logged with a reason."""
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, EmailStr, Field

from app.api.deps import Admin, Anonymous, Services, ok, ok_page
from app.core.enums import (
    AccountType, AdminRole, DisputeDecision, Permission, Queue, QueueDecision, TicketStatus,
)

router = APIRouter(prefix="/admin", tags=["admin"])
Reason = Field(min_length=3, max_length=2000)


class ReasonIn(BaseModel):
    reason: str = Reason


class ModerateIn(ReasonIn):
    notify: bool = True


class InviteIn(ReasonIn):
    email: EmailStr
    role: AdminRole = AdminRole.ADMIN


class AcceptInviteIn(BaseModel):
    token: str
    password: str = Field(min_length=12, max_length=128)
    handle: str = Field(min_length=3, max_length=30)
    name: str = Field(min_length=1, max_length=120)


class ConfirmTwoFactorIn(BaseModel):
    setup_token: str
    code: str = Field(min_length=6, max_length=6)


class GrantIn(ReasonIn):
    permission: Permission
    payout_limit: int | None = Field(default=None, gt=0, description="Local minor units; payouts only")


class QueueDecisionIn(ReasonIn):
    decision: QueueDecision
    payload: dict = Field(default_factory=dict, description="e.g. {'suspend': true} or {'false_report': true}")


class DisputeDecisionIn(ReasonIn):
    decision: DisputeDecision
    release_pct: int | None = Field(default=None, gt=0, lt=100)


class ManualPayoutIn(ReasonIn):
    earner_id: str


class CategoryMapIn(ReasonIn):
    seller_category: str = Field(min_length=2, max_length=64)
    buyer_categories: list[str] = Field(max_length=50)


class StaffReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    resolve: bool = False


# --- AD-01 admin management --------------------------------------------------------------------------
@router.post("/invites", status_code=201)
async def invite_admin(body: InviteIn, p: Admin, svc: Services):
    return ok(await svc.admin.invite(p.account, body.email, body.role, body.reason))


@router.post("/invites/accept", dependencies=[Anonymous])
async def accept_admin_invite(body: AcceptInviteIn, svc: Services):
    return ok(await svc.admin.accept_invite(body.token, body.password, body.handle, body.name))


@router.post("/invites/confirm-2fa", dependencies=[Anonymous])
async def confirm_admin_two_factor(body: ConfirmTwoFactorIn, svc: Services):
    return ok(await svc.admin.confirm_invite_2fa(body.setup_token, body.code))


@router.get("/admins")
async def list_admins(p: Admin, svc: Services):
    return ok(await svc.admin.list_admins(p.account))


@router.delete("/admins/{admin_id}")
async def remove_admin(admin_id: str, body: ReasonIn, p: Admin, svc: Services):
    return ok(await svc.admin.remove_admin(p.account, admin_id, body.reason))


@router.put("/admins/{admin_id}/permissions")
async def grant_permission(admin_id: str, body: GrantIn, p: Admin, svc: Services):
    return ok(await svc.admin.grant(p.account, admin_id, body.permission, body.payout_limit, body.reason))


@router.delete("/admins/{admin_id}/permissions/{permission}")
async def revoke_permission(admin_id: str, permission: Permission, body: ReasonIn, p: Admin, svc: Services):
    await svc.admin.revoke(p.account, admin_id, permission, body.reason)
    return ok()


@router.get("/me/permissions")
async def my_permissions(p: Admin, svc: Services):
    return ok(await svc.admin.permissions(p.account))


# --- oversight and AD-02 suspend / remove ---------------------------------------------------------------
@router.get("/accounts")
async def list_accounts(p: Admin, svc: Services, type: AccountType | None = None, status: str | None = None,
                        q: str | None = Query(default=None, max_length=100), cursor: str | None = None,
                        limit: int = Query(default=50, ge=1, le=100)):
    return ok_page(await svc.admin.accounts(type=type, status=status, q=q, cursor=cursor, limit=limit))


@router.get("/accounts/count")
async def total_user_count(p: Admin, svc: Services):
    return ok(await svc.admin.total_user_count())


@router.get("/accounts/{account_id}")
async def account_detail(account_id: str, p: Admin, svc: Services):
    return ok(await svc.admin.account_detail(account_id))


@router.post("/accounts/{account_id}/suspend")
async def suspend_account(account_id: str, body: ModerateIn, p: Admin, svc: Services):
    return ok(await svc.trust.suspend(p.account, account_id, body.reason, body.notify))


@router.post("/accounts/{account_id}/reinstate")
async def reinstate_account(account_id: str, body: ReasonIn, p: Admin, svc: Services):
    return ok(await svc.trust.reinstate(p.account, account_id, body.reason))


@router.post("/accounts/{account_id}/remove")
async def remove_account(account_id: str, body: ModerateIn, p: Admin, svc: Services):
    return ok(await svc.trust.remove(p.account, account_id, body.reason, body.notify))


@router.get("/agents/content-stats")
async def agent_content_stats(p: Admin, svc: Services):
    return ok(await svc.admin.agent_content_stats())


@router.get("/content/{target_type}/{target_id}/edits")
async def content_edit_history(target_type: Literal["post", "comment"], target_id: str, p: Admin, svc: Services):
    """FD-08: what a post or comment said before each edit, oldest first."""
    return ok(await svc.posts.edit_history(target_type, target_id))


@router.get("/minor-contracts")
async def minor_contracts(p: Admin, svc: Services, cursor: str | None = None, limit: int = 50):
    return ok_page(await svc.admin.minor_contracts(cursor, limit))


@router.get("/conversations/{conversation_id}")
async def view_conversation(conversation_id: str, p: Admin, svc: Services):
    return ok(await svc.messaging.admin_view(conversation_id))


@router.get("/watermarks/{watermark_id}")
async def trace_watermark(watermark_id: str, p: Admin, svc: Services):
    return ok(await svc.videos.trace_watermark(watermark_id))


@router.post("/popularity-signals/{signal_id}/revoke")
async def revoke_popularity_signal(signal_id: str, body: ReasonIn, p: Admin, svc: Services):
    return ok({"level": await svc.popularity.revoke_signal(p.account, signal_id, body.reason)})


# --- Epic 13 review queues -----------------------------------------------------------------------------------
@router.get("/queues/health")
async def queue_health(p: Admin, svc: Services):
    svc.admin.require_super(p.account)
    return ok(await svc.queues.health())


@router.get("/queues/{queue}")
async def queue_items(queue: Queue, p: Admin, svc: Services, open_only: bool = True, mine: bool = False,
                      cursor: str | None = None, limit: int = Query(default=50, ge=1, le=100)):
    page = await svc.queues.list_items(queue, open_only=open_only, mine=p.id if mine else None, cursor=cursor, limit=limit)
    now = svc.queues.now()
    items = [{"id": i.id, "queue": i.queue, "target_type": i.target_type, "target_id": i.target_id,
              "subject_account_id": i.subject_account_id, "summary": i.summary, "severity": i.severity,
              "claimed_by": i.claimed_by, "escalated": i.escalated, "decision": i.decision, "reason": i.reason,
              "due_at": i.due_at.isoformat(), "past_target": i.decision is None and i.due_at < now,
              "created_at": i.created_at.isoformat()} for i in page.items]
    return {"success": True, "data": items, **page.meta()}


@router.post("/queue-items/{item_id}/claim")
async def claim_item(item_id: str, p: Admin, svc: Services):
    item = await svc.queues.claim(p.account, item_id)
    context = await svc.admin.account_detail(item.subject_account_id) if item.subject_account_id else None
    return ok({"id": item.id, "claimed_by": item.claimed_by, "target_type": item.target_type,
               "target_id": item.target_id, "context": context})


@router.post("/queue-items/{item_id}/release")
async def release_item(item_id: str, p: Admin, svc: Services):
    item = await svc.queues.release(p.account, item_id)
    return ok({"id": item.id, "claimed_by": item.claimed_by})


@router.post("/queue-items/{item_id}/decide")
async def decide_item(item_id: str, body: QueueDecisionIn, p: Admin, svc: Services):
    item = await svc.queues.decide(p.account, item_id, body.decision, body.reason, body.payload)
    return ok({"id": item.id, "decision": item.decision, "escalated": item.escalated})


# --- disputes, payouts, audit ------------------------------------------------------------------------------------
@router.get("/disputes/{dispute_id}")
async def dispute_detail(dispute_id: str, p: Admin, svc: Services):
    return ok(await svc.disputes.view(p.account, dispute_id))


@router.post("/disputes/{dispute_id}/decide")
async def decide_dispute(dispute_id: str, body: DisputeDecisionIn, p: Admin, svc: Services):
    return ok(await svc.disputes.decide(p.account, dispute_id, body.decision, body.release_pct, body.reason))


@router.post("/disputes/{dispute_id}/decide-appeal")
async def decide_dispute_appeal(dispute_id: str, body: DisputeDecisionIn, p: Admin, svc: Services):
    return ok(await svc.disputes.decide_appeal(p.account, dispute_id, body.decision, body.release_pct, body.reason))


@router.post("/payouts/manual", status_code=201)
async def manual_payout(body: ManualPayoutIn, p: Admin, svc: Services):
    return ok(await svc.payouts.request_manual(p.account, body.earner_id, body.reason))


@router.get("/audit-log")
async def audit_log(p: Admin, svc: Services, actor_id: str | None = None, action: str | None = None,
                    target_id: str | None = None, cursor: str | None = None,
                    limit: int = Query(default=50, ge=1, le=100)):
    page = await svc.audit.search(actor_id=actor_id, action=action, target_id=target_id, cursor=cursor, limit=limit)
    items = [{"id": e.id, "actor_id": e.actor_id, "action": e.action, "target_type": e.target_type,
              "target_id": e.target_id, "reason": e.reason, "details": e.details, "at": e.created_at.isoformat()}
             for e in page.items]
    return {"success": True, "data": items, **page.meta()}


# --- B2B category map (CO-03) and support ---------------------------------------------------------------------------
@router.get("/b2b/category-map")
async def category_map(p: Admin, svc: Services):
    return ok(await svc.b2b.category_map())


@router.put("/b2b/category-map")
async def set_category_map(body: CategoryMapIn, p: Admin, svc: Services):
    return ok(await svc.b2b.set_buyers(p.account, body.seller_category, body.buyer_categories, body.reason))


@router.get("/support/tickets")
async def support_tickets(p: Admin, svc: Services, status: TicketStatus | None = None, cursor: str | None = None,
                          limit: int = 50):
    return ok_page(await svc.support.staff_list(status, cursor, limit))


@router.get("/support/tickets/{ticket_id}")
async def support_ticket(ticket_id: str, p: Admin, svc: Services):
    return ok(await svc.support.get(p.account, ticket_id))


@router.post("/support/tickets/{ticket_id}/replies")
async def reply_as_staff(ticket_id: str, body: StaffReplyIn, p: Admin, svc: Services):
    return ok(await svc.support.staff_reply(p.account, ticket_id, body.body, body.resolve))


# --- AD-05 platform analytics ----------------------------------------------------------------------------------------
def _range(svc: Services, start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    end = end or svc.queues.now()
    return start or end - timedelta(days=30), end


@router.get("/dashboard")
async def dashboard(p: Admin, svc: Services, start: datetime | None = None, end: datetime | None = None,
                    role: AccountType | None = None, niche: str | None = None, region: str | None = None,
                    compare: bool = True):
    start, end = _range(svc, start, end)
    return ok(await svc.admin_analytics.dashboard(p.account, start=start, end=end, role=role, niche=niche,
                                                  region=region, compare=compare))


@router.get("/dashboard/export.csv", response_class=PlainTextResponse)
async def dashboard_csv(p: Admin, svc: Services, start: datetime | None = None, end: datetime | None = None,
                        role: AccountType | None = None, niche: str | None = None, region: str | None = None):
    start, end = _range(svc, start, end)
    body = await svc.admin_analytics.export_csv(p.account, start=start, end=end, role=role, niche=niche, region=region)
    return PlainTextResponse(body, media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=wamiwa-dashboard.csv"})


# --- going live: test payment keys until now, live ones from now on ----------------------------------------------
class GoLiveIn(BaseModel):
    totp_code: str = Field(min_length=6, max_length=8)
    confirm: str = Field(max_length=16, description='Type "GO LIVE"')


@router.get("/platform")
async def platform(p: Admin, svc: Services):
    svc.admin.require_super(p.account)
    return ok({**await svc.launch.status(), "test_money_left": await svc.launch.test_money()})


@router.post("/platform/go-live")
async def go_live(body: GoLiveIn, p: Admin, svc: Services):
    """Switch payments to the live keys for good. Super Admin, fresh two-factor code, and "GO LIVE" typed out.
    Refused while any test payment is still recorded (run the reset first)."""
    return ok(await svc.launch.go_live(p.account, body.totp_code, body.confirm))
