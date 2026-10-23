"""Shared contract lifecycle, Hire Me (Epic 4) and agent orders (AG-04)."""
import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, select

from app.core.enums import (
    OPEN_CONTRACT_STATUSES, AccountStatus, ContractStatus, ContractType, ConversationType, EventType,
    IntegrationStatus, NotificationType,
)
from app.core.exceptions import Conflict, Forbidden, NotFound, Unprocessable
from app.core.utils import Cursor, FeeBreakdown, Page, convert_minor
from app.models import (
    Account, AgentIntegration, AgentProfile, Contract, EscrowTransaction, HireMePage, UserProfile,
)
from app.services.base import BaseService


EQUITY_NOTICE = ("Equity is agreed directly between you and the company under the attached agreement. Wamiwa "
                 "doesn't hold, value, transfer or guarantee it. Escrow and disputes cover only the cash, and "
                 "shares in private companies are often worth nothing.")


class ContractService(BaseService):
    """Rules every hire type shares: fees, FX lock, activation, completion, expiry and auto-clear."""

    def fee_pct(self, type: ContractType) -> int:
        return {ContractType.HIRE_ME: self.s.fee_pct_hire_me, ContractType.LIKENESS: self.s.fee_pct_likeness,
                ContractType.AGENT: self.s.fee_pct_agent}[type]

    async def quote(self, type: ContractType, price_cents: int) -> dict[str, Any]:
        """PY-01: both sides see the price, fee and net amount before paying or accepting."""
        usd = FeeBreakdown.of(price_cents, self.fee_pct(type))
        local_price, rate = await self.svc.fx.to_local(price_cents)
        local = FeeBreakdown.of(local_price, usd.fee_pct)
        return {"usd_cents": usd.to_dict(), "local": {**local.to_dict(), "currency": self.s.local_currency},
                "fx_rate": str(rate), "note": "The fee comes out of the price; nothing is added on top."}

    def present(self, contract: Contract, escrow: EscrowTransaction | None = None) -> dict[str, Any]:
        usd = FeeBreakdown.of(contract.price_cents, contract.fee_pct)
        local = FeeBreakdown.of(convert_minor(contract.price_cents, contract.fx_rate), contract.fee_pct)

        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        data = {
            "id": contract.id, "type": contract.type, "status": contract.status, "hirer_id": contract.hirer_id,
            "earner_id": contract.earner_id, "agent_id": contract.agent_account_id, "title": contract.title,
            "deliverables": contract.deliverables, "num_posts": contract.num_posts,
            "price": {"usd_cents": usd.to_dict(), "local": {**local.to_dict(), "currency": contract.currency},
                      "fx_rate": str(contract.fx_rate)},
            "deadline": iso(contract.deadline), "deadline_extended": contract.deadline_extended,
            "exclusivity": contract.exclusivity, "no_ai_replies": contract.no_ai_replies,
            "with_voice": contract.with_voice, "influencer_must_post": contract.influencer_must_post,
            "requires_guardian": contract.requires_guardian, "offer_expires_at": iso(contract.offer_expires_at),
            "start": iso(contract.start), "end": iso(contract.end), "work_started_at": iso(contract.work_started_at),
            "delivered_at": iso(contract.delivered_at), "review_ends_at": iso(contract.review_ends_at),
            "completed_at": iso(contract.completed_at), "partial_delivery": contract.partial_delivery,
            "revision_used": contract.revision_used, "delivery": contract.delivery_ref,
            "change_rounds": contract.change_rounds, "cancel_reason": contract.cancel_reason,
            "conversation_id": contract.conversation_id, "created_at": contract.created_at.isoformat(),
            "equity": None if not contract.equity_terms else {
                **contract.equity_terms, "acknowledged_at": iso(contract.equity_acknowledged_at),
                "status": contract.equity_terms.get("status", "offered"), "equity_only": not contract.price_cents,
                "vesting_dates": {k: d.isoformat() for k, d in self.svc.equity.schedule(contract)},
                "notice": EQUITY_NOTICE, "covered_by_escrow": False},
        }
        if contract.type == ContractType.AGENT:
            data["display_status"] = {ContractStatus.ACTIVE: "Order received",
                                      ContractStatus.DELIVERED: "Delivered"}.get(contract.status, contract.status)
        if contract.status == ContractStatus.PENDING_GUARDIAN:
            data["display_status"] = "Awaiting guardian"
        if escrow:
            data["escrow"] = self.svc.escrow.present(escrow)
        return data

    async def require_party(self, account: Account, contract_id: str, *, role: str | None = None) -> Contract:
        """Load a contract the account may see. `role` narrows it to "hirer" or "earner"."""
        contract = await self.repo(Contract).get(contract_id)
        if contract is None:
            raise NotFound("Contract not found")
        is_hirer, is_earner = contract.hirer_id == account.id, contract.earner_id == account.id
        if role == "hirer" and not is_hirer or role == "earner" and not is_earner:
            raise NotFound("Contract not found")
        if is_hirer or is_earner or account.is_admin:
            return contract
        if role is None and await self.svc.access.is_guardian_of(account.id, contract.earner_id):
            return contract
        raise NotFound("Contract not found")

    async def detail(self, account: Account, contract_id: str) -> dict[str, Any]:
        contract = await self.require_party(account, contract_id)
        escrow = await self.repo(EscrowTransaction).first(EscrowTransaction.contract_id == contract.id)
        return self.present(contract, escrow)

    async def list_for(self, account_id: str, *, role: str, type: ContractType | None, status: str | None,
                       cursor: str | None, limit: int) -> Page[dict]:
        where = [Contract.hirer_id == account_id if role == "hirer" else Contract.earner_id == account_id]
        if type:
            where.append(Contract.type == type)
        if status:
            where.append(Contract.status == status)
        page = await self.repo(Contract).page(*where, cursor=cursor, limit=limit)
        return Page(items=[self.present(c) for c in page.items], next_cursor=page.next_cursor)

    async def build(self, *, type: ContractType, hirer: Account, earner_id: str, price_cents: int,
                    payment_method: str, equity_only: bool = False, **fields: Any) -> Contract:
        """Create the contract with today's FX rate locked in, then fund escrow (hirer pays first).
        An equity-only deal (HM-06) has no cash, so no escrow."""
        if price_cents <= 0 and not (equity_only and price_cents == 0):
            raise Unprocessable("Price must be above zero", code="invalid_price")
        await self.limiter.enforce(self.policies.offers, hirer.id)
        company = await self.svc.access.company(hirer.id)
        contract = await self.repo(Contract).add(Contract(
            type=type, hirer_id=hirer.id, earner_id=earner_id, price_cents=price_cents,
            fee_pct=self.fee_pct(type), currency=self.s.local_currency, fx_rate=await self.svc.fx.rate(),
            hirer_category=company.category if company else None,
            offer_expires_at=self.now() + timedelta(days=self.s.offer_expiry_days), **fields))
        if contract.price_cents:
            await self.svc.escrow.fund(contract, hirer, payment_method)
        return contract

    async def activate(self, contract: Contract, actor_id: str | None) -> Contract:
        contract.status, contract.start, contract.offer_expires_at = ContractStatus.ACTIVE, self.now(), None
        if contract.type == ContractType.LIKENESS:
            contract.end = contract.start + timedelta(days=self.s.likeness_term_days)  # LK-08: each hire runs 1 month
        if contract.price_cents:
            await self.svc.escrow.activate(contract.id, actor_id)
        participants = [contract.hirer_id, contract.earner_id]
        if contract.agent_account_id:
            participants.append(contract.agent_account_id)
        thread_type = ConversationType.ORDER if contract.type == ContractType.AGENT else ConversationType.CONTRACT
        conversation = await self.svc.messaging.open_thread(thread_type, contract.id, participants, contract.hirer_id)
        contract.conversation_id = conversation.id
        if contract.type == ContractType.AGENT:
            await self.svc.notifications.notify(
                contract.hirer_id, NotificationType.CONTRACT, f"Order received: {contract.title}",
                "The agent has your order. You will hear from us when it is delivered.", {"contract_id": contract.id})
        else:
            await self.svc.notifications.notify(contract.hirer_id, NotificationType.CONTRACT, "Your contract is active",
                                                data={"contract_id": contract.id})
            await self.svc.notifications.notify(
                contract.earner_id, NotificationType.CONTRACT, "Your contract is active",
                "You can start the work now." if contract.type == ContractType.HIRE_ME else
                "The brand can now generate your likeness video. You approve the final cut before it goes live.",
                {"contract_id": contract.id}, include_guardian=True)
        return contract

    async def close_with_refund(self, contract: Contract, status: ContractStatus, actor_id: str | None,
                                reason: str) -> Contract:
        """End an offer or contract with a full refund to the hirer."""
        if contract.price_cents:
            await self.svc.escrow.settle(contract.id, 0, actor_id, reason)
        elif contract.equity_terms and contract.start is None:
            await self.svc.equity.refund_offer_fee(contract)  # never accepted: the brand's $5 comes back
        contract.status, contract.cancel_reason, contract.cancelled_by = status, reason, actor_id
        contract.offer_expires_at = None
        await self.svc.videos.refund_unused_production(contract)
        for party in (contract.hirer_id, contract.earner_id):
            if party != actor_id:
                await self.svc.notifications.notify(
                    party, NotificationType.CONTRACT, f"Contract {status}", reason, {"contract_id": contract.id},
                    include_guardian=party == contract.earner_id)
        return contract

    async def settle_and_close(self, contract: Contract, release_pct: float, status: ContractStatus,
                               actor_id: str | None, reason: str) -> Contract:
        if contract.price_cents:
            await self.svc.escrow.settle(contract.id, release_pct, actor_id, reason)
        contract.status, contract.cancel_reason, contract.cancelled_by = status, reason, actor_id
        contract.completed_at = self.now()
        for party in (contract.hirer_id, contract.earner_id):
            await self.svc.notifications.notify(
                party, NotificationType.CONTRACT, f"Contract {status}", reason, {"contract_id": contract.id},
                include_guardian=party == contract.earner_id)
        return contract

    async def mark_delivered(self, contract: Contract, actor_id: str | None, partial: bool = False) -> Contract:
        """Delivery opens the 7-day review window."""
        contract.status, contract.delivered_at = ContractStatus.DELIVERED, self.now()
        contract.review_ends_at = self.now() + timedelta(days=self.s.review_window_days)
        contract.partial_delivery = partial
        if contract.price_cents:
            await self.svc.escrow.mark_delivered(contract.id, actor_id)
        await self.svc.notifications.notify(
            contract.hirer_id, NotificationType.CONTRACT, "Work delivered",
            f"You have {self.s.review_window_days} days to approve or raise a dispute.", {"contract_id": contract.id})
        return contract

    async def complete(self, contract: Contract, actor_id: str | None, reason: str) -> Contract:
        """Funds clear in full: the hirer approved, or the review window passed with no dispute."""
        if contract.price_cents:
            await self.svc.escrow.settle(contract.id, 100, actor_id, reason)
        contract.status, contract.completed_at = ContractStatus.COMPLETED, self.now()
        await self.svc.notifications.notify(
            contract.agent_account_id or contract.earner_id, NotificationType.CONTRACT,
            f"{'Order' if contract.type == ContractType.AGENT else 'Contract'} completed",
            reason + (". Your earnings are paid in the next payout cycle." if contract.price_cents else "."),
            {"contract_id": contract.id},
            include_guardian=contract.type != ContractType.AGENT)
        if contract.type == ContractType.HIRE_ME:
            page = await self.repo(HireMePage).first(HireMePage.account_id == contract.earner_id)
            if page:
                page.past_hires += 1
        elif contract.type == ContractType.LIKENESS:
            await self.svc.likeness_pages.record_hire(contract.earner_id)
        elif contract.agent_account_id:
            agent = await self.repo(AgentProfile).first(AgentProfile.account_id == contract.agent_account_id)
            if agent:
                agent.completed_orders += 1
                await self.cache.invalidate_prefix("marketplace:")
        return contract

    async def rate(self, hirer: Account, contract_id: str, stars: int) -> dict[str, Any]:
        contract = await self.require_party(hirer, contract_id, role="hirer")
        if contract.status != ContractStatus.COMPLETED:
            raise Unprocessable("You can rate a contract once it is completed", code="not_completed")
        if contract.rating is not None:
            raise Conflict("You have already rated this contract", code="already_rated")
        contract.rating = stars
        target = None
        if contract.agent_account_id:
            target = await self.repo(AgentProfile).first(AgentProfile.account_id == contract.agent_account_id)
        elif contract.type == ContractType.HIRE_ME:
            target = await self.repo(HireMePage).first(HireMePage.account_id == contract.earner_id)
        if target is not None:
            target.rating_sum += stars
            target.rating_count += 1
            if isinstance(target, AgentProfile):
                target.rating_avg = round(target.rating_sum / target.rating_count, 2)
                await self.cache.invalidate_prefix("marketplace:")
        return {"contract_id": contract.id, "rating": stars}

    async def open_blockers(self, account_id: str) -> list[Contract]:
        return list(await self.repo(Contract).list(
            (Contract.hirer_id == account_id) | (Contract.earner_id == account_id),
            Contract.status.in_(OPEN_CONTRACT_STATUSES)))

    async def expire_offers(self) -> int:
        """Job: no response in 7 days -> the offer expires and the hirer is refunded in full."""
        expired = await self.repo(Contract).list(
            Contract.status.in_([ContractStatus.OFFERED, ContractStatus.CHANGES_REQUESTED]),
            Contract.offer_expires_at.is_not(None), Contract.offer_expires_at <= self.now())
        for contract in expired:
            await self.close_with_refund(contract, ContractStatus.EXPIRED, None, "Offer expired with no response")
        return len(expired)

    async def auto_clear(self) -> int:
        """Job: 7 days after delivery with no dispute, funds clear."""
        due = await self.repo(Contract).list(
            Contract.status == ContractStatus.DELIVERED, Contract.review_ends_at <= self.now())
        for contract in due:
            await self.complete(contract, None, "Review window passed with no dispute")
            await self.svc.notifications.notify(contract.hirer_id, NotificationType.CONTRACT,
                                                "Contract completed automatically",
                                                data={"contract_id": contract.id})
        return len(due)


