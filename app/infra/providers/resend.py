"""Email delivery through Resend (https://resend.com)."""
import asyncio
import logging

import httpx

from app.infra.providers.base import EmailGateway

log = logging.getLogger("wamiwa.email")


class EmailDeliveryError(RuntimeError):
    pass


class ResendEmailGateway(EmailGateway):
    """Sends plain-text email. Temporary failures (rate limit, server error, network) are retried a
    couple of times; a refusal such as an unverified sending domain is raised at once with the reason."""

    URL = "https://api.resend.com/emails"
    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, api_key: str, from_address: str, from_name: str = "Wamiwa", *, reply_to: str | None = None,
                 timeout: float = 10.0,
                 retries: int = 2, backoff_seconds: float = 0.5, transport: httpx.AsyncBaseTransport | None = None):
        self._sender = f"{from_name} <{from_address}>" if from_name else from_address
        self._reply_to = reply_to
        self._retries, self._backoff = retries, backoff_seconds
        self._client = httpx.AsyncClient(
            timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "wamiwa-backend/0.1"})

    async def send(self, to: str, subject: str, body: str) -> None:
        payload = {"from": self._sender, "to": [to], "subject": subject, "text": body}
        if self._reply_to:
            payload["reply_to"] = self._reply_to
        problem = "no attempt made"
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.post(self.URL, json=payload)
            except httpx.HTTPError as exc:
                problem = f"network error: {exc}"
            else:
                if response.status_code < 300:
                    log.info("email sent to=%s subject=%s", to, subject)
                    return
                problem = f"HTTP {response.status_code}: {self._reason(response)}"
                if response.status_code not in self.RETRY_STATUSES:
                    break  # e.g. the sending domain is not verified, or the key is wrong: retrying cannot help
            if attempt < self._retries:
                await asyncio.sleep(self._backoff * (2 ** attempt))
        log.error("email NOT sent to=%s subject=%s (%s)", to, subject, problem)
        raise EmailDeliveryError(problem)

    @staticmethod
    def _reason(response: httpx.Response) -> str:
        try:
            return str(response.json().get("message") or response.text)[:300]
        except ValueError:
            return response.text[:300]

    async def close(self) -> None:
        await self._client.aclose()
