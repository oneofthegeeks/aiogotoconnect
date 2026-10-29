"""Port of store/file.ts."""

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Callable
from pathlib import Path

from aiogotoconnect.sms.store.memory import MemoryStore
from aiogotoconnect.sms.store.types import Json


class FileStore(MemoryStore):
    """JSON-file store: an in-memory map (MemoryStore) flushed to disk after every mutation.

    Flushes are serialized through one lock and written to a unique temp file then
    ``os.replace``d, so readers never see a half-written file. Single-process only;
    cross-process exclusion is the caller's job (claim).
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        now: Callable[[], float] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(now)
        self._path = Path(path)
        self._logger = logger
        self._lock = asyncio.Lock()
        self._load()

    async def set(self, tenant: str, key: str, value: Json) -> None:
        await super().set(tenant, key, value)
        await self._flush()

    async def delete(self, tenant: str, key: str) -> None:
        await super().delete(tenant, key)
        await self._flush()

    async def compare_and_set(self, tenant: str, key: str, expected: Json, next: Json) -> bool:
        won = await super().compare_and_set(tenant, key, expected, next)
        if won:
            await self._flush()
        return won

    async def claim(self, tenant: str, key: str, ttl_ms: float, owner: str) -> bool:
        won = await super().claim(tenant, key, ttl_ms, owner)
        if won:
            await self._flush()
        return won

    def _load(self) -> None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        try:
            snapshot = json.loads(text)
            if not isinstance(snapshot, dict) or not all(
                isinstance(entries, dict) for entries in snapshot.values()
            ):
                raise ValueError("unexpected file shape")
        except ValueError:
            # Keep the unreadable file for forensics rather than overwrite it, and start empty.
            aside = f"{self._path}.corrupt-{int(self._now())}"
            os.replace(self._path, aside)
            if self._logger is not None:
                self._logger.warning(
                    "goto-sms: store file was not valid JSON; moved aside and starting empty "
                    "(movedTo=%s)",
                    aside,
                )
            return
        self._data = snapshot

    async def _flush(self) -> None:
        # The lock serializes writers; the snapshot is taken inside it so the last writer
        # always persists the newest state. A failed write raises to its caller only.
        async with self._lock:
            payload = json.dumps(self._data)
            await asyncio.to_thread(self._write, payload)

    def _write(self, payload: str) -> None:
        # Unique name + O_EXCL: never follows or overwrites a file/symlink planted at the tmp
        # path, and mode 0600 is guaranteed because the file is always freshly created.
        tmp = f"{self._path}.{os.getpid()}.{uuid.uuid4()}.tmp"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
