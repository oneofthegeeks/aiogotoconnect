"""Sends texts (port of Node send.ts).

`Sender.send()` returns a result for every expected outcome and never raises for them. It never
reports "sent" for an unsent text, never texts an opted-out number (except an explicit bypass for
the STOP confirmation), and never double-sends a dedupe key.
"""

import asyncio
import dataclasses
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from aiogotoconnect.sms.config import ConfigResolver
from aiogotoconnect.sms.errors import (
    GoToApiError,
    GoToSmsError,
    LoginExpiredError,
    NotConfiguredError,
    SendFailedError,
    TemporaryError,
)
from aiogotoconnect.sms.http import GoToHttp, error_name
from aiogotoconnect.sms.log import PRUNE_EVERY, MessageLog
from aiogotoconnect.sms.oauth import GoToAuth
from aiogotoconnect.sms.optouts import OptOuts
from aiogotoconnect.sms.phone import normalize_phone
from aiogotoconnect.sms.store.types import GoToSmsStore, Json
from aiogotoconnect.sms.types import SendResult, SendStatus

SEND_PATH = "/messaging/v1/messages"
DAY_MS = 86_400_000
LOCK_TTL_MS = 60_000
DEDUPE_PREFIX = "dedupe:"
LOCK_PREFIX = "dedupe-lock:"


class _Reasons:
    """Node's `R` table, verbatim."""

    invalid_number = "Not a valid phone number"
    no_client = "GoTo client ID and secret aren't set"
    unreadable = (
        "Saved GoTo credentials can't be decrypted — re-enter the client secret and reconnect"
    )
    bad_from = "The configured sending number isn't a valid phone number"
    no_from = "No sending number chosen"
    not_connected = "Not connected to GoTo — open the setup page and click Connect"
    login_expired = "GoTo login expired — reconnect"
    self_ = "GoTo can't text the sending number itself"
    opted_out = "This number replied STOP and has opted out of texts"
    empty = "Message text is empty"
    log_only = "log-only mode"
    locked = "A send with this dedupeKey is already in progress"
    timeout = "GoTo didn't answer in time — the text may or may not have been sent; not retried"
    unreadable_answer = (
        "GoTo accepted the request but its answer couldn't be read — the text may or may not "
        "have been sent; not retried"
    )
    no_token = "Couldn't get a GoTo access token — the text was not sent; try again"
    forbidden = (
        "GoTo refused (403): the login lacks messaging.v1.send, or this number isn't "
        "SMS-enabled in GoTo Admin"
    )
    ten_dlc = "This number isn't registered to a 10DLC texting campaign"


R = _Reasons
_TEN_DLC = re.compile(r"10dlc|campaign", re.IGNORECASE)


def send_result_to_json(r: SendResult) -> dict[str, Json]:
    """The stored and wire form of a SendResult: Node's camelCase, unset fields omitted."""
    out: dict[str, Json] = {"status": r.status}
    if r.reason is not None:
        out["reason"] = r.reason
    if r.retryable is not None:
        out["retryable"] = r.retryable
    if r.message_id is not None:
        out["messageId"] = r.message_id
    if r.to is not None:
        out["to"] = r.to
    if r.deduped is not None:
        out["deduped"] = r.deduped
    return out


def _send_result_from_json(v: dict[str, Json]) -> SendResult:
    def string(k: str) -> str | None:
        x = v.get(k)
        return x if isinstance(x, str) else None

    def flag(k: str) -> bool | None:
        x = v.get(k)
        return x if isinstance(x, bool) else None

    return SendResult(
        status=cast(SendStatus, v["status"]),
        reason=string("reason"),
        retryable=flag("retryable"),
        message_id=string("messageId"),
        to=string("to"),
        deduped=flag("deduped"),
    )


def _goto_message(body: str) -> str | None:
    """GoTo's `message` field from a JSON error body, cut to 200 chars; never the raw body."""
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return None
    m = parsed.get("message") if isinstance(parsed, dict) else None
    return m.strip()[:200] if isinstance(m, str) and m.strip() else None


