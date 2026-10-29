"""Port of packages/node/test/express.test.ts onto the ASGI adapter (server/asgi.py), plus the
vendored setup page checks, the adapter's body parsing (Review Focus 3) and the forgotten
lifespan / concurrent first requests (Review Focus 1).

The app is mounted under a tiny FastAPI host, so Starlette's `root_path` handling is exercised;
tests that need a precise scope (the bare-mount redirect, lifespan) drive the ASGI app directly."""

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from importlib.resources import files
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI

from aiogotoconnect.sms.client import GoToSms, create_goto_sms
from aiogotoconnect.sms.core import AuthorizeRequest
from aiogotoconnect.sms.errors import NotConfiguredError
from aiogotoconnect.sms.server.asgi import RouterSource, build_asgi_app, setup_html
from aiogotoconnect.sms.server.handlers import HttpRequest, HttpResponse
from aiogotoconnect.sms.store import MemoryStore

from .fake_goto import FakeGoTo
from .helpers import connected_sms, recorder

BASE = "https://app.example"
# sha256 of index.html inside goto-sms-setup-ui-v0.2.1.zip (the goto-sms v0.2.1 release asset).
SETUP_PAGE_SHA256 = "a9ebabe96d20dd122528410288554be7d450120d970740866ed6157d5c5148b7"
NO_AUTH = (
    "goto-sms: pass authorize(req) to create_goto_sms "
    "(or allow_unauthenticated_setup=True in development)"
)
NO_KEY = "Set GOTO_SMS_ENCRYPTION_KEY (or give a data_dir/store the package can keep a key file in)"
LIMIT = 100 * 1024

Cleanups = list[Callable[[], Awaitable[None]]]


@pytest.fixture
async def cleanups() -> AsyncIterator[Cleanups]:
    """Async callables run after the test, last registered first (Node's cleanups array)."""
    pending: Cleanups = []
    yield pending
    while pending:
        await pending.pop()()


class CountingStore(MemoryStore):
    """Counts process-lock claims: one claim per start()."""

    def __init__(self) -> None:
        super().__init__()
        self.claims = 0

    async def claim(self, tenant: str, key: str, ttl_ms: float, owner: str) -> bool:
        if key == "process-lock":
            self.claims += 1
            await asyncio.sleep(0.01)  # keep the start in flight while both requests arrive
        return await super().claim(tenant, key, ttl_ms, owner)


def client_for(app: Any, cleanups: Cleanups, root_path: str = "") -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app, root_path=root_path), base_url=BASE
    )
    cleanups.append(client.aclose)
    return client


def host_with(mount: str, app: Any) -> FastAPI:
    host = FastAPI()
    host.mount(mount, app)
    return host


async def mounted(
    fake: FakeGoTo, cleanups: Cleanups, mount: str = "/goto-sms", **opts: Any
) -> tuple[GoToSms, httpx.AsyncClient]:
    """A connected instance with asgi_app() mounted at `mount` under FastAPI."""
    sms = await connected_sms(fake, **{"authorize": lambda _r: True, **opts})
    cleanups.append(sms.close)
    return sms, client_for(host_with(mount, sms.asgi_app()), cleanups)


def bare(**opts: Any) -> GoToSms:
    """No key and a custom store: start() fails with NotConfiguredError."""
    return create_goto_sms(**{"store": MemoryStore(), "env": {}, "logger": False, **opts})


# Same-origin headers for a state-changing request.
JSON_POST = {"content-type": "application/json", "origin": BASE}


# --- the setup page ---


def test_setup_page_matches_pinned_release() -> None:
    page = files("aiogotoconnect.sms").joinpath("setup/index.html").read_bytes()
    assert hashlib.sha256(page).hexdigest() == SETUP_PAGE_SHA256
    version = files("aiogotoconnect.sms").joinpath("setup/SETUP_UI_VERSION").read_text()
    assert version.strip() == "v0.2.1"
    assert setup_html().encode("utf-8") == page


def test_setup_page_relative_urls_only() -> None:
    absolute = re.compile(r"[\"'`](/goto-sms|/api)/")
    page = setup_html()
    assert "Create your GoTo app" in page, "the real page, not a stub"
    assert not absolute.search(page)


