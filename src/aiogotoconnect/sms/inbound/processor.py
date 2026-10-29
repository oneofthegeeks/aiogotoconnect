"""Durable inbound text handling (port of Node inbound/processor.ts).

A webhook/poller calls ``persist()`` first (so a crash can't lose a STOP), then ``process()``.
On start, ``replay_pending()`` finishes anything persisted but not processed. Keywords
(STOP/START/HELP) are acted on before the rate limit, so a flood of texts can never swallow an
opt-out.
"""

import dataclasses
import inspect
import logging
import math
import re
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from aiogotoconnect.sms.errors import GoToSmsError
from aiogotoconnect.sms.http import error_name
from aiogotoconnect.sms.inbound.keywords import KeywordReplies, detect_keyword
from aiogotoconnect.sms.log import PRUNE_EVERY, MessageLog, iso_ms
from aiogotoconnect.sms.optouts import OptOuts
from aiogotoconnect.sms.phone import normalize_phone
from aiogotoconnect.sms.send import Sender
from aiogotoconnect.sms.store.types import GoToSmsStore, Json
from aiogotoconnect.sms.types import InboundMessage, Keyword

PENDING_PREFIX = "inbound:pending:"
SEEN_PREFIX = "inbound:seen:"
RATE_PREFIX = "rate:"
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
DEFAULT_SEEN_RETENTION_DAYS = 90

MessageHandler = Callable[[InboundMessage], Awaitable[None] | None]
"""An ``on("message")`` handler, or the processor's ``emit``: sync, or async (awaited)."""

_HANDLER_THREW = "goto-sms: inbound handler threw (%s)"


def _last4(phone: str) -> str:
    return re.sub(r"\D", "", phone)[-4:]


def _non_empty(v: object) -> bool:
    return isinstance(v, str) and v != ""


def _parse_ms(v: Json) -> float:
    """Epoch ms of an ISO timestamp (naive = UTC); NaN if it isn't one (as JS Date.parse)."""
    if not isinstance(v, str):
        return math.nan
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return math.nan
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp() * 1000


def _from_row(v: Json, fallback_at: str) -> InboundMessage | None:
    """A stored pending row back to a message; None if it isn't one.

    Only id, from and text are required (what keyword handling needs), so a STOP is never
    dropped over a missing ``to`` or ``receivedAt``.
    """
    if not isinstance(v, dict):
        return None
    id_, from_, text = v.get("id"), v.get("from"), v.get("text")
    if not (isinstance(id_, str) and id_ and isinstance(from_, str) and from_):
        return None
    if not (isinstance(text, str) and text):
        return None
    to, received_at, account_key = v.get("to"), v.get("receivedAt"), v.get("accountKey")
    return InboundMessage(
        id=id_,
        from_=from_,
        to=to if isinstance(to, str) else "",
        text=text,
        received_at=received_at if isinstance(received_at, str) else fallback_at,
        raw=v.get("raw"),
        account_key=account_key if isinstance(account_key, str) else None,
    )


async def call_handlers(
    handlers: Iterable[MessageHandler], m: InboundMessage, logger: logging.Logger
) -> None:
    """Calls each handler in turn, awaiting async ones in the caller's task (no new tasks).

    A handler that raises is logged and the rest still run. Only the error's name is logged at
    WARNING: a handler's message could quote the text. The traceback goes to DEBUG.
    CancelledError (a BaseException) still propagates.
    """
    for handler in list(handlers):
        try:
            result = handler(m)
            if inspect.isawaitable(result):
                await result
        except Exception as err:
            logger.warning(_HANDLER_THREW, error_name(err))
            logger.debug("goto-sms: inbound handler traceback", exc_info=True)


