"""Blocks (MS-03), follows (SO-01), profiles (SO-02) and search (SO-03)."""
from typing import Any

from sqlalchemy import String, cast, or_, select

from app.core.enums import AccountStatus, AccountType, ModerationStatus, NotificationType
from app.core.exceptions import Forbidden, NotFound, Unprocessable
from app.core.utils import Cursor, Page
from app.models import (
    Account, AgentProfile, Block, CompanyProfile, Follow, HireMePage, LikenessPage, Post, UserProfile,
)
from app.services.base import BaseService


class BlockService(BaseService):
    async def hidden_ids(self, account_id: str | None) -> set[str]:
        """Everyone this account has blocked or been blocked by. Cached; dropped on any change."""
        if not account_id:
            return set()

        async def load() -> list[str]:
            rows = await self.repo(Block).list(or_(Block.blocker_id == account_id, Block.blocked_id == account_id))
            return sorted({r.blocked_id if r.blocker_id == account_id else r.blocker_id for r in rows})

        return set(await self.cache.get_or_set(f"blocks:{account_id}", 300, load) or [])

    async def has_blocked(self, blocker_id: str, blocked_id: str) -> bool:
        return await self.repo(Block).exists(Block.blocker_id == blocker_id, Block.blocked_id == blocked_id)

    async def either(self, a: str, b: str) -> bool:
        return b in await self.hidden_ids(a)

    async def block(self, account: Account, target_id: str) -> None:
        if target_id == account.id:
            raise Unprocessable("You cannot block yourself", code="self_block")
        await self.repo(Account).require(target_id, "Account not found")
        if await self.has_blocked(account.id, target_id):
            return
        await self.repo(Block).add(Block(blocker_id=account.id, blocked_id=target_id))
        # A block also ends the follow relationship in both directions.
        await self.svc.follows.remove(account.id, target_id)
        await self.svc.follows.remove(target_id, account.id)
        await self.cache.delete(f"blocks:{account.id}", f"blocks:{target_id}")

    async def unblock(self, account: Account, target_id: str) -> None:
        block = await self.repo(Block).first(Block.blocker_id == account.id, Block.blocked_id == target_id)
        if block:
            await self.repo(Block).delete(block)
            await self.cache.delete(f"blocks:{account.id}", f"blocks:{target_id}")

    async def list_blocked(self, account: Account) -> list[dict[str, Any]]:
        blocks = await self.repo(Block).list(Block.blocker_id == account.id, order_by=Block.id.desc())
        accounts = await self.repo(Account).by_ids([b.blocked_id for b in blocks])
        return [{"id": b.blocked_id, "handle": accounts[b.blocked_id].handle, "type": accounts[b.blocked_id].type,
                 "blocked_at": b.created_at.isoformat()} for b in blocks if b.blocked_id in accounts]


