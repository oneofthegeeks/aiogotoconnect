"""Framework-free HTTP handlers for the setup page, its JSON API, the OAuth callback and the GoTo
webhook (port of Node server/handlers.ts). A thin adapter (the ASGI app) maps its requests and
responses onto these. contract/http.md in the goto-sms repo is the language-neutral write-up.

Adapter duties that stay out of here, as in Node's express.ts: body parsing (a missing body is
None; 413 for over 100 kB, 400 for non-JSON), the mount-without-trailing-slash redirect, and the
response headers (Cache-Control, X-Frame-Options, CSP). HEAD is handled here: `HEAD /callback`
is a side-effect-free 200 and every other HEAD is answered as GET (the adapter drops the body).
"""

import asyncio
import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from aiogotoconnect.sms.core import AuthorizeRequest, Core, IncomingMode, ResolvedOptions
from aiogotoconnect.sms.directory import GoToAccount, GoToNumber, list_accounts, list_numbers
from aiogotoconnect.sms.errors import GoToApiError, GoToSmsError, TemporaryError
from aiogotoconnect.sms.http import error_name
from aiogotoconnect.sms.inbound.webhook import (
    check_webhook_secret,
    disable_webhook,
    enable_webhook,
    ensure_webhook_secret,
    get_webhook_account,
    get_webhook_secret,
    parse_webhook_envelope,
)
from aiogotoconnect.sms.log import log_entry_to_json
from aiogotoconnect.sms.send import send_result_to_json
from aiogotoconnect.sms.server.status import (
    INBOUND_ERROR,
    REQUIRED_SCOPES,
    IncomingError,
    StatusPayload,
    build_status,
    startup_error_text,
)
from aiogotoconnect.sms.store.types import Json

__all__ = [
    "REQUIRED_SCOPES",
    "AuthorizeRequest",
    "Handler",
    "HttpRequest",
    "HttpResponse",
    "IncomingError",
    "StatusPayload",
    "assert_setup_auth",
    "build_status",
    "create_deferred_handlers",
    "create_handlers",
    "startup_error_text",
]


@dataclass(frozen=True)
class HttpRequest:
    method: str
    # Relative to the mount, e.g. "/api/status".
    path: str
    query: Mapping[str, str] = field(default_factory=dict)
    # Lower-case header names.
    headers: Mapping[str, str] = field(default_factory=dict)
    cookies: Mapping[str, str] = field(default_factory=dict)
    # The parsed JSON body (None when there was none).
    body: object = None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    json: Any = None
    html: str | None = None
    redirect: str | None = None


Handler = Callable[[HttpRequest], Awaitable[HttpResponse]]

NO_AUTHORIZE = (
    "goto-sms: pass authorize(req) to create_goto_sms "
    "(or allow_unauthenticated_setup=True in development)"
)
TEST_TEXT = "Test message from goto-sms"
LOG_DEFAULT = 50
LOG_MAX = 500
_MODES: dict[str, IncomingMode] = {"webhook": "webhook", "polling": "polling", "off": "off"}

# Package error codes that mean "the request can't be done as asked", not a server fault.
BAD_REQUEST = frozenset(
    {
        "invalid_input",
        "invalid_number",
        "invalid_inbound",
        "not_configured",
        "login_expired",
        "bad_state",
        "no_public_url",
    }
)

# encodeURIComponent leaves these unescaped.
_URI_COMPONENT_SAFE = "-_.!~*'()"
_INBOUND = re.compile(r"/inbound/([^/]+)")
# JS parseInt(s, 10): optional leading whitespace and sign, then the leading digits.
_LEADING_INT = re.compile(r"\s*([+-]?\d+)")


def _bad_input(message: str) -> GoToSmsError:
    return GoToSmsError(message, "invalid_input")


def _as_object(body: object) -> dict[str, Any]:
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise _bad_input("Expected a JSON object")
    return body


def _opt_string(o: Mapping[str, Any], key: str) -> str | None:
    if key not in o:
        return None
    v = o[key]
    if not isinstance(v, str):  # JSON null included, as in Node (typeof null is "object")
        raise _bad_input(f"{key} must be a string")
    return v


