"""Port of store/types.ts: the store protocol plus the shared equality and claim rules."""

import builtins
from typing import Protocol

# Stored JSON `null` is not a supported value: as in Node, where `undefined` means absent,
# `None` returned by `get` means "no such key". Nested nulls (inside lists/dicts) are fine.
type Json = None | bool | int | float | str | list[Json] | dict[str, Json]


class GoToSmsStore(Protocol):
    """Async key/value store, namespaced by tenant. Values are JSON (never a top-level None)."""

    async def get(self, tenant: str, key: str) -> Json:
        """The stored value, or None when the key is absent."""
        ...

    async def set(self, tenant: str, key: str, value: Json) -> None: ...

    async def delete(self, tenant: str, key: str) -> None: ...

    async def list(self, tenant: str, prefix: str) -> builtins.list[tuple[str, Json]]:
        """Entries whose key starts with `prefix`, sorted by key ascending."""
        ...

    async def compare_and_set(self, tenant: str, key: str, expected: Json, next: Json) -> bool:
        """Atomically set `next` iff the current value deep-equals `expected` (None = absent)."""
        ...

    async def claim(self, tenant: str, key: str, ttl_ms: float, owner: str) -> bool:
        """True if the key is free, expired, or already owned by `owner` (lease is renewed)."""
        ...


def json_equal(a: Json, b: Json) -> bool:
    """Deep equality for JSON values; dict key order is irrelevant.

    Unlike Python's ``==``, ``True`` does not equal ``1`` (JSON booleans are not numbers).
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, dict):
        return (
            isinstance(b, dict)
            and len(a) == len(b)
            and all(k in b and json_equal(v, b[k]) for k, v in a.items())
        )
    if isinstance(a, list):
        return (
            isinstance(b, list)
            and len(a) == len(b)
            and all(json_equal(x, y) for x, y in zip(a, b, strict=True))
        )
    if isinstance(b, dict | list):
        return False
    return a == b


def next_claim(current: Json, owner: str, ttl_ms: float, now: float) -> Json:
    """Shared claim rule. Claims are stored as ``{"owner", "expiresAt"}`` under the key.

    Returns the new claim value to store, or None when someone else holds a live claim.
    A value that isn't a claim record is treated as free.
    """
    if isinstance(current, dict):
        held_by = current.get("owner")
        expires_at = current.get("expiresAt")
        if (
            isinstance(held_by, str)
            and isinstance(expires_at, int | float)
            and not isinstance(expires_at, bool)
            and held_by != owner
            and expires_at > now
        ):
            return None
    return {"owner": owner, "expiresAt": now + ttl_ms}
