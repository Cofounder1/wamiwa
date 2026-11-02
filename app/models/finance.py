"""Escrow, ledger, payments, payouts, disputes and subscriptions (Epics 6 and 8)."""
from datetime import date, datetime

from sqlalchemy import JSON, BigInteger, Boolean, Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import DisputeStatus, EscrowState, PaymentStatus, PayoutStatus, SubscriptionStatus
from app.models.accounts import FK_ACCOUNT
from app.models.base import Model, UTCDateTime, append_only
from app.models.contracts import FK_CONTRACT


class EscrowTransaction(Model):
    """Funds held for one contract, in local-currency minor units."""

    __tablename__ = "escrow_transactions"

    contract_id: Mapped[str] = mapped_column(ForeignKey(FK_CONTRACT), unique=True)
    hirer_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    earner_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    fee_pct: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(10), default=EscrowState.FUNDED, index=True)
    state_before_freeze: Mapped[str | None] = mapped_column(String(10))
    # Settlement: amount == released_gross + refunded_amount once settled.
    released_gross: Mapped[int] = mapped_column(BigInteger, default=0)
    fee_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    net_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    refunded_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    provider_ref: Mapped[str | None] = mapped_column(String(64), index=True)
    risk_cleared: Mapped[bool] = mapped_column(Boolean, default=True)
    cleared_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    payout_id: Mapped[str | None] = mapped_column(String(32), index=True)
    refund_due_by: Mapped[datetime | None] = mapped_column(UTCDateTime)
    charged_back: Mapped[bool] = mapped_column(Boolean, default=False)


@append_only
class LedgerEntry(Model):
    """Append-only record of every escrow state change (section 20, audit and money integrity)."""

    __tablename__ = "ledger_entries"

    escrow_id: Mapped[str] = mapped_column(ForeignKey("escrow_transactions.id"), index=True)
    contract_id: Mapped[str] = mapped_column(String(32), index=True)
    from_state: Mapped[str | None] = mapped_column(String(10))
    to_state: Mapped[str] = mapped_column(String(10))
    amount: Mapped[int] = mapped_column(BigInteger, default=0)
    actor_id: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")


class Payment(Model):
    """A non-escrow charge: subscriptions, AI video, video production, plans and extras."""

    __tablename__ = "payments"

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)
    purpose: Mapped[str] = mapped_column(String(20), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)  # USD list price
    amount_local: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    fx_rate: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(14), default=PaymentStatus.SUCCEEDED, index=True)
    provider_ref: Mapped[str | None] = mapped_column(String(64), index=True)
    refunded_local: Mapped[int] = mapped_column(BigInteger, default=0)
    reference_id: Mapped[str | None] = mapped_column(String(32), index=True)
    reusable_method: Mapped[str | None] = mapped_column(String(200))  # a saved card for renewals (PY-05)


class Payout(Model):
    __tablename__ = "payouts"

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)  # who receives the money
    earner_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), index=True)  # who earned it (the minor)
    cycle_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(18), default=PayoutStatus.APPROVED, index=True)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    requested_by: Mapped[str | None] = mapped_column(String(32))
    approved_by: Mapped[str | None] = mapped_column(String(32))
    second_approved_by: Mapped[str | None] = mapped_column(String(32))
    needs_second_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text)
    provider_ref: Mapped[str | None] = mapped_column(String(64))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Dispute(Model):
    __tablename__ = "disputes"

    contract_id: Mapped[str] = mapped_column(ForeignKey(FK_CONTRACT), unique=True)
    raised_by: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT))
    reason: Mapped[str] = mapped_column(Text)
    is_misuse_claim: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(14), default=DisputeStatus.EVIDENCE, index=True)
    evidence_due_at: Mapped[datetime] = mapped_column(UTCDateTime)
    decision_due_at: Mapped[datetime] = mapped_column(UTCDateTime)
    decision: Mapped[str | None] = mapped_column(String(10))
    release_pct: Mapped[int | None] = mapped_column(Integer)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(32))
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    appealed_by: Mapped[list] = mapped_column(JSON, default=list)  # each side may appeal once
    appeal_reason: Mapped[str | None] = mapped_column(Text)
    appeal_decided_by: Mapped[str | None] = mapped_column(String(32))
    contract_status_before: Mapped[str] = mapped_column(String(20))
    post_payout_adjustment: Mapped[int] = mapped_column(BigInteger, default=0)


class DisputeEvidence(Model):
    __tablename__ = "dispute_evidence"

    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id"), index=True)
    submitted_by: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT))
    statement: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list] = mapped_column(JSON, default=list)


class Subscription(Model):
    __tablename__ = "subscriptions"

    account_id: Mapped[str] = mapped_column(ForeignKey(FK_ACCOUNT), unique=True)
    tier: Mapped[str] = mapped_column(String(16), default="standard")  # standard (Creator) | pro
    next_tier: Mapped[str | None] = mapped_column(String(16))  # a downgrade waiting for the next renewal
    target_regions: Mapped[list | None] = mapped_column(JSON)  # Pro: where they want content ideas for
    period: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(10), default=SubscriptionStatus.ACTIVE, index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    renews_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    payment_method: Mapped[str] = mapped_column(String(64))
    lapse_notice_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    free_seconds_month: Mapped[str | None] = mapped_column(String(7))  # "YYYY-MM" the allowance was used in
    free_seconds_used: Mapped[int] = mapped_column(Integer, default=0)


class MoneyMovement(Model):
    """A refund waiting to be made, or made, at the payment provider.

    Written in the same transaction as the decision to refund, and carried out only after that
    transaction commits, so a rolled-back request can never have refunded anyone. The provider sees
    `reference`, which is unique, so a retried refund is never paid twice.
    """

    __tablename__ = "money_movements"

    kind: Mapped[str] = mapped_column(String(10))  # refund | void (undoing a charge whose request failed)
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)  # pending | succeeded | failed
    reference: Mapped[str] = mapped_column(String(64), unique=True)
    charge_ref: Mapped[str] = mapped_column(String(64))
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    payment_id: Mapped[str | None] = mapped_column(String(32), index=True)
    escrow_id: Mapped[str | None] = mapped_column(String(32), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    provider_ref: Mapped[str | None] = mapped_column(String(64))
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
