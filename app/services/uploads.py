"""Uploaded files: who owns each one, checking a file is yours before it's used, and cleaning up.

A file reference ("ref") is only accepted where it belongs to the person using it and is the right kind of
file. On the live site anything that isn't an uploaded file is refused; in development and tests, stand-in
refs (demo photos, sandbox documents) are allowed so the app can be clicked through without real files.
"""
from datetime import timedelta
from typing import Iterable

from app.core.exceptions import NotFound, Unprocessable
from app.infra.storage import KEY_RE, LocalFileStorage
from app.models import Account, AgentProfile, Upload
from app.services.base import BaseService

UNUSED_FILE_HOURS = 24


class UploadService(BaseService):
    def storage(self) -> LocalFileStorage:
        return LocalFileStorage(self.s.upload_dir)

    async def record(self, owner: Account, key: str, kind: str, content_type: str, size: int) -> Upload:
        upload = await self.repo(Upload).add(Upload(
            key=key, owner_id=owner.id, kind=kind, content_type=content_type, size_bytes=size))

        async def remove_file() -> None:  # the request failed after the file was written: don't keep it
            await self.storage().delete(key)

        self.uow.on_rollback(remove_file)
        return upload

    async def _owners(self, account: Account) -> set[str]:
        """An agent's files are uploaded by its owner from the app."""
        if account.is_agent:
            agent = await self.repo(AgentProfile).first(AgentProfile.account_id == account.id)
            return {account.id, agent.owner_account_id} if agent else {account.id}
        return {account.id}

    async def claim(self, account: Account, refs: Iterable[str | None], kinds: set[str], *,
                    allow_links: bool = False) -> None:
        """Check each ref is a file this account uploaded, of one of `kinds`, and mark it used."""
        owners: set[str] | None = None
        for ref in refs:
            if not ref:
                continue
            if KEY_RE.match(ref):
                owners = owners or await self._owners(account)
                upload = await self.repo(Upload).first(Upload.key == ref)
                if upload is None or upload.owner_id not in owners:
                    raise NotFound("File not found. Upload it again.", code="file_not_found")
                if upload.kind not in kinds:
                    raise Unprocessable(f"This needs a file of kind: {', '.join(sorted(kinds))}", code="wrong_file_kind",
                                        found=upload.kind)
                upload.used_at = upload.used_at or self.now()
            elif allow_links and ref.startswith("https://"):
                continue
            elif self.s.is_production:
                raise Unprocessable("Upload the file first with POST /uploads and use the ref it returns",
                                    code="invalid_file_ref")

    async def purge_unused(self) -> int:
        """Job: files uploaded but never attached to anything within a day are deleted."""
        cutoff = self.now() - timedelta(hours=UNUSED_FILE_HOURS)
        stale = await self.repo(Upload).list(Upload.used_at.is_(None), Upload.created_at < cutoff, limit=1000)
        for upload in stale:
            await self._remove(upload)
        return len(stale)

    async def delete_for_account(self, account_id: str) -> int:
        """DL-01: a deleted account's photos, voice samples, documents and videos are deleted too."""
        uploads = await self.repo(Upload).list(Upload.owner_id == account_id, limit=100_000)
        for upload in uploads:
            await self._remove(upload)
        return len(uploads)

    async def _remove(self, upload: Upload) -> None:
        key = upload.key

        async def remove_file() -> None:  # only once the database agrees the file is gone
            await self.storage().delete(key)

        await self.repo(Upload).delete(upload)
        self.uow.on_commit(remove_file)
