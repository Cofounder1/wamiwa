"""Epic 2: posts, system-set labels, feeds, comments, agent tags, analytics and daily post ideas."""
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update

from app.core.enums import (
    AccountStatus, AgentCommentPolicy, AiBadge, ContractStatus, EventType, FeedTab, IntegrationStatus,
    Label, ModerationStatus, NotificationType, PostType, Queue, TagStatus, VideoStatus,
)
from app.core.exceptions import Forbidden, NotFound, RateLimited, Unprocessable
from app.core.utils import Cursor, Page, extract_mentions
from app.models import (
    Account, AgentIntegration, AgentProfile, AgentTag, Comment, CommentRating, CompanyProfile, Contract,
    Follow, GeneratedVideo, Post, PostIdea, PostLike, UserProfile,
)
from app.services.base import BaseService, Principal

FREE_TAG_UNAVAILABLE = ("I am currently not available for free tag, you can check me out in the marketplace "
                        "to see how I can help you with this and more")


class LabelPolicy:
    """Labeling rules table (Epic 2). Labels come from how content was made; users cannot set them."""

    @staticmethod
    def for_post(author: Account, *, ai_generated: bool, likeness: bool) -> tuple[Label, AiBadge]:
        if likeness:
            return Label.SPONSORED, AiBadge.AI_LIKENESS
        if author.is_agent:
            return Label.AGENT, AiBadge.NONE
        return Label.HUMAN, AiBadge.AI_GENERATED if ai_generated else AiBadge.NONE

    @staticmethod
    def for_comment(principal: Principal) -> Label:
        # Product decision (supersedes API-02): a company's API replies carry no Human/Agent tag.
        if principal.kind == "company_key":
            return Label.NONE
        return Label.AGENT if principal.account.is_agent else Label.HUMAN

    @staticmethod
    def feed_labels(tab: FeedTab) -> tuple[Label, ...]:
        return (Label.AGENT,) if tab == FeedTab.AGENTS else (Label.HUMAN, Label.SPONSORED)

    @staticmethod
    def display(label: str, ai_badge: str = AiBadge.NONE, on_behalf_of_handle: str | None = None) -> str:
        if on_behalf_of_handle:
            return f"Agent — replying for @{on_behalf_of_handle}"
        if label == Label.NONE:
            return ""
        text = {Label.HUMAN: "Human", Label.AGENT: "Agent", Label.SPONSORED: "Sponsored"}[Label(label)]
        badge = {AiBadge.AI_GENERATED: "AI-generated", AiBadge.AI_LIKENESS: "AI likeness"}.get(AiBadge(ai_badge))
        return f"{text} + {badge}" if badge else text


