"""A fake GoTo (auth + API on one aiohttp.web server) for tests; port of fake-goto.ts.

Answers are modeled on the reference apps (swu-ideas, party-invites, geeks-tracker). Later tasks
add API routes with `fake.route(method, path_prefix, handler)`.

Standalone (the e2e): `python -m tests.sms.fake_goto` serves on 127.0.0.1:$FAKE_GOTO_PORT
(default 4010) with one SMS-enabled number and `GET /__sent` (what was sent through it).
"""

import asyncio
import base64
import inspect
import json
import os
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from aiohttp import web


@dataclass
class FakeRequest:
    method: str
    path: str  # including the query string, as sent
    pathname: str  # without the query string
    query: dict[str, str]  # first value of each key
    headers: dict[str, str]  # lower-cased names
    body: str


@dataclass
class FakeResponse:
    status: int
    body: Any = None  # JSON-serialised unless it's a str; None -> empty body
    location: str | None = None  # sent as the Location header (redirects)


FakeHandler = Callable[[FakeRequest], FakeResponse | Awaitable[FakeResponse]]


@dataclass
class _Route:
    method: str
    prefix: str
    handler: FakeHandler
    auth: bool


@dataclass
class _Override:
    method: str
    prefix: str
    status: int | None  # None: hang (never answer)
    body: Any = None


@dataclass
class Tokens:
    """valid: access tokens the API accepts (clear() it to force a 401).
    refresh: the refresh token the token endpoint accepts (and returns on code exchange).
    codes: authorization code -> required redirect_uri ('' = any)."""

    valid: set[str] = field(default_factory=set)
    refresh: str = "refresh-1"
    rotate_on_refresh: bool = False
    codes: dict[str, str] = field(default_factory=dict)


def _token() -> str:
    return secrets.token_hex(16)


