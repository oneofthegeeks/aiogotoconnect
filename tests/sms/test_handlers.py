"""Port of packages/node/test/handlers.test.ts (C3, C22-C25 and the contract routes), driving the
framework-free handlers directly with HttpRequest objects, plus Python-side checks of the
wire-format conversions and the deferred (startup-failed) handlers from contract/http.md."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from aiogotoconnect.sms.core import AuthorizeRequest, BackLink, Core, ResolvedOptions
from aiogotoconnect.sms.errors import GoToSmsError, NotConfiguredError
from aiogotoconnect.sms.log import iso_ms
from aiogotoconnect.sms.server.handlers import (
    Handler,
    HttpRequest,
    HttpResponse,
    assert_setup_auth,
    create_deferred_handlers,
    create_handlers,
)
from aiogotoconnect.sms.server.status import (
    REQUIRED_SCOPES,
    build_status,
    startup_error_text,
)
from aiogotoconnect.sms.types import VERSION

from .fake_goto import FakeGoTo
from .helpers import FROM, PEER, SmsRig, connected_core, recorder, state_of

HTML = "<!doctype html><title>setup</title>"
NO_AUTH = (
    "goto-sms: pass authorize(req) to create_goto_sms "
    "(or allow_unauthenticated_setup=True in development)"
)


def allow(_r: AuthorizeRequest) -> bool:
    return True


def deny(_r: AuthorizeRequest) -> bool:
    return False


@dataclass
class Env:
    fake: FakeGoTo
    make: Callable[..., Awaitable[SmsRig]]

    async def setup(self, **kw: Any) -> tuple[SmsRig, Handler]:
        """A connected instance (authorize allows everything unless overridden) + its handlers."""
        kw.setdefault("authorize", allow)
        rig = await self.make(**kw)
        return rig, create_handlers(rig.core, HTML)


@pytest.fixture
async def env(fake_goto: FakeGoTo) -> AsyncIterator[Env]:
    rigs: list[SmsRig] = []

    async def make(**kw: Any) -> SmsRig:
        rig = await connected_core(fake_goto, **kw)
        rigs.append(rig)
        return rig

    try:
        yield Env(fake_goto, make)
    finally:
        for rig in reversed(rigs):
            await rig.close()


def req(
    method: str,
    path: str,
    *,
    body: object = None,
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> HttpRequest:
    """A same-origin request for the default public_url host, unless headers override it."""
    h = {"host": "app.example"}
    if method != "GET":
        h["origin"] = "https://app.example"
    h.update(headers or {})
    return HttpRequest(
        method=method,
        path=path,
        query=query or {},
        headers=h,
        cookies=cookies or {},
        body=body,
    )


API_ROUTES: list[tuple[str, str]] = [
    ("GET", "/"),
    ("GET", "/api/status"),
    ("POST", "/api/credentials"),
    ("POST", "/api/connect"),
    ("POST", "/api/disconnect"),
    ("GET", "/api/accounts"),
    ("GET", "/api/numbers"),
    ("POST", "/api/selection"),
    ("POST", "/api/test"),
    ("POST", "/api/incoming"),
    ("GET", "/api/log"),
    ("GET", "/api/opt-outs"),
    ("POST", "/api/opt-outs"),
]


async def webhook_mode(core: Core, secret: str = "S" * 43) -> str:
    """Puts the instance in webhook mode with a known secret, without calling GoTo."""
    await core.store.set(core.tenant, "inbound:mode", "webhook")
    await core.store.set(core.tenant, "inbound:webhookSecret", secret)
    return secret


def envelope(id_: str, text: str = "hello there", owner: str = FROM) -> dict[str, Any]:
    return {
        "type": "INBOUND_MESSAGE",
        "content": {
            "payload": {
                "id": id_,
                "direction": "INBOUND",
                "authorTouchpoint": PEER,
                "ownerPhoneNumber": owner,
                "body": text,
                "timestamp": "2026-10-27T10:00:00Z",
            }
        },
    }


async def pending(core: Core, id_: str) -> Any:
    return await core.store.get(core.tenant, f"inbound:pending:{id_}")


async def settle() -> None:
    """Lets post-204 background work start (Node: one setImmediate)."""
    for _ in range(3):
        await asyncio.sleep(0)


# --- authorization (C23) -------------------------------------------------------------------


async def test_c23_without_authorize_raises_authorize_false_is_403_callback_and_inbound_bypass(
    env: Env,
) -> None:
    bare = await env.make(connect=False)
    with pytest.raises(RuntimeError) as exc:
        create_handlers(bare.core, HTML)
    assert str(exc.value) == NO_AUTH

    prod = await env.make(
        connect=False, allow_unauthenticated_setup=True, env={"GOTO_SMS_ENV": "production"}
    )
    with pytest.raises(RuntimeError) as exc:
        create_handlers(prod.core, HTML)
    assert str(exc.value) == NO_AUTH

    dev = await env.make(connect=False, allow_unauthenticated_setup=True)
    dev_handle = create_handlers(dev.core, HTML)
    assert (await dev_handle(req("GET", "/api/status"))).status == 200

    seen: list[AuthorizeRequest] = []

    def refuse(r: AuthorizeRequest) -> bool:
        seen.append(r)
        return False

    rig = await env.make(authorize=refuse)
    handle = create_handlers(rig.core, HTML)
    for method, path in API_ROUTES:
        r = await handle(req(method, path, body={}))
        assert r.status == 403, f"{method} {path}"
    assert len(seen) == len(API_ROUTES), "authorize got the request each time"

    # /callback answers with a redirect, /inbound with 204: neither asked authorize.
    cb = await handle(req("GET", "/callback", query={"error": "access_denied"}))
    assert cb.redirect == "./?error=goto_login"
    secret = await webhook_mode(rig.core)
    inbound = await handle(req("POST", f"/inbound/{secret}", body=envelope("g-c23")))
    assert inbound.status == 204
    assert len(seen) == len(API_ROUTES)


def test_assert_setup_auth_rules() -> None:
    assert_setup_auth(ResolvedOptions(authorize=allow))
    assert_setup_auth(ResolvedOptions(authorize=allow, env={"GOTO_SMS_ENV": "production"}))
    assert_setup_auth(ResolvedOptions(allow_unauthenticated_setup=True))
    for bad in (
        ResolvedOptions(),
        ResolvedOptions(allow_unauthenticated_setup=True, env={"GOTO_SMS_ENV": "production"}),
    ):
        with pytest.raises(RuntimeError, match=r"pass authorize\(req\) to create_goto_sms"):
            assert_setup_auth(bad)


async def test_authorize_gets_a_read_only_request_and_may_be_async(env: Env) -> None:
    seen: list[AuthorizeRequest] = []

    async def check(r: AuthorizeRequest) -> bool:
        seen.append(r)
        await asyncio.sleep(0)
        return r.cookies.get("session") == "admin"

    _, handle = await env.setup(authorize=check)
    ok = await handle(req("GET", "/api/log", query={"limit": "5"}, cookies={"session": "admin"}))
    assert ok.status == 200
    refused = await handle(req("GET", "/api/log", cookies={"session": "guest"}))
    assert refused.status == 403
    assert refused.json == {"error": "forbidden", "message": "Not allowed"}
    first = seen[0]
    assert (first.method, first.path) == ("GET", "/api/log")
    assert first.query == {"limit": "5"}
    assert first.headers["host"] == "app.example"
    with pytest.raises(AttributeError):
        first.method = "POST"  # type: ignore[misc]


# --- status (C22) --------------------------------------------------------------------------


async def test_c22_api_status_never_contains_client_secret_or_tokens(env: Env) -> None:
    rig, handle = await env.setup()
    core = rig.core
    await core.sender.send(PEER, "warm the access token")
    secret = await webhook_mode(core)
    r = await handle(req("GET", "/api/status"))
    assert r.status == 200
    s = r.json
    assert s["connected"] is True
    assert s["configured"] is True
    assert s["ready"] is True
    assert s["clientId"] == "cid"
    assert s["fromNumber"] == FROM
    assert s["version"] == VERSION
    assert s["incoming"]["mode"] == "webhook"
    assert s["incoming"]["canWebhook"] is True
    assert s["requiredScopes"] == [
        "messaging.v1.send",
        "messaging.v1.read",
        "messaging.v1.notifications.manage",
        "voice-admin.v1.read",
    ]

    text = json.dumps(s)
    assert "csecret" not in text
    assert env.fake.tokens.refresh not in text
    assert len(env.fake.tokens.valid) > 0
    for token in env.fake.tokens.valid:
        assert token not in text
    # The webhook secret appears only inside webhookUrl (shown to the authorized admin).
    assert s["incoming"]["webhookUrl"] == f"https://app.example/goto-sms/inbound/{secret}"
    assert text.count(secret) == 1

    # sms.status() is built by the same code, minus webhookUrl (R30): hosts may expose it
    # without authorize, and the secret would let anyone post forged inbound texts.
    programmatic = await build_status(core.options, core)
    assert "webhookUrl" not in programmatic["incoming"]
    assert secret not in json.dumps(programmatic)
    incoming = {k: v for k, v in s["incoming"].items() if k != "webhookUrl"}
    assert programmatic == {**s, "incoming": incoming}


async def test_status_without_a_core_reports_not_set_up_with_the_startup_error() -> None:
    # Node: sms.status() answers configured:false/ready:false when startup failed for lack of a
    # key. The facade part (status() catching NotConfiguredError) is Task 11's; this is the
    # payload it returns.
    options = ResolvedOptions(back=BackLink("/admin", "Spring Ideas"))
    err = NotConfiguredError("No encryption key: set GOTO_SMS_ENCRYPTION_KEY")
    s = await build_status(options, None, startup_error_text(err))
    assert s == {
        "version": VERSION,
        "redirectUri": "",
        "requiredScopes": REQUIRED_SCOPES,
        "back": {"url": "/admin", "label": "Spring Ideas"},
        "configured": False,
        "connected": False,
        "loginExpired": False,
        "credentialsUnreadable": False,
        "ready": False,
        "sources": {
            "clientId": "unset",
            "clientSecret": "unset",
            "accountKey": "unset",
            "fromNumber": "unset",
        },
        "incoming": {"mode": "off", "canWebhook": False},
        "startupError": "No encryption key: set GOTO_SMS_ENCRYPTION_KEY",
    }


def test_startup_error_text_names_only_foreign_errors() -> None:
    assert startup_error_text(GoToSmsError("fixed message", "x")) == "fixed message"
    text = startup_error_text(OSError("/secret/path/.goto-sms.key: permission denied"))
    assert text == "goto-sms failed to start (OSError); see the server logs"


async def test_status_wire_omits_unset_fields_and_converts_flags(env: Env) -> None:
    # invalidFromNumber only when true; refreshSource only with stored tokens;
    # tokensUnreadable folds into credentialsUnreadable.
    rig = await env.make(connect=False, env={"GOTO_FROM_NUMBER": "call me"}, authorize=allow)
    s = await build_status(rig.core.options, rig.core)
    assert s["invalidFromNumber"] is True
    assert "fromNumber" not in s
    assert "refreshSource" not in s
    assert "clientId" not in s and "accountKey" not in s
    assert "back" not in s and "startupError" not in s
    assert "error" not in s["incoming"] and "webhookUrl" not in s["incoming"]
    assert s["credentialsUnreadable"] is False

    good = await env.make(authorize=allow)
    g = await build_status(good.core.options, good.core)
    assert "invalidFromNumber" not in g
    assert g["refreshSource"] == "login"
    # A login sealed with another key reads as unreadable.
    tokens = await good.core.store.get(good.core.tenant, "tokens")
    assert isinstance(tokens, dict)
    await good.core.store.set(good.core.tenant, "tokens", {**tokens, "refresh": "v1.x.y.z"})
    u = await build_status(good.core.options, good.core)
    assert u["credentialsUnreadable"] is True
    assert u["connected"] is False


# --- same-origin, redirect URI, connect (C3) ----------------------------------------------


async def test_post_without_matching_origin_is_403(env: Env) -> None:
    _, handle = await env.setup()
    evil = await handle(
        req("POST", "/api/disconnect", body={}, headers={"origin": "https://evil.example"})
    )
    assert evil.status == 403
    assert evil.json["error"] == "cross_origin"
    none = await handle(req("POST", "/api/disconnect", body={}, headers={"origin": ""}))
    assert none.status == 403
    nul = await handle(req("POST", "/api/disconnect", body={}, headers={"origin": "null"}))
    assert nul.status == 403
    cross_site = await handle(
        req(
            "POST",
            "/api/disconnect",
            body={},
            headers={"origin": "", "sec-fetch-site": "cross-site"},
        )
    )
    assert cross_site.status == 403

    # Sec-Fetch-Site: same-origin alone passes; so does Origin matching the public_url host
    # when the proxy rewrote Host.
    fetch_site = await handle(
        req(
            "POST",
            "/api/disconnect",
            body={},
            headers={"origin": "", "sec-fetch-site": "same-origin"},
        )
    )
    assert fetch_site.status == 200
    proxied = await handle(
        req("POST", "/api/disconnect", body={}, headers={"host": "internal:3000"})
    )
    assert proxied.status == 200


async def test_redirect_uri_for_nested_mount_and_public_url(env: Env) -> None:
    _, handle = await env.setup(mount_path="/admin/tools/sms", public_url="https://x.example")
    s = (await handle(req("GET", "/api/status"))).json
    assert s["redirectUri"] == "https://x.example/admin/tools/sms/callback"
    r = await handle(req("POST", "/api/connect", headers={"origin": "https://x.example"}))
    assert r.status == 200
    q = parse_qs(urlsplit(r.json["url"]).query)
    assert q["redirect_uri"] == ["https://x.example/admin/tools/sms/callback"]


async def test_connect_without_a_public_url_is_400_no_public_url(env: Env) -> None:
    _, handle = await env.setup(public_url="")
    r = await handle(req("POST", "/api/connect", body={}))
    assert r.status == 400
    assert r.json["error"] == "no_public_url"


async def test_c3_connect_is_a_post(env: Env) -> None:
    rig, handle = await env.setup(connect=False)
    await rig.core.config.save({"clientId": "cid", "clientSecret": "csecret"})
    for method in ("GET", "HEAD"):
        r = await handle(req(method, "/api/connect"))
        assert r.status == 404, method
        assert r.json == {"error": "not_found", "message": "No such route"}
    assert await rig.core.store.list(rig.core.tenant, "state:") == []
    ok = await handle(req("POST", "/api/connect"))
    assert ok.status == 200
    assert set(ok.json) == {"url"}
    state = state_of(ok.json["url"])
    assert await rig.core.store.get(rig.core.tenant, f"state:{state}") is not None


# --- inbound webhook (C24, C25) ------------------------------------------------------------


async def test_inbound_204_returned_and_message_pending_before_processing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, handle = await env.setup()
    core = rig.core
    secret = await webhook_mode(core)
    original = core.processor.process
    gate = asyncio.Event()
    processed = asyncio.Event()

    async def gated(id_: str) -> None:
        await gate.wait()
        await original(id_)
        processed.set()

    monkeypatch.setattr(core.processor, "process", gated)

    r = await handle(req("POST", f"/inbound/{secret}", body=envelope("g-1")))
    assert r.status == 204
    assert r.json is None and r.html is None
    assert await pending(core, "g-1"), "pending first"
    assert rig.got == []

    gate.set()
    await processed.wait()
    assert [m.text for m in rig.got] == ["hello there"]
    assert await pending(core, "g-1") is None


async def test_post_204_processing_is_tracked_in_inflight_until_it_finishes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Node: close() resolves only after a slow post-204 process finishes. close() itself is
    # Task 11's; it awaits core.inflight, which is what this checks.
    rig, handle = await env.setup()
    core = rig.core
    secret = await webhook_mode(core)
    original = core.processor.process
    gate = asyncio.Event()
    finished = False

    async def slow(id_: str) -> None:
        nonlocal finished
        await gate.wait()
        await original(id_)
        finished = True

    monkeypatch.setattr(core.processor, "process", slow)

    r = await handle(req("POST", f"/inbound/{secret}", body=envelope("g-close")))
    assert r.status == 204
    assert len(core.inflight) == 1, "registered before the 204"
    waiting = asyncio.ensure_future(asyncio.gather(*core.inflight))
    await asyncio.sleep(0.02)
    assert not waiting.done(), "the in-flight inbound text is still running"
    gate.set()
    await waiting
    assert finished is True
    assert len(core.inflight) == 0
    assert await pending(core, "g-close") is None


async def test_post_204_processing_failure_is_logged_by_name_only(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger, records = recorder()
    rig, handle = await env.setup(logger=logger)
    core = rig.core
    secret = await webhook_mode(core)

    async def boom(_id: str) -> None:
        raise OSError("hello there was the text")

    monkeypatch.setattr(core.processor, "process", boom)
    r = await handle(req("POST", f"/inbound/{secret}", body=envelope("g-fail")))
    assert r.status == 204
    await asyncio.gather(*core.inflight)
    errors = [x for x in records if x.levelno == logging.ERROR]
    assert [x.getMessage() for x in errors] == [
        "goto-sms: processing an inbound text failed (OSError)"
    ]
    assert await pending(core, "g-fail"), "left pending for the background retry"
    assert len(core.inflight) == 0


async def test_c24_options_inbound_answers_200_before_webhook_mode_is_on(env: Env) -> None:
    # GoTo sends OPTIONS to the webhook URL while creating the channel, which happens before
    # the stored mode switches to webhook; a non-2xx makes it refuse the channel.
    rig, handle = await env.setup(authorize=deny)
    secret = "V" * 43
    await rig.core.store.set(rig.core.tenant, "inbound:webhookSecret", secret)
    ok = await handle(req("OPTIONS", f"/inbound/{secret}", headers={"origin": ""}))
    assert ok.status == 200, "right secret, mode still off, no authorize or Origin"
    assert ok.json is None
    wrong = await handle(req("OPTIONS", f"/inbound/{'w' * 43}"))
    assert wrong.status == 404, "wrong secret"


async def test_c25_text_to_other_number_dropped(env: Env) -> None:
    # A GoTo webhook subscription covers the whole account, so texts to the account's other
    # numbers (another app's) arrive here too. Acting on them would opt people out of, and
    # reply from, the wrong app.
    logger, records = recorder()
    rig, handle = await env.setup(logger=logger)
    core = rig.core
    secret = await webhook_mode(core)
    records.clear()  # setup's own lines
    other = await handle(
        req("POST", f"/inbound/{secret}", body=envelope("g-other", "STOP", "+13855550000"))
    )
    assert other.status == 204, "acknowledged so GoTo does not retry"
    assert await pending(core, "g-other") is None, "not stored"
    await settle()
    assert await core.opt_outs.has(PEER) is False, "STOP to another number does not opt out"
    assert [(x.levelno, x.getMessage()) for x in records] == [
        (logging.DEBUG, "goto-sms: ignored an inbound text to another number in the account")
    ]
    mine = await handle(req("POST", f"/inbound/{secret}", body=envelope("g-mine")))
    assert mine.status == 204
    assert await pending(core, "g-mine"), "own number still stored"


async def test_inbound_bad_secret_is_404(env: Env) -> None:
    rig, handle = await env.setup()
    core = rig.core
    r1 = await handle(req("POST", "/inbound/wrong", body=envelope("g-2")))
    assert r1.status == 404, "not in webhook mode"
    secret = await webhook_mode(core)
    r2 = await handle(req("POST", "/inbound/wrong", body=envelope("g-2")))
    assert r2.status == 404
    await core.store.set(core.tenant, "inbound:mode", "polling")
    r3 = await handle(req("POST", f"/inbound/{secret}", body=envelope("g-2")))
    assert r3.status == 404, "right secret but polling mode"
    assert r3.json is None
    assert await pending(core, "g-2") is None


async def test_inbound_other_event_types_and_duplicates_are_204_without_reprocessing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, handle = await env.setup()
    core = rig.core
    secret = await webhook_mode(core)
    other = await handle(
        req("POST", f"/inbound/{secret}", body={"type": "MESSAGE_STATUS", "content": {}})
    )
    assert other.status == 204
    calls = 0
    original = core.processor.process

    async def counting(id_: str) -> None:
        nonlocal calls
        calls += 1
        await original(id_)

    monkeypatch.setattr(core.processor, "process", counting)
    await handle(req("POST", f"/inbound/{secret}", body=envelope("g-3")))
    await settle()
    dup = await handle(req("POST", f"/inbound/{secret}", body=envelope("g-3")))
    assert dup.status == 204
    await settle()
    await asyncio.gather(*core.inflight)
    assert calls == 1


async def test_inbound_persist_invalid_inbound_is_400(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, handle = await env.setup()
    secret = await webhook_mode(rig.core)

    # parse_webhook_envelope never yields a message persist rejects, so stand in for one.
    async def reject(_m: object) -> bool:
        raise GoToSmsError("Inbound text is missing its id", "invalid_inbound")

    monkeypatch.setattr(rig.core.processor, "persist", reject)
    r = await handle(req("POST", f"/inbound/{secret}", body=envelope("g-4")))
    assert r.status == 400
    assert r.json["error"] == "invalid_inbound"


# --- credentials ---------------------------------------------------------------------------


async def test_credentials_post_for_env_locked_field_is_409(env: Env) -> None:
    _, handle = await env.setup(connect=False, env={"GOTO_CLIENT_ID": "env-cid"})
    r = await handle(req("POST", "/api/credentials", body={"clientId": "other"}))
    assert r.status == 409
    assert r.json["error"] == "locked"

    # A blank value for the locked field is "not editable", not a conflict.
    ok = await handle(
        req("POST", "/api/credentials", body={"clientId": "", "clientSecret": "new-secret"})
    )
    assert ok.status == 200
    s = ok.json
    assert s["clientId"] == "env-cid"
    assert s["sources"]["clientId"] == "env"
    assert "new-secret" not in json.dumps(s)


async def test_credentials_post_with_a_non_string_field_is_400(env: Env) -> None:
    _, handle = await env.setup()
    r = await handle(req("POST", "/api/credentials", body={"clientId": 5}))
    assert r.status == 400
    assert r.json == {"error": "invalid_input", "message": "clientId must be a string"}
    arr = await handle(req("POST", "/api/credentials", body=["clientId"]))
    assert arr.status == 400
    assert arr.json == {"error": "invalid_input", "message": "Expected a JSON object"}


# --- incoming mode -------------------------------------------------------------------------


async def test_incoming_webhook_enable_surfaces_goto_403_body_verbatim(env: Env) -> None:
    rig, handle = await env.setup(account_key="acct-1")
    env.fake.fail_next("POST", "/messaging/v2/subscriptions", 403, {"error": "first"})
    env.fake.fail_next("POST", "/messaging/v2/subscriptions", 403, {"message": "missing scope"})
    r = await handle(req("POST", "/api/incoming", body={"mode": "webhook"}))
    assert r.status == 502
    assert r.json == {
        "error": "goto_api",
        "message": "GoTo API error 403",
        "gotoStatus": 403,
        "gotoBody": json.dumps({"message": "missing scope"}, separators=(",", ":")),
    }
    assert await rig.core.incoming_mode() == "off", "mode unchanged on failure"


async def test_incoming_webhook_polling_off_webhook_switches_modes_poller_and_channels(
    env: Env,
) -> None:
    rig, handle = await env.setup(account_key="acct-1")
    core, fake = rig.core, env.fake

    async def post(mode: str) -> HttpResponse:
        return await handle(req("POST", "/api/incoming", body={"mode": mode}))

    w1 = await post("webhook")
    assert w1.status == 200
    s1 = w1.json
    assert s1["incoming"]["mode"] == "webhook"
    assert await core.store.get(core.tenant, "inbound:mode") == "webhook"
    assert len(fake.subscriptions) == 1
    assert core.poller.running is False
    first_url = s1["incoming"]["webhookUrl"]
    first_secret = first_url.split("/")[-1]
    assert fake.channels[0]["webhookChannelData"]["webhook"]["url"] == first_url

    p = await post("polling")
    assert p.json["incoming"]["mode"] == "polling"
    assert core.poller.running is True
    assert len(fake.subscriptions) == 0, "webhook subscription deleted"
    old_secret = await handle(req("POST", f"/inbound/{first_secret}", body=envelope("x")))
    assert old_secret.status == 404

    off = await post("off")
    assert off.json["incoming"]["mode"] == "off"
    assert core.poller.running is False
    assert await core.store.get(core.tenant, "inbound:mode") == "off"

    # Re-enable: the rotated secret means a new URL, so a new channel pointing at it.
    w2 = await post("webhook")
    second_url = w2.json["incoming"]["webhookUrl"]
    assert second_url != first_url
    assert len(fake.channels) == 2
    assert fake.channels[1]["webhookChannelData"]["webhook"]["url"] == second_url
    assert fake.subscriptions[0]["channelId"] == fake.channels[1]["channelId"]
    fresh = await handle(req("POST", f"/inbound/{second_url.split('/')[-1]}", body=envelope("y")))
    assert fresh.status == 204
    stale = await handle(req("POST", f"/inbound/{first_secret}", body=envelope("z")))
    assert stale.status == 404


async def test_incoming_webhook_needs_an_https_public_url_and_bad_mode_is_400(env: Env) -> None:
    _, handle = await env.setup(account_key="acct-1", public_url="http://app.example")
    r = await handle(req("POST", "/api/incoming", body={"mode": "webhook"}))
    assert r.status == 400
    assert r.json["error"] == "no_public_url"
    bad = await handle(req("POST", "/api/incoming", body={"mode": "carrier-pigeon"}))
    assert bad.status == 400
    assert bad.json == {
        "error": "invalid_input",
        "message": 'mode must be "webhook", "polling" or "off"',
    }


async def test_incoming_webhook_to_polling_resets_the_cursor_off_to_polling_keeps_it(
    env: Env,
) -> None:
    t = datetime(2026, 10, 27, 10, 0, tzinfo=UTC).timestamp() * 1000
    rig, handle = await env.setup(account_key="acct-1", now=lambda: t)
    store, tenant, poller = rig.core.store, rig.core.tenant, rig.core.poller

    async def post(mode: str) -> HttpResponse:
        return await handle(req("POST", "/api/incoming", body={"mode": mode}))

    old = iso_ms(t - 3_600_000)

    # The webhook covered the gap, so polling starts from now instead of re-reading it.
    await store.set(tenant, "poll:cursor", old)
    assert (await post("webhook")).status == 200
    assert (await post("polling")).status == 200
    await asyncio.sleep(0)
    await poller.idle()
    assert await store.get(tenant, "poll:cursor") == iso_ms(t)

    # Nothing handled texts while off, so the cursor is kept (a STOP sent then is still read).
    assert (await post("off")).status == 200
    await poller.idle()
    await store.set(tenant, "poll:cursor", old)
    assert (await post("polling")).status == 200
    await asyncio.sleep(0)
    await poller.idle()
    assert await store.get(tenant, "poll:cursor") == old


# --- callback ------------------------------------------------------------------------------


async def test_callback_finishes_the_login_and_redirects_bad_state_is_an_error_redirect(
    env: Env,
) -> None:
    rig, handle = await env.setup(connect=False)
    core = rig.core
    await core.config.save({"clientId": "cid", "clientSecret": "csecret"})
    state = state_of(await core.auth.start_connect())
    ok = await handle(req("GET", "/callback", query={"code": "code-ok", "state": state}))
    assert ok == HttpResponse(status=302, redirect="./?connected=1")
    assert (await build_status(core.options, core))["connected"] is True
    replay = await handle(req("GET", "/callback", query={"code": "code-ok", "state": state}))
    assert replay.redirect == "./?error=bad_state"
    missing = await handle(req("GET", "/callback"))
    assert missing.redirect == "./?error=bad_state"


async def test_head_callback_is_200_without_consuming_the_state(env: Env) -> None:
    rig, handle = await env.setup(connect=False, authorize=deny)
    core = rig.core
    await core.config.save({"clientId": "cid", "clientSecret": "csecret"})
    state = state_of(await core.auth.start_connect())
    head = await handle(req("HEAD", "/callback", query={"code": "code-ok", "state": state}))
    assert head == HttpResponse(status=200)
    ok = await handle(req("GET", "/callback", query={"code": "code-ok", "state": state}))
    assert ok.redirect == "./?connected=1", "the state survived the HEAD"


async def test_head_is_answered_as_get(env: Env) -> None:
    _, handle = await env.setup()
    page = await handle(req("HEAD", "/"))
    assert page == HttpResponse(status=200, html=HTML)
    status = await handle(req("HEAD", "/api/status", headers={"origin": ""}))
    assert status.status == 200, "a GET: no same-origin check"
    assert status.json["version"] == VERSION


# --- setup routes --------------------------------------------------------------------------


async def test_setup_page_accounts_numbers_selection_test_send_log_opt_outs(env: Env) -> None:
    _, handle = await env.setup()
    fake = env.fake
    page = await handle(req("GET", "/"))
    assert page == HttpResponse(status=200, html=HTML)

    accounts = await handle(req("GET", "/api/accounts"))
    assert accounts.json == {"accounts": [{"key": "acct-1", "name": "Test Account"}]}

    fake.numbers_pages = [[{"number": "+18015550199", "name": "Main"}]]
    numbers = await handle(req("GET", "/api/numbers", query={"accountKey": "acct-1"}))
    assert numbers.json == {
        "numbers": [{"number": "+18015550199", "name": "Main", "accountKey": "acct-1"}]
    }
    assert (await handle(req("GET", "/api/numbers"))).status == 400

    sel = await handle(
        req(
            "POST",
            "/api/selection",
            body={"accountKey": "acct-1", "fromNumber": "(801) 555-0199"},
        )
    )
    assert sel.status == 200
    assert sel.json["fromNumber"] == "+18015550199"
    assert sel.json["accountKey"] == "acct-1"
    bad_sel = await handle(req("POST", "/api/selection", body={"fromNumber": "call me"}))
    assert bad_sel.status == 400
    assert bad_sel.json["error"] == "invalid_number"

    sent = await handle(req("POST", "/api/test", body={"to": "801-555-0123"}))
    assert sent.status == 200
    assert sent.json["status"] == "sent"
    assert fake.sent[-1]["contactPhoneNumbers"][0] == PEER
    assert fake.sent[-1]["body"] == "Test message from goto-sms"

    log = await handle(req("GET", "/api/log", query={"limit": "9999"}))
    entries = log.json["entries"]
    assert len(entries) == 1
    assert entries[0]["to"] == PEER

    add = await handle(
        req("POST", "/api/opt-outs", body={"number": "801 555 0123", "action": "add"})
    )
    assert add.status == 200
    listed = await handle(req("GET", "/api/opt-outs"))
    assert [(x["number"], x["source"]) for x in listed.json["optOuts"]] == [(PEER, "admin")]
    await handle(req("POST", "/api/opt-outs", body={"number": PEER, "action": "remove"}))
    assert (await handle(req("GET", "/api/opt-outs"))).json["optOuts"] == []
    bad = await handle(req("POST", "/api/opt-outs", body={"number": PEER, "action": "nuke"}))
    assert bad.status == 400

    assert (await handle(req("GET", "/api/nope"))).status == 404


async def test_numbers_and_send_results_use_camel_case_on_the_wire(env: Env) -> None:
    _, handle = await env.setup()
    env.fake.numbers_pages = [[{"number": "+18015550199", "name": "Main", "smsEnabled": False}]]
    numbers = await handle(req("GET", "/api/numbers", query={"accountKey": "acct-1"}))
    assert numbers.json == {
        "numbers": [
            {
                "number": "+18015550199",
                "name": "Main",
                "accountKey": "acct-1",
                "smsEnabled": False,
            }
        ]
    }
    sent = await handle(req("POST", "/api/test", body={"to": PEER}))
    assert sent.json == {"status": "sent", "messageId": "msg-1", "to": PEER}
    missing = await handle(req("POST", "/api/test", body={}))
    assert missing.json == {"error": "invalid_input", "message": "to is required"}
    log = (await handle(req("GET", "/api/log"))).json["entries"]
    assert set(log[0]) == {"id", "direction", "from", "to", "status", "gotoId", "at"}
    assert log[0]["gotoId"] == "msg-1"


async def test_log_limit_parses_like_parse_int(env: Env) -> None:
    rig, handle = await env.setup()
    for i in range(60):
        await rig.core.log.append(
            direction="outbound", from_=FROM, to=PEER, status="sent", goto_id=f"m{i}"
        )

    async def count(limit: str | None) -> int:
        query = {} if limit is None else {"limit": limit}
        return len((await handle(req("GET", "/api/log", query=query))).json["entries"])

    assert await count(None) == 50
    assert await count("7") == 7
    assert await count(" 12abc") == 12, "parseInt reads the leading digits"
    assert await count("0") == 50
    assert await count("-3") == 50
    assert await count("abc") == 50
    assert await count("3.9") == 3


async def test_disconnect_clears_the_login_and_unexpected_errors_are_a_generic_500(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger, records = recorder()
    rig, handle = await env.setup(logger=logger)
    r = await handle(req("POST", "/api/disconnect", body={}))
    assert r.json["connected"] is False

    async def on_fire() -> list[dict[str, str]]:
        raise OSError("disk on fire: secret-ish detail")

    monkeypatch.setattr(rig.core.opt_outs, "list", on_fire)
    boom = await handle(req("GET", "/api/opt-outs"))
    assert boom.status == 500
    assert boom.json == {
        "error": "internal",
        "message": "Something went wrong; see the server logs",
    }
    assert "disk on fire" not in json.dumps(boom.json)
    assert all("disk on fire" not in x.getMessage() for x in records)
    assert any(
        x.levelno == logging.DEBUG and "GET /api/opt-outs" in x.getMessage() for x in records
    )


async def test_goto_errors_are_502_and_timeouts_503(env: Env) -> None:
    _, handle = await env.setup(timeout_s=0.05)  # don't wait 10 s for the hung request
    env.fake.fail_next("GET", "/identity/v1/Users/me", 503, {"message": "down"})
    r = await handle(req("GET", "/api/accounts"))
    # A GoTo 5xx is a goto_api error (GoTo answered); only timeouts/network are temporary.
    assert r.status == 502
    env.fake.hang_next("GET", "/identity/v1/Users/me")
    rig_handle_timeout = await handle(req("GET", "/api/accounts"))
    assert rig_handle_timeout.status == 503
    assert rig_handle_timeout.json == {
        "error": "temporary",
        "message": "GoTo did not answer in time; try again",
    }


# --- selection follows the account in webhook mode ----------------------------------------


async def test_selection_new_account_in_webhook_mode_moves_the_subscription(env: Env) -> None:
    rig, handle = await env.setup()
    fake = env.fake
    await rig.core.config.save({"accountKey": "acct-1"})
    on = await handle(req("POST", "/api/incoming", body={"mode": "webhook"}))
    url = on.json["incoming"]["webhookUrl"]
    assert [s["accountKey"] for s in fake.subscriptions] == ["acct-1"]

    r = await handle(req("POST", "/api/selection", body={"accountKey": "acct-2"}))
    assert r.status == 200
    s = r.json
    assert s["accountKey"] == "acct-2"
    assert s["incoming"]["mode"] == "webhook"
    assert "error" not in s["incoming"]
    assert s["incoming"]["webhookUrl"] == url, "same URL and secret: GoTo keeps the channel"
    assert [x["accountKey"] for x in fake.subscriptions] == ["acct-2"]
    assert len(fake.channels) == 1
    # Saving the same account again doesn't touch GoTo.
    fake.requests.clear()
    await handle(req("POST", "/api/selection", body={"accountKey": "acct-2"}))
    assert not any(x.path.startswith("/messaging/v2/subscriptions") for x in fake.requests)


async def test_selection_failed_resubscribe_keeps_the_selection_and_shows_the_error(
    env: Env,
) -> None:
    rig, handle = await env.setup()
    fake = env.fake
    await rig.core.config.save({"accountKey": "acct-1"})
    await handle(req("POST", "/api/incoming", body={"mode": "webhook"}))
    # Both subscription shapes refused (enable_webhook retries ORGANIZATION on a 4xx).
    fake.fail_next("POST", "/messaging/v2/subscriptions", 403, {"message": "nope"})
    fake.fail_next("POST", "/messaging/v2/subscriptions", 403, {"message": "nope"})

    r = await handle(req("POST", "/api/selection", body={"accountKey": "acct-2"}))
    assert r.status == 200
    s = r.json
    assert s["accountKey"] == "acct-2", "selection kept"
    assert s["incoming"]["error"] == {
        "error": "goto_api",
        "message": "GoTo API error 403",
        "gotoStatus": 403,
    }
    # Still reported on a plain status read, and cleared once a mode switch succeeds.
    again = (await handle(req("GET", "/api/status"))).json
    assert again["incoming"]["error"]["gotoStatus"] == 403
    fixed = await handle(req("POST", "/api/incoming", body={"mode": "webhook"}))
    assert "error" not in fixed.json["incoming"]
    assert [x["accountKey"] for x in fake.subscriptions] == ["acct-2"]


async def test_retry_after_failed_move_deletes_the_old_subscription_before_making_the_new(
    env: Env,
) -> None:
    rig, handle = await env.setup()
    fake = env.fake
    await rig.core.config.save({"accountKey": "acct-1"})
    await handle(req("POST", "/api/incoming", body={"mode": "webhook"}))
    old_id = fake.subscriptions[0]["id"]
    # The DELETE of the acct-1 subscription fails, so the move stops with the error stored.
    fake.fail_next("DELETE", f"/messaging/v2/subscriptions/{old_id}", 500, {"message": "down"})
    moved = await handle(req("POST", "/api/selection", body={"accountKey": "acct-2"}))
    assert moved.json["incoming"]["error"]["gotoStatus"] == 500
    assert [x["accountKey"] for x in fake.subscriptions] == ["acct-1"]

    # The page's "Retry webhook setup" re-posts webhook mode.
    retry = await handle(req("POST", "/api/incoming", body={"mode": "webhook"}))
    assert retry.status == 200
    assert "error" not in retry.json["incoming"]
    assert [x["accountKey"] for x in fake.subscriptions] == ["acct-2"], (
        "no acct-1 subscription is left delivering"
    )


async def test_retry_after_failed_move_second_failed_delete_creates_nothing_keeps_error(
    env: Env,
) -> None:
    rig, handle = await env.setup()
    fake = env.fake
    await rig.core.config.save({"accountKey": "acct-1"})
    await handle(req("POST", "/api/incoming", body={"mode": "webhook"}))
    old_id = fake.subscriptions[0]["id"]
    fake.fail_next("DELETE", f"/messaging/v2/subscriptions/{old_id}", 500, {"message": "down"})
    fake.fail_next("DELETE", f"/messaging/v2/subscriptions/{old_id}", 500, {"message": "down"})
    await handle(req("POST", "/api/selection", body={"accountKey": "acct-2"}))

    retry = await handle(req("POST", "/api/incoming", body={"mode": "webhook"}))
    assert retry.status == 502
    assert [x["accountKey"] for x in fake.subscriptions] == ["acct-1"], (
        "no acct-2 subscription while the old one is still there"
    )
    status = (await handle(req("GET", "/api/status"))).json
    assert status["incoming"]["error"]["gotoStatus"] == 500


# --- back link and login_url ---------------------------------------------------------------


async def test_back_link_status_carries_back_url_and_label(env: Env) -> None:
    # Node also checks the label default ("the app") and the backUrl/loginUrl validation;
    # both live in the facade's option handling (client.ts linkOption), so Task 11 ports them.
    rig, handle = await env.setup(back=BackLink("/admin", "Spring Ideas"))
    s = (await handle(req("GET", "/api/status"))).json
    assert s["back"] == {"url": "/admin", "label": "Spring Ideas"}
    assert (await build_status(rig.core.options, rig.core))["back"] == {
        "url": "/admin",
        "label": "Spring Ideas",
    }
    none = await env.make()
    assert "back" not in await build_status(none.core.options, none.core)


async def test_login_url_unauthorized_page_redirects_there_api_routes_stay_403(env: Env) -> None:
    _, handle = await env.setup(authorize=deny, login_url="/login?next=%2Fgoto")
    page = await handle(req("GET", "/"))
    assert page.status == 302
    assert page.redirect == "/login?next=%2Fgoto"
    for method, path in [x for x in API_ROUTES if x[1] != "/"]:
        r = await handle(req(method, path, body={}))
        assert r.status == 403, f"{method} {path}"
        assert r.redirect is None
    # Without login_url the page is a plain 403.
    _, plain = await env.setup(authorize=deny)
    assert (await plain(req("GET", "/"))).status == 403


# --- deferred handlers (contract/http.md "When startup failed") ---------------------------


async def test_deferred_handlers_serve_the_live_routes_once_started(env: Env) -> None:
    rig = await env.make(authorize=allow)
    starts = 0

    async def start() -> Core:
        nonlocal starts
        starts += 1
        await asyncio.sleep(0.01)
        return rig.core

    handle = create_deferred_handlers(rig.core.options, rig.core.logger, start, HTML)
    a, b = await asyncio.gather(
        handle(req("GET", "/api/status")), handle(req("GET", "/api/status"))
    )
    assert a.status == b.status == 200
    assert a.json["connected"] is True
    assert (await handle(req("GET", "/"))).html == HTML
    assert starts == 1, "concurrent first requests share one startup"


async def test_deferred_handlers_after_a_failed_startup() -> None:
    options = ResolvedOptions(
        authorize=allow,
        public_url="https://app.example",
        redirect_uri="https://app.example/goto-sms/callback",
    )
    starts = 0

    async def start() -> Core:
        nonlocal starts
        starts += 1
        raise NotConfiguredError("No encryption key: set GOTO_SMS_ENCRYPTION_KEY")

    logger = logging.getLogger("aiogotoconnect.sms.tests")
    handle = create_deferred_handlers(options, logger, start, HTML)
    assert (await handle(req("GET", "/"))) == HttpResponse(status=200, html=HTML)
    s = (await handle(req("GET", "/api/status"))).json
    assert s["startupError"] == "No encryption key: set GOTO_SMS_ENCRYPTION_KEY"
    assert (s["configured"], s["connected"], s["ready"]) == (False, False, False)
    assert s["redirectUri"] == "https://app.example/goto-sms/callback"
    assert s["incoming"] == {"mode": "off", "canWebhook": True}

    for method, path in [("GET", "/api/accounts"), ("POST", "/api/connect")]:
        r = await handle(req(method, path, body={}))
        assert r.status == 400, path
        assert r.json == {
            "error": "not_configured",
            "message": "No encryption key: set GOTO_SMS_ENCRYPTION_KEY",
        }
    assert (await handle(req("GET", "/nope"))).json["error"] == "not_found"
    cb = await handle(req("GET", "/callback", query={"code": "c", "state": "s"}))
    assert cb == HttpResponse(status=302, redirect="./?error=not_configured")
    assert (await handle(req("POST", f"/inbound/{'S' * 43}", body=envelope("g")))).status == 404
    assert (await handle(req("OPTIONS", f"/inbound/{'S' * 43}"))).status == 404
    # authorize and the same-origin check still apply.
    cross = await handle(req("POST", "/api/connect", headers={"origin": "https://evil.example"}))
    assert cross.json["error"] == "cross_origin"
    refused = create_deferred_handlers(ResolvedOptions(authorize=deny), logger, start, HTML)
    assert (await refused(req("GET", "/api/status"))).status == 403
    assert starts == 2, "each handler set starts once"


async def test_deferred_handlers_name_foreign_startup_errors_only() -> None:
    async def start() -> Core:
        raise OSError("/data/.goto-sms.key: permission denied")

    logger = logging.getLogger("aiogotoconnect.sms.tests")
    handle = create_deferred_handlers(ResolvedOptions(authorize=allow), logger, start, HTML)
    s = (await handle(req("GET", "/api/status"))).json
    assert s["startupError"] == "goto-sms failed to start (OSError); see the server logs"
    assert "permission denied" not in json.dumps(s)
