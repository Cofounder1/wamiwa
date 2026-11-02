"""ON-05 with YouVerify: its live check, NIN and driver's licence lookups, and passports checked by staff, against a
fake YouVerify that answers like its v2 API (shapes taken from its sandbox)."""
import json

import httpx

from app.infra.providers.youverify import YouVerifyIdentityGateway
from app.infra.storage import LocalFileStorage

FACE = "https://cdn.youverify.co/live-face.jpg"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


class FakeYouVerify:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}  # session id -> liveness result, once the person has done it
        self.people = {"12345678901": {"firstName": "ADA", "middleName": "", "lastName": "OBI",
                                       "dateOfBirth": "1990-05-01"}}
        self.face_matches = True
        self.lookups: list[dict] = []
        self.compares: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["token"] == "yv-secret"
        body = json.loads(request.content) if request.content else {}
        path = request.url.path

        def ok(data):
            return httpx.Response(200, json={"success": True, "statusCode": 200, "message": "success", "data": data})

        if path == "/v2/api/identity/sdk/session/generate":
            session_id = f"sess{len(self.sessions) + 1}"
            self.sessions[session_id] = {}
            return ok({"sessionId": session_id, "expiresAt": "2026-10-24T10:00:00Z", "status": "active"})
        if path == "/v2/api/identity/sdk/liveness/token":
            return ok({"authToken": f"tok-{body['deviceCorrelationId']}", "sessionId": "x"})
        if path == "/v2/api/identity/liveness":
            sid = request.url.params["sessionId"]
            done = self.sessions.get(sid) or {}
            docs = [{"method": "liveness", "sessionId": sid, **done}] if done else []
            return ok({"docs": docs, "pagination": {"totalDocs": len(docs)}})
        if path in ("/v2/api/identity/ng/nin", "/v2/api/identity/ng/drivers-license"):
            self.lookups.append(body)
            person = self.people.get(body["id"])
            if person is None:
                return ok({"status": "not_found", "reason": "No record"})
            match = self.face_matches and body["validations"]["selfie"]["image"] == FACE
            return ok({"status": "found", **person, "image": "data:image/jpg;base64,...", "validations": {
                "selfie": {"selfieVerification": {"confidenceLevel": 91 if match else 40, "threshold": 70,
                                                  "match": match}}}})
        if path == "/v2/api/identity/compare-image":
            self.compares.append(body)
            return ok({"status": "completed", "imageComparison": {
                "confidenceLevel": 88 if self.face_matches else 30, "threshold": 80, "match": self.face_matches}})
        return httpx.Response(404, json={"message": "404 page not found"})

    def pass_live_check(self, session_id: str, passed: bool = True) -> None:
        self.sessions[session_id] = {"passed": passed, "faceImage": FACE if passed else None}


def connect(api) -> FakeYouVerify:
    fake = FakeYouVerify()
    api.infra.providers.identity = YouVerifyIdentityGateway(
        "yv-secret", "merchant-1", "sandbox", storage=LocalFileStorage(api.infra.settings.upload_dir),
        transport=httpx.MockTransport(fake.handler), backoff_seconds=0)
    return fake


async def test_nin_lookup_with_youverify_live_check(api):
    fake = connect(api)
    ada = await api.signup("ada", name="Ada")
    start = await api.ok("POST", "/verification/identity/live-check", ada)
    assert start == {"provider": "youverify", "session_id": "sess1", "session_token": "tok-sess1",
                     "public_merchant_key": "merchant-1", "sandbox": True}
    nin = {"document_type": "national_id", "id_number": "1234 5678 901", "liveness_session_id": "sess1"}

    # The browser can't claim a pass: YouVerify must have recorded one for the session.
    r = await api.ok("POST", "/verification/identity", ada, json=nin)
    assert r["status"] == "failed" and "live check didn't pass" in r["reason"]
    # Each live check counts once, and only for the account that started it.
    r = await api.call("POST", "/verification/identity", ada, json=nin, expect=403)
    assert r.json()["code"] == "live_check_expired"
    other = await api.signup("bola", name="Bola")
    mine = (await api.ok("POST", "/verification/identity/live-check", ada))["session_id"]
    r = await api.call("POST", "/verification/identity", other, json=nin | {"liveness_session_id": mine}, expect=403)
    assert r.json()["code"] == "live_check_expired"

    # An unknown number is refused.
    fake.pass_live_check(mine)
    r = await api.ok("POST", "/verification/identity", ada, json=nin | {"id_number": "99999999999",
                                                                       "liveness_session_id": mine})
    assert r["status"] == "failed" and "couldn't find that NIN" in r["reason"]

    # Third time: verified with the government record's name and date of birth, and the live face.
    last = (await api.ok("POST", "/verification/identity/live-check", ada))["session_id"]
    fake.pass_live_check(last)
    r = await api.ok("POST", "/verification/identity", ada, json=nin | {"liveness_session_id": last})
    assert r["status"] == "passed" and r["id_verified"]
    assert fake.lookups[-1] == {"id": "12345678901", "isSubjectConsent": True, "validations": {"selfie": {"image": FACE}}}
    me = await api.ok("GET", "/accounts/me", ada)
    assert me["profile"]["id_verified"] and me["profile"]["verified_name"] == "Ada Obi"
    # The full NIN is never stored.
    async with api.services() as svc:
        from app.models import IdentityVerification
        record = (await svc.identity.repo(IdentityVerification).list(IdentityVerification.account_id == ada.id))[-1]
        assert record.document_ref == "national_id:*******8901" and record.selfie_ref == FACE