def _translate(err: Exception) -> SendResult:
    """Maps a failed POST (or token fetch) to a result. Unexpected errors are re-raised."""
    if isinstance(err, LoginExpiredError):
        return SendResult(status="not_connected", reason=R.login_expired)
    # NotConfiguredError messages are fixed, secret-free texts from oauth.py.
    if isinstance(err, NotConfiguredError):
        return SendResult(status="not_connected", reason=str(err))
    if isinstance(err, TemporaryError):
        # After the request left, GoTo may have sent it: never report rejected (it invites a
        # retry).
        if err.request_sent:
            return SendResult(status="uncertain", reason=R.timeout)
        return SendResult(status="rejected", reason=R.no_token, retryable=True)
    if isinstance(err, GoToApiError):
        status, body = err.status, err.body
        # The token endpoint failed before the POST left, so the text was not sent. A garbled
        # 2xx there is likely transient; its other answers are login/config problems.
        if err.token_endpoint:
            if 200 <= status < 300:
                return SendResult(status="rejected", reason=R.no_token, retryable=True)
            return SendResult(
                status="rejected",
                reason=f"GoTo's login service answered HTTP {status} — the text was not sent",
            )
        # Any other 2xx error (e.g. an unparseable body): the send endpoint accepted the
        # request and we can't prove the text didn't go out.
        if 200 <= status < 300:
            return SendResult(status="uncertain", reason=R.unreadable_answer)
        # GoTo reports 10DLC problems under several statuses, so the body is checked first.
        if _TEN_DLC.search(body):
            return SendResult(status="rejected", reason=R.ten_dlc)
        if status == 403:
            return SendResult(status="rejected", reason=R.forbidden)
        # 408 is the server's definitive "not processed", like 429/5xx: safe to retry.
        if status in (408, 429) or status >= 500:
            return SendResult(
                status="rejected",
                reason=f"GoTo is busy or unavailable (HTTP {status}) — the text was not sent; "
                "try again later",
                retryable=True,
            )
        message = _goto_message(body)
        return SendResult(
            status="rejected",
            reason=f"GoTo rejected the text (HTTP {status})" + (f": {message}" if message else ""),
        )
    raise err


@dataclass(frozen=True)
class _Pass:
    to: str
    from_: str


@dataclass(frozen=True)
class _Refuse:
    result: SendResult
    from_: str
    log_to: str