class PostService(BaseService):
    def present(self, post: Post, author: Account | None = None, liked: bool | None = None) -> dict[str, Any]:
        data = {
            "id": post.id, "type": post.type, "text": post.text, "media": post.media_ref,
            "duration_seconds": post.duration_seconds, "label": post.label, "ai_badge": post.ai_badge,
            "label_display": LabelPolicy.display(post.label, post.ai_badge),
            "sponsored_contract_id": post.sponsored_contract_id, "niche": post.niche,
            "tagged_companies": post.tagged_company_ids, "moderation_status": post.moderation_status,
            "likes": {"human": post.likes_human, "agent": post.likes_agent},
            "comments": {"human": post.comments_human, "agent": post.comments_agent},
            "created_at": post.created_at.isoformat(), "author_id": post.author_account_id,
        }
        if author:
            data["author"] = {"id": author.id, "handle": author.handle, "name": author.display_name, "type": author.type}
        if liked is not None:
            data["liked"] = liked
        return data

    async def _niche_of(self, account: Account) -> str | None:
        if account.is_company:
            company = await self.svc.access.company(account.id)
            return company.category if company else None
        profile = await self.svc.access.profile(account.id)
        return profile.primary_niche if profile else None

    async def create(self, principal: Principal, *, type: PostType, text: str = "", media_ref: str | None = None,
                     duration_seconds: int = 0, ai_generated: bool = False, generated_video_id: str | None = None,
                     tagged_handles: list[str] | None = None, likeness_contract_id: str | None = None) -> Post:
        author = principal.account
        if principal.kind == "company_key":
            await self.limiter.enforce(self.policies.api_posts, principal.api_key_id)  # API-01: 60/hour per key
        elif author.is_agent:
            await self.limiter.enforce(self.policies.agent_posts, author.id)
        else:
            await self.limiter.enforce(self.policies.posts, author.id)

        if generated_video_id:
            video = await self.repo(GeneratedVideo).require(generated_video_id, "Video not found")
            if video.owner_account_id != author.id or video.status != VideoStatus.READY:
                raise Forbidden("You can only post your own finished videos", code="video_not_available")
            media_ref, duration_seconds, ai_generated, type = video.file_ref, video.seconds, True, PostType.VIDEO
        if type == PostType.VIDEO and not media_ref:
            raise Unprocessable("A video post needs a video", code="media_required")
        if type == PostType.TEXT and not text.strip():
            raise Unprocessable("A text post needs text", code="text_required")

        mislabeled = False
        if media_ref and not ai_generated and not likeness_contract_id and not author.is_agent:
            if await self.providers.moderation.detect_ai_media(media_ref):
                ai_generated, mislabeled = True, True  # API-01: unlabeled AI content gets labeled when detected

        label, badge = LabelPolicy.for_post(author, ai_generated=ai_generated, likeness=bool(likeness_contract_id))
        moderation = await self.providers.moderation.moderate_text(text) if text else None
        tagged: list[str] = []
        if tagged_handles:
            handles = [h.lstrip("@").lower() for h in tagged_handles][:10]
            rows = await self.repo(Account).list(Account.handle.in_(handles), Account.type == "company")
            hidden = await self.svc.blocks.hidden_ids(author.id)
            tagged = [r.id for r in rows if r.id not in hidden]

        post = await self.repo(Post).add(Post(
            author_account_id=author.id, type=type, text=text.strip(), media_ref=media_ref,
            duration_seconds=duration_seconds, label=label, ai_badge=badge,
            sponsored_contract_id=likeness_contract_id, api_key_id=principal.api_key_id if principal.kind == "company_key" else None,
            niche=await self._niche_of(author), tagged_company_ids=tagged,
            moderation_status=ModerationStatus.FLAGGED if moderation and moderation.flagged else ModerationStatus.VISIBLE,
        ))
        if post.moderation_status == ModerationStatus.FLAGGED:
            await self.svc.queues.enqueue(Queue.REPORTS, "post", post.id, subject_account_id=author.id,
                                          summary=f"Auto-flagged post: {moderation.reason}")
        if mislabeled:
            await self.register_mislabel(author, post.id)
        await self.cache.invalidate_prefix("feed:")
        if not likeness_contract_id:
            await self.svc.promos.scan(principal, post)
        return post

    async def register_mislabel(self, account: Account, post_id: str) -> None:
        """FD-01: AI-made content posted without the badge -> a warning, then suspension on repeat."""
        account.warnings += 1
        if account.warnings >= self.s.mislabel_strikes_before_suspension:
            await self.svc.trust.system_suspend(account, "Repeatedly posting AI-made content without the AI badge")
        else:
            await self.svc.notifications.notify(
                account.id, NotificationType.LEGAL, "Warning: AI-made content must carry the AI badge",
                "We added the badge to your post. Posting unlabeled AI content again will suspend your account.",
                {"post_id": post_id})

    async def apply_mislabel_report(self, post: Post) -> None:
        """An upheld "mislabeled AI" report: add the badge and count a strike."""
        if post.ai_badge == AiBadge.NONE and post.label == Label.HUMAN:
            post.ai_badge = AiBadge.AI_GENERATED
            await self.register_mislabel(await self.repo(Account).require(post.author_account_id), post.id)

    async def compose_suggestions(self, account: Account) -> list[dict[str, Any]]:
        """FD-01: brands and companies in my niche that I can tag or target."""
        profile = await self.svc.access.profile(account.id)
        niches = [n for n in [profile.primary_niche if profile else None, *(profile.secondary_niches if profile else [])] if n]
        if not niches:
            return []
        hidden = await self.svc.blocks.hidden_ids(account.id)
        stmt = (select(Account, CompanyProfile).join(CompanyProfile, CompanyProfile.account_id == Account.id)
                .where(CompanyProfile.category.in_(niches), CompanyProfile.verified.is_(True),
                       Account.status == AccountStatus.ACTIVE).limit(30))
        return [{"id": a.id, "handle": a.handle, "name": c.business_name, "category": c.category}
                for a, c in (await self.db.execute(stmt)).all() if a.id not in hidden]

    async def get(self, viewer: Account | None, post_id: str) -> dict[str, Any]:
        post = await self.repo(Post).require(post_id, "Post not found")
        is_author = bool(viewer and viewer.id == post.author_account_id)
        if post.moderation_status != ModerationStatus.VISIBLE and not is_author:
            raise NotFound("Post not found")
        if viewer and await self.svc.blocks.either(viewer.id, post.author_account_id):
            raise NotFound("Post not found")
        author = await self.repo(Account).require(post.author_account_id)
        liked = None
        if viewer:
            liked = await self.repo(PostLike).exists(PostLike.post_id == post.id, PostLike.account_id == viewer.id)
            await self.svc.feed.record_impressions([post.id], viewer)
        data = self.present(post, author, liked)
        await self.svc.promos.decorate(viewer, [data], "post")
        return data

    async def list_by_author(self, viewer: Account | None, author_id: str, cursor: str | None, limit: int) -> Page[dict]:
        if viewer and await self.svc.blocks.either(viewer.id, author_id):
            raise NotFound("Account not found")
        where = [Post.author_account_id == author_id]
        if not viewer or viewer.id != author_id:
            where.append(Post.moderation_status == ModerationStatus.VISIBLE)
        page = await self.repo(Post).page(*where, cursor=cursor, limit=limit)
        return Page(items=[self.present(p) for p in page.items], next_cursor=page.next_cursor)

    async def delete(self, account: Account, post_id: str) -> None:
        post = await self.repo(Post).require(post_id, "Post not found")
        if post.author_account_id != account.id:
            raise Forbidden("You can only delete your own posts")
        post.moderation_status = ModerationStatus.REMOVED
        await self.cache.invalidate_prefix("feed:")

    async def set_like(self, account: Account, post_id: str, liked: bool) -> dict[str, Any]:
        post = await self.repo(Post).require(post_id, "Post not found")
        if post.moderation_status != ModerationStatus.VISIBLE or await self.svc.blocks.either(
                account.id, post.author_account_id):
            raise NotFound("Post not found")
        existing = await self.repo(PostLike).first(PostLike.post_id == post_id, PostLike.account_id == account.id)
        column = "likes_agent" if account.is_agent else "likes_human"
        if liked and existing is None:
            await self.repo(PostLike).add(PostLike(post_id=post_id, account_id=account.id, by_agent=account.is_agent))
            setattr(post, column, getattr(post, column) + 1)
            if account.id != post.author_account_id and not account.is_agent:
                await self.svc.notifications.notify(post.author_account_id, NotificationType.LIKE,
                                                    f"@{account.handle} liked your post", data={"post_id": post.id})
        elif not liked and existing is not None:
            await self.repo(PostLike).delete(existing)
            setattr(post, column, max(0, getattr(post, column) - 1))
        return {"liked": liked, "likes": {"human": post.likes_human, "agent": post.likes_agent}}

    async def take_down_expired(self) -> int:
        """Job (LK-08): likeness videos come down 60 days after the contract ends."""
        due = await self.repo(Post).list(
            Post.takedown_due_at.is_not(None), Post.takedown_due_at <= self.now(),
            Post.moderation_status == ModerationStatus.VISIBLE)
        for post in due:
            post.moderation_status = ModerationStatus.REMOVED
        if due:
            await self.cache.invalidate_prefix("feed:")
        return len(due)

    async def take_down_contract_posts(self, contract_id: str) -> int:
        posts = await self.repo(Post).list(
            Post.sponsored_contract_id == contract_id, Post.moderation_status != ModerationStatus.REMOVED)
        for post in posts:
            post.moderation_status = ModerationStatus.REMOVED
        if posts:
            await self.cache.invalidate_prefix("feed:")
        return len(posts)


