"""Port of store/memory.ts."""

import builtins
import copy
import time
from collections.abc import Callable

from aiogotoconnect.sms.store.types import Json, json_equal, next_claim


def _now_ms() -> float:
    return time.time() * 1000


class MemoryStore:
    """In-process store. No method awaits, so each operation is atomic on the event loop.

    Values are deep-copied on the way in and out. FileStore extends this and persists after
    each mutation.
    """

    def __init__(self, now: Callable[[], float] | None = None) -> None:
        self._data: dict[str, dict[str, Json]] = {}
        self._now = now or _now_ms

    async def get(self, tenant: str, key: str) -> Json:
        return copy.deepcopy(self._data.get(tenant, {}).get(key))

    async def set(self, tenant: str, key: str, value: Json) -> None:
        self._put(tenant, key, value)

    async def delete(self, tenant: str, key: str) -> None:
        self._data.get(tenant, {}).pop(key, None)

    async def list(self, tenant: str, prefix: str) -> builtins.list[tuple[str, Json]]:
        bucket = self._data.get(tenant, {})
        return [(k, copy.deepcopy(bucket[k])) for k in sorted(bucket) if k.startswith(prefix)]

    async def compare_and_set(self, tenant: str, key: str, expected: Json, next: Json) -> bool:
        if not json_equal(self._data.get(tenant, {}).get(key), expected):
            return False
        self._put(tenant, key, next)
        return True

    async def claim(self, tenant: str, key: str, ttl_ms: float, owner: str) -> bool:
        claim = next_claim(self._data.get(tenant, {}).get(key), owner, ttl_ms, self._now())
        if claim is None:
            return False
        self._put(tenant, key, claim)
        return True

    def _put(self, tenant: str, key: str, value: Json) -> None:
        self._data.setdefault(tenant, {})[key] = copy.deepcopy(value)