class HireMeService(BaseService):
    # --- HM-01 page ----------------------------------------------------------------------
    def present_page(self, account: Account, profile: UserProfile, page: HireMePage, show_price: bool = True) -> dict:
        return {
            "account_id": account.id, "handle": account.handle, "name": account.display_name,
            "niche": profile.primary_niche, "photos": page.photos, "videos": page.videos,
            "past_hires": page.past_hires,
            "rating": round(page.rating_sum / page.rating_count, 2) if page.rating_count else None,
            "starting_price_cents": page.starting_price_cents if show_price else None,
            "available": page.available and not profile.new_orders_paused,
            "open_to_equity": page.open_to_equity and not profile.is_minor,
            "equity_min_cash_cents": math.ceil(page.starting_price_cents * self.s.equity_min_cash_pct / 100)
            if show_price else None,
            "potential_rating": None,
        }

    async def save_page(self, account: Account, starting_price_cents: int, photos: list[str], videos: list[str]) -> dict:
        profile = await self.svc.access.profile(account.id)
        if profile is None:
            raise Unprocessable("Only user accounts can have a Hire Me page", code="not_a_user")
        # ON-05, or guardian setup for minors (the guardian's ID covers the minor).
        await self.svc.access.require_id_verified(account, "set up a Hire Me page")
        if starting_price_cents <= 0:
            raise Unprocessable("Set a starting price above zero", code="invalid_price")
        page = await self.repo(HireMePage).first(HireMePage.account_id == account.id)
        if page is None:
            page = await self.repo(HireMePage).add(HireMePage(account_id=account.id, starting_price_cents=starting_price_cents))
        page.starting_price_cents, page.photos, page.videos = starting_price_cents, photos, videos
        await self.svc.profiles.invalidate(account)
        return self.present_page(account, profile, page)

    async def set_open_to_equity(self, account: Account, open_to_equity: bool) -> dict:
        """HM-06: creators choose whether brands may offer equity alongside cash. Adults only."""
        page = await self.repo(HireMePage).first(HireMePage.account_id == account.id)
        if page is None:
            raise NotFound("Set up your Hire Me page first")
        if open_to_equity:
            await self.svc.access.require_adult(account, "receive equity offers")
        page.open_to_equity = open_to_equity
        await self.svc.profiles.invalidate(account)
        return {"open_to_equity": page.open_to_equity}

    async def set_availability(self, account: Account, available: bool) -> dict:
        """ "Not available to work as human": the page is hidden and new offers blocked; contracts continue."""
        page = await self.repo(HireMePage).first(HireMePage.account_id == account.id)
        if page is None:
            raise NotFound("Set up your Hire Me page first")
        page.available = available
        await self.svc.profiles.invalidate(account)
        return {"available": available}

    async def _load_page(self, viewer: Account, influencer_id: str) -> tuple[Account, UserProfile, HireMePage]:
        account = await self.repo(Account).get(influencer_id)
        page = await self.repo(HireMePage).first(HireMePage.account_id == influencer_id)
        if account is None or page is None or account.status != AccountStatus.ACTIVE:
            raise NotFound("Hire Me page not found")
        if viewer.id != account.id and (not page.available or await self.svc.blocks.either(viewer.id, account.id)):
            raise NotFound("Hire Me page not found")
        return account, await self.svc.access.profile(account.id), page

    async def _price_visible(self, viewer: Account, profile: UserProfile) -> bool:
        if not profile.is_minor or viewer.id == profile.account_id:
            return True
        return viewer.is_company and await self.svc.access.company_approved_for_minor(profile.account_id, viewer.id)

    async def view_page(self, viewer: Account, influencer_id: str) -> dict:
        account, profile, page = await self._load_page(viewer, influencer_id)
        data = self.present_page(account, profile, page, await self._price_visible(viewer, profile))
        if page.past_hires == 0:
            data["potential_rating"] = await self.svc.likeness_pages.potential_rating(account, profile)
        return data

    async def browse(self, viewer: Account, niche: str | None, cursor: str | None, limit: int) -> Page[dict]:
        """CO-04: region targeting decides who a company sees first; it never hides anyone."""
        limit, offset = max(1, min(limit, 50)), int(Cursor.decode(cursor) or 0)
        company = await self.svc.access.company(viewer.id)
        region = (company.region_targeting if company else None) or viewer.region
        stmt = (select(Account, UserProfile, HireMePage)
                .join(UserProfile, UserProfile.account_id == Account.id)
                .join(HireMePage, HireMePage.account_id == Account.id)
                .where(Account.status == AccountStatus.ACTIVE, HireMePage.available.is_(True),
                       UserProfile.new_orders_paused.is_(False)))
        if niche:
            stmt = stmt.where(UserProfile.primary_niche == niche.lower())
        hidden = await self.svc.blocks.hidden_ids(viewer.id)
        if hidden:
            stmt = stmt.where(Account.id.not_in(hidden))
        order = [UserProfile.ranking_penalty.asc(), Account.id.desc()]
        if region:
            order.insert(0, case((Account.region == region, 0), else_=1))
        rows = (await self.db.execute(stmt.order_by(*order).offset(offset).limit(limit + 1))).all()
        has_more = len(rows) > limit
        items = []
        for account, profile, page in rows[:limit]:
            item = self.present_page(account, profile, page, await self._price_visible(viewer, profile))
            if company and profile.primary_niche and company.category and profile.primary_niche != company.category:
                item["niche_warning"] = f"This influencer's niche is {profile.primary_niche}"
            items.append(item)
        return Page(items=items, next_cursor=Cursor.encode(offset + limit) if has_more else None)

    # --- HM-03 offers ----------------------------------------------------------------------
    async def send_offer(self, brand: Account, *, influencer_id: str, price_cents: int, deliverables: str,
                         deadline: datetime, num_posts: int, no_ai_replies: bool, payment_method: str,
                         equity: dict[str, Any] | None = None) -> dict:
        company = await self.svc.access.require_verified_company(brand, "send offers")
        account, profile, page = await self._load_page(brand, influencer_id)
        if profile.new_orders_paused:
            raise Conflict("This influencer is not taking new orders right now", code="orders_paused")
        if deadline <= self.now():
            raise Unprocessable("The deadline must be in the future", code="invalid_deadline")
        equity = await self._check_equity(brand, company, profile, page, price_cents, equity)
        if not equity and price_cents < page.starting_price_cents:
            raise Unprocessable("The offer is below this influencer's starting price", code="below_starting_price",
                                starting_price_cents=page.starting_price_cents)
        if profile.is_minor and self.svc.access.is_blocked_category_for_minors(company.category):
            raise Forbidden("Companies in this category cannot hire 13-17 accounts", code="blocked_category")
        contract = await self.svc.contracts.build(
            type=ContractType.HIRE_ME, hirer=brand, earner_id=account.id, price_cents=price_cents,
            payment_method=payment_method, title=f"Hire Me offer from {company.business_name}",
            deliverables=deliverables, deadline=deadline, num_posts=num_posts, no_ai_replies=no_ai_replies,
            requires_guardian=profile.is_minor, status=ContractStatus.OFFERED, equity_only=bool(equity) and price_cents == 0)
        contract.equity_terms = equity
        if equity and price_cents == 0:
            await self.svc.equity.charge_fee(brand, contract, payment_method)  # equity-only: the brand's $5
        net = FeeBreakdown.of(price_cents, contract.fee_pct).net
        await self.svc.notifications.notify(
            account.id, NotificationType.CONTRACT, f"New offer from {company.business_name}",
            (f"{self._equity_summary(equity)}, with no cash. Accepting costs a "
             f"${self.s.equity_service_fee_cents / 100:.0f} Wamiwa service fee" if equity and not price_cents else
             f"You would receive ${net / 100:,.2f} after Wamiwa's {contract.fee_pct}% fee"
             + (f", plus {self._equity_summary(equity)}" if equity else ""))
            + f". Respond within {self.s.offer_expiry_days} days.", {"contract_id": contract.id}, include_guardian=True)
        return self.svc.contracts.present(contract)

    # --- HM-06 cash plus equity ----------------------------------------------------------------
    @staticmethod
    def _equity_summary(equity: dict[str, Any]) -> str:
        amount = f"{equity['percent']}% equity" if equity.get("percent") else f"{equity['shares']:,} shares"
        vesting = f", vesting over {equity['vesting_months']} months" if equity.get("vesting_months") else ""
        return amount + vesting

    async def _check_equity(self, brand: Account, company, profile, page: HireMePage, price_cents: int,
                            equity: dict[str, Any] | None) -> dict[str, Any] | None:
        """Equity alone, or equity plus cash. With cash, the cash part goes through escrow, carries Wamiwa's fee and
        must be at least half the starting price; equity-only deals instead cost each side a flat service fee.
        Only adults who opted in, only registered companies, and only with a written agreement."""
        if not equity:
            return None
        if profile.is_minor:
            raise Forbidden("Equity can't be offered to under-18s", code="equity_minor")
        if not page.open_to_equity:
            raise Forbidden("This influencer isn't taking equity offers", code="equity_not_accepted")
        if not company.registration_no:
            raise Forbidden("Only registered companies can offer equity", code="equity_unregistered_company")
        percent, shares = equity.get("percent"), equity.get("shares")
        if bool(percent) == bool(shares):
            raise Unprocessable("Offer either a percentage or a number of shares", code="invalid_equity")
        minimum = math.ceil(page.starting_price_cents * self.s.equity_min_cash_pct / 100)
        if 0 < price_cents < minimum:
            raise Unprocessable(f"With equity, the cash part must be at least {self.s.equity_min_cash_pct}% of this "
                                f"influencer's starting price", code="equity_cash_too_low", minimum_cents=minimum)
        await self.svc.uploads.claim(brand, [equity["agreement_ref"]], {"document"})
        return {"percent": percent, "shares": shares, "share_class": equity.get("share_class") or "ordinary",
                "vesting_months": equity.get("vesting_months") or 0, "cliff_months": equity.get("cliff_months") or 0,
                "agreement_ref": equity["agreement_ref"], "description": (equity.get("description") or "").strip(),
                "company_registration_no": company.registration_no}

    async def _own(self, account: Account, contract_id: str, role: str, *statuses: ContractStatus) -> Contract:
        contract = await self.svc.contracts.require_party(account, contract_id, role=role)
        if contract.type != ContractType.HIRE_ME:
            raise NotFound("Contract not found")
        if statuses and contract.status not in statuses:
            raise Conflict(f"This contract is {contract.status}", code="invalid_contract_status")
        return contract

    async def accept(self, influencer: Account, contract_id: str, equity_acknowledged: bool = False,
                     payment_method: str | None = None) -> dict:
        contract = await self._own(influencer, contract_id, "earner", ContractStatus.OFFERED)
        if contract.equity_terms:
            if not equity_acknowledged:
                raise Unprocessable("Confirm you understand how the equity works before accepting",
                                    code="equity_acknowledgement_required", notice=EQUITY_NOTICE)
            if not contract.price_cents:
                await self.svc.equity.charge_fee(influencer, contract, payment_method)  # equity-only: the creator's $5
            contract.equity_acknowledged_at = self.now()
        if contract.requires_guardian:
            # HM-03 / LK-09: a minor's contract needs guardian approval and a confirmation call first.
            contract.status = ContractStatus.PENDING_GUARDIAN
            await self.svc.guardian_confirmations.open(contract)
        else:
            await self.svc.contracts.activate(contract, influencer.id)
            if contract.equity_terms:
                await self.svc.equity.on_accepted(contract)  # both sides get the deal summary
        return self.svc.contracts.present(contract)

    async def decline(self, influencer: Account, contract_id: str) -> dict:
        contract = await self._own(influencer, contract_id, "earner", ContractStatus.OFFERED)
        await self.svc.contracts.close_with_refund(contract, ContractStatus.DECLINED, influencer.id, "Offer declined")
        return self.svc.contracts.present(contract)

    async def start_work(self, influencer: Account, contract_id: str) -> dict:
        contract = await self._own(influencer, contract_id, "earner", ContractStatus.ACTIVE)
        contract.work_started_at = contract.work_started_at or self.now()
        return self.svc.contracts.present(contract)

    # --- HM-04 deliver and complete -----------------------------------------------------------
    async def deliver(self, influencer: Account, contract_id: str, delivery: str, partial: bool) -> dict:
        contract = await self._own(influencer, contract_id, "earner", ContractStatus.ACTIVE)
        contract.work_started_at = contract.work_started_at or self.now()
        contract.delivery_ref = delivery
        await self.svc.contracts.mark_delivered(contract, influencer.id, partial)
        return self.svc.contracts.present(contract)

    async def approve(self, brand: Account, contract_id: str) -> dict:
        contract = await self._own(brand, contract_id, "hirer", ContractStatus.DELIVERED)
        await self.svc.contracts.complete(contract, brand.id, "Approved by hirer")
        return self.svc.contracts.present(contract)

    async def release_partial(self, brand: Account, contract_id: str, release_pct: int) -> dict:
        """HM-05 partial delivery: the brand releases part of the payment; the rest is refunded."""
        contract = await self._own(brand, contract_id, "hirer", ContractStatus.DELIVERED)
        if not contract.partial_delivery:
            raise Unprocessable("Part payment applies to partial deliveries", code="not_partial")
        if not 0 < release_pct < 100:
            raise Unprocessable("Choose a share between 1 and 99 percent", code="invalid_split")
        await self.svc.contracts.settle_and_close(
            contract, release_pct, ContractStatus.COMPLETED, brand.id, f"Brand released {release_pct}% for partial delivery")
        return self.svc.contracts.present(contract)

    # --- HM-05 cancellations and missed deadlines ------------------------------------------------
    async def extend_deadline(self, brand: Account, contract_id: str, new_deadline: datetime) -> dict:
        contract = await self._own(brand, contract_id, "hirer", ContractStatus.ACTIVE)
        if contract.deadline_extended:
            raise Conflict("The deadline can only be extended once", code="already_extended")
        if contract.deadline is None or new_deadline <= max(contract.deadline, self.now()):
            raise Unprocessable("The new deadline must be later than the current one", code="invalid_deadline")
        contract.deadline, contract.deadline_extended = new_deadline, True
        await self.svc.notifications.notify(contract.earner_id, NotificationType.CONTRACT, "Deadline extended",
                                            data={"contract_id": contract.id}, include_guardian=True)
        return self.svc.contracts.present(contract)

    async def cancel(self, account: Account, contract_id: str, reason: str) -> dict:
        contract = await self.svc.contracts.require_party(account, contract_id)
        if contract.type != ContractType.HIRE_ME or account.id not in (contract.hirer_id, contract.earner_id):
            raise NotFound("Contract not found")
        by_brand = account.id == contract.hirer_id
        contracts = self.svc.contracts
        if contract.status in (ContractStatus.OFFERED, ContractStatus.PENDING_GUARDIAN):
            status = ContractStatus.CANCELLED if by_brand else ContractStatus.DECLINED
            await contracts.close_with_refund(contract, status, account.id, reason or "Offer withdrawn")
        elif contract.status == ContractStatus.ACTIVE and not by_brand:
            # Influencer cancels: full refund. 3 cancellations in 90 days lowers ranking.
            await contracts.close_with_refund(contract, ContractStatus.CANCELLED, account.id, reason or "Cancelled by influencer")
            await self._apply_ranking_penalty(account.id)
        elif contract.status == ContractStatus.ACTIVE:
            missed = contract.deadline is not None and contract.deadline < self.now()
            if contract.work_started_at is None or missed:
                # Before work starts, or after a missed deadline: full refund.
                await contracts.close_with_refund(contract, ContractStatus.CANCELLED, account.id,
                                                  reason or ("Deadline missed" if missed else "Cancelled before start"))
            else:
                # Brand cancels after work started: the influencer keeps 25% of the net amount.
                await contracts.settle_and_close(
                    contract, self.s.brand_cancel_after_start_keep_pct, ContractStatus.CANCELLED, account.id,
                    reason or "Cancelled by brand after work started")
        elif contract.status == ContractStatus.DELIVERED and by_brand:
            # Brand cancels after delivery: the influencer keeps 100% of the net amount.
            await contracts.settle_and_close(contract, 100, ContractStatus.CANCELLED, account.id,
                                             reason or "Cancelled by brand after delivery")
        else:
            raise Conflict(f"This contract is {contract.status} and cannot be cancelled", code="invalid_contract_status")
        return contracts.present(contract)

    async def _apply_ranking_penalty(self, influencer_id: str) -> None:
        since = self.now() - timedelta(days=self.s.cancellation_window_days)
        cancelled = await self.repo(Contract).count(
            Contract.earner_id == influencer_id, Contract.cancelled_by == influencer_id,
            Contract.status == ContractStatus.CANCELLED, Contract.updated_at >= since)
        profile = await self.svc.access.profile(influencer_id)
        if profile:
            profile.ranking_penalty = cancelled >= self.s.cancellations_for_ranking_penalty

    async def refresh_ranking_penalties(self) -> int:
        """Job: lift the penalty once cancellations age out of the 90-day window."""
        penalised = await self.repo(UserProfile).list(UserProfile.ranking_penalty.is_(True))
        for profile in penalised:
            await self._apply_ranking_penalty(profile.account_id)
        return len(penalised)