class InboundProcessor:
    """Handles inbound texts durably; see the module docstring.

    ``emit`` is supplied by the facade (it fans out to ``on("message")`` handlers, typically via
    ``call_handlers``). It may be sync or async; an async emit is awaited in the processor's
    task. Anything it raises is logged and never fails processing.
    """

    def __init__(
        self,
        *,
        store: GoToSmsStore,
        tenant: str,
        sender: Sender,
        opt_outs: OptOuts,
        log: MessageLog,
        emit: MessageHandler,
        replies: KeywordReplies,
        yes_is_opt_in: bool,
        per_hour: int,
        now: Callable[[], float],
        logger: logging.Logger,
        seen_retention_days: float = DEFAULT_SEEN_RETENTION_DAYS,
    ) -> None:
        self._store = store
        self._tenant = tenant
        self._sender = sender
        self._opt_outs = opt_outs
        self._log = log
        self._emit = emit
        self._replies = replies
        self._yes_is_opt_in = yes_is_opt_in
        # Texts per number per clock hour that reach `emit`; keywords are handled regardless.
        self._per_hour = per_hour
        self._now = now
        self._logger = logger
        # How long a handled GoTo id is remembered for dedupe. Matches the log's retention.
        self._seen_retention_days = seen_retention_days
        # Ids being processed in this process; a concurrent call for the same id is a no-op.
        self._in_progress: set[str] = set()
        self._processed = 0

    async def persist(self, m: InboundMessage) -> bool:
        """Stores the message as pending. False if this GoTo id was already seen or is pending.

        Raises ``GoToSmsError("invalid_inbound")`` when id, from or text is missing, so the
        caller doesn't acknowledge a message that couldn't be stored.
        """
        # Checked at runtime: the message comes from a parsed GoTo payload, not typed code.
        id_: Any = getattr(m, "id", None)
        from_: Any = getattr(m, "from_", None)
        text: Any = getattr(m, "text", None)
        if not (_non_empty(id_) and _non_empty(from_) and _non_empty(text)):
            raise GoToSmsError("Inbound text is missing its id, sender or body", "invalid_inbound")
        if await self._store.get(self._tenant, SEEN_PREFIX + id_) is not None:
            return False
        to = getattr(m, "to", None)
        received_at = getattr(m, "received_at", None)
        account_key = getattr(m, "account_key", None)
        row: dict[str, Json] = {
            "id": id_,
            "from": from_,
            # `to` and `receivedAt` are optional in practice; default them so the row is complete.
            "to": to if isinstance(to, str) else "",
            "text": text,
            "receivedAt": received_at if isinstance(received_at, str) else iso_ms(self._now()),
        }
        if account_key is not None:
            row["accountKey"] = account_key
        # GoTo's payload is parsed JSON, so it is stored as-is.
        row["raw"] = getattr(m, "raw", None)
        # compare_and_set against "absent" makes the duplicate check and the write one step.
        return await self._store.compare_and_set(self._tenant, PENDING_PREFIX + id_, None, row)

    async def process(self, id_: str) -> None:
        """Handles one pending message.

        Raises only if a store write fails, in which case the pending row stays and
        ``replay_pending()`` retries it.
        """
        # Check-and-add before any await, so a concurrent call for the same id is a no-op.
        if id_ in self._in_progress:
            return
        self._in_progress.add(id_)
        try:
            await self._handle(id_)
        finally:
            self._in_progress.discard(id_)

    async def replay_pending(self) -> int:
        """Processes every pending message in arrival order. Returns how many were handled."""

        # Keys sort by GoTo id, which isn't time order. Order matters: a START then a STOP from
        # one number must end opted out, so replay by receivedAt (unreadable rows last, where
        # _handle() drops them), with the key as a stable tie-break.
        def arrival(entry: tuple[str, Json]) -> tuple[float, str]:
            key, value = entry
            t = _parse_ms(value.get("receivedAt") if isinstance(value, dict) else None)
            return (math.inf if math.isnan(t) else t, key)

        pending = sorted(await self._store.list(self._tenant, PENDING_PREFIX), key=arrival)
        count = 0
        for key, _ in pending:
            try:
                await self.process(key[len(PENDING_PREFIX) :])
                count += 1
            except Exception as err:  # noqa: BLE001 - one bad row must not stop the replay
                # Left pending for the next replay; keep going with the others.
                self._logger.warning(
                    "goto-sms: could not process a pending inbound text (%s)", error_name(err)
                )
        return count

    async def prune(self) -> None:
        """Drops rate buckets before the current hour and seen ids past their retention."""
        t = self._now()
        hour = math.floor(t / HOUR_MS)
        for key, _ in await self._store.list(self._tenant, RATE_PREFIX):
            # `rate:<from>:<bucket>`; the bucket is after the last colon. Unparseable = stale.
            try:
                bucket = float(key[key.rfind(":") + 1 :])
            except ValueError:
                bucket = math.nan
            if not bucket >= hour:
                await self._store.delete(self._tenant, key)
        cutoff = t - self._seen_retention_days * DAY_MS
        for key, value in await self._store.list(self._tenant, SEEN_PREFIX):
            at = value.get("at") if isinstance(value, dict) else None
            if not (isinstance(at, int | float) and not isinstance(at, bool)) or at < cutoff:
                await self._store.delete(self._tenant, key)

    # --- internals ---

    async def _handle(self, id_: str) -> None:
        store, tenant, logger = self._store, self._tenant, self._logger
        pending_key = PENDING_PREFIX + id_
        row = await store.get(tenant, pending_key)
        if row is None:
            return
        if await store.get(tenant, SEEN_PREFIX + id_) is not None:
            await store.delete(tenant, pending_key)
            return
        msg = _from_row(row, iso_ms(self._now()))
        if msg is None:
            # Unreadable row: drop it rather than fail on it at every replay.
            logger.warning("goto-sms: dropped a malformed pending inbound row")
            await store.delete(tenant, pending_key)
            return

        # Keyword first, before the rate limit: a STOP must take effect even mid-flood.
        keyword = detect_keyword(msg.text, self._yes_is_opt_in)
        if keyword is not None:
            await self._apply_keyword(keyword, msg)

        if await self._over_rate_limit(msg.from_):
            await self._log_inbound(msg, "rate_limited")
            logger.info("goto-sms: inbound from …%s: rate_limited", _last4(msg.from_))
        else:
            await self._log_inbound(msg, "received")
            logger.info(
                "goto-sms: inbound from …%s: received%s",
                _last4(msg.from_),
                f" ({keyword})" if keyword else "",
            )
            try:
                result = self._emit(dataclasses.replace(msg, keyword=keyword) if keyword else msg)
                if inspect.isawaitable(result):
                    await result
            except Exception as err:
                # Only the error's name: a handler's message could quote the text.
                logger.warning(_HANDLER_THREW, error_name(err))
                logger.debug("goto-sms: inbound handler traceback", exc_info=True)

        # Marked seen only after emit: a crash in between replays the message, so delivery is
        # at-least-once (the keyword reply is deduped by its dedupe key; the handler may see a
        # message twice). Marking first would make it at-most-once and could lose a message.
        await store.set(tenant, SEEN_PREFIX + id_, {"at": self._now()})
        await store.delete(tenant, pending_key)

        self._processed += 1
        if self._processed % PRUNE_EVERY == 0:
            try:
                await self.prune()
            except Exception as err:  # noqa: BLE001 - pruning is housekeeping
                logger.warning("goto-sms: could not prune inbound records (%s)", error_name(err))

    async def _apply_keyword(self, keyword: Keyword, msg: InboundMessage) -> None:
        """Saves the opt-out change (store errors propagate, so the message stays pending and
        is retried), then sends the reply. A failed reply is only logged."""
        logger = self._logger
        from_ = normalize_phone(msg.from_)
        if from_ is None:
            # OptOuts can't store a number it can't normalize; failing here would make the row
            # fail at every replay. There's nothing to opt out and no number to reply to.
            logger.warning(
                "goto-sms: %s from unusable number …%s; ignored", keyword, _last4(msg.from_)
            )
            return
        if keyword == "STOP":
            await self._opt_outs.add(msg.from_, "keyword")
        if keyword == "START":
            await self._opt_outs.remove(msg.from_)

        text = (
            self._replies["stop"]
            if keyword == "STOP"
            else self._replies["start"]
            if keyword == "START"
            else self._replies["help"]
        )
        try:
            # bypass_opt_out only for STOP (its confirmation goes to a number that just opted
            # out) and HELP (carriers require it answered even for opted-out numbers). START
            # must not bypass: if a STOP landed after the removal above, the START reply must
            # not go out.
            #
            # The dedupe key allows one reply per keyword per number per clock hour, so a flood
            # of keyword texts can't be used to make us send unlimited texts. It also covers a
            # replay after a crash. Trade-off: STOP->START->STOP within one hour gets only the
            # first STOP confirmation (and one START reply); the opt-out state is still applied
            # every time.
            hour = math.floor(self._now() / HOUR_MS)
            r = await self._sender.send(
                msg.from_,
                text,
                f"reply:{from_}:{keyword}:{hour}",
                bypass_opt_out=keyword != "START",
            )
            if r.status != "sent":
                logger.warning("goto-sms: %s reply not sent: %s", keyword, r.status)
        except Exception as err:  # noqa: BLE001 - a failed reply must not block processing
            logger.warning("goto-sms: %s reply failed (%s)", keyword, error_name(err))

    async def _over_rate_limit(self, from_: str) -> bool:
        """Counts this text against its number's current hour; True once over ``per_hour``."""
        key = f"{RATE_PREFIX}{from_}:{math.floor(self._now() / HOUR_MS)}"
        # Compare-and-set loop so two processes sharing a store can't lose an increment.
        while True:
            current = await self._store.get(self._tenant, key)
            count = (
                current if isinstance(current, int | float) and not isinstance(current, bool) else 0
            )
            if await self._store.compare_and_set(self._tenant, key, current, count + 1):
                return count + 1 > self._per_hour

    async def _log_inbound(self, msg: InboundMessage, status: str) -> None:
        try:
            await self._log.append(
                direction="inbound", from_=msg.from_, to=msg.to, status=status, goto_id=msg.id
            )
        except Exception as err:  # noqa: BLE001 - logging must never fail processing
            self._logger.warning("goto-sms: could not write the message log (%s)", error_name(err))


__all__ = ["InboundProcessor", "MessageHandler", "call_handlers"]
