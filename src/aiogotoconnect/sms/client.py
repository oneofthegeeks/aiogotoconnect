"""create_goto_sms: the public facade (port of Node client.ts). Wires config, OAuth, HTTP, sending,
opt-outs, the log, inbound processing and polling together over one store.

Node starts its work in the constructor and exposes `ready`; Python can't start async work in a
plain constructor, so `start()` does the same work (key, process lock, pending replay, poller) and
every method that needs it awaits it. `start()` is memoized: the lifespan's `async with sms`, the
deferred handlers' first request and any early `send()` share one startup.

This module never imports `cryptography` (the optional [sms] extra) at import time:
`create_goto_sms` checks for it first, and `secrets` is imported when a start runs.
"""

import asyncio
import contextlib
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import TracebackType
from typing import Literal, Self
from urllib.parse import urlsplit

import aiohttp

from aiogotoconnect.sms.config import ConfigResolver, config_value
from aiogotoconnect.sms.core import (
    DEFAULT_API_BASE,
    DEFAULT_AUTH_BASE,
    Authorize,
    BackLink,
    Core,
    ResolvedOptions,
)
from aiogotoconnect.sms.errors import (
    GoToApiError,
    GoToSmsError,
    NotConfiguredError,
    SendFailedError,
    TemporaryError,
)
from aiogotoconnect.sms.http import create_goto_http, error_name
from aiogotoconnect.sms.inbound.keywords import DEFAULT_REPLIES, KeywordReplies
from aiogotoconnect.sms.inbound.poller import Poller
from aiogotoconnect.sms.inbound.processor import InboundProcessor, MessageHandler, call_handlers
from aiogotoconnect.sms.log import MessageLog
from aiogotoconnect.sms.oauth import GoToAuth
from aiogotoconnect.sms.optouts import OptOuts, OptOutSource
from aiogotoconnect.sms.send import Sender
from aiogotoconnect.sms.server.status import StatusPayload, build_status, startup_error_text
from aiogotoconnect.sms.store.file import FileStore
from aiogotoconnect.sms.store.types import GoToSmsStore
from aiogotoconnect.sms.types import InboundMessage, SendResult

LOCK_KEY = "process-lock"
LOCK_TTL_MS = 90_000
# Module-level so tests can shorten it; read on every loop iteration.
LOCK_REFRESH_S = 30.0
MAX_BODY = 1600
ONE_PROCESS = "Another process is using this goto-sms store; run one process per store (see README)"
NO_CRYPTOGRAPHY = (
    "aiogotoconnect SMS needs the optional 'cryptography' package: pip install aiogotoconnect[sms]"
)
DEFAULT_LOGGER = "aiogotoconnect.sms"

_SCHEME = re.compile(r"^[a-z][a-z\d+.-]*:", re.IGNORECASE)
_HTTP_ABSOLUTE = re.compile(r"^https?://", re.IGNORECASE)
# Whitespace or control characters can hide a scheme from a human reader; never needed. Python's
# \s lacks U+FEFF, which JS's \s (Node's check) includes.
_NOT_PLAIN = re.compile(r"[\s﻿\x00-\x1f\x7f]")


def _require_cryptography() -> None:
    try:
        import cryptography  # noqa: F401
    except ImportError as exc:
        raise ImportError(NO_CRYPTOGRAPHY) from exc


def _silent_logger() -> logging.Logger:
    """For logger=False: an unregistered, disabled logger (never touches the logging tree, and
    its NullHandler keeps logging's lastResort from printing to stderr)."""
    # Not getLogger: a registered logger is shared, and disabling it would mute the host's too.
    logger = logging.Logger(f"{DEFAULT_LOGGER}.silent")  # noqa: LOG001
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.disabled = True
    return logger


def trim_trailing_slashes(s: str) -> str:
    """Drops trailing slashes with a loop, no regex (Node: `/\\/+$/` backtracks quadratically on
    long runs of `/` followed by another character)."""
    end = len(s)
    while end > 0 and s[end - 1] == "/":
        end -= 1
    return s[:end]


