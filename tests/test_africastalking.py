"""Africa's Talking for SMS and the LK-09 guardian confirmation call, against a fake that behaves like its API."""
from urllib.parse import parse_qs

import httpx
import pytest

from app.infra.providers.africastalking import AfricasTalkingError, AfricasTalkingSmsGateway, AfricasTalkingVoiceGateway
from tests.test_hiring import offer


class FakeAfricasTalking:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict, str]] = []
        self.sms_status = (101, "Success")

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append((str(request.url), form, request.headers.get("apiKey")))
        if request.url.path == "/version1/messaging":
            code, status = self.sms_status
            return httpx.Response(201, json={"SMSMessageData": {"Message": "Sent to 1/1", "Recipients": [
                {"statusCode": code, "number": form["to"], "status": status, "messageId": "ATXid_1"}]}})
        if request.url.path == "/call":
            return httpx.Response(200, json={"entries": [
                {"phoneNumber": form["to"], "status": "Queued", "sessionId": f"ATVId_{len(self.requests)}"}],
                "errorMessage": "None"})
        return httpx.Response(404)


async def test_sms_goes_to_africastalking_and_failures_are_raised():
    fake = FakeAfricasTalking()
    sms = AfricasTalkingSmsGateway("wamiwa", "key-1", "Wamiwa", transport=httpx.MockTransport(fake.handler),
                                   backoff_seconds=0)
    await sms.send("2348011112222", "Your Wamiwa code is 123456")
    url, form, key = fake.requests[-1]
    assert url == "https://api.africastalking.com/version1/messaging" and key == "key-1"
    assert form == {"username": "wamiwa", "to": "+2348011112222", "message": "Your Wamiwa code is 123456",
                    "from": "Wamiwa"}
    fake.sms_status = (406, "UserInBlacklist")
    with pytest.raises(AfricasTalkingError, match="UserInBlacklist"):
        await sms.send("+2348011112222", "hi")
    # The sandbox username uses the sandbox, which reaches no real phone.
    sandbox = AfricasTalkingSmsGateway("sandbox", "k", transport=httpx.MockTransport(fake.handler), backoff_seconds=0)
    fake.sms_status = (101, "Success")
    await sandbox.send("+2348011112222", "hi")
    assert fake.requests[-1][0] == "https://api.sandbox.africastalking.com/version1/messaging"


async def minor_contract_awaiting_call(api):
    """A minor's contract the guardian has approved in the app, so the confirmation call is placed."""
    admin = await api.admin()
    brand = await api.company("acme", admin, category="fashion")
    minor = await api.signup("teen", dob="2010-03-01", name="Teen Ogunleye")
    guardian = await api.signup("mum", name="Funmilayo Ogunleye")
    await api.ok("POST", "/guardians/invite", minor, json={"contact": "mum@example.com"})
    token = api.infra.providers.email.last_for("mum@example.com")["body"].split("token=")[1]
    await api.ok("POST", "/guardians/accept", guardian, json={"token": token, "relationship": "mother",
                                                              "confirm_relationship": True})
    await api.verify_id(guardian, "Funmilayo Ogunleye")
    await api.bank(guardian, "Funmilayo Ogunleye")
    await api.ok("PUT", "/hire-me/page", minor, json={"starting_price_cents": 20_00})
    contract = await api.ok("POST", "/hire-me/offers", brand, json=offer(api, minor, 40_00))
    await api.ok("POST", f"/hire-me/contracts/{contract['id']}/accept", minor)
    await api.verify_phone(guardian, "+2348011112222")
    await api.ok("POST", f"/guardians/confirmations/{contract['id']}/approve", guardian)
    return brand, guardian, contract["id"]


