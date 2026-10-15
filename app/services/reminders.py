"""Reminders before something happens automatically, so nobody is surprised by an expiry or a release."""
from datetime import timedelta

from app.core.enums import ContractStatus, ContractType, GuardianResult, NotificationType
from app.models import Contract, GuardianConfirmation
from app.services.base import BaseService


class ReminderService(BaseService):
    async def _once(self, key: str) -> bool:
        return await self.kv.set_if_absent(f"reminder:{key}", "1", 30 * 86400)

    async def send_due(self) -> int:
        """Job: each reminder goes once, about a day before its deadline (three days for a likeness term)."""
        now, sent = self.now(), 0
        soon = now + timedelta(hours=24)

        # An offer or script about to expire: remind whoever has to act.
        for contract in await self.repo(Contract).list(
                Contract.status.in_([ContractStatus.OFFERED, ContractStatus.CHANGES_REQUESTED]),
                Contract.offer_expires_at.is_not(None), Contract.offer_expires_at <= soon, Contract.offer_expires_at > now):
            if await self._once(f"offer:{contract.id}:{contract.status}"):
                if contract.status == ContractStatus.OFFERED:
                    await self.svc.notifications.notify(
                        contract.earner_id, NotificationType.CONTRACT, "An offer expires in less than a day",
                        "Accept or decline it before then, or it expires and the brand is refunded.",
                        {"contract_id": contract.id}, include_guardian=True)
                else:
                    await self.svc.notifications.notify(
                        contract.hirer_id, NotificationType.CONTRACT, "Script changes are waiting for you",
                        "Respond within a day, or the offer expires and you are refunded.", {"contract_id": contract.id})
                sent += 1

        # Work delivered: the hirer has a day left to approve or dispute before funds release.
        for contract in await self.repo(Contract).list(
                Contract.status == ContractStatus.DELIVERED, Contract.review_ends_at.is_not(None),
                Contract.review_ends_at <= soon, Contract.review_ends_at > now):
            if await self._once(f"review:{contract.id}"):
                await self.svc.notifications.notify(
                    contract.hirer_id, NotificationType.CONTRACT, "One day left to review delivered work",
                    "Approve it or open a dispute. Otherwise the payment is released automatically tomorrow.",
                    {"contract_id": contract.id})
                sent += 1

        # A likeness term ending in three days.
        for contract in await self.repo(Contract).list(
                Contract.type == ContractType.LIKENESS, Contract.status == ContractStatus.ACTIVE,
                Contract.end.is_not(None), Contract.end <= now + timedelta(days=3), Contract.end > now):
            if await self._once(f"term:{contract.id}"):
                for party in (contract.hirer_id, contract.earner_id):
                    await self.svc.notifications.notify(
                        party, NotificationType.CONTRACT, "This likeness hire ends in 3 days",
                        "After it ends, no new use is allowed. Posted videos stay up for the agreed time.",
                        {"contract_id": contract.id}, include_guardian=party == contract.earner_id)
                sent += 1

        # A guardian who has not yet approved a minor's contract, two days before it expires.
        for confirmation in await self.repo(GuardianConfirmation).list(
                GuardianConfirmation.result.in_([GuardianResult.AWAITING_APP_APPROVAL, GuardianResult.AWAITING_CALL]),
                GuardianConfirmation.expires_at <= now + timedelta(days=2), GuardianConfirmation.expires_at > now):
            if await self._once(f"guardian:{confirmation.id}"):
                await self.svc.notifications.notify(
                    confirmation.guardian_id, NotificationType.CONTRACT, "A contract for your child is waiting for you",
                    "Approve it in the app and answer the confirmation call, or it expires in 2 days.",
                    {"contract_id": confirmation.contract_id, "minor_id": confirmation.minor_id})
                sent += 1
        return sent
