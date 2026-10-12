"""Safe retries for write requests.

A client that sends `Idempotency-Key: <unique value>` with a POST, PUT, PATCH or DELETE can repeat
that request after a timeout or dropped connection without the action happening twice: the first
successful response is stored for 24 hours and replayed for any repeat. Works on every API surface
(people, agents, companies). Assistants and agents retry a lot, so they should always send it.
"""
import hashlib
import json

from app.infra.kv import KeyValueStore

HEADER = b"idempotency-key"
REPLAY_HEADER = b"idempotent-replay"
TTL_SECONDS = 24 * 3600
LOCK_SECONDS = 60
MAX_KEY_LENGTH = 255
MAX_STORED_BYTES = 256 * 1024
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class IdempotencyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in WRITE_METHODS:
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        key = headers.get(HEADER, b"").decode("latin-1").strip()
        if not key:
            return await self.app(scope, receive, send)
        if len(key) > MAX_KEY_LENGTH:
            return await self._json(send, 422, {"success": False, "error": "Idempotency-Key is too long",
                                                "code": "invalid_idempotency_key"})

        kv: KeyValueStore = scope["app"].state.infra.kv
        body = await self._read_body(receive)
        # Keys are scoped to the caller's credential and the exact endpoint, so they never collide across accounts.
        caller = hashlib.sha256(headers.get(b"authorization", b"anonymous")).hexdigest()[:32]
        slot = hashlib.sha256(f"{caller}:{scope['method']}:{scope['path']}:{key}".encode()).hexdigest()
        fingerprint = hashlib.sha256(body).hexdigest()
        stored_key, lock_key = f"idem:{slot}", f"idem:{slot}:lock"

        stored = await kv.get(stored_key)
        if stored is not None:
            saved = json.loads(stored)
            if saved["fingerprint"] != fingerprint:
                return await self._json(send, 422, {
                    "success": False, "error": "This Idempotency-Key was already used with a different request",
                    "code": "idempotency_key_reused", "hint": "Use a new key for a new request"})
            return await self._replay(send, saved)
        if not await kv.set_if_absent(lock_key, "1", LOCK_SECONDS):
            return await self._json(send, 409, {
                "success": False, "error": "The original request is still being processed",
                "code": "request_in_progress", "hint": "Retry in a moment with the same key"})

        captured = {"status": 500, "content_type": "application/json", "chunks": []}

        async def capture(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                for name, value in message.get("headers", []):
                    if name.lower() == b"content-type":
                        captured["content_type"] = value.decode("latin-1")
            elif message["type"] == "http.response.body":
                captured["chunks"].append(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, self._replaying_receive(body, receive), capture)
            payload = b"".join(captured["chunks"])
            # Only successes are remembered: a failed request changed nothing and may simply be retried.
            if 200 <= captured["status"] < 300 and len(payload) <= MAX_STORED_BYTES:
                await kv.set(stored_key, json.dumps({
                    "fingerprint": fingerprint, "status": captured["status"],
                    "content_type": captured["content_type"], "body": payload.decode("utf-8", "replace"),
                }), TTL_SECONDS)
        finally:
            await kv.delete(lock_key)

    @staticmethod
    async def _read_body(receive) -> bytes:
        chunks = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        return b"".join(chunks)

    @staticmethod
    def _replaying_receive(body: bytes, receive):
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return replay

    @staticmethod
    async def _replay(send, saved: dict) -> None:
        body = saved["body"].encode()
        await send({"type": "http.response.start", "status": saved["status"], "headers": [
            (b"content-type", saved["content_type"].encode("latin-1")),
            (b"content-length", str(len(body)).encode()), (REPLAY_HEADER, b"true")]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _json(send, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
