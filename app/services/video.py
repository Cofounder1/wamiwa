"""LK-06 video generation and approval, LK-10 production pricing and plans, AV-01 personal AI video."""
import math
from datetime import timedelta
from typing import Any

from app.core.enums import (
    ContractStatus, ContractType, NotificationType, PaymentPurpose, RejectionReason, TicketCategory,
    VideoDecision, VideoKind, VideoPlan, VideoStatus, VideoTier,
)
from app.core.exceptions import Conflict, Forbidden, NotFound, PaymentRequired, Unprocessable
from app.infra.providers.base import VideoRequest
from app.models import (
    Account, BrandVideoPlan, Contract, CreatorVideoPlan, GeneratedVideo, LikenessConsent, Subscription, VideoProduction,
)
from app.services.base import BaseService

PLAN_PERIOD_DAYS = 30


class VideoPlanService(BaseService):
    def _terms(self, plan: VideoPlan) -> tuple[int, int]:
        return {VideoPlan.GROWTH: (self.s.plan_growth_cents, self.s.plan_growth_videos),
                VideoPlan.SCALE: (self.s.plan_scale_cents, self.s.plan_scale_videos)}[plan]

    def price_list(self) -> dict[str, Any]:
        """LK-10 price table. Charged in local currency at the day's rate, locked for the contract."""
        return {
            "pay_per_video_cents": self.s.video_price_cents, "included_seconds": self.s.video_default_seconds,
            "included": {"draft_previews": self.s.video_drafts_included, "final_renders": self.s.video_finals_included},
            "extra_final_render_cents": self.s.extra_final_render_cents,
            "extra_minute_cents": self.s.extra_minute_cents,
            "plans": {"growth": {"price_cents": self.s.plan_growth_cents, "videos": self.s.plan_growth_videos},
                      "scale": {"price_cents": self.s.plan_scale_cents, "videos": self.s.plan_scale_videos}},
            "arcon_filing": None if self.s.arcon_handling_fee_cents is None else {
                "arcon_fee_cents": self.s.arcon_fee_cents, "handling_fee_cents": self.s.arcon_handling_fee_cents},
        }

    def present(self, plan: BrandVideoPlan | None) -> dict[str, Any]:
        if plan is None or not plan.active:
            return {"active": False, "videos_left": 0, "rollover_left": 0}
        return {"active": True, "plan": plan.plan, "videos_left": plan.videos_left, "rollover_left": plan.rollover_left,
                "renews_at": plan.renews_at.isoformat(), "cancel_at_period_end": plan.cancel_at_period_end}

    async def status(self, company: Account) -> dict[str, Any]:
        return self.present(await self.repo(BrandVideoPlan).first(BrandVideoPlan.company_account_id == company.id))

    async def subscribe(self, company: Account, plan: VideoPlan, payment_method: str) -> dict[str, Any]:
        await self.svc.access.require_verified_company(company, "buy a video plan")
        price, videos = self._terms(plan)
        existing = await self.repo(BrandVideoPlan).first(BrandVideoPlan.company_account_id == company.id)
        if existing and existing.active and not existing.cancel_at_period_end:
            raise Conflict("You already have an active plan", code="plan_active")
        payment = await self.svc.payment.charge(company, PaymentPurpose.VIDEO_PLAN, price, payment_method)
        payment_method = self.svc.payment.renewal_method(payment, payment_method)
        if existing is None:
            existing = await self.repo(BrandVideoPlan).add(BrandVideoPlan(
                company_account_id=company.id, plan=plan, renews_at=self.now(), payment_method=payment_method))
        existing.plan, existing.videos_left, existing.active = plan, videos, True
        existing.renews_at = self.now() + timedelta(days=PLAN_PERIOD_DAYS)
        existing.cancel_at_period_end, existing.payment_method = False, payment_method
        existing.amount_local = payment.amount_local
        return self.present(existing)

    async def cancel(self, company: Account) -> dict[str, Any]:
        """Cancellation takes effect at the end of the paid month."""
        plan = await self.repo(BrandVideoPlan).first(BrandVideoPlan.company_account_id == company.id)
        if plan is None or not plan.active:
            raise NotFound("You have no active plan")
        plan.cancel_at_period_end = True
        return self.present(plan)

    async def consume(self, company_id: str) -> bool:
        plan = await self.repo(BrandVideoPlan).first(BrandVideoPlan.company_account_id == company_id)
        if plan is None or not plan.active:
            return False
        plan = await self.repo(BrandVideoPlan).lock(plan.id)
        if plan.rollover_left > 0:  # rolled-over videos expire first, so use them first
            plan.rollover_left -= 1
        elif plan.videos_left > 0:
            plan.videos_left -= 1
        else:
            return False
        return True

    async def restore(self, company_id: str) -> None:
        plan = await self.repo(BrandVideoPlan).first(BrandVideoPlan.company_account_id == company_id)
        if plan and plan.active:
            plan.videos_left += 1

    async def renew_due(self) -> int:
        """Job: plans renew monthly. Unused videos roll over for one month, then expire."""
        due = await self.repo(BrandVideoPlan).list(BrandVideoPlan.active.is_(True), BrandVideoPlan.renews_at <= self.now())
        for plan in due:
            company = await self.repo(Account).require(plan.company_account_id)
            price, videos = self._terms(VideoPlan(plan.plan))
            if plan.cancel_at_period_end:
                plan.active, plan.videos_left, plan.rollover_left = False, 0, 0
                continue
            try:
                await self.svc.payment.charge(company, PaymentPurpose.VIDEO_PLAN, price, plan.payment_method)
            except PaymentRequired:
                plan.active, plan.videos_left, plan.rollover_left = False, 0, 0
                await self.svc.notifications.notify(company.id, NotificationType.PAYMENT, "Your video plan was not renewed",
                                                    "The payment failed. Subscribe again to keep your plan.")
                continue
            plan.rollover_left, plan.videos_left = plan.videos_left, videos
            plan.renews_at = plan.renews_at + timedelta(days=PLAN_PERIOD_DAYS)
        return len(due)

    async def request_arcon_filing(self, company: Account, contract_id: str, payment_method: str) -> dict[str, Any]:
        """Optional: Wamiwa files the advert for ARCON approval; ARCON's fee at cost plus a flat handling fee."""
        if self.s.arcon_handling_fee_cents is None:
            raise Unprocessable("The ARCON filing service is not available yet", code="service_unavailable")
        contract = await self.svc.contracts.require_party(company, contract_id, role="hirer")
        payment = await self.svc.payment.charge(
            company, PaymentPurpose.ARCON_FILING, self.s.arcon_fee_cents + self.s.arcon_handling_fee_cents,
            payment_method, reference_id=contract.id)
        ticket = await self.svc.support.create(
            company, category=TicketCategory.CONTRACTS, subject="ARCON filing request",
            body="Please file this advert for ARCON approval.", attachments=[],
            linked_object={"type": "contract", "id": contract.id}, minor_id=None)
        return {"payment_id": payment.id, "ticket_number": ticket["number"]}


