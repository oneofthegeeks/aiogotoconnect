import asyncio
import base64

import aiohttp
import pytest
from aiohttp import web

from gotoconnect.auth import GoToOAuth
from gotoconnect.exceptions import GoToAuthError


def token_app(calls: list, status: int = 200, payload: dict | None = None) -> web.Application:
    """Fake OAuth token endpoint; records each request for the test to inspect."""

    async def handler(request: web.Request) -> web.Response:
        form = await request.post()
        calls.append({"authorization": request.headers.get("Authorization"), "form": dict(form)})
        body = payload or {"access_token": "AT1", "refresh_token": "RT2", "expires_in": 3600}
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_post("/oauth/token", handler)
    return app


async def test_refreshes_token_with_basic_auth(aiohttp_server):
    calls: list = []
    server = await aiohttp_server(token_app(calls))
    oauth = GoToOAuth("id", "secret", "RT1", token_url=str(server.make_url("/oauth/token")))

    assert await oauth.async_get_access_token() == "AT1"
    assert oauth.refresh_token == "RT2"

    expected_basic = base64.b64encode(b"id:secret").decode()
    assert calls[0]["authorization"] == f"Basic {expected_basic}"
    assert calls[0]["form"] == {"grant_type": "refresh_token", "refresh_token": "RT1"}


async def test_caches_until_near_expiry(aiohttp_server):
    calls: list = []
    server = await aiohttp_server(token_app(calls))
    oauth = GoToOAuth("id", "secret", "RT1", token_url=str(server.make_url("/oauth/token")))

    await oauth.async_get_access_token()
    await oauth.async_get_access_token()

    assert len(calls) == 1


async def test_http_error_raises_gotoautherror(aiohttp_server):
    calls: list = []
    server = await aiohttp_server(token_app(calls, status=401, payload={"error": "invalid_grant"}))
    oauth = GoToOAuth("id", "secret", "RT1", token_url=str(server.make_url("/oauth/token")))

    with pytest.raises(GoToAuthError):
        await oauth.async_get_access_token()


async def test_concurrent_gets_on_expired_token_refresh_once(aiohttp_server):
    calls: list = []

    async def slow_handler(request: web.Request) -> web.Response:
        calls.append(await request.post())
        await asyncio.sleep(0.05)  # keep the first refresh in flight while the second call arrives
        return web.json_response({"access_token": "AT1", "refresh_token": "RT2", "expires_in": 3600})

    app = web.Application()
    app.router.add_post("/oauth/token", slow_handler)
    server = await aiohttp_server(app)
    oauth = GoToOAuth("id", "secret", "RT1", token_url=str(server.make_url("/oauth/token")))

    tokens = await asyncio.gather(oauth.async_get_access_token(), oauth.async_get_access_token())

    assert tokens == ["AT1", "AT1"]
    assert len(calls) == 1


async def test_connection_dropped_raises_gotoautherror(aiohttp_server):
    async def drop_handler(request: web.Request) -> web.Response:
        request.transport.close()  # no response at all -> aiohttp ClientError
        return web.Response()

    app = web.Application()
    app.router.add_post("/oauth/token", drop_handler)
    server = await aiohttp_server(app)
    oauth = GoToOAuth("id", "secret", "RT1", token_url=str(server.make_url("/oauth/token")))

    with pytest.raises(GoToAuthError):
        await oauth.async_get_access_token()


async def test_timeout_raises_gotoautherror(aiohttp_server):
    async def slow_handler(request: web.Request) -> web.Response:
        await asyncio.sleep(1)
        return web.json_response({"access_token": "AT1", "expires_in": 3600})

    app = web.Application()
    app.router.add_post("/oauth/token", slow_handler)
    server = await aiohttp_server(app)
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.05))
    oauth = GoToOAuth(
        "id", "secret", "RT1", token_url=str(server.make_url("/oauth/token")), session=session
    )

    try:
        with pytest.raises(GoToAuthError):
            await oauth.async_get_access_token()
    finally:
        await session.close()


@pytest.mark.parametrize(
    "body, content_type",
    [
        ("<html>maintenance</html>", "text/html"),  # wrong content type -> ContentTypeError
        ("not json", "application/json"),  # invalid body -> JSONDecodeError
        ('{"refresh_token": "RT2", "expires_in": 3600}', "application/json"),  # no access_token
        ('{"access_token": "AT1", "refresh_token": "RT2"}', "application/json"),  # no expires_in
        ("[]", "application/json"),  # JSON, but not an object
    ],
)
async def test_malformed_token_response_raises_gotoautherror(aiohttp_server, body, content_type):
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=body, content_type=content_type)

    app = web.Application()
    app.router.add_post("/oauth/token", handler)
    server = await aiohttp_server(app)
    oauth = GoToOAuth("id", "secret", "RT1", token_url=str(server.make_url("/oauth/token")))

    with pytest.raises(GoToAuthError):
        await oauth.async_get_access_token()