async def test_guardian_confirmation_call_step_by_step(api):
    api.infra.settings.africastalking_callback_token = "cb-secret"
    fake = FakeAfricasTalking()
    api.infra.providers.voice = AfricasTalkingVoiceGateway(
        "wamiwa", "key-1", "+2342017001234", transport=httpx.MockTransport(fake.handler), backoff_seconds=0)
    brand, guardian, cid = await minor_contract_awaiting_call(api)
    url, form, _ = fake.requests[-1]
    assert url == "https://voice.africastalking.com/call"
    assert form["from"] == "+2342017001234" and form["to"] == "+2348011112222"
    session = "ATVId_1"
    code = api.sms_code("+2348011112222")

    async def step(**fields) -> str:
        r = await api.call("POST", "/webhooks/africastalking/voice?token=cb-secret", expect=200,
                           data={"sessionId": session, "isActive": "1", **fields})
        assert r.headers["content-type"].startswith("application/xml")
        return r.text

    # Without the token, nothing answers.
    await api.call("POST", "/webhooks/africastalking/voice?token=wrong", expect=404, data={"sessionId": session})
    # Answered: the contract is read out (escaped for XML) and the code asked for.
    first = await step()
    assert "Acme Ltd" in first and "40.00" in first and '<GetDigits numDigits="6"' in first
    # A wrong code is refused, then the right one leads to the choice.
    assert "not correct" in await step(dtmfDigits="000000")
    assert "Press 1 to approve" in await step(dtmfDigits=f"{code}#")
    # 1, then the spoken confirmation is recorded; only then is the contract active.
    recording = await step(dtmfDigits="1")
    assert "<Record" in recording and "I approve" in recording
    assert (await api.ok("GET", f"/contracts/{cid}", brand))["status"] == "pending_guardian"
    assert "approved" in await step(recordingUrl="https://voice.africastalking.com/rec/1.mp3")
    assert (await api.ok("GET", f"/contracts/{cid}", brand))["status"] == "active"
    status = await api.ok("GET", f"/guardians/confirmations/{cid}", guardian)
    assert status["result"] == "approved" and status["recorded"]
    # The call is over: later posts for it change nothing.
    assert await step(isActive="0") == '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


async def test_a_call_that_ends_early_decides_nothing(api):
    api.infra.settings.africastalking_callback_token = "cb-secret"
    fake = FakeAfricasTalking()
    api.infra.providers.voice = AfricasTalkingVoiceGateway(
        "wamiwa", "key-1", "+2342017001234", transport=httpx.MockTransport(fake.handler), backoff_seconds=0)
    brand, guardian, cid = await minor_contract_awaiting_call(api)
    code = api.sms_code("+2348011112222")
    url = "/webhooks/africastalking/voice?token=cb-secret"
    for fields in ({"isActive": "1"}, {"isActive": "1", "dtmfDigits": code}, {"isActive": "1", "dtmfDigits": "1"},
                   {"isActive": "0", "durationInSeconds": "40"}):  # hung up before the recording
        await api.call("POST", url, expect=200, data={"sessionId": "ATVId_1", **fields})
    assert (await api.ok("GET", f"/contracts/{cid}", brand))["status"] == "pending_guardian"
    status = await api.ok("GET", f"/guardians/confirmations/{cid}", guardian)
    assert status["result"] == "awaiting_call" and status["next_call_at"]  # the retry schedule carries on


def test_africastalking_is_used_once_its_settings_are_set():
    from app.container import build_sms_and_voice
    from app.core.config import Settings

    def kinds(**values):
        sms, voice = build_sms_and_voice(Settings(_env_file=None, **values))
        return type(sms).__name__, type(voice).__name__

    assert kinds() == ("SandboxSmsGateway", "SandboxVoiceGateway")
    keys = dict(africastalking_username="wamiwa", africastalking_api_key="k")
    # Calls need the voice number and the callback token as well; SMS doesn't.
    assert kinds(**keys) == ("AfricasTalkingSmsGateway", "SandboxVoiceGateway")
    assert kinds(**keys, africastalking_voice_number="+2342017001234", africastalking_callback_token="t") == (
        "AfricasTalkingSmsGateway", "AfricasTalkingVoiceGateway")