async def test_get_page_serves_the_vendored_html_with_node_headers(
    fake_goto: FakeGoTo, cleanups: Cleanups
) -> None:
    _, client = await mounted(fake_goto, cleanups, "/api/goto", mount_path="/api/goto")
    res = await client.get("/api/goto/")
    assert res.status_code == 200
    assert res.headers["content-type"] == "text/html; charset=utf-8"
    assert "goto-sms v" in res.text
    assert "Create your GoTo app" in res.text
    assert hashlib.sha256(res.content).hexdigest() == SETUP_PAGE_SHA256
    assert res.headers["cache-control"] == "no-store"
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["content-security-policy"] == "frame-ancestors 'none'"
    # JSON gets no-store but not the framing headers (HTML only, as in express.ts).
    api = await client.get("/api/goto/api/status")
    assert api.status_code == 200
    assert api.headers["content-type"] == "application/json; charset=utf-8"
    assert api.headers["cache-control"] == "no-store"
    assert "x-frame-options" not in api.headers
    assert "content-security-policy" not in api.headers


# --- routing ---


async def test_mount_without_a_trailing_slash_redirects_relatively(
    fake_goto: FakeGoTo, cleanups: Cleanups
) -> None:
    sms = await connected_sms(fake_goto, authorize=lambda _r: True, mount_path="/admin/tools/sms")
    cleanups.append(sms.close)
    # Driven directly (root_path set as a host's mount would): Starlette's own redirect_slashes
    # answers a bare Mount path before the sub-app sees it.
    client = client_for(sms.asgi_app(), cleanups, root_path="/admin/tools/sms")
    res = await client.get("/admin/tools/sms?x=1")
    assert res.status_code == 302
    # Relative, so it also works behind a proxy that strips a path prefix.
    assert res.headers["location"] == "./sms/?x=1"
    assert res.headers["cache-control"] == "no-store"
    page = await client.get(str(httpx.URL(f"{BASE}/admin/tools/sms?x=1").join("./sms/?x=1")))
    assert page.status_code == 200
    # Under FastAPI the bare mount still ends on the page.
    host = client_for(host_with("/admin/tools/sms", sms.asgi_app()), cleanups)
    followed = await host.get("/admin/tools/sms?x=1", follow_redirects=True)
    assert followed.status_code == 200
    assert "Create your GoTo app" in followed.text


async def test_head_maps_to_get(fake_goto: FakeGoTo, cleanups: Cleanups) -> None:
    _, client = await mounted(fake_goto, cleanups)
    res = await client.head("/goto-sms/api/status")
    assert res.status_code == 200
    assert "json" in res.headers["content-type"]
    assert res.content == b""
    assert int(res.headers["content-length"]) > 0


async def test_json_body_round_trip_through_post_api_credentials(
    fake_goto: FakeGoTo, cleanups: Cleanups
) -> None:
    _, client = await mounted(fake_goto, cleanups)
    res = await client.post(
        "/goto-sms/api/credentials", headers=JSON_POST, content=json.dumps({"clientId": "new"})
    )
    assert res.status_code == 200
    status = res.json()
    assert status["clientId"] == "new"
    assert status["sources"]["clientId"] == "store"
    assert "csecret" not in res.text


async def test_malformed_json_gives_the_contract_json_error(
    fake_goto: FakeGoTo, cleanups: Cleanups
) -> None:
    _, client = await mounted(fake_goto, cleanups)
    res = await client.post("/goto-sms/api/credentials", headers=JSON_POST, content="{nope")
    assert res.status_code == 400
    assert res.json() == {"error": "invalid_input", "message": "Request body isn't valid JSON"}
    assert res.headers["cache-control"] == "no-store"


async def test_cross_origin_post_is_refused_through_the_app(
    fake_goto: FakeGoTo, cleanups: Cleanups
) -> None:
    _, client = await mounted(fake_goto, cleanups)
    res = await client.post(
        "/goto-sms/api/credentials",
        headers={"content-type": "application/json", "origin": "https://evil.example"},
        content="{}",
    )
    assert res.status_code == 403
    assert res.json()["error"] == "cross_origin"


