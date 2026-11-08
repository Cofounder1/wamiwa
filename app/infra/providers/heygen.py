"""HeyGen for AI video (AV-01, LK-04), through its v3 API: one photo of the person's face plus the script.

How a video is made:
  1. `submit` sends the face from the person's ID check (the selfie it took) and the script, read by the voice they
     chose (female or male), to `POST /v3/videos` as an image-to-video job. HeyGen gives back a video id.
  2. The `videos.poll_processing` job calls `fetch` every minute. When HeyGen says the video is done, it's
     downloaded straight away (HeyGen's link expires) into Wamiwa's own storage, and the stored file is used
     from then on.

Failures HeyGen reports go back as a failed job, so Wamiwa retries once and then refunds. A face HeyGen can't use is
refused at once, so the request fails before anyone is charged for it.
"""
import asyncio
import base64
import hashlib
import logging
from collections.abc import AsyncIterator

import httpx

from app.core.exceptions import ServiceUnavailable, Unprocessable
from app.infra.providers.base import VideoGateway, VideoJob, VideoRequest
from app.infra.storage import FileStorage

log = logging.getLogger("wamiwa.heygen")

BASE_URL = "https://api.heygen.com"
MAX_SCRIPT = 5000  # HeyGen reads scripts of up to 5,000 characters
FACE_MAX_BYTES = 20 * 1024 * 1024
IMAGE_TYPES = {b"\xff\xd8": "image/jpeg", b"\x89P": "image/png"}
# Better quality for what is paid for at a higher price or delivered to a brand.
RESOLUTION = {"premium": "1080p", "final": "1080p"}
WORKING = {"waiting", "pending", "processing"}


class HeyGenError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class HeyGenVideoGateway(VideoGateway):
    def __init__(self, api_key: str, *, storage: FileStorage, voices: dict[str, str], max_video_bytes: int,
                 timeout: float = 60.0, retries: int = 2, backoff_seconds: float = 0.5,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._storage, self._voices, self._max_bytes = storage, voices, max_video_bytes
        self._retries, self._backoff = retries, backoff_seconds
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout, transport=transport,
                                         headers={"X-Api-Key": api_key, "User-Agent": "wamiwa-backend/0.1"})
        # Downloads go to HeyGen's file host, with no API key.
        self._files = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=True)

    async def _call(self, method: str, path: str, *, json: dict | None = None) -> dict:
        """`data` from HeyGen's answer. Retries network errors, 429 and 5xx; other refusals raise with the reason."""
        problem, status = "no attempt made", None
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.request(method, path, json=json)
            except httpx.TransportError as exc:
                problem, status = f"network error: {exc}", None
            else:
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                if response.status_code < 300:
                    return body.get("data") or {}
                status = response.status_code
                error = body.get("error") or {}
                detail = error.get("message") if isinstance(error, dict) else error
                problem = f"HTTP {status}: {detail or body.get('message') or response.text[:200]}"
                if status != 429 and status < 500:
                    break
            if attempt < self._retries:
                await asyncio.sleep(self._backoff * 2 ** attempt)
        log.error("HeyGen %s %s failed: %s", method, path, problem)
        raise HeyGenError(problem, status)

    # --- the face ----------------------------------------------------------------------------------------
    async def _image(self, face_ref: str) -> dict:
        """The face from the ID check, as HeyGen takes it: a link, or the photo itself."""
        ref = face_ref.removeprefix("face:")
        if ref.startswith(("http://", "https://")):
            return {"type": "url", "url": ref}
        if ref.startswith("data:image/") and ";base64," in ref:
            head, data = ref.split(";base64,", 1)
            return {"type": "base64", "media_type": head.removeprefix("data:"), "data": data}
        data = None
        if ref and "/" in ref:
            try:
                data = await self._storage.read(ref, FACE_MAX_BYTES)
            except (OSError, ValueError):
                data = None
        kind = IMAGE_TYPES.get((data or b"")[:2])
        if not data or kind is None:
            raise Unprocessable("We don't have a clear photo of your face from your ID check. Redo the ID check, "
                                "then try again.", code="face_photo_missing")
        return {"type": "base64", "media_type": kind, "data": base64.b64encode(data).decode()}

    # --- making the video ----------------------------------------------------------------------------------
    async def submit(self, request: VideoRequest) -> str:
        if len(request.script) > MAX_SCRIPT:
            raise Unprocessable(f"Scripts can be up to {MAX_SCRIPT:,} characters", code="script_too_long")
        body = {
            "type": "image", "image": await self._image(request.face_ref), "script": request.script,
            "voice_id": self._voices.get(request.voice) or self._voices["female"],
            "resolution": RESOLUTION.get(request.quality, "720p"), "aspect_ratio": "auto",
            "expressiveness": "medium" if request.expressive else "low",
            "title": f"Wamiwa {request.reference}",
        }
        try:
            data = await self._call("POST", "/v3/videos", json=body)
        except HeyGenError as exc:
            if exc.status == 400 and "face" in str(exc).lower():
                raise Unprocessable("The video service couldn't find a clear face in the photo from your ID check. "
                                    "Redo the ID check in good light, then try again.", code="face_not_usable") from exc
            raise ServiceUnavailable("AI video isn't available right now. You haven't been charged. Try again shortly.",
                                     code="video_unavailable") from exc
        video_id = data.get("video_id")
        if not video_id:
            raise ServiceUnavailable("AI video isn't available right now. You haven't been charged. Try again shortly.",
                                     code="video_unavailable")
        return video_id

    async def fetch(self, job_id: str) -> VideoJob:
        try:
            data = await self._call("GET", f"/v3/videos/{job_id}")
        except HeyGenError as exc:
            if exc.status == 404:
                return VideoJob("failed", failure_reason="The video service lost this video")
            return VideoJob("processing")  # asked again on the next poll
        status = data.get("status")
        if status in WORKING:
            return VideoJob("processing")
        if status == "failed":
            reason = data.get("failure_message") or data.get("failure_code") or "The video service couldn't make it"
            log.warning("HeyGen video %s failed: %s", job_id, reason)
            return VideoJob("failed", failure_reason=f"Generation failed: {reason}")
        if status != "completed" or not data.get("video_url"):
            return VideoJob("processing")
        # Served like any uploaded video (/media/video/...); named from HeyGen's id so a retry overwrites, not repeats.
        key = f"video/{hashlib.sha256(job_id.encode()).hexdigest()[:32]}.mp4"
        try:
            await self._keep(key, data["video_url"])
        except ValueError:
            return VideoJob("failed", failure_reason="The finished video was too large to store")
        except (httpx.HTTPError, OSError) as exc:
            log.warning("HeyGen video %s not downloaded yet: %s", job_id, exc)
            return VideoJob("processing")  # the link is fetched again next time
        return VideoJob("ready", file_ref=key)

    async def _keep(self, key: str, url: str) -> None:
        """Copy the finished video into Wamiwa's storage."""
        async with self._files.stream("GET", url) as response:
            response.raise_for_status()

            async def chunks() -> AsyncIterator[bytes]:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    yield chunk

            await self._storage.save(key, chunks(), self._max_bytes)
        await self._storage.finalize(key, "video/mp4")
