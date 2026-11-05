"""YouVerify for ID checks (ON-05) and face matching (LK-01), through its v2 API.

How a check works:
  1. The browser asks Wamiwa to start YouVerify's live check (`start_liveness`): a session id and token, made
     here with the secret key so it never reaches the browser.
  2. YouVerify's liveness SDK runs in the page. Afterwards Wamiwa asks YouVerify itself whether that session
     passed and for the face it captured; the browser's own report is never trusted.
  3. Nigerian NIN or driver's licence: the number is looked up in the government database, which gives the name,
     date of birth and photo, and YouVerify matches that photo with the live face. A match verifies at once.
  4. A passport, or an ID from outside Nigeria: the photo of the document is compared with the live face, and the
     check goes to Wamiwa's staff to confirm the name and date of birth (`needs_review`).

Sandbox keys work only against the sandbox, which answers with test data.
"""
import asyncio
import base64
import logging
from datetime import date

import httpx

from app.core.exceptions import ServiceUnavailable
from app.infra.providers.base import IdentityGateway, IdentityResult
from app.infra.storage import FileStorage

log = logging.getLogger("wamiwa.youverify")

BASE_URLS = {"sandbox": "https://api.sandbox.youverify.co", "live": "https://api.youverify.co"}
LOOKUPS = {"national_id": ("/v2/api/identity/ng/nin", "NIN"),
           "drivers_licence": ("/v2/api/identity/ng/drivers-license", "driver's licence number")}
COMPARE_MAX_BYTES = 1024 * 1024  # YouVerify compares images of up to 1 MB
IMAGE_TYPES = {b"\xff\xd8": "image/jpeg", b"\x89P": "image/png"}


class YouVerifyError(RuntimeError):
    pass