async def test_webhook_app_exposes_only_inbound(fake_goto: FakeGoTo, cleanups: Cleanups) -> None:
    # No authorize: the webhook is authenticated by its path secret alone.
    sms = await connected_sms(fake_goto)
    cleanups.append(sms.close)
    secret = "S" * 43
    await sms.core.store.set(sms.core.tenant, "inbound:mode", "webhook")
    await sms.core.store.set(sms.core.tenant, "inbound:webhookSecret", secret)
    client = client_for(host_with("/goto-sms", sms.webhook_app()), cleanups)

    for path in ["/", "/api/status", f"/inbound/{secret}", "/callback?code=x&state=y"]:
        res = await client.get(f"/goto-sms{path}")
        assert res.status_code == 404, f"GET {path}"
    assert (await client.head("/goto-sms/callback")).status_code == 404
    post = await client.post("/goto-sms/api/credentials", headers=JSON_POST, content="{}")
    assert post.status_code == 404
    # The filter runs before body parsing: an oversized body elsewhere is a 404, not a 413.
    big = await client.post(
        "/goto-sms/api/credentials", headers=JSON_POST, content="{" + " " * LIMIT + "}"
    )
    assert big.status_code == 404
    # Not an inbound text: acknowledged and ignored.
    hook = await client.post(f"/goto-sms/inbound/{secret}", headers=JSON_POST, content="{}")
    assert hook.status_code == 204
    assert hook.content == b""
    assert "content-type" not in hook.headers
    wrong = await client.post(f"/goto-sms/inbound/{'x' * 43}", headers=JSON_POST, content="{}")
    assert wrong.status_code == 404
    # GoTo's URL check while creating the channel.
    options = await client.options(f"/goto-sms/inbound/{secret}")
    assert options.status_code == 200, "OPTIONS reaches the webhook through webhook_app()"


async def test_mount_mismatch_warns_once(fake_goto: FakeGoTo, cleanups: Cleanups) -> None:
    logger, records = recorder()
    _, client = await mounted(fake_goto, cleanups, "/elsewhere", logger=logger)
    await client.get("/elsewhere/api/status")
    await client.get("/elsewhere/api/status")
    warns = [r for r in records if "mounted at" in r.getMessage()]
    assert len(warns) == 1
    assert warns[0].levelname == "WARNING"
    assert warns[0].getMessage() == (
        "goto-sms: mounted at /elsewhere but mount_path option is /goto-sms; "
        "the redirect URI shown will be wrong"
    )


def test_asgi_app_raises_at_creation_without_authorize() -> None:
    sms = create_goto_sms(store=MemoryStore(), encryption_key="k", env={}, logger=False)
    with pytest.raises(RuntimeError) as caught:
        sms.asgi_app()
    assert str(caught.value) == NO_AUTH
    # The webhook needs no authorize; neither creation starts anything.
    assert callable(sms.webhook_app())
    assert sms._start_task is None


async def test_the_page_and_status_still_answer_when_startup_failed(
    cleanups: Cleanups,
) -> None:
    sms = bare(authorize=lambda _r: True)
    cleanups.append(sms.close)
    with pytest.raises(NotConfiguredError):
        await sms.start()
    client = client_for(host_with("/goto-sms", sms.asgi_app()), cleanups)

    page = await client.get("/goto-sms/")
    assert page.status_code == 200
    assert "goto-sms v" in page.text
    status = (await client.get("/goto-sms/api/status")).json()
    assert status["configured"] is False
    assert status["startupError"] == NO_KEY
    save = await client.post("/goto-sms/api/credentials", headers=JSON_POST, content="{}")
    assert save.status_code == 400
    assert save.json()["error"] == "not_configured"
    assert await sms.status() == status


async def test_a_forgotten_lifespan_first_request_shows_the_startup_error(
    cleanups: Cleanups,
) -> None:
    # Nobody called start(): the first request starts it, and the failure is on the page.
    sms = bare(authorize=lambda _r: True)
    cleanups.append(sms.close)
    client = client_for(host_with("/goto-sms", sms.asgi_app()), cleanups)
    res = await client.get("/goto-sms/api/status")
    assert res.status_code == 200
    assert res.json()["startupError"] == NO_KEY
    assert (await client.get("/goto-sms/")).status_code == 200