def _req_string(o: Mapping[str, Any], key: str) -> str:
    v = _opt_string(o, key)
    if not v:
        raise _bad_input(f"{key} is required")
    return v


def _parse_int(s: str | None) -> int | None:
    m = _LEADING_INT.match(s or "")
    return int(m.group(1)) if m else None


_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21}


def _host_of(url: str | None) -> str | None:
    """`new URL(url).host`: hostname plus any non-default port; None if unparseable."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not parts.scheme or not hostname:
        return None  # e.g. `Origin: null`
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if port is None or _DEFAULT_PORTS.get(parts.scheme.lower()) == port:
        return hostname
    return f"{hostname}:{port}"


def _same_origin(req: HttpRequest, public_url: str | None) -> bool:
    """CSRF guard for state-changing routes. `Sec-Fetch-Site: same-origin` passes on its own;
    otherwise Origin's host must equal the Host header or, behind a proxy that rewrites Host,
    the public_url's host."""
    if req.headers.get("sec-fetch-site") == "same-origin":
        return True
    origin = _host_of(req.headers.get("origin"))
    if not origin:
        return False
    return origin == req.headers.get("host") or origin == _host_of(public_url)


def _error_response(logger: logging.Logger, route: str, err: Exception) -> HttpResponse:
    """One error → response mapping for every route. Details go to the debug log only."""
    code = err.code if isinstance(err, GoToSmsError) else error_name(err)
    logger.debug("goto-sms: setup request failed (%s: %s)", route, code)
    if isinstance(err, GoToApiError):
        return HttpResponse(
            502,
            {
                "error": "goto_api",
                "message": str(err),
                "gotoStatus": err.status,
                "gotoBody": err.body,
            },
        )
    if isinstance(err, TemporaryError):
        return HttpResponse(
            503, {"error": "temporary", "message": "GoTo did not answer in time; try again"}
        )
    if isinstance(err, GoToSmsError) and err.code == "locked":
        return HttpResponse(409, {"error": "locked", "message": str(err)})
    if isinstance(err, GoToSmsError) and err.code in BAD_REQUEST:
        return HttpResponse(400, {"error": err.code, "message": str(err)})
    return HttpResponse(
        500, {"error": "internal", "message": "Something went wrong; see the server logs"}
    )


def _account_json(a: GoToAccount) -> dict[str, Json]:
    return {"key": a.key, "name": a.name}


def _number_json(n: GoToNumber) -> dict[str, Json]:
    out: dict[str, Json] = {"number": n.number, "name": n.name, "accountKey": n.account_key}
    if n.sms_enabled is not None:
        out["smsEnabled"] = n.sms_enabled
    return out


async def _subscribe_webhook(core: Core) -> None:
    """Subscribes the webhook on the selected account (channel and subscription reused if they
    match)."""
    store, tenant, http = core.store, core.tenant, core.http
    if not (core.options.public_url or "").startswith("https://"):
        raise GoToSmsError(
            "Webhooks need an https public_url; use polling instead", "no_public_url"
        )
    account_key = (await core.config.resolve()).account_key
    if not account_key:
        raise GoToSmsError("Pick a GoTo account before turning on webhooks", "not_configured")
    # A subscription still stored for another account (a move whose DELETE failed) would keep
    # delivering once we subscribe on this one, so it goes first. If this DELETE fails too, it
    # raises before anything new is created and the caller's error state stays visible.
    stored_account = await get_webhook_account(store, tenant)
    if stored_account and stored_account != account_key:
        await disable_webhook(http, store, tenant, rotate_secret=False)
    # The URL embeds the secret, so it has to exist before GoTo is told the URL.
    secret = await ensure_webhook_secret(store, tenant)
    webhook_url = core.webhook_url(secret)
    assert webhook_url is not None  # public_url is https, checked above
    await enable_webhook(
        http=http,
        store=store,
        tenant=tenant,
        account_key=account_key,
        webhook_url=webhook_url,
        channel_prefix="goto-sms",
    )


