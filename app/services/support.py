"""Epic 14: help centre, support tickets and replies."""
import secrets
from datetime import timedelta
from typing import Any

from app.core.enums import NotificationType, TicketCategory, TicketStatus
from app.core.exceptions import Conflict, NotFound, Unprocessable
from app.core.utils import Page
from app.models import Account, SupportTicket, TicketMessage
from app.services.base import BaseService

FAQ = [
    {"q": "How do I get paid?", "a": "Funds clear when the hirer approves or 7 days pass with no dispute, and are "
                                     "paid to your verified bank account every 2 weeks."},
    {"q": "What does Wamiwa charge?", "a": "20% on Hire Me and likeness contracts and 10% on agent hires. The fee "
                                           "comes out of the price; nothing is added on top."},
    {"q": "Why can't I add my bank account?", "a": "Verify your identity first. The account name must match your "
                                                   "verified ID name."},
    {"q": "How do I open a dispute?", "a": "From the contract, during the 7-day review window, or within 30 days of "
                                           "payout for misuse of your likeness."},
    {"q": "My guardian missed the confirmation call", "a": "We try 3 times over 48 hours. Your guardian can also "
                                                           "request a call from their account or through support."},
]
URGENT = {TicketCategory.PAYMENTS, TicketCategory.REPORT_A_PROBLEM}


class SupportService(BaseService):
    async def help_centre(self) -> dict[str, Any]:
        """Common questions first, then the contact form."""
        return {"faq": FAQ, "categories": [str(c) for c in TicketCategory], "contact": "POST /support/tickets"}

    def present(self, ticket: SupportTicket, messages: list[TicketMessage] | None = None) -> dict[str, Any]:
        data = {"id": ticket.id, "number": ticket.number, "category": ticket.category, "subject": ticket.subject,
                "status": ticket.status, "linked_object": ticket.linked_object,
                "on_behalf_of_minor_id": ticket.on_behalf_of_minor_id,
                "first_response_due_at": ticket.first_response_due_at.isoformat(),
                "created_at": ticket.created_at.isoformat()}
        if messages is not None:
            data["messages"] = [{"from_staff": m.from_staff, "body": m.body, "attachments": m.attachments,
                                 "at": m.created_at.isoformat()} for m in messages]
        return data

    async def create(self, account: Account, *, category: TicketCategory, subject: str, body: str,
                     attachments: list[str], linked_object: dict | None, minor_id: str | None) -> dict[str, Any]:
        await self.limiter.enforce(self.policies.support_tickets, account.id)
        if minor_id:
            await self.svc.access.require_guardian_of(account.id, minor_id)  # guardians can open tickets for minors
        if linked_object and linked_object.get("type") not in ("contract", "post", "chat"):
            raise Unprocessable("You can link a contract, post or chat", code="invalid_link")
        # First response target: 24 hours; payment and safety issues: 4 hours.
        hours = 4 if category in URGENT else 24
        ticket = await self.repo(SupportTicket).add(SupportTicket(
            number=f"WM-{secrets.randbelow(10**8):08d}", account_id=account.id, on_behalf_of_minor_id=minor_id,
            category=category, subject=subject.strip(), linked_object=linked_object,
            first_response_due_at=self.now() + timedelta(hours=hours)))
        await self.repo(TicketMessage).add(TicketMessage(
            ticket_id=ticket.id, sender_id=account.id, body=body.strip(), attachments=attachments))
        if account.email:
            self.svc.notifications.email(account.email, f"We received your request ({ticket.number})",
                                         f"Ticket {ticket.number}: {ticket.subject}. We aim to reply within {hours} hours.")
        return self.present(ticket)

    async def _mine(self, account: Account, ticket_id: str) -> SupportTicket:
        ticket = await self.repo(SupportTicket).get(ticket_id)
        if ticket is None or (ticket.account_id != account.id and not account.is_admin):
            raise NotFound("Ticket not found")
        return ticket

    async def list_mine(self, account: Account, cursor: str | None, limit: int) -> Page[dict]:
        page = await self.repo(SupportTicket).page(SupportTicket.account_id == account.id, cursor=cursor, limit=limit)
        return Page(items=[self.present(t) for t in page.items], next_cursor=page.next_cursor)

    async def get(self, account: Account, ticket_id: str) -> dict[str, Any]:
        ticket = await self._mine(account, ticket_id)
        return self.present(ticket, list(await self.repo(TicketMessage).list(TicketMessage.ticket_id == ticket.id)))

    async def reply(self, account: Account, ticket_id: str, body: str, attachments: list[str]) -> dict[str, Any]:
        ticket = await self._mine(account, ticket_id)
        if ticket.status == TicketStatus.RESOLVED:
            raise Conflict("This ticket is resolved. Open a new one if you still need help.", code="ticket_resolved")
        await self.repo(TicketMessage).add(TicketMessage(
            ticket_id=ticket.id, sender_id=account.id, body=body.strip(), attachments=attachments))
        ticket.status = TicketStatus.OPEN
        return await self.get(account, ticket_id)

    async def staff_reply(self, admin: Account, ticket_id: str, body: str, resolve: bool) -> dict[str, Any]:
        ticket = await self.repo(SupportTicket).require(ticket_id, "Ticket not found")
        await self.repo(TicketMessage).add(TicketMessage(
            ticket_id=ticket.id, sender_id=admin.id, from_staff=True, body=body.strip()))
        ticket.first_responded_at = ticket.first_responded_at or self.now()
        ticket.assigned_to = ticket.assigned_to or admin.id
        ticket.status = TicketStatus.RESOLVED if resolve else TicketStatus.WAITING_ON_USER
        await self.svc.notifications.notify(
            ticket.account_id, NotificationType.SECURITY, f"Support replied to {ticket.number}", body,
            {"ticket_id": ticket.id})
        return await self.get(admin, ticket_id)

    async def staff_list(self, status: TicketStatus | None, cursor: str | None, limit: int) -> Page[dict]:
        where = [SupportTicket.status == status] if status else []
        page = await self.repo(SupportTicket).page(*where, cursor=cursor, limit=limit, newest_first=False)
        now = self.now()
        return Page(items=[{**self.present(t), "overdue": t.first_responded_at is None and t.first_response_due_at < now}
                           for t in page.items], next_cursor=page.next_cursor)