async def test_authorize_still_applies_when_startup_failed(cleanups: Cleanups) -> None:
    sms = bare(authorize=lambda _r: False)
    cleanups.append(sms.close)
    client = client_for(host_with("/goto-sms", sms.asgi_app()), cleanups)
    assert (await client.get("/goto-sms/")).status_code == 403
    assert (await client.get("/goto-sms/api/status")).status_code == 403


async def test_root_mount_one_slash_and_no_mount_warning(
    fake_goto: FakeGoTo, cleanups: Cleanups
) -> None:
    logger, records = recorder()
    sms, client = await mounted(fake_goto, cleanups, "/", mount_path="/", logger=logger)
    status = (await client.get("/api/status")).json()
    assert status["redirectUri"] == "https://app.example/callback"
    assert sms.core.webhook_url("S") == "https://app.example/inbound/S"
    assert sms.core.options.mount_path == ""
    assert (await client.get("/")).status_code == 200
    assert not any("mounted at" in r.getMessage() for r in records), "no false mount warning"


async def test_public_url_trailing_slash_with_root_mount(
    fake_goto: FakeGoTo, cleanups: Cleanups
) -> None:
    for mount in ["/", ""]:
        sms = await connected_sms(fake_goto, public_url="https://app.example/", mount_path=mount)
        cleanups.append(sms.close)
        assert (await sms.status())["redirectUri"] == "https://app.example/callback"


async def test_an_error_while_building_the_response_is_a_json_500(cleanups: Cleanups) -> None:
    options = bare(mount_path="/")._options

    async def unserializable(_req: HttpRequest) -> HttpResponse:
        return HttpResponse(200, json={"n": object()})  # json.dumps raises, like a BigInt

    async def raising(_req: HttpRequest) -> HttpResponse:
        raise ValueError("boom: a secret-looking detail")

    logger, _ = recorder()
    for handle in (unserializable, raising):
        app = build_asgi_app(RouterSource(options=options, logger=logger, handle=handle))
        res = await client_for(app, cleanups).get("/api/status")
        assert res.status_code == 500
        assert res.json() == {
            "error": "internal",
            "message": "Something went wrong; see the server logs",
        }
        assert "boom" not in res.text


async def test_head_callback_does_not_consume_the_oauth_state(
    fake_goto: FakeGoTo, cleanups: Cleanups
) -> None:
    sms, client = await mounted(fake_goto, cleanups, connect=False)
    await sms.core.config.save({"clientId": "cid", "clientSecret": "csecret"})
    url = httpx.URL(await sms.core.auth.start_connect())
    state = url.params["state"]
    target = f"/goto-sms/callback?code=code-ok&state={quote(state, safe='')}"
    head = await client.head(target)
    assert head.status_code == 200
    get = await client.get(target)
    assert get.status_code == 302
    assert get.headers["location"] == "./?connected=1"


async def test_login_url_redirects_an_unauthorized_page_load_when_startup_failed(
    cleanups: Cleanups,
) -> None:
    sms = bare(authorize=lambda _r: False, login_url="/login")
    cleanups.append(sms.close)
    client = client_for(host_with("/goto-sms", sms.asgi_app()), cleanups)
    page = await client.get("/goto-sms/")
    assert page.status_code == 302
    assert page.headers["location"] == "/login"
    assert (await client.get("/goto-sms/api/status")).status_code == 403


# --- Review Focus 3: malformed requests never 500 ---


