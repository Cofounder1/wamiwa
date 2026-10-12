"""FastAPI dependencies: unit of work, authentication, general rate limits and response helpers."""
from typing import Annotated, Any, AsyncIterator

from fastapi import Depends, Request

from app.core.enums import AccountStatus
from app.core.exceptions import Forbidden, Unauthorized
from app.core.security import SecretFactory, WebhookSigner
from app.core.utils import Page
from app.infra.rate_limit import RateLimitPolicy
from app.services.base import Infrastructure, Principal, ServiceContext
from app.services.registry import ServiceRegistry


def ok(data: Any = None, **meta: Any) -> dict[str, Any]:
    return {"success": True, "data": data, **meta}


def ok_page(page: Page, **extra: Any) -> dict[str, Any]:
    return {"success": True, "data": list(page.items), **page.meta(), **extra}


def get_infra(request: Request) -> Infrastructure:
    return request.app.state.infra


async def get_ctx(infra: Annotated[Infrastructure, Depends(get_infra)]) -> AsyncIterator[ServiceContext]:
    """One transaction per request: commit when the endpoint returns, roll back on any error."""
    uow = infra.db.unit_of_work()
    try:
        yield ServiceContext(infra, uow)
        await uow.commit()
    except BaseException:
        await uow.rollback()
        raise
    finally:
        await uow.close()


# scope="function": commit before the response is sent, so a client never reads its own write too early.
Ctx = Annotated[ServiceContext, Depends(get_ctx, scope="function")]


def get_services(ctx: Ctx) -> ServiceRegistry:
    return ctx.services


Services = Annotated[ServiceRegistry, Depends(get_services)]


def client_ip(request: Request) -> str:
    # Deploy behind a proxy that overwrites X-Forwarded-For; uvicorn's --proxy-headers then sets client.host.
    return request.client.host if request.client else "unknown"


ClientIp = Annotated[str, Depends(client_ip)]


def _bearer(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthorized("Send your credentials as 'Authorization: Bearer <token>'", code="missing_credentials")
    return token.strip()


async def _budget(request: Request, ctx: ServiceContext, policy: RateLimitPolicy, identity: str) -> None:
    """Spend one request from the caller's general budget and expose it via X-RateLimit-* headers."""
    result = await ctx.infra.limiter.hit(policy, identity)
    request.state.rate_limit = result
    result.raise_if_blocked()


def _general_policy(request: Request, ctx: ServiceContext, machine: bool) -> RateLimitPolicy:
    policies, read = ctx.infra.policies, request.method in ("GET", "HEAD", "OPTIONS")
    if machine:
        return policies.machine_read if read else policies.machine_write
    return policies.human_read if read else policies.human_write


async def anonymous_budget(request: Request, ctx: Ctx, ip: ClientIp) -> None:
    await _budget(request, ctx, ctx.infra.policies.anonymous, f"ip:{ip}")


Anonymous = Depends(anonymous_budget)


async def any_principal(request: Request, ctx: Ctx) -> Principal:
    """A logged-in account in any state short of deleted (suspended accounts must be able to appeal)."""
    account = await ctx.services.auth.resolve_access_token(_bearer(request))
    principal = Principal(account=account)
    await _budget(request, ctx, _general_policy(request, ctx, machine=False), principal.rate_limit_identity)
    return principal


async def active_principal(principal: Annotated[Principal, Depends(any_principal)], ctx: Ctx) -> Principal:
    account = principal.account
    if account.status == AccountStatus.PENDING_GUARDIAN:
        raise Forbidden("Link a parent or guardian to finish signing up", code="guardian_required",
                        hint="POST /guardians/invite")
    if account.status != AccountStatus.ACTIVE:
        raise Forbidden(f"Your account is {account.status}", code=f"account_{account.status}",
                        reason=account.suspended_reason)
    if account.is_agent:
        raise Forbidden("Agents use the Agent API with their own key", code="use_agent_api")
    await ctx.services.accounts.touch(account)
    return principal


async def admin_principal(principal: Annotated[Principal, Depends(active_principal)]) -> Principal:
    if not principal.account.is_admin:
        raise Forbidden("Admins only", code="admin_required")
    return principal


async def agent_principal(request: Request, ctx: Ctx) -> Principal:
    """Agent API: authenticated by the agent's own key (AI-01)."""
    principal = await ctx.services.integrations.resolve_key(_bearer(request))
    await _budget(request, ctx, _general_policy(request, ctx, machine=True), principal.rate_limit_identity)
    return principal


async def company_key_principal(request: Request, ctx: Ctx) -> Principal:
    """Company API: authenticated by a company-scoped API key (API-01)."""
    token = _bearer(request)
    if not token.startswith(SecretFactory.COMPANY_KEY_PREFIX):
        raise Unauthorized("Invalid API key", code="invalid_api_key")
    principal = await ctx.services.api_keys.resolve_key(token)
    await _budget(request, ctx, _general_policy(request, ctx, machine=True), principal.rate_limit_identity)
    return principal


Any_ = Annotated[Principal, Depends(any_principal)]
Active = Annotated[Principal, Depends(active_principal)]
Admin = Annotated[Principal, Depends(admin_principal)]
Agent = Annotated[Principal, Depends(agent_principal)]
CompanyKey = Annotated[Principal, Depends(company_key_principal)]


async def signed_body(request: Request, secret: str) -> bytes:
    """Verify a provider webhook's HMAC signature and return the raw body."""
    body = await request.body()
    infra: Infrastructure = request.app.state.infra
    if not WebhookSigner.verify(secret, request.headers.get(WebhookSigner.HEADER), body, infra.clock.timestamp()):
        raise Unauthorized("Invalid webhook signature", code="invalid_signature")
    return body
