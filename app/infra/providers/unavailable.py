"""Used on the live site in place of a stand-in that would approve everything.

Until the real payment provider (Flutterwave) and ID provider (YouVerify) are connected, the stand-ins would let anyone
"pay" for plans, fund escrow with no money, or pass ID checks. In production these refuse instead, with a clear
message; free features keep working. Local development and tests keep the stand-ins.

PayPal has no stand-in at all: until its keys are set it refuses everywhere, and people pay through Flutterwave.
"""
from app.core.exceptions import ServiceUnavailable
from app.infra.providers.base import IdentityGateway, IdentityResult, PaymentGateway, PayPalCheckout, TransferResult


class PaymentsNotConnected(PaymentGateway):
    MESSAGE = "Payments aren't available on Wamiwa yet. Free features work as usual."

    def _refuse(self):
        raise ServiceUnavailable(self.MESSAGE, code="payments_unavailable")

    async def charge(self, **_):
        self._refuse()

    async def refund(self, **_) -> TransferResult:
        self._refuse()

    async def transfer(self, **_) -> TransferResult:
        self._refuse()

    async def resolve_account_name(self, bank_code: str, account_number: str, *args, **kwargs):
        self._refuse()

    async def held_balance(self, currency: str) -> int:
        return 0  # nothing can have been collected, so reconciliation has nothing to compare


class PayPalNotConnected(PaymentsNotConnected, PayPalCheckout):
    MESSAGE = "PayPal isn't available on Wamiwa yet. Pay by card or bank transfer instead."

    def _refuse(self):
        raise ServiceUnavailable(self.MESSAGE, code="paypal_unavailable")

    async def create_order(self, **_) -> str:
        self._refuse()


class IdentityNotConnected(IdentityGateway):
    MESSAGE = "ID verification isn't available on Wamiwa yet. We'll let you know when it opens."

    def _refuse(self):
        raise ServiceUnavailable(self.MESSAGE, code="identity_unavailable")

    async def verify(self, document_type: str, document_ref: str, selfie_ref: str,
                     liveness_ref: str | None = None) -> IdentityResult:
        self._refuse()

    async def face_matches(self, face_ref: str, photo_ref: str) -> bool:
        self._refuse()

    async def is_protected_identity(self, name: str) -> bool:
        self._refuse()
