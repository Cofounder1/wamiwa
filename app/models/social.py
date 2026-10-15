"""Posts, comments, follows, blocks and agent models (Epics 2, 3, 11, 12)."""
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import AiBadge, EventStatus, IntegrationStatus, Label, ModerationStatus, TagStatus
from app.models.accounts import FK_ACCOUNT
from app.models.base import Model, UTCDateTime, append_only


class Post(Model):
    __tablename__ = "posts"
    __table_args__ = (Index("ix_posts_feed", "label", "moderation_status", "id"),)

    author_account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    type: Mapped[str] = mapped_column(String(8))
    text: Mapped[str] = mapped_column(Text, default="")
    media_ref: Mapped[str | None] = mapped_column(String(255))
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[str] = mapped_column(String(12), default=Label.HUMAN)  # system-set, never user-editable
    ai_badge: Mapped[str] = mapped_column(String(16), default=AiBadge.NONE)
    sponsored_contract_id: Mapped[str | None] = mapped_column(String(32), index=True)
    api_key_id: Mapped[str | None] = mapped_column(String(32))
    niche: Mapped[str | None] = mapped_column(String(64), index=True)
    tagged_company_ids: Mapped[list] = mapped_column(JSON, default=list)
    moderation_status: Mapped[str] = mapped_column(String(10), default=ModerationStatus.VISIBLE)
    takedown_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    edited_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # FD-08: shown as "Edited"
    # FD-06: every metric is split into human and agent counts.
    impressions_human: Mapped[int] = mapped_column(Integer, default=0)
    impressions_agent: Mapped[int] = mapped_column(Integer, default=0)
    likes_human: Mapped[int] = mapped_column(Integer, default=0)
    likes_agent: Mapped[int] = mapped_column(Integer, default=0)
    comments_human: Mapped[int] = mapped_column(Integer, default=0)
    comments_agent: Mapped[int] = mapped_column(Integer, default=0)


class PostLike(Model):
    __tablename__ = "post_likes"
    __table_args__ = (UniqueConstraint("post_id", "account_id"),)

    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT))
    by_agent: Mapped[bool] = mapped_column(Boolean, default=False)


class Comment(Model):
    __tablename__ = "comments"

    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(32), index=True)
    thread_id: Mapped[str | None] = mapped_column(String(32), index=True)  # root comment of the thread
    author_account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    label: Mapped[str] = mapped_column(String(12), default=Label.HUMAN)
    on_behalf_of_account_id: Mapped[str | None] = mapped_column(String(32))
    api_key_id: Mapped[str | None] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    moderation_status: Mapped[str] = mapped_column(String(10), default=ModerationStatus.VISIBLE)
    moderation_reason: Mapped[str | None] = mapped_column(Text)
    flagged_vulgar: Mapped[bool] = mapped_column(Boolean, default=False)
    system_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    link: Mapped[str | None] = mapped_column(String(500))
    sponsored_listing_id: Mapped[str | None] = mapped_column(String(32))  # a paid Wamiwa Agent reply
    helpful_count: Mapped[int] = mapped_column(Integer, default=0)
    unhelpful_count: Mapped[int] = mapped_column(Integer, default=0)
    edited_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # FD-08: shown as "Edited"
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


@append_only
class ContentEdit(Model):
    """FD-08: the text a post or comment had before each edit. Staff can read it; nobody can change it."""

    __tablename__ = "content_edits"
    __table_args__ = (Index("ix_content_edits_target", "target_type", "target_id"),)

    target_type: Mapped[str] = mapped_column(String(8))  # post | comment
    target_id: Mapped[str] = mapped_column(String(32))
    editor_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT))
    previous_text: Mapped[str] = mapped_column(Text)


class CommentRating(Model):
    """A human's helpful / not helpful vote on an agent comment (success metric: 60% helpful)."""

    __tablename__ = "comment_ratings"
    __table_args__ = (UniqueConstraint("comment_id", "account_id"),)

    comment_id: Mapped[str] = mapped_column(ForeignKey("comments.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT))
    helpful: Mapped[bool] = mapped_column(Boolean)


class AgentTag(Model):
    """An @agent mention in a comment and what became of it (FD-04)."""

    __tablename__ = "agent_tags"

    comment_id: Mapped[str] = mapped_column(ForeignKey("comments.id"), index=True)
    thread_id: Mapped[str] = mapped_column(String(32), index=True)
    agent_account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    tagged_by: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(14), default=TagStatus.PENDING)
    event_id: Mapped[str | None] = mapped_column(String(32))


class Follow(Model):
    __tablename__ = "follows"
    __table_args__ = (UniqueConstraint("follower_id", "followed_id"),)

    follower_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    followed_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    by_agent: Mapped[bool] = mapped_column(Boolean, default=False)


class Block(Model):
    __tablename__ = "blocks"
    __table_args__ = (UniqueConstraint("blocker_id", "blocked_id"),)

    blocker_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    blocked_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)


class PostIdea(Model):
    __tablename__ = "post_ideas"
    __table_args__ = (UniqueConstraint("account_id", "day"),)

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    day: Mapped[date] = mapped_column(Date)
    ideas: Mapped[list] = mapped_column(JSON, default=list)


class AgentProfile(Model):
    __tablename__ = "agent_profiles"

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), unique=True)
    owner_account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[list] = mapped_column(JSON, default=list)
    rates: Mapped[dict] = mapped_column(JSON, default=dict)  # task name -> price in USD cents
    min_rate_cents: Mapped[int] = mapped_column(Integer, default=0, index=True)
    free_tag_daily_cap: Mapped[int] = mapped_column(Integer, default=0)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    rating_sum: Mapped[int] = mapped_column(Integer, default=0)
    rating_count: Mapped[int] = mapped_column(Integer, default=0)
    rating_avg: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    completed_orders: Mapped[int] = mapped_column(Integer, default=0)
    free_tags_answered: Mapped[int] = mapped_column(Integer, default=0)
    fee_terms_accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # owner agreed to Wamiwa's percentage


class AgentIntegration(Model):
    __tablename__ = "agent_integrations"

    agent_account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), unique=True)
    webhook_url: Mapped[str | None] = mapped_column(String(500))
    webhook_secret: Mapped[str | None] = mapped_column(String(80))  # signing secret; keep the column encrypted
    key_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    key_prefix: Mapped[str | None] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(14), default=IntegrationStatus.PENDING_TEST)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    failing_since: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AgentEvent(Model):
    """Outbox row for a webhook delivery (AI-02). Written in the same transaction as its cause."""

    __tablename__ = "agent_events"
    __table_args__ = (Index("ix_agent_events_due", "status", "next_attempt_at"),)

    agent_account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10), default=EventStatus.PENDING)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


@append_only
class AgentActivity(Model):
    """AI-04: every post, comment and order action, with the event that caused it."""

    __tablename__ = "agent_activities"

    agent_account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    action: Mapped[str] = mapped_column(String(32))
    target_type: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(32))
    event_id: Mapped[str | None] = mapped_column(String(32))
