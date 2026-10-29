"""Port of packages/node/test/directory.test.ts (C6, C7) plus tests of the HTTP wrapper."""

import logging
from collections.abc import AsyncIterator

import aiohttp
import pytest

from aiogotoconnect.sms.directory import GoToAccount, GoToNumber, list_accounts, list_numbers
from aiogotoconnect.sms.errors import GoToApiError, LoginExpiredError, TemporaryError
from aiogotoconnect.sms.http import GoToHttp, create_goto_http, error_name

from .fake_goto import FakeGoTo, FakeResponse

EXT = "urn:scim:schemas:extension:getgo:1.0"
LOGGER = logging.getLogger("test")


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as s:
        yield s


def make_http(
    session: aiohttp.ClientSession, fake: FakeGoTo, *, timeout_s: float = 10.0
) -> GoToHttp:
    async def get_token() -> str:
        return "tok"

    fake.tokens.valid.add("tok")
    return create_goto_http(session, fake.api_base_url, get_token, get_token, timeout_s, LOGGER)


async def test_c6_accounts_from_scim_extension(
    session: aiohttp.ClientSession, fake_goto: FakeGoTo
) -> None:
    fake_goto.me = {
        EXT: {"accounts": [{"value": "a1", "display": "Acme"}, {"value": "a2"}, {"display": "x"}]}
    }
    assert await list_accounts(make_http(session, fake_goto)) == [
        GoToAccount("a1", "Acme"),
        GoToAccount("a2", "a2"),  # display missing: falls back to the key
    ]


async def test_c6_missing_extension_gives_empty(
    session: aiohttp.ClientSession, fake_goto: FakeGoTo
) -> None:
    fake_goto.me = {}
    assert await list_accounts(make_http(session, fake_goto)) == []


async def test_c7_numbers_follow_page_marker_and_dedupe(
    session: aiohttp.ClientSession, fake_goto: FakeGoTo
) -> None:
    fake_goto.numbers_pages = [
        [{"number": "+18015550101"}, {"number": "+18015550102"}],
        [{"number": "+18015550102"}, {"number": "+18015550103"}],  # 0102 repeats
        [{"number": "+18015550104"}],
    ]
    numbers = await list_numbers(make_http(session, fake_goto), "acct-1")
    assert [n.number for n in numbers] == [
        "+18015550101",
        "+18015550102",
        "+18015550103",
        "+18015550104",
    ]
    calls = [r for r in fake_goto.requests if r.path.startswith("/voice-admin/")]
    assert len(calls) == 3
    assert "accountKey=acct-1" in calls[0].path and "pageSize=100" in calls[0].path
    assert "pageMarker" not in calls[0].path
    assert "pageMarker=2" in calls[2].path


async def test_numbers_normalized_sms_enabled_passes_through_bad_skipped(
    session: aiohttp.ClientSession, fake_goto: FakeGoTo
) -> None:
    fake_goto.numbers_pages = [
        [
            {"number": "(801) 555-0101", "name": "Main", "smsEnabled": True},
            {"number": "801-555-0102", "callerIdName": "Front desk", "smsEnabled": False},
            {"number": "801-555-0103"},
            {"number": "garbage"},
            {},
        ]
    ]
    assert await list_numbers(make_http(session, fake_goto), "acct-1") == [
        GoToNumber("+18015550101", "Main", "acct-1", True),
        GoToNumber("+18015550102", "Front desk", "acct-1", False),
        GoToNumber("+18015550103", "", "acct-1"),
    ]


async def test_number_as_name_is_not_a_label(
    session: aiohttp.ClientSession, fake_goto: FakeGoTo
) -> None:
    fake_goto.numbers_pages = [[{"number": "+18015550101", "name": "801-555-0101"}]]
    (n,) = await list_numbers(make_http(session, fake_goto), "acct-1")
    assert n.name == ""


async def test_page_cap_returns_what_was_collected(
    session: aiohttp.ClientSession, fake_goto: FakeGoTo
) -> None:
    fake_goto.numbers_pages = [[{"number": f"+1801555{1000 + i}"}] for i in range(60)]
    assert len(await list_numbers(make_http(session, fake_goto), "acct-1")) == 50


# --- GoToHttp (http.ts behaviours) ---


async def test_http_401_refreshes_once_and_retries(
    session: aiohttp.ClientSession, fake_goto: FakeGoTo
) -> None:
    fake_goto.tokens.valid.add("fresh")
    calls: list[str] = []

    async def get_token() -> str:
        return "stale"

    async def force_refresh() -> str:
        calls.append("refresh")
        return "fresh"

    http = create_goto_http(session, fake_goto.api_base_url, get_token, force_refresh, 5, LOGGER)
    assert await http.json("GET", "/identity/v1/Users/me") == fake_goto.me
    assert calls == ["refresh"]
    assert fake_goto.requests[1].headers["authorization"] == "Bearer fresh"