class FeedService(BaseService):
    async def feed(self, viewer: Account, tab: FeedTab | None, following_only: bool, cursor: str | None,
                   limit: int) -> tuple[Page[dict], FeedTab]:
        """FD-02: two tabs, People and Agents. An explicit choice is remembered for next time."""
        if tab is None:
            tab = FeedTab(viewer.feed_preference)
        elif viewer.feed_preference != tab:
            viewer.feed_preference = tab
        limit = max(1, min(limit, 50))
        hidden = await self.svc.blocks.hidden_ids(viewer.id)

        if following_only:
            following = await self.svc.follows.following_ids(viewer.id)
            raw = await self._query(tab, cursor, limit, list(following - hidden) or [""])
        else:
            # The shared, viewer-independent page is cached briefly; blocks are applied per viewer afterwards.
            key = f"feed:{tab}:{cursor or 'first'}:{limit}"
            raw = await self.cache.get_or_set(key, self.s.cache_ttl_feed, lambda: self._query(tab, cursor, limit, None))
        items = [dict(item) for item in raw["items"] if item["author_id"] not in hidden]
        await self.record_impressions([item["id"] for item in items], viewer)
        await self.svc.promos.decorate(viewer, items, "post")
        return Page(items=items, next_cursor=raw["next_cursor"]), tab

    async def _query(self, tab: FeedTab, cursor: str | None, limit: int, author_ids: list[str] | None) -> dict[str, Any]:
        stmt = (select(Post, Account).join(Account, Account.id == Post.author_account_id)
                .where(Post.label.in_(LabelPolicy.feed_labels(tab)),
                       Post.moderation_status == ModerationStatus.VISIBLE,
                       Account.status == AccountStatus.ACTIVE))
        if author_ids is not None:
            stmt = stmt.where(Post.author_account_id.in_(author_ids))
        last_id = Cursor.decode(cursor)
        if last_id:
            stmt = stmt.where(Post.id < last_id)
        rows = (await self.db.execute(stmt.order_by(Post.id.desc()).limit(limit + 1))).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {"items": [self.svc.posts.present(post, author) for post, author in rows],
                "next_cursor": Cursor.encode(rows[-1][0].id) if has_more and rows else None}

    async def record_impressions(self, post_ids: list[str], viewer: Account) -> None:
        """FD-06: impressions are counted separately for human and agent viewers."""
        if not post_ids:
            return
        column = Post.impressions_agent if viewer.is_agent else Post.impressions_human
        await self.db.execute(update(Post).where(Post.id.in_(post_ids)).values({column: column + 1})
                              .execution_options(synchronize_session=False))


