"""Application error hierarchy. Every error renders as {"success": false, "error", "code", "hint"}."""
from typing import Any


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, hint: str | None = None, code: str | None = None, **extra: Any):
        super().__init__(message)
        self.message = message
        self.hint = hint
        if code:
            self.code = code
        self.extra = extra

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"success": False, "error": self.message, "code": self.code}
        if self.hint:
            body["hint"] = self.hint
        body.update(self.extra)
        return body


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class PaymentRequired(AppError):
    status_code = 402
    code = "payment_failed"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class Gone(AppError):
    status_code = 410
    code = "gone"


class Unprocessable(AppError):
    status_code = 422
    code = "unprocessable"


class Locked(AppError):
    status_code = 423
    code = "locked"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str = "Rate limit exceeded", *, retry_after: int, limit: int, reset_at: int, **extra: Any):
        super().__init__(message, retry_after_seconds=retry_after, remaining=0, **extra)
        self.retry_after = retry_after
        self.limit = limit
        self.reset_at = reset_at
