"""The ASGI adapter: maps ASGI 3 requests onto the framework-free handlers (handlers.py). A port
of Node server/express.ts, with no web framework: the host mounts the app, e.g.
`app.mount("/goto-sms", sms.asgi_app())` in FastAPI/Starlette.

What lives here, as in express.ts: JSON body parsing (body-parser's `express.json({limit:
'100kb'})` semantics; 413 too_large, 400 invalid_input), the redirect of the bare mount to its
trailing-slash form, flattening query/header values, the response headers, and turning any
error into a JSON 500 instead of a traceback. HEAD handling is in the handlers.
"""

import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass
from functools import cache
from http.cookies import CookieError, SimpleCookie
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qsl, quote

from aiogotoconnect.sms.core import ResolvedOptions
from aiogotoconnect.sms.server.handlers import Handler, HttpRequest, HttpResponse

__all__ = ["BODY_LIMIT", "ASGIApp", "RouterSource", "build_asgi_app", "setup_html"]

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# express.json({ limit: '100kb' }): body-parser's kb is 1024 bytes.
BODY_LIMIT = 100 * 1024
_INBOUND = re.compile(r"/inbound/[^/]+")
# JSON whitespace (body-parser's strict-mode first-character check skips exactly these).
_JSON_WS = " \t\n\r"

_TOO_LARGE = {"error": "too_large", "message": "Request body is too large"}
_INVALID_JSON = {"error": "invalid_input", "message": "Request body isn't valid JSON"}
_INTERNAL = {"error": "internal", "message": "Something went wrong; see the server logs"}
_NOT_FOUND = {"error": "not_found", "message": "No such route"}


@cache
def setup_html() -> str:
    """The vendored goto-sms setup page (src/aiogotoconnect/sms/setup/index.html), read as bytes
    so it is served byte-for-byte as released."""
    page = files("aiogotoconnect.sms").joinpath("setup/index.html").read_bytes()
    return page.decode("utf-8")


@dataclass(frozen=True)
class RouterSource:
    options: ResolvedOptions
    logger: logging.Logger
    handle: Handler


class _BodyRejected(Exception):
    """body-parser's error, carrying its `type` for the debug line."""

    def __init__(self, status: int, payload: Mapping[str, str], kind: str) -> None:
        super().__init__(kind)
        self.status = status
        self.payload = payload
        self.kind = kind


class _Disconnected(Exception):
    """The client went away before its body arrived; nothing is sent."""


def _route_path(scope: Scope) -> str:
    """The path below the mount. ASGI servers and Starlette's Mount keep the full path in
    `path` and the mount in `root_path` (as Starlette's get_route_path does); a server that
    already stripped the prefix leaves `path` alone. "" means the bare mount itself."""
    path: str = scope.get("path", "") or ""
    root: str = scope.get("root_path", "") or ""
    if not root or not path.startswith(root):
        return path
    if path == root:
        return ""
    if path[len(root)] == "/":
        return path[len(root) :]
    return path


def _headers(scope: Scope) -> dict[str, str]:
    """Header names lower-cased; a repeated header joined with ", " (Node's flatten(join=true)),
    except cookie, which Node's http joins with "; "."""
    out: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", []):
        name = bytes(raw_name).decode("latin-1").lower()
        value = bytes(raw_value).decode("latin-1")
        if name in out:
            out[name] += ("; " if name == "cookie" else ", ") + value
        else:
            out[name] = value
    return out


def _query(scope: Scope) -> dict[str, str]:
    """The first value of each query parameter (Node's flatten(join=false))."""
    raw = bytes(scope.get("query_string", b"")).decode("latin-1")
    out: dict[str, str] = {}
    for k, v in parse_qsl(raw, keep_blank_values=True):
        out.setdefault(k, v)
    return out


def _cookies(header: str | None) -> dict[str, str]:
    if not header:
        return {}
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(header)
    except CookieError:  # a hostile or malformed header: no cookies, never a 500
        return {}
    return {name: morsel.value for name, morsel in jar.items()}


def _reject_constant(name: str) -> object:
    # JSON.parse refuses NaN and Infinity; Python's json accepts them by default.
    raise ValueError(f"not JSON: {name}")


async def _read_body(headers: Mapping[str, str], receive: Receive) -> object:
    """express.json({ limit: '100kb' }): only an application/json body is read (anything else
    counts as missing); over the limit is 413; the first character must be `{` or `[`
    (strict mode); anything JSON.parse would refuse is 400. A missing or empty body is None."""
    content_type = headers.get("content-type", "")
    mime, _, params = content_type.partition(";")
    if mime.strip().lower() != "application/json":
        return None
    charset = ""
    for param in params.split(";"):
        key, _, value = param.partition("=")
        if key.strip().lower() == "charset":
            charset = value.strip().strip('"').lower()
    # body-parser refuses other charsets and encodings (415); express.ts turns a 4xx into this.
    encoding = headers.get("content-encoding", "identity").strip().lower()
    if charset not in ("", "utf-8", "utf8") or encoding != "identity":
        raise _BodyRejected(400, _INVALID_JSON, "charset.unsupported")
    declared = headers.get("content-length", "").strip()
    if declared.isdigit() and int(declared) > BODY_LIMIT:
        raise _BodyRejected(413, _TOO_LARGE, "entity.too.large")

    raw = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise _Disconnected
        raw += message.get("body", b"")
        if len(raw) > BODY_LIMIT:
            raise _BodyRejected(413, _TOO_LARGE, "entity.too.large")
        if not message.get("more_body", False):
            break
    if not raw:
        return None
    # iconv-lite (body-parser) replaces bad UTF-8 and drops a BOM; do the same.
    text = raw.decode("utf-8", errors="replace").removeprefix("﻿")
    first = text.lstrip(_JSON_WS)[:1]
    if first not in ("{", "["):
        raise _BodyRejected(400, _INVALID_JSON, "entity.parse.failed")
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):  # RecursionError: 100 kB of `[[[[...`
        raise _BodyRejected(400, _INVALID_JSON, "entity.parse.failed") from None