class CommentService(BaseService):
    def present(self, comment: Comment, authors: dict[str, Account], tags: list[AgentTag] | None = None) -> dict[str, Any]:
        author = authors.get(comment.author_account_id)
        behalf = authors.get(comment.on_behalf_of_account_id) if comment.on_behalf_of_account_id else None
        deleted = comment.deleted_at is not None
        data = {
            "id": comment.id, "post_id": comment.post_id, "parent_id": comment.parent_id, "thread_id": comment.thread_id,
            "text": "[deleted]" if deleted else comment.text, "label": comment.label,
            "label_display": "Agent — Sponsored" if comment.sponsored_listing_id else LabelPolicy.display(
                comment.label, on_behalf_of_handle=behalf.handle if behalf else None),
            "author": None if author is None else {"id": author.id, "handle": author.handle, "type": author.type},
            "on_behalf_of": None if behalf is None else {"id": behalf.id, "handle": behalf.handle},
            "moderation_status": comment.moderation_status, "link": comment.link,
            "sponsored": comment.sponsored_listing_id is not None,
            "helpful": {"yes": comment.helpful_count, "no": comment.unhelpful_count},
            "created_at": comment.created_at.isoformat(), "replies": [],
        }
        if tags:
            data["agent_tags"] = [self._present_tag(tag, authors) for tag in tags]
        return data

    async def created_view(self, comment: Comment, author: Account) -> dict[str, Any]:
        """A just-created comment with the outcome of each agent tag in it."""
        tags = list(await self.repo(AgentTag).list(AgentTag.comment_id == comment.id))
        accounts = await self.repo(Account).by_ids([t.agent_account_id for t in tags] + [comment.on_behalf_of_account_id])
        return self.present(comment, {**accounts, author.id: author}, tags)

    def _present_tag(self, tag: AgentTag, accounts: dict[str, Account]) -> dict[str, Any]:
        agent = accounts.get(tag.agent_account_id)
        status, message, extra = tag.status, None, {}
        waited = (self.now() - tag.created_at).total_seconds()
        if status == TagStatus.PENDING and waited >= self.s.tag_no_response_seconds:
            status = TagStatus.NO_RESPONSE  # FD-04: no reply within 10 minutes
        if status == TagStatus.NO_RESPONSE:
            message = "No response yet"
        elif status == TagStatus.UNAVAILABLE:
            message = "This agent is no longer available"
            extra["similar_agents_url"] = f"/marketplace/agents?similar_to={tag.agent_account_id}"
        elif status == TagStatus.CAP_REACHED:
            extra["marketplace_url"] = f"/marketplace/agents/{tag.agent_account_id}"
            extra["check_other_agents_url"] = "/marketplace/agents"
        return {"agent_id": tag.agent_account_id, "handle": agent.handle if agent else None, "status": status,
                "message": message, **extra}

    # --- creation ------------------------------------------------------------------------
    async def create(self, principal: Principal, post_id: str, text: str, *, parent_id: str | None = None,
                     on_behalf_of: str | None = None, event_id: str | None = None) -> Comment:
        author = principal.account
        text = text.strip()
        if not text:
            raise Unprocessable("A comment needs text", code="text_required")
        post = await self.repo(Post).require(post_id, "Post not found")
        if post.moderation_status != ModerationStatus.VISIBLE:
            raise NotFound("Post not found")
        if await self.svc.blocks.either(author.id, post.author_account_id):
            raise Forbidden("You can't comment on this post", code="blocked")
        parent = None
        if parent_id:
            parent = await self.repo(Comment).require(parent_id, "Comment not found")
            if parent.post_id != post.id or parent.deleted_at is not None:
                raise NotFound("Comment not found")

        label = LabelPolicy.for_comment(principal)
        if principal.kind == "company_key":
            await self._guard_company_reply(principal, post)
        elif author.is_agent:
            await self._guard_agent_comment(author, post, parent, on_behalf_of)
        else:
            await self.limiter.enforce(self.policies.comments, author.id)
            on_behalf_of = None

        mentioned_agents = [] if label != Label.HUMAN else await self._mentioned_agents(text, post)
        moderation = await self.providers.moderation.moderate_text(text)
        if moderation.flagged and principal.kind == "company_key":
            # API-02: a reply that fails moderation is blocked and the API returns the reason.
            raise Unprocessable("Reply blocked by moderation", code="moderation_blocked", reason=moderation.reason)

        comment = await self.repo(Comment).add(Comment(
            post_id=post.id, parent_id=parent.id if parent else None,
            thread_id=(parent.thread_id or parent.id) if parent else None, author_account_id=author.id,
            label=label, on_behalf_of_account_id=on_behalf_of, api_key_id=principal.api_key_id if principal.kind == "company_key" else None, text=text,
            flagged_vulgar=moderation.vulgar, moderation_reason=moderation.reason,
            # FD-05: flagged replies are hidden pending review, for agents exactly as for humans.
            moderation_status=ModerationStatus.FLAGGED if moderation.flagged else ModerationStatus.VISIBLE,
        ))
        if comment.thread_id is None:
            comment.thread_id = comment.id
        # Only comments typed by people count as human engagement (it feeds pricing and thresholds).
        if label == Label.HUMAN:
            post.comments_human += 1
        else:
            post.comments_agent += 1
        if moderation.flagged:
            await self.svc.queues.enqueue(Queue.REPORTS, "comment", comment.id, subject_account_id=author.id,
                                          summary=f"Auto-flagged comment: {moderation.reason}")
            return comment

        for agent in mentioned_agents:
            await self._handle_tag(comment, agent, author)
        if author.is_agent:
            await self._mark_tags_answered(comment, author)
        await self._emit_reply_events(comment, post, parent, author, event_id)
        await self.svc.promos.scan(principal, post, comment)
        return comment

    async def _guard_company_reply(self, principal: Principal, post: Post) -> None:
        owns = post.author_account_id == principal.id
        if not owns and post.sponsored_contract_id:
            contract = await self.repo(Contract).get(post.sponsored_contract_id)
            owns = bool(contract and contract.hirer_id == principal.id)
        if not owns:
            raise Forbidden("The comment API works on your own posts and posts made on your behalf", code="not_your_post")
        await self.limiter.enforce(self.policies.api_replies, principal.api_key_id)  # API-02: 600/hour per key

    async def _guard_agent_comment(self, agent: Account, post: Post, parent: Comment | None,
                                   on_behalf_of: str | None) -> None:
        owner_profile = await self.svc.access.profile(post.author_account_id)
        if on_behalf_of:
            await self._guard_auto_reply(agent, post, owner_profile, on_behalf_of)
        elif owner_profile and post.author_account_id != agent.id:
            # FD-03: the creator can block all agent comments, or allow only agents they follow.
            policy = owner_profile.agent_comment_policy
            if policy == AgentCommentPolicy.NONE or (policy == AgentCommentPolicy.FOLLOWED and not
                                                     await self.svc.follows.is_following(post.author_account_id, agent.id)):
                raise Forbidden("This creator does not allow agent comments", code="agent_comments_off")
        if parent is not None and parent.label == Label.AGENT and parent.author_account_id != agent.id:
            # FD-05: an agent replies to another agent at most 3 times per thread.
            agent_parents = select(Comment.id).where(Comment.thread_id == parent.thread_id, Comment.label == Label.AGENT,
                                                     Comment.author_account_id != agent.id)
            replies = await self.repo(Comment).count(
                Comment.thread_id == parent.thread_id, Comment.author_account_id == agent.id,
                Comment.parent_id.in_(agent_parents))
            if replies >= self.s.agent_to_agent_replies_per_thread:
                raise RateLimited("Agent-to-agent reply limit reached for this thread", retry_after=3600,
                                  limit=self.s.agent_to_agent_replies_per_thread, reset_at=0, code="agent_loop_limit")
        await self.limiter.enforce(self.policies.agent_comments, agent.id,
                                   "Agents can post at most 30 comments per hour")

    async def _guard_auto_reply(self, agent: Account, post: Post, owner_profile: UserProfile | None, on_behalf_of: str) -> None:
        """AG-05: only the influencer's chosen agent, only while subscribed, never on "no AI replies" posts."""
        if on_behalf_of != post.author_account_id or owner_profile is None:
            raise Forbidden("Auto-replies are only for the post owner's own posts", code="not_auto_reply_agent")
        if not owner_profile.auto_reply_enabled or owner_profile.auto_reply_agent_id != agent.id:
            raise Forbidden("You are not this account's auto-reply agent", code="not_auto_reply_agent")
        if not await self.auto_reply_allowed(post, owner_profile):
            raise Forbidden("Auto-replies are off for this post", code="auto_reply_off")

    async def auto_reply_allowed(self, post: Post, owner_profile: UserProfile) -> bool:
        if owner_profile.is_minor or not owner_profile.auto_reply_enabled:
            return False
        if not await self.svc.access.is_subscribed(owner_profile.account_id):
            return False
        if post.sponsored_contract_id:
            # Each brand's "no AI replies" flag applies only to its own sponsored posts.
            contract = await self.repo(Contract).get(post.sponsored_contract_id)
            if contract and contract.no_ai_replies:
                return False
        return True

    async def _mentioned_agents(self, text: str, post: Post) -> list[Account]:
        handles = extract_mentions(text)
        if not handles:
            return []
        agents = await self.repo(Account).list(Account.handle.in_(handles), Account.type == "agent")
        if len(agents) > self.s.max_agent_tags_per_comment:
            raise Unprocessable(f"You can tag at most {self.s.max_agent_tags_per_comment} agents per comment",
                                code="too_many_agent_tags")
        if agents:
            owner_profile = await self.svc.access.profile(post.author_account_id)
            if owner_profile and owner_profile.agent_comment_policy == AgentCommentPolicy.NONE:
                raise Forbidden("This creator does not allow agent comments", code="agent_comments_off")
        return list(agents)

    async def _handle_tag(self, comment: Comment, agent: Account, tagger: Account) -> AgentTag:
        """FD-04: route a free tag to the agent, or answer with the right fallback."""
        tag = AgentTag(comment_id=comment.id, thread_id=comment.thread_id, agent_account_id=agent.id, tagged_by=tagger.id)
        profile = await self.repo(AgentProfile).first(AgentProfile.account_id == agent.id)
        integration = await self.repo(AgentIntegration).first(AgentIntegration.agent_account_id == agent.id)
        if agent.status != AccountStatus.ACTIVE or profile is None:
            tag.status = TagStatus.UNAVAILABLE
            return await self.repo(AgentTag).add(tag)

        live = integration is not None and integration.status == IntegrationStatus.LIVE and not profile.paused
        within_cap = False
        if live:
            day_key = f"freetag:{agent.id}:{self.clock.today().isoformat()}"
            used = await self.kv.incr(day_key, ttl=2 * 86400)
            within_cap = used <= profile.free_tag_daily_cap
            if not within_cap:
                await self.kv.incr(day_key, -1)
        if not within_cap:
            # Paused agents and agents over their daily cap give the "not available" reply, once per thread.
            tag.status = TagStatus.CAP_REACHED
            already = await self.repo(Comment).exists(
                Comment.thread_id == comment.thread_id, Comment.author_account_id == agent.id,
                Comment.system_generated.is_(True))
            if not already:
                await self.repo(Comment).add(Comment(
                    post_id=comment.post_id, parent_id=comment.id, thread_id=comment.thread_id,
                    author_account_id=agent.id, label=Label.AGENT, text=FREE_TAG_UNAVAILABLE,
                    system_generated=True, link=f"/marketplace/agents/{agent.id}"))
                post = await self.repo(Post).require(comment.post_id)
                post.comments_agent += 1
            return await self.repo(AgentTag).add(tag)

        event = await self.svc.webhooks.emit(agent.id, EventType.TAG_CREATED, {
            "post_id": comment.post_id, "comment_id": comment.id, "thread_id": comment.thread_id,
            "tagged_by": {"id": tagger.id, "handle": tagger.handle},
            "comment_text": self.svc.webhooks.untrusted(comment.text),
        })
        tag.event_id = event.id
        return await self.repo(AgentTag).add(tag)

    async def _mark_tags_answered(self, comment: Comment, agent: Account) -> None:
        pending = await self.repo(AgentTag).list(
            AgentTag.thread_id == comment.thread_id, AgentTag.agent_account_id == agent.id,
            AgentTag.status.in_([TagStatus.PENDING, TagStatus.NO_RESPONSE]))
        if not pending:
            return
        for tag in pending:
            tag.status = TagStatus.REPLIED
        profile = await self.repo(AgentProfile).first(AgentProfile.account_id == agent.id)
        if profile:
            profile.free_tags_answered += len(pending)

    async def _emit_reply_events(self, comment: Comment, post: Post, parent: Comment | None, author: Account,
                                 event_id: str | None) -> None:
        untrusted = self.svc.webhooks.untrusted(comment.text)
        if parent and parent.label == Label.AGENT and parent.author_account_id != author.id and not parent.system_generated:
            parent_author = await self.repo(Account).get(parent.author_account_id)
            if parent_author and parent_author.is_agent:
                await self.svc.webhooks.emit(parent_author.id, EventType.COMMENT_REPLY, {
                    "post_id": post.id, "comment_id": comment.id, "parent_comment_id": parent.id,
                    "thread_id": comment.thread_id, "from": {"id": author.id, "handle": author.handle,
                                                             "label": comment.label},
                    "comment_text": untrusted})
        if comment.label == Label.HUMAN and author.id != post.author_account_id:
            owner_profile = await self.svc.access.profile(post.author_account_id)
            if owner_profile and owner_profile.auto_reply_agent_id and await self.auto_reply_allowed(post, owner_profile):
                await self.svc.webhooks.emit(owner_profile.auto_reply_agent_id, EventType.AUTOREPLY_COMMENT, {
                    "post_id": post.id, "comment_id": comment.id, "thread_id": comment.thread_id,
                    "on_behalf_of": post.author_account_id,
                    "from": {"id": author.id, "handle": author.handle}, "comment_text": untrusted})
            await self.svc.notifications.notify(post.author_account_id, NotificationType.COMMENT,
                                                f"@{author.handle} commented on your post",
                                                data={"post_id": post.id, "comment_id": comment.id})
        if parent and parent.author_account_id not in (author.id, post.author_account_id):
            parent_author = await self.repo(Account).get(parent.author_account_id)
            if parent_author and not parent_author.is_agent:
                await self.svc.notifications.notify(parent.author_account_id, NotificationType.COMMENT,
                                                    f"@{author.handle} replied to your comment",
                                                    data={"post_id": post.id, "comment_id": comment.id})

    # --- reading and management ------------------------------------------------------------
    async def list_for_post(self, viewer: Account | None, post_id: str, sort: str, cursor: str | None,
                            limit: int) -> Page[dict]:
        """Top-level comments are paginated; every reply to the returned roots is nested under them."""
        post = await self.repo(Post).require(post_id, "Post not found")
        if viewer and await self.svc.blocks.either(viewer.id, post.author_account_id):
            raise NotFound("Post not found")
        hidden = await self.svc.blocks.hidden_ids(viewer.id if viewer else None)
        viewer_id = viewer.id if viewer else ""
        visible = (Comment.moderation_status == ModerationStatus.VISIBLE) | (Comment.author_account_id == viewer_id)
        roots = await self.repo(Comment).page(
            Comment.post_id == post_id, Comment.parent_id.is_(None), visible,
            cursor=cursor, limit=min(limit, 100), newest_first=sort != "old")
        root_ids = [c.id for c in roots.items]
        replies = await self.repo(Comment).list(
            Comment.thread_id.in_(root_ids or [""]), Comment.parent_id.is_not(None), visible) if root_ids else []
        everything = [c for c in [*roots.items, *replies] if c.author_account_id not in hidden]
        tags = await self.repo(AgentTag).list(AgentTag.comment_id.in_([c.id for c in everything] or [""]))
        tags_by_comment: dict[str, list[AgentTag]] = {}
        for tag in tags:
            tags_by_comment.setdefault(tag.comment_id, []).append(tag)
        accounts = await self.repo(Account).by_ids(
            [c.author_account_id for c in everything] + [c.on_behalf_of_account_id for c in everything]
            + [t.agent_account_id for t in tags])
        nodes = {c.id: self.present(c, accounts, tags_by_comment.get(c.id)) for c in everything}
        await self.svc.promos.decorate(viewer, list(nodes.values()), "comment")
        for comment in everything:
            if comment.parent_id and comment.parent_id in nodes:
                nodes[comment.parent_id]["replies"].append(nodes[comment.id])
            elif comment.parent_id and comment.thread_id in nodes:
                nodes[comment.thread_id]["replies"].append(nodes[comment.id])
        return Page(items=[nodes[c.id] for c in roots.items if c.id in nodes], next_cursor=roots.next_cursor)

    async def delete(self, account: Account, comment_id: str) -> None:
        """Authors delete their own comments; an influencer can delete any reply made on their behalf."""
        comment = await self.repo(Comment).require(comment_id, "Comment not found")
        if account.id not in (comment.author_account_id, comment.on_behalf_of_account_id):
            raise Forbidden("You can't delete this comment")
        if comment.deleted_at is None:
            comment.deleted_at = self.now()

    async def rate(self, account: Account, comment_id: str, helpful: bool) -> dict[str, Any]:
        """Humans rate agent comments as helpful or not (success metric)."""
        comment = await self.repo(Comment).require(comment_id, "Comment not found")
        if comment.label != Label.AGENT:
            raise Unprocessable("Only agent comments can be rated", code="not_agent_comment")
        if account.is_agent:
            raise Forbidden("Only humans can rate agent comments", code="humans_only")
        rating = await self.repo(CommentRating).first(
            CommentRating.comment_id == comment_id, CommentRating.account_id == account.id)
        if rating is None:
            await self.repo(CommentRating).add(CommentRating(comment_id=comment_id, account_id=account.id, helpful=helpful))
        elif rating.helpful != helpful:
            if rating.helpful:
                comment.helpful_count -= 1
            else:
                comment.unhelpful_count -= 1
            rating.helpful = helpful
        else:
            return {"helpful": {"yes": comment.helpful_count, "no": comment.unhelpful_count}}
        if helpful:
            comment.helpful_count += 1
        else:
            comment.unhelpful_count += 1
        return {"helpful": {"yes": comment.helpful_count, "no": comment.unhelpful_count}}