def _normalize_mount(p: str) -> str:
    """'/goto-sms/' -> '/goto-sms', 'sms' -> '/sms'; '/' and '' -> '' (the root mount)."""
    trimmed = trim_trailing_slashes(p)
    if trimmed == "":
        return ""
    return trimmed if trimmed.startswith("/") else f"/{trimmed}"


def _is_relative(v: str) -> bool:
    """No scheme (`http:evil.com` is relative to an http: parser base but absolute on an https
    page), and not protocol-relative: browsers read `\\` as `/` in http(s) URLs, so `//host`,
    `/\\host`, `\\\\host` and `\\/host` all leave the site."""
    if _SCHEME.match(v):
        return False
    return not (len(v) >= 2 and v[0] in "/\\" and v[1] in "/\\")


def _is_absolute_http(v: str) -> bool:
    """`http(s)://` followed by a host a browser would parse (WHATWG skips extra slashes after a
    special scheme and treats `\\` as `/`); `http://` and `http://[::1` don't parse."""
    if not _HTTP_ABSOLUTE.match(v):
        return False
    rest = v[v.index("//") + 2 :].lstrip("/\\")
    authority = re.split(r"[/\\?#]", rest, maxsplit=1)[0]
    try:
        parts = urlsplit(f"http://{authority}")
        _ = parts.port  # raises ValueError for a bad or out-of-range port
    except ValueError:
        return False
    return bool(parts.hostname)


def _link_option(name: str, raw: str | None) -> str | None:
    """A link option the page renders as an href (back_url) or the server redirects to
    (login_url): only an absolute http(s) URL or a same-origin relative path. Rejects
    `javascript:`, `data:` and the like, protocol-relative `//host` (or `/\\host`), scheme-relative
    `http:host`, and the literal `back` (Node refuses it: Express 4 redirects it to the Referer)."""
    v = config_value(raw)
    if v is None:
        return None
    plain = _NOT_PLAIN.search(v) is None
    magic = v.lower() == "back"
    if not plain or magic or not (_is_relative(v) or _is_absolute_http(v)):
        raise ValueError(f"goto-sms: {name} must be an http(s) URL or a relative path")
    return v


def _merge_replies(replies: Mapping[str, str] | None) -> KeywordReplies:
    merged = DEFAULT_REPLIES.copy()
    if replies:
        if "stop" in replies:
            merged["stop"] = replies["stop"]
        if "start" in replies:
            merged["start"] = replies["start"]
        if "help" in replies:
            merged["help"] = replies["help"]
    return merged


class GoToSmsOptOuts:
    """`sms.opt_outs`: the opt-out list, waiting for startup first."""

    def __init__(self, core: Callable[[], Awaitable[Core]]) -> None:
        self._core = core

    async def has(self, e164: str) -> bool:
        return await (await self._core()).opt_outs.has(e164)

    async def add(self, e164: str, source: OptOutSource) -> None:
        await (await self._core()).opt_outs.add(e164, source)

    async def remove(self, e164: str) -> None:
        await (await self._core()).opt_outs.remove(e164)

    async def list(self) -> list[dict[str, str]]:
        return await (await self._core()).opt_outs.list()


