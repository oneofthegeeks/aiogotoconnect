"""Inbound texts by polling, for hosts GoTo can't reach with a webhook (port of poller.ts).

Follows the geeks-tracker `fetchInbound` pattern: list recent conversations on the sending
number, then each conversation's messages since the cursor.
"""

import asyncio
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from aiogotoconnect.sms.config import ConfigResolver
from aiogotoconnect.sms.errors import GoToSmsError
from aiogotoconnect.sms.http import GoToHttp, error_name
from aiogotoconnect.sms.inbound.processor import InboundProcessor
from aiogotoconnect.sms.log import iso_ms
from aiogotoconnect.sms.phone import normalize_phone
from aiogotoconnect.sms.store.types import GoToSmsStore
from aiogotoconnect.sms.types import InboundMessage

CONVERSATIONS = "/messaging/v1/conversations"
MESSAGES = "/messaging/v1/messages"
CURSOR_KEY = "poll:cursor"
# Each poll reaches back this far before the cursor, to absorb clock and precision skew.
# Repeats are harmless: processor.persist() refuses ids it has already seen.
OVERLAP_MS = 5 * 60_000
# The furthest back a poll ever reads (R31). A cursor older than this (polling was off or the
# process down for longer, or paging stayed incomplete) is clamped, and the clamp is logged at
# error level: texts older than this are never fetched, so never auto-replied to months later.
MAX_LOOKBACK_MS = 24 * 3_600_000
# A complete tick with no traffic leaves the cursor at the last message. Once that is this
# stale, it moves up to now (minus the overlap), so a quiet number never reaches the clamp.
QUIET_ADVANCE_MS = 3_600_000
PAGE_SIZE = "100"
# Guards against a marker that never ends (same cap as directory.py list_numbers).
MAX_PAGES = 50


