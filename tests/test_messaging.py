import aiohttp
import pytest
from aiohttp import web

import asyncio

from aiogotoconnect.exceptions import GoToApiError, GoToAuthError, GoToError
from aiogotoconnect.messaging import GoToMessaging

OWNER = "+15550000001"
CONTACTS = ["+15550000002", "+15550000003"]


async def static_token() -> str:
    return "TOKEN-1"


def sms_app(calls: list, status: int = 200, payload: dict | None = None) -> web.Application:
    """Fake Messaging v2 endpoint; records each request for the test to inspect."""

    async def handler(request: web.Request) -> web.Response:
        calls.append({"authorization": request.headers.get("Authorization"), "json": await request.json()})
        body = payload if payload is not None else {"messageId": "M1"}
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_post("/messaging/v1/messages", handler)
    return app


async def test_sends_bearer_auth_and_exact_body(aiohttp_server):
    calls: list = []
    server = await aiohttp_server(sms_app(calls))
    client = GoToMessaging(static_token, base_url=str(server.make_url("")).rstrip("/"))

    await client.async_send_sms(OWNER, CONTACTS, "hello")

    assert calls[0]["authorization"] == "Bearer TOKEN-1"
    assert calls[0]["json"] == {
        "ownerPhoneNumber": OWNER,
        "contactPhoneNumbers": CONTACTS,
        "body": "hello",
    }


async def test_success_returns_parsed_json(aiohttp_server):
    server = await aiohttp_server(sms_app([], payload={"messageId": "M1", "status": "sent"}))
    client = GoToMessaging(static_token, base_url=str(server.make_url("")).rstrip("/"))

    assert await client.async_send_sms(OWNER, CONTACTS, "hello") == {
        "messageId": "M1",
        "status": "sent",
    }


async def test_uses_injected_session(aiohttp_server):
    calls: list = []
    server = await aiohttp_server(sms_app(calls))
    session = aiohttp.ClientSession()
    client = GoToMessaging(static_token, session=session, base_url=str(server.make_url("")).rstrip("/"))

    try:
        await client.async_send_sms(OWNER, CONTACTS, "hello")
    finally:
        await session.close()

    assert len(calls) == 1


async def test_401_raises_gotoautherror(aiohttp_server):
    server = await aiohttp_server(sms_app([], status=401, payload={"error": "invalid_token"}))
    client = GoToMessaging(static_token, base_url=str(server.make_url("")).rstrip("/"))

    with pytest.raises(GoToAuthError):
        await client.async_send_sms(OWNER, CONTACTS, "hello")


@pytest.mark.parametrize("status", [400, 403, 500])
async def test_other_error_status_raises_gotoapierror_with_status(aiohttp_server, status):
    server = await aiohttp_server(sms_app([], status=status, payload={"error": "nope"}))
    client = GoToMessaging(static_token, base_url=str(server.make_url("")).rstrip("/"))

    with pytest.raises(GoToApiError) as caught:
        await client.async_send_sms(OWNER, CONTACTS, "hello")

    assert caught.value.status == status


async def test_connection_dropped_raises_gotoerror(aiohttp_server):
    async def drop_handler(request: web.Request) -> web.Response:
        request.transport.close()  # no response at all -> aiohttp ClientError
        return web.Response()

    app = web.Application()
    app.router.add_post("/messaging/v1/messages", drop_handler)
    server = await aiohttp_server(app)
    client = GoToMessaging(static_token, base_url=str(server.make_url("")).rstrip("/"))

    with pytest.raises(GoToError) as caught:
        await client.async_send_sms(OWNER, CONTACTS, "hello")

    assert not isinstance(caught.value, GoToApiError)
    assert isinstance(caught.value.__cause__, aiohttp.ClientError)


async def test_timeout_raises_gotoerror(aiohttp_server):
    async def slow_handler(request: web.Request) -> web.Response:
        await asyncio.sleep(1)
        return web.json_response({"messageId": "M1"})

    app = web.Application()
    app.router.add_post("/messaging/v1/messages", slow_handler)
    server = await aiohttp_server(app)
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.05))
    client = GoToMessaging(
        static_token, session=session, base_url=str(server.make_url("")).rstrip("/")
    )

    try:
        with pytest.raises(GoToError) as caught:
            await client.async_send_sms(OWNER, CONTACTS, "hello")
    finally:
        await session.close()

    assert isinstance(caught.value.__cause__, (asyncio.TimeoutError, aiohttp.ServerTimeoutError))


@pytest.mark.parametrize(
    "body, content_type",
    [
        ("", "application/json"),  # empty 200 body
        ("", "text/plain"),  # empty body, wrong content type
        ("accepted", "text/plain"),  # non-JSON 200 body
    ],
)
async def test_empty_or_non_json_success_body_returns_empty_dict(aiohttp_server, body, content_type):
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=body, status=200, content_type=content_type)

    app = web.Application()
    app.router.add_post("/messaging/v1/messages", handler)
    server = await aiohttp_server(app)
    client = GoToMessaging(static_token, base_url=str(server.make_url("")).rstrip("/"))

    assert await client.async_send_sms(OWNER, CONTACTS, "hello") == {}


async def test_owner_equal_to_contact_fails_fast_without_request(aiohttp_server):
    calls: list = []
    server = await aiohttp_server(sms_app(calls))
    client = GoToMessaging(static_token, base_url=str(server.make_url("")).rstrip("/"))

    with pytest.raises(ValueError):
        await client.async_send_sms(OWNER, [OWNER], "hello")

    assert calls == []