async def _set_incoming(core: Core, mode: IncomingMode) -> None:
    """Switches inbound delivery. The stored mode changes only once the switch succeeded."""
    store, tenant, http, poller = core.store, core.tenant, core.http, core.poller
    if mode == "webhook":
        await _subscribe_webhook(core)
        await store.set(tenant, "inbound:mode", mode)
        await store.delete(tenant, INBOUND_ERROR)
        poller.stop()
        return
    # Leaving webhook mode deletes the subscription and rotates the secret, so the old URL
    # stops authenticating. A failed DELETE raises here and the mode stays as it was.
    from_webhook = await core.incoming_mode() == "webhook"
    if from_webhook:
        await disable_webhook(http, store, tenant)
    # R31: the webhook covered the time since polling last ran, so polling starts from now
    # (re-reading that span would replay texts the webhook already handled). Coming from off,
    # the cursor is kept: nothing handled texts then, and the poller caps the lookback at 24 h.
    # Set before start(), which polls straight away.
    if mode == "polling" and from_webhook:
        await poller.reset_cursor()
    await store.set(tenant, "inbound:mode", mode)
    await store.delete(tenant, INBOUND_ERROR)
    if mode == "polling":
        poller.start()
    else:
        poller.stop()


async def _follow_account(core: Core) -> None:
    """R29: the subscription is per account, so when the selected account changes in webhook
    mode it moves: delete the old one (keeping the secret, so the URL and channel stay),
    subscribe on the new account. A failure keeps the selection and is stored as
    `inbound:error`, which the status shows (code and GoTo's HTTP status only) until a later
    switch succeeds."""
    store, tenant, logger = core.store, core.tenant, core.logger
    if await core.incoming_mode() != "webhook":
        return
    account_key = (await core.config.resolve()).account_key
    if account_key == await get_webhook_account(store, tenant):
        return
    try:
        await _subscribe_webhook(core)  # deletes the old account's subscription first
        await store.delete(tenant, INBOUND_ERROR)
    except Exception as err:  # noqa: BLE001 - any failure is stored and shown, never raised
        body = _error_response(logger, "POST /api/selection (webhook)", err).json
        logger.warning("goto-sms: moving the webhook to the new account failed (%s)", body["error"])
        record: dict[str, Json] = {"error": body["error"], "message": body["message"]}
        if body.get("gotoStatus") is not None:
            record["gotoStatus"] = body["gotoStatus"]
        await store.set(tenant, INBOUND_ERROR, record)


async def _inbound(core: Core, secret: str, body: object) -> HttpResponse:
    """POST /inbound/:secret: GoTo's webhook. Authenticated by the path secret alone."""
    not_found = HttpResponse(404)
    if await core.incoming_mode() != "webhook":
        return not_found
    expected = await get_webhook_secret(core.store, core.tenant)
    if not expected or not check_webhook_secret(secret, expected):
        return not_found
    # GoTo sends other event types to the same channel; acknowledge and ignore them.
    message = parse_webhook_envelope(body)
    if message is None:
        return HttpResponse(204)
    # A subscription covers the whole GoTo account, so texts to its other numbers (often
    # another app's) arrive here too. Acting on them would opt people out of, and reply from,
    # the wrong app. Only a known `to` that differs from our number is dropped; polling is
    # already scoped to our number. (C25)
    from_number = (await core.config.resolve()).from_number
    if from_number and message.to and message.to != from_number:
        core.logger.debug("goto-sms: ignored an inbound text to another number in the account")
        return HttpResponse(204)
    # Persisted before the 204, so an acknowledged STOP survives a crash.
    if await core.processor.persist(message):
        _process_later(core, message.id)
    return HttpResponse(204)


def _process_later(core: Core, message_id: str) -> None:
    """Processes the text after the response has gone out. process() leaves the row pending if
    it fails, and the background replay retries it. Only the error's name is logged (no message
    text). Registered on core.inflight now (before the 204) so close() waits for it."""

    async def job() -> None:
        await asyncio.sleep(0)  # Node: setImmediate, i.e. after the response is written
        try:
            await core.processor.process(message_id)
        except Exception as err:  # noqa: BLE001 - nothing to answer; the row stays pending
            core.logger.error("goto-sms: processing an inbound text failed (%s)", error_name(err))

    task = asyncio.get_running_loop().create_task(job())
    core.inflight.add(task)
    task.add_done_callback(core.inflight.discard)


