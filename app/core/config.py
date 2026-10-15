"""Application settings. Every PRD "starting value" lives here so it can change without a deploy."""
from decimal import Decimal
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WAMIWA_", env_file=".env", extra="ignore")

    env: str = "development"
    debug: bool = False
    app_name: str = "Wamiwa API"
    public_base_url: str = "https://wamiwa.site"

    database_url: str = "sqlite+aiosqlite:///./wamiwa.db"
    database_pool_size: int = 5  # steady connections per worker; hosted poolers cap the total per plan
    database_max_overflow: int = 5  # extra connections allowed at busy moments
    database_schema: str | None = None  # Postgres only: keep the tables in a schema other than "public"
    redis_url: str | None = None  # falls back to an in-process store when unset (single worker only)
    run_jobs: bool = True  # run the in-process scheduler; disable on all but one worker

    # --- security ---
    jwt_secret: str = "change-me-in-production-change-me-in-production"
    hash_pepper: str = "change-me-too"
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 30 * 24 * 3600
    signup_token_ttl_seconds: int = 30 * 60
    payment_webhook_secret: str = "payment-webhook-secret"
    voice_webhook_secret: str = "voice-webhook-secret"
    video_webhook_secret: str = "video-webhook-secret"
    allow_private_webhook_urls: bool = False  # SSRF guard for agent webhooks

    # --- third-party credentials (PRD section 19) ---
    # Not read yet: every provider still runs on its sandbox adapter. Each key is used once the
    # matching adapter is written and wired in app/container.py.
    youverify_api_key: str | None = None
    korapay_secret_key: str | None = None
    korapay_public_key: str | None = None
    heygen_api_key: str | None = None
    sms_voice_api_key: str | None = None  # vendor to choose
    email_provider: str = "sandbox"  # "resend" sends real email; "sandbox" only records it (development, tests)
    email_api_key: str | None = None  # Resend API key
    email_from_address: str = "no-reply@wamiwa.site"  # its domain must be verified in Resend
    email_from_name: str = "Wamiwa"
    email_reply_to: str | None = "info@wamiwa.site"  # where replies to Wamiwa's emails land
    moderation_api_key: str | None = None  # vendor to choose
    watermark_api_key: str | None = None  # vendor to choose
    fx_api_key: str | None = None  # live USD -> NGN rate source, to choose
    link_safety_api_key: str | None = None  # scam / phishing reputation, to choose
    trends_api_key: str | None = None  # outside trends by region, to choose
    spotify_client_id: str | None = None  # looking artists up by name
    spotify_client_secret: str | None = None
    youtube_api_key: str | None = None
    audiomack_api_key: str | None = None
    instagram_client_id: str | None = None
    instagram_client_secret: str | None = None
    tiktok_client_id: str | None = None
    tiktok_client_secret: str | None = None
    x_client_id: str | None = None
    x_client_secret: str | None = None
    youtube_client_id: str | None = None
    youtube_client_secret: str | None = None
    facebook_client_id: str | None = None
    facebook_client_secret: str | None = None

    # --- ON-01 OTP ---
    otp_length: int = 6
    otp_ttl_seconds: int = 10 * 60
    otp_max_wrong_attempts: int = 5
    otp_lock_seconds: int = 15 * 60
    otp_resends_per_hour: int = 3
    otp_resend_hint_after_seconds: int = 60

    # --- ON-02 / ON-05 / ON-06 / ON-07 ---
    min_age: int = 13
    adult_age: int = 18
    id_max_attempts: int = 3
    bank_change_hold_hours: int = 48
    verified_creator_followers: int = 5000
    likeness_min_followers: int = 500
    social_recheck_days: int = 90
    fake_follower_threshold: float = 0.4
    popularity_review_months: int = 12
    popularity_appeal_days: int = 30

    # --- content / agents (FD-04, FD-05, AI-02) ---
    max_agent_tags_per_comment: int = 3
    agent_to_agent_replies_per_thread: int = 3
    agent_comments_per_hour: int = 30
    tag_no_response_seconds: int = 10 * 60
    default_free_tag_daily_cap: int = 20
    webhook_retry_delays_seconds: list[int] = Field(default_factory=lambda: [60, 300, 540])  # 3 retries / 15 min
    webhook_timeout_seconds: float = 10.0
    webhook_auto_pause_hours: int = 24
    deliver_webhooks_inline: bool = False  # await delivery in the request (tests); otherwise background task
    mislabel_strikes_before_suspension: int = 2

    # --- fees (PY-01) ---
    fee_pct_hire_me: int = 20
    fee_pct_likeness: int = 20
    fee_pct_agent: int = 10

    # --- contracts ---
    offer_expiry_days: int = 7
    review_window_days: int = 7
    likeness_term_days: int = 30
    likeness_takedown_days: int = 60
    script_change_rounds: int = 3
    video_approval_hours: int = 72
    video_approval_warning_hours: int = 48
    brand_cancel_after_start_keep_pct: int = 25
    likeness_non_refundable_pct: int = 25
    cancellations_for_ranking_penalty: int = 3
    cancellation_window_days: int = 90
    misuse_takedown_hours: int = 24
    free_defect_claims_per_contract: int = 2
    minor_blocked_categories: list[str] = Field(
        default_factory=lambda: [
            "alcohol", "tobacco", "vaping", "betting", "gambling", "dating", "adult",
            "weight_loss", "political", "loans", "crypto",
        ]
    )

    # --- guardian confirmation call (LK-09) ---
    guardian_call_attempts: int = 3
    guardian_call_retry_hours: int = 24  # 2 more attempts over 48 hours
    guardian_confirmation_days: int = 7

    # --- video production (LK-06, LK-10, AV-01) ---
    video_price_cents: int = 45_00
    video_default_seconds: int = 60
    video_drafts_included: int = 4
    video_finals_included: int = 2
    extra_final_render_cents: int = 15_00
    extra_draft_cents: int | None = None  # the PRD prices no extra drafts; None blocks them
    extra_minute_cents: int = 25_00
    plan_growth_cents: int = 199_00
    plan_growth_videos: int = 5
    plan_scale_cents: int = 499_00
    plan_scale_videos: int = 14
    arcon_fee_cents: int = 0  # ARCON's own fee, passed through at cost
    arcon_handling_fee_cents: int | None = None  # "amount to set"; None disables the service
    ai_video_standard_cents_per_minute: int = 3_00
    ai_video_premium_cents_per_minute: int = 25_00
    subscriber_free_standard_seconds: int = 60
    video_slow_job_seconds: int = 10 * 60
    video_sync_retries: int = 1  # automatic re-renders when lips or expression do not follow the script

    # --- Wamiwa Agent: smart mentions and suggested sellers (Epic 15) ---
    promo_plan_monthly_cents: int = 20_00  # business, seller and artist plans
    promo_label_monthly_cents: int = 80_00  # label plan: one price for a roster of up to `promo_max_names` artists
    promo_period_days: int = 30
    promo_max_names: int = 50  # a label's roster
    promo_plus_addon_cents: int = 10_00  # the Plus package: double the links, plus trend-based content ideas
    promo_links_business: int = 5  # businesses and individual sellers; doubled on Plus
    promo_links_artist: int = 3  # artists, and each artist on a label's roster; doubled on Plus
    promo_max_target_regions: int = 5
    promo_mentions_per_item: int = 3
    promo_suggestions_shown: int = 5
    promo_author_triggers_per_day: int = 5  # sponsored replies one person can trigger in a day
    wamiwa_agent_handle: str = "wamiwa"
    wamiwa_agent_replies_per_hour: int = 600  # first-party agent; not bound by the 30/hour agent limit

    # --- subscriptions (Epic 8) ---
    subscription_monthly_cents: int = 10_00
    subscription_yearly_cents: int = 100_00

    # --- payments / payouts ---
    local_currency: str = "NGN"
    sandbox_fx_rate: Decimal = Decimal("1500")  # NGN per USD used by the sandbox FX provider
    payout_cycle_days: int = 14
    manual_payout_dual_approval_minor_units: int = 500_000_00  # AD-03 "set amount", in kobo
    refund_working_days: int = 5

    # --- disputes (PY-04) ---
    dispute_evidence_days: int = 5
    dispute_decision_working_days: int = 10
    dispute_misuse_window_days: int = 30
    dispute_appeal_days: int = 3  # assumption: funds stay frozen this long so either side can appeal

    # --- B2B / messaging ---
    b2b_new_conversations_per_day: int = 20
    b2b_spam_reports_for_ban: int = 3
    b2b_ban_days: int = 30
    max_attachment_bytes: int = 25 * 1024 * 1024
    off_platform_warnings_before_review: int = 3

    # --- company API (API-01, API-02) ---
    api_posts_per_hour: int = 60
    api_replies_per_hour: int = 600

    # --- general rate limits (per minute) ---
    human_reads_per_minute: int = 120
    human_writes_per_minute: int = 60
    machine_reads_per_minute: int = 60
    machine_writes_per_minute: int = 30
    anonymous_requests_per_minute: int = 30
    logins_per_hour: int = 10

    # --- account lifecycle / admin ---
    deletion_grace_days: int = 30
    suspension_appeal_days: int = 14
    min_super_admins: int = 2
    admin_invite_ttl_hours: int = 48
    false_reports_before_warning: int = 3
    user_churn_days: int = 30
    brand_churn_days: int = 90

    # --- cache TTLs (seconds) ---
    cache_ttl_profile: int = 60
    cache_ttl_marketplace: int = 60
    cache_ttl_feed: int = 15
    cache_ttl_dashboard: int = 300
    cache_ttl_category_map: int = 600
    cache_ttl_fx: int = 24 * 3600
    cache_ttl_api_key: int = 60
    cache_ttl_sentiment: int = 120

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
