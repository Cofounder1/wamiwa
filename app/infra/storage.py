"""Where uploaded files live. Local disk for development; a cloud bucket adapter replaces it later."""
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import AsyncIterator

KEY_RE = re.compile(r"^[a-z_]+/[0-9a-f]{32}\.[a-z0-9]{1,5}$")


class FileStorage(ABC):
    @abstractmethod
    async def save(self, key: str, chunks: AsyncIterator[bytes], max_bytes: int) -> int:
        """Store the file and return its size. Raises ValueError (and keeps nothing) past `max_bytes`."""

    @abstractmethod
    def path(self, key: str) -> Path | None:
        """Local path to serve the file from, or None if it does not exist."""

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
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial, size = target.with_suffix(target.suffix + ".part"), 0
        try:
            with open(partial, "wb") as handle:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("file too large")
                    handle.write(chunk)
            os.replace(partial, target)
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
            target.unlink()