async def _inbound_check(core: Core, secret: str) -> HttpResponse:
    """OPTIONS /inbound/:secret: GoTo's reachability check on channel creation (C24). 200 iff
    the secret matches; the mode isn't checked, since GoTo checks before it is webhook."""
    expected = await get_webhook_secret(core.store, core.tenant)
    return HttpResponse(200 if expected and check_webhook_secret(secret, expected) else 404)


async def _callback(core: Core, query: Mapping[str, str]) -> HttpResponse:
    """GET /callback: the OAuth redirect, authenticated by `state`. Always redirects to the
    page."""

    def back(q: str) -> HttpResponse:
        return HttpResponse(302, redirect=f"./?{q}")

    # GoTo's own error text is never echoed; the page explains the usual causes.
    if query.get("error"):
        return back("error=goto_login")
    code, state = query.get("code"), query.get("state")
    if not code or not state:
        return back("error=bad_state")
    try:
        await core.auth.finish_connect(code, state)
        return back("connected=1")
    except Exception as err:  # noqa: BLE001 - the callback always redirects to the page
        err_code = err.code if isinstance(err, GoToSmsError) else "internal"
        core.logger.debug("goto-sms: OAuth callback failed (%s)", err_code)
        return back(f"error={quote(err_code, safe=_URI_COMPONENT_SAFE)}")


def assert_setup_auth(options: ResolvedOptions) -> None:
    """Raises unless the setup routes are protected: `authorize` is required, and
    `allow_unauthenticated_setup` is a development-only escape hatch, refused when
    GOTO_SMS_ENV=production."""
    if options.authorize:
        return
    if not options.allow_unauthenticated_setup or options.env.get("GOTO_SMS_ENV") == "production":
        raise RuntimeError(NO_AUTHORIZE)


@dataclass
class _RouteTable:
    """What a dispatcher needs besides the shared auth/exemption logic."""

    inbound: Callable[[str, object], Awaitable[HttpResponse]]
    inbound_check: Callable[[str], Awaitable[HttpResponse]]
    callback: Callable[[Mapping[str, str]], Awaitable[HttpResponse]]
    routes: dict[str, Handler]
    # Runs when no route matched; returns None for a plain 404.
    fallback: Callable[[HttpRequest], HttpResponse | None] | None = None


async def _authorized(options: ResolvedOptions, req: HttpRequest) -> bool:
    if options.authorize is None:
        return True
    view = AuthorizeRequest(
        method=req.method,
        path=req.path,
        query=dict(req.query),
        headers=dict(req.headers),
        cookies=dict(req.cookies),
    )
    result = options.authorize(view)
    if inspect.isawaitable(result):
        result = await result
    return bool(result)


