"""Lazy registry of services for one unit of work.

Services reach each other through `self.svc.<name>`, so there are no import cycles and each
service is built at most once per request.
"""
from __future__ import annotations

from functools import cached_property

from app.services import (
    accounts, admin, agents, auth, company, content, contracts, disputes, emails, growth, insights, likeness,
    messaging, money, notifications, payments, promos, queues, reminders, social, support, trust,
    uploads, verification, video,
)
from app.services.base import ServiceContext


class ServiceRegistry:
    def __init__(self, ctx: ServiceContext):
        self._ctx = ctx

    # --- platform -------------------------------------------------------------------------------
    @cached_property
    def notifications(self) -> notifications.NotificationService:
        return notifications.NotificationService(self._ctx)

    @cached_property
    def audit(self) -> queues.AuditService:
        return queues.AuditService(self._ctx)

    @cached_property
    def queues(self) -> queues.QueueService:
        return queues.QueueService(self._ctx)

    @cached_property
    def welcome(self) -> emails.WelcomeEmailService:
        return emails.WelcomeEmailService(self._ctx)

    @cached_property
    def admin(self) -> admin.AdminService:
        return admin.AdminService(self._ctx)

    @cached_property
    def admin_analytics(self) -> admin.AdminAnalyticsService:
        return admin.AdminAnalyticsService(self._ctx)

    @cached_property
    def reminders(self) -> reminders.ReminderService:
        return reminders.ReminderService(self._ctx)

    @cached_property
    def support(self) -> support.SupportService:
        return support.SupportService(self._ctx)

    # --- identity -------------------------------------------------------------------------------
    @cached_property
    def otp(self) -> auth.OtpService:
        return auth.OtpService(self._ctx)

    @cached_property
    def auth(self) -> auth.AuthService:
        return auth.AuthService(self._ctx)

    @cached_property
    def access(self) -> accounts.AccessPolicy:
        return accounts.AccessPolicy(self._ctx)

    @cached_property
    def accounts(self) -> accounts.AccountService:
        return accounts.AccountService(self._ctx)

    @cached_property
    def guardians(self) -> accounts.GuardianService:
        return accounts.GuardianService(self._ctx)

    @cached_property
    def identity(self) -> verification.IdentityService:
        return verification.IdentityService(self._ctx)

    @cached_property
    def bank_accounts(self) -> verification.BankAccountService:
        return verification.BankAccountService(self._ctx)

    @cached_property
    def creators(self) -> verification.CreatorVerificationService:
        return verification.CreatorVerificationService(self._ctx)

    @cached_property
    def popularity(self) -> verification.PopularityCardService:
        return verification.PopularityCardService(self._ctx)

    @cached_property
    def friends(self) -> verification.FriendImportService:
        return verification.FriendImportService(self._ctx)

    # --- social and content -----------------------------------------------------------------------
    @cached_property
    def blocks(self) -> social.BlockService:
        return social.BlockService(self._ctx)

    @cached_property
    def follows(self) -> social.FollowService:
        return social.FollowService(self._ctx)

    @cached_property
    def profiles(self) -> social.ProfileService:
        return social.ProfileService(self._ctx)

    @cached_property
    def search(self) -> social.SearchService:
        return social.SearchService(self._ctx)

    @cached_property
    def posts(self) -> content.PostService:
        return content.PostService(self._ctx)

    @cached_property
    def feed(self) -> content.FeedService:
        return content.FeedService(self._ctx)

    @cached_property
    def comments(self) -> content.CommentService:
        return content.CommentService(self._ctx)

    @cached_property
    def analytics(self) -> content.AnalyticsService:
        return content.AnalyticsService(self._ctx)

    @cached_property
    def post_ideas(self) -> content.PostIdeaService:
        return content.PostIdeaService(self._ctx)

    @cached_property
    def messaging(self) -> messaging.MessagingService:
        return messaging.MessagingService(self._ctx)

    # --- agents -----------------------------------------------------------------------------------
    @cached_property
    def agents(self) -> agents.AgentService:
        return agents.AgentService(self._ctx)

    @cached_property
    def integrations(self) -> agents.AgentIntegrationService:
        return agents.AgentIntegrationService(self._ctx)

    @cached_property
    def webhooks(self) -> agents.WebhookDispatcher:
        return agents.WebhookDispatcher(self._ctx)

    @cached_property
    def agent_actions(self) -> agents.AgentActionService:
        return agents.AgentActionService(self._ctx)

    # --- money and contracts ------------------------------------------------------------------------
    @cached_property
    def money(self) -> money.MoneyMovementService:
        return money.MoneyMovementService(self._ctx)

    @cached_property
    def fx(self) -> payments.FxService:
        return payments.FxService(self._ctx)

    @cached_property
    def payment(self) -> payments.PaymentService:
        return payments.PaymentService(self._ctx)

    @cached_property
    def escrow(self) -> payments.EscrowService:
        return payments.EscrowService(self._ctx)

    @cached_property
    def payouts(self) -> payments.PayoutService:
        return payments.PayoutService(self._ctx)

    @cached_property
    def payment_events(self) -> payments.PaymentEventService:
        return payments.PaymentEventService(self._ctx)

    @cached_property
    def reconciliation(self) -> payments.ReconciliationService:
        return payments.ReconciliationService(self._ctx)

    @cached_property
    def contracts(self) -> contracts.ContractService:
        return contracts.ContractService(self._ctx)

    @cached_property
    def hire_me(self) -> contracts.HireMeService:
        return contracts.HireMeService(self._ctx)

    @cached_property
    def agent_orders(self) -> contracts.AgentOrderService:
        return contracts.AgentOrderService(self._ctx)

    @cached_property
    def likeness_pages(self) -> likeness.LikenessPageService:
        return likeness.LikenessPageService(self._ctx)

    @cached_property
    def likeness(self) -> likeness.LikenessContractService:
        return likeness.LikenessContractService(self._ctx)

    @cached_property
    def guardian_confirmations(self) -> likeness.GuardianConfirmationService:
        return likeness.GuardianConfirmationService(self._ctx)

    @cached_property
    def video_plans(self) -> video.VideoPlanService:
        return video.VideoPlanService(self._ctx)

    @cached_property
    def videos(self) -> video.VideoGenerationService:
        return video.VideoGenerationService(self._ctx)

    @cached_property
    def disputes(self) -> disputes.DisputeService:
        return disputes.DisputeService(self._ctx)

    @cached_property
    def subscriptions(self) -> disputes.SubscriptionService:
        return disputes.SubscriptionService(self._ctx)

    @cached_property
    def interests(self) -> growth.InterestService:
        return growth.InterestService(self._ctx)

    @cached_property
    def growth(self) -> growth.GrowthService:
        return growth.GrowthService(self._ctx)

    @cached_property
    def chat_insights(self) -> insights.ChatInsightService:
        return insights.ChatInsightService(self._ctx)

    @cached_property
    def uploads(self) -> uploads.UploadService:
        return uploads.UploadService(self._ctx)

    # --- companies, trust -----------------------------------------------------------------------------
    @cached_property
    def companies(self) -> company.CompanyService:
        return company.CompanyService(self._ctx)

    @cached_property
    def b2b(self) -> company.B2BService:
        return company.B2BService(self._ctx)

    @cached_property
    def api_keys(self) -> company.ApiKeyService:
        return company.ApiKeyService(self._ctx)

    @cached_property
    def company_api(self) -> company.CompanyApiService:
        return company.CompanyApiService(self._ctx)

    @cached_property
    def reports(self) -> trust.ReportService:
        return trust.ReportService(self._ctx)

    @cached_property
    def trust(self) -> trust.TrustService:
        return trust.TrustService(self._ctx)

    @cached_property
    def deletion(self) -> trust.DeletionService:
        return trust.DeletionService(self._ctx)

    @cached_property
    def promos(self) -> promos.PromoService:
        return promos.PromoService(self._ctx)