class AnalyticsService(BaseService):
    """FD-06: every metric is split into human and agent counts; only human counts feed pricing."""

    async def creator(self, account: Account, days: int) -> dict[str, Any]:
        since = self.now() - timedelta(days=max(1, min(days, 365)))
        totals = (await self.db.execute(
            select(func.count(Post.id),
                   func.coalesce(func.sum(Post.impressions_human), 0), func.coalesce(func.sum(Post.impressions_agent), 0),
                   func.coalesce(func.sum(Post.likes_human), 0), func.coalesce(func.sum(Post.likes_agent), 0),
                   func.coalesce(func.sum(Post.comments_human), 0), func.coalesce(func.sum(Post.comments_agent), 0))
            .where(Post.author_account_id == account.id, Post.created_at >= since))).one()
        growth = dict((await self.db.execute(
            select(Follow.by_agent, func.count()).where(Follow.followed_id == account.id, Follow.created_at >= since)
            .group_by(Follow.by_agent))).all())
        posts, imp_h, imp_a, likes_h, likes_a, com_h, com_a = (int(v) for v in totals)
        return {
            "period_days": days, "posts": posts,
            "impressions": {"human": imp_h, "agent": imp_a},
            "engagement": {"human": likes_h + com_h, "agent": likes_a + com_a},
            "likes": {"human": likes_h, "agent": likes_a},
            "comments": {"human": com_h, "agent": com_a},
            "follower_growth": {"human": int(growth.get(False, 0)), "agent": int(growth.get(True, 0))},
            "followers": {"human": account.followers_human, "agent": account.followers_agent},
            "human_engagement_rate": round((likes_h + com_h) / imp_h, 4) if imp_h else 0.0,
            "note": "Only human followers and human engagement count toward pricing and thresholds.",
            # Wamiwa Agent results (cards, replies, suggested sellers), when the account has a plan.
            "wamiwa_agent": await self.svc.promos.analytics(account, days),
        }

    async def human_engagement_rate(self, account_id: str) -> float:
        row = (await self.db.execute(
            select(func.coalesce(func.sum(Post.impressions_human), 0),
                   func.coalesce(func.sum(Post.likes_human + Post.comments_human), 0))
            .where(Post.author_account_id == account_id))).one()
        return float(row[1]) / float(row[0]) if row[0] else 0.0


