"""Live provider adapters, exercised against a fake HTTP server so nothing real is sent."""
import json

import httpx
import pytest

from app.container import build_email
from app.infra.providers.resend import EmailDeliveryError, ResendEmailGateway
from app.infra.providers.sandbox import SandboxEmailGateway
from tests.conftest import make_settings


def gateway(handler, **kwargs) -> ResendEmailGateway:
    return ResendEmailGateway("re_test_key", "no-reply@wamiwa.site", transport=httpx.MockTransport(handler),
                              backoff_seconds=0, **kwargs)


async def test_resend_sends_the_expected_request():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "abc"})

    await gateway(handler, reply_to="info@wamiwa.site").send("ada@example.com", "Your Wamiwa code", "Your code is 123456.")
    request = seen[0]
    assert str(request.url) == "https://api.resend.com/emails" and request.method == "POST"
    assert request.headers["Authorization"] == "Bearer re_test_key"
    assert json.loads(request.content) == {
        "from": "Wamiwa <no-reply@wamiwa.site>", "to": ["ada@example.com"],
        "subject": "Your Wamiwa code", "text": "Your code is 123456.", "reply_to": "info@wamiwa.site"}


async def test_resend_retries_temporary_failures_then_succeeds():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, json={"message": "Too many requests"}) if len(calls) < 3 else httpx.Response(200, json={"id": "x"})

    await gateway(handler).send("ada@example.com", "Hi", "Body")
    assert len(calls) == 3


async def test_resend_gives_up_at_once_on_a_refusal_and_says_why():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403, json={"message": "The wamiwa.site domain is not verified."})

    with pytest.raises(EmailDeliveryError, match="not verified"):
        await gateway(handler).send("ada@example.com", "Hi", "Body")
    assert len(calls) == 1  # retrying cannot fix an unverified domain

    def always_down(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="down")

    calls.clear()
    with pytest.raises(EmailDeliveryError, match="503"):
        await gateway(always_down).send("ada@example.com", "Hi", "Body")
    assert len(calls) == 3  # the first try plus two retries


def test_real_email_is_only_used_when_asked_for_by_name():
    assert isinstance(build_email(make_settings()), SandboxEmailGateway)  # tests never send real email
    assert isinstance(build_email(make_settings(email_provider="sandbox", email_api_key="re_x")), SandboxEmailGateway)
    assert isinstance(build_email(make_settings(email_provider="resend", email_api_key="re_x")), ResendEmailGateway)
    with pytest.raises(RuntimeError):
        build_email(make_settings(email_provider="resend", email_api_key=None))
