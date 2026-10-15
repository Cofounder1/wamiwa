"""Lifecycle emails written in Wamiwa's own voice (as opposed to short system notices)."""
from app.models import Account
from app.services.base import BaseService

SIGNATURE = "Damilola Oladimeji\nFounder & CEO, Wamiwa"


class WelcomeEmailService(BaseService):
    """The founder's welcome, sent once an account is ready to use. A 13-17 account is only ready once
    its guardian is linked, so theirs waits until then; companies get a version about verification."""

    @staticmethod
    def first_name(account: Account) -> str:
        name = (account.display_name or "").strip()
        return name.split()[0] if name else account.handle

    @classmethod
    def compose(cls, account: Account, *, minor: bool = False) -> tuple[str, str]:
        if account.is_company:
            body = (f"Hi {account.display_name or account.handle} team,\n\n"
                    "I'm Damilola, and I'm really glad you're here.\n\n"
                    "Your company account is ready. To pay, hire creators or use our APIs, verify your business "
                    "first: it takes a few minutes in your company settings.\n\n"
                    "If you have questions, just reply. A real person reads every message.\n\n"
                    f"Welcome aboard!\n\n{SIGNATURE}\n")
        elif minor:
            body = (f"Hi {cls.first_name(account)},\n\n"
                    "I'm Damilola, and I'm really glad you're here.\n\n"
                    "Your guardian has linked your account, so you're ready to go. Explore, post and make "
                    "yourself at home. If you have questions, just reply. A real person reads every message.\n\n"
                    f"Welcome aboard!\n\n{SIGNATURE}\n")
        else:
            body = (f"Hi {cls.first_name(account)},\n\n"
                    "I'm Damilola, and I'm really glad you're here.\n\n"
                    "Your account is ready. Explore, play around, and make yourself at home. If you have "
                    "questions, just reply. A real person reads every message.\n\n"
                    f"Welcome aboard!\n\n{SIGNATURE}\n")
        return "Welcome to Wamiwa", body

    async def send(self, account: Account, *, minor: bool = False) -> None:
        """Sent after the transaction commits; at most once per account."""
        if not account.email or not await self.kv.set_if_absent(f"welcome:{account.id}", "1", 365 * 86400):
            return
        subject, body = self.compose(account, minor=minor)
        self.svc.notifications.email(account.email, subject, body)
