"""Epic 5: likeness pages, pricing, script flow, consent, term handling and guardian confirmation."""
import hashlib
import math
from datetime import timedelta
from typing import Any

from sqlalchemy import case, select

from app.core.enums import (
    AccountStatus, AssetKind, ChangeResponse, ContractStatus, ContractType, GuardianResult,
    ModerationStatus, NotificationType, PopularityLevel, PostType, Queue, ReportReason, ReportTarget,
    ScriptStatus, VideoDecision, VideoKind, VideoStatus,
)
from app.core.exceptions import Conflict, Forbidden, NotFound, Unprocessable
from app.core.security import SecretFactory
from app.core.utils import Cursor, FeeBreakdown, Page
from app.models import (
    Account, Contract, GeneratedVideo, GuardianConfirmation, LikenessAsset, LikenessConsent, LikenessPage,
    Post, QueueItem, Script, ScriptChangeRequest, UserProfile,
)
from app.services.base import BaseService, Principal

MIN_VOICE_CLIPS, VOICE_MIN_SECONDS, VOICE_MAX_SECONDS = 3, 30, 60


class LikenessPageService(BaseService):
    # --- eligibility and pricing (LK-01, LK-02) ---------------------------------------------
    async def eligibility(self, account: Account, profile: UserProfile) -> tuple[bool, str | None]:
        if not await self.svc.access.is_id_verified(account):
            return False, "ID and liveness verification is required"
        if profile.is_verified_creator or profile.is_major_person:
            return True, None
        followers = await self.svc.creators.effective_human_followers(account)
        if followers >= self.s.likeness_min_followers:
            return True, None
        return False, f"You need {self.s.likeness_min_followers}+ human followers (you have {followers})"

    async def price_ceiling(self, account: Account) -> int:
        """Follower-based ceiling for the image-only price, from human followers and human engagement.

        $20 to start, plus 1 cent per follower ($10 per 1,000), scaled by engagement between 0.75x
        (no engagement) and 1.5x (15%+ of human impressions liked or commented). No upper cap.
        """
        followers = await self.svc.creators.effective_human_followers(account)
        engagement = await self.svc.analytics.human_engagement_rate(account.id)
        multiplier = min(1.5, max(0.75, 0.75 + engagement * 5))
        price = int((20_00 + followers) * multiplier)  # $20 base + $10 per 1,000 followers
        return max(20_00, price // 100 * 100)

    async def pricing_guide(self, account: Account) -> dict[str, Any]:
        profile = await self.svc.access.profile(account.id)
        if profile is None:
            raise Unprocessable("Only user accounts have a likeness page", code="not_a_user")
        ceiling = await self.price_ceiling(account)
        if profile.is_major_person:
            factor = {PopularityLevel.MAJOR: 25, PopularityLevel.PROMINENT: 10}.get(profile.popularity_level, 5)
            return {"type": "verified_major_person", "names_own_price": True,
                    "recommended_range_cents": [ceiling * factor // 2, ceiling * factor]}
        return {"type": "follower_based", "names_own_price": False, "max_price_cents": ceiling,
                "note": "This limit is for image only. Image + voice and exclusivity are yours to set above it."}

    async def potential_rating(self, account: Account, profile: UserProfile) -> int:
        """Shown to brands for never-hired influencers: human followers, popularity card and posts (0-100)."""
        followers = await self.svc.creators.effective_human_followers(account)
        posts = await self.repo(Post).count(Post.author_account_id == account.id,
                                            Post.moderation_status == ModerationStatus.VISIBLE)
        follower_score = min(50.0, math.log10(followers + 1) * 10)
        card_score = {PopularityLevel.NOTABLE: 10, PopularityLevel.PROMINENT: 20, PopularityLevel.MAJOR: 30}.get(
            profile.popularity_level, 0)
        return int(round(follower_score + card_score + min(20.0, posts * 0.5)))

    async def subject(self, actor: Account, minor_id: str | None) -> tuple[Account, UserProfile]:
        """Whose likeness is being managed. A minor's page is managed through the guardian's account (LK-09)."""
        if minor_id:
            await self.svc.access.require_guardian_of(actor.id, minor_id)
            account = await self.repo(Account).require(minor_id)
        else:
            account = actor
        profile = await self.svc.access.profile(account.id)
        if profile is None:
            raise Unprocessable("Only user accounts have a likeness page", code="not_a_user")
        if profile.is_minor and not minor_id:
            raise Forbidden("Your guardian manages your likeness page", code="guardian_manages_likeness")
        return account, profile

    def present(self, account: Account, profile: UserProfile, page: LikenessPage, photos: list[str],
                show_price: bool = True) -> dict[str, Any]:
        return {
            "account_id": account.id, "handle": account.handle, "name": account.display_name,
            "niche": profile.primary_niche, "photos": photos, "voice_available": page.voice_enabled,
            "price_image_cents": page.price_image_cents if show_price else None,
            "price_image_voice_cents": page.price_voice_cents if show_price and page.voice_enabled else None,
            "exclusivity_price_cents": page.exclusivity_price_cents if show_price else None,
            "past_hires": page.past_hires, "paused": page.paused,
            "badges": [b for b, on in (("verified_creator", profile.is_verified_creator),
                                       ("verified_major_person", profile.is_major_person)) if on],
        }

    async def _photos(self, account_id: str) -> list[str]:
        assets = await self.repo(LikenessAsset).list(
            LikenessAsset.account_id == account_id, LikenessAsset.kind == AssetKind.PHOTO,
            LikenessAsset.deleted_at.is_(None))
        return [a.file_ref for a in assets]

    async def save(self, actor: Account, minor_id: str | None, *, price_image_cents: int,
                   price_voice_cents: int | None, exclusivity_price_cents: int | None) -> dict[str, Any]:
        account, profile = await self.subject(actor, minor_id)
        eligible, reason = await self.eligibility(account, profile)
        if not eligible:
            raise Forbidden(reason or "Not eligible", code="likeness_not_eligible")
        if not profile.is_major_person and await self.providers.identity.is_protected_identity(account.display_name):
            raise Forbidden("Celebrity names are blocked unless that person verifies the account themselves",
                            code="protected_identity", hint="POST /verification/public-figure")
        if price_image_cents <= 0:
            raise Unprocessable("Set a price above zero", code="invalid_price")
        if price_voice_cents is not None and price_voice_cents <= price_image_cents:
            raise Unprocessable("The image + voice price must be higher than image only", code="invalid_price")
        base = max(price_image_cents, price_voice_cents or 0)
        if exclusivity_price_cents is not None and exclusivity_price_cents <= base:
            raise Unprocessable("The exclusivity price must be higher than your other prices", code="invalid_price")
        if not profile.is_major_person:
            # The follower-based ceiling applies to the image-only price. Voice and exclusivity are
            # charged on top at whatever the influencer chooses.
            ceiling = await self.price_ceiling(account)
            if price_image_cents > ceiling:
                raise Unprocessable(f"Your image-only likeness price can be at most ${ceiling / 100:,.0f} per month",
                                    code="price_above_ceiling", max_price_cents=ceiling)
        page = await self.repo(LikenessPage).first(LikenessPage.account_id == account.id)
        if page is None:
            page = await self.repo(LikenessPage).add(LikenessPage(account_id=account.id, price_image_cents=price_image_cents))
        # LK-02: price changes apply only to new contracts (contracts keep their own locked price).
        page.price_image_cents, page.price_voice_cents = price_image_cents, price_voice_cents
        page.exclusivity_price_cents, page.paused = exclusivity_price_cents, False
        await self.svc.profiles.invalidate(account)
        return self.present(account, profile, page, await self._photos(account.id))

    async def add_photo(self, actor: Account, minor_id: str | None, file_ref: str,
                        liveness_selfie_ref: str | None) -> dict[str, Any]:
        account, profile = await self.subject(actor, minor_id)
        if profile.is_minor:
            # LK-09: uploaded through the guardian's account, with a liveness check of the minor.
            if not liveness_selfie_ref or "spoof" in liveness_selfie_ref.lower():
                raise Unprocessable("A liveness check of the minor is required", code="liveness_required")
            profile.face_ref = profile.face_ref or f"face:{liveness_selfie_ref}"
        if not profile.face_ref:
            raise Forbidden("Verify your identity first", code="id_verification_required")
        if not await self.providers.identity.face_matches(profile.face_ref, file_ref):
            raise Unprocessable("This photo does not match the verified face", code="face_mismatch")
        asset = await self.repo(LikenessAsset).add(LikenessAsset(
            account_id=account.id, kind=AssetKind.PHOTO, file_ref=file_ref, face_match=True, uploaded_by=actor.id))
        return {"asset_id": asset.id, "kind": asset.kind, "file": asset.file_ref}

    async def add_voice(self, actor: Account, minor_id: str | None, file_ref: str, duration_seconds: int,
                        recorded_in_app: bool, voice_consent: bool) -> dict[str, Any]:
        account, _ = await self.subject(actor, minor_id)
        page = await self.repo(LikenessPage).first(LikenessPage.account_id == account.id)
        if page is None:
            raise NotFound("Set up the likeness page first")
        if not recorded_in_app:
            raise Unprocessable("Voice samples must be recorded in-app while reading the on-screen prompt",
                                code="in_app_recording_required")
        if not VOICE_MIN_SECONDS <= duration_seconds <= VOICE_MAX_SECONDS:
            raise Unprocessable("Each clip must be 30-60 seconds", code="invalid_clip_length")
        if not voice_consent and page.voice_consent_at is None:
            raise Unprocessable("Tick the separate voice-consent box to add voice samples", code="voice_consent_required")
        page.voice_consent_at = page.voice_consent_at or self.now()
        asset = await self.repo(LikenessAsset).add(LikenessAsset(
            account_id=account.id, kind=AssetKind.VOICE, file_ref=file_ref, duration_seconds=duration_seconds,
            consent_at=page.voice_consent_at, uploaded_by=actor.id))
        clips = await self._voice_count(account.id)
        page.voice_enabled = clips >= MIN_VOICE_CLIPS
        return {"asset_id": asset.id, "kind": asset.kind, "clips": clips, "voice_enabled": page.voice_enabled,
                "clips_needed": max(0, MIN_VOICE_CLIPS - clips)}

    async def _voice_count(self, account_id: str) -> int:
        return await self.repo(LikenessAsset).count(
            LikenessAsset.account_id == account_id, LikenessAsset.kind == AssetKind.VOICE,
            LikenessAsset.deleted_at.is_(None))

    async def delete_asset(self, actor: Account, minor_id: str | None, asset_id: str) -> dict[str, Any]:
        """Deleting voice samples stops new voice use but not signed contracts."""
        account, _ = await self.subject(actor, minor_id)
        asset = await self.repo(LikenessAsset).require(asset_id, "Asset not found")
        if asset.account_id != account.id:
            raise NotFound("Asset not found")
        asset.deleted_at = asset.deleted_at or self.now()
        page = await self.repo(LikenessPage).first(LikenessPage.account_id == account.id)
        if page and asset.kind == AssetKind.VOICE:
            page.voice_enabled = await self._voice_count(account.id) >= MIN_VOICE_CLIPS
        return {"deleted": True, "voice_enabled": bool(page and page.voice_enabled)}

    async def assets(self, actor: Account, minor_id: str | None) -> list[dict[str, Any]]:
        account, _ = await self.subject(actor, minor_id)
        rows = await self.repo(LikenessAsset).list(LikenessAsset.account_id == account.id, LikenessAsset.deleted_at.is_(None))
        return [{"asset_id": a.id, "kind": a.kind, "file": a.file_ref, "duration_seconds": a.duration_seconds} for a in rows]

    async def voice_ref(self, account_id: str) -> str | None:
        clip = await self.repo(LikenessAsset).first(
            LikenessAsset.account_id == account_id, LikenessAsset.kind == AssetKind.VOICE,
            LikenessAsset.deleted_at.is_(None))
        return clip.file_ref if clip else None

    async def refresh_eligibility(self, account: Account) -> None:
        """LK-02: when followers drop below 500 the listing pauses; running contracts finish."""
        page = await self.repo(LikenessPage).first(LikenessPage.account_id == account.id)
        profile = await self.svc.access.profile(account.id)
        if page is None or profile is None:
            return
        eligible, _ = await self.eligibility(account, profile)
        if page.paused == eligible:
            page.paused = not eligible
            await self.svc.profiles.invalidate(account)

    async def record_hire(self, account_id: str) -> None:
        page = await self.repo(LikenessPage).first(LikenessPage.account_id == account_id)
        if page:
            page.past_hires += 1

    # --- LK-03 browsing ------------------------------------------------------------------------
    async def _decorate(self, brand: Account, account: Account, profile: UserProfile, page: LikenessPage) -> dict:
        company = await self.svc.access.company(brand.id)
        show_price = not profile.is_minor or await self.svc.access.company_approved_for_minor(account.id, brand.id)
        item = self.present(account, profile, page, await self._photos(account.id), show_price)
        warnings = []
        if company and company.category and profile.primary_niche and company.category != profile.primary_niche:
            warnings.append(f"This influencer's niche is {profile.primary_niche}, which differs from yours.")
        if company and company.category and await self.repo(Contract).exists(
                Contract.type == ContractType.LIKENESS, Contract.earner_id == account.id,
                Contract.hirer_id != brand.id, Contract.hirer_category == company.category,
                Contract.status.in_([ContractStatus.ACTIVE, ContractStatus.PENDING_GUARDIAN])):
            warnings.append("A direct competitor already holds a hire on this likeness.")
        item["warnings"] = warnings
        if page.past_hires == 0:
            item["potential_rating"] = await self.potential_rating(account, profile)
        return item

    async def browse(self, brand: Account, niche: str | None, cursor: str | None, limit: int) -> Page[dict]:
        limit, offset = max(1, min(limit, 50)), int(Cursor.decode(cursor) or 0)
        company = await self.svc.access.company(brand.id)
        region = (company.region_targeting if company else None) or brand.region
        stmt = (select(Account, UserProfile, LikenessPage)
                .join(UserProfile, UserProfile.account_id == Account.id)
                .join(LikenessPage, LikenessPage.account_id == Account.id)
                .where(Account.status == AccountStatus.ACTIVE, LikenessPage.paused.is_(False)))
        if niche:
            stmt = stmt.where(UserProfile.primary_niche == niche.lower())
        hidden = await self.svc.blocks.hidden_ids(brand.id)
        if hidden:
            stmt = stmt.where(Account.id.not_in(hidden))
        order = [Account.id.desc()]
        if region:
            order.insert(0, case((Account.region == region, 0), else_=1))  # CO-04: first, never hidden
        rows = (await self.db.execute(stmt.order_by(*order).offset(offset).limit(limit + 1))).all()
        items = [await self._decorate(brand, a, p, page) for a, p, page in rows[:limit]]
        return Page(items=items, next_cursor=Cursor.encode(offset + limit) if len(rows) > limit else None)

    async def load(self, viewer: Account, influencer_id: str) -> tuple[Account, UserProfile, LikenessPage]:
        account = await self.repo(Account).get(influencer_id)
        page = await self.repo(LikenessPage).first(LikenessPage.account_id == influencer_id)
        if account is None or page is None or account.status != AccountStatus.ACTIVE \
                or await self.svc.blocks.either(viewer.id, influencer_id):
            raise NotFound("Likeness page not found")
        return account, await self.svc.access.profile(account.id), page

    async def view(self, brand: Account, influencer_id: str) -> dict[str, Any]:
        account, profile, page = await self.load(brand, influencer_id)
        company = await self.svc.access.company(brand.id)
        if company and await self.kv.set_if_absent(f"lkview:{brand.id}:{account.id}", "1", 24 * 3600):
            # The company's name stays hidden until an offer is sent.
            await self.svc.notifications.notify(
                account.id, NotificationType.PROFILE_VIEW,
                f"A company in {company.category or 'an unlisted niche'} viewed your likeness page")
        return await self._decorate(brand, account, profile, page)


class LikenessContractService(BaseService):
    async def _own(self, account: Account, contract_id: str, role: str | None, *statuses: ContractStatus) -> Contract:
        contract = await self.svc.contracts.require_party(account, contract_id, role=role)
        if contract.type != ContractType.LIKENESS:
            raise NotFound("Contract not found")
        if statuses and contract.status not in statuses:
            raise Conflict(f"This contract is {contract.status}", code="invalid_contract_status")
        return contract

    async def current_script(self, contract_id: str) -> Script:
        script = await self.repo(Script).first(Script.contract_id == contract_id, order_by=Script.version.desc())
        if script is None:
            raise NotFound("Script not found")
        return script

    async def signed_script(self, contract_id: str) -> Script:
        consent = await self.repo(LikenessConsent).first(LikenessConsent.contract_id == contract_id)
        if consent is None or consent.withdrawn_at is not None:
            raise Forbidden("The influencer has not consented to a script for this contract", code="no_consent")
        return await self.repo(Script).first(Script.contract_id == contract_id, Script.version == consent.script_version)

    async def _add_script(self, contract: Contract, text: str, always_review: bool) -> Script:
        """LK-04: screen every version. Flagged scripts, and all scripts for minors, go to admin review
        before the influencer sees them."""
        text = text.strip()
        if not text:
            raise Unprocessable("Write a script", code="script_required")
        screening = await self.providers.moderation.screen_script(text)
        previous = await self.repo(Script).list(Script.contract_id == contract.id)
        for old in previous:
            if old.status in (ScriptStatus.PENDING, ScriptStatus.ADMIN_REVIEW):
                old.status = ScriptStatus.SUPERSEDED
        needs_review = screening.flagged or always_review
        script = await self.repo(Script).add(Script(
            contract_id=contract.id, version=len(previous) + 1, text=text,
            screening_result={"flagged": screening.flagged, "categories": list(screening.categories)},
            status=ScriptStatus.ADMIN_REVIEW if needs_review else ScriptStatus.PENDING))
        if needs_review:
            contract.status, contract.offer_expires_at = ContractStatus.ADMIN_REVIEW, None
            await self.svc.queues.enqueue(
                Queue.SCRIPTS, "script", script.id, subject_account_id=contract.hirer_id,
                summary=screening.reason or "Script for a 13-17 account")
        else:
            await self._release_to_influencer(contract)
        return script

    async def _release_to_influencer(self, contract: Contract) -> None:
        contract.status = ContractStatus.OFFERED
        contract.offer_expires_at = self.now() + timedelta(days=self.s.offer_expiry_days)  # LK-05: 7 days to respond
        net = FeeBreakdown.of(contract.price_cents, contract.fee_pct).net
        await self.svc.notifications.notify(
            contract.earner_id, NotificationType.CONTRACT, "A script is waiting for your review",
            f"You would receive ${net / 100:,.2f} after Wamiwa's {contract.fee_pct}% fee. Respond within "
            f"{self.s.offer_expiry_days} days.", {"contract_id": contract.id}, include_guardian=True)

    # --- LK-04 send a script and pay ------------------------------------------------------------
    async def send_script(self, brand: Account, *, influencer_id: str, script: str, with_voice: bool,
                          exclusivity: bool, influencer_must_post: bool, takedown_days: int | None,
                          video_seconds: int, use_plan: bool, no_ai_replies: bool, payment_method: str) -> dict:
        company = await self.svc.access.require_verified_company(brand, "hire a likeness")
        account, profile, page = await self.svc.likeness_pages.load(brand, influencer_id)
        if page.paused or profile.new_orders_paused:
            raise Conflict("This likeness is not open to new hires right now", code="listing_paused")
        if profile.is_minor and self.svc.access.is_blocked_category_for_minors(company.category):
            raise Forbidden("Companies in this category cannot hire 13-17 accounts", code="blocked_category")
        if with_voice and not (page.voice_enabled and page.price_voice_cents):
            raise Unprocessable("This influencer has not enabled voice", code="voice_unavailable")
        if exclusivity and not page.exclusivity_price_cents:
            raise Unprocessable("This influencer does not offer exclusivity", code="exclusivity_unavailable")
        if takedown_days is not None and takedown_days < self.s.likeness_takedown_days:
            raise Unprocessable(f"Videos stay up at least {self.s.likeness_takedown_days} days", code="invalid_takedown")
        price = page.price_voice_cents if with_voice else page.price_image_cents
        if exclusivity:
            # The exclusivity price is quoted against image-only; keep its premium when voice is added.
            price += page.exclusivity_price_cents - page.price_image_cents
        contract = await self.svc.contracts.build(
            type=ContractType.LIKENESS, hirer=brand, earner_id=account.id, price_cents=price,
            payment_method=payment_method, title=f"Likeness hire by {company.business_name}", with_voice=with_voice,
            exclusivity=exclusivity, influencer_must_post=influencer_must_post, takedown_days=takedown_days,
            no_ai_replies=no_ai_replies, requires_guardian=profile.is_minor)
        await self.svc.videos.open_production(contract, brand, video_seconds, use_plan, payment_method)
        await self._add_script(contract, script, always_review=profile.is_minor)
        return await self.detail(brand, contract.id)

    async def apply_script_review(self, item: QueueItem, approved: bool, admin: Account, reason: str, payload: dict) -> None:
        script = await self.repo(Script).require(item.target_id)
        contract = await self.repo(Contract).require(script.contract_id)
        if contract.status != ContractStatus.ADMIN_REVIEW or script.status != ScriptStatus.ADMIN_REVIEW:
            return
        if approved:
            script.status = ScriptStatus.PENDING
            await self._release_to_influencer(contract)
        else:
            script.status = ScriptStatus.REJECTED
            await self.svc.contracts.close_with_refund(contract, ContractStatus.DECLINED, admin.id,
                                                       f"Script rejected in review: {reason}")

    async def detail(self, account: Account, contract_id: str) -> dict:
        contract = await self._own(account, contract_id, None)
        data = await self.svc.contracts.detail(account, contract_id)
        scripts = await self.repo(Script).list(Script.contract_id == contract.id, order_by=Script.version)
        # The influencer (and guardian) never see a script that is still with an admin.
        hide_unreviewed = account.id != contract.hirer_id and not account.is_admin
        data["scripts"] = [{"version": s.version, "text": s.text, "status": s.status,
                            "approved_at": s.approved_at.isoformat() if s.approved_at else None}
                           for s in scripts if not (hide_unreviewed and s.status in (ScriptStatus.ADMIN_REVIEW,
                                                                                     ScriptStatus.REJECTED))]
        requests = await self.repo(ScriptChangeRequest).list(ScriptChangeRequest.contract_id == contract.id)
        data["change_requests"] = [{"round": r.round, "request": r.request_text, "response": r.response,
                                    "resulting_version": r.resulting_version} for r in requests]
        data["change_rounds_left"] = max(0, self.s.script_change_rounds - contract.change_rounds)
        data["production"] = await self.svc.videos.counter(contract.id)
        return data

    # --- LK-05 review, modify or accept ------------------------------------------------------------
    async def accept(self, influencer: Account, contract_id: str, *, consent_image: bool, consent_voice: bool,
                     accept_responsibility: bool, signer_ip: str | None) -> dict:
        contract = await self._own(influencer, contract_id, "earner", ContractStatus.OFFERED)
        script = await self.current_script(contract.id)
        if script.status != ScriptStatus.PENDING:
            raise Conflict("There is no script waiting for your approval", code="no_pending_script")
        if not consent_image or (contract.with_voice and not consent_voice):
            raise Unprocessable("Sign consent for your image" + (" and voice" if contract.with_voice else ""),
                                code="consent_required")
        if not accept_responsibility:
            raise Unprocessable("Confirm that you are responsible for the script you approve",
                                code="responsibility_required")
        script.status, script.approved_at = ScriptStatus.APPROVED, self.now()
        await self.repo(LikenessConsent).add(LikenessConsent(
            contract_id=contract.id, signed_by=influencer.id, script_version=script.version,
            script_sha256=hashlib.sha256(script.text.encode()).hexdigest(),
            includes_voice=contract.with_voice and consent_voice, accepted_responsibility=True,
            signed_at=self.now(), signer_ip=signer_ip))
        if contract.requires_guardian:
            contract.status = ContractStatus.PENDING_GUARDIAN
            await self.svc.guardian_confirmations.open(contract)
        else:
            await self.svc.contracts.activate(contract, influencer.id)
        # In-app notification and an email to the brand.
        await self.svc.notifications.notify(
            contract.hirer_id, NotificationType.CONTRACT, "Your script was accepted",
            "Continue to the generation screen." if contract.status == ContractStatus.ACTIVE
            else "Waiting for the guardian's approval and confirmation call.",
            {"contract_id": contract.id, "next": f"/contracts/{contract.id}/videos"})
        return await self.detail(influencer, contract.id)

    async def reject(self, influencer: Account, contract_id: str, reason: str) -> dict:
        contract = await self._own(influencer, contract_id, "earner", ContractStatus.OFFERED,
                                   ContractStatus.CHANGES_REQUESTED)
        await self.svc.contracts.close_with_refund(contract, ContractStatus.DECLINED, influencer.id,
                                                   reason or "Script rejected by influencer")
        return self.svc.contracts.present(contract)

    async def request_changes(self, influencer: Account, contract_id: str, request_text: str) -> dict:
        contract = await self._own(influencer, contract_id, "earner", ContractStatus.OFFERED)
        if contract.change_rounds >= self.s.script_change_rounds:
            raise Conflict(f"You have used all {self.s.script_change_rounds} change rounds. Accept or reject the script.",
                           code="change_rounds_exhausted")
        contract.change_rounds += 1
        contract.status = ContractStatus.CHANGES_REQUESTED
        contract.offer_expires_at = self.now() + timedelta(days=self.s.offer_expiry_days)
        await self.repo(ScriptChangeRequest).add(ScriptChangeRequest(
            contract_id=contract.id, round=contract.change_rounds, request_text=request_text.strip()))
        await self.svc.notifications.notify(
            contract.hirer_id, NotificationType.CONTRACT, "The influencer asked for script changes",
            request_text, {"contract_id": contract.id, "round": contract.change_rounds})
        return await self.detail(influencer, contract.id)

    async def respond_to_changes(self, brand: Account, contract_id: str, action: ChangeResponse,
                                 new_script: str | None) -> dict:
        """The brand can accept, modify or reject a change request. After the last round it may only
        accept the requested version or withdraw for a full refund."""
        contract = await self._own(brand, contract_id, "hirer", ContractStatus.CHANGES_REQUESTED)
        request = await self.repo(ScriptChangeRequest).first(
            ScriptChangeRequest.contract_id == contract.id, ScriptChangeRequest.response == ChangeResponse.PENDING)
        if request is None:
            raise Conflict("There is no change request to answer", code="no_change_request")
        final_round = contract.change_rounds >= self.s.script_change_rounds
        if final_round and action != ChangeResponse.ACCEPTED:
            raise Conflict("This was the last change round: accept the requested version or withdraw",
                           code="final_round", hint="POST /likeness/contracts/{id}/withdraw")
        request.response = action
        if action == ChangeResponse.REJECTED:
            await self._release_to_influencer(contract)
        else:
            if not new_script or not new_script.strip():
                raise Unprocessable("Send the updated script", code="script_required")
            minor = await self.svc.access.is_minor(contract.earner_id)
            script = await self._add_script(contract, new_script, always_review=minor)
            request.resulting_version = script.version
        return await self.detail(brand, contract.id)

    async def withdraw(self, brand: Account, contract_id: str) -> dict:
        contract = await self._own(brand, contract_id, "hirer", ContractStatus.OFFERED, ContractStatus.CHANGES_REQUESTED,
                                   ContractStatus.ADMIN_REVIEW, ContractStatus.PENDING_GUARDIAN)
        await self.svc.contracts.close_with_refund(contract, ContractStatus.CANCELLED, brand.id, "Offer withdrawn by brand")
        return self.svc.contracts.present(contract)

    # --- LK-07 post the video ---------------------------------------------------------------------
    async def post_video(self, actor: Account, contract_id: str, video_id: str, caption: str) -> dict:
        """Brand or influencer posts the approved final cut, labeled Sponsored + AI likeness."""
        contract = await self._own(actor, contract_id, None, ContractStatus.ACTIVE)
        if actor.id not in (contract.hirer_id, contract.earner_id):
            raise NotFound("Contract not found")
        video = await self.repo(GeneratedVideo).require(video_id, "Video not found")
        if video.contract_id != contract.id or video.kind != VideoKind.FINAL or video.status != VideoStatus.READY:
            raise NotFound("Video not found")
        if video.influencer_decision not in (VideoDecision.APPROVED, VideoDecision.AUTO_APPROVED):
            raise Forbidden("The influencer has not approved this video yet", code="video_not_approved")
        post = await self.svc.posts.create(
            Principal(account=actor), type=PostType.VIDEO, text=caption, media_ref=video.file_ref,
            duration_seconds=video.seconds, likeness_contract_id=contract.id)
        if actor.id == contract.hirer_id:
            video.posted_post_id = post.id
        return self.svc.posts.present(post, actor)

    # --- LK-08 term, disengagement and takedown ----------------------------------------------------
    def _unused_pct(self, contract: Contract) -> float:
        total = (contract.end - contract.start).total_seconds()
        unused = max(0.0, (contract.end - self.now()).total_seconds())
        return round(unused / total * 100, 4) if total else 0.0

    async def _schedule_takedown(self, contract: Contract, immediately: bool = False) -> None:
        if immediately:
            await self.svc.posts.take_down_contract_posts(contract.id)
            return
        due = self.now() + timedelta(days=contract.takedown_days or self.s.likeness_takedown_days)
        for post in await self.repo(Post).list(Post.sponsored_contract_id == contract.id):
            post.takedown_due_at = due

    async def _end_early(self, contract: Contract, actor_id: str | None, reason: str, immediate_takedown: bool) -> None:
        refund_pct = self._unused_pct(contract)
        if contract.video_generated:
            # 25% is non-refundable once a video has been generated.
            refund_pct = min(refund_pct, 100 - self.s.likeness_non_refundable_pct)
        await self.svc.contracts.settle_and_close(contract, 100 - refund_pct, ContractStatus.CANCELLED, actor_id, reason)
        await self._schedule_takedown(contract, immediate_takedown)

    async def disengage(self, brand: Account, contract_id: str) -> dict:
        contract = await self._own(brand, contract_id, "hirer", ContractStatus.ACTIVE)
        await self._end_early(contract, brand.id, "Brand disengaged early: unused days refunded pro rata", False)
        return self.svc.contracts.present(contract)

    async def guardian_end(self, guardian: Account, contract_id: str, reason: str) -> dict:
        """LK-09: the guardian can end a likeness contract at any time if content harms the minor."""
        contract = await self.repo(Contract).get(contract_id)
        if contract is None or contract.type != ContractType.LIKENESS:
            raise NotFound("Contract not found")
        await self.svc.access.require_guardian_of(guardian.id, contract.earner_id)
        if contract.status != ContractStatus.ACTIVE:
            raise Conflict(f"This contract is {contract.status}", code="invalid_contract_status")
        await self._end_early(contract, guardian.id, f"Ended by guardian: {reason}", True)
        return self.svc.contracts.present(contract)

    async def end_for_account_removal(self, account_id: str, actor_id: str | None) -> int:
        """LK-08: when the influencer's account goes, active contracts end with pro-rata refunds."""
        active = await self.repo(Contract).list(
            Contract.type == ContractType.LIKENESS, Contract.earner_id == account_id,
            Contract.status == ContractStatus.ACTIVE)
        for contract in active:
            await self._end_early(contract, actor_id, "Influencer account closed", True)
        return len(active)

    async def report_misuse(self, influencer: Account, contract_id: str, details: str) -> dict:
        """The influencer may withdraw consent mid-contract only for misuse (content outside the signed
        script). Wamiwa then has 24 hours to remove the content on-platform."""
        contract = await self.svc.contracts.require_party(influencer, contract_id)
        if contract.type != ContractType.LIKENESS or contract.status not in (
                ContractStatus.ACTIVE, ContractStatus.DELIVERED, ContractStatus.COMPLETED, ContractStatus.CANCELLED):
            raise NotFound("Contract not found")
        consent = await self.repo(LikenessConsent).first(LikenessConsent.contract_id == contract.id)
        if consent and consent.withdrawn_at is None:
            consent.withdrawn_at = self.now()
        report = await self.svc.reports.create(
            influencer, ReportTarget.SCRIPT, contract.id, ReportReason.MISUSE_OF_LIKENESS, details,
            target_account_id=contract.hirer_id, due_at=self.now() + timedelta(hours=self.s.misuse_takedown_hours))
        return {"report_id": report["id"], "consent_withdrawn": True,
                "takedown_due_by": (self.now() + timedelta(hours=self.s.misuse_takedown_hours)).isoformat()}

    async def end_terms(self) -> int:
        """Job: at the end of the month the brand must stop new use; the review window then opens and
        posted videos are scheduled to come down."""
        ended = await self.repo(Contract).list(
            Contract.type == ContractType.LIKENESS, Contract.status == ContractStatus.ACTIVE,
            Contract.end.is_not(None), Contract.end <= self.now())
        for contract in ended:
            await self.svc.contracts.mark_delivered(contract, None)
            await self._schedule_takedown(contract)
            await self.svc.notifications.notify(
                contract.hirer_id, NotificationType.CONTRACT, "Your likeness term has ended",
                "Stop new use of this likeness. Posted videos will come down as agreed.", {"contract_id": contract.id})
        return len(ended)


class GuardianConfirmationService(BaseService):
    """LK-09: the guardian approves twice, in the app and then on a recorded confirmation call."""

    def present(self, c: GuardianConfirmation) -> dict[str, Any]:
        return {
            "contract_id": c.contract_id, "minor_id": c.minor_id, "result": c.result,
            "app_approved_at": c.app_approved_at.isoformat() if c.app_approved_at else None,
            "call_attempts": c.call_attempts, "max_call_attempts": self.s.guardian_call_attempts,
            "next_call_at": c.next_call_at.isoformat() if c.next_call_at else None,
            "expires_at": c.expires_at.isoformat(), "recorded": c.recording_ref is not None,
        }

    async def open(self, contract: Contract) -> GuardianConfirmation:
        guardian = await self.svc.access.guardian_of(contract.earner_id)
        if guardian is None:
            raise Conflict("This account has no linked guardian", code="no_guardian")
        confirmation = await self.repo(GuardianConfirmation).add(GuardianConfirmation(
            contract_id=contract.id, guardian_id=guardian.id, minor_id=contract.earner_id,
            expires_at=self.now() + timedelta(days=self.s.guardian_confirmation_days)))
        contract.offer_expires_at = None
        await self.svc.notifications.notify(
            guardian.id, NotificationType.CONTRACT, "A contract needs your approval",
            "Review the full contract and script, then approve it in the app.", {"contract_id": contract.id})
        return confirmation

    async def _get(self, guardian: Account, contract_id: str) -> GuardianConfirmation:
        confirmation = await self.repo(GuardianConfirmation).first(GuardianConfirmation.contract_id == contract_id)
        if confirmation is None or confirmation.guardian_id != guardian.id:
            raise NotFound("Contract not found")
        return confirmation

    async def pending(self, guardian: Account) -> list[dict[str, Any]]:
        rows = await self.repo(GuardianConfirmation).list(
            GuardianConfirmation.guardian_id == guardian.id,
            GuardianConfirmation.result.in_([GuardianResult.AWAITING_APP_APPROVAL, GuardianResult.AWAITING_CALL]))
        return [self.present(r) for r in rows]

    async def status(self, guardian: Account, contract_id: str) -> dict[str, Any]:
        return self.present(await self._get(guardian, contract_id))

    async def app_approve(self, guardian: Account, contract_id: str) -> dict[str, Any]:
        confirmation = await self._get(guardian, contract_id)
        if confirmation.result != GuardianResult.AWAITING_APP_APPROVAL:
            raise Conflict(f"This approval is {confirmation.result}", code="invalid_confirmation_status")
        if not guardian.phone or not guardian.phone_verified_at:
            raise Unprocessable("Verify your phone number to receive the confirmation call", code="phone_required",
                                hint="POST /accounts/me/phone")
        confirmation.app_approved_at, confirmation.result = self.now(), GuardianResult.AWAITING_CALL
        await self._place_call(confirmation, guardian)
        return self.present(confirmation)

    async def _place_call(self, confirmation: GuardianConfirmation, guardian: Account) -> None:
        contract = await self.repo(Contract).require(confirmation.contract_id)
        brand = await self.svc.access.company(contract.hirer_id)
        code = SecretFactory.otp(self.s.otp_length)
        confirmation.otp_hash = self.infra.hasher.digest(code)
        confirmation.call_attempts += 1
        more = confirmation.call_attempts < self.s.guardian_call_attempts
        confirmation.next_call_at = self.now() + timedelta(hours=self.s.guardian_call_retry_hours) if more else None
        length = f"{self.s.likeness_term_days} days" if contract.type == ContractType.LIKENESS else (
            f"until {contract.deadline.date().isoformat()}" if contract.deadline else "as agreed")
        script = (f"This is Wamiwa. {brand.business_name if brand else 'A company'} wants to hire your child for "
                  f"{contract.price_cents / 100:,.2f} US dollars. Contract length: {length}. Enter the one-time code "
                  "we sent to this number, then press 1 to approve or 2 to decline.")
        phone, reference = guardian.phone, confirmation.id

        async def call() -> None:
            await self.providers.sms.send(phone, f"Wamiwa confirmation code: {code}")
            await self.providers.voice.place_confirmation_call(to=phone, reference=reference, script=script)

        self.uow.on_commit(call)

    async def handle_call_result(self, reference: str, *, answered: bool, otp: str | None, digit: str | None,
                                 recording_ref: str | None, call_id: str | None) -> dict[str, Any]:
        """Voice webhook: the call is recorded and linked to the contract; only then is it active."""
        confirmation = await self.repo(GuardianConfirmation).get(reference)
        if confirmation is None:
            raise NotFound("Unknown call reference")
        if confirmation.result != GuardianResult.AWAITING_CALL:
            return self.present(confirmation)
        confirmation.last_call_id = call_id
        if recording_ref:
            confirmation.recording_ref = recording_ref
        if not answered or not otp or not self.infra.hasher.matches(otp, confirmation.otp_hash):
            return self.present(confirmation)  # missed or unverified: the retry schedule continues
        confirmation.otp_hash, confirmation.next_call_at = None, None
        if digit == "1":
            await self._approve(confirmation)
        elif digit == "2":
            await self._decline(confirmation, "Declined by guardian on the confirmation call")
        return self.present(confirmation)

    async def _approve(self, confirmation: GuardianConfirmation) -> None:
        confirmation.result, confirmation.decided_at = GuardianResult.APPROVED, self.now()
        contract = await self.repo(Contract).require(confirmation.contract_id)
        if contract.status == ContractStatus.PENDING_GUARDIAN:
            await self.svc.contracts.activate(contract, confirmation.guardian_id)
            await self.svc.notifications.notify(contract.earner_id, NotificationType.CONTRACT,
                                                "Your guardian approved the contract", data={"contract_id": contract.id})

    async def _decline(self, confirmation: GuardianConfirmation, reason: str, result=GuardianResult.DECLINED) -> None:
        confirmation.result, confirmation.decided_at, confirmation.next_call_at = result, self.now(), None
        contract = await self.repo(Contract).require(confirmation.contract_id)
        if contract.status == ContractStatus.PENDING_GUARDIAN:
            status = ContractStatus.EXPIRED if result == GuardianResult.EXPIRED else ContractStatus.DECLINED
            await self.svc.contracts.close_with_refund(contract, status, confirmation.guardian_id, reason)

    async def decline(self, guardian: Account, contract_id: str) -> dict[str, Any]:
        confirmation = await self._get(guardian, contract_id)
        if confirmation.result not in (GuardianResult.AWAITING_APP_APPROVAL, GuardianResult.AWAITING_CALL):
            raise Conflict(f"This approval is {confirmation.result}", code="invalid_confirmation_status")
        await self._decline(confirmation, "Declined by guardian")
        return self.present(confirmation)

    async def request_call(self, guardian: Account, contract_id: str) -> dict[str, Any]:
        """SP-02: after missed calls the guardian can ask for another confirmation call."""
        confirmation = await self._get(guardian, contract_id)
        if confirmation.result != GuardianResult.AWAITING_CALL:
            raise Conflict("Approve the contract in the app first", code="invalid_confirmation_status")
        if not await self.kv.set_if_absent(f"gcall:{confirmation.id}", "1", 15 * 60):
            raise Conflict("A call was placed recently. Try again in a few minutes.", code="call_recently_placed")
        await self._place_call(confirmation, guardian)
        return self.present(confirmation)

    async def retry_calls(self) -> int:
        """Job: a missed call gets 2 more attempts over 48 hours."""
        due = await self.repo(GuardianConfirmation).list(
            GuardianConfirmation.result == GuardianResult.AWAITING_CALL,
            GuardianConfirmation.next_call_at.is_not(None), GuardianConfirmation.next_call_at <= self.now(),
            GuardianConfirmation.call_attempts < self.s.guardian_call_attempts)
        for confirmation in due:
            guardian = await self.repo(Account).require(confirmation.guardian_id)
            await self._place_call(confirmation, guardian)
        return len(due)

    async def expire(self) -> int:
        """Job: no confirmation within 7 days -> the offer expires and escrow is refunded."""
        stale = await self.repo(GuardianConfirmation).list(
            GuardianConfirmation.result.in_([GuardianResult.AWAITING_APP_APPROVAL, GuardianResult.AWAITING_CALL]),
            GuardianConfirmation.expires_at <= self.now())
        for confirmation in stale:
            await self._decline(confirmation, "No guardian confirmation within 7 days", GuardianResult.EXPIRED)
        return len(stale)

    async def minor_contracts(self, guardian: Account, minor_id: str, cursor: str | None, limit: int) -> Page[dict]:
        await self.svc.access.require_guardian_of(guardian.id, minor_id)
        return await self.svc.contracts.list_for(minor_id, role="earner", type=None, status=None, cursor=cursor, limit=limit)
