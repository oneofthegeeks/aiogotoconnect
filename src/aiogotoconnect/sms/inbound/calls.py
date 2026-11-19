"""Inbound calls to the sending number, by polling GoTo's call reports (`watch_calls=True`).

No Node counterpart yet. Shaped like poller.py: its own loop, one look at a time, state in the
store. The call-events-report API is only partly documented; this is what was observed live
(2026-11-19):

  GET /call-events-report/v1/report-summaries?accountKey&startTime&endTime[&pageSize&pageMarker]
      -> {"pageSize", "nextPageMarker"?, "items": [...]}, one item per call on the WHOLE account:
      conversationSpaceId, callCreated, callEnded, direction (INBOUND/OUTBOUND), callerOutcome
      (MISSED, NORMAL, LEFT_DIAL_PLAN, ...), caller {name, number, type {value, number}}.
      For an outside call in, caller.type.value is PHONE_NUMBER and caller.type.number is the
      number that was DIALLED; caller.name / caller.number are who called.
  GET /call-events-report/v1/reports/{conversationSpaceId}
      -> participants[].type {value: PHONE_NUMBER, number: dialled, caller {name, number}}.
      Read only when a summary does not name the dialled number.

A report appears once the call has ended. One ring can be reported as two conversations. Reading
needs the scope cr.v1.read on the OAuth client. Caller ID is whatever the caller's carrier sent:
it is data, it can be spoofed, and it never appears in a log line at INFO or above.
"""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from aiogotoconnect.sms.config import ConfigResolver
from aiogotoconnect.sms.errors import (
    GoToApiError,
    LoginExpiredError,
    NotConfiguredError,
    TemporaryError,
)
from aiogotoconnect.sms.http import GoToHttp, error_name
from aiogotoconnect.sms.inbound.poller import _ms
from aiogotoconnect.sms.log import iso_ms
from aiogotoconnect.sms.phone import normalize_phone
from aiogotoconnect.sms.store.types import GoToSmsStore, Json, json_equal
from aiogotoconnect.sms.types import InboundCall

SUMMARIES = "/call-events-report/v1/report-summaries"
REPORTS = "/call-events-report/v1/reports/"
CURSOR_KEY = "calls:cursor"
# When watching was first switched on. Calls that started before it are never delivered.
SINCE_KEY = "calls:since"
# [[id, ring, at_ms], ...]: every call already looked at. `ring` is set only for a delivered
# call whose caller number is a real one: "<E.164>|<outcome>", the memory for the double-ring
# rule. It is "" for everything else.
SEEN_KEY = "calls:seen"
ERROR_KEY = "calls:error"
# Written by oauth.py: the sealed token record. Read here only to notice that it changed.
TOKENS_KEY = "tokens"

# Each look reaches back this far before the cursor. A report appears only after the call ends,
# and the list is searched by when the call STARTED, so this is also the longest call that can
# still be noticed. (Whether GoTo filters on the start or the end is not documented; the start
# is the safe assumption.)
REACH_BACK_MS = 2 * 3_600_000
# The furthest back a look ever reads, however stale the cursor (as poller.MAX_LOOKBACK_MS).
MAX_LOOKBACK_MS = 24 * 3_600_000
# One ring reported as two conversations: the same caller with the same outcome again within
# this long is not an event. A different outcome (voicemail, then a miss) is a second call.
DOUBLE_RING_MS = 120_000
# Seen entries older than the reach-back are dropped, oldest first, down to this many.
SEEN_CAP = 400
# The window of the very first look. GoTo answers 400 to an empty one (startTime == endTime).
FIRST_LOOK_MS = 60_000
PAGE_SIZE = "100"
MAX_PAGES = 50
# While GoTo refuses the call reports (scope missing), look only this often. A 401 makes
# GoToHttp refresh the token before giving up; every two minutes, for good, would be abuse.
REFUSED_BACKOFF_MS = 3_600_000
SCOPE_MESSAGE = (
    "GoTo refused the call reports. Enable the scope cr.v1.read on the OAuth client, "
    "then reconnect."
)
NAME_MAX = 40
NUMBER_MAX = 20