class AgentOrderService(BaseService):
    """AG-04: marketplace orders use the same escrow as human hires, with a 10% fee."""

    async def _order(self, contract_id: str, *statuses: ContractStatus) -> Contract:
        contract = await self.repo(Contract).get(contract_id)
        if contract is None or contract.type != ContractType.AGENT:
            raise NotFound("Order not found")
        if statuses and contract.status not in statuses:
            raise Conflict(f"This order is {contract.status}", code="invalid_contract_status")
        return contract

    @staticmethod
    def _is_seller(actor: Account, contract: Contract) -> bool:
        return actor.id in (contract.earner_id, contract.agent_account_id)

    async def place(self, hirer: Account, agent_id: str, task: str, brief: str, payment_method: str) -> dict:
        if hirer.is_company:
            await self.svc.access.require_verified_company(hirer, "hire agents")
        elif hirer.is_user:
            await self.svc.access.require_adult(hirer, "hire agents")
        else:
            raise Forbidden("Only users and companies can hire agents", code="not_allowed")
        agent_account = await self.repo(Account).get(agent_id)
        agent = await self.repo(AgentProfile).first(AgentProfile.account_id == agent_id)
        integration = await self.repo(AgentIntegration).first(AgentIntegration.agent_account_id == agent_id)
        if agent is None or agent_account is None or agent_account.status != AccountStatus.ACTIVE:
            raise NotFound("This agent is no longer available", code="agent_unavailable")
        if agent_account.handle == self.s.wamiwa_agent_handle:
            raise Conflict("The Wamiwa Agent is bought as a monthly plan", code="use_promotions",
                           hint="POST /promotions")
        if agent.paused or integration is None or integration.status != IntegrationStatus.LIVE:
            raise Conflict("This agent is not taking orders right now", code="agent_paused")
        if agent.owner_account_id == hirer.id:
            raise Unprocessable("You cannot hire your own agent", code="self_hire")
        if await self.svc.blocks.either(hirer.id, agent.owner_account_id):
            raise Forbidden("You can't hire this agent", code="blocked")
        task = task.strip().lower()
        if task not in agent.rates:
            raise Unprocessable("This agent does not offer that task", code="unknown_task", tasks=list(agent.rates))
        contract = await self.svc.contracts.build(
            type=ContractType.AGENT, hirer=hirer, earner_id=agent.owner_account_id, price_cents=agent.rates[task],
            payment_method=payment_method, agent_account_id=agent_id, title=task, deliverables=brief)
        # No accept step: the order is live as soon as escrow is funded ("Order received").
        await self.svc.contracts.activate(contract, hirer.id)
        await self.svc.webhooks.emit(agent_id, EventType.ORDER_CREATED, {
            "order_id": contract.id, "task": task, "hirer": {"id": hirer.id, "handle": hirer.handle},
            "brief": self.svc.webhooks.untrusted(brief)})
        await self.svc.notifications.notify(agent_id, NotificationType.CONTRACT, f"New order for {agent.name}",
                                            f"Task: {task}", {"contract_id": contract.id})
        return self.svc.contracts.present(contract)

    async def mark_in_progress(self, actor: Account, order_id: str, note: str) -> dict:
        contract = await self._order(order_id, ContractStatus.ACTIVE)
        if not self._is_seller(actor, contract):
            raise NotFound("Order not found")
        contract.work_started_at = contract.work_started_at or self.now()
        if note:
            await self.svc.messaging.send_order_message(actor, order_id, note)
        return self.svc.contracts.present(contract)

    async def deliver(self, actor: Account, order_id: str, delivery: str) -> dict:
        contract = await self._order(order_id, ContractStatus.ACTIVE)
        if not self._is_seller(actor, contract):
            raise NotFound("Order not found")
        contract.work_started_at = contract.work_started_at or self.now()
        contract.delivery_ref = delivery
        await self.svc.contracts.mark_delivered(contract, actor.id)
        return self.svc.contracts.present(contract)

    async def approve(self, hirer: Account, order_id: str) -> dict:
        contract = await self._order(order_id, ContractStatus.DELIVERED)
        if contract.hirer_id != hirer.id:
            raise NotFound("Order not found")
        await self.svc.contracts.complete(contract, hirer.id, "Approved by hirer")
        return self.svc.contracts.present(contract)

    async def request_revision(self, hirer: Account, order_id: str, note: str) -> dict:
        """Within the 7-day window the hirer may request one revision."""
        contract = await self._order(order_id, ContractStatus.DELIVERED)
        if contract.hirer_id != hirer.id:
            raise NotFound("Order not found")
        if contract.revision_used:
            raise Conflict("You have already used your one revision", code="revision_used",
                           hint="Approve the delivery or open a dispute")
        contract.revision_used, contract.status = True, ContractStatus.ACTIVE
        contract.review_ends_at = contract.delivered_at = None
        await self.svc.escrow.reopen(contract.id, hirer.id, "Revision requested")
        await self.svc.webhooks.emit(contract.agent_account_id, EventType.ORDER_REVISION_REQUESTED, {
            "order_id": contract.id, "note": self.svc.webhooks.untrusted(note)})
        await self.svc.notifications.notify(contract.agent_account_id, NotificationType.CONTRACT,
                                            "Revision requested", note, {"contract_id": contract.id})
        return self.svc.contracts.present(contract)

    async def refund(self, actor: Account, order_id: str, reason: str) -> dict:
        """The owner refunds in full when the agent cannot complete; the hirer may cancel before work starts."""
        contract = await self._order(order_id, ContractStatus.ACTIVE, ContractStatus.DELIVERED)
        is_owner, is_hirer = actor.id == contract.earner_id, actor.id == contract.hirer_id
        if not (is_owner or is_hirer):
            raise NotFound("Order not found")
        if is_hirer and (contract.work_started_at is not None or contract.status != ContractStatus.ACTIVE):
            raise Conflict("Work has started. Ask for a revision or open a dispute instead.", code="work_started")
        await self.svc.contracts.close_with_refund(contract, ContractStatus.CANCELLED, actor.id,
                                                   reason or "Order refunded in full")
        await self.svc.webhooks.emit(contract.agent_account_id, EventType.ORDER_CANCELLED, {
            "order_id": contract.id, "reason": self.svc.webhooks.untrusted(reason)})
        return self.svc.contracts.present(contract)

    async def list_for_owner(self, owner: Account, agent_id: str | None, cursor: str | None, limit: int) -> Page[dict]:
        where = [Contract.type == ContractType.AGENT, Contract.earner_id == owner.id]
        if agent_id:
            where.append(Contract.agent_account_id == agent_id)
        page = await self.repo(Contract).page(*where, cursor=cursor, limit=limit)
        return Page(items=[self.svc.contracts.present(c) for c in page.items], next_cursor=page.next_cursor)