class GoToSms:
    """Send and receive GoTo Connect SMS. Make one with `create_goto_sms(...)`.

    Use it as an async context manager (`async with sms:` in a FastAPI lifespan), or call
    `await sms.start()` / `await sms.close()` yourself. Methods that need the store or GoTo start
    it on first use.
    """

    def __init__(
        self,
        options: ResolvedOptions,
        *,
        store: GoToSmsStore | None,
        tenant: str,
        logger: logging.Logger,
        encryption_key: str | None,
        credentials: Mapping[str, str | None],
    ) -> None:
        # Internal: Task 12's ASGI adapter reads _options and _logger and passes _ready_core to
        # create_deferred_handlers.
        self._options = options
        self._logger = logger
        self._store = store
        self._tenant = tenant
        self._encryption_key = encryption_key
        self._credentials = credentials
        self._given_session = options.session
        self._owned_session: aiohttp.ClientSession | None = None
        # An insertion-ordered set (Node uses a Set): registering a handler twice runs it once.
        self._listeners: dict[MessageHandler, None] = {}
        self._lock_owner = str(uuid.uuid4())
        self._core: Core | None = None
        self._closed = False
        self._start_task: asyncio.Task[Core] | None = None
        self._lock_task: asyncio.Task[None] | None = None
        self._lock_warned = False
        # The maintenance run in flight (lock refresh + pending retry); close() waits for it.
        self._maintaining: asyncio.Task[None] | None = None
        self.opt_outs = GoToSmsOptOuts(self._ready_core)

    # --- lifecycle ---

    async def start(self) -> None:
        """Resolves the encryption key, takes the process lock, replays pending inbound texts and
        starts the poller in polling mode (Node's `ready`). Idempotent and shared by concurrent
        callers. A startup error is cached and raised again (status() then reports it); a
        cancelled startup is not cached, so a later call retries."""
        await self._ready_core()

    async def _ready_core(self) -> Core:
        """The started Core (internal; the deferred handlers' `core` callable)."""
        task = self._start_task
        if task is None:
            task = asyncio.get_running_loop().create_task(self._start())
            task.add_done_callback(self._start_done)
            self._start_task = task
        # Shielded: one caller giving up must not cancel the startup the others are waiting on.
        return await asyncio.shield(task)

    def _start_done(self, task: "asyncio.Task[Core]") -> None:
        if task.cancelled():
            # Not cached: the next start() builds again.
            if self._start_task is task:
                self._start_task = None
            return
        err = task.exception()  # also marks it retrieved: no "never retrieved" warning
        if err is not None:
            # Package errors carry fixed, secret-free messages; anything else only by name.
            detail = str(err) if isinstance(err, GoToSmsError) else error_name(err)
            self._logger.error("goto-sms: startup failed (%s)", detail)

    async def _start(self) -> Core:
        try:
            return await self._build()
        except BaseException:
            # A failed start is cached and a cancelled one rebuilds everything, so the session it
            # made is never used again. self._core stays, as in Node, so close() can still let
            # go of a lock the start took.
            await self._close_owned_session()
            raise

    async def _build(self) -> Core:
        from aiogotoconnect.sms.secrets import SecretBox, resolve_encryption_key

        o = self._options
        logger, tenant, now = self._logger, self._tenant, o.now
        key, source = await resolve_encryption_key(self._encryption_key, o.env, o.data_dir, logger)
        box = SecretBox(key)
        store = self._store or FileStore(
            Path(o.data_dir or "./data") / "goto-sms.json", now, logger
        )
        session = self._given_session
        if session is None:
            session = self._owned_session = aiohttp.ClientSession()
        o.session = session
        config = ConfigResolver(store, tenant, box, self._credentials, o.env)
        auth = GoToAuth(
            store=store,
            tenant=tenant,
            box=box,
            config=config,
            session=session,
            auth_base_url=o.auth_base_url,
            # Without a public_url there is no valid redirect; the setup handlers report that.
            redirect_uri=o.redirect_uri or "",
            logger=logger,
            now=now,
        )
        http = create_goto_http(
            session, o.api_base_url, auth.get_access_token, auth.force_refresh, logger=logger
        )
        opt_outs = OptOuts(store, tenant, now)
        log = MessageLog(store, tenant, now, retention_days=o.log_retention_days)
        sender = Sender(
            http=http,
            auth=auth,
            config=config,
            opt_outs=opt_outs,
            log=log,
            store=store,
            tenant=tenant,
            now=now,
            send_timeout_s=o.send_timeout_s,
            max_body=MAX_BODY,
            log_only=o.log_only,
            env=o.env,
            logger=logger,
        )
        processor = InboundProcessor(
            store=store,
            tenant=tenant,
            sender=sender,
            opt_outs=opt_outs,
            log=log,
            emit=self._emit,
            replies=o.replies,
            yes_is_opt_in=o.yes_is_opt_in,
            per_hour=o.inbound_per_hour,
            now=now,
            logger=logger,
            seen_retention_days=o.log_retention_days,
        )
        poller = Poller(
            http=http,
            config=config,
            processor=processor,
            store=store,
            tenant=tenant,
            poll_interval_s=o.poll_interval_s,
            now=now,
            logger=logger,
        )
        c = Core(
            store=store,
            tenant=tenant,
            box=box,
            key_source=source,
            config=config,
            auth=auth,
            http=http,
            sender=sender,
            opt_outs=opt_outs,
            log=log,
            processor=processor,
            poller=poller,
            logger=logger,
            options=o,
        )
        self._core = c

        await self._claim_lock(c)
        # Finish anything persisted but not processed (a crash mid-STOP) before taking new texts.
        await c.processor.replay_pending()
        if self._closed:
            return c
        if self._lock_task is None:
            self._lock_task = asyncio.get_running_loop().create_task(self._lock_loop())
        if await c.incoming_mode() == "polling":
            c.poller.start()
        return c

    async def close(self) -> None:
        """Stops polling and the lock refresh, waits for in-flight work (a poll, post-204 inbound
        processing, maintenance) so nothing writes to the store afterwards, releases the process
        lock if it's still ours, and closes the aiohttp session if this instance created it.
        Idempotent; a no-op before start()."""
        self._closed = True
        task = self._start_task
        if task is not None and not task.done():
            await asyncio.wait([task])
        lock_task, self._lock_task = self._lock_task, None
        if lock_task is not None:
            lock_task.cancel()
            await asyncio.wait([lock_task])
        c = self._core
        if c is not None:
            c.poller.stop()
            await c.poller.idle()
            if c.inflight:
                await asyncio.gather(*list(c.inflight), return_exceptions=True)
            if self._maintaining is not None:
                await asyncio.wait([self._maintaining])
            # Release the lock only if it's still ours.
            lock = await c.store.get(c.tenant, LOCK_KEY)
            if isinstance(lock, dict) and lock.get("owner") == self._lock_owner:
                await c.store.delete(c.tenant, LOCK_KEY)
        await self._close_owned_session()

    async def __aenter__(self) -> Self:
        """Starts; a startup failure is logged and reported by status() and the setup page rather
        than raised, so the host app still boots and the page can show why (as in Node, where a
        host that never awaits `ready` keeps running). CancelledError still propagates."""
        # Already logged once by _start_done; status() and the page say why.
        with contextlib.suppress(Exception):
            await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def _close_owned_session(self) -> None:
        session, self._owned_session = self._owned_session, None
        if session is not None:
            await session.close()

    # --- process lock and maintenance ---

    async def _claim_lock(self, c: Core) -> None:
        won = await c.store.claim(c.tenant, LOCK_KEY, LOCK_TTL_MS, self._lock_owner)
        if won:
            self._lock_warned = False
        elif not self._lock_warned:
            self._lock_warned = True
            self._logger.warning(ONE_PROCESS)

    async def _lock_loop(self) -> None:
        """Node's 30 s setInterval: each tick starts a maintenance run (not awaited, so close()
        cancelling this loop never interrupts a run; close() waits for it instead)."""
        while True:
            await asyncio.sleep(LOCK_REFRESH_S)
            if self._core is not None:
                self._maintain(self._core)

    def _maintain(self, c: Core) -> "asyncio.Task[None]":
        """Refreshes the process lock and retries inbound rows a failed process() left pending.
        While polling, each tick already replays pending rows, so this skips the replay then to
        keep one replay running at a time. One run at a time."""
        if self._maintaining is None:
            task = asyncio.get_running_loop().create_task(self._maintenance_run(c))

            def done(t: "asyncio.Task[None]") -> None:
                if self._maintaining is t:
                    self._maintaining = None

            task.add_done_callback(done)
            self._maintaining = task
        return self._maintaining

    async def _maintenance_run(self, c: Core) -> None:
        try:
            await self._claim_lock(c)
            if not c.poller.running:
                await c.processor.replay_pending()
        except Exception as err:  # noqa: BLE001 - retried on the next tick; never kills the loop
            self._logger.warning("goto-sms: background maintenance failed (%s)", error_name(err))

    # --- messages ---

    async def _emit(self, m: InboundMessage) -> None:
        # A snapshot, so a handler that calls on() doesn't get this same message. call_handlers
        # logs a raising handler (sync or async) and keeps going with the rest.
        await call_handlers(list(self._listeners), m, self._logger)

    def on(self, event: Literal["message"], fn: MessageHandler) -> Self:
        """Registers an inbound-text handler (sync or async). Returns self, for chaining."""
        self._check_event(event)
        self._listeners[fn] = None
        return self

    def off(self, event: Literal["message"], fn: MessageHandler) -> Self:
        self._check_event(event)
        self._listeners.pop(fn, None)
        return self

    @staticmethod
    def _check_event(event: str) -> None:
        if event != "message":
            raise ValueError(f"goto-sms: unknown event {event!r}; the only event is 'message'")

    async def _core_or_result(self) -> Core | SendResult:
        """The core, or a not_connected result when startup failed for lack of configuration."""
        try:
            return await self._ready_core()
        except NotConfiguredError as err:
            return SendResult(status="not_connected", reason=str(err))

    async def send(self, to: str, text: str, dedupe_key: str | None = None) -> SendResult:
        c = await self._core_or_result()
        if isinstance(c, SendResult):
            return c
        # Only dedupe_key is forwarded: the sender's bypass_opt_out is internal.
        return await c.sender.send(to, text, dedupe_key)

    async def send_or_raise(self, to: str, text: str, dedupe_key: str | None = None) -> SendResult:
        c = await self._core_or_result()
        if isinstance(c, SendResult):
            raise SendFailedError(c)
        return await c.sender.send_or_raise(to, text, dedupe_key)

    # --- status ---

    async def status(self) -> StatusPayload:
        """The setup page's status: the GET /api/status payload without `incoming.webhookUrl`
        (it holds the webhook secret, shown only behind `authorize`). Never raises for missing
        configuration: if startup failed for lack of an encryption key it reports not set up,
        with `startupError` saying why."""
        try:
            c = await self._ready_core()
        except NotConfiguredError as err:
            return await build_status(self._options, None, startup_error_text(err))
        return await build_status(self._options, c)

    async def verify_connection(self) -> bool:
        """Checks the GoTo login now by getting a new access token (exchanging a
        GOTO_REFRESH_TOKEN seed, or refreshing), then returns `status()["connected"]`. Never
        raises for login or configuration problems: it returns False and status() says why. Each
        call makes one GoTo token request."""
        try:
            c = await self._ready_core()
        except NotConfiguredError:
            return False
        try:
            await c.auth.verify()
        except GoToSmsError as err:
            # Login, configuration and GoTo errors all land in status(); anything else is a bug.
            c.logger.debug("goto-sms: verify_connection failed (%s)", err.code)
            # GoTo unreachable or erroring: the stored login may be fine, but it wasn't verified,
            # so a health check must not report it as working.
            if isinstance(err, TemporaryError | GoToApiError):
                return False
        return (await build_status(self._options, c))["connected"]

    # --- web apps (Task 12) ---

    def asgi_app(self) -> object:
        raise NotImplementedError("asgi_app() lands with the ASGI adapter")

    def webhook_app(self) -> object:
        raise NotImplementedError("webhook_app() lands with the ASGI adapter")

    @property
    def core(self) -> Core:
        """Internal, for the package's own handlers; may change in any release. Raises until
        start() has built it."""
        if self._core is None:
            raise GoToSmsError("goto-sms is not ready yet; await sms.start() first", "not_ready")
        return self._core


