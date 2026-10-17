import aiohttp
import pytest
from aiohttp import web

from gotoconnect.exceptions import GoToApiError, GoToAuthError
from gotoconnect.messaging import GoToMessaging

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


async def test_owner_equal_to_contact_fails_fast_without_request(aiohttp_server):
    calls: list = []
    server = await aiohttp_server(sms_app(calls))
    client = GoToMessaging(static_token, base_url=str(server.make_url("")).rstrip("/"))

    with pytest.raises(ValueError):
        await client.async_send_sms(OWNER, [OWNER], "hello")

    assert calls == []
