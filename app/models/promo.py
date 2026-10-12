"""Wamiwa Agent (Epic 15): paid smart mentions, sponsored replies and suggested sellers."""
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.accounts import FK_ACCOUNT
from app.models.base import Model, UTCDateTime


class PromoListing(Model):
    """One paid Wamiwa Agent plan per account."""

    __tablename__ = "promo_listings"

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), unique=True)
    kind: Mapped[str] = mapped_column(String(10))  # business | seller | artist | label
    status: Mapped[str] = mapped_column(String(16), index=True)  # pending_review | active | paused | lapsed | rejected
    tier: Mapped[str] = mapped_column(String(8), default="base")  # base | plus (more links + trend ideas)
    target_regions: Mapped[list] = mapped_column(JSON, default=list)  # where the owner wants to be seen
    category: Mapped[str | None] = mapped_column(String(64), index=True)
    renews_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    payment_method: Mapped[str] = mapped_column(String(64))
    last_payment_id: Mapped[str | None] = mapped_column(String(32))


class PromoName(Model):
    """A name that triggers a listing's card: a brand, a seller's shop name, an artist or a label's artist."""

    __tablename__ = "promo_names"

    listing_id: Mapped[str] = mapped_column(ForeignKey("promo_listings.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    normalized: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)  # pending | approved | rejected
    plain_text_match: Mapped[bool] = mapped_column(Boolean, default=True)  # False: needs @, quotes or a cue word
    # Artists: the profile found on Spotify, YouTube Music or Audiomack, used when no link was added.
    default_link_url: Mapped[str | None] = mapped_column(String(500))
    default_link_label: Mapped[str | None] = mapped_column(String(60))
    default_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    primary: Mapped[bool] = mapped_column(Boolean, default=False)


class PromoLink(Model):
    """A link the owner wants the Wamiwa Agent to share. Saved links outlive a lapsed plan."""

    __tablename__ = "promo_links"

    listing_id: Mapped[str] = mapped_column(ForeignKey("promo_listings.id"), index=True)
    name_id: Mapped[str | None] = mapped_column(String(32), index=True)  # labels: the artist this link is for
    url: Mapped[str] = mapped_column(String(500))
    label: Mapped[str] = mapped_column(String(60))  # e.g. "Spotify", "New single", "Shop"
    keywords: Mapped[list] = mapped_column(JSON, default=list)  # words that make this link the right one to share
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)  # suspected scam: not shared
    flag_reason: Mapped[str | None] = mapped_column(String(300))
    reports: Mapped[int] = mapped_column(Integer, default=0)
    cleared_reasons: Mapped[str | None] = mapped_column(String(300))  # what an admin has already judged safe


class PromoMatch(Model):
    """A card or suggestion attached to one post or comment when it was written."""

    __tablename__ = "promo_matches"

    target_type: Mapped[str] = mapped_column(String(8))  # post | comment
    target_id: Mapped[str] = mapped_column(String(32), index=True)
    post_id: Mapped[str] = mapped_column(String(32), index=True)
    thread_id: Mapped[str | None] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # mention | suggestion
    listing_id: Mapped[str | None] = mapped_column(String(32), index=True)
    name_id: Mapped[str | None] = mapped_column(String(32))
    category: Mapped[str | None] = mapped_column(String(64))
    start: Mapped[int] = mapped_column(Integer, default=0)
    end: Mapped[int] = mapped_column(Integer, default=0)


class PromoDailyStat(Model):
    __tablename__ = "promo_daily_stats"
    __table_args__ = (UniqueConstraint("listing_id", "name_id", "day"),)

    listing_id: Mapped[str] = mapped_column(String(32), index=True)
    name_id: Mapped[str] = mapped_column(String(32), default="")  # "" = not tied to one name (suggested list)
    day: Mapped[date] = mapped_column(Date, index=True)
    card_impressions: Mapped[int] = mapped_column(Integer, default=0)
    card_opens: Mapped[int] = mapped_column(Integer, default=0)
    link_clicks: Mapped[int] = mapped_column(Integer, default=0)
    agent_replies: Mapped[int] = mapped_column(Integer, default=0)
    list_impressions: Mapped[int] = mapped_column(Integer, default=0)
    list_clicks: Mapped[int] = mapped_column(Integer, default=0)


class PromoCategoryKeyword(Model):
    """Admin-maintained words that point a wish to buy at a category ("shirt" -> fashion)."""

    __tablename__ = "promo_category_keywords"

    category: Mapped[str] = mapped_column(String(64), index=True)
    keyword: Mapped[str] = mapped_column(String(64), unique=True)
    updated_by: Mapped[str] = mapped_column(String(32))


class PromoClaim(Model):
    """ "This is my name": someone says a name on another plan belongs to them."""

    __tablename__ = "promo_claims"

    name_id: Mapped[str] = mapped_column(String(32), index=True)
    normalized: Mapped[str] = mapped_column(String(80))
    claimant_account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    evidence: Mapped[str] = mapped_column(String(1000), default="")
    froze_name: Mapped[bool] = mapped_column(Boolean, default=False)  # the claimant could already show it is theirs
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)  # open | upheld | dismissed
