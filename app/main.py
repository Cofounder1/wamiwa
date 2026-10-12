"""Application factory."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.idempotency import IdempotencyMiddleware
from app.api.routers import ROUTERS
from app.container import build_infrastructure
from app.core.config import Settings, get_settings
from app.core.exceptions import AppError, RateLimited
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


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(_request: Request, exc: AppError) -> JSONResponse:
        headers = {}
        if isinstance(exc, RateLimited):
            headers = {"Retry-After": str(exc.retry_after), "X-RateLimit-Limit": str(exc.limit),
                       "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(exc.reset_at)}
        return JSONResponse(exc.to_body(), status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse({"success": False, "error": "Invalid request", "code": "validation_error",
                             "hint": "Check the listed fields", "fields": fields}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return JSONResponse({"success": False, "error": "Something went wrong on our side",
                             "code": "internal_error"}, status_code=500)


def create_app(settings: Settings | None = None, infra: Infrastructure | None = None) -> FastAPI:
    settings = settings or get_settings()
    if settings.is_production:
        defaults = Settings.model_fields
        unsafe = [name for name in ("jwt_secret", "hash_pepper", "payment_webhook_secret", "voice_webhook_secret",
                                    "video_webhook_secret") if getattr(settings, name) == defaults[name].default]
        if unsafe or not settings.redis_url:
            raise RuntimeError(f"Refusing to start in production with default secrets {unsafe} or without Redis")
    infra = infra or build_infrastructure(settings)
    runner = JobRunner(infra)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if not settings.is_production:
            await infra.db.create_all()  # production schema changes go through migrations
        try:  # the first-party Wamiwa Agent should be in the marketplace from the start
            async with infra.db.unit_of_work() as uow:
                await ServiceContext(infra, uow).services.promos.ensure_agent()
        except Exception:  # noqa: BLE001 - e.g. migrations not applied yet; it is created on first use anyway
            log.warning("could not create the Wamiwa Agent at startup")
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
    register_error_handlers(app)
    for router in ROUTERS:
        app.include_router(router)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn app.main:app_factory --factory`."""
    return create_app()
