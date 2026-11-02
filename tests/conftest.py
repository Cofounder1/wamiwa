"""Test harness: an in-memory app with a frozen clock, sandbox providers and a fake agent webhook."""
import json
import zlib
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.container import build_infrastructure, build_providers
from app.core.config import Settings
from app.core.security import WebhookSigner
from app.core.utils import FrozenClock
from app.infra.providers.base import WebhookResponse, WebhookTransport
from app.jobs.runner import JobRunner
from app.main import create_app
from app.models import Account
from app.models.base import Base
from app.services.base import ServiceContext

ADULT_DOB = "1990-05-01"
PASSWORD = "correct-horse-battery"


class FakeAgentEndpoint(WebhookTransport):
    """Stands in for an owner's externally hosted agent: echoes test challenges, records events."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.headers: list[dict] = []
        self.fail = False

    async def post(self, url: str, body: bytes, headers: dict[str, str]) -> WebhookResponse:
        if self.fail:
            return WebhookResponse(500, "down")
        event = json.loads(body)
        self.events.append(event)
        self.headers.append({**headers, "_body": body})
        if event["type"] == "test.ping":
            return WebhookResponse(200, json.dumps({"challenge": event["data"]["challenge"]}))
        return WebhookResponse(200, "{}")

    def of_type(self, event_type: str) -> list[dict]:
        return [e for e in self.events if e["type"] == event_type]


@dataclass
class Actor:
    id: str
    handle: str
    email: str
    token: str
    headers: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.headers = {"Authorization": f"Bearer {self.token}"}


class Api:
    def __init__(self, client: httpx.AsyncClient, infra, agent_endpoint: FakeAgentEndpoint):
        self.client, self.infra, self.agent_endpoint = client, infra, agent_endpoint
        self.clock: FrozenClock = infra.clock
        self.jobs = JobRunner(infra)
        self._counter = 0

    # --- plumbing ----------------------------------------------------------------------------------
    async def call(self, method: str, path: str, actor: Actor | None = None, expect: int | None = None, **kwargs):
        headers = kwargs.pop("headers", {}) | (actor.headers if actor else {})
        response = await self.client.request(method, path, headers=headers, **kwargs)
        if expect is not None:
            assert response.status_code == expect, f"{method} {path} -> {response.status_code}: {response.text}"
        return response

    async def ok(self, method: str, path: str, actor: Actor | None = None, **kwargs):
        response = await self.call(method, path, actor, **kwargs)
        assert response.status_code < 300, f"{method} {path} -> {response.status_code}: {response.text}"
        return response.json()["data"]

    def services(self):
        """Async context manager giving direct access to services inside one transaction."""
        infra = self.infra

        class _Scope:
            async def __aenter__(self_inner):
                self_inner.uow = infra.db.unit_of_work()
                return ServiceContext(infra, self_inner.uow).services

            async def __aexit__(self_inner, exc_type, exc, tb):
                await self_inner.uow.__aexit__(exc_type, exc, tb)

        return _Scope()

    def last_code(self, outbox: list[dict], to: str) -> str:
        message = next(m for m in reversed(outbox) if m["to"] == to)
        return re.search(r"\b(\d{6})\b", message["body"]).group(1)

    def email_code(self, email: str) -> str:
        return self.last_code(self.infra.providers.email.outbox, email)

    def sms_code(self, phone: str) -> str:
        return self.last_code(self.infra.providers.sms.outbox, phone)

    # --- accounts -----------------------------------------------------------------------------------
    async def signup(self, handle: str, kind: str = "user", dob: str | None = ADULT_DOB, name: str | None = None,
                     category: str | None = None, region: str | None = None) -> Actor:
        email = f"{handle}@example.com"
        await self.ok("POST", "/auth/signup/request-otp", json={"email": email})
        token = (await self.ok("POST", "/auth/signup/verify-otp",
                               json={"email": email, "code": self.email_code(email)}))["signup_token"]
        body = {"signup_token": token, "account_type": kind, "password": PASSWORD, "handle": handle,
                "name": name or handle.title(), "category": category, "region": region}
        if kind == "user":
            body["date_of_birth"] = dob
        data = await self.ok("POST", "/auth/signup/complete", json=body)
        return Actor(data["account"]["id"], handle, email, data["access_token"])

    async def verify_id(self, actor: Actor, name: str, dob: str = ADULT_DOB) -> dict:
        return await self.ok("POST", "/verification/identity", actor, json={
            "document_type": "passport", "document_ref": f"{name}|{dob}", "selfie_ref": f"selfie-{actor.handle}"})

    async def influencer(self, handle: str, name: str | None = None, followers: int = 0, niche: str = "fashion") -> Actor:
        name = name or f"{handle.title()} Person"
        actor = await self.signup(handle, name=name)
        await self.verify_id(actor, name)
        await self.ok("PATCH", "/accounts/me/profile", actor, json={"primary_niche": niche})
        if followers:
            await self.set_followers(actor, followers)
        return actor

    async def set_followers(self, actor: Actor, followers: int) -> None:
        async with self.services() as svc:
            account = await svc.accounts.repo(Account).require(actor.id)
            account.followers_human = followers
            await svc.likeness_pages.refresh_eligibility(account)

    async def verify_phone(self, actor: Actor, phone: str) -> None:
        await self.ok("POST", "/accounts/me/phone", actor, json={"phone": phone})
        await self.ok("POST", "/accounts/me/phone/verify", actor, json={"code": self.sms_code(phone)})

    async def admin(self, handle: str = "root") -> Actor:
        async with self.services() as svc:
            created = await svc.admin.bootstrap_super_admin(f"{handle}@wamiwa-admin.com", PASSWORD, handle)
        return await self.login(f"{handle}@wamiwa-admin.com", handle, created["totp_secret"])

    async def login(self, email: str, handle: str, totp_secret: str | None = None) -> Actor:
        body = {"email": email, "password": PASSWORD}
        if totp_secret:
            body["totp_code"] = self.infra.totp.now(totp_secret)
        data = await self.ok("POST", "/auth/login", json=body)
        return Actor(data["account"]["id"], handle, email, data["access_token"])

    async def decide(self, admin: Actor, queue: str, target_id: str, decision: str = "approve",
                     payload: dict | None = None, reason: str = "Checked against the source") -> dict:
        items = await self.ok("GET", f"/admin/queues/{queue}", admin)
        item = next(i for i in items if i["target_id"] == target_id)
        await self.ok("POST", f"/admin/queue-items/{item['id']}/claim", admin)
        return await self.ok("POST", f"/admin/queue-items/{item['id']}/decide", admin,
                             json={"decision": decision, "reason": reason, "payload": payload or {}})

    async def company(self, handle: str, admin: Actor, category: str = "fashion", region: str | None = None) -> Actor:
        actor = await self.signup(handle, kind="company", name=f"{handle.title()} Ltd", category=category, region=region)
        await self.ok("POST", "/companies/verification", actor,
                      json={"registration_no": f"RC{zlib.crc32(handle.encode()) % 10**7}", "document_ref": "docs/cac.pdf"})
        await self.decide(admin, "company_verification", actor.id)
        return actor

    async def bank(self, actor: Actor, name: str, number: str = "0123456789") -> dict:
        return await self.ok("PUT", "/verification/bank-account", actor,
                             json={"bank_code": "058", "account_number": number, "account_name": name})

    async def live_agent(self, owner: Actor, handle: str = "helperbot", cap: int = 20, rates: dict | None = None) -> dict:
        agent = await self.ok("POST", "/agents", owner, json={
            "name": handle.title(), "handle": handle, "description": "Explains things", "skills": ["explain", "research"],
            "rates": rates or {"research": 50_00}, "free_tag_daily_cap": cap, "accept_platform_fee": True})
        hook = await self.ok("PUT", f"/agents/{agent['id']}/integration/webhook", owner,
                             json={"webhook_url": "https://agent.example.com/hook"})
        key = await self.ok("POST", f"/agents/{agent['id']}/integration/key", owner)
        status = await self.ok("POST", f"/agents/{agent['id']}/integration/test", owner)
        assert status["status"] == "live"
        return {**agent, "api_key": key["api_key"], "webhook_secret": hook["webhook_secret"],
                "headers": {"Authorization": f"Bearer {key['api_key']}"}}

    async def run(self, job: str):
        return await self.jobs.run_job(job)

    def signed(self, secret: str, payload: dict) -> dict:
        body = json.dumps(payload).encode()
        return {"content": body, "headers": {
            WebhookSigner.HEADER: WebhookSigner.sign(secret, int(self.clock.timestamp()), body),
            "Content-Type": "application/json"}}


# Set WAMIWA_TEST_ON_POSTGRES=1 to run the suite against the database in .env instead of in-memory SQLite.
# Tests then live in their own schema, so the real tables in "public" are never touched.
ON_POSTGRES = os.environ.get("WAMIWA_TEST_ON_POSTGRES") == "1"
TEST_SCHEMA = "wamiwa_test"
_schema_ready = False


async def prepare_postgres(infra) -> None:
    """Create the test schema and tables once, then empty every table before each test."""
    global _schema_ready
    async with infra.db.engine.begin() as conn:
        if not _schema_ready:
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{TEST_SCHEMA}"'))
            await conn.execute(text(f'SET search_path TO "{TEST_SCHEMA}"'))
            await conn.run_sync(Base.metadata.create_all)
            _schema_ready = True
        tables = ", ".join(f'"{TEST_SCHEMA}"."{t.name}"' for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


_sized_columns: dict[type, list[tuple[str, str, int]]] = {}


@event.listens_for(Session, "before_flush")
def enforce_column_lengths(session, _flush_context, _instances) -> None:
    """SQLite ignores VARCHAR lengths; Postgres does not. Fail here, in the fast local run, on any value
    that would be rejected in production."""
    for obj in list(session.new) + list(session.dirty):
        cls = type(obj)
        if cls not in _sized_columns:
            _sized_columns[cls] = [(attr.key, column.name, column.type.length)
                                   for attr in cls.__mapper__.column_attrs for column in attr.columns
                                   if getattr(column.type, "length", None)]
        for key, name, length in _sized_columns[cls]:
            value = obj.__dict__.get(key)
            if isinstance(value, str) and len(value) > length:
                raise AssertionError(f"{cls.__name__}.{name} is {len(value)} characters; the column holds {length}")


def make_settings(**overrides) -> Settings:
    if ON_POSTGRES:
        overrides = {"database_url": Settings().database_url, "database_schema": TEST_SCHEMA,
                     "database_pool_size": 2, "database_max_overflow": 2} | overrides
    defaults = dict(
        database_url="sqlite+aiosqlite:///:memory:", run_jobs=False, email_provider="sandbox", deliver_webhooks_inline=True,
        allow_private_webhook_urls=True, anonymous_requests_per_minute=10_000, human_reads_per_minute=10_000,
        human_writes_per_minute=10_000, machine_reads_per_minute=10_000, machine_writes_per_minute=10_000,
        logins_per_hour=1000, access_token_ttl_seconds=10 * 365 * 86400)
    # Never .env: real keys there (PayPal, Flutterwave, Resend) would make tests call real providers.
    return Settings(_env_file=None, **(defaults | overrides))


async def build_api(**overrides):
    settings = make_settings(**overrides)
    clock = FrozenClock(datetime(2026, 10, 12, 9, 0, tzinfo=UTC))  # a Monday
    endpoint = FakeAgentEndpoint()
    providers = build_providers(settings)
    providers.webhooks = endpoint
    infra = build_infrastructure(settings, clock=clock, providers=providers)
    if ON_POSTGRES:
        await prepare_postgres(infra)
    else:
        await infra.db.create_all()
    app = create_app(settings, infra)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return Api(client, infra, endpoint)


@pytest.fixture
async def api():
    harness = await build_api()
    yield harness
    await harness.client.aclose()
    await harness.infra.db.dispose()
