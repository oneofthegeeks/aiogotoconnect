"""Port of packages/node/test/webhook.test.ts (C16, C17, C18)."""

import copy
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

from aiogotoconnect.sms.errors import GoToApiError
from aiogotoconnect.sms.http import GoToHttp, create_goto_http
from aiogotoconnect.sms.inbound.webhook import (
    check_webhook_secret,
    disable_webhook,
    enable_webhook,
    ensure_webhook_secret,
    get_webhook_account,
    get_webhook_secret,
    parse_webhook_envelope,
)
from aiogotoconnect.sms.store.memory import MemoryStore

from .fake_goto import FakeGoTo, FakeRequest

URL = "https://example.test/goto/webhook"
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass
class Env:
    fake: FakeGoTo
    http: GoToHttp
    store: MemoryStore

    async def enable(self, **over: Any) -> dict[str, str]:
        args: dict[str, Any] = {
            "http": self.http,
            "store": self.store,
            "tenant": "t",
            "account_key": "acct-1",
            "webhook_url": URL,
            "channel_prefix": "goto-sms",
        }
        return await enable_webhook(**{**args, **over})

    async def disable(self, **over: Any) -> None:
        await disable_webhook(self.http, self.store, "t", **over)


@pytest.fixture
async def env(fake_goto: FakeGoTo) -> AsyncIterator[Env]:
    fake_goto.tokens.valid.add("tok")

    async def token() -> str:
        return "tok"

    async with aiohttp.ClientSession() as session:
        http = create_goto_http(
            session, fake_goto.api_base_url, token, token, 10.0, logging.getLogger("test")
        )
        yield Env(fake_goto, http, MemoryStore())


def channel_posts(fake: FakeGoTo) -> list[FakeRequest]:
    return [
        r
        for r in fake.requests
        if r.method == "POST" and r.pathname.startswith("/notification-channel/v1/channels/")
    ]


def sub_posts(fake: FakeGoTo) -> list[FakeRequest]:
    return [
        r
        for r in fake.requests
        if r.method == "POST" and r.pathname.startswith("/messaging/v2/subscriptions")
    ]


async def test_c17_creates_webhook_channel_then_reuses_a_matching_one(env: Env) -> None:
    await env.enable()
    assert len(channel_posts(env.fake)) == 1
    assert channel_posts(env.fake)[0].pathname.startswith(
        "/notification-channel/v1/channels/goto-sms-"
    )
    assert json.loads(channel_posts(env.fake)[0].body) == {
        "channelType": "Webhook",
        "webhookChannelData": {"webhook": {"url": URL}},
    }

    await env.enable()
    assert len(channel_posts(env.fake)) == 1, "matching channel reused"
    assert len(env.fake.channels) == 1


async def test_c18_subscription_has_account_key_in_query_and_body_and_is_reused(env: Env) -> None:
    result = await env.enable()
    assert result == {"channel_id": "ch-1", "subscription_id": "sub-1"}
    post = sub_posts(env.fake)[0]
    assert parse_qs(urlsplit(post.path).query)["accountKey"] == ["acct-1"]
    assert json.loads(post.body) == {
        "accountKey": "acct-1",
        "ownerEntity": "acct-1",
        "ownerEntityType": "ACCOUNT",
        "channelId": "ch-1",
        "eventTypes": ["INBOUND_MESSAGE"],
    }
    assert await env.store.get("t", "inbound:channelId") == "ch-1"
    assert await env.store.get("t", "inbound:subscriptionId") == "sub-1"
    assert await get_webhook_account(env.store, "t") == "acct-1"

    assert await env.enable() == result
    assert len(sub_posts(env.fake)) == 1, "no second POST"


async def test_c18_a_4xx_retries_with_organization(env: Env) -> None:
    env.fake.fail_next("POST", "/messaging/v2/subscriptions", 400, {"error": "bad owner"})
    await env.enable()
    assert len(sub_posts(env.fake)) == 2
    assert json.loads(sub_posts(env.fake)[1].body)["ownerEntityType"] == "ORGANIZATION"
    assert env.fake.subscriptions[0]["ownerEntityType"] == "ORGANIZATION"


async def test_c18_a_403_on_both_shapes_carries_the_goto_body(env: Env) -> None:
    env.fake.fail_next("POST", "/messaging/v2/subscriptions", 403, {"error": "first"})
    env.fake.fail_next("POST", "/messaging/v2/subscriptions", 403, {"message": "missing scope"})
    with pytest.raises(GoToApiError) as info:
        await env.enable()
    assert info.value.status == 403
    assert "missing scope" in info.value.body


async def test_a_5xx_on_the_first_subscription_post_is_not_retried(env: Env) -> None:
    env.fake.fail_next("POST", "/messaging/v2/subscriptions", 500, {})
    with pytest.raises(GoToApiError):
        await env.enable()
    assert len(sub_posts(env.fake)) == 1


async def test_enable_requires_an_account_key(env: Env) -> None:
    with pytest.raises(ValueError, match="account_key is required"):
        await env.enable(account_key="")