class FollowService(BaseService):
    async def is_following(self, follower_id: str, followed_id: str) -> bool:
        return await self.repo(Follow).exists(Follow.follower_id == follower_id, Follow.followed_id == followed_id)

    async def following_ids(self, follower_id: str) -> set[str]:
        rows = await self.db.execute(select(Follow.followed_id).where(Follow.follower_id == follower_id))
        return set(rows.scalars().all())

    async def follow(self, account: Account, target_id: str) -> dict[str, Any]:
        if target_id == account.id:
            raise Unprocessable("You cannot follow yourself", code="self_follow")
        target = await self.repo(Account).require(target_id, "Account not found")
        if not target.is_visible or target.is_admin:
            raise NotFound("Account not found")
        if await self.svc.blocks.either(account.id, target_id):
            raise Forbidden("You can't follow this account", code="blocked")
        if account.is_agent and not target.is_agent:
            # Agents read human content and answer tags, but they only follow other agents.
            raise Forbidden("Agents can only follow other agents", code="agents_follow_agents_only")
        if await self.is_following(account.id, target_id):
            return {"following": True}
        await self.limiter.enforce(self.policies.follows, account.id)
        await self.repo(Follow).add(Follow(follower_id=account.id, followed_id=target_id, by_agent=account.is_agent))
        # AG-02: agent follows are kept apart and excluded from every follower metric.
        if account.is_agent:
            target.followers_agent += 1
        else:
            target.followers_human += 1
        account.following_count += 1
        if not target.is_agent and await self.kv.set_if_absent(f"follownotice:{account.id}:{target.id}", "1", 365 * 86400):
            await self.svc.notifications.notify(
                target.id, NotificationType.FOLLOW, f"@{account.handle} started following you",
                data={"follower_handle": account.handle})
        await self.svc.likeness_pages.refresh_eligibility(target)
        await self.svc.profiles.invalidate(target)
        return {"following": True}

    async def remove(self, follower_id: str, followed_id: str) -> bool:
        follow = await self.repo(Follow).first(Follow.follower_id == follower_id, Follow.followed_id == followed_id)
        if follow is None:
            return False
        follower = await self.repo(Account).require(follower_id)
        target = await self.repo(Account).require(followed_id)
        await self.repo(Follow).delete(follow)
        if follow.by_agent:
            target.followers_agent = max(0, target.followers_agent - 1)
        else:
            target.followers_human = max(0, target.followers_human - 1)
        follower.following_count = max(0, follower.following_count - 1)
        await self.svc.likeness_pages.refresh_eligibility(target)
        await self.svc.profiles.invalidate(target)
        return True

    async def unfollow(self, account: Account, target_id: str) -> dict[str, Any]:
        await self.remove(account.id, target_id)
        return {"following": False}

    async def list_follows(self, account_id: str, direction: str, cursor: str | None, limit: int) -> Page[dict]:
        column, other = (Follow.followed_id, Follow.follower_id) if direction == "followers" else (
            Follow.follower_id, Follow.followed_id)
        page = await self.repo(Follow).page(column == account_id, cursor=cursor, limit=limit)
        ids = [f.follower_id if direction == "followers" else f.followed_id for f in page.items]
        accounts = await self.repo(Account).by_ids(ids)
        items = [{"id": i, "handle": accounts[i].handle, "name": accounts[i].display_name, "type": accounts[i].type,
                  "label": "agent" if accounts[i].is_agent else "human"} for i in ids if i in accounts]
        return Page(items=items, next_cursor=page.next_cursor)


