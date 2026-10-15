"""Operational commands.

    python -m app.cli create-super-admin <email> <handle>   # prompts for a password; run for two people
    python -m app.cli run-job <job name>                    # e.g. payouts.run_cycle
    python -m app.cli list-jobs
    python -m app.cli seed-demo                             # development only: demo accounts for the frontend
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


async def seed_demo() -> None:
    from app.demo_seed import DEMO_PASSWORD, seed

    result = await seed(get_settings())
    print(f"Demo accounts created. Password for all of them: {DEMO_PASSWORD}\n")
    for role, email, note in result.accounts:
        print(f"  {role:<12} {email:<24} {note}")
    print("\nSuper Admins also need a code from an authenticator app. Add these secrets to one:")
    for email, secret in result.admin_totp.items():
        print(f"  {email:<24} {secret}")


def main(argv: list[str]) -> None:
    if len(argv) == 3 and argv[0] == "create-super-admin":
        asyncio.run(create_super_admin(argv[1], argv[2]))
    elif len(argv) == 2 and argv[0] == "run-job":
        asyncio.run(run_job(argv[1]))
    elif argv == ["seed-demo"]:
        asyncio.run(seed_demo())
    elif argv == ["list-jobs"]:
        for job in JOBS:
            print(f"{job.name:45s} every {job.interval_seconds}s")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