def _dispatcher(options: ResolvedOptions, logger: logging.Logger, t: _RouteTable) -> Handler:
    """The exemptions, authorize, the same-origin check and the error mapping, for every
    route."""

    async def handle(req: HttpRequest) -> HttpResponse:
        method = req.method.upper()
        # A HEAD (link prefetchers, scanners) must not consume the single-use OAuth state.
        if method == "HEAD" and req.path == "/callback":
            return HttpResponse(200)
        if method == "HEAD":
            method = "GET"
        hook = _INBOUND.fullmatch(req.path)
        # The webhook path holds its secret, so logs name the pattern instead.
        route = f"{method} /inbound/:secret" if hook else f"{method} {req.path}"
        try:
            # Exempt from authorize and the Origin check: GoTo and the OAuth redirect send
            # neither the host's session nor a same-origin header. Each carries its own secret.
            if hook and method == "POST":
                return await t.inbound(hook.group(1), req.body)
            # GoTo checks the URL with OPTIONS while creating the channel, i.e. before the
            # stored mode is webhook, so only the secret decides; a non-2xx makes GoTo refuse
            # the channel.
            if hook and method == "OPTIONS":
                return await t.inbound_check(hook.group(1))
            if route == "GET /callback":
                return await t.callback(req.query)

            if not await _authorized(options, req):
                # A person opening the page goes to the host's login; API callers get the 403.
                if route == "GET /" and options.login_url:
                    return HttpResponse(302, redirect=options.login_url)
                return HttpResponse(403, {"error": "forbidden", "message": "Not allowed"})
            if method != "GET" and not _same_origin(req, options.public_url):
                return HttpResponse(
                    403, {"error": "cross_origin", "message": "Cross-origin request refused"}
                )
            handler = t.routes.get(route)
            if handler is not None:
                return await handler(req)
            fallback = t.fallback(req) if t.fallback else None
            return fallback or HttpResponse(404, {"error": "not_found", "message": "No such route"})
        except Exception as err:  # noqa: BLE001 - one error mapping for every route
            return _error_response(logger, route, err)

    return handle


def _live_handlers(core: Core, setup_html: str) -> Handler:
    """The routes of a started instance."""

    async def status() -> StatusPayload:
        # Behind authorize, so this is the one place the webhook URL (and its secret) is shown.
        return await build_status(core.options, core, include_webhook_url=True)

    def ok(body: Any) -> HttpResponse:
        return HttpResponse(200, body)

    async def page(_req: HttpRequest) -> HttpResponse:
        return HttpResponse(200, html=setup_html)

    async def get_status(_req: HttpRequest) -> HttpResponse:
        return ok(await status())

    async def post_credentials(req: HttpRequest) -> HttpResponse:
        b = _as_object(req.body)
        patch: dict[Any, str] = {}
        for key in ("clientId", "clientSecret"):
            v = _opt_string(b, key)
            if v is not None:
                patch[key] = v
        await core.config.save(patch)
        return ok(await status())

    async def post_connect(_req: HttpRequest) -> HttpResponse:
        if not core.options.redirect_uri:
            raise GoToSmsError(
                "Set public_url (or GOTO_SMS_PUBLIC_URL) so GoTo has a redirect URI to send "
                "you back to",
                "no_public_url",
            )
        return ok({"url": await core.auth.start_connect()})

    async def post_disconnect(_req: HttpRequest) -> HttpResponse:
        await core.auth.disconnect()
        return ok(await status())

    async def get_accounts(_req: HttpRequest) -> HttpResponse:
        return ok({"accounts": [_account_json(a) for a in await list_accounts(core.http)]})

    async def get_numbers(req: HttpRequest) -> HttpResponse:
        account_key = req.query.get("accountKey")
        if not account_key:
            raise _bad_input("accountKey is required")
        numbers = await list_numbers(core.http, account_key)
        return ok({"numbers": [_number_json(n) for n in numbers]})

    async def post_selection(req: HttpRequest) -> HttpResponse:
        b = _as_object(req.body)
        patch: dict[Any, str] = {}
        for key in ("accountKey", "fromNumber"):
            v = _opt_string(b, key)
            if v is not None:
                patch[key] = v
        await core.config.save(patch)
        await _follow_account(core)
        return ok(await status())

    async def post_test(req: HttpRequest) -> HttpResponse:
        to = _req_string(_as_object(req.body), "to")
        return ok(send_result_to_json(await core.sender.send(to, TEST_TEXT)))

    async def post_incoming(req: HttpRequest) -> HttpResponse:
        mode = _MODES.get(_opt_string(_as_object(req.body), "mode") or "")
        if mode is None:
            raise _bad_input('mode must be "webhook", "polling" or "off"')
        await _set_incoming(core, mode)
        return ok(await status())

    async def get_log(req: HttpRequest) -> HttpResponse:
        n = _parse_int(req.query.get("limit"))
        limit = min(n, LOG_MAX) if n is not None and n > 0 else LOG_DEFAULT
        return ok({"entries": [log_entry_to_json(e) for e in await core.log.list(limit)]})

    async def get_opt_outs(_req: HttpRequest) -> HttpResponse:
        return ok({"optOuts": await core.opt_outs.list()})

    async def post_opt_outs(req: HttpRequest) -> HttpResponse:
        b = _as_object(req.body)
        number = _req_string(b, "number")
        action = _opt_string(b, "action")
        if action == "add":
            await core.opt_outs.add(number, "admin")
        elif action == "remove":
            await core.opt_outs.remove(number)
        else:
            raise _bad_input('action must be "add" or "remove"')
        return ok({"optOuts": await core.opt_outs.list()})

    routes: dict[str, Handler] = {
        "GET /": page,
        "GET /api/status": get_status,
        "POST /api/credentials": post_credentials,
        "POST /api/connect": post_connect,
        "POST /api/disconnect": post_disconnect,
        "GET /api/accounts": get_accounts,
        "GET /api/numbers": get_numbers,
        "POST /api/selection": post_selection,
        "POST /api/test": post_test,
        "POST /api/incoming": post_incoming,
        "GET /api/log": get_log,
        "GET /api/opt-outs": get_opt_outs,
        "POST /api/opt-outs": post_opt_outs,
    }

    async def inbound(secret: str, body: object) -> HttpResponse:
        return await _inbound(core, secret, body)

    async def inbound_check(secret: str) -> HttpResponse:
        return await _inbound_check(core, secret)

    async def callback(query: Mapping[str, str]) -> HttpResponse:
        return await _callback(core, query)

    return _dispatcher(
        core.options, core.logger, _RouteTable(inbound, inbound_check, callback, routes)
    )


