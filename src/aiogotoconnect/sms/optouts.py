"""Numbers that replied STOP, or that an admin added (port of Node optouts.ts).

Stored as `optout:<e164>` = `{"at": <ISO>, "source": "keyword" | "admin"}`.
"""

import builtins
from collections.abc import Callable
from typing import Literal

from aiogotoconnect.sms.errors import GoToSmsError
from aiogotoconnect.sms.log import iso_ms
from aiogotoconnect.sms.phone import normalize_phone
from aiogotoconnect.sms.store.types import GoToSmsStore, Json

PREFIX = "optout:"

OptOutSource = Literal["keyword", "admin"]


def _to_e164(number: str) -> str:
    e164 = normalize_phone(number)
    if e164 is None:
        raise GoToSmsError(f'"{number}" is not a valid phone number', "invalid_number")
    return e164


def _field(v: Json, k: str) -> str:
    x = v.get(k) if isinstance(v, dict) else None
    return x if isinstance(x, str) else ""


class OptOuts:
    def __init__(self, store: GoToSmsStore, tenant: str, now: Callable[[], float]) -> None:
        self._store = store
        self._tenant = tenant
        self._now = now

    async def has(self, e164: str) -> bool:
        n = normalize_phone(e164)
        return n is not None and await self._store.get(self._tenant, PREFIX + n) is not None

    async def add(self, e164: str, source: OptOutSource) -> None:
        await self._store.set(
            self._tenant, PREFIX + _to_e164(e164), {"at": iso_ms(self._now()), "source": source}
        )

    async def remove(self, e164: str) -> None:
        await self._store.delete(self._tenant, PREFIX + _to_e164(e164))

    async def list(self) -> builtins.list[dict[str, str]]:
        """Newest first (a stable sort, so equal times keep key order, as in Node)."""
        entries = [
            {
                "number": key[len(PREFIX) :],
                "at": _field(value, "at"),
                "source": _field(value, "source"),
            }
            for key, value in await self._store.list(self._tenant, PREFIX)
        ]
        return sorted(entries, key=lambda e: e["at"], reverse=True)


__all__ = ["OptOutSource", "OptOuts"]
