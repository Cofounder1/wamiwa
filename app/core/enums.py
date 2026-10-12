"""Domain enumerations. Stored as plain strings so they stay portable across databases."""
from enum import StrEnum


class AccountType(StrEnum):
    USER = "user"
    COMPANY = "company"
    AGENT = "agent"
    ADMIN = "admin"


class AccountStatus(StrEnum):
    PENDING_GUARDIAN = "pending_guardian"  # 13-17 signup waiting for a verified guardian (ON-02)
    ACTIVE = "active"
    SUSPENDED = "suspended"
    FROZEN = "frozen"  # guardian freeze (ON-03) or chargeback freeze (PY-03)
    PENDING_DELETION = "pending_deletion"
    DELETED = "deleted"
    REMOVED = "removed"


class AdminRole(StrEnum):
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


class Permission(StrEnum):
    SUSPEND = "suspend"
    APPROVE_PAYOUTS = "approve_payouts"
    B2B_CATEGORY_MAP = "b2b_category_map"
    VIEW_DASHBOARD = "view_dashboard"
    VIEW_REVENUE = "view_revenue"


class FeedTab(StrEnum):
    PEOPLE = "people"
    AGENTS = "agents"


class Label(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SPONSORED = "sponsored"
    NONE = "none"  # a company's replies through its API key: shown under the company's name, with no tag


class AiBadge(StrEnum):
    NONE = "none"
    AI_GENERATED = "ai_generated"
    AI_LIKENESS = "ai_likeness"


class PostType(StrEnum):
    VIDEO = "video"
    TEXT = "text"


class ModerationStatus(StrEnum):
    VISIBLE = "visible"
    FLAGGED = "flagged"  # hidden pending review
    REMOVED = "removed"


class AgentCommentPolicy(StrEnum):
    ALL = "all"
    FOLLOWED = "followed"
    NONE = "none"


class TagStatus(StrEnum):
    PENDING = "pending"
    REPLIED = "replied"
    CAP_REACHED = "cap_reached"
    UNAVAILABLE = "unavailable"
    NO_RESPONSE = "no_response"


class GuardianLinkStatus(StrEnum):
    INVITED = "invited"
    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    ENDED = "ended"  # minor turned 18 or link revoked


class VerificationStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review"
    REJECTED = "rejected"


class DocumentType(StrEnum):
    NATIONAL_ID = "national_id"
    DRIVERS_LICENCE = "drivers_licence"
    PASSPORT = "passport"


class PublicFigureMethod(StrEnum):
    POPULARITY_CARD = "popularity_card"
    KNOWN_NAME = "known_name"
    PROOF = "proof"


class SignalCategory(StrEnum):
    MEDIA = "media_popularity"
    POSITION = "position"
    SPEAKING = "speaking"
    RECOGNITION = "recognition"
    PUBLIC_WORK = "public_work"


class PopularityLevel(StrEnum):
    NONE = "none"
    NOTABLE = "notable"
    PROMINENT = "prominent"
    MAJOR = "major"


class SocialPlatform(StrEnum):
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    X = "x"
    YOUTUBE = "youtube"
    FACEBOOK = "facebook"


class AssetKind(StrEnum):
    PHOTO = "photo"
    VOICE = "voice"


class ContractType(StrEnum):
    HIRE_ME = "hire_me"
    LIKENESS = "likeness"
    AGENT = "agent"


class ContractStatus(StrEnum):
    ADMIN_REVIEW = "admin_review"  # flagged or minor script waiting for an admin (LK-04)
    OFFERED = "offered"  # waiting for the earner
    CHANGES_REQUESTED = "changes_requested"  # influencer asked for script changes (LK-05)
    PENDING_GUARDIAN = "pending_guardian"  # accepted by a minor, waiting for guardian + call (LK-09)
    ACTIVE = "active"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    DISPUTED = "disputed"
    CANCELLED = "cancelled"
    DECLINED = "declined"
    EXPIRED = "expired"


OPEN_CONTRACT_STATUSES = (
    ContractStatus.ADMIN_REVIEW,
    ContractStatus.OFFERED,
    ContractStatus.CHANGES_REQUESTED,
    ContractStatus.PENDING_GUARDIAN,
    ContractStatus.ACTIVE,
    ContractStatus.DELIVERED,
    ContractStatus.DISPUTED,
)


class EscrowState(StrEnum):
    FUNDED = "funded"
    ACTIVE = "active"
    DELIVERED = "delivered"
    CLEARED = "cleared"
    PAID = "paid"
    FROZEN = "frozen"
    REFUNDED = "refunded"


class ScriptStatus(StrEnum):
    ADMIN_REVIEW = "admin_review"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ChangeResponse(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    MODIFIED = "modified"
    REJECTED = "rejected"


class VideoKind(StrEnum):
    DRAFT = "draft"
    FINAL = "final"
    PERSONAL = "personal"  # AV-01, outside a contract


class VideoStatus(StrEnum):
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class VideoDecision(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    AUTO_APPROVED = "auto_approved"
    REJECTED = "rejected"


class RejectionReason(StrEnum):
    TECHNICAL_DEFECT = "technical_defect"
    SCRIPT_MISMATCH = "script_mismatch"


class VideoTier(StrEnum):
    STANDARD = "standard"
    PREMIUM = "premium"


class GuardianResult(StrEnum):
    AWAITING_APP_APPROVAL = "awaiting_app_approval"
    AWAITING_CALL = "awaiting_call"
    APPROVED = "approved"
    DECLINED = "declined"
    EXPIRED = "expired"


class PaymentPurpose(StrEnum):
    ESCROW = "escrow"
    SUBSCRIPTION = "subscription"
    AI_VIDEO = "ai_video"
    VIDEO_PRODUCTION = "video_production"
    VIDEO_PLAN = "video_plan"
    EXTRA_RENDER = "extra_render"
    ARCON_FILING = "arcon_filing"
    PROMOTION = "promotion"


class PaymentStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PENDING_RISK = "pending_risk"
    FAILED = "failed"
    REFUNDED = "refunded"
    CHARGED_BACK = "charged_back"


class PayoutStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    PAID = "paid"
    HELD = "held"
    FAILED = "failed"
    REJECTED = "rejected"


class DisputeStatus(StrEnum):
    EVIDENCE = "evidence"
    UNDER_REVIEW = "under_review"
    DECIDED = "decided"
    APPEALED = "appealed"
    FINAL = "final"


class DisputeDecision(StrEnum):
    RELEASE = "release"
    REFUND = "refund"
    SPLIT = "split"


class ReportReason(StrEnum):
    SPAM = "spam"
    SCAM = "scam"
    HARASSMENT = "harassment"
    IMPERSONATION = "impersonation"
    MISUSE_OF_LIKENESS = "misuse_of_likeness"
    MISLABELED_AI = "mislabeled_ai"
    OTHER = "other"


class ReportTarget(StrEnum):
    ACCOUNT = "account"
    POST = "post"
    COMMENT = "comment"
    SCRIPT = "script"
    MESSAGE = "message"
    CONVERSATION = "conversation"


class ReportStatus(StrEnum):
    OPEN = "open"
    UPHELD = "upheld"
    DISMISSED = "dismissed"


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"  # stays usable until renews_at
    LAPSED = "lapsed"


class BillingPeriod(StrEnum):
    MONTHLY = "monthly"
    YEARLY = "yearly"


class VideoPlan(StrEnum):
    GROWTH = "growth"
    SCALE = "scale"


class ConversationType(StrEnum):
    DIRECT = "direct"
    BUSINESS = "business"
    ORDER = "order"
    CONTRACT = "contract"


class ChatTab(StrEnum):
    PRIMARY = "primary"
    REQUESTS = "requests"
    BUSINESS = "business"


class IntegrationStatus(StrEnum):
    PENDING_TEST = "pending_test"
    LIVE = "live"
    PAUSED = "paused"


class EventType(StrEnum):
    TEST_PING = "test.ping"
    TAG_CREATED = "tag.created"
    COMMENT_REPLY = "comment.reply"
    ORDER_CREATED = "order.created"
    ORDER_REVISION_REQUESTED = "order.revision_requested"
    ORDER_CANCELLED = "order.cancelled"
    AUTOREPLY_COMMENT = "autoreply.comment"


class EventStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class Queue(StrEnum):
    IDENTITY = "identity"
    PUBLIC_FIGURE = "public_figure"
    COMPANY = "company_verification"
    SCRIPTS = "scripts"
    REPORTS = "reports"
    DISPUTES = "disputes"
    PAYOUTS = "payouts"
    FAKE_FOLLOWERS = "fake_followers"
    PROMOTIONS = "promotions"


class QueueDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_INFO = "request_info"
    ESCALATE = "escalate"


class TicketCategory(StrEnum):
    ACCOUNT = "account"
    PAYMENTS = "payments"
    CONTRACTS = "contracts"
    VERIFICATION = "verification"
    REPORT_A_PROBLEM = "report_a_problem"
    OTHER = "other"


class TicketStatus(StrEnum):
    OPEN = "open"
    WAITING_ON_USER = "waiting_on_me"
    RESOLVED = "resolved"


class NotificationType(StrEnum):
    # Always on (NT-01)
    PAYMENT = "payment"
    CONTRACT = "contract"
    DISPUTE = "dispute"
    SECURITY = "security"
    LEGAL = "legal"
    # Optional per type
    LIKE = "like"
    COMMENT = "comment"
    PROFILE_VIEW = "profile_view"
    POST_IDEAS = "post_ideas"


MANDATORY_NOTIFICATIONS = frozenset(
    {
        NotificationType.PAYMENT,
        NotificationType.CONTRACT,
        NotificationType.DISPUTE,
        NotificationType.SECURITY,
        NotificationType.LEGAL,
    }
)
