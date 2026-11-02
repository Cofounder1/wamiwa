"""Africa's Talking, for SMS and the guardian confirmation call (ON-04, LK-09).

SMS: one request per message, from Wamiwa's registered sender ID when it has one.

Calls are a conversation, not a single request:
  1. `place_confirmation_call` asks Africa's Talking to ring the guardian from Wamiwa's voice number. It answers
     at once with a session id; the call itself happens afterwards.
  2. While the call is live, Africa's Talking posts to the callback URL set on that number in its dashboard
     (`/webhooks/africastalking/voice?token=...`), and Wamiwa answers each post with XML saying what to do next:
     speak, collect keypad digits, record. `VoiceXml` builds those answers.
  3. When the call ends it posts once more with `isActive=0`.

The username "sandbox" uses Africa's Talking's sandbox, which sends nothing to real phones.
"""
import asyncio
import logging
from xml.sax.saxutils import escape, quoteattr

import httpx

from app.infra.providers.base import SmsGateway, VoiceGateway

log = logging.getLogger("wamiwa.africastalking")

SMS_SENT = {100, 101, 102}  # processed, sent, queued


class AfricasTalkingError(RuntimeError):
    pass


def e164(phone: str) -> str:
    return phone if phone.startswith("+") else f"+{phone}"


class _Client:
    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, username: str, api_key: str, *, timeout: float, retries: int, backoff_seconds: float,
                 transport: httpx.AsyncBaseTransport | None):
        self.username = username
        self.domain = "sandbox.africastalking.com" if username == "sandbox" else "africastalking.com"
        self._retries, self._backoff = retries, backoff_seconds
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport, headers={
            "apiKey": api_key, "Accept": "application/json", "User-Agent": "wamiwa-backend/0.1"})

    async def post(self, url: str, data: dict) -> dict:
        """Retries network errors, 429 and 5xx; anything else is raised at once with Africa's Talking's reason."""
        problem = "no attempt made"
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.post(url, data={"username": self.username, **data})
            except httpx.HTTPError as exc:
                problem = f"network error: {exc}"
            else:
                if response.status_code < 300:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise AfricasTalkingError(f"unexpected answer: {response.text[:200]}") from exc
                problem = f"HTTP {response.status_code}: {response.text[:300]}"
                if response.status_code not in self.RETRY_STATUSES:
                    break  # e.g. a wrong key or username: retrying cannot help
            if attempt < self._retries:
                await asyncio.sleep(self._backoff * 2 ** attempt)
        raise AfricasTalkingError(problem)


class AfricasTalkingSmsGateway(SmsGateway):
    def __init__(self, username: str, api_key: str, sender_id: str | None = None, *, timeout: float = 10.0,
                 retries: int = 2, backoff_seconds: float = 0.5, transport: httpx.AsyncBaseTransport | None = None):
        self._api = _Client(username, api_key, timeout=timeout, retries=retries, backoff_seconds=backoff_seconds,
                            transport=transport)
        self._sender_id = sender_id
        self._url = f"https://api.{self._api.domain}/version1/messaging"

    async def send(self, to: str, body: str) -> None:
        data = {"to": e164(to), "message": body}
        if self._sender_id:
            data["from"] = self._sender_id
        answer = await self._api.post(self._url, data)
        recipients = (answer.get("SMSMessageData") or {}).get("Recipients") or []
        if not recipients or recipients[0].get("statusCode") not in SMS_SENT:
            status = recipients[0].get("status") if recipients else (answer.get("SMSMessageData") or {}).get("Message")
            log.error("sms NOT sent to=%s (%s)", to, status)
            raise AfricasTalkingError(f"SMS not sent: {status}")
        log.info("sms sent to=%s", to)


class AfricasTalkingVoiceGateway(VoiceGateway):
    def __init__(self, username: str, api_key: str, from_number: str, *, timeout: float = 10.0, retries: int = 2,
                 backoff_seconds: float = 0.5, transport: httpx.AsyncBaseTransport | None = None):
        self._api = _Client(username, api_key, timeout=timeout, retries=retries, backoff_seconds=backoff_seconds,
                            transport=transport)
        self._from = e164(from_number)
        self._url = f"https://voice.{self._api.domain}/call"

    async def place_confirmation_call(self, *, to: str, reference: str, script: str) -> str:
        """Returns the session id that every callback for this call carries. The script is spoken later, when the
        guardian answers (see the voice callback)."""
        answer = await self._api.post(self._url, {"from": self._from, "to": e164(to), "clientRequestId": reference})
        entry = (answer.get("entries") or [{}])[0]
        if not entry.get("sessionId") or str(entry.get("status", "")).lower() != "queued":
            raise AfricasTalkingError(f"call not placed: {entry.get('status') or answer.get('errorMessage')}")
        log.info("voice call placed to=%s reference=%s", to, reference)
        return entry["sessionId"]


class VoiceXml:
    """The answers Africa's Talking expects during a call. Text is escaped, so a brand name can't break the XML."""

    @staticmethod
    def say(text: str) -> str:
        return f"<Say>{escape(text)}</Say>"

    @staticmethod
    def get_digits(prompt: str, *, num_digits: int, timeout: int = 30, then: str = "") -> str:
        """Ask for keypad digits; they arrive in the next callback as `dtmfDigits`. `then` runs if none are entered."""
        return (f'<GetDigits numDigits="{num_digits}" timeout="{timeout}" finishOnKey="#">{VoiceXml.say(prompt)}'
                f"</GetDigits>{then}")

    @staticmethod
    def record(prompt: str, *, max_seconds: int = 20) -> str:
        """Record what the person says after a beep; the file's address arrives in the next callback as `recordingUrl`."""
        return (f'{VoiceXml.say(prompt)}<Record finishOnKey="#" maxLength={quoteattr(str(max_seconds))} '
                f'trimSilence="true" playBeep="true"/>')

    @staticmethod
    def response(*actions: str) -> str:
        return f'<?xml version="1.0" encoding="UTF-8"?><Response>{"".join(actions)}</Response>'