class VideoGenerationService(BaseService):
    def present(self, video: GeneratedVideo) -> dict[str, Any]:
        return {
            "id": video.id, "contract_id": video.contract_id, "kind": video.kind, "tier": video.tier,
            "version": video.version, "status": video.status, "seconds": video.seconds,
            "file": video.file_ref if video.status == VideoStatus.READY else None,
            "watermark_id": video.watermark_id, "influencer_decision": video.influencer_decision,
            "rejection_reason": video.rejection_reason, "rejection_note": video.rejection_note,
            "decision_due_at": video.decision_due_at.isoformat() if video.decision_due_at else None,
            "ai_label": "AI likeness" if video.contract_id else "AI-generated",
            "created_at": video.created_at.isoformat(),
        }

    # --- LK-10 production allowance ---------------------------------------------------------------
    def production_price_cents(self, seconds: int) -> int:
        extra_minutes = max(0, math.ceil((seconds - self.s.video_default_seconds) / 60))
        return self.s.video_price_cents + extra_minutes * self.s.extra_minute_cents

    async def check_production(self, brand: Account, seconds: int, use_plan: bool) -> None:
        """Everything that could refuse the production, checked before any money is taken."""
        if seconds and (seconds < 5 or seconds > 600):
            raise Unprocessable("Video length must be between 5 seconds and 10 minutes", code="invalid_length")
        if use_plan:
            plan = await self.repo(BrandVideoPlan).first(BrandVideoPlan.company_account_id == brand.id)
            if plan is None or not plan.active or plan.videos_left + plan.rollover_left <= 0:
                raise Conflict("Your plan has no videos left", code="plan_exhausted")

    def production_charge_cents(self, seconds: int, use_plan: bool) -> int:
        """What the brand pays for production on top of the likeness price: a plan video covers the first
        60 seconds, so with a plan only the extra length is charged."""
        seconds = seconds or self.s.video_default_seconds
        extra_cents = self.production_price_cents(seconds) - self.s.video_price_cents
        return extra_cents if use_plan else self.s.video_price_cents + extra_cents

    async def open_production(self, contract: Contract, brand: Account, seconds: int, use_plan: bool) -> VideoProduction:
        """Video production is charged on top of the influencer's likeness price, in the same payment as the
        escrow (`production_charge_cents` was added to it); here that share is recorded and a plan video used."""
        seconds = seconds or self.s.video_default_seconds
        if seconds < 5 or seconds > 600:
            raise Unprocessable("Video length must be between 5 seconds and 10 minutes", code="invalid_length")
        by_plan = use_plan and await self.svc.video_plans.consume(brand.id)
        if use_plan and not by_plan:
            raise Conflict("Your plan has no videos left", code="plan_exhausted")
        charge_cents = self.production_charge_cents(seconds, use_plan)
        payment = None
        if charge_cents > 0:
            escrow = await self.svc.escrow.for_contract(contract.id)
            payment = await self.svc.payment.record_share(brand, PaymentPurpose.VIDEO_PRODUCTION, charge_cents,
                                                          escrow, reference_id=contract.id)
        return await self.repo(VideoProduction).add(VideoProduction(
            contract_id=contract.id, seconds=seconds, drafts_left=self.s.video_drafts_included,
            finals_left=self.s.video_finals_included, funded_by_plan=by_plan,
            payment_id=payment.id if payment else None))

    async def refund_unused_production(self, contract: Contract) -> None:
        """An offer that dies before any render gets its production fee (or plan video) back."""
        production = await self.repo(VideoProduction).first(VideoProduction.contract_id == contract.id)
        if production is None or contract.video_generated:
            return
        if await self.repo(GeneratedVideo).exists(GeneratedVideo.contract_id == contract.id):
            return
        await self.svc.payment.refund(production.payment_id)
        if production.funded_by_plan:
            await self.svc.video_plans.restore(contract.hirer_id)
        production.drafts_left = production.finals_left = 0

    async def counter(self, contract_id: str) -> dict[str, Any] | None:
        production = await self.repo(VideoProduction).first(VideoProduction.contract_id == contract_id)
        if production is None:
            return None
        return {"drafts_left": production.drafts_left, "finals_left": production.finals_left,
                "free_rerenders": production.free_rerender_credit, "seconds": production.seconds,
                "display": f"{production.drafts_left} drafts, {production.finals_left} final render"
                           f"{'' if production.finals_left == 1 else 's'} left"}

    # --- LK-06 generate ----------------------------------------------------------------------------
    async def generate(self, brand: Account, contract_id: str, kind: VideoKind, payment_method: str | None) -> dict:
        contract = await self.svc.contracts.require_party(brand, contract_id, role="hirer")
        if contract.type != ContractType.LIKENESS or kind not in (VideoKind.DRAFT, VideoKind.FINAL):
            raise NotFound("Contract not found")
        if contract.status != ContractStatus.ACTIVE:
            raise Conflict("Videos can be generated once the contract is active", code="invalid_contract_status")
        script = await self.svc.likeness.signed_script(contract.id)  # only the exact script that was signed
        consent = await self.repo(LikenessConsent).first(LikenessConsent.contract_id == contract.id)
        await self.limiter.enforce(self.policies.video_generation, brand.id)
        production = await self.repo(VideoProduction).first(VideoProduction.contract_id == contract.id)
        production = await self.repo(VideoProduction).lock(production.id)

        payment = None
        if kind == VideoKind.DRAFT:
            if production.drafts_left > 0:
                production.drafts_left -= 1
            elif self.s.extra_draft_cents is not None:
                payment = await self._charge_extra(brand, self.s.extra_draft_cents, payment_method, contract.id)
            else:
                raise Conflict("No draft previews left. Request a final render.", code="no_drafts_left")
        elif production.free_rerender_credit > 0:
            production.free_rerender_credit -= 1  # free fix guarantee
        elif production.finals_left > 0:
            production.finals_left -= 1
        else:
            payment = await self._charge_extra(brand, self.s.extra_final_render_cents, payment_method, contract.id)

        profile = await self.svc.access.profile(contract.earner_id)
        voice = await self.svc.likeness_pages.voice_ref(contract.earner_id) if consent.includes_voice else None
        version = await self.repo(GeneratedVideo).count(GeneratedVideo.contract_id == contract.id) + 1
        video = await self.repo(GeneratedVideo).add(GeneratedVideo(
            contract_id=contract.id, owner_account_id=brand.id, kind=kind, version=version, script_text=script.text,
            seconds=production.seconds, payment_id=payment.id if payment else None))
        video.provider_job_id = await self.providers.video.submit(VideoRequest(
            reference=video.id, script=script.text, face_ref=profile.face_ref or "", voice_ref=voice,
            quality=kind, seconds=production.seconds))
        await self._refresh(video)
        return {**self.present(video), "counter": await self.counter(contract.id)}

    async def _charge_extra(self, brand: Account, cents: int, payment_method: str | None, contract_id: str):
        if not payment_method:
            raise PaymentRequired("This render is charged. Send a payment method.", code="payment_required",
                                  price_cents=cents)
        return await self.svc.payment.charge(brand, PaymentPurpose.EXTRA_RENDER, cents, payment_method,
                                             reference_id=contract_id)

    async def claim_defect(self, brand: Account, video_id: str, note: str) -> dict:
        """Free fix guarantee: a final render with a technical defect is re-rendered free."""
        video = await self.repo(GeneratedVideo).require(video_id, "Video not found")
        if video.owner_account_id != brand.id or video.kind != VideoKind.FINAL or video.status != VideoStatus.READY:
            raise NotFound("Video not found")
        if video.rejection_reason:
            raise Conflict("A re-render was already granted for this video", code="already_claimed")
        production = await self.repo(VideoProduction).first(VideoProduction.contract_id == video.contract_id)
        if production.defect_claims >= self.s.free_defect_claims_per_contract:
            raise Conflict("Contact support to review further defects on this contract", code="defect_claim_limit",
                           hint="POST /support/tickets")
        production.defect_claims += 1
        production.free_rerender_credit += 1
        video.influencer_decision, video.rejection_reason = VideoDecision.REJECTED, RejectionReason.TECHNICAL_DEFECT
        video.rejection_note, video.decision_due_at = note, None
        return {**self.present(video), "counter": await self.counter(video.contract_id)}

    async def _refresh(self, video: GeneratedVideo) -> GeneratedVideo:
        if video.status != VideoStatus.PROCESSING or not video.provider_job_id:
            return video
        job = await self.providers.video.fetch(video.provider_job_id)
        if job.status == "processing":
            return video
        if job.status == "failed":
            return await self._handle_failure(video, job.failure_reason or "Generation failed")
        if job.in_sync is False:
            # Lips or facial expression do not follow the script: never hand this render to anyone.
            # Re-render automatically, at no cost to the brand; give the allowance back if it keeps failing.
            if video.retries < self.s.video_sync_retries:
                video.retries += 1
                video.provider_job_id = await self.providers.video.submit(await self._request_for(video))
                return await self._refresh(video)
            return await self._handle_failure(video, "The render's lip movement and expression did not match the script")
        # Every video carries an invisible watermark that traces back to its contract or owner.
        mark = await self.providers.watermark.embed(job.file_ref, video.contract_id or video.owner_account_id)
        video.status, video.file_ref, video.watermark_id = VideoStatus.READY, mark.file_ref, mark.watermark_id
        if video.contract_id:
            contract = await self.repo(Contract).require(video.contract_id)
            contract.video_generated = True
            if video.kind == VideoKind.FINAL:
                video.influencer_decision = VideoDecision.PENDING
                video.decision_due_at = self.now() + timedelta(hours=self.s.video_approval_hours)
                await self.svc.notifications.notify(
                    contract.earner_id, NotificationType.CONTRACT, "A video is waiting for your approval",
                    f"Approve or reject it within {self.s.video_approval_hours} hours, or it is approved automatically.",
                    {"contract_id": contract.id, "video_id": video.id}, include_guardian=True)
        if video.slow_notice_due:
            await self.svc.notifications.notify(video.owner_account_id, NotificationType.CONTRACT,
                                                "Your video is ready", data={"video_id": video.id})
        return video

    async def _request_for(self, video: GeneratedVideo) -> VideoRequest:
        """Rebuild the provider request for a re-render of the same video."""
        if video.contract_id is None:
            profile = await self.svc.access.profile(video.owner_account_id)
            return VideoRequest(reference=video.id, script=video.script_text, face_ref=profile.face_ref or "",
                                voice_ref=None, quality=video.tier or VideoTier.STANDARD, seconds=video.seconds)
        contract = await self.repo(Contract).require(video.contract_id)
        consent = await self.repo(LikenessConsent).first(LikenessConsent.contract_id == contract.id)
        profile = await self.svc.access.profile(contract.earner_id)
        voice = await self.svc.likeness_pages.voice_ref(contract.earner_id) if consent and consent.includes_voice else None
        return VideoRequest(reference=video.id, script=video.script_text, face_ref=profile.face_ref or "",
                            voice_ref=voice, quality=video.kind, seconds=video.seconds)

    async def _handle_failure(self, video: GeneratedVideo, reason: str) -> GeneratedVideo:
        if video.kind == VideoKind.PERSONAL and video.retries < 1:
            # AV-01: generation failed after payment -> retry once, then refund automatically.
            video.retries += 1
            video.provider_job_id = await self.providers.video.submit(await self._request_for(video))
            return await self._refresh(video)
        video.status = VideoStatus.FAILED
        if video.kind == VideoKind.PERSONAL:
            if video.payment_id:
                await self.svc.payment.refund(video.payment_id)
            if video.plan_videos_used:  # AV-02: the plan's videos come back
                plan = await self.repo(CreatorVideoPlan).first(CreatorVideoPlan.account_id == video.owner_account_id)
                if plan and plan.active:
                    plan.videos_left += video.plan_videos_used
        else:
            production = await self.repo(VideoProduction).first(VideoProduction.contract_id == video.contract_id)
            if video.payment_id:
                await self.svc.payment.refund(video.payment_id)
            elif video.kind == VideoKind.DRAFT:
                production.drafts_left += 1
            else:
                production.finals_left += 1
        await self.svc.notifications.notify(
            video.owner_account_id, NotificationType.PAYMENT, "Video generation failed",
            f"{reason}. You have not been charged for it.", {"video_id": video.id})
        return video

    async def handle_provider_event(self, job_id: str) -> dict[str, Any]:
        video = await self.repo(GeneratedVideo).first(GeneratedVideo.provider_job_id == job_id)
        if video is None:
            raise NotFound("Unknown job")
        return self.present(await self._refresh(video))

    async def poll_processing(self) -> int:
        """Job: advance running jobs; anything over 10 minutes notifies the user when it finishes."""
        running = await self.repo(GeneratedVideo).list(GeneratedVideo.status == VideoStatus.PROCESSING)
        slow_after = timedelta(seconds=self.s.video_slow_job_seconds)
        for video in running:
            if self.now() - video.created_at >= slow_after:
                video.slow_notice_due = True
            await self._refresh(video)
        return len(running)

    async def get(self, account: Account, video_id: str) -> dict:
        video = await self.repo(GeneratedVideo).require(video_id, "Video not found")
        if video.contract_id:
            await self.svc.contracts.require_party(account, video.contract_id)
        elif video.owner_account_id != account.id:
            raise NotFound("Video not found")
        return self.present(await self._refresh(video))

    async def list_for_contract(self, account: Account, contract_id: str) -> dict:
        contract = await self.svc.contracts.require_party(account, contract_id)
        videos = await self.repo(GeneratedVideo).list(GeneratedVideo.contract_id == contract.id)
        if account.id != contract.hirer_id and not account.is_admin:
            videos = [v for v in videos if v.kind == VideoKind.FINAL]  # drafts are the brand's working copies
        return {"videos": [self.present(v) for v in videos], "counter": await self.counter(contract.id)}

    # --- influencer approval ---------------------------------------------------------------------
    async def decide(self, influencer: Account, video_id: str, approve: bool, reason: RejectionReason | None,
                     note: str) -> dict:
        video = await self.repo(GeneratedVideo).require(video_id, "Video not found")
        contract = await self.repo(Contract).get(video.contract_id) if video.contract_id else None
        if contract is None or contract.earner_id != influencer.id or video.kind != VideoKind.FINAL:
            raise NotFound("Video not found")
        if video.influencer_decision != VideoDecision.PENDING:
            raise Conflict(f"This video is already {video.influencer_decision}", code="already_decided")
        if approve:
            video.influencer_decision = VideoDecision.APPROVED
        else:
            if reason is None:
                raise Unprocessable("Say why: the video differs from the signed script, or it has a technical defect",
                                    code="reason_required")
            video.influencer_decision, video.rejection_reason, video.rejection_note = VideoDecision.REJECTED, reason, note
            production = await self.repo(VideoProduction).first(VideoProduction.contract_id == contract.id)
            production.free_rerender_credit += 1  # a mismatch or defect re-render does not count against the brand
        video.decision_due_at = None
        await self.svc.notifications.notify(
            contract.hirer_id, NotificationType.CONTRACT,
            "Your video was approved" if approve else "Your video was rejected",
            note if not approve else "You can post it now.", {"contract_id": contract.id, "video_id": video.id})
        return self.present(video)

    async def auto_approve(self) -> dict[str, int]:
        """Job: warn the influencer before the 72 hours are up, then approve automatically."""
        pending = await self.repo(GeneratedVideo).list(GeneratedVideo.influencer_decision == VideoDecision.PENDING)
        warn_before = timedelta(hours=self.s.video_approval_hours - self.s.video_approval_warning_hours)
        warned = approved = 0
        for video in pending:
            contract = await self.repo(Contract).require(video.contract_id)
            if video.decision_due_at <= self.now():
                video.influencer_decision, video.decision_due_at = VideoDecision.AUTO_APPROVED, None
                for party in (contract.hirer_id, contract.earner_id):
                    await self.svc.notifications.notify(
                        party, NotificationType.CONTRACT, "Video approved automatically",
                        "No response was given within 72 hours.", {"video_id": video.id},
                        include_guardian=party == contract.earner_id)
                approved += 1
            elif not video.warning_sent and video.decision_due_at - self.now() <= warn_before:
                video.warning_sent = True
                await self.svc.notifications.notify(
                    contract.earner_id, NotificationType.CONTRACT, "A video will be approved automatically soon",
                    "Approve or reject it before the 72-hour window closes.", {"video_id": video.id},
                    include_guardian=True)
                warned += 1
        return {"warned": warned, "approved": approved}

    # --- AV-01 personal AI video -------------------------------------------------------------------
    def _rate(self, tier: VideoTier) -> int:
        return self.s.ai_video_premium_cents_per_minute if tier == VideoTier.PREMIUM else self.s.ai_video_standard_cents_per_minute

    def plan_videos_for(self, seconds: int) -> int:
        """AV-02: each started 2 minutes is one of the plan's videos (a 5-minute video uses 3)."""
        return math.ceil(seconds / self.s.ai_video_plan_seconds_per_video)

    async def personal_quote(self, account: Account, seconds: int, tier: VideoTier) -> dict[str, Any]:
        """Standard quality comes from the monthly plan when it has enough videos left; otherwise the subscriber's
        free minute, then pay per minute. Premium is always pay per minute."""
        base = {"tier": tier, "seconds": seconds, "rate_cents_per_minute": self._rate(tier), "plan_videos_applied": 0,
                "free_seconds_applied": 0}
        if tier == VideoTier.STANDARD:
            plan = await self._active_plan(account.id)
            needed = self.plan_videos_for(seconds)
            if plan and plan.videos_left >= needed:
                return {**base, "plan_videos_applied": needed, "plan_videos_left_after": plan.videos_left - needed,
                        "price_cents": 0}
        free = await self._free_seconds_available(account) if tier == VideoTier.STANDARD else 0
        billable = max(0, seconds - free)
        return {**base, "free_seconds_applied": seconds - billable,
                "price_cents": math.ceil(self._rate(tier) * billable / 60)}

    # --- AV-02 monthly AI video plan ---------------------------------------------------------------
    async def _active_plan(self, account_id: str) -> CreatorVideoPlan | None:
        plan = await self.repo(CreatorVideoPlan).first(CreatorVideoPlan.account_id == account_id)
        return plan if plan and plan.active and plan.renews_at > self.now() else None

    def _present_plan(self, plan: CreatorVideoPlan | None) -> dict[str, Any]:
        terms = {"price_cents": self.s.ai_video_plan_cents, "videos": self.s.ai_video_plan_videos,
                 "minutes_per_video": self.s.ai_video_plan_seconds_per_video // 60, "quality": "standard"}
        if plan is None or not plan.active:
            return {"active": False, "terms": terms}
        return {"active": True, "terms": terms, "videos_left": plan.videos_left,
                "renews_at": plan.renews_at.isoformat(), "cancel_at_period_end": plan.cancel_at_period_end}

    async def personal_plan(self, account: Account) -> dict[str, Any]:
        return self._present_plan(await self._active_plan(account.id))

    async def subscribe_personal_plan(self, account: Account, payment_method: str) -> dict[str, Any]:
        await self.svc.access.require_adult(account, "buy an AI video plan")  # AV-01: not for minors
        plan = await self.repo(CreatorVideoPlan).first(CreatorVideoPlan.account_id == account.id)
        if plan and plan.active and plan.renews_at > self.now():
            if plan.cancel_at_period_end:  # changed their mind: keep it renewing
                plan.cancel_at_period_end = False
                return self._present_plan(plan)
            raise Conflict("You already have the AI video plan", code="already_subscribed")
        payment = await self.svc.payment.charge(account, PaymentPurpose.AI_VIDEO_PLAN, self.s.ai_video_plan_cents, payment_method)
        payment_method = self.svc.payment.renewal_method(payment, payment_method)
        if plan is None:
            plan = await self.repo(CreatorVideoPlan).add(CreatorVideoPlan(
                account_id=account.id, renews_at=self.now(), payment_method=payment_method))
        plan.active, plan.cancel_at_period_end, plan.payment_method = True, False, payment_method
        plan.videos_left = self.s.ai_video_plan_videos
        plan.renews_at = self.now() + timedelta(days=PLAN_PERIOD_DAYS)
        return self._present_plan(plan)

    async def cancel_personal_plan(self, account: Account) -> dict[str, Any]:
        plan = await self._active_plan(account.id)
        if plan is None:
            raise NotFound("You have no AI video plan")
        plan.cancel_at_period_end = True  # the month already paid for stays usable
        return self._present_plan(plan)

    async def renew_personal_plans(self) -> int:
        """Job: monthly renewal. The month's videos reset to 15; unused ones don't carry over."""
        due = await self.repo(CreatorVideoPlan).list(CreatorVideoPlan.active.is_(True), CreatorVideoPlan.renews_at <= self.now())
        for plan in due:
            account = await self.repo(Account).require(plan.account_id)
            if plan.cancel_at_period_end or not account.is_active:
                plan.active, plan.videos_left = False, 0
                continue
            try:
                await self.svc.payment.charge(account, PaymentPurpose.AI_VIDEO_PLAN, self.s.ai_video_plan_cents,
                                              plan.payment_method)
            except PaymentRequired:
                plan.active, plan.videos_left = False, 0
                await self.svc.notifications.notify(account.id, NotificationType.PAYMENT,
                                                    "Your AI video plan was not renewed",
                                                    "The payment failed. Subscribe again to keep making videos on the plan.")
                continue
            plan.videos_left = self.s.ai_video_plan_videos
            plan.renews_at = plan.renews_at + timedelta(days=PLAN_PERIOD_DAYS)
        return len(due)

    async def _subscription(self, account_id: str) -> Subscription | None:
        subscription = await self.repo(Subscription).first(Subscription.account_id == account_id)
        return subscription if subscription and await self.svc.access.is_subscribed(account_id) else None

    async def _free_seconds_available(self, account: Account) -> int:
        """Subscribers get 1 standard minute free each month."""
        subscription = await self._subscription(account.id)
        if subscription is None:
            return 0
        month = self.clock.today().strftime("%Y-%m")
        used = subscription.free_seconds_used if subscription.free_seconds_month == month else 0
        return max(0, self.s.subscriber_free_standard_seconds - used)

    async def generate_personal(self, account: Account, script: str, seconds: int, tier: VideoTier,
                                payment_method: str | None) -> dict:
        profile = await self.svc.access.require_adult(account, "generate AI video")  # not for minors outside a contract
        if not profile.id_verified or not profile.face_ref:
            # Only your own face, matched to your verified identity; there is no way to supply another face.
            raise Forbidden("Verify your identity to generate video of your own face", code="id_verification_required")
        if not script.strip() or not 5 <= seconds <= 600:
            raise Unprocessable("Send a script and a length between 5 seconds and 10 minutes", code="invalid_request")
        moderation = await self.providers.moderation.screen_script(script)
        if moderation.flagged:
            raise Unprocessable("This script cannot be generated", code="moderation_blocked", reason=moderation.reason)
        await self.limiter.enforce(self.policies.video_generation, account.id)
        quote = await self.personal_quote(account, seconds, tier)
        payment = None
        if quote["price_cents"] > 0:
            if not payment_method:
                raise PaymentRequired("Payment is taken before generation", code="payment_required",
                                      price_cents=quote["price_cents"])
            payment = await self.svc.payment.charge(account, PaymentPurpose.AI_VIDEO, quote["price_cents"], payment_method)
        if quote["plan_videos_applied"]:
            plan = await self._active_plan(account.id)
            plan.videos_left -= quote["plan_videos_applied"]
        if quote["free_seconds_applied"]:
            subscription = await self._subscription(account.id)
            month = self.clock.today().strftime("%Y-%m")
            if subscription.free_seconds_month != month:
                subscription.free_seconds_month, subscription.free_seconds_used = month, 0
            subscription.free_seconds_used += quote["free_seconds_applied"]
        video = await self.repo(GeneratedVideo).add(GeneratedVideo(
            owner_account_id=account.id, kind=VideoKind.PERSONAL, tier=tier, script_text=script.strip(),
            seconds=seconds, payment_id=payment.id if payment else None,
            plan_videos_used=quote["plan_videos_applied"]))
        video.provider_job_id = await self.providers.video.submit(VideoRequest(
            reference=video.id, script=video.script_text, face_ref=profile.face_ref, voice_ref=None,
            quality=tier, seconds=seconds))
        await self._refresh(video)
        return {**self.present(video), "charged_cents": quote["price_cents"],
                "plan_videos_used": quote["plan_videos_applied"]}

    async def download(self, account: Account, video_id: str) -> dict[str, Any]:
        video = await self.repo(GeneratedVideo).require(video_id, "Video not found")
        if video.owner_account_id != account.id or video.kind != VideoKind.PERSONAL:
            raise NotFound("Video not found")
        if video.status != VideoStatus.READY:
            raise Conflict("This video is not ready yet", code="video_not_ready")
        return {"download": video.file_ref, "watermark_id": video.watermark_id, "watermarked": True}

    async def list_personal(self, account: Account) -> list[dict]:
        videos = await self.repo(GeneratedVideo).list(
            GeneratedVideo.owner_account_id == account.id, GeneratedVideo.kind == VideoKind.PERSONAL,
            order_by=GeneratedVideo.id.desc(), limit=100)
        return [self.present(v) for v in videos]

    async def trace_watermark(self, watermark_id: str) -> dict[str, Any]:
        """Admin tool: the watermark supports tracing a leaked video back to its source."""
        video = await self.repo(GeneratedVideo).first(GeneratedVideo.watermark_id == watermark_id)
        if video is None:
            raise NotFound("No video carries this watermark")
        return {"video_id": video.id, "contract_id": video.contract_id, "owner_account_id": video.owner_account_id,
                "kind": video.kind, "created_at": video.created_at.isoformat()}
