"""Clearing the test data before launch. Run once, by hand, from a laptop:

    python -m app.cli reset-test-data --database-url "<live database address>"                       # shows what goes
    python -m app.cli reset-test-data --database-url "<live database address>" --confirm wamiwa.site  # deletes it

Kept: the admin accounts and their permissions, the Wamiwa Agent and the company that owns it, the B2B category map,
the Suggested-sellers keywords, pending admin invites and the audit log (with an entry for this reset). Everything
else goes: test accounts, posts, payments, contracts, payouts, uploads, notifications and queues.

Refused once Wamiwa has gone live, from anywhere, so real data can never be wiped this way. Then a Super Admin
presses Go live, which switches payments to the live keys.
"""
from dataclasses import dataclass

from sqlalchemy import Table, delete, func, or_, select

import app.models  # noqa: F401 - every table registered
from app.core.config import Settings
from app.core.enums import AccountType
from app.infra.database import Database, UnitOfWork
from app.models import Account, AuditLog, PlatformState
from app.models.base import Model

KEEP_ALL = {"audit_logs", "b2b_category_links", "promo_category_keywords", "admin_invites", "platform_state"}
# Rows kept when every account they point to is kept (an admin's permissions, the Wamiwa Agent's profile).
KEEP_IF_KEPT_ACCOUNTS = {"accounts", "admin_permissions", "user_profiles", "company_profiles", "agent_profiles",
                         "agent_integrations"}


class AlreadyLive(RuntimeError):
    pass


@dataclass
class ResetPlan:
    kept_accounts: list[str]
    deletions: dict[str, int]  # table -> rows to delete

    @property
    def total(self) -> int:
        return sum(self.deletions.values())


async def _kept_accounts(uow: UnitOfWork, settings: Settings) -> list[str]:
    handle = settings.wamiwa_agent_handle
    rows = await uow.session.execute(select(Account.id).where(or_(
        Account.admin_role.is_not(None),
        (Account.handle == handle) & (Account.type == AccountType.AGENT),
        Account.handle == f"{handle}_official")))
    return [r[0] for r in rows]


def _doomed(table: Table, kept: list[str]):
    """The WHERE clause for the rows of `table` that the reset deletes, or None to keep them all."""
    if table.name in KEEP_ALL:
        return None
    if table.name == "accounts":
        return table.c.id.not_in(kept)
    if table.name in KEEP_IF_KEPT_ACCOUNTS:
        refs = [fk.parent for fk in table.foreign_keys if fk.column.table.name == "accounts"]
        return or_(*(col.not_in(kept) for col in refs)) if refs else None
    return True  # everything else is test activity


async def plan(uow: UnitOfWork, settings: Settings) -> ResetPlan:
    if (await uow.session.execute(select(PlatformState.id).where(PlatformState.launched_at.is_not(None)))).first():
        raise AlreadyLive("Wamiwa is live. The reset only clears test data from before launch.")
    kept = await _kept_accounts(uow, settings)
    deletions = {}
    for table in Model.metadata.sorted_tables:
        where = _doomed(table, kept)
        if where is None:
            continue
        query = select(func.count()).select_from(table)
        if where is not True:
            query = query.where(where)
        count = (await uow.session.execute(query)).scalar_one()
        if count:
            deletions[table.name] = count
    return ResetPlan(kept, deletions)


async def reset(uow: UnitOfWork, settings: Settings, *, execute: bool) -> ResetPlan:
    """Work out what goes and, if `execute`, delete it in this transaction (committed by the caller)."""
    result = await plan(uow, settings)
    if execute and result.total:
        for table in reversed(Model.metadata.sorted_tables):  # rows that point at others go first
            where = _doomed(table, result.kept_accounts)
            if where is not None and table.name in result.deletions:
                await uow.session.execute(delete(table) if where is True else delete(table).where(where))
        uow.session.add(AuditLog(actor_id="reset-test-data", action="platform.reset_test_data",
                                 target_type="platform", target_id="test-data",
                                 reason="Test data cleared before launch", details=result.deletions))
    return result


async def run(settings: Settings, *, execute: bool) -> ResetPlan:
    """The command: its own connection to the database given, one transaction."""
    db = Database(settings.database_url, schema=settings.database_schema)
    try:
        async with db.unit_of_work() as uow:
            return await reset(uow, settings, execute=execute)
    finally:
        await db.dispose()