async def test_bad_json_400_and_oversized_413(fake_goto: FakeGoTo, cleanups: Cleanups) -> None:
    _, client = await mounted(fake_goto, cleanups)
    invalid = {"error": "invalid_input", "message": "Request body isn't valid JSON"}

    async def post(path: str, content: Any, headers: dict[str, str] = JSON_POST) -> httpx.Response:
        return await client.post(f"/goto-sms{path}", headers=headers, content=content)

    # Not JSON, a top-level primitive (body-parser's strict mode), NaN, bad UTF-8, deep nesting.
    for body in [b"{nope", b'"just a string"', b"42", b"null", b'{"a": NaN}', b"\xff{}"]:
        res = await post("/api/credentials", body)
        assert (res.status_code, res.json()) == (400, invalid), body
    deep = await post("/api/credentials", b"[" * (LIMIT - 10))
    assert (deep.status_code, deep.json()) == (400, invalid)
    # An array parses (as in Node) and the handler refuses it.
    arr = await post("/api/credentials", b"[1, 2]")
    assert arr.status_code == 400
    assert arr.json() == {"error": "invalid_input", "message": "Expected a JSON object"}

    # Exactly 100 kB is accepted; one byte more is 413, with or without a Content-Length.
    at_limit = b'{"x":"' + b"a" * (LIMIT - 8) + b'"}'
    assert len(at_limit) == LIMIT
    assert (await post("/api/connect", at_limit)).status_code == 200
    too_large = {"error": "too_large", "message": "Request body is too large"}
    over = await post("/api/connect", at_limit + b" ")
    assert (over.status_code, over.json()) == (413, too_large)

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(11):
            yield b" " * 10_000

    streamed = await post("/api/connect", chunks())
    assert (streamed.status_code, streamed.json()) == (413, too_large)

    # A non-JSON content type isn't parsed (express.json skips it): the body counts as missing.
    plain = await post("/api/credentials", b"{nope", {"content-type": "text/plain", "origin": BASE})
    assert plain.status_code == 200
    # An empty JSON body is a missing body.
    assert (await post("/api/credentials", b"")).status_code == 200
    # A charset parameter is fine.
    utf8 = {"content-type": "application/json; charset=utf-8", "origin": BASE}
    assert (await post("/api/credentials", b"{}", utf8)).status_code == 200


# --- Review Focus 1: a forgotten lifespan, concurrent first requests ---


async def test_concurrent_first_requests_start_once(cleanups: Cleanups) -> None:
    store = CountingStore()
    sms = bare(store=store, encryption_key="k", authorize=lambda _r: True)
    cleanups.append(sms.close)
    client = client_for(host_with("/goto-sms", sms.asgi_app()), cleanups)
    assert store.claims == 0, "creating the app starts nothing"
    a, b = await asyncio.gather(client.get("/goto-sms/api/status"), client.get("/goto-sms/"))
    assert (a.status_code, b.status_code) == (200, 200)
    assert a.json()["configured"] is False  # started fine, nothing saved yet
    assert "startupError" not in a.json()
    assert store.claims == 1, "one start for both requests"
    await sms.start()
    assert store.claims == 1


async def test_lifespan_is_acknowledged_without_starting() -> None:
    store = CountingStore()
    sms = bare(store=store, encryption_key="k", authorize=lambda _r: True)
    app = sms.asgi_app()
    inbox: list[dict[str, Any]] = [
        {"type": "lifespan.startup"},
        {"type": "lifespan.shutdown"},
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return inbox.pop(0)

    async def send(message: Any) -> None:
        sent.append(dict(message))

    await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    assert [m["type"] for m in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
    assert store.claims == 0
    assert sms._start_task is None


# --- request mapping ---


async def test_query_headers_and_cookies_reach_authorize_flattened(
    fake_goto: FakeGoTo, cleanups: Cleanups
) -> None:
    seen: list[AuthorizeRequest] = []

    def authorize(req: AuthorizeRequest) -> bool:
        seen.append(req)
        return True

    _, client = await mounted(fake_goto, cleanups, authorize=authorize)
    res = await client.get(
        "/goto-sms/api/status?a=1&a=2&b=",
        headers=[("x-dup", "1"), ("x-dup", "2"), ("cookie", 'sid=abc; theme="dark"')],
    )
    assert res.status_code == 200
    req = seen[-1]
    assert (req.method, req.path) == ("GET", "/api/status")
    assert req.query == {"a": "1", "b": ""}
    assert req.headers["x-dup"] == "1, 2"
    assert req.cookies == {"sid": "abc", "theme": "dark"}
    # A cookie header SimpleCookie can't parse is no cookies, never a 500.
    bad = await client.get("/goto-sms/api/status", headers={"cookie": 'a="unterminated'})
    assert bad.status_code == 200