class ProfileService(BaseService):
    async def invalidate(self, account: Account) -> None:
        await self.cache.delete(f"profile:{account.handle}")

    async def _public(self, handle: str) -> dict[str, Any] | None:
        """Viewer-independent part of a profile, cached briefly."""
        account = await self.repo(Account).first(Account.handle == handle)
        if account is None or not account.is_visible or account.is_admin:
            return None
        base: dict[str, Any] = {
            "id": account.id, "handle": account.handle, "type": account.type, "name": account.display_name,
            "followers": {"human": account.followers_human, "agent": account.followers_agent},
            "following": account.following_count,
        }
        if account.type == AccountType.USER:
            profile = await self.svc.access.profile(account.id)
            hire_me = await self.repo(HireMePage).first(HireMePage.account_id == account.id)
            likeness = await self.repo(LikenessPage).first(LikenessPage.account_id == account.id)
            base.update({
                "account_kind": "human", "photo": profile.photo_ref, "bio": profile.bio,
                "niche": profile.primary_niche, "secondary_niches": profile.secondary_niches, "links": profile.links,
                "is_minor": profile.is_minor,  # internal flag; stripped before returning
                "badges": [badge for badge, on in (
                    ("id_verified", profile.id_verified), ("verified_creator", profile.is_verified_creator),
                    ("verified_major_person", profile.is_major_person),
                    ("subscriber", await self.svc.access.has_subscriber_badge(account.id, profile))) if on],
                "popularity_level": profile.popularity_level,
                "hire_me": None if not (hire_me and hire_me.available) else {
                    "url": f"/hire-me/{account.handle}", "starting_price_cents": hire_me.starting_price_cents},
                "likeness": None if not (likeness and not likeness.paused) else {
                    "url": f"/likeness/{account.handle}", "price_image_cents": likeness.price_image_cents},
            })
        elif account.type == AccountType.COMPANY:
            company = await self.svc.access.company(account.id)
            base.update({"account_kind": "company", "name": company.business_name, "category": company.category,
                         "bio": company.bio, "verified": company.verified,
                         "allow_user_messages": company.allow_user_messages})
        else:
            agent = await self.repo(AgentProfile).first(AgentProfile.account_id == account.id)
            owner = await self.repo(Account).get(agent.owner_account_id)
            base.update({"account_kind": "agent", "name": agent.name, "description": agent.description,
                         "owner": {"id": owner.id, "handle": owner.handle, "name": owner.display_name},
                         "skills": agent.skills, "rates": agent.rates, "rating": agent.rating_avg,
                         "completed_orders": agent.completed_orders, "hire_url": f"/marketplace/agents/{account.id}"})
        return base

    async def view(self, viewer: Account | None, handle: str) -> dict[str, Any]:
        handle = handle.lstrip("@").lower()
        data = await self.cache.get_or_set(f"profile:{handle}", self.s.cache_ttl_profile, lambda: self._public(handle))
        if data is None:
            raise NotFound("Account not found")
        data = dict(data)
        if viewer and viewer.id != data["id"] and await self.svc.blocks.has_blocked(data["id"], viewer.id):
            raise NotFound("Account not found")  # MS-03: blocked accounts cannot view my profile

        is_minor = data.pop("is_minor", False)
        if is_minor:
            # SO-02: never show a minor's prices to unapproved companies (date of birth is never public).
            approved = bool(viewer and viewer.is_company and await self.svc.access.company_approved_for_minor(
                data["id"], viewer.id))
            for page in ("hire_me", "likeness"):
                if data.get(page) and not approved:
                    data[page] = {"url": data[page]["url"]}
        if data["type"] == AccountType.COMPANY:
            allow = data.pop("allow_user_messages", False)
            # MS-01: the message button is hidden for humans when the company has messages switched off.
            data["can_message"] = bool(viewer) and (viewer.is_company or (viewer.is_user and allow))
        if viewer and viewer.id != data["id"]:
            data["is_following"] = await self.svc.follows.is_following(viewer.id, data["id"])
            if data["type"] == AccountType.USER and await self.kv.set_if_absent(
                    f"pview:{viewer.id}:{data['id']}", "1", 24 * 3600):
                await self.svc.notifications.notify(
                    data["id"], NotificationType.PROFILE_VIEW, "Someone viewed your profile",
                    data={"viewer_type": viewer.type})
        return data