def _str(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _iso_s(t_ms: float) -> str:
    """Whole seconds with a Z, the form GoTo's own examples use for startTime / endTime."""
    return f"{datetime.fromtimestamp(int(t_ms) // 1000, UTC):%Y-%m-%dT%H:%M:%SZ}"


def _number(raw: Any) -> str:
    """E.164 when it normalises, else GoTo's text cut short; "" when withheld."""
    text = _str(raw).strip()
    return normalize_phone(text) or text[:NUMBER_MAX]


def _name(raw: Any) -> str | None:
    return " ".join(_str(raw).split())[:NAME_MAX] or None


class CallWatcher:
    """Polls GoTo's call reports and emits one `InboundCall` per call dialled to the sending
    number. Delivery is at least once: a call is marked seen after `emit` returns, so a crash in
    between delivers that one call again."""

    def __init__(
        self,
        *,
        http: GoToHttp,
        config: ConfigResolver,
        store: GoToSmsStore,
        tenant: str,
        emit: Callable[[InboundCall], Awaitable[None]],
        interval_s: float,
        now: Callable[[], float],
        logger: logging.Logger,
    ) -> None:
        self._http = http
        self._config = config
        self._store = store
        self._tenant = tenant
        self._emit = emit
        self._interval_s = interval_s
        self._now = now
        self._logger = logger
        self._loop_task: asyncio.Task[None] | None = None
        self._stopped: asyncio.Task[None] | None = None
        self._current: asyncio.Task[int] | None = None
        # When GoTo last refused the call reports (epoch ms). In memory only, so a restart looks
        # once before backing off again.
        self._refused_at: float | None = None
        # The stored login (the sealed token record) at that moment. A different one means the
        # admin reconnected, or the token was renewed: worth one more look.
        self._refused_login: Json = None

    @property
    def running(self) -> bool:
        return self._loop_task is not None

    async def tick(self) -> int:
        """One look. Returns how many calls were delivered. Concurrent callers share the one
        look in flight (and its result or error)."""
        if self._current is None:
            task = asyncio.get_running_loop().create_task(self._look())
            task.add_done_callback(self._tick_done)
            self._current = task
        # Shielded: a caller giving up must not cancel the look the others are waiting on.
        return await asyncio.shield(self._current)

    def _tick_done(self, task: "asyncio.Task[int]") -> None:
        self._current = None
        if not task.cancelled():
            task.exception()  # mark retrieved; callers that were awaiting still see it

    def start(self) -> None:
        """Looks now, then every interval. Idempotent. Needs a running event loop."""
        if self._loop_task is None:
            self._loop_task = asyncio.get_running_loop().create_task(self._run())

    def stop(self) -> None:
        """Stops the loop. A look already in flight finishes; `idle()` waits for it."""
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._stopped = self._loop_task
            self._loop_task = None

    async def idle(self) -> None:
        """Returns once no look is in flight and a stopped loop has finished unwinding."""
        stopped, self._stopped = self._stopped, None
        if stopped is not None:
            await asyncio.wait([stopped])
        current = self._current
        if current is not None:
            await asyncio.wait([current])

    # --- internals ---

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as err:  # noqa: BLE001 - nothing may kill the loop
                self._logger.warning("goto-sms: checking for calls failed (%s)", error_name(err))
            await asyncio.sleep(self._interval_s)

    async def _summaries(self, query: dict[str, str]) -> tuple[list[dict[str, Any]], bool]:
        """Every page of the summaries list, following `nextPageMarker` as the text poller does.
        The flag is False when the page cap was hit or a marker repeated: items may be missing."""
        items: list[dict[str, Any]] = []
        markers: set[str] = set()
        marker: str | None = None
        for _ in range(MAX_PAGES):
            q = {**query, "pageSize": PAGE_SIZE}
            if marker:
                q["pageMarker"] = marker
            res = await self._http.json("GET", SUMMARIES, None, query=q)
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

    async def _from_report(self, call_id: str, owner: str) -> tuple[dict[str, Any], Any] | None:
        """(caller, the report) from the full report when the summary did not name the dialled
        number: the PHONE_NUMBER participant dialled to `owner` decides. None: not a call to us."""
        report = await self._http.json("GET", REPORTS + quote(call_id, safe=""), None)
        for participant in _dict(report).get("participants") or []:
            kind = _dict(_dict(participant).get("type"))
            if kind.get("value") == "PHONE_NUMBER" and _number(kind.get("number")) == owner:
                return _dict(kind.get("caller")), report
        return None

    async def _failed(self, err: Exception, now: float) -> None:
        """Records why the look failed, for `status()["calls"]`. The cursor is left alone, so the
        next good look reads the same window."""
        error: dict[str, Json]
        if isinstance(err, LoginExpiredError) or (
            isinstance(err, GoToApiError) and err.status == 403
        ):
            if await self._store.get(self._tenant, "login_expired") is True:
                # The login itself is dead (no request went out): say that, not "scope".
                error = {"error": "login_expired", "message": str(err)}
            else:
                # GoToHttp raises LoginExpiredError when a freshly refreshed token gets a second
                # 401. For this API that means the scope is missing, not that the login is gone:
                # nothing here marks the login expired, and texting carries on.
                status = err.status if isinstance(err, GoToApiError) else 401
                error = {"error": "calls_scope", "message": SCOPE_MESSAGE, "gotoStatus": status}
                self._refused_at = now
                self._refused_login = await self._store.get(self._tenant, TOKENS_KEY)
        elif isinstance(err, GoToApiError):
            error = {"error": "goto_api", "message": str(err), "gotoStatus": err.status}
        else:
            error = {"error": "goto_unreachable", "message": "GoTo could not be reached"}
        previous = await self._store.get(self._tenant, ERROR_KEY)
        if previous != error:
            await self._store.set(self._tenant, ERROR_KEY, error)
            if error["error"] == "calls_scope":
                self._logger.warning("goto-sms: %s", SCOPE_MESSAGE)
            else:
                self._logger.warning("goto-sms: checking for calls failed (%s)", error["error"])

    async def _load_seen(self) -> list[tuple[str, str, float]]:
        stored = await self._store.get(self._tenant, SEEN_KEY)
        seen: list[tuple[str, str, float]] = []
        for entry in stored if isinstance(stored, list) else []:
            if (
                isinstance(entry, list)
                and len(entry) == 3
                and isinstance(entry[0], str)
                and isinstance(entry[1], str)
                and isinstance(entry[2], int | float)
                and not isinstance(entry[2], bool)
            ):
                seen.append((entry[0], entry[1], float(entry[2])))
        return seen

    async def _save_seen(self, seen: list[tuple[str, str, float]]) -> None:
        value: Json = [[i, caller, at] for i, caller, at in seen]
        await self._store.set(self._tenant, SEEN_KEY, value)

    async def _look(self) -> int:
        resolved = await self._config.resolve()
        owner, account_key = resolved.from_number, resolved.account_key
        if not owner or not account_key:
            self._logger.debug("goto-sms: call check skipped; no account or sending number chosen")
            return 0

        now = self._now()
        if (
            self._refused_at is not None
            and 0 <= now - self._refused_at < REFUSED_BACKOFF_MS
            and json_equal(await self._store.get(self._tenant, TOKENS_KEY), self._refused_login)
        ):
            return 0
        cursor = _ms(await self._store.get(self._tenant, CURSOR_KEY))
        if math.isnan(cursor):
            # The first look ever: start from now. Old calls are never imported: whatever the
            # request below finds started before `since` and is only marked seen. It still goes
            # out, so a missing scope shows up at once.
            await self._store.set(self._tenant, SINCE_KEY, iso_ms(now))
            await self._store.set(self._tenant, CURSOR_KEY, iso_ms(now))
            start = now - FIRST_LOOK_MS
        else:
            start = max(cursor - REACH_BACK_MS, now - MAX_LOOKBACK_MS)
        since = _ms(await self._store.get(self._tenant, SINCE_KEY))
        try:
            summaries, complete = await self._summaries(
                {"accountKey": account_key, "startTime": _iso_s(start), "endTime": _iso_s(now)}
            )
        except NotConfiguredError:
            self._logger.debug("goto-sms: call check skipped; GoTo is not connected")
            return 0
        except (LoginExpiredError, GoToApiError, TemporaryError) as err:
            await self._failed(err, now)
            return 0
        self._refused_at = None
        if await self._store.get(self._tenant, ERROR_KEY) is not None:
            await self._store.delete(self._tenant, ERROR_KEY)

        seen = await self._load_seen()
        known = {i for i, _, _ in seen}
        dirty = False
        delivered = 0

        def created(s: dict[str, Any]) -> float:
            at = _ms(s.get("callCreated"))
            return now if math.isnan(at) else at

        # Oldest first, whatever order GoTo answered in, so a double ring keeps the first.
        for summary in sorted(summaries, key=created):
            call_id = _str(summary.get("conversationSpaceId"))
            if not call_id or call_id in known:
                continue
            at = created(summary)
            caller = _dict(summary.get("caller"))
            kind = _dict(caller.get("type"))
            raw: Any = summary
            # An outside call in is PHONE_NUMBER. A summary that does not say what the caller is
            # at all (an older shape) is settled by the full report; LINE and the rest are not ours.
            ours = summary.get("direction") == "INBOUND" and kind.get("value") in (
                "PHONE_NUMBER",
                None,
            )
            if ours and _str(kind.get("number")):
                ours = _number(kind.get("number")) == owner
            elif ours:
                try:
                    found = await self._from_report(call_id, owner)
                except Exception as err:  # noqa: BLE001 - left unseen: the next look tries again
                    self._logger.warning(
                        "goto-sms: reading a call report failed (%s)", error_name(err)
                    )
                    continue
                ours = found is not None
                if found is not None:
                    caller, raw = found
            from_ = _number(caller.get("number")) if ours else ""
            if ours and not math.isnan(since) and at < since:
                ours = False  # it started before watching was switched on
            outcome = _str(summary.get("callerOutcome"))
            # Only a real number can be "the same caller": a withheld one, or a carrier's
            # placeholder text, is shared by everyone who hides theirs.
            ring = f"{from_}|{outcome}" if ours and normalize_phone(from_) == from_ else ""
            if ring and any(r == ring and abs(at - t) < DOUBLE_RING_MS for _, r, t in seen):
                ours = False  # the same ring, reported twice
            if not ours:
                # `caller` stays empty: only a delivered call counts for the double-ring rule.
                seen.append((call_id, "", at))
                known.add(call_id)
                dirty = True
                continue

            created_text = _str(summary.get("callCreated"))
            call = InboundCall(
                id=call_id,
                from_=from_,
                to=owner,
                at=created_text if not math.isnan(_ms(created_text)) else iso_ms(now),
                outcome=outcome,
                raw=raw,
                caller_name=_name(caller.get("name")),
                ended_at=_str(summary.get("callEnded")) or None,
            )
            try:
                await self._emit(call)
            except Exception as err:  # noqa: BLE001 - as with texts: logged, and it stays delivered
                self._logger.warning("goto-sms: call handler threw (%s)", error_name(err))
            # One call at a time, after its handlers: a crash here redelivers at most this one.
            seen.append((call_id, ring, at))
            known.add(call_id)
            await self._save_seen(seen)
            dirty = False
            delivered += 1

        if not complete:
            # Paging was cut short, so newer calls may be unread: keep the cursor and read the
            # window again next time (seen ids are skipped). The 24 h clamp bounds that window.
            self._logger.warning(
                "goto-sms: GoTo paging of call reports did not finish; the cursor was not advanced"
            )
            if dirty:
                await self._save_seen(seen)
            return delivered

        # Entries the next look can still be shown (inside its reach-back) are never dropped;
        # older ones go, oldest first, down to the cap.
        keep_from = now - REACH_BACK_MS
        if len(seen) > SEEN_CAP:
            seen.sort(key=lambda entry: entry[2])
            droppable = sum(1 for _, _, t in seen if t < keep_from)
            del seen[: min(droppable, len(seen) - SEEN_CAP)]
            dirty = True
        if dirty:
            await self._save_seen(seen)
        await self._store.set(self._tenant, CURSOR_KEY, iso_ms(now))
        return delivered
