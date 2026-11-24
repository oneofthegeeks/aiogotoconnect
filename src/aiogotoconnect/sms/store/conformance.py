"""Port of store/conformance.ts: the behavioural contract every GoToSmsStore must satisfy."""

import asyncio
from collections.abc import Awaitable, Callable

from aiogotoconnect.sms.store.types import GoToSmsStore

MakeStore = Callable[[], Awaitable[GoToSmsStore]]


async def run_store_conformance(make_store: MakeStore) -> None:
    """Run every conformance case, each against a fresh empty store from `make_store`.

    Raises AssertionError on the first violation. The claim-expiry case uses real (short)
    sleeps because the factory has no way to inject a clock.
    """
    for case in _CASES:
        await case(await make_store())


async def _set_get_round_trip(s: GoToSmsStore) -> None:
    assert await s.get("t", "missing") is None
    value: dict[str, object] = {"a": [1, "two", None, True], "b": {"c": "d"}}
    await s.set("t", "k", value)  # type: ignore[arg-type]
    assert await s.get("t", "k") == value
    await s.set("t", "k", "overwritten")
    assert await s.get("t", "k") == "overwritten"


async def _delete(s: GoToSmsStore) -> None:
    await s.set("t", "k", 1)
    await s.delete("t", "k")
    assert await s.get("t", "k") is None
    await s.delete("t", "never-existed")


async def _list_prefix_sorted(s: GoToSmsStore) -> None:
    await s.set("t", "log:b", 2)
    await s.set("t", "log:a", 1)
    await s.set("t", "log:c", 3)
    await s.set("t", "optout:x", 9)
    await s.set("t", "lo%", 8)  # LIKE wildcard must not leak into prefix matching
    assert await s.list("t", "log:") == [("log:a", 1), ("log:b", 2), ("log:c", 3)]
    assert await s.list("t", "nothing:") == []
    assert await s.list("t", "lo%") == [("lo%", 8)]


async def _tenant_isolation(s: GoToSmsStore) -> None:
    await s.set("one", "k", "x")
    await s.set("two", "k", "y")
    assert await s.get("one", "k") == "x"
    assert await s.get("two", "k") == "y"
    assert len(await s.list("one", "")) == 1
    await s.delete("one", "k")
    assert await s.get("two", "k") == "y"


async def _compare_and_set(s: GoToSmsStore) -> None:
    assert await s.compare_and_set("t", "k", None, {"n": 1}) is True
    assert await s.compare_and_set("t", "k", None, {"n": 2}) is False
    assert await s.get("t", "k") == {"n": 1}
    assert await s.compare_and_set("t", "k", {"n": 99}, {"n": 2}) is False
    # Deep equality, independent of dict key order.
    assert await s.compare_and_set("t", "k", {"n": 1}, {"n": 2, "m": [1]}) is True
    assert await s.compare_and_set("t", "k", {"m": [1], "n": 2}, "done") is True
    assert await s.get("t", "k") == "done"


async def _compare_and_set_concurrent(s: GoToSmsStore) -> None:
    await s.set("t", "k", 0)
    results = await asyncio.gather(
        s.compare_and_set("t", "k", 0, 1), s.compare_and_set("t", "k", 0, 2)
    )
    assert sum(results) == 1


async def _claim(s: GoToSmsStore) -> None:
    ttl_ms = 400
    assert await s.claim("t", "lock", ttl_ms, "a") is True
    assert await s.claim("t", "lock", ttl_ms, "b") is False
    assert await s.claim("t", "lock", ttl_ms, "a") is True  # same owner re-claims
    await asyncio.sleep(0.2)
    assert await s.claim("t", "lock", ttl_ms, "b") is False  # lease was renewed
    await asyncio.sleep(0.3)  # past the renewed expiry
    assert await s.claim("t", "lock", ttl_ms, "b") is True
    assert await s.claim("t", "lock", ttl_ms, "a") is False


_CASES = [
    _set_get_round_trip,
    _delete,
    _list_prefix_sorted,
    _tenant_isolation,
    _compare_and_set,
    _compare_and_set_concurrent,
    _claim,
]