class YouVerifyIdentityGateway(IdentityGateway):
    def __init__(self, api_key: str, public_merchant_key: str, environment: str = "sandbox", *, storage: FileStorage,
                 protected_names: set[str] | frozenset[str] = frozenset(), timeout: float = 30.0, retries: int = 2,
                 backoff_seconds: float = 0.5, transport: httpx.AsyncBaseTransport | None = None):
        if environment not in BASE_URLS:
            raise ValueError("YouVerify environment must be 'sandbox' or 'live'")
        self.environment, self._merchant = environment, public_merchant_key
        self._storage, self._protected = storage, {n.strip().lower() for n in protected_names}
        self._retries, self._backoff = retries, backoff_seconds
        self._client = httpx.AsyncClient(base_url=BASE_URLS[environment], timeout=timeout, transport=transport,
                                         headers={"token": api_key, "User-Agent": "wamiwa-backend/0.1"})

    async def _call(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> dict:
        """The response body. Retries network errors, 429 and 5xx; a refusal is raised with YouVerify's reason."""
        problem = "no attempt made"
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.request(method, path, json=json, params=params)
            except httpx.TransportError as exc:
                problem = f"network error: {exc}"
            else:
                try:
                    body = response.json()
                except ValueError:
                    body = {"message": response.text[:200]}
                if response.status_code < 300:
                    return body
                problem = f"HTTP {response.status_code}: {body.get('message')}"
                if response.status_code != 429 and response.status_code < 500:
                    break
            if attempt < self._retries:
                await asyncio.sleep(self._backoff * 2 ** attempt)
        log.error("YouVerify %s %s failed: %s", method, path, problem)
        raise YouVerifyError(problem)

    @staticmethod
    def _unavailable(error: YouVerifyError) -> ServiceUnavailable:
        """What the person sees when YouVerify can't be used; the real reason is in the log."""
        return ServiceUnavailable("ID checks aren't available right now. Try again shortly.", code="identity_unavailable")

    # --- live check ------------------------------------------------------------------------------------
    async def start_liveness(self, account_id: str) -> dict:
        try:
            return await self._start_liveness(account_id)
        except YouVerifyError as exc:
            raise self._unavailable(exc) from exc

    async def _start_liveness(self, account_id: str) -> dict:
        session = await self._call("POST", "/v2/api/identity/sdk/session/generate", json={
            "publicMerchantID": self._merchant, "metadata": {"account_id": account_id}})
        session_id = (session.get("data") or {}).get("sessionId")
        token = await self._call("POST", "/v2/api/identity/sdk/liveness/token", json={
            "publicMerchantID": self._merchant, "deviceCorrelationId": session_id})
        auth_token = (token.get("data") or {}).get("authToken")
        if not session_id or not auth_token:
            raise YouVerifyError("YouVerify gave no session for the live check")
        return {"provider": "youverify", "session_id": session_id, "session_token": auth_token,
                "public_merchant_key": self._merchant, "sandbox": self.environment == "sandbox"}

    async def _liveness(self, session_id: str) -> tuple[bool, str | None]:
        """Whether YouVerify recorded a passed live check for this session, and the face it captured."""
        body = await self._call("GET", "/v2/api/identity/liveness", params={"sessionId": session_id, "limit": "5"})
        docs = [d for d in (body.get("data") or {}).get("docs") or [] if d.get("sessionId") == session_id]
        passed = next((d for d in docs if d.get("passed") and d.get("faceImage")), None)
        return (True, passed["faceImage"]) if passed else (False, None)

    # --- ID check --------------------------------------------------------------------------------------
    async def verify(self, document_type: str, document_ref: str | None, selfie_ref: str | None,
                     liveness_ref: str | None = None, *, id_number: str | None = None,
                     liveness_session: str | None = None, in_home_country: bool = True) -> IdentityResult:
        try:
            return await self._verify(document_type, document_ref, id_number, liveness_session, in_home_country)
        except YouVerifyError as exc:
            raise self._unavailable(exc) from exc

    async def _verify(self, document_type: str, document_ref: str | None, id_number: str | None,
                      liveness_session: str | None, in_home_country: bool) -> IdentityResult:
        if not liveness_session:
            return IdentityResult(False, False, failure_reason="Complete the live check first")
        alive, face = await self._liveness(liveness_session)
        if not alive:
            return IdentityResult(False, False, failure_reason="The live check didn't pass. Try it again in good light.")

        if in_home_country and document_type in LOOKUPS:
            path, what = LOOKUPS[document_type]
            number = (id_number or "").replace(" ", "")
            if not number:
                return IdentityResult(False, True, failure_reason=f"Enter your {what}")
            body = await self._call("POST", path, json={"id": number, "isSubjectConsent": True,
                                                        "validations": {"selfie": {"image": face}}})
            record = body.get("data") or {}
            if record.get("status") != "found":
                return IdentityResult(False, True, failure_reason=f"We couldn't find that {what}. Check it and try again.")
            selfie = ((record.get("validations") or {}).get("selfie") or {}).get("selfieVerification") or {}
            if not selfie.get("match"):
                return IdentityResult(False, True, failure_reason="Your face doesn't match the photo on your ID")
            name = " ".join(p for p in (record.get("firstName"), record.get("middleName"), record.get("lastName")) if p)
            try:
                born = date.fromisoformat(str(record.get("dateOfBirth"))[:10])
            except ValueError:
                born = None
            return IdentityResult(True, True, verified_name=name.title() or None, date_of_birth=born, face_ref=face)

        # Passports and IDs from elsewhere: the document's photo against the live face, then a person checks.
        if not document_ref:
            return IdentityResult(False, True, failure_reason="Upload a photo of your passport or ID")
        match, note = await self._compare(face, document_ref)
        note = {True: f"Live face matches the document photo ({note})",
                False: f"Live face did NOT match the document photo ({note})",
                None: f"Faces not compared automatically: {note}. Compare them by eye"}[match]
        return IdentityResult(False, True, face_ref=face, needs_review=True, review_note=note)

    async def _compare(self, face_url: str, file_ref: str) -> tuple[bool | None, str]:
        """Compare a face with an uploaded image: (match, confidence), or (None, why it couldn't be compared)."""
        try:
            data = await self._storage.read(file_ref, COMPARE_MAX_BYTES + 1)
        except ValueError:
            data = None
        if data is None:
            return None, "the file is missing or over 1 MB"
        kind = IMAGE_TYPES.get(data[:2])
        if kind is None:
            return None, "the file isn't a photo"
        if len(data) > COMPARE_MAX_BYTES:
            return None, "the photo is over 1 MB"
        body = await self._call("POST", "/v2/api/identity/compare-image", json={
            "image1": face_url, "image2": f"data:{kind};base64,{base64.b64encode(data).decode()}",
            "isSubjectConsent": True})
        result = (body.get("data") or {}).get("imageComparison") or {}
        return bool(result.get("match")), f"{result.get('confidenceLevel')}% confidence"

    async def face_matches(self, face_ref: str, photo_ref: str) -> bool:
        """LK-01: a likeness photo must show the verified face. One that can't be compared is not accepted."""
        try:
            match, why = await self._compare(face_ref, photo_ref)
        except YouVerifyError as exc:
            raise self._unavailable(exc) from exc
        if match is None:
            log.warning("likeness photo %s not compared: %s", photo_ref, why)
        return bool(match)

    async def is_protected_identity(self, name: str) -> bool:
        return name.strip().lower() in self._protected
