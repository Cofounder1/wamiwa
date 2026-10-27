"""Application factory."""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.errors import REQUEST_ID_HEADER, RequestContextMiddleware, register_error_handlers
from app.api.idempotency import IdempotencyMiddleware
from app.api.routers import ROUTERS
from app.infra.launch import LaunchSwitch
from app.container import build_infrastructure
from app.core.config import Settings, get_settings
from app.jobs.runner import JobRunner
from app.services.base import Infrastructure, ServiceContext

log = logging.getLogger("wamiwa")


class RateLimitHeadersMiddleware:
    """Adds X-RateLimit-* to every response from the budget the request spent (pure ASGI, no buffering)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})

        async def send_with_headers(message):
            result = state.get("rate_limit")
            if message["type"] == "http.response.start" and result is not None:
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers += [(k.lower().encode(), v.encode()) for k, v in result.headers().items()
                            if k.lower().encode() not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(settings: Settings | None = None, infra: Infrastructure | None = None) -> FastAPI:
    settings = settings or get_settings()
    if settings.is_production:
        defaults = Settings.model_fields
        unsafe = [name for name in ("jwt_secret", "hash_pepper", "payment_webhook_secret", "voice_webhook_secret",
                                    "video_webhook_secret") if getattr(settings, name) == defaults[name].default]
        if unsafe or not settings.redis_url:
            raise RuntimeError(f"Refusing to start in production with default secrets {unsafe} or without Redis")
        if settings.dev_fixed_otp:
            raise RuntimeError("WAMIWA_DEV_FIXED_OTP must not be set in production")
        if "*" in settings.cors_origins:
            raise RuntimeError("WAMIWA_CORS_ORIGINS must list the allowed sites in production, not *")
    infra = infra or build_infrastructure(settings)
    log.warning("CORS: browsers may call this API from %s", ", ".join(settings.cors_origins) or "nowhere")
    runner = JobRunner(infra)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if not settings.is_production:
            await infra.db.create_all()  # production schema changes go through migrations
        async def create_agent() -> None:
            async with infra.db.unit_of_work() as uow:
                await ServiceContext(infra, uow).services.promos.ensure_agent()

        try:  # the first-party Wamiwa Agent should be in the marketplace from the start
            # Bounded, because the port only opens once start-up finishes; a slow database must not keep it shut.
            await asyncio.wait_for(create_agent(), timeout=20)
        except Exception:  # noqa: BLE001 - e.g. migrations not applied yet; it is created on first use anyway
            log.warning("could not create the Wamiwa Agent at startup", exc_info=True)
        if settings.run_jobs:
            runner.start()
        yield
        await runner.stop()
        await infra.kv.close()
        await infra.db.dispose()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan,
                  docs_url=None if settings.is_production else "/docs", redoc_url=None)
    app.state.infra = infra
    app.state.jobs = runner
    app.add_middleware(RateLimitHeadersMiddleware)
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(RequestContextMiddleware)  # request IDs, and crashes as readable 500s inside CORS
    # Added last so it is outermost: error and rate-limit responses carry CORS headers too, and the
    # browser can read the rate-limit and replay headers.
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", REQUEST_ID_HEADER],
        expose_headers=["X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After",
                        "Idempotent-Replay", "Content-Disposition", REQUEST_ID_HEADER],
        max_age=600)
    register_error_handlers(app)
    for router in ROUTERS:
        app.include_router(router)

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        """The process is up (for restarts). Says nothing about the database or Redis."""
        return {"status": "ok"}

    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        """Ready to serve: the database answers and the key-value store works. 503 when either does not,
        so a load balancer or monitor sees the real state."""
        checks = {"database": "ok", "cache": "ok"}
        try:
            async with infra.db.engine.connect() as conn:
                await asyncio.wait_for(conn.execute(text("SELECT 1")), timeout=3)
        except Exception:  # noqa: BLE001
            checks["database"] = "unavailable"
        ping = getattr(infra.kv, "ping", None)
        if ping is not None and not await ping():
            checks["cache"] = "unavailable"
        healthy = all(value == "ok" for value in checks.values())
        # For information only: which outside services are real, stand-ins (development) or not connected yet.
        def mode(provider) -> str:
            name = type(provider).__name__
            return "not_connected" if name.endswith("NotConnected") else "stand_in" if name.startswith("Sandbox") else "connected"
        providers = infra.providers

        async def current(provider):  # on the live site, the test or live provider behind the launch switch
            return await provider.current() if isinstance(provider, LaunchSwitch) else provider

        payments, paypal = await current(providers.payments), await current(providers.paypal)
        services = {"payments": mode(getattr(payments, "main", payments)), "paypal": mode(paypal),
                    "payments_mode": "live" if infra.settings.is_production and await infra.launch.is_live() else "test",
                    "identity": mode(providers.identity), "email": mode(providers.email), "sms": mode(providers.sms),
                    "voice": mode(providers.voice)}
        return JSONResponse({"status": "ok" if healthy else "degraded", **checks, "services": services},
                            status_code=200 if healthy else 503)

    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn app.main:app_factory --factory`."""
    return create_app()
