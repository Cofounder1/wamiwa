"""Epic 10: one-to-one chats, message requests, contract and order threads, chat safety."""
import re
from typing import Any

from sqlalchemy import select, update

from app.core.enums import AccountStatus, ChatTab, ContractType, ConversationType, Queue
from app.core.exceptions import Forbidden, NotFound, Unprocessable
from app.core.utils import Page
from app.models import (
    Account, Contract, Conversation, ConversationParticipant, LikenessPage, Message,
)
from app.services.base import BaseService

ATTACHMENT_KINDS = {"image", "video", "voice", "document"}
OFF_PLATFORM_PATTERNS = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),  # email address
    re.compile(r"(?<!\d)(?:\+?\d[\s-]?){10,14}(?!\d)"),  # phone or bank account number
    re.compile(r"\b(?:account\s*(?:number|no)|acct|iban|sort\s*code|whatsapp|telegram)\b", re.I),
)
OFF_PLATFORM_WARNING = ("Keep paid work on Wamiwa. Deals made off-platform get no dispute support, and repeated "
                        "attempts to move them can lead to suspension.")
CANNOT_MESSAGE = "You can't message this account"


class MessagingService(BaseService):
    @staticmethod
    def _pair_key(a: str, b: str) -> str:
        return ":".join(sorted((a, b)))

    def present_message(self, message: Message, viewer_id: str | None = None) -> dict[str, Any]:
        data = {"id": message.id, "conversation_id": message.conversation_id, "sender_id": message.sender_id,
                "body": message.body, "attachments": message.attachments, "created_at": message.created_at.isoformat(),
                "read_at": message.read_at.isoformat() if message.read_at else None}
        if message.off_platform_warning:
            data["warning"] = OFF_PLATFORM_WARNING  # HM-02: both sides see the warning
        return data

    # --- who can start a chat --------------------------------------------------------------------
    async def _guard_start(self, sender: Account, recipient: Account) -> ConversationType:
        """The Epic 10 "who can start a chat" table."""
        if sender.is_agent or recipient.is_agent:
            raise Forbidden("Agents talk to hirers only inside an order thread", code="agents_cannot_chat")
        if recipient.is_admin or sender.is_admin or recipient.status != AccountStatus.ACTIVE:
            raise Forbidden(CANNOT_MESSAGE, code="cannot_message")
        if sender.is_user and recipient.is_user:
            await self.limiter.enforce(self.policies.new_conversations, sender.id)
            return ConversationType.DIRECT
        if sender.is_user:  # human -> company
            company = await self.svc.access.company(recipient.id)
            if not company or not company.allow_user_messages:
                raise Forbidden(CANNOT_MESSAGE, code="company_messages_off")
            if await self.repo(LikenessPage).exists(LikenessPage.account_id == sender.id):
                # SB-01: contacting companies first about likeness is a subscriber feature.
                await self.svc.access.require_subscription(sender, "Contacting companies first about your likeness")
            await self.limiter.enforce(self.policies.new_conversations, sender.id)
            return ConversationType.BUSINESS
        if recipient.is_company:  # company -> company
            await self.svc.b2b.guard_new_conversation(sender)
            return ConversationType.BUSINESS
        # company -> human: only verified companies DM about work; minors need the guardian's approval.
        await self.svc.access.require_verified_company(sender, "message people about work")
        if await self.svc.access.is_minor(recipient.id) and not await self.svc.access.company_approved_for_minor(
                recipient.id, sender.id):
            raise Forbidden("This account's guardian has not approved your company", code="guardian_approval_required")
        await self.limiter.enforce(self.policies.new_conversations, sender.id)
        return ConversationType.BUSINESS

    async def _start(self, sender: Account, recipient: Account) -> Conversation:
        kind = await self._guard_start(sender, recipient)
        conversation = await self.repo(Conversation).add(Conversation(
            type=kind, pair_key=self._pair_key(sender.id, recipient.id), created_by=sender.id))
        if sender.is_company:
            recipient_tab, accepted = ChatTab.BUSINESS, True  # MS-02: company messages land in Business
        elif await self.svc.follows.is_following(recipient.id, sender.id) or recipient.is_company:
            recipient_tab, accepted = ChatTab.BUSINESS if recipient.is_company else ChatTab.PRIMARY, True
        else:
            recipient_tab, accepted = ChatTab.REQUESTS, False  # first message from someone I don't follow
        await self.repo(ConversationParticipant).add(ConversationParticipant(
            conversation_id=conversation.id, account_id=sender.id,
            tab=ChatTab.BUSINESS if recipient.is_company else ChatTab.PRIMARY))
        await self.repo(ConversationParticipant).add(ConversationParticipant(
            conversation_id=conversation.id, account_id=recipient.id, tab=recipient_tab, accepted=accepted))
        return conversation

    # --- sending ---------------------------------------------------------------------------------
    def _validate(self, body: str, attachments: list[dict]) -> list[dict]:
        if not body.strip() and not attachments:
            raise Unprocessable("Write a message or attach a file", code="empty_message")
        cleaned = []
        for item in attachments[:10]:
            if item.get("kind") not in ATTACHMENT_KINDS or not item.get("ref"):
                raise Unprocessable("Attachments can be images, videos, voice notes or documents", code="invalid_attachment")
            if int(item.get("size_bytes", 0)) > self.s.max_attachment_bytes:
                raise Unprocessable("Attachments can be up to 25 MB", code="attachment_too_large")
            cleaned.append({"kind": item["kind"], "ref": item["ref"], "size_bytes": int(item.get("size_bytes", 0))})
        return cleaned

    async def _store(self, conversation: Conversation, sender: Account, body: str, attachments: list[dict],
                     paid_work: bool) -> Message:
        await self.limiter.enforce(self.policies.messages, sender.id)
        warn = paid_work and any(pattern.search(body) for pattern in OFF_PLATFORM_PATTERNS)
        message = await self.repo(Message).add(Message(
            conversation_id=conversation.id, sender_id=sender.id, body=body.strip(), attachments=attachments,
            off_platform_warning=warn))
        conversation.last_message_at = self.now()
        await self.db.execute(update(ConversationParticipant).where(
            ConversationParticipant.conversation_id == conversation.id).values(deleted_at=None))
        await self._tell_recipients(conversation, sender)
        if warn:
            attempts = await self.kv.incr(f"offplatform:{sender.id}", ttl=30 * 86400)
            if attempts == self.s.off_platform_warnings_before_review:
                await self.svc.queues.enqueue(
                    Queue.REPORTS, "off_platform", sender.id, subject_account_id=sender.id,
                    summary="Repeated attempts to move paid work off-platform")
        return message

    async def _tell_recipients(self, conversation: Conversation, sender: Account) -> None:
        """A new-message notice, at most once an hour per conversation, so a busy chat is not a flood of email."""
        from app.core.enums import NotificationType

        others = await self.repo(ConversationParticipant).list(
            ConversationParticipant.conversation_id == conversation.id, ConversationParticipant.account_id != sender.id)
        for other in others:
            if other.account_id == sender.id or not await self.kv.set_if_absent(
                    f"msgnotice:{conversation.id}:{other.account_id}", "1", 3600):
                continue
            kind = {"order": "your order", "contract": "your contract"}.get(str(conversation.type), "Wamiwa")
            await self.svc.notifications.notify(
                other.account_id, NotificationType.MESSAGE, f"New message from @{sender.handle}",
                f"You have a new message in {kind}.", {"conversation_id": conversation.id})

    async def send_direct(self, sender: Account, recipient_id: str, body: str, attachments: list[dict]) -> dict:
        if recipient_id == sender.id:
            raise Unprocessable("You cannot message yourself", code="self_message")
        recipient = await self.repo(Account).get(recipient_id)
        if recipient is None or not recipient.is_visible:
            raise NotFound("Account not found")
        if await self.svc.blocks.either(sender.id, recipient.id):
            # MS-03: blocking does not cancel contracts; their messages move to the contract thread.
            thread = await self._shared_contract_thread(sender.id, recipient.id)
            raise Forbidden(CANNOT_MESSAGE, code="blocked",
                            **({"contract_thread_id": thread} if thread else {}))
        attachments = self._validate(body, attachments)
        conversation = await self.repo(Conversation).first(
            Conversation.pair_key == self._pair_key(sender.id, recipient.id))
        if conversation is None:
            conversation = await self._start(sender, recipient)
        paid_work = conversation.type == ConversationType.BUSINESS
        message = await self._store(conversation, sender, body, attachments, paid_work)
        return self.present_message(message)

    async def _shared_contract_thread(self, a: str, b: str) -> str | None:
        contract = await self.repo(Contract).first(
            ((Contract.hirer_id == a) & (Contract.earner_id == b)) | ((Contract.hirer_id == b) & (Contract.earner_id == a)),
            Contract.conversation_id.is_not(None), Contract.status.in_(["active", "delivered", "disputed"]))
        return contract.conversation_id if contract else None

    async def _participant(self, account_id: str, conversation_id: str) -> tuple[Conversation, ConversationParticipant]:
        conversation = await self.repo(Conversation).get(conversation_id)
        participant = await self.repo(ConversationParticipant).first(
            ConversationParticipant.conversation_id == conversation_id, ConversationParticipant.account_id == account_id)
        if conversation is None or participant is None:
            raise NotFound("Conversation not found")
        return conversation, participant

    async def send_in_conversation(self, sender: Account, conversation_id: str, body: str, attachments: list[dict]) -> dict:
        conversation, _ = await self._participant(sender.id, conversation_id)
        threaded = conversation.type in (ConversationType.ORDER, ConversationType.CONTRACT)
        if sender.is_agent and conversation.type != ConversationType.ORDER:
            raise Forbidden("Agents talk to hirers only inside an order thread", code="agents_cannot_chat")
        if not threaded:
            others = await self.repo(ConversationParticipant).list(
                ConversationParticipant.conversation_id == conversation_id,
                ConversationParticipant.account_id != sender.id)
            for other in others:
                if await self.svc.blocks.either(sender.id, other.account_id):
                    raise Forbidden(CANNOT_MESSAGE, code="blocked")
        message = await self._store(conversation, sender, body, self._validate(body, attachments),
                                    paid_work=conversation.type != ConversationType.DIRECT)
        return self.present_message(message)

    # --- contract and order threads ------------------------------------------------------------
    async def open_thread(self, type: ConversationType, contract_id: str, participant_ids: list[str],
                          created_by: str) -> Conversation:
        existing = await self.repo(Conversation).first(Conversation.contract_id == contract_id)
        if existing:
            return existing
        conversation = await self.repo(Conversation).add(Conversation(
            type=type, contract_id=contract_id, created_by=created_by))
        for account_id in dict.fromkeys(participant_ids):
            await self.repo(ConversationParticipant).add(ConversationParticipant(
                conversation_id=conversation.id, account_id=account_id, tab=ChatTab.BUSINESS))
        return conversation

    async def send_order_message(self, sender: Account, order_id: str, body: str) -> dict:
        contract = await self.repo(Contract).get(order_id)
        if contract is None or contract.type != ContractType.AGENT or not contract.conversation_id:
            raise NotFound("Order not found")
        return await self.send_in_conversation(sender, contract.conversation_id, body, [])

    # --- reading -----------------------------------------------------------------------------------
    async def conversations(self, account: Account, tab: ChatTab, cursor: str | None, limit: int) -> Page[dict]:
        """Chats list with Primary, Requests and Business tabs."""
        page = await self.repo(ConversationParticipant).page(
            ConversationParticipant.account_id == account.id, ConversationParticipant.tab == tab,
            ConversationParticipant.deleted_at.is_(None), cursor=cursor, limit=limit)
        conversation_ids = [p.conversation_id for p in page.items]
        conversations = await self.repo(Conversation).by_ids(conversation_ids)
        others = await self.repo(ConversationParticipant).list(
            ConversationParticipant.conversation_id.in_(conversation_ids or [""]),
            ConversationParticipant.account_id != account.id)
        accounts = await self.repo(Account).by_ids([o.account_id for o in others])
        hidden = await self.svc.blocks.hidden_ids(account.id)
        items = []
        for participant in page.items:
            conversation = conversations[participant.conversation_id]
            members = [o.account_id for o in others if o.conversation_id == conversation.id]
            threaded = conversation.type in (ConversationType.ORDER, ConversationType.CONTRACT)
            if not threaded and any(m in hidden for m in members):
                continue
            items.append({
                "id": conversation.id, "type": conversation.type, "contract_id": conversation.contract_id,
                "accepted": participant.accepted,
                "with": [{"id": m, "handle": accounts[m].handle, "type": accounts[m].type} for m in members if m in accounts],
                "last_message_at": conversation.last_message_at.isoformat() if conversation.last_message_at else None,
            })
        return Page(items=items, next_cursor=page.next_cursor)

    async def messages(self, account: Account, conversation_id: str, cursor: str | None, limit: int) -> dict[str, Any]:
        conversation, participant = await self._participant(account.id, conversation_id)
        page = await self.repo(Message).page(Message.conversation_id == conversation_id, cursor=cursor, limit=limit)
        profile = await self.svc.access.profile(account.id)
        if participant.accepted and (profile is None or profile.read_receipts):
            # Read receipts, which users can turn off.
            await self.db.execute(update(Message).where(
                Message.conversation_id == conversation_id, Message.sender_id != account.id,
                Message.read_at.is_(None)).values(read_at=self.now()))
        participant.last_read_at = self.now()
        typing = []
        for other in await self.repo(ConversationParticipant).list(
                ConversationParticipant.conversation_id == conversation_id,
                ConversationParticipant.account_id != account.id):
            if await self.kv.get(f"typing:{conversation_id}:{other.account_id}"):
                typing.append(other.account_id)
        return {"conversation": {"id": conversation.id, "type": conversation.type, "accepted": participant.accepted},
                "messages": [self.present_message(m) for m in page.items], "typing": typing, **page.meta()}

    async def set_typing(self, account: Account, conversation_id: str) -> None:
        await self._participant(account.id, conversation_id)
        profile = await self.svc.access.profile(account.id)
        if profile is None or profile.typing_indicators:
            await self.kv.set(f"typing:{conversation_id}:{account.id}", "1", 6)

    async def respond_to_request(self, account: Account, conversation_id: str, accept: bool) -> dict[str, Any]:
        """MS-02: accept or delete a message request (blocking goes through the block endpoint)."""
        _, participant = await self._participant(account.id, conversation_id)
        if accept:
            participant.accepted, participant.tab = True, ChatTab.PRIMARY
        else:
            participant.deleted_at = self.now()
        return {"conversation_id": conversation_id, "accepted": participant.accepted, "deleted": not accept}

    # --- oversight ---------------------------------------------------------------------------------
    async def guardian_view(self, guardian: Account, minor_id: str) -> list[dict[str, Any]]:
        """MS-04: guardians can see their minor's chats with companies."""
        await self.svc.access.require_guardian_of(guardian.id, minor_id)
        stmt = (select(Conversation).join(ConversationParticipant,
                                          ConversationParticipant.conversation_id == Conversation.id)
                .where(ConversationParticipant.account_id == minor_id,
                       Conversation.type.in_([ConversationType.BUSINESS, ConversationType.CONTRACT])))
        result = []
        for conversation in (await self.db.execute(stmt)).scalars().all():
            messages = await self.repo(Message).list(Message.conversation_id == conversation.id,
                                                     order_by=Message.id.desc(), limit=200)
            result.append({"conversation_id": conversation.id, "type": conversation.type,
                           "messages": [self.present_message(m) for m in reversed(messages)]})
        return result

    async def admin_view(self, conversation_id: str) -> dict[str, Any]:
        """Admins see contract and order threads in full, and elsewhere only reported messages."""
        conversation = await self.repo(Conversation).require(conversation_id, "Conversation not found")
        where = [Message.conversation_id == conversation_id]
        if conversation.type not in (ConversationType.ORDER, ConversationType.CONTRACT):
            where.append(Message.reported.is_(True))
        messages = await self.repo(Message).list(*where, limit=500)
        return {"conversation_id": conversation.id, "type": conversation.type,
                "messages": [self.present_message(m) for m in messages]}
