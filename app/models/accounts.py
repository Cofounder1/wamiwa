"""Accounts, profiles, guardians and verification (Epic 1, Epic 7 company profile)."""
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import (
    AccountStatus, AccountType, AdminRole, AgentCommentPolicy, FeedTab, GuardianLinkStatus,
    PopularityLevel, VerificationStatus,
)
from app.models.base import Model, UTCDateTime

FK_ACCOUNT = "accounts.id"


class Account(Model):
    """Base row for every role: user, company, agent and admin."""

    __tablename__ = "accounts"

    type: Mapped[str] = mapped_column(String(16), index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    handle: Mapped[str] = mapped_column(String(30), unique=True)
    display_name: Mapped[str] = mapped_column(String(120), default="", index=True)
    phone: Mapped[str | None] = mapped_column(String(20))
    phone_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    phone_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default=AccountStatus.ACTIVE, index=True)
    admin_role: Mapped[str | None] = mapped_column(String(16))
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    feed_preference: Mapped[str] = mapped_column(String(10), default=FeedTab.PEOPLE)
    region: Mapped[str | None] = mapped_column(String(64), index=True)
    followers_human: Mapped[int] = mapped_column(Integer, default=0)
    followers_agent: Mapped[int] = mapped_column(Integer, default=0)
    following_count: Mapped[int] = mapped_column(Integer, default=0)
    report_weight: Mapped[float] = mapped_column(Float, default=1.0)
    false_reports: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[int] = mapped_column(Integer, default=0)
    status_before_hold: Mapped[str | None] = mapped_column(String(20))
    suspended_reason: Mapped[str | None] = mapped_column(Text)
    suspended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    appeal_used: Mapped[bool] = mapped_column(Boolean, default=False)
    promo_opt_out: Mapped[bool] = mapped_column(Boolean, default=False)  # no sponsored cards on my content
    deletion_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_active_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)

    @property
    def is_user(self) -> bool:
        return self.type == AccountType.USER

    @property
    def is_company(self) -> bool:
        return self.type == AccountType.COMPANY

    @property
    def is_agent(self) -> bool:
        return self.type == AccountType.AGENT

    @property
    def is_admin(self) -> bool:
        return self.type == AccountType.ADMIN

    @property
    def is_super_admin(self) -> bool:
        return self.is_admin and self.admin_role == AdminRole.SUPER_ADMIN

    @property
    def is_active(self) -> bool:
        return self.status == AccountStatus.ACTIVE

    @property
    def is_visible(self) -> bool:
        """Shown in search, feeds and profiles."""
        return self.status in (AccountStatus.ACTIVE, AccountStatus.FROZEN, AccountStatus.PENDING_DELETION)


class UserProfile(Model):
    __tablename__ = "user_profiles"

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), unique=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    verified_name: Mapped[str | None] = mapped_column(String(120))
    date_of_birth: Mapped[date] = mapped_column(Date)
    is_minor: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    converted_to_adult_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    photo_ref: Mapped[str | None] = mapped_column(String(255))
    bio: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str | None] = mapped_column(String(120), index=True)
    primary_niche: Mapped[str | None] = mapped_column(String(64), index=True)
    secondary_niches: Mapped[list] = mapped_column(JSON, default=list)
    links: Mapped[list] = mapped_column(JSON, default=list)
    # Verification (ON-05, ON-07, ON-07a)
    id_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    id_attempts: Mapped[int] = mapped_column(Integer, default=0)
    face_ref: Mapped[str | None] = mapped_column(String(255))
    is_verified_creator: Mapped[bool] = mapped_column(Boolean, default=False)
    is_major_person: Mapped[bool] = mapped_column(Boolean, default=False)
    popularity_level: Mapped[str] = mapped_column(String(12), default=PopularityLevel.NONE)
    popularity_review_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    popularity_last_appeal_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    new_orders_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    ranking_penalty: Mapped[bool] = mapped_column(Boolean, default=False)
    # Preferences
    agent_comment_policy: Mapped[str] = mapped_column(String(10), default=AgentCommentPolicy.ALL)
    notification_prefs: Mapped[dict] = mapped_column(JSON, default=dict)
    daily_digest: Mapped[bool] = mapped_column(Boolean, default=False)
    read_receipts: Mapped[bool] = mapped_column(Boolean, default=True)
    typing_indicators: Mapped[bool] = mapped_column(Boolean, default=True)
    # AG-05 auto-reply agent
    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_reply_agent_id: Mapped[str | None] = mapped_column(String(32))