async def test_enable_generates_the_secret_once_and_disable_clears_ids_and_rotates(
    env: Env,
) -> None:
    await env.enable()
    secret = await env.store.get("t", "inbound:webhookSecret")
    assert isinstance(secret, str)
    assert SECRET_RE.fullmatch(secret)
    await env.enable()
    assert await env.store.get("t", "inbound:webhookSecret") == secret, "stable across enables"
    assert await get_webhook_secret(env.store, "t") == secret

    await env.disable()
    assert env.fake.subscriptions == []
    assert await env.store.get("t", "inbound:channelId") is None
    assert await env.store.get("t", "inbound:subscriptionId") is None
    assert await get_webhook_account(env.store, "t") is None
    rotated = await env.store.get("t", "inbound:webhookSecret")
    assert isinstance(rotated, str)
    assert SECRET_RE.fullmatch(rotated)
    assert rotated != secret


async def test_disable_can_keep_the_secret(env: Env) -> None:
    await env.enable()
    secret = await get_webhook_secret(env.store, "t")
    await env.disable(rotate_secret=False)
    assert await get_webhook_secret(env.store, "t") == secret


async def test_re_enable_after_disable_makes_a_new_channel_for_the_rotated_url(env: Env) -> None:
    async def url_for() -> str:
        return f"{URL}/{await ensure_webhook_secret(env.store, 't')}"

    first = await url_for()
    assert await url_for() == first, "ensure_webhook_secret is stable"
    await env.enable(webhook_url=first)
    await env.disable()
    second = await url_for()
    assert second != first
    await env.enable(webhook_url=second)
    assert len(channel_posts(env.fake)) == 2
    assert env.fake.channels[1]["webhookChannelData"]["webhook"]["url"] == second
    assert len(env.fake.subscriptions) == 1
    assert env.fake.subscriptions[0]["channelId"] == env.fake.channels[1]["channelId"]


async def test_disable_ignores_a_404_and_works_when_never_enabled(env: Env) -> None:
    await env.enable()
    env.fake.subscriptions.clear()  # gone on GoTo's side already
    await env.disable()
    assert await env.store.get("t", "inbound:subscriptionId") is None
    await env.disable()  # nothing stored: no raise


async def test_disable_propagates_a_non_404_failure_and_keeps_the_ids(env: Env) -> None:
    await env.enable()
    env.fake.fail_next("DELETE", "/messaging/v2/subscriptions/", 500, {})
    with pytest.raises(GoToApiError):
        await env.disable()
    assert await env.store.get("t", "inbound:subscriptionId") == "sub-1"


def test_c16_parse_party_invites_envelope() -> None:
    body = {
        "type": "INBOUND_MESSAGE",
        "content": {
            "payload": {
                "id": "m-1",
                "direction": "INBOUND",
                "authorTouchpoint": {"phoneNumber": "(801) 555-0123"},
                "ownerPhoneNumber": "+18015550199",
                "body": "  YES 3  ",
                "timestamp": "2026-10-27T10:00:00Z",
            }
        },
    }
    msg = parse_webhook_envelope(body)
    assert msg is not None
    assert (msg.id, msg.from_, msg.to, msg.text, msg.received_at) == (
        "m-1",
        "+18015550123",
        "+18015550199",
        "YES 3",
        "2026-10-27T10:00:00Z",
    )
    assert msg.raw is body
    assert msg.keyword is None
    assert msg.account_key is None


def test_c16_parse_id_falls_back_to_a_stable_sha256() -> None:
    body = {
        "payload": {"from": "+18015550123", "to": "+18015550199", "text": "hi", "timestamp": "T1"}
    }
    first = parse_webhook_envelope(body)
    assert first is not None
    assert re.fullmatch(r"[0-9a-f]{64}", first.id)
    again = parse_webhook_envelope(copy.deepcopy(body))
    assert again is not None
    assert again.id == first.id
    other = parse_webhook_envelope({"payload": {**body["payload"], "text": "bye"}})
    assert other is not None
    assert other.id != first.id


def test_c16_parse_without_a_timestamp_uses_now() -> None:
    msg = parse_webhook_envelope({"payload": {"from": "+18015550123", "body": "x"}})
    assert msg is not None
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", msg.received_at)


def test_c16_parse_wrong_type_direction_and_garbage_give_none() -> None:
    assert (
        parse_webhook_envelope(
            {"type": "OUTBOUND_MESSAGE", "payload": {"from": "+18015550123", "body": "x"}}
        )
        is None
    )
    assert (
        parse_webhook_envelope(
            {"payload": {"direction": "OUTBOUND", "from": "+18015550123", "body": "x"}}
        )
        is None
    )
    garbage: list[object] = [
        None,
        42,
        "str",
        [],
        {},
        {"payload": 5},
        {"content": {"payload": None}},
        {"payload": {"from": {}, "body": {}}},
        {"payload": {"from": "not a phone", "body": "x"}},
        {"payload": {"from": "+18015550123", "body": "   "}},
        {"payload": {"body": "x", "contactTouchpoints": 7}},
        {"payload": {"from": 5, "body": 5, "contactTouchpoints": [None, 3]}},
        {"payload": {"from": "+18015550123", "body": "x", "direction": ["INBOUND"]}},
    ]
    for g in garbage:
        assert parse_webhook_envelope(g) is None, g


def test_c16_parse_text_is_trimmed_and_capped_at_500() -> None:
    msg = parse_webhook_envelope({"payload": {"from": "+18015550123", "body": "a" * 600}})
    assert msg is not None
    assert len(msg.text) == 500


def test_check_webhook_secret() -> None:
    assert check_webhook_secret("abc123", "abc123") is True
    assert check_webhook_secret("abc124", "abc123") is False
    assert check_webhook_secret("abc", "abc123") is False
    assert check_webhook_secret("", "") is False, "an empty secret never authenticates"
    assert check_webhook_secret("é", "e") is False