async def test_http_second_401_is_login_expired(
    session: aiohttp.ClientSession, fake_goto: FakeGoTo
) -> None:
    async def bad() -> str:
        return "nope"

    http = create_goto_http(session, fake_goto.api_base_url, bad, bad, 5, LOGGER)
    with pytest.raises(LoginExpiredError):
        await http.json("GET", "/identity/v1/Users/me")
    assert len(fake_goto.requests) == 2


async def test_http_non_2xx_is_api_error_with_truncated_body(
    session: aiohttp.ClientSession, fake_goto: FakeGoTo
) -> None:
    fake_goto.fail_next("GET", "/x", 500, "y" * 900)
    with pytest.raises(GoToApiError) as info:
        await make_http(session, fake_goto).json("GET", "/x")
    assert info.value.status == 500 and len(info.value.body) == 500


async def test_http_204_and_empty_body_give_empty_dict_and_bad_json_is_fixed_text(
    session: aiohttp.ClientSession, fake_goto: FakeGoTo
) -> None:
    http = make_http(session, fake_goto)
    fake_goto.fail_next("DELETE", "/a", 204)
    assert await http.json("DELETE", "/a") == {}
    fake_goto.fail_next("GET", "/b", 200)
    assert await http.json("GET", "/b") == {}
    fake_goto.fail_next("GET", "/c", 200, "{not json 555-0100")
    with pytest.raises(GoToApiError) as info:
        await http.json("GET", "/c")
    assert info.value.status == 200
    assert "555" not in info.value.body and "Malformed" in info.value.body


async def test_http_posts_json_body(session: aiohttp.ClientSession, fake_goto: FakeGoTo) -> None:
    fake_goto.route("POST", "/echo", lambda r: FakeResponse(200, {"got": r.body}))
    res = await make_http(session, fake_goto).json("POST", "/echo", {"a": 1})
    assert res == {"got": '{"a":1}'}
    assert fake_goto.requests[0].headers["content-type"] == "application/json"


async def test_http_timeout_is_temporary_request_sent(
    session: aiohttp.ClientSession, fake_goto: FakeGoTo
) -> None:
    fake_goto.hang_next("GET", "/slow")
    http = make_http(session, fake_goto, timeout_s=0.1)
    with pytest.raises(TemporaryError) as info:
        await http.json("GET", "/slow")
    assert info.value.request_sent is True
    assert "TimeoutError" in str(info.value)


async def test_http_connection_refused_is_temporary(session: aiohttp.ClientSession) -> None:
    async def tok() -> str:
        return "t"

    http = create_goto_http(session, "http://127.0.0.1:1", tok, tok, 2, LOGGER)
    with pytest.raises(TemporaryError) as info:
        await http.json("GET", "/x")
    assert info.value.request_sent is True


async def test_http_token_errors_propagate_unchanged(
    session: aiohttp.ClientSession, fake_goto: FakeGoTo
) -> None:
    async def boom() -> str:
        raise TemporaryError("no token")

    http = create_goto_http(session, fake_goto.api_base_url, boom, boom, 5, LOGGER)
    with pytest.raises(TemporaryError, match="no token"):
        await http.json("GET", "/x")
    assert fake_goto.requests == []


def test_error_name() -> None:
    assert error_name(TimeoutError()) == "TimeoutError"


# --- the fake itself ---


async def test_fake_routes_poll_and_subscriptions(
    session: aiohttp.ClientSession, fake_goto: FakeGoTo
) -> None:
    fake_goto.inbound = [
        {
            "id": "m1",
            "from": "+1801",
            "to": "+1000",
            "body": "hi",
            "timestamp": "2026-01-01T00:00:00Z",
        },
        {
            "id": "m2",
            "from": "+1801",
            "to": "+1000",
            "body": "yo",
            "timestamp": "2026-01-02T00:00:00.123Z",
        },
    ]
    http = make_http(session, fake_goto)
    conv = await http.json(
        "GET", "/messaging/v1/conversations", query={"ownerPhoneNumber": "+1000"}
    )
    assert conv["items"][0]["lastMessageTimestamp"] == "2026-01-02T00:00:00.123Z"
    msgs = await http.json(
        "GET",
        "/messaging/v1/messages",
        query={"ownerPhoneNumber": "+1000", "contactPhoneNumber": "+1801"},
    )
    assert [m["id"] for m in msgs["items"]] == ["m2", "m1"]  # reversed on purpose
    sub = await http.json("POST", "/messaging/v2/subscriptions", {"accountKey": "a"})
    assert sub["id"] == "sub-1"
    await http.json("DELETE", "/messaging/v2/subscriptions/sub-1")
    with pytest.raises(GoToApiError) as info:
        await http.json("DELETE", "/messaging/v2/subscriptions/sub-1")
    assert info.value.status == 404
