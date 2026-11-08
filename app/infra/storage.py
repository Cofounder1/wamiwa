"""Where uploaded files live: local disk on a laptop, a private Supabase Storage bucket on the live site.

Every upload lands on local disk first, where its size and contents are checked and photo location data is
removed; `finalize` then moves it to the bucket. Files are always served by the API itself (never straight from the
bucket), so the safety headers apply and private kinds are never served at all.
"""
import asyncio
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from functools import lru_cache
from typing import AsyncIterator

import httpx

KEY_RE = re.compile(r"^[a-z_]+/[0-9a-f]{32}\.[a-z0-9]{1,5}$")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


@dataclass
class Served:
    """A file, or the part of it a Range header asked for (206), ready to send. Phones and Safari only play a video
    when the server answers byte ranges."""
    chunks: AsyncIterator[bytes]
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """(first, last) byte asked for, inclusive; None for the whole file. ValueError when it can't be satisfied."""
    match = RANGE_RE.match((header or "").strip())
    if not match or match.groups() == ("", ""):
        return None
    start, end = match.groups()
    if start == "":  # the last N bytes
        first, last = max(0, size - int(end)), size - 1
    else:
        first, last = int(start), min(int(end), size - 1) if end else size - 1
    if first > last or first >= size:
        raise ValueError("range not satisfiable")
    return first, last


class FileStorage(ABC):
    @abstractmethod
    async def save(self, key: str, chunks: AsyncIterator[bytes], max_bytes: int) -> int:
        """Store the file and return its size. Raises ValueError (and keeps nothing) past `max_bytes`."""

    @abstractmethod
    def path(self, key: str) -> Path | None:
        """The local copy, if this storage keeps one."""

    async def finalize(self, key: str, content_type: str) -> None:
        """After the checks: move the upload to permanent storage. Local disk already is."""

    async def read(self, key: str, max_bytes: int) -> bytes | None:
        """The whole file, if it exists and is no larger than `max_bytes`."""
        path = self.path(key)
        if path is None or path.stat().st_size > max_bytes:
            return None
        return await asyncio.to_thread(path.read_bytes)

    async def stream(self, key: str) -> AsyncIterator[bytes] | None:
        """The file in chunks, for serving, or None if it doesn't exist."""
        path = self.path(key)
        if path is None:
            return None

        async def chunks() -> AsyncIterator[bytes]:
            handle = await asyncio.to_thread(open, path, "rb")
            try:
                while chunk := await asyncio.to_thread(handle.read, 1024 * 1024):
                    yield chunk
            finally:
                await asyncio.to_thread(handle.close)

        return chunks()

    async def serve(self, key: str, range_header: str | None = None) -> Served | None:
        """The file for an HTTP answer: all of it, or the byte range asked for."""
        path = self.path(key)
        if path is None:
            return None
        size = (await asyncio.to_thread(path.stat)).st_size
        try:
            span = byte_range(range_header, size)
        except ValueError:
            return Served(_empty(), 416, {"Content-Range": f"bytes */{size}"})
        first, last = span or (0, size - 1)

        async def chunks() -> AsyncIterator[bytes]:
            handle = await asyncio.to_thread(open, path, "rb")
            try:
                await asyncio.to_thread(handle.seek, first)
                left = last - first + 1
                while left > 0 and (chunk := await asyncio.to_thread(handle.read, min(1024 * 1024, left))):
                    left -= len(chunk)
                    yield chunk
            finally:
                await asyncio.to_thread(handle.close)

        headers = {"Accept-Ranges": "bytes", "Content-Length": str(last - first + 1)}
        if span is None:
            return Served(chunks(), 200, headers)
        return Served(chunks(), 206, headers | {"Content-Range": f"bytes {first}-{last}/{size}"})

    @abstractmethod
    async def delete(self, key: str) -> None: ...


class LocalFileStorage(FileStorage):
    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def _resolve(self, key: str) -> Path:
        if not KEY_RE.match(key):
            raise ValueError("invalid file key")
        return self.root / key

    async def save(self, key: str, chunks: AsyncIterator[bytes], max_bytes: int) -> int:
        """Disk writes run in a worker thread, so a 200 MB video doesn't hold up other requests."""
        target = self._resolve(key)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        partial, size = target.with_suffix(target.suffix + ".part"), 0
        try:
            handle = await asyncio.to_thread(open, partial, "wb")
            try:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("file too large")
                    await asyncio.to_thread(handle.write, chunk)
            finally:
                await asyncio.to_thread(handle.close)
            await asyncio.to_thread(os.replace, partial, target)
        finally:
            if partial.exists():
                partial.unlink()
        return size

    def path(self, key: str) -> Path | None:
        try:
            target = self._resolve(key)
        except ValueError:
            return None
        return target if target.is_file() else None

    async def delete(self, key: str) -> None:
        target = self.path(key)
        if target:
            target.unlink(missing_ok=True)