def create_goto_sms(
    *,
    public_url: str | None = None,
    mount_path: str = "/goto-sms",
    data_dir: str | os.PathLike[str] | None = None,
    store: GoToSmsStore | None = None,
    tenant: str = "default",
    authorize: Authorize | None = None,
    allow_unauthenticated_setup: bool = False,
    back_url: str | None = None,
    back_label: str | None = None,
    login_url: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    account_key: str | None = None,
    from_number: str | None = None,
    refresh_token: str | None = None,
    encryption_key: str | None = None,
    log_only: bool = False,
    replies: Mapping[str, str] | None = None,
    yes_is_opt_in: bool = True,
    poll_interval: float = 30.0,
    send_timeout: float = 10.0,
    inbound_per_hour: int = 10,
    log_retention_days: float = 90,
    logger: logging.Logger | Literal[False] | None = None,
    session: aiohttp.ClientSession | None = None,
    auth_base_url: str = DEFAULT_AUTH_BASE,
    api_base_url: str = DEFAULT_API_BASE,
    env: Mapping[str, str] | None = None,
    now: Callable[[], float] | None = None,
) -> GoToSms:
    """Creates the facade. Validation runs here, synchronously (link options, the production
    guards); the async work waits for start(). Intervals are seconds; `now` returns epoch ms."""
    _require_cryptography()
    env = os.environ if env is None else env
    log = _silent_logger() if logger is False else (logger or logging.getLogger(DEFAULT_LOGGER))
    public_url_raw = config_value(public_url) or config_value(env.get("GOTO_SMS_PUBLIC_URL"))
    resolved_url = trim_trailing_slashes(public_url_raw) if public_url_raw else None
    resolved_url = resolved_url or None  # "///" trims to "": unset
    mount = _normalize_mount(mount_path)
    back = _link_option("back_url", back_url)
    login = _link_option("login_url", login_url)
    production = env.get("GOTO_SMS_ENV") == "production"
    if log_only and production:
        raise GoToSmsError(
            "log_only mode would silently drop every text, so it is refused when "
            "GOTO_SMS_ENV=production",
            "log_only_in_production",
        )
    if allow_unauthenticated_setup and production:
        raise GoToSmsError(
            "allow_unauthenticated_setup=True is a development-only escape hatch, so it is "
            "refused when GOTO_SMS_ENV=production",
            "unauthenticated_setup_in_production",
        )
    # A custom store and no data_dir means there is nowhere for a keyfile: the key must then
    # come from the option or env (resolve_encryption_key raises NotConfiguredError).
    resolved_dir = os.fspath(data_dir) if data_dir is not None else (None if store else "./data")
    options = ResolvedOptions(
        public_url=resolved_url,
        mount_path=mount,
        redirect_uri=f"{resolved_url}{mount}/callback" if resolved_url else None,
        data_dir=resolved_dir,
        authorize=authorize,
        allow_unauthenticated_setup=allow_unauthenticated_setup,
        back=BackLink(back, config_value(back_label) or "the app") if back else None,
        login_url=login,
        log_only=log_only,
        replies=_merge_replies(replies),
        yes_is_opt_in=yes_is_opt_in,
        poll_interval_s=poll_interval,
        send_timeout_s=send_timeout,
        inbound_per_hour=inbound_per_hour,
        log_retention_days=log_retention_days,
        session=session,
        auth_base_url=auth_base_url,
        api_base_url=api_base_url,
        env=env,
    )
    if now is not None:
        options.now = now
    credentials: dict[str, str | None] = {
        "clientId": client_id,
        "clientSecret": client_secret,
        "accountKey": account_key,
        "fromNumber": from_number,
        "refreshToken": refresh_token,
    }
    return GoToSms(
        options,
        store=store,
        tenant=tenant,
        logger=log,
        encryption_key=encryption_key,
        credentials={k: v for k, v in credentials.items() if v is not None},
    )


__all__ = ["GoToSms", "GoToSmsOptOuts", "create_goto_sms", "trim_trailing_slashes"]
