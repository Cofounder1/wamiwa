"""MS-05 chat summaries and insights, done by a hired marketplace agent.

Who can ask: Pro subscribers, and verified businesses that paid Wamiwa for anything in the last 90 days.
It is an ordinary agent order (task "chat_summary"): the agent's owner sets the price, escrow holds it,
and the hirer reviews the delivery. Before the agent sees the chat, personal information is removed, and
nothing the agent writes back may contain any (AI-05). Chats with an under-18 are never shared.
"""
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.core import pii
from app.core.enums import AccountStatus, IntegrationStatus, PaymentStatus
from app.core.exceptions import Forbidden, NotFound, Unprocessable
from app.models import (
    Account, AgentIntegration, AgentProfile, ConversationParticipant, Message, Payment,
)
from app.services.base import BaseService

TASK = "chat_summary"
MAX_MESSAGES = 300
MAX_CHARS = 20_000
PAID_WINDOW_DAYS = 90


class ChatInsightService(BaseService):
    async def require_eligible(self, account: Account) -> None:
        if account.is_user:
            await self.svc.access.require_pro(account, "Chat summaries")
            return
        if account.is_company:
            await self.svc.access.require_verified_company(account, "ask for chat summaries")
            paid = await self.repo(Payment).exists(
                Payment.account_id == account.id, Payment.status == PaymentStatus.SUCCEEDED,
                Payment.created_at >= self.now() - timedelta(days=PAID_WINDOW_DAYS))
            if not paid:
                raise Forbidden("Chat summaries are for businesses that paid for a Wamiwa service in the last 90 days",
                                code="paying_business_required")
            return
        raise Forbidden("Only people and businesses can ask for chat summaries", code="not_allowed")

    async def agents(self, account: Account) -> list[dict[str, Any]]:
        """Live marketplace agents whose owners offer chat summaries, cheapest first."""
        await self.require_eligible(account)
        rows = (await self.db.execute(
            select(AgentProfile, Account).join(Account, Account.id == AgentProfile.account_id)
            .join(AgentIntegration, AgentIntegration.agent_account_id == AgentProfile.account_id)
            .where(Account.status == AccountStatus.ACTIVE, AgentProfile.paused.is_(False),
                   AgentIntegration.status == IntegrationStatus.LIVE).limit(500))).all()
        hidden = await self.svc.blocks.hidden_ids(account.id)
        offers = [{"id": acc.id, "handle": acc.handle, "name": agent.name, "price_cents": agent.rates[TASK],
                   "rating": agent.rating_avg, "completed_orders": agent.completed_orders}
                  for agent, acc in rows
                  if TASK in (agent.rates or {}) and agent.owner_account_id != account.id
                  and agent.owner_account_id not in hidden and acc.id not in hidden]
        return sorted(offers, key=lambda o: o["price_cents"])

    async def order(self, account: Account, conversation_id: str, agent_id: str, kind: str, question: str | None,
                    payment_method: str) -> dict[str, Any]:
        await self.require_eligible(account)
        participants = await self.repo(ConversationParticipant).list(
            ConversationParticipant.conversation_id == conversation_id)
        if account.id not in {p.account_id for p in participants}:
            raise NotFound("Conversation not found")
        others = [p.account_id for p in participants if p.account_id != account.id]
        for other in others:
            if await self.svc.access.is_minor(other):
                raise Forbidden("Chats with under-18s can't be shared with agents", code="minor_chat")

        messages = list(reversed(await self.repo(Message).list(
            Message.conversation_id == conversation_id, order_by=Message.id.desc(), limit=MAX_MESSAGES)))
        if not messages:
            raise Unprocessable("This chat has no messages yet", code="empty_chat")
        names = {account.id: "You"} | {other: ("Them" if len(others) == 1 else f"Person {n}") for n, other in enumerate(others, 2)}
        removed = 0
        lines = []
        for message in messages:
            body = message.body or ""
            removed += len(pii.find(body))
            text = pii.redact(body)
            if message.attachments:
                text += f" [{len(message.attachments)} attachment(s) not shared]"
            lines.append(f"{message.created_at:%Y-%m-%d %H:%M} {names.get(message.sender_id, 'Them')}: {text.strip()}")
        transcript = "\n".join(lines)
        if len(transcript) > MAX_CHARS:  # keep the most recent part
            transcript = "[earlier messages not included]\n" + transcript[-MAX_CHARS:]

        request = "Summarize this chat." if kind == "summary" else "Give insights from this chat."
        if question and question.strip():
            request += f" Focus on: {question.strip()}"
        brief = (f"{request}\n\nPersonal details were removed before sharing. Your answer must not contain any "
                 f"personal information (phone numbers, email addresses, account, ID or card numbers).\n\n"
                 f"--- Chat ({len(messages)} messages) ---\n{transcript}")
        order = await self.svc.agent_orders.place(account, agent_id, TASK, brief, payment_method)
        return {**order, "shared_messages": len(messages), "personal_details_removed": removed}