class Sender:
    """Options as Node's SenderOptions; `send_timeout_s` is seconds (Node's timeoutMs / 1000)."""

    def __init__(
        self,
        *,
        http: GoToHttp,
        auth: GoToAuth,
        config: ConfigResolver,
        opt_outs: OptOuts,
        log: MessageLog,
        store: GoToSmsStore,
        tenant: str,
        now: Callable[[], float],
        send_timeout_s: float,
        max_body: int,
        log_only: bool,
        env: Mapping[str, str],
        logger: logging.Logger,
    ) -> None:
        if log_only and env.get("GOTO_SMS_ENV") == "production":
            raise GoToSmsError(
                "log_only mode would silently drop every text, so it is refused when "
                "GOTO_SMS_ENV=production",
                "log_only_in_production",
            )
        self._http = http
        self._auth = auth
        self._config = config
        self._opt_outs = opt_outs
        self._log = log
        self._store = store
        self._tenant = tenant
        self._now = now
        self._send_timeout_s = send_timeout_s
        self._max_body = max_body
        self._log_only = log_only
        self._logger = logger
        # In-process sends in flight per dedupe key; concurrent callers share one task.
        self._in_flight: dict[str, asyncio.Task[SendResult]] = {}
        # Sends recorded by this instance; drives the periodic prune_dedupe().
        self._recorded = 0

    async def send(
        self, to: str, text: str, dedupe_key: str | None = None, *, bypass_opt_out: bool = False
    ) -> SendResult:
        """`bypass_opt_out` is internal (the STOP confirmation); the facade never exposes it."""
        gate = await self._gate(to, text, bypass_opt_out)
        if isinstance(gate, _Refuse):
            return await self._record(gate.result, gate.from_, gate.log_to)

        if dedupe_key is None:
            return await self._deliver(gate.to, gate.from_, text)
        key, to_e164, from_ = dedupe_key, gate.to, gate.from_

        # No await between get and set: the check-and-register is atomic on the event loop.
        running = self._in_flight.get(key)
        if running is not None:
            return self._deduped(await asyncio.shield(running))

        async def run() -> SendResult:
            try:
                return await self._deduped_send(key, to_e164, from_, text)
            finally:
                # Cleared inside the task, before its waiters resume (Node's .finally()).
                if self._in_flight.get(key) is task:
                    del self._in_flight[key]

        task = asyncio.ensure_future(run())
        # Mark the outcome retrieved even if every waiter was cancelled.
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        self._in_flight[key] = task
        # Shielded: a cancelled caller must not cancel a POST in flight, or its dedupe record
        # would never be written and a retry would send a second copy.
        return await asyncio.shield(task)

    async def send_or_raise(self, to: str, text: str, dedupe_key: str | None = None) -> SendResult:
        # Forwards only dedupe_key: bypass_opt_out is not reachable through this method.
        result = await self.send(to, text, dedupe_key)
        if result.status != "sent":
            raise SendFailedError(result)
        return result

    async def prune_dedupe(self) -> None:
        """Removes `dedupe:*` records older than the message log's retention."""
        cutoff = self._now() - self._log.retention_days * DAY_MS
        for key, value in await self._store.list(self._tenant, DEDUPE_PREFIX):
            at = value.get("at") if isinstance(value, dict) else 0
            if not isinstance(at, int | float) or isinstance(at, bool) or at < cutoff:
                await self._store.delete(self._tenant, key)

    # --- internals ---

    async def _gate(self, raw_to: str, text: str, bypass_opt_out: bool) -> _Pass | _Refuse:
        """Every check that needs no network call, in the documented order."""
        c = await self._config.resolve()
        from_ = c.from_number or ""
        to = normalize_phone(raw_to)

        def fail(result: SendResult) -> _Refuse:
            return _Refuse(
                result=result if to is None else dataclasses.replace(result, to=to),
                from_=from_,
                # An invalid input is logged as given (trimmed and capped): it has no E.164 form.
                log_to=to if to is not None else raw_to.strip()[:32],
            )

        if to is None:
            return fail(SendResult(status="invalid_number", reason=R.invalid_number))

        # log_only needs only a sending number, so it can be tried before connecting to GoTo.
        if not self._log_only:
            # Unreadable leaves client_secret unset, so it's checked before "missing".
            if c.credentials_unreadable:
                return fail(SendResult(status="not_connected", reason=R.unreadable))
            if not c.client_id or not c.client_secret:
                return fail(SendResult(status="not_connected", reason=R.no_client))
        if c.invalid_from_number:
            return fail(SendResult(status="not_connected", reason=R.bad_from))
        if not c.from_number:
            return fail(SendResult(status="not_connected", reason=R.no_from))
        if not self._log_only:
            s = await self._auth.status()
            # With GOTO_REFRESH_TOKEN set, the first token call seeds the login, so only refuse
            # up front when there's no seed; otherwise the token errors decide (_translate()).
            if not s.connected and not self._config.seed_refresh_token():
                reason = R.login_expired if s.login_expired else R.not_connected
                return fail(SendResult(status="not_connected", reason=reason))

        if to == c.from_number:
            return fail(SendResult(status="rejected", reason=R.self_))
        if not bypass_opt_out and await self._opt_outs.has(to):
            return fail(SendResult(status="opted_out", reason=R.opted_out))
        if text.strip() == "":
            return fail(SendResult(status="rejected", reason=R.empty))
        # Python's len() counts code points, as Node's [...text].length does: an emoji is one
        # character to the person typing it.
        length = len(text)
        if length > self._max_body:
            return fail(
                SendResult(
                    status="rejected",
                    reason=f"Message is {length} characters; the limit is {self._max_body} "
                    "(not truncated)",
                )
            )
        return _Pass(to=to, from_=c.from_number)

    async def _deduped_send(self, key: str, to: str, from_: str, text: str) -> SendResult:
        earlier = await self._stored_result(key)
        if earlier is not None:
            return self._deduped(earlier)

        # Cross-process seam: a unique owner per call, so two Senders sharing a store can't
        # both hold the lock (claim succeeds again for the same owner).
        lock_key = LOCK_PREFIX + key
        owner = os.urandom(8).hex()
        if not await self._store.claim(self._tenant, lock_key, LOCK_TTL_MS, owner):
            refused = SendResult(status="rejected", reason=R.locked, retryable=True, to=to)
            return await self._record(refused, from_, to)
        try:
            # Another holder may have finished between our first read and the claim.
            just_sent = await self._stored_result(key)
            if just_sent is not None:
                return self._deduped(just_sent)
            return await self._deliver(to, from_, text, key)
        finally:
            try:
                await self._store.delete(self._tenant, lock_key)
            except Exception as err:  # noqa: BLE001 - a failed release must not fail the send
                self._logger.warning(
                    "goto-sms: could not release dedupe lock (%s)", error_name(err)
                )

    async def _deliver(
        self, to: str, from_: str, text: str, dedupe_key: str | None = None
    ) -> SendResult:
        """The network step (skipped in log_only), then the dedupe record and the log entry."""
        if self._log_only:
            result = SendResult(status="sent", reason=R.log_only, to=to)
        else:
            result = await self._post(to, from_, text)

        # Nothing after the POST may raise: an exception here would look like a failed send
        # and invite a retry, i.e. a double send. Store failures are logged instead.
        # log_only never persists: a "sent" that wasn't would be replayed as deduped once
        # log_only is turned off on the same store. (The in-flight map still dedupes.)
        if dedupe_key is not None and not self._log_only and result.status in ("sent", "uncertain"):
            try:
                await self._store.set(
                    self._tenant,
                    DEDUPE_PREFIX + dedupe_key,
                    {"result": send_result_to_json(result), "at": self._now()},
                )
            except Exception as err:  # noqa: BLE001 - see above
                self._logger.warning(
                    "goto-sms: could not store dedupe result (%s)", error_name(err)
                )
        return await self._record(result, from_, to)

    async def _post(self, to: str, from_: str, text: str) -> SendResult:
        try:
            res: Any = await self._http.json(
                "POST",
                SEND_PATH,
                {"ownerPhoneNumber": from_, "contactPhoneNumbers": [to], "body": text},
                timeout_s=self._send_timeout_s,
            )
        except Exception as err:  # noqa: BLE001 - _translate re-raises what it doesn't know
            return dataclasses.replace(_translate(err), to=to)
        message_id = res.get("id") if isinstance(res, dict) else None
        return SendResult(
            status="sent", to=to, message_id=message_id if isinstance(message_id, str) else None
        )

    async def _record(self, result: SendResult, from_: str, to: str) -> SendResult:
        """Appends to the message log (no body) and writes one info line (last 4 digits only)."""
        try:
            await self._log.append(
                direction="outbound",
                from_=from_,
                to=to,
                status=result.status,
                reason=result.reason,
                goto_id=result.message_id,
            )
        except Exception as err:  # noqa: BLE001 - logging must never fail a send
            self._logger.warning("goto-sms: could not write the message log (%s)", error_name(err))
        self._info_line(result, to)
        # Keep dedupe records bounded on the same every-Nth cadence as the log's own prune.
        self._recorded += 1
        if self._recorded % PRUNE_EVERY == 0:
            try:
                await self.prune_dedupe()
            except Exception as err:  # noqa: BLE001
                self._logger.warning(
                    "goto-sms: could not prune dedupe records (%s)", error_name(err)
                )
        return result

    def _deduped(self, earlier: SendResult) -> SendResult:
        result = dataclasses.replace(earlier, deduped=True)
        self._info_line(result, result.to or "")
        return result

    def _info_line(self, result: SendResult, to: str) -> None:
        last4 = re.sub(r"[^0-9]", "", to)[-4:]
        self._logger.info(
            "goto-sms: send to …%s: %s%s",
            last4,
            result.status,
            " (deduped)" if result.deduped else "",
        )

    async def _stored_result(self, key: str) -> SendResult | None:
        v = await self._store.get(self._tenant, DEDUPE_PREFIX + key)
        r = v.get("result") if isinstance(v, dict) else None
        if not isinstance(r, dict) or r.get("status") not in ("sent", "uncertain"):
            return None
        return _send_result_from_json(r)


__all__ = ["Sender", "send_result_to_json"]
