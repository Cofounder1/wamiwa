"""Scheduled jobs: every time-based rule in the PRD (expiries, auto-approvals, payouts, retries).

Each job runs in its own unit of work. A key-value lock per job and interval means that with
Redis only one worker runs a given job at a time, however many API workers are up.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.services.base import Infrastructure, ServiceContext
from app.services.registry import ServiceRegistry

log = logging.getLogger("wamiwa.jobs")
MINUTE, HOUR, DAY = 60, 3600, 86400


@dataclass(frozen=True)
class Job:
    name: str
    interval_seconds: int
    run: Callable[[ServiceRegistry], Awaitable[Any]]


JOBS: tuple[Job, ...] = (
    # Agents and webhooks
    Job("webhooks.deliver_due", MINUTE, lambda s: s.webhooks.deliver_due()),
    Job("webhooks.pause_failing_agents", HOUR, lambda s: s.webhooks.pause_failing_agents()),
    # Contracts and escrow
    Job("contracts.expire_offers", 10 * MINUTE, lambda s: s.contracts.expire_offers()),
    Job("contracts.auto_clear", 10 * MINUTE, lambda s: s.contracts.auto_clear()),
    Job("likeness.end_terms", 10 * MINUTE, lambda s: s.likeness.end_terms()),
    Job("posts.take_down_expired", HOUR, lambda s: s.posts.take_down_expired()),
    Job("hire_me.refresh_ranking_penalties", DAY, lambda s: s.hire_me.refresh_ranking_penalties()),
    # Guardian confirmation calls
    Job("guardian.retry_calls", 10 * MINUTE, lambda s: s.guardian_confirmations.retry_calls()),
    Job("guardian.expire", HOUR, lambda s: s.guardian_confirmations.expire()),
    # Video
    Job("videos.poll_processing", MINUTE, lambda s: s.videos.poll_processing()),
    Job("videos.auto_approve", 10 * MINUTE, lambda s: s.videos.auto_approve()),
    Job("video_plans.renew_due", HOUR, lambda s: s.video_plans.renew_due()),
    Job("ai_video_plans.renew_due", HOUR, lambda s: s.videos.renew_personal_plans()),
    # Disputes
    Job("disputes.close_evidence_windows", HOUR, lambda s: s.disputes.close_evidence_windows()),
    Job("disputes.apply_unappealed", HOUR, lambda s: s.disputes.apply_unappealed()),
    # Money
    Job("payouts.run_cycle", DAY, lambda s: s.payouts.run_cycle()),
    Job("payouts.finish_stuck", 10 * MINUTE, lambda s: s.payouts.finish_stuck()),
    Job("money.retry_due", 5 * MINUTE, lambda s: s.money.retry_due()),
    Job("money.refund_unrecorded", 30 * MINUTE, lambda s: s.stray_payments.refund_unrecorded()),
    Job("payouts.confirm_sent", 30 * MINUTE, lambda s: s.payouts.confirm_sent()),
    Job("reconciliation.run", DAY, lambda s: s.reconciliation.run()),
    # Subscriptions and daily content
    Job("subscriptions.process_renewals", HOUR, lambda s: s.subscriptions.process_renewals()),
    Job("subscriptions.send_lapse_notices", HOUR, lambda s: s.subscriptions.send_lapse_notices()),
    Job("promos.renew_due", HOUR, lambda s: s.promos.renew_due()),
    Job("promos.recheck_links", DAY, lambda s: s.promos.recheck_links()),
    Job("promos.refresh_defaults", DAY, lambda s: s.promos.refresh_defaults()),
    Job("post_ideas.generate_daily", DAY, lambda s: s.post_ideas.generate_daily()),
    Job("notifications.send_daily_digests", DAY, lambda s: s.notifications.send_daily_digests()),
    Job("reminders.send_due", HOUR, lambda s: s.reminders.send_due()),
    Job("queues.send_daily_summary", DAY, lambda s: s.queues.send_daily_summary()),
    # Accounts and verification
    Job("accounts.convert_minors_turning_18", DAY, lambda s: s.accounts.convert_minors_turning_18()),
    Job("creators.recheck_due", DAY, lambda s: s.creators.recheck_due()),
    Job("popularity.queue_due_reviews", DAY, lambda s: s.popularity.queue_due_reviews()),
    Job("deletion.purge_due", DAY, lambda s: s.deletion.purge_due()),
    Job("uploads.purge_unused", DAY, lambda s: s.uploads.purge_unused()),
    Job("equity.send_vesting_reminders", DAY, lambda s: s.equity.send_vesting_reminders()),
)


class JobRunner:
    TICK_SECONDS = 30

    def __init__(self, infra: Infrastructure, jobs: tuple[Job, ...] = JOBS):
        self._infra = infra
        self._jobs = {job.name: job for job in jobs}
        self._task: asyncio.Task | None = None

    async def run_job(self, name: str) -> Any:
        """Run one job now, in its own transaction. Also used by tests and the CLI."""
        job = self._jobs[name]
        async with self._infra.db.unit_of_work() as uow:
            return await job.run(ServiceContext(self._infra, uow).services)

    async def run_due(self) -> list[str]:
        ran = []
        for job in self._jobs.values():
            # The lock expires after one interval, which is also what makes the job due again. If the
            # store is down, no job runs: better late than twice (money jobs move funds).
            kv = getattr(self._infra.kv, "strict", self._infra.kv)
            try:
                if not await kv.set_if_absent(f"job:{job.name}", "1", job.interval_seconds):
                    continue
            except Exception:  # noqa: BLE001
                log.warning("job %s skipped: lock store unavailable", job.name)
                continue
            try:
                await self.run_job(job.name)
                ran.append(job.name)
            except Exception:  # noqa: BLE001 - one failing job must not stop the others
                log.exception("job %s failed", job.name)
        return ran

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_due()
            except Exception:  # noqa: BLE001
                log.exception("job loop error")
            await asyncio.sleep(self.TICK_SECONDS)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