class SupabaseFileStorage(LocalFileStorage):
    """A private Supabase Storage bucket, through its REST API with a server-only key. Uploads are staged on local
    disk for the checks, then copied to the bucket and the local copy deleted."""

    def __init__(self, staging_root: str, url: str, key: str, bucket: str, *, timeout: float = 120.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(staging_root)
        headers = {"apikey": key}
        if key.startswith("eyJ"):  # a legacy service_role JWT also goes in Authorization; sb_secret_ keys don't
            headers["Authorization"] = f"Bearer {key}"
        self._client = httpx.AsyncClient(base_url=f"{url.rstrip('/')}/storage/v1", headers=headers, timeout=timeout,
                                         transport=transport)
        self.bucket = bucket

    def _object(self, key: str) -> str:
        if not KEY_RE.match(key):
            raise ValueError("invalid file key")
        return f"/object/{self.bucket}/{key}"

    async def finalize(self, key: str, content_type: str) -> None:
        local = super().path(key)
        if local is None:
            raise OSError(f"upload {key} vanished before it was stored")

        async def body() -> AsyncIterator[bytes]:
            handle = await asyncio.to_thread(open, local, "rb")
            try:
                while chunk := await asyncio.to_thread(handle.read, 1024 * 1024):
                    yield chunk
            finally:
                await asyncio.to_thread(handle.close)

        response = await self._client.post(self._object(key), content=body(), headers={
            "Content-Type": content_type, "x-upsert": "true", "Content-Length": str(local.stat().st_size)})
        if response.status_code >= 300:
            raise OSError(f"Supabase Storage refused {key}: HTTP {response.status_code} {response.text[:200]}")
        local.unlink(missing_ok=True)

    async def read(self, key: str, max_bytes: int) -> bytes | None:
        local = await super().read(key, max_bytes)
        if local is not None or super().path(key) is not None:
            return local
        response = await self._client.get(self._object(key))
        if response.status_code != 200 or len(response.content) > max_bytes:
            return None
        return response.content

    async def stream(self, key: str) -> AsyncIterator[bytes] | None:
        if super().path(key) is not None:  # still staged: not moved yet
            return await super().stream(key)
        request = self._client.build_request("GET", self._object(key))
        response = await self._client.send(request, stream=True)
        if response.status_code != 200:
            await response.aclose()
            return None

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    yield chunk
            finally:
                await response.aclose()

        return chunks()

    async def serve(self, key: str, range_header: str | None = None) -> Served | None:
        if super().path(key) is not None:  # still staged: not moved yet
            return await super().serve(key, range_header)
        headers = {"Range": range_header} if range_header and RANGE_RE.match(range_header.strip()) else {}
        request = self._client.build_request("GET", self._object(key), headers=headers)
        response = await self._client.send(request, stream=True)
        if response.status_code == 416:
            await response.aclose()
            return Served(_empty(), 416, {k: v for k, v in response.headers.items() if k.lower() == "content-range"})
        if response.status_code not in (200, 206):
            await response.aclose()
            return None

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    yield chunk
            finally:
                await response.aclose()

        passed = {"Accept-Ranges": "bytes"}
        for name in ("Content-Length", "Content-Range"):
            if name in response.headers:
                passed[name] = response.headers[name]
        return Served(chunks(), response.status_code, passed)

    async def delete(self, key: str) -> None:
        await super().delete(key)
        response = await self._client.request("DELETE", f"/object/{self.bucket}", json={"prefixes": [key]})
        if response.status_code >= 300 and response.status_code != 404:
            raise OSError(f"Supabase Storage didn't delete {key}: HTTP {response.status_code}")


async def _empty() -> AsyncIterator[bytes]:
    return
    yield b""


@lru_cache(maxsize=4)
def _cached(staging_root: str, url: str | None, key: str | None, bucket: str) -> FileStorage:
    if url and key:
        return SupabaseFileStorage(staging_root, url, key, bucket)
    return LocalFileStorage(staging_root)


def build_storage(settings) -> FileStorage:
    """The one storage for this process: Supabase once its URL and key are set, local disk otherwise."""
    return _cached(settings.upload_dir, settings.supabase_url, settings.supabase_secret_key, settings.supabase_storage_bucket)
