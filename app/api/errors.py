"""How every failure reaches the client: one JSON shape, a request ID, and CORS headers even on crashes."""
import asyncio
import contextvars
import json
import logging
import re
import secrets

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppError, RateLimited

log = logging.getLogger("wamiwa")
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
REQUEST_ID_HEADER = "X-Request-ID"
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

# Every log line carries the request it belongs to, so a user's error can be found in the logs.
_previous_factory = logging.getLogRecordFactory()


def _record_factory(*args, **kwargs):
    record = _previous_factory(*args, **kwargs)
    record.request_id = request_id_var.get()
    return record


logging.setLogRecordFactory(_record_factory)


def error_body(message: str, code: str, **extra) -> dict:
    return {"success": False, "error": message, "code": code, **extra, "request_id": request_id_var.get()}


class RequestContextMiddleware:
    """Gives each request an ID (the caller's X-Request-ID if it sent a sensible one), returns it in the
    X-Request-ID header, and turns any crash into the standard 500 body. It sits inside the CORS layer,
    so even a crash reaches the browser as a readable error rather than a "network error"."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        incoming = dict(scope["headers"]).get(REQUEST_ID_HEADER.lower().encode(), b"").decode("latin-1")
        request_id = incoming if _VALID_ID.match(incoming) else secrets.token_hex(8)
        token = request_id_var.set(request_id)
        started = False

        async def send_with_id(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message = {**message, "headers": [*message.get("headers", []),
                                                  (REQUEST_ID_HEADER.lower().encode(), request_id.encode())]}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            log.exception("unhandled error request_id=%s path=%s", request_id, scope.get("path"))
            if started:
                raise  # too late to replace a response that has begun; the connection is closed instead
            body = json.dumps(error_body("Something went wrong on our side", "internal_error",
                                         hint="Try again. If it keeps happening, contact support with the request ID.")).encode()
            await send({"type": "http.response.start", "status": 500, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                (REQUEST_ID_HEADER.lower().encode(), request_id.encode())]})
            await send({"type": "http.response.body", "body": body})
        finally:
            request_id_var.reset(token)


def _unavailable(what: str) -> JSONResponse:
    return JSONResponse(error_body(f"{what} is temporarily unavailable. Please try again shortly.",
                                   "temporarily_unavailable"), status_code=503, headers={"Retry-After": "5"})


def _is_outage(exc: BaseException) -> bool:
    """Database or cache trouble that will likely pass: lost connection, pool exhausted, query timed out."""
    if isinstance(exc, (OperationalError, InterfaceError, PoolTimeoutError, ConnectionError, TimeoutError,
                        asyncio.TimeoutError)):
        return True
    try:
        from redis.exceptions import RedisError

        if isinstance(exc, RedisError):
            return True
    except ImportError:  # pragma: no cover
        pass
    if isinstance(exc, DBAPIError):
        name = type(getattr(exc, "orig", None)).__name__
        return any(word in name for word in ("QueryCanceled", "Timeout", "Connection", "CannotConnect", "TooManyConnections"))
    return False


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(_request: Request, exc: AppError) -> JSONResponse:
        headers = {}
        if isinstance(exc, RateLimited):
            headers = {"Retry-After": str(exc.retry_after), "X-RateLimit-Limit": str(exc.limit),
                       "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(exc.reset_at)}
        elif exc.status_code == 503:
            headers = {"Retry-After": "5"}
        return JSONResponse({**exc.to_body(), "request_id": request_id_var.get()}, status_code=exc.status_code,
                            headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse(error_body("Invalid request", "validation_error", hint="Check the listed fields",
                                       fields=fields), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: ("No such endpoint", "not_found"), 405: ("That method is not allowed here", "method_not_allowed")}
        message, code = codes.get(exc.status_code, (str(exc.detail), f"http_{exc.status_code}"))
        return JSONResponse(error_body(message, code), status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(IntegrityError)
    async def already_exists(_request: Request, exc: IntegrityError) -> JSONResponse:
        """Two requests raced for something unique; the database stopped the second. Say so clearly."""
        detail = str(getattr(exc, "orig", exc)).lower()
        if "handle" in detail:
            return JSONResponse(error_body("That handle is taken", "handle_taken"), status_code=409)
        if "email" in detail:
            return JSONResponse(error_body("This email is already registered. Log in instead.", "email_registered"),
                                status_code=409)
        log.warning("integrity conflict request_id=%s: %s", request_id_var.get(), detail[:300])
        return JSONResponse(error_body("This conflicts with something that already exists. Refresh and try again.",
                                       "already_exists"), status_code=409)

    for exc_type in (OperationalError, InterfaceError, PoolTimeoutError, DBAPIError, ConnectionError, TimeoutError):
        @app.exception_handler(exc_type)
        async def outage(_request: Request, exc: Exception) -> JSONResponse:
            if not _is_outage(exc):
                raise exc  # a real bug, not an outage: let it become a 500 with a request ID
            log.warning("dependency unavailable request_id=%s: %s", request_id_var.get(), type(exc).__name__)
            return _unavailable("The service")

    try:
        from redis.exceptions import RedisError

        @app.exception_handler(RedisError)
        async def redis_outage(_request: Request, exc: Exception) -> JSONResponse:
            log.warning("redis unavailable request_id=%s: %s", request_id_var.get(), exc)
            return _unavailable("Sign-in")
    except ImportError:  # pragma: no cover
        pass