def _startup_failed_handlers(
    options: ResolvedOptions, logger: logging.Logger, err: BaseException, setup_html: str
) -> Handler:
    """The routes when startup failed (e.g. no encryption key): still behind authorize, the
    page and status answer so the admin can see why, and every other API route is
    `not_configured`."""
    startup_error = startup_error_text(err)

    async def not_found(*_: object) -> HttpResponse:
        return HttpResponse(404)

    async def callback(_query: Mapping[str, str]) -> HttpResponse:
        return HttpResponse(302, redirect="./?error=not_configured")

    async def page(_req: HttpRequest) -> HttpResponse:
        return HttpResponse(200, html=setup_html)

    async def get_status(_req: HttpRequest) -> HttpResponse:
        return HttpResponse(200, await build_status(options, None, startup_error))

    def fallback(req: HttpRequest) -> HttpResponse | None:
        if req.path.startswith("/api/"):
            return HttpResponse(400, {"error": "not_configured", "message": startup_error})
        return None

    return _dispatcher(
        options,
        logger,
        _RouteTable(
            inbound=not_found,
            inbound_check=not_found,
            callback=callback,
            routes={"GET /": page, "GET /api/status": get_status},
            fallback=fallback,
        ),
    )


def create_handlers(core: Core, setup_html: str) -> Handler:
    assert_setup_auth(core.options)
    return _live_handlers(core, setup_html)


def create_deferred_handlers(
    options: ResolvedOptions,
    logger: logging.Logger,
    core: Callable[[], Awaitable[Core]],
    setup_html: str,
) -> Handler:
    """For apps created before startup finishes (hosts mount them synchronously). The first
    request awaits `core()` (startup) once, shared by concurrent first requests; if it failed,
    the startup-failed routes are served from then on. Doesn't check `authorize` itself:
    asgi_app() does, webhook_app() needs none."""
    pending: asyncio.Future[Handler] | None = None

    async def build() -> Handler:
        try:
            c = await core()
        except Exception as err:  # noqa: BLE001 - any startup failure is shown on the page
            return _startup_failed_handlers(options, logger, err, setup_html)
        return _live_handlers(c, setup_html)

    async def handle(req: HttpRequest) -> HttpResponse:
        nonlocal pending
        if pending is None:
            pending = asyncio.ensure_future(build())
        # Shielded: one request giving up must not cancel the startup the others wait on.
        handler = await asyncio.shield(pending)
        return await handler(req)

    return handle