class PostIdeaService(BaseService):
    TEMPLATES = (
        "Share one lesson you learned this week about {niche}.",
        "Answer the question you get asked most about {niche}.",
        "Show a behind-the-scenes moment from your {niche} work.",
        "Bust a common myth about {niche}.",
        "Recommend a tool or habit that changed how you approach {niche}.",
        "Tell the story of your first win in {niche}.",
        "Compare two popular options in {niche} and pick a side.",
    )

    def _ideas(self, niche: str, day_ordinal: int) -> list[str]:
        start = day_ordinal % len(self.TEMPLATES)
        return [self.TEMPLATES[(start + i) % len(self.TEMPLATES)].format(niche=niche) for i in range(3)]

    async def today(self, account: Account) -> dict[str, Any]:
        """FD-07. After a lapse, ideas stop from the next day: nothing new is generated."""
        day = self.clock.today()
        existing = await self.repo(PostIdea).first(PostIdea.account_id == account.id, PostIdea.day == day)
        if existing is None:
            await self.svc.access.require_subscription(account, "Daily post ideas")
            existing = await self._generate(account, day)
        return {"day": day.isoformat(), "ideas": existing.ideas}

    async def _generate(self, account: Account, day) -> PostIdea:
        profile = await self.svc.access.profile(account.id)
        niche = (profile.primary_niche if profile else None) or "your niche"
        return await self.repo(PostIdea).add(PostIdea(account_id=account.id, day=day, ideas=self._ideas(niche, day.toordinal())))

    async def generate_daily(self) -> int:
        """Job: ideas for every subscribed creator, in-app and by email when enabled."""
        from app.models import Subscription

        day, count = self.clock.today(), 0
        active = await self.repo(Subscription).list(Subscription.status != "lapsed", Subscription.renews_at > self.now())
        for subscription in active:
            if await self.repo(PostIdea).exists(PostIdea.account_id == subscription.account_id, PostIdea.day == day):
                continue
            account = await self.repo(Account).get(subscription.account_id)
            if account is None or not account.is_active:
                continue
            idea = await self._generate(account, day)
            await self.svc.notifications.notify(account.id, NotificationType.POST_IDEAS, "Your post ideas for today",
                                                "\n".join(idea.ideas), {"day": day.isoformat()})
            count += 1
        return count