class CompanyProfile(Model):
    """Company profile plus its messaging and discovery settings (CO-01, CO-03, CO-04)."""

    __tablename__ = "company_profiles"

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), unique=True)
    business_name: Mapped[str] = mapped_column(String(160), index=True)
    registration_no: Mapped[str | None] = mapped_column(String(64))
    document_ref: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(64), index=True)
    bio: Mapped[str] = mapped_column(Text, default="")
    website: Mapped[str | None] = mapped_column(String(500))
    verified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    verification_status: Mapped[str | None] = mapped_column(String(16))
    allow_user_messages: Mapped[bool] = mapped_column(Boolean, default=False)
    b2b_opt_out: Mapped[bool] = mapped_column(Boolean, default=False)
    region_targeting: Mapped[str | None] = mapped_column(String(64))
    b2b_spam_reports: Mapped[int] = mapped_column(Integer, default=0)
    b2b_banned_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    off_platform_warnings: Mapped[int] = mapped_column(Integer, default=0)


class GuardianLink(Model):
    __tablename__ = "guardian_links"

    minor_account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    guardian_account_id: Mapped[str | None] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    invite_contact: Mapped[str] = mapped_column(String(255))
    invite_token_hash: Mapped[str] = mapped_column(String(64), index=True)
    relationship: Mapped[str | None] = mapped_column(String(40))
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(24), default=GuardianLinkStatus.INVITED, index=True)


class GuardianCompanyApproval(Model):
    """A guardian's approval for one company to DM and send offers to one minor (HM-02)."""

    __tablename__ = "guardian_company_approvals"
    __table_args__ = (UniqueConstraint("minor_account_id", "company_account_id"),)

    guardian_account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT))
    minor_account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    company_account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT))


class IdentityVerification(Model):
    __tablename__ = "identity_verifications"

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    document_type: Mapped[str] = mapped_column(String(20))
    document_ref: Mapped[str] = mapped_column(String(255))  # encrypted object-store reference
    selfie_ref: Mapped[str] = mapped_column(String(255))  # a frame from the live camera check
    liveness_ref: Mapped[str | None] = mapped_column(String(255))  # the clip of the head turns
    status: Mapped[str] = mapped_column(String(16), default=VerificationStatus.PENDING)
    liveness_result: Mapped[bool | None] = mapped_column(Boolean)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    verified_name: Mapped[str | None] = mapped_column(String(120))
    verified_dob: Mapped[date | None] = mapped_column(Date)
    reviewed_by: Mapped[str | None] = mapped_column(String(32))


class PublicFigureVerification(Model):
    __tablename__ = "public_figure_verifications"

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    method: Mapped[str] = mapped_column(String(20))
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default=VerificationStatus.PENDING)
    reviewed_by: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)


class PopularitySignal(Model):
    """One piece of evidence on a popularity rating card (ON-07a)."""

    __tablename__ = "popularity_signals"

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    category: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(200))
    evidence_ref: Mapped[str] = mapped_column(String(500))  # never exposed on the card
    status: Mapped[str] = mapped_column(String(16), default=VerificationStatus.PENDING)
    reviewed_by: Mapped[str | None] = mapped_column(String(32))
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_second_review: Mapped[bool] = mapped_column(Boolean, default=False)


class ExternalSocialLink(Model):
    __tablename__ = "external_social_links"
    __table_args__ = (UniqueConstraint("account_id", "platform"),)

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    platform: Mapped[str] = mapped_column(String(16))
    handle: Mapped[str] = mapped_column(String(120))
    external_id: Mapped[str | None] = mapped_column(String(160), index=True)
    follower_count: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime] = mapped_column(UTCDateTime)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    connected: Mapped[bool] = mapped_column(Boolean, default=True)
    friend_ids: Mapped[list] = mapped_column(JSON, default=list)


class BankAccount(Model):
    __tablename__ = "bank_accounts"

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), unique=True)
    bank_code: Mapped[str] = mapped_column(String(16))
    account_number: Mapped[str] = mapped_column(String(20))
    account_name: Mapped[str] = mapped_column(String(160))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)  # name matches the verified ID
    hold_until: Mapped[datetime | None] = mapped_column(UTCDateTime)


class FriendInvite(Model):
    """ON-08: one invite per contact, keyed by a hash so non-users' numbers are never stored."""

    __tablename__ = "friend_invites"
    __table_args__ = (UniqueConstraint("inviter_account_id", "contact_hash"),)

    inviter_account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    contact_hash: Mapped[str] = mapped_column(String(64))