def _timestamp_ms(value: Any) -> float:
    """Date.parse stand-in for GoTo's mixed-precision ISO timestamps; 0 when unparseable."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value)).timestamp() * 1000
    except ValueError:
        return 0.0


class FakeGoTo:
    def __init__(
        self, client_id: str = "cid", client_secret: str = "csecret", port: int = 0
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._port = port
        self._routes: list[_Route] = []
        self._overrides: list[_Override] = []
        self._release_hung = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self.url = ""  # use as auth_base_url AND api_base_url
        self.requests: list[FakeRequest] = []
        # Accepted sends: {"ownerPhoneNumber", "contactPhoneNumbers", "body"}.
        self.sent: list[dict[str, Any]] = []
        self.tokens = Tokens(codes={"code-ok": ""})
        # Body served for GET /identity/v1/Users/me (default: one account, acct-1).
        self.me: Any = {
            "urn:scim:schemas:extension:getgo:1.0": {
                "accounts": [{"value": "acct-1", "display": "Test Account"}]
            }
        }
        # Pages served by GET /voice-admin/v1/phone-numbers, for any accountKey. pageMarker is
        # the next page's index; the last page carries no nextPageMarker.
        self.numbers_pages: list[list[Any]] = [[]]
        # Messages served by the polling routes: id, from (the contact), to (our number), body,
        # timestamp, and optional direction ("OUT" = one of our sends to that contact; default IN).
        self.inbound: list[dict[str, Any]] = []
        # Page size for the polling routes, overriding the request's pageSize (paging tests).
        self.poll_page_size: int | None = None
        # Notification channels; channelId is `ch-<n>`.
        self.channels: list[dict[str, Any]] = []
        # Messaging subscriptions; id is `sub-<n>`.
        self.subscriptions: list[dict[str, Any]] = []
        self._install_routes()

    @property
    def auth_base_url(self) -> str:
        return self.url

    @property
    def api_base_url(self) -> str:
        return self.url

    def fail_next(self, method: str, path_prefix: str, status: int, body: Any = None) -> None:
        """The next request matching method + path prefix gets `status` (+ body) instead."""
        self._overrides.append(_Override(method, path_prefix, status, body))

    def hang_next(self, method: str, path_prefix: str) -> None:
        """The next matching request is accepted but never answered (timeout tests)."""
        self._overrides.append(_Override(method, path_prefix, None))

    def route(
        self, method: str, path_prefix: str, handler: FakeHandler, *, auth: bool = True
    ) -> None:
        """Register a handler. The longest matching prefix wins. API routes require a bearer
        token from `tokens.valid` (else 401) unless auth=False."""
        self._routes.append(_Route(method, path_prefix, handler, auth))

    # --- lifecycle ---

    async def start(self) -> "FakeGoTo":
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, handle_signals=False, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self._port).start()
        port = self._runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def close(self) -> None:
        # Release hung requests first so shutdown doesn't wait on them.
        self._release_hung.set()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # --- request handling ---

    def _take_override(self, method: str, path: str) -> _Override | None:
        for i, o in enumerate(self._overrides):
            if o.method == method and path.startswith(o.prefix):
                return self._overrides.pop(i)
        return None

    @staticmethod
    def _send(r: FakeResponse) -> web.Response:
        if r.body is None:
            text = ""
        elif isinstance(r.body, str):
            text = r.body
        else:
            text = json.dumps(r.body, separators=(",", ":"))
        headers: dict[str, str] = {}
        if r.location:
            headers["Location"] = r.location
        res = web.Response(status=r.status, text=text, headers=headers)
        if text:
            res.content_type = "application/json"
        return res

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        method = request.method
        path = request.raw_path
        headers = {k.lower(): v for k, v in request.headers.items()}
        body = (await request.read()).decode("utf-8", errors="replace")
        self.requests.append(FakeRequest(method, path, request.path, {}, headers, body))

        override = self._take_override(method, path)
        if override is not None:
            if override.status is None:
                await self._release_hung.wait()  # hang: never answer (until close)
                return web.Response(status=503)
            return self._send(FakeResponse(override.status, override.body))

        pathname = urlsplit(path).path
        query = {k: v[0] for k, v in parse_qs(urlsplit(path).query, keep_blank_values=True).items()}
        self.requests[-1].pathname = pathname
        self.requests[-1].query = query
        matches = [r for r in self._routes if r.method == method and pathname.startswith(r.prefix)]
        if not matches:
            return self._send(FakeResponse(404, {"error": "not_found"}))
        match = max(matches, key=lambda r: len(r.prefix))
        if match.auth:
            auth = headers.get("authorization", "")
            bearer = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
            if not bearer or bearer not in self.tokens.valid:
                return self._send(FakeResponse(401, {"error": "invalid_token"}))
        result = match.handler(FakeRequest(method, path, pathname, query, headers, body))
        if inspect.isawaitable(result):
            result = await result
        return self._send(result)

    # --- built-in routes ---

    def _install_routes(self) -> None:
        self.route("POST", "/oauth/token", self._token_endpoint, auth=False)
        self.route("GET", "/oauth/authorize", self._authorize, auth=False)
        self.route("GET", "/identity/v1/Users/me", lambda _r: FakeResponse(200, self.me))
        self.route("GET", "/voice-admin/v1/phone-numbers", self._phone_numbers)
        self.route("POST", "/messaging/v1/messages", self._send_text)
        self.route("GET", "/messaging/v1/conversations", self._conversations)
        self.route("GET", "/messaging/v1/messages", self._messages)
        self.route(
            "GET",
            "/notification-channel/v1/channels",
            lambda _r: FakeResponse(200, {"items": self.channels}),
        )
        self.route("POST", "/notification-channel/v1/channels/", self._create_channel)
        self.route("GET", "/messaging/v2/subscriptions", self._list_subscriptions)
        self.route("POST", "/messaging/v2/subscriptions", self._create_subscription)
        self.route("DELETE", "/messaging/v2/subscriptions/", self._delete_subscription)

    def _token_endpoint(self, req: FakeRequest) -> FakeResponse:
        raw = f"{self._client_id}:{self._client_secret}".encode()
        expected = f"Basic {base64.b64encode(raw).decode()}"
        if req.headers.get("authorization") != expected:
            return FakeResponse(401, {"error": "invalid_client"})
        form = {k: v[0] for k, v in parse_qs(req.body, keep_blank_values=True).items()}
        # GoTo wants client credentials in the Basic header only.
        if "client_secret" in form:
            return FakeResponse(400, {"error": "invalid_request"})

        def issue(refresh: str | None = None) -> FakeResponse:
            access = _token()
            self.tokens.valid.add(access)
            payload: dict[str, Any] = {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": 3600,
            }
            if refresh:
                payload["refresh_token"] = refresh
            return FakeResponse(200, payload)

        grant = form.get("grant_type")
        if grant == "authorization_code":
            required = self.tokens.codes.get(form.get("code", ""))
            if required is None or (required and required != form.get("redirect_uri")):
                return FakeResponse(400, {"error": "invalid_grant"})
            return issue(self.tokens.refresh)
        if grant == "refresh_token":
            if form.get("refresh_token") != self.tokens.refresh:
                return FakeResponse(400, {"error": "invalid_grant"})
            if not self.tokens.rotate_on_refresh:
                return issue()
            self.tokens.refresh = f"refresh-{_token()}"
            return issue(self.tokens.refresh)
        return FakeResponse(400, {"error": "unsupported_grant_type"})

    @staticmethod
    def _authorize(req: FakeRequest) -> FakeResponse:
        """The browser step: approves at once with code-ok (e2e only)."""
        redirect = req.query.get("redirect_uri")
        if not redirect:
            return FakeResponse(400, {"error": "invalid_request"})
        parts = urlsplit(redirect)
        extra = urlencode({"code": "code-ok", "state": req.query.get("state", "")})
        query = f"{parts.query}&{extra}" if parts.query else extra
        return FakeResponse(
            302,
            location=urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment)),
        )

    def _phone_numbers(self, req: FakeRequest) -> FakeResponse:
        try:
            index = int(req.query.get("pageMarker", "0"))
            items = self.numbers_pages[index]
        except (ValueError, IndexError):
            return FakeResponse(400, {"error": "bad_page_marker"})
        body: dict[str, Any] = {"items": items}
        if index + 1 < len(self.numbers_pages):
            body["nextPageMarker"] = str(index + 1)
        return FakeResponse(200, body)

    def _send_text(self, req: FakeRequest) -> FakeResponse:
        """Only accepted sends land in `sent`."""
        self.sent.append(json.loads(req.body))
        return FakeResponse(200, {"id": f"msg-{len(self.sent)}"})

    # Polling (geeks-tracker fetchInbound): both routes read `inbound`, filtered by owner number
    # and oldestTimestamp (compared as instants, like GoTo's mixed precision).
    def _for_owner(self, req: FakeRequest) -> list[dict[str, Any]]:
        since = _timestamp_ms(req.query.get("oldestTimestamp", ""))
        owner = req.query.get("ownerPhoneNumber")
        return [
            m for m in self.inbound if m["to"] == owner and _timestamp_ms(m["timestamp"]) >= since
        ]

    def _paged(self, req: FakeRequest, items: list[Any]) -> FakeResponse:
        """Paged like GoTo: pageSize per page (poll_page_size overrides), pageMarker = offset."""
        size = self.poll_page_size or _to_int(req.query.get("pageSize")) or 100
        start = _to_int(req.query.get("pageMarker")) or 0
        body: dict[str, Any] = {"items": items[start : start + size]}
        if start + size < len(items):
            body["nextPageMarker"] = str(start + size)
        return FakeResponse(200, body)

    def _conversations(self, req: FakeRequest) -> FakeResponse:
        last: dict[str, str] = {}
        for m in self._for_owner(req):
            prev = last.get(m["from"])
            if prev is None or _timestamp_ms(m["timestamp"]) > _timestamp_ms(prev):
                last[m["from"]] = m["timestamp"]
        items = [
            {"contactPhoneNumbers": [contact], "lastMessageTimestamp": ts}
            for contact, ts in last.items()
        ]
        return self._paged(req, items)

    def _messages(self, req: FakeRequest) -> FakeResponse:
        contact = req.query.get("contactPhoneNumber")
        items = [
            {
                "id": m["id"],
                "direction": m.get("direction") or "IN",
                "authorPhoneNumber": m["to"] if m.get("direction") == "OUT" else m["from"],
                "ownerPhoneNumber": m["to"],
                "contactPhoneNumber": m["from"],
                "body": m["body"],
                "timestamp": m["timestamp"],
            }
            for m in self._for_owner(req)
            if m["from"] == contact
        ]
        # Served in reverse order on purpose: callers must sort, not trust the API's order.
        items.reverse()
        return self._paged(req, items)

    def _create_channel(self, req: FakeRequest) -> FakeResponse:
        channel = {**json.loads(req.body), "channelId": f"ch-{len(self.channels) + 1}"}
        self.channels.append(channel)
        return FakeResponse(200, channel)

    def _list_subscriptions(self, req: FakeRequest) -> FakeResponse:
        key = req.query.get("accountKey")
        return FakeResponse(
            200, {"items": [s for s in self.subscriptions if s.get("accountKey") == key]}
        )

    def _create_subscription(self, req: FakeRequest) -> FakeResponse:
        sub = {**json.loads(req.body), "id": f"sub-{len(self.subscriptions) + 1}"}
        self.subscriptions.append(sub)
        return FakeResponse(200, sub)

    def _delete_subscription(self, req: FakeRequest) -> FakeResponse:
        sub_id = req.pathname[len("/messaging/v2/subscriptions/") :]
        for i, s in enumerate(self.subscriptions):
            if s["id"] == sub_id:
                del self.subscriptions[i]
                return FakeResponse(204)
        return FakeResponse(404, {"error": "not_found"})


def _to_int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


async def start_fake_goto(
    *, client_id: str = "cid", client_secret: str = "csecret", port: int = 0
) -> FakeGoTo:
    return await FakeGoTo(client_id, client_secret, port).start()


async def _serve() -> None:
    fake = await start_fake_goto(port=int(os.environ.get("FAKE_GOTO_PORT", "4010")))
    fake.numbers_pages = [
        [{"number": "+18015550100", "callerIdName": "Main line", "smsEnabled": True}]
    ]
    fake.route("GET", "/__sent", lambda _r: FakeResponse(200, fake.sent), auth=False)
    print(f"fake GoTo on {fake.url}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await fake.close()


if __name__ == "__main__":
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
