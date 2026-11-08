"""File uploads for the apps: photos, videos, voice notes and documents.

Upload first, then pass the returned `ref` wherever the API asks for one (a post's media, a chat
attachment, a likeness photo, an ID document). Who can fetch a file back depends on its kind:

  image, video                 anyone with the link (they appear in public posts and profiles)
  voice_note, document         anyone with the link, which is unguessable; not cached publicly
                               (chat attachments: browsers cannot send a login token for <audio> or links)
  likeness_voice, id_document, never served back; only Wamiwa's own services read them
  liveness                     (liveness: the live camera frame and clip taken during ID verification)
"""
import asyncio
import logging
import secrets
from typing import AsyncIterator

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.api.deps import Active, Services, ok
from app.core.exceptions import NotFound, ServiceUnavailable, Unprocessable
from app.infra import filetypes, metadata
from app.infra.storage import FileStorage, build_storage

router = APIRouter(tags=["uploads"])
log = logging.getLogger("wamiwa.uploads")

ALLOWED_TYPES: dict[str, dict[str, str]] = {
    "image": {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic"},
    "video": {"video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm"},
    "voice_note": {"audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac", "audio/ogg": "ogg",
                   "audio/webm": "weba", "audio/wav": "wav"},
    "likeness_voice": {"audio/wav": "wav", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/webm": "weba"},
    "document": {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "text/plain": "txt",
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx"},
    "id_document": {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"},
    "liveness": {"image/jpeg": "jpg", "video/webm": "webm", "video/mp4": "mp4"},
}
PUBLIC_KINDS = {"image", "video"}
MEMBER_KINDS = {"voice_note", "document"}


def storage(request: Request) -> FileStorage:
    return build_storage(request.app.state.infra.settings)


def upload_limit(settings, kind: str) -> int:
    """The kind's limit, never above what the bucket accepts once uploads go to Supabase."""
    limit = settings.upload_max_bytes[kind]
    if settings.supabase_url and settings.supabase_secret_key:
        limit = min(limit, settings.supabase_max_file_mb * 1024 * 1024)
    return limit


EXTENSION_TYPES = {ext: content_type for types in ALLOWED_TYPES.values() for content_type, ext in types.items()}


async def _chunks(upload: UploadFile, first: bytes) -> AsyncIterator[bytes]:
    if first:
        yield first
    while chunk := await upload.read(1024 * 1024):
        yield chunk


@router.post("/uploads", status_code=201)
async def upload_file(request: Request, p: Active, svc: Services, kind: str = Form(...), file: UploadFile = File(...)):
    allowed = ALLOWED_TYPES.get(kind)
    if allowed is None:
        raise Unprocessable(f"kind must be one of {', '.join(ALLOWED_TYPES)}", code="invalid_kind")
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in allowed:
        raise Unprocessable(f"A {kind.replace('_', ' ')} must be one of: {', '.join(sorted(allowed))}",
                            code="unsupported_file_type")
    settings = request.app.state.infra.settings
    await svc.access.limiter.enforce(svc.access.policies.uploads, p.id)
    first = await file.read(64 * 1024)
    if not first:
        raise Unprocessable("The file is empty", code="empty_file")
    if not filetypes.matches(content_type, first):
        # The browser's label is not trusted: the file's own first bytes must match the type it claims.
        raise Unprocessable(f"This file isn't a real {content_type} file", code="file_content_mismatch")
    key = f"{kind}/{secrets.token_hex(16)}.{allowed[content_type]}"
    limit = upload_limit(settings, kind)
    try:
        size = await storage(request).save(key, _chunks(file, first), limit)
    except ValueError as exc:
        raise Unprocessable(f"Files of this kind can be up to {limit // (1024 * 1024)} MB", code="file_too_large") from exc
    except OSError as exc:
        log.exception("could not store upload %s", key)
        raise ServiceUnavailable("We couldn't store your file right now. Try again shortly.",
                                 code="storage_unavailable") from exc
    if kind in PUBLIC_KINDS:  # photos and videos are public: remove the GPS position and other hidden metadata
        stored = storage(request).path(key)
        if stored is not None:
            size = await asyncio.to_thread(metadata.scrub_file, stored, content_type)
    try:
        await storage(request).finalize(key, content_type)  # to the bucket on the live site; nothing on a laptop
    except OSError as exc:
        log.exception("could not store upload %s", key)
        await storage(request).delete(key)
        raise ServiceUnavailable("We couldn't store your file right now. Try again shortly.",
                                 code="storage_unavailable") from exc
    await svc.uploads.record(p.account, key, kind, content_type, size)
    url = f"/media/{key}" if kind in PUBLIC_KINDS | MEMBER_KINDS else None
    return ok({"ref": key, "kind": kind, "size_bytes": size, "content_type": content_type, "url": url})


@router.get("/media/{kind}/{name}", include_in_schema=False)
async def serve_file(kind: str, name: str, request: Request):
    if kind not in PUBLIC_KINDS | MEMBER_KINDS:
        raise NotFound("File not found")
    try:
        served = await storage(request).serve(f"{kind}/{name}", request.headers.get("range"))
    except ValueError:  # not a valid file name
        served = None
    if served is None:
        raise NotFound("File not found")
    headers = {
        **served.headers,
        "Cache-Control": "public, max-age=31536000, immutable" if kind in PUBLIC_KINDS else "private, max-age=3600",
        # Served exactly as the stored type and never run as a page, whatever is inside.
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cross-Origin-Resource-Policy": "cross-origin",
    }
    if kind == "document":  # documents download rather than open in the API's own origin
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    media_type = EXTENSION_TYPES.get(name.rsplit(".", 1)[-1].lower(), "application/octet-stream")
    return StreamingResponse(served.chunks, status_code=served.status, media_type=media_type, headers=headers)