class SearchService(BaseService):
    """One search box with tabs: People, Companies, Agents, Posts. Hidden accounts never appear."""

    TABS = ("people", "companies", "agents", "posts")

    async def search(self, viewer: Account, q: str, tab: str, filters: dict[str, Any], cursor: str | None,
                     limit: int) -> Page[dict]:
        if tab not in self.TABS:
            raise Unprocessable(f"tab must be one of {', '.join(self.TABS)}", code="invalid_tab")
        await self.limiter.enforce(self.policies.search, viewer.id)
        limit = max(1, min(limit, 50))
        hidden = await self.svc.blocks.hidden_ids(viewer.id)
        pattern = f"%{q.strip().lower()}%" if q.strip() else None
        last_id = Cursor.decode(cursor)
        builder = getattr(self, f"_{tab}")
        stmt, key, present = builder(pattern, filters)
        if hidden:
            stmt = stmt.where(key.not_in(hidden))
        if last_id:
            stmt = stmt.where(self._order_column(tab) < last_id)
        stmt = stmt.order_by(self._order_column(tab).desc()).limit(limit + 1)
        rows = (await self.db.execute(stmt)).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [present(*row) for row in rows]
        if tab == "posts":
            await self.svc.posts.for_viewer(viewer, items)
            await self.svc.promos.decorate(viewer, items, "post")
        return Page(items=items, next_cursor=Cursor.encode(items[-1]["cursor_id"]) if has_more and items else None)

    @staticmethod
    def _order_column(tab: str):
        return Post.id if tab == "posts" else Account.id

    def _people(self, pattern, f):
        stmt = (select(Account, UserProfile).join(UserProfile, UserProfile.account_id == Account.id)
                .where(Account.type == AccountType.USER, Account.status == AccountStatus.ACTIVE))
        if pattern:
            stmt = stmt.where(or_(Account.handle.ilike(pattern), Account.display_name.ilike(pattern)))
        if f.get("niche"):
            stmt = stmt.where(UserProfile.primary_niche == f["niche"].lower())
        if f.get("location"):
            stmt = stmt.where(UserProfile.location.ilike(f"%{f['location']}%"))
        if f.get("verified_only"):
            stmt = stmt.where(or_(UserProfile.id_verified, UserProfile.is_verified_creator, UserProfile.is_major_person))
        if f.get("available_for_hire"):
            stmt = stmt.where(Account.id.in_(select(HireMePage.account_id).where(HireMePage.available.is_(True))),
                              UserProfile.new_orders_paused.is_(False))

        def present(account: Account, profile: UserProfile) -> dict:
            return {"cursor_id": account.id, "id": account.id, "handle": account.handle, "name": account.display_name,
                    "label": "human", "niche": profile.primary_niche, "location": profile.location,
                    "photo": profile.photo_ref,
                    "verified": profile.id_verified or profile.is_verified_creator or profile.is_major_person}

        return stmt, Account.id, present

    def _companies(self, pattern, f):
        stmt = (select(Account, CompanyProfile).join(CompanyProfile, CompanyProfile.account_id == Account.id)
                .where(Account.type == AccountType.COMPANY, Account.status == AccountStatus.ACTIVE))
        if pattern:
            stmt = stmt.where(or_(Account.handle.ilike(pattern), CompanyProfile.business_name.ilike(pattern)))
        if f.get("category"):
            stmt = stmt.where(CompanyProfile.category == f["category"].lower())
        if f.get("verified_only"):
            stmt = stmt.where(CompanyProfile.verified.is_(True))

        def present(account: Account, company: CompanyProfile) -> dict:
            return {"cursor_id": account.id, "id": account.id, "handle": account.handle,
                    "name": company.business_name, "category": company.category, "verified": company.verified}

        return stmt, Account.id, present

    def _agents(self, pattern, f):
        stmt = (select(Account, AgentProfile).join(AgentProfile, AgentProfile.account_id == Account.id)
                .where(Account.type == AccountType.AGENT, Account.status == AccountStatus.ACTIVE))
        if pattern:
            stmt = stmt.where(or_(Account.handle.ilike(pattern), AgentProfile.name.ilike(pattern),
                                  AgentProfile.description.ilike(pattern)))
        if f.get("skill"):
            stmt = stmt.where(cast(AgentProfile.skills, String).ilike(f'%"{f["skill"].lower()}"%'))
        if f.get("max_price_cents") is not None:
            stmt = stmt.where(AgentProfile.min_rate_cents <= f["max_price_cents"])
        if f.get("min_rating") is not None:
            stmt = stmt.where(AgentProfile.rating_avg >= f["min_rating"])

        def present(account: Account, agent: AgentProfile) -> dict:
            return {"cursor_id": account.id, "id": account.id, "handle": account.handle, "name": agent.name,
                    "label": "agent", "skills": agent.skills, "rates": agent.rates, "rating": agent.rating_avg,
                    "completed_orders": agent.completed_orders}

        return stmt, Account.id, present

    def _posts(self, pattern, f):
        stmt = (select(Post, Account).join(Account, Account.id == Post.author_account_id)
                .where(Post.moderation_status == ModerationStatus.VISIBLE, Account.status == AccountStatus.ACTIVE))
        if pattern:
            stmt = stmt.where(Post.text.ilike(pattern))
        if f.get("niche"):
            stmt = stmt.where(Post.niche == f["niche"].lower())

        def present(post: Post, author: Account) -> dict:  # the same shape as the feed, so it renders the same
            return {"cursor_id": post.id, **self.svc.posts.present(post, author)}

        return stmt, Post.author_account_id, present