async def test_passports_and_foreign_ids_are_checked_by_staff(api):
    fake = connect(api)
    ben = await api.signup("ben", name="Ben", region="United Kingdom")
    doc = await api.ok("POST", "/uploads", ben, data={"kind": "id_document"},
                       files={"file": ("passport.jpg", JPEG, "image/jpeg")})
    session = (await api.ok("POST", "/verification/identity/live-check", ben))["session_id"]
    fake.pass_live_check(session)
    r = await api.ok("POST", "/verification/identity", ben, json={
        "document_type": "passport", "document_ref": doc["ref"], "liveness_session_id": session})
    assert r["status"] == "manual_review" and not r["id_verified"]
    assert fake.compares[-1]["image1"] == FACE and fake.compares[-1]["image2"].startswith("data:image/jpeg;base64,")

    # Staff see what the face check found, and must give the name and date of birth from the document.
    admin = await api.admin()
    items = await api.ok("GET", "/admin/queues/identity", admin)
    item = next(i for i in items if i["target_type"] == "identity_document")
    assert "Live face matches the document photo" in item["summary"]
    await api.ok("POST", f"/admin/queue-items/{item['id']}/claim", admin)
    r = await api.call("POST", f"/admin/queue-items/{item['id']}/decide", admin, expect=422,
                       json={"decision": "approve", "reason": "Checked", "payload": {"verified_name": "Ben Carter"}})
    assert r.json()["code"] == "document_details_required"
    await api.ok("POST", f"/admin/queue-items/{item['id']}/decide", admin, json={
        "decision": "approve", "reason": "Checked", "payload": {"verified_name": "Ben  Carter", "verified_dob": "1991-02-03"}})
    me = await api.ok("GET", "/accounts/me", ben)
    assert me["profile"]["id_verified"] and me["profile"]["verified_name"] == "Ben Carter"


async def test_live_check_falls_back_to_the_camera_without_youverify(api):
    ada = await api.signup("ada", name="Ada")
    assert await api.ok("POST", "/verification/identity/live-check", ada) == {"provider": "camera"}


async def test_a_face_that_doesnt_match_the_government_photo_is_refused(api):
    fake = connect(api)
    fake.face_matches = False
    ada = await api.signup("ada", name="Ada")
    session = (await api.ok("POST", "/verification/identity/live-check", ada))["session_id"]
    fake.pass_live_check(session)
    r = await api.ok("POST", "/verification/identity", ada, json={
        "document_type": "drivers_licence", "id_number": "12345678901", "liveness_session_id": session})
    assert r["status"] == "failed" and "doesn't match the photo" in r["reason"] and not r["id_verified"]


async def test_youverify_refusing_gives_a_clear_message(api):
    fake = connect(api)
    ada = await api.signup("ada", name="Ada")
    session = (await api.ok("POST", "/verification/identity/live-check", ada))["session_id"]
    real = fake.handler

    def refuse_liveness(request):  # as the sandbox does today until Liveness History is enabled on the account
        if request.url.path == "/v2/api/identity/liveness":
            return httpx.Response(403, json={"success": False, "message": "Permission denied"})
        return real(request)

    fake.handler = refuse_liveness
    api.infra.providers.identity._client._transport = httpx.MockTransport(refuse_liveness)
    r = await api.call("POST", "/verification/identity", ada, expect=503, json={
        "document_type": "national_id", "id_number": "12345678901", "liveness_session_id": session})
    assert r.json()["code"] == "identity_unavailable"