@dataclass(frozen=True)
class _Rendered:
    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes


def _render(out: HttpResponse) -> _Rendered:
    """express.ts send(): serialized up front, so a failure becomes a 500 before anything is
    sent."""
    # Status and log entries change; nothing here should come from a cache.
    headers: list[tuple[bytes, bytes]] = [(b"cache-control", b"no-store")]
    body = b""
    if out.redirect is not None:
        # Percent-encoded like Express's encodeurl: existing escapes and URL syntax kept.
        location = quote(out.redirect, safe="!#$%&'()*+,/:;=?@[]~")
        headers.append((b"location", location.encode("latin-1")))
    elif out.html is not None:
        # The admin page: never framed by another site (clickjacking).
        headers += [
            (b"x-frame-options", b"DENY"),
            (b"content-security-policy", b"frame-ancestors 'none'"),
            (b"content-type", b"text/html; charset=utf-8"),
        ]
        body = out.html.encode("utf-8")
    elif out.json is not None:
        # JSON.stringify's output: compact, non-ASCII kept, no NaN.
        text = json.dumps(out.json, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        headers.append((b"content-type", b"application/json; charset=utf-8"))
        body = text.encode("utf-8")
    if out.status not in (204, 304):
        headers.append((b"content-length", str(len(body)).encode("latin-1")))
    return _Rendered(out.status, headers, body)


async def _send(send: Send, r: _Rendered, head: bool) -> None:
    await send({"type": "http.response.start", "status": r.status, "headers": r.headers})
    # HEAD: the GET's headers (Content-Length included) without its body.
    await send({"type": "http.response.body", "body": b"" if head else r.body})


async def _lifespan(receive: Receive, send: Send) -> None:
    """Acknowledged without starting anything: start() runs on the first request or from the
    host's own lifespan (`async with sms:`)."""
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


def _bare_mount_location(scope: Scope) -> str:
    """`/goto-sms?x=1` → `./goto-sms/?x=1`: relative (the last segment only), so it also works
    behind a proxy that strips a prefix."""
    raw_path = scope.get("raw_path")
    if raw_path:
        # The path as sent (percent-encoded); some servers include the query in raw_path.
        pathname = bytes(raw_path).split(b"?", 1)[0].decode("latin-1")
    else:
        pathname = quote(scope.get("path", ""), safe="/:@!$&'()*+,;=-._~")
    last = pathname[pathname.rfind("/") + 1 :]
    query = bytes(scope.get("query_string", b"")).decode("latin-1")
    return f"./{last}/" + (f"?{query}" if query else "")


def build_asgi_app(source: RouterSource, inbound_only: bool = False) -> ASGIApp:
    """The ASGI app over `source.handle`. With `inbound_only`, only `POST` and `OPTIONS
    /inbound/:secret` are handled; everything else is 404 (Node's webhookRouter falls through
    to the host's routes; an ASGI mount owns its prefix)."""
    options, logger = source.options, source.logger
    mount_path = options.mount_path
    warned = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal warned
        if scope["type"] == "lifespan":
            await _lifespan(receive, send)
            return
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1000})
            return

        method = str(scope.get("method", "GET")).upper()
        route_path = _route_path(scope)
        path = route_path or "/"
        head = method == "HEAD"
        if inbound_only and not (method in ("POST", "OPTIONS") and _INBOUND.fullmatch(path)):
            await _send(send, _render(HttpResponse(404, _NOT_FOUND)), head)
            return

        # The redirect URI and webhook URL are built from the mount_path option, not the request.
        mounted_at = str(scope.get("root_path", "") or "").rstrip("/")
        if not warned and mounted_at != mount_path:
            warned = True
            logger.warning(
                "goto-sms: mounted at %s but mount_path option is %s; "
                "the redirect URI shown will be wrong",
                mounted_at or "/",
                mount_path or "/",
            )

        # `/goto-sms` → `./goto-sms/`, so the page's relative URLs resolve under the mount.
        full_path = str(scope.get("path", ""))
        if (
            not inbound_only
            and method in ("GET", "HEAD")
            and route_path == ""
            and not full_path.endswith("/")
        ):
            location = _bare_mount_location(scope)
            await _send(send, _render(HttpResponse(302, redirect=location)), head)
            return

        headers = _headers(scope)
        try:
            body = await _read_body(headers, receive)
        except _Disconnected:
            return
        except _BodyRejected as err:
            # Body-parser failures as the contract's JSON errors.
            logger.debug("goto-sms: request body rejected (%s)", err.kind)
            await _send(send, _render(HttpResponse(err.status, dict(err.payload))), head)
            return

        req = HttpRequest(
            method=method,
            path=path,
            query=_query(scope),
            headers=headers,
            cookies=_cookies(headers.get("cookie")),
            body=body,
        )
        try:
            rendered = _render(await source.handle(req))
        except Exception:  # noqa: BLE001 - a bug must reach the client as JSON, not a traceback
            logger.debug("goto-sms: request body rejected (%s)", "unknown")
            rendered = _render(HttpResponse(500, _INTERNAL))
        await _send(send, rendered, head)

    return app
