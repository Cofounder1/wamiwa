"""Messaging, reports, admin, queues, support, notifications and company API keys."""
from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ChatTab, ReportStatus, TicketStatus
from app.models.accounts import FK_ACCOUNT
from app.models.base import Model, UTCDateTime, append_only


class Conversation(Model):
    __tablename__ = "conversations"

    type: Mapped[str] = mapped_column(String(10), index=True)
    pair_key: Mapped[str | None] = mapped_column(String(65), unique=True)  # "<low id>:<high id>" for 1:1 chats
    contract_id: Mapped[str | None] = mapped_column(String(32), index=True)
    created_by: Mapped[str] = mapped_column(String(32))
    last_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    status: Mapped[str] = mapped_column(String(10), default="open")


class ConversationParticipant(Model):
    __tablename__ = "conversation_participants"
    __table_args__ = (UniqueConstraint("conversation_id", "account_id"),)

    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    tab: Mapped[str] = mapped_column(String(10), default=ChatTab.PRIMARY)
    accepted: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Message(Model):
    __tablename__ = "messages"

    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    sender_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    body: Mapped[str] = mapped_column(Text, default="")
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reported: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    off_platform_warning: Mapped[bool] = mapped_column(Boolean, default=False)


class Report(Model):
    __tablename__ = "reports"

    reporter_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    target_type: Mapped[str] = mapped_column(String(14))
    target_id: Mapped[str] = mapped_column(String(32), index=True)
    target_account_id: Mapped[str | None] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(String(20), index=True)
    details: Mapped[str] = mapped_column(Text, default="")
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(10), default=ReportStatus.OPEN, index=True)


class ApiKey(Model):
    """Company-scoped API key (API-01). Only the hash is stored; the key is shown once."""

    __tablename__ = "api_keys"

    company_account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    name: Mapped[str] = mapped_column(String(80), default="")
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    key_prefix: Mapped[str] = mapped_column(String(12))
    rate_limits: Mapped[dict] = mapped_column(JSON, default=dict)  # per-key overrides of the defaults
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AdminPermission(Model):
    __tablename__ = "admin_permissions"
    __table_args__ = (UniqueConstraint("admin_id", "permission"),)

    admin_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    permission: Mapped[str] = mapped_column(String(24))
    payout_limit: Mapped[int | None] = mapped_column(BigInteger)
    granted_by: Mapped[str] = mapped_column(String(32))


class AdminInvite(Model):
    __tablename__ = "admin_invites"

    email: Mapped[str] = mapped_column(String(255), index=True)
    role: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


@append_only
class AuditLog(Model):
    """AD-04: who, what, when and why. Cannot be edited or deleted."""

    __tablename__ = "audit_logs"

    actor_id: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(48), index=True)
    target_type: Mapped[str] = mapped_column(String(24))
    target_id: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column("metadata", JSON, default=dict)


class QueueItem(Model):
    __tablename__ = "queue_items"

    queue: Mapped[str] = mapped_column(String(24), index=True)
    target_type: Mapped[str] = mapped_column(String(24))
    target_id: Mapped[str] = mapped_column(String(32), index=True)
    subject_account_id: Mapped[str | None] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(8), default="normal")
    summary: Mapped[str] = mapped_column(Text, default="")
    claimed_by: Mapped[str | None] = mapped_column(String(32), index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    decision: Mapped[str | None] = mapped_column(String(14))
    decided_by: Mapped[str | None] = mapped_column(String(32), index=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reason: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class SupportTicket(Model):
    __tablename__ = "support_tickets"

    number: Mapped[str] = mapped_column(String(16), unique=True)
    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    on_behalf_of_minor_id: Mapped[str | None] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(20), index=True)
    subject: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(14), default=TicketStatus.OPEN, index=True)
    linked_object: Mapped[dict | None] = mapped_column(JSON)
    first_response_due_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    first_responded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    assigned_to: Mapped[str | None] = mapped_column(String(32))


class TicketMessage(Model):
    __tablename__ = "ticket_messages"

    ticket_id: Mapped[str] = mapped_column(ForeignKey("support_tickets.id"), index=True)
    sender_id: Mapped[str] = mapped_column(String(32))
    from_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    body: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list] = mapped_column(JSON, default=list)


class Notification(Model):
    __tablename__ = "notifications"

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    type: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    emailed: Mapped[bool] = mapped_column(Boolean, default=False)
    digest_pending: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class B2BCategoryLink(Model):
    """Admin-maintained map: companies in `seller_category` likely sell to `buyer_category` (CO-03)."""

    __tablename__ = "b2b_category_links"
    __table_args__ = (UniqueConstraint("seller_category", "buyer_category"),)

    seller_category: Mapped[str] = mapped_column(String(64), index=True)
    buyer_category: Mapped[str] = mapped_column(String(64))
    updated_by: Mapped[str] = mapped_column(String(32))


class ReconciliationRun(Model):
    __tablename__ = "reconciliation_runs"

    currency: Mapped[str] = mapped_column(String(3))
    expected: Mapped[int] = mapped_column(BigInteger)
    actual: Mapped[int] = mapped_column(BigInteger)
    matched: Mapped[bool] = mapped_column(Boolean)
    difference: Mapped[int] = mapped_column(BigInteger, default=0)
    note: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[int] = mapped_column(Integer, default=0)


class Upload(Model):
    """Every uploaded file and who uploaded it, so a file can only be used by its owner, files nobody used are
    cleaned up, and a deleted account's files are deleted too."""

    __tablename__ = "uploads"

    key: Mapped[str] = mapped_column(String(64), unique=True)  # kind/<random>.<ext>, as stored
    owner_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    content_type: Mapped[str] = mapped_column(String(80))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)  # first attached to something