def _str(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _ms(v: Any) -> float:
    """Epoch ms of an ISO timestamp, NaN if unparseable (JS Date.parse, naive = UTC).

    GoTo mixes µs and ms precision, so timestamps are always compared as parsed instants, never
    as strings. Sub-millisecond digits are dropped, as a JS Date does.
    """
    if not isinstance(v, str):
        return math.nan
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return math.nan
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    whole = dt.replace(microsecond=0)
    return whole.timestamp() * 1000 + dt.microsecond // 1000


class Poller:
    """Polls GoTo for inbound texts. Each tick persists new messages (oldest first) through the
    processor, then runs `replay_pending()`, which processes them in arrival order and also
    retries any row an earlier attempt left pending (e.g. a failed opt-out write)."""

    def __init__(
        self,
        *,
        http: GoToHttp,
        config: ConfigResolver,
        processor: InboundProcessor,
        store: GoToSmsStore,
        tenant: str,
        poll_interval_s: float,
        now: Callable[[], float],
        logger: logging.Logger,
    ) -> None:
        self._http = http
        self._config = config
        self._processor = processor
        self._store = store
        self._tenant = tenant
        self._interval_s = poll_interval_s
        self._now = now
        self._logger = logger
        self._loop_task: asyncio.Task[None] | None = None
        # A stopped loop task until it has finished unwinding, so idle() can wait for it.
        self._stopped: asyncio.Task[None] | None = None
        # The tick in flight: ticks never overlap, and idle() waits for it.
        self._current: asyncio.Task[int] | None = None
        # The saved cursor last reported as clamped, so a held cursor logs once, not every tick.
        self._clamp_logged: float | None = None

    @property
    def running(self) -> bool:
        return self._loop_task is not None

    async def tick(self) -> int:
        """One poll. Returns how many new messages were stored. Raises on API/store errors.

        Concurrent callers share the one in-flight poll (and its result or error).
        """
        if self._current is None:
            task = asyncio.get_running_loop().create_task(self._poll())
            task.add_done_callback(self._tick_done)
            self._current = task
        # Shielded: a caller giving up must not cancel the poll the others are waiting on.
        return await asyncio.shield(self._current)

    def _tick_done(self, task: "asyncio.Task[int]") -> None:
        self._current = None
        if not task.cancelled():
            task.exception()  # mark retrieved; callers that were awaiting still see it

    def start(self) -> None:
        """Polls now (to catch up from the cursor), then every interval. Idempotent. Needs a
        running event loop."""
        if self._loop_task is not None:
            return
        self._loop_task = asyncio.get_running_loop().create_task(self._run())

    def stop(self) -> None:
        """Stops the loop. A tick already in flight finishes; `idle()` waits for it."""
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._stopped = self._loop_task
            self._loop_task = None

    async def reset_cursor(self) -> None:
        """Moves the cursor to now, so the next poll reads only from here on."""
        await self._store.set(self._tenant, CURSOR_KEY, iso_ms(self._now()))

    async def idle(self) -> None:
        """Returns once no tick is in flight and a stopped loop has finished unwinding."""
        stopped, self._stopped = self._stopped, None
        if stopped is not None:
            await asyncio.wait([stopped])
        current = self._current
        if current is not None:
            await asyncio.wait([current])

    # --- internals ---

    async def _run(self) -> None:
        while True:
            await self._safe_tick()
            await asyncio.sleep(self._interval_s)

    async def _safe_tick(self) -> None:
        try:
            await self.tick()
        except Exception as err:  # noqa: BLE001 - nothing may kill the polling loop
            self._logger.warning("goto-sms: polling for inbound texts failed (%s)", error_name(err))

    async def _all_pages(
        self, path: str, query: dict[str, str]
    ) -> tuple[list[dict[str, Any]], bool]:
        """GET every page of a list, following `nextPageMarker` as directory.py does. The flag
        is False when the page cap was hit or a marker repeated (a loop), i.e. items may be
        missing."""
        items: list[dict[str, Any]] = []
        markers: set[str] = set()
        marker: str | None = None
        for _ in range(MAX_PAGES):
            q = {**query, "pageSize": PAGE_SIZE}
            if marker:
                q["pageMarker"] = marker
            res = await self._http.json("GET", path, None, query=q)
            page = res.get("items") if isinstance(res, dict) else None
            if isinstance(page, list):
                items.extend(i for i in page if isinstance(i, dict))
            nxt = res.get("nextPageMarker") if isinstance(res, dict) else None
            if not isinstance(nxt, str) or nxt == "":
                return items, True
            if nxt in markers:
                return items, False
            markers.add(nxt)
            marker = nxt
        return items, False

    async def _poll(self) -> int:
        owner = (await self._config.resolve()).from_number
        if not owner:
            self._logger.debug("goto-sms: polling skipped; no sending number chosen")
            return 0

        # No cursor yet (first poll ever): start from now rather than replaying old history.
        tick_at = self._now()
        saved = _ms(await self._store.get(self._tenant, CURSOR_KEY))
        cursor = tick_at if math.isnan(saved) else saved
        floor = tick_at - MAX_LOOKBACK_MS
        if cursor - OVERLAP_MS < floor:
            cursor = floor + OVERLAP_MS
            if self._clamp_logged != saved:
                self._clamp_logged = saved
                self._logger.error(
                    "goto-sms: the poll cursor is more than 24 h old; texts before then are "
                    "not fetched (cursor %s)",
                    iso_ms(saved),
                )
        oldest = cursor - OVERLAP_MS
        oldest_timestamp = iso_ms(oldest)

        # Every page is read: the cursor advances past whatever the window held, so a text on
        # an unread page (a STOP, after downtime or a bulk send) would otherwise be lost for good.
        conversations, complete = await self._all_pages(
            CONVERSATIONS, {"ownerPhoneNumber": owner, "oldestTimestamp": oldest_timestamp}
        )
        batch: list[tuple[float, dict[str, Any]]] = []
        # The cursor follows all traffic, ours included (like geeks-tracker's use of
        # lastMessageTimestamp). Following only inbound texts would park it at the last reply,
        # and a busy sender would then re-list every contact texted since, every tick.
        # It's saved only after every new message is persisted, so a failed tick loses nothing.
        newest = cursor

        def seen_at(t: float) -> None:
            nonlocal newest
            if not math.isnan(t) and t > newest:
                newest = t

        for c in conversations:
            last = _ms(c.get("lastMessageTimestamp"))
            # Nothing new in this conversation. Strictly older only: µs and ms stamps can parse
            # equal.
            if last < oldest:
                continue
            contacts = c.get("contactPhoneNumbers")
            contact = _str(contacts[0]) if isinstance(contacts, list) and contacts else ""
            if not contact:
                continue
            seen_at(last)
            msgs, msgs_complete = await self._all_pages(
                MESSAGES,
                {
                    "ownerPhoneNumber": owner,
                    "contactPhoneNumber": contact,
                    "oldestTimestamp": oldest_timestamp,
                },
            )
            if not msgs_complete:
                complete = False
            for m in msgs:
                at = _ms(m.get("timestamp"))
                seen_at(at)
                if m.get("direction") not in ("IN", "INBOUND"):
                    continue
                batch.append((at, m))
        # Oldest first, so a START then a STOP from one number ends opted out. Unparseable
        # timestamps go last. (sorted is stable, like JS Array.sort.)
        batch.sort(key=lambda entry: math.inf if math.isnan(entry[0]) else entry[0])

        added = 0
        for at, m in batch:
            from_raw = _str(m.get("authorPhoneNumber"))
            from_ = normalize_phone(from_raw) or from_raw
            if from_ == owner:
                continue  # our own number: never treat our sends as inbound
            msg = InboundMessage(
                id=_str(m.get("id")),
                from_=from_,
                to=normalize_phone(_str(m.get("ownerPhoneNumber"))) or owner,
                text=_str(m.get("body")).strip(),
                received_at=iso_ms(self._now()) if math.isnan(at) else _str(m.get("timestamp")),
                raw=m,
            )
            try:
                if await self._processor.persist(msg):
                    added += 1
            except GoToSmsError as err:
                # A message that can't be stored (no id/sender/text) would fail every poll;
                # skip it rather than wedge the tick. Any other error (the store) aborts the
                # tick before the cursor is saved, so the message is fetched again next time.
                if err.code != "invalid_inbound":
                    raise
                self._logger.warning("goto-sms: skipped an inbound text with no id, sender or body")
        if not complete:
            # Paging was cut short, so texts newer than what we saw may be unread: keep the
            # cursor and read the window again next tick (persist() skips what's already
            # stored). The 24 h clamp above stops that window growing for good.
            self._logger.warning(
                "goto-sms: GoTo paging did not finish; the poll cursor was not advanced"
            )
        else:
            # Everything since `oldest` was read, so a stale cursor can safely move up to now.
            if newest < tick_at - QUIET_ADVANCE_MS:
                newest = tick_at - OVERLAP_MS
            if math.isnan(saved) or newest != saved:
                await self._store.set(self._tenant, CURSOR_KEY, iso_ms(newest))

        await self._processor.replay_pending()
        return added
