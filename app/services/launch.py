"""Going live: the one-way switch from test payment keys to live ones (see app/infra/launch.py)."""
from typing import Any

from sqlalchemy import func, select

from app.core.exceptions import Conflict, Unprocessable
from app.models import Account, EscrowTransaction, Payment, Payout, PlatformState
from app.models.finance import MoneyMovement
from app.services.base import BaseService

CONFIRM = "GO LIVE"


class LaunchService(BaseService):
    async def status(self) -> dict[str, Any]:
        """Public: whether payments are in test mode, for the site's banner."""
        state = await self.repo(PlatformState).first(PlatformState.launched_at.is_not(None))
        return {"test_mode": await self.svc.payment.test_mode(),
                "launched_at": state.launched_at.isoformat() if state else None}

    async def test_money(self) -> dict[str, int]:
        """Money records left from testing. All must be gone (run the reset) before going live."""
        counts = {}
        for name, model in (("payments", Payment), ("escrow", EscrowTransaction), ("payouts", Payout),
                            ("refunds", MoneyMovement)):
            counts[name] = (await self.db.execute(select(func.count()).select_from(model))).scalar_one()
        return counts

    async def go_live(self, admin: Account, totp_code: str | None, confirm: str) -> dict[str, Any]:
        """Super Admin only, with a fresh two-factor code and "GO LIVE" typed out. Can't be undone."""
        self.svc.admin.require_super(admin)
        self.svc.auth.require_totp(admin, totp_code)
        if confirm.strip() != CONFIRM:
            raise Unprocessable(f'Type "{CONFIRM}" to confirm', code="confirmation_required")
        if await self.repo(PlatformState).exists(PlatformState.launched_at.is_not(None)):
            raise Conflict("Wamiwa is already live", code="already_live")
        s = self.s
        if s.is_production and not (s.flutterwave_secret_key and s.flutterwave_public_key):
            raise Unprocessable("Set the live Flutterwave keys on Render first", code="live_keys_missing")
        if s.is_production and "TEST" in s.flutterwave_secret_key:
            raise Unprocessable("WAMIWA_FLUTTERWAVE_SECRET_KEY is a test key; set the live one", code="live_keys_missing")
        left = {k: v for k, v in (await self.test_money()).items() if v}
        if left:
            raise Conflict("Test payments are still recorded. Run the reset first.", code="reset_required", left=left)

        state = await self.repo(PlatformState).first() or await self.repo(PlatformState).add(PlatformState())
        state.launched_at, state.launched_by = self.now(), admin.id
        await self.svc.audit.log(admin.id, "platform.go_live", "platform", state.id,
                                 "Payments switched from test keys to live keys")
        launch = self.infra.launch

        async def switch() -> None:
            await launch.mark_live()

        self.uow.on_commit(switch)
        return {"test_mode": False, "launched_at": state.launched_at.isoformat()}
