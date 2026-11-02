"""Operational commands.

    python -m app.cli create-super-admin <email> <handle>   # prompts for a password; run for two people
    python -m app.cli run-job <job name>                    # e.g. payouts.run_cycle
    python -m app.cli list-jobs
    python -m app.cli run-scheduler                         # runs every job on its interval until stopped
    python -m app.cli seed-demo                             # development only: demo accounts for the frontend
    python -m app.cli reset-test-data --database-url URL [--confirm wamiwa.site]
                                                            # before launch only: clear test data (see app/reset.py)
"""
import asyncio
import getpass
import sys

from app.container import build_infrastructure
from app.core.config import get_settings
from app.jobs.runner import JOBS, JobRunner
from app.services.base import ServiceContext


async def create_super_admin(email: str, handle: str) -> None:
    password = getpass.getpass("Password (12+ characters): ")
    if len(password) < 12:
        raise SystemExit("Password too short")
    infra = build_infrastructure(get_settings())
    if not infra.settings.is_production:
        await infra.db.create_all()
    async with infra.db.unit_of_work() as uow:
        created = await ServiceContext(infra, uow).services.admin.bootstrap_super_admin(email, password, handle)
    print(f"Super Admin created: {created['id']}")
    print(f"Add this to an authenticator app now (shown once): {created['otpauth_url']}")
    await infra.db.dispose()


async def run_job(name: str) -> None:
    infra = build_infrastructure(get_settings())
    print(await JobRunner(infra).run_job(name))
    await infra.db.dispose()


async def run_scheduler() -> None:
    """The worker process: runs due jobs every 30 seconds, for when the API runs with WAMIWA_RUN_JOBS=false."""
    infra = build_infrastructure(get_settings())
    runner = JobRunner(infra)
    runner.start()
    try:
        await asyncio.Event().wait()  # until the process is stopped
    finally:
        await runner.stop()
        await infra.kv.close()
        await infra.db.dispose()


async def seed_demo() -> None:
    from app.demo_seed import DEMO_PASSWORD, seed

    result = await seed(get_settings())
    print(f"Demo accounts created. Password for all of them: {DEMO_PASSWORD}\n")
    for role, email, note in result.accounts:
        print(f"  {role:<12} {email:<24} {note}")
    print("\nSuper Admins also need a code from an authenticator app. Add these secrets to one:")
    for email, secret in result.admin_totp.items():
        print(f"  {email:<24} {secret}")


async def reset_test_data(database_url: str, confirm: str | None) -> None:
    from urllib.parse import urlparse

    from app.reset import AlreadyLive, run

    base = get_settings()
    settings = base.model_copy(update={"database_url": database_url})
    site = urlparse(base.public_base_url).hostname or "wamiwa.site"
    execute = confirm == site
    if confirm is not None and not execute:
        raise SystemExit(f"--confirm must be the site's name: {site}")
    try:
        result = await run(settings, execute=execute)
    except AlreadyLive as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Kept: {len(result.kept_accounts)} accounts (admins and the Wamiwa Agent), the category maps and the audit log.")
    if not result.total:
        print("Nothing to delete.")
        return
    print(f"{'Deleted' if execute else 'Would delete'} {result.total} rows:")
    for table, count in sorted(result.deletions.items()):
        print(f"  {table:34s} {count}")
    if not execute:
        print(f"\nNothing was deleted. To delete these, run again with --confirm {site}")
    else:
        print("\nDone. Next: press Go live in the admin panel (Super Admin) to switch payments to the live keys.")


def main(argv: list[str]) -> None:
    if len(argv) == 3 and argv[0] == "create-super-admin":
        asyncio.run(create_super_admin(argv[1], argv[2]))
    elif len(argv) == 2 and argv[0] == "run-job":
        asyncio.run(run_job(argv[1]))
    elif argv == ["run-scheduler"]:
        try:
            asyncio.run(run_scheduler())
        except KeyboardInterrupt:
            pass
    elif argv and argv[0] == "reset-test-data":
        options = dict(zip(argv[1::2], argv[2::2]))
        if "--database-url" not in options or len(argv) % 2 == 0:
            raise SystemExit("reset-test-data needs --database-url <the database to clear> (no default, on purpose)")
        asyncio.run(reset_test_data(options["--database-url"], options.get("--confirm")))
    elif argv == ["seed-demo"]:
        asyncio.run(seed_demo())
    elif argv == ["list-jobs"]:
        for job in JOBS:
            print(f"{job.name:45s} every {job.interval_seconds}s")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
