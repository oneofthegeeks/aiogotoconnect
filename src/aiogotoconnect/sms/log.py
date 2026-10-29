"""Message log, capped by age and count (port of Node log.ts). Message bodies are never stored."""

import builtins
import math
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, cast

from aiogotoconnect.sms.store.types import GoToSmsStore, Json
from aiogotoconnect.sms.types import LogEntry

PREFIX = "log:"
DAY_MS = 86_400_000
DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_ENTRIES = 5000
DEFAULT_LIST_LIMIT = 50
# Prune on every Nth append instead of on a timer, so the store stays small on its own.
PRUNE_EVERY = 100


def iso_ms(t_ms: float) -> str:
    """JavaScript's `new Date(t).toISOString()`: UTC, millisecond precision, a trailing Z.

    Fractional ms are truncated, as a JS Date does.
    """
    ms = int(t_ms)
    dt = datetime.fromtimestamp(ms // 1000, UTC)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{ms % 1000:03d}Z"


def _key_time(key: str) -> float:
    """The ms timestamp encoded in a `log:<15-digit ms>:<hex>` key; NaN if it has none."""
    try:
        return float(key[len(PREFIX) : len(PREFIX) + 15])
    except ValueError:
        return math.nan


def log_entry_to_json(e: LogEntry) -> dict[str, Json]:
    """The stored and wire form, Node's camelCase in Node's key order; unset fields omitted."""
    out: dict[str, Json] = {
        "id": e.id,
        "direction": e.direction,
        "from": e.from_,
        "to": e.to,
        "status": e.status,
    }
    if e.reason is not None:
        out["reason"] = e.reason
    if e.goto_id is not None:
        out["gotoId"] = e.goto_id
    out["at"] = e.at
    return out


def _as_log_entry(v: Json) -> LogEntry | None:
    """Node casts any stored object; Python checks the fields and skips what doesn't fit."""
    if not isinstance(v, dict):
        return None
    required = ("id", "direction", "from", "to", "status", "at")
    if not all(isinstance(v.get(k), str) for k in required):
        return None
    if v["direction"] not in ("outbound", "inbound"):
        return None
    reason = v.get("reason")
    goto_id = v.get("gotoId")
    return LogEntry(
        id=cast(str, v["id"]),
        direction=cast(Literal["outbound", "inbound"], v["direction"]),
        from_=cast(str, v["from"]),
        to=cast(str, v["to"]),
        status=cast(str, v["status"]),
        at=cast(str, v["at"]),
        reason=reason if isinstance(reason, str) else None,
        goto_id=goto_id if isinstance(goto_id, str) else None,
    )


class MessageLog:
    """Keys sort by time (zero-padded ms), so the store's ascending key order is oldest first."""

    def __init__(
        self,
        store: GoToSmsStore,
        tenant: str,
        now: Callable[[], float],
        retention_days: float = DEFAULT_RETENTION_DAYS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._store = store
        self._tenant = tenant
        self._now = now
        self.retention_days = retention_days
        self._max_entries = max_entries
        self._appends = 0

    async def append(
        self,
        *,
        direction: Literal["outbound", "inbound"],
        from_: str,
        to: str,
        status: str,
        reason: str | None = None,
        goto_id: str | None = None,
    ) -> LogEntry:
        # Keyword-only fields, so there is no way to hand a message body to the log.
        t = int(self._now())
        entry = LogEntry(
            id=f"{t:015d}:{os.urandom(4).hex()}",
            direction=direction,
            from_=from_,
            to=to,
            status=status,
            at=iso_ms(t),
            reason=reason,
            goto_id=goto_id,
        )
        await self._store.set(self._tenant, PREFIX + entry.id, log_entry_to_json(entry))
        self._appends += 1
        if self._appends % PRUNE_EVERY == 0:
            await self.prune()
        return entry

    async def list(self, limit: int = DEFAULT_LIST_LIMIT) -> builtins.list[LogEntry]:
        """Newest first."""
        out: builtins.list[LogEntry] = []
        for _key, value in reversed(await self._store.list(self._tenant, PREFIX)):
            if len(out) >= limit:
                break
            entry = _as_log_entry(value)
            if entry is not None:
                out.append(entry)
        return out

    async def prune(self) -> None:
        """Drops entries older than the retention period, then all but the newest max_entries."""
        cutoff = self._now() - self.retention_days * DAY_MS
        keys = [k for k, _ in await self._store.list(self._tenant, PREFIX)]
        # A NaN key time compares False, so a malformed key is never "expired" (as in Node).
        expired = [k for k in keys if _key_time(k) < cutoff]
        live = [k for k in keys if not _key_time(k) < cutoff]
        # Oldest first, so the overflow is the front of what's left.
        overflow = live[: max(0, len(live) - self._max_entries)]
        for key in expired + overflow:
            await self._store.delete(self._tenant, key)


__all__ = ["PRUNE_EVERY", "MessageLog", "iso_ms", "log_entry_to_json"]
