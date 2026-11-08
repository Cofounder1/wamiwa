"""AV-01 AI video through HeyGen's v3 API, against a fake that behaves like it."""
import base64
import json

import httpx
import pytest

from app.core.exceptions import ServiceUnavailable, Unprocessable
from app.infra.providers.base import VideoRequest
from app.infra.providers.heygen import HeyGenVideoGateway
from app.infra.storage import LocalFileStorage

JPEG = b"\xff\xd8\xff\xe0" + b"face" * 100
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"v" * 5000


class FakeHeyGen:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.status: dict[str, dict] = {}
        self.downloads = 0
        self.refuse_face = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "files.heygen.ai":
            self.downloads += 1
            return httpx.Response(200, content=MP4)
        assert request.headers["X-Api-Key"] == "key"
        if request.url.path == "/v3/videos" and request.method == "POST":
            body = json.loads(request.content)
            if self.refuse_face:
                return httpx.Response(400, json={"error": {"code": "invalid_parameter",
                                                           "message": "No face detected in image"}})
            video_id = f"vid{len(self.created) + 1}"
            self.created.append(body)
            self.status[video_id] = {"video_id": video_id, "status": "processing"}
            return httpx.Response(200, json={"data": {"video_id": video_id, "status": "waiting"}})
        if request.url.path.startswith("/v3/videos/"):
            found = self.status.get(request.url.path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"data": found}) if found else httpx.Response(404, json={})
        return httpx.Response(404)


def gateway(fake: FakeHeyGen, tmp_path, max_bytes: int = 10_000_000) -> tuple[HeyGenVideoGateway, LocalFileStorage]:
    storage = LocalFileStorage(str(tmp_path))
    heygen = HeyGenVideoGateway("key", storage=storage, voices={"female": "voice-f", "male": "voice-m"},
                                max_video_bytes=max_bytes, transport=httpx.MockTransport(fake.handler),
                                backoff_seconds=0)
    return heygen, storage


def request(face_ref: str, **extra) -> VideoRequest:
    return VideoRequest(reference="v1", script="Hello from Lagos", face_ref=face_ref, voice_ref=None,
                        quality=extra.pop("quality", "standard"), seconds=10, **extra)


async def stored_face(storage: LocalFileStorage) -> str:
    async def chunks():
        yield JPEG
    await storage.save("liveness/abababababababababababababababab.jpg", chunks(), 1_000_000)
    return "face:liveness/abababababababababababababababab.jpg"


async def test_a_video_from_the_id_check_face_is_made_then_kept_in_wamiwa_storage(tmp_path):
    fake = FakeHeyGen()
    heygen, storage = gateway(fake, tmp_path)
    job = await heygen.submit(request(await stored_face(storage), voice="male", quality="premium"))
    body = fake.created[0]
    assert body["type"] == "image" and body["voice_id"] == "voice-m" and body["resolution"] == "1080p"
    assert body["image"] == {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(JPEG).decode()}
    assert (await heygen.fetch(job)).status == "processing"

    fake.status[job] |= {"status": "completed", "video_url": "https://files.heygen.ai/v/vid1.mp4?sig=x"}
    ready = await heygen.fetch(job)
    assert ready.status == "ready" and ready.file_ref.startswith("video/") and ready.file_ref.endswith(".mp4")
    assert await storage.read(ready.file_ref, 1_000_000) == MP4  # HeyGen's link expires; the copy doesn't

    # A face held as a link (YouVerify) is passed as a link.
    await heygen.submit(request("face:https://cdn.youverify.co/face.jpg"))
    assert fake.created[1]["image"] == {"type": "url", "url": "https://cdn.youverify.co/face.jpg"}
    assert fake.created[1]["voice_id"] == "voice-f" and fake.created[1]["resolution"] == "720p"


async def test_failures_are_reported_and_no_face_refuses_before_any_video(tmp_path):
    fake = FakeHeyGen()
    heygen, storage = gateway(fake, tmp_path)
    # No usable photo from the ID check (e.g. the stand-in's placeholder): refused, nothing sent.
    with pytest.raises(Unprocessable) as missing:
        await heygen.submit(request("face:stand-in"))
    assert missing.value.code == "face_photo_missing" and fake.created == []
    # HeyGen finds no face in the photo.
    fake.refuse_face = True
    with pytest.raises(Unprocessable) as refused:
        await heygen.submit(request(await stored_face(storage)))
    assert refused.value.code == "face_not_usable"
    fake.refuse_face = False
    with pytest.raises(Unprocessable):
        await heygen.submit(VideoRequest(reference="v", script="x" * 5001, face_ref="face:https://a/b.jpg",
                                         voice_ref=None, quality="standard", seconds=600))

    job = await heygen.submit(request(await stored_face(storage)))
    fake.status[job] |= {"status": "failed", "failure_code": "rendering_failed", "failure_message": "Render error"}
    failed = await heygen.fetch(job)
    assert failed.status == "failed" and "Render error" in failed.failure_reason
    assert (await heygen.fetch("unknown")).status == "failed"


async def test_a_video_too_large_to_store_fails_so_it_is_refunded(tmp_path):
    fake = FakeHeyGen()
    heygen, storage = gateway(fake, tmp_path, max_bytes=100)
    job = await heygen.submit(request(await stored_face(storage)))
    fake.status[job] |= {"status": "completed", "video_url": "https://files.heygen.ai/v/big.mp4"}
    assert (await heygen.fetch(job)).status == "failed"


async def test_heygen_down_means_nobody_is_charged(api):
    """A refused submit fails the request, so the payment taken for it is reversed."""
    async def down(_request):
        raise ServiceUnavailable("AI video isn't available right now.", code="video_unavailable")

    api.infra.providers.video.submit = down
    user = await api.influencer("maker", "Maker Person")
    payments = api.infra.providers.payments.main
    r = await api.call("POST", "/ai-videos", user, json={"script": "Hello there", "seconds": 60, "tier": "premium",
                                                          "payment_method": "tok_visa", "voice": "male"}, expect=503)
    assert r.json()["code"] == "video_unavailable"
    charged = list(payments.charges.items())[-1]
    assert payments.refunds[-1]["provider_ref"] == charged[0]
    assert await api.ok("GET", "/ai-videos", user) == []


def test_heygen_is_used_once_its_key_is_set():
    from app.container import build_video
    from app.core.config import Settings

    kind = lambda **v: type(build_video(Settings(_env_file=None, **v))).__name__  # noqa: E731
    assert kind() == "SandboxVideoGateway"
    assert kind(env="production") == "VideoNotConnected"
    assert kind(heygen_api_key="k") == "HeyGenVideoGateway"
