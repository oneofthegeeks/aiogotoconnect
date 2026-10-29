"""Port of packages/node/test/client.test.ts (the create_goto_sms facade), the facade-level
handler tests Task 10 deferred (status after a startup failure, close() awaiting post-204 work,
back_url/login_url validation), C15's production half, and the Python lifecycle: start() is
memoized, shared by concurrent callers, retried after a cancellation, and close() lets go of
everything it made."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import aiohttp
import pytest

from aiogotoconnect.sms import client as client_module
from aiogotoconnect.sms.client import GoToSms, create_goto_sms, trim_trailing_slashes
from aiogotoconnect.sms.core import AuthorizeRequest, Core
from aiogotoconnect.sms.errors import GoToSmsError, NotConfiguredError
from aiogotoconnect.sms.server.handlers import (
    HttpRequest,
    assert_setup_auth,
    create_deferred_handlers,
    create_handlers,
)
from aiogotoconnect.sms.store import MemoryStore
from aiogotoconnect.sms.types import InboundMessage

from .fake_goto import FakeGoTo
from .helpers import FROM, PEER, connected_sms, recorder, state_of

T0 = datetime(2026, 10, 27, 10, 0, tzinfo=UTC).timestamp() * 1000
ONE_PROCESS = "Another process is using this goto-sms store; run one process per store (see README)"
HTML = "<!doctype html><title>setup</title>"


def at_t0() -> float:
    return T0


@pytest.fixture
async def cleanups() -> AsyncIterator[list[Callable[[], Awaitable[None]]]]:
    """Async callables run after the test, last registered first (Node's cleanups array)."""
    pending: list[Callable[[], Awaitable[None]]] = []
    yield pending
    while pending:
        await pending.pop()()


def inbound(id_: str, ts: str, body: str) -> dict[str, Any]:
    return {"id": id_, "from": PEER, "to": FROM, "body": body, "timestamp": ts}


def bare(**opts: Any) -> GoToSms:
    """An instance on a MemoryStore with no GoTo behind it (Node's `make` in several tests)."""
    defaults: dict[str, Any] = {"store": MemoryStore(), "env": {}, "logger": False}
    return create_goto_sms(**{**defaults, **opts})


def get(path: str) -> HttpRequest:
    return HttpRequest(
        method="GET", path=path, query={}, headers={"host": "app.example"}, cookies={}, body=None
    )


class CountingStore(MemoryStore):
    """Counts process-lock claims; `before_claim` may raise or wait (failure/cancel tests)."""

    def __init__(self) -> None:
        super().__init__()
        self.claims = 0
        self.before_claim: Callable[[int], Awaitable[None]] | None = None

    async def claim(self, tenant: str, key: str, ttl_ms: float, owner: str) -> bool:
        if key == "process-lock":
            self.claims += 1
            if self.before_claim is not None:
                await self.before_claim(self.claims)
        return await super().claim(tenant, key, ttl_ms, owner)


def record_sessions(monkeypatch: pytest.MonkeyPatch) -> list[aiohttp.ClientSession]:
    """Records every aiohttp session the facade creates (a factory, not a subclass)."""
    made: list[aiohttp.ClientSession] = []
    real = aiohttp.ClientSession

    def factory(*args: Any, **kwargs: Any) -> aiohttp.ClientSession:
        session = real(*args, **kwargs)
        made.append(session)
        return session

    monkeypatch.setattr(client_module.aiohttp, "ClientSession", factory)
    return made


# --- ported from client.test.ts ---


async def test_facade_end_to_end_on_fake(
    fake_goto: FakeGoTo, cleanups: list[Callable[[], Awaitable[None]]]
) -> None:
    sms = await connected_sms(fake_goto, now=at_t0)
    cleanups.append(sms.close)

    r = await sms.send("(801) 555-0123", "hello")
    assert r.status == "sent"
    assert fake_goto.sent[0]["body"] == "hello"

    got: list[InboundMessage] = []
    sms.on("message", got.append)
    fake_goto.inbound.append(inbound("g-1", "2026-10-27T10:00:01Z", "hi back"))
    await sms.core.poller.tick()
    assert len(got) == 1
    assert got[0].text == "hi back"

    # off() removes the handler.
    def off(m: InboundMessage) -> None:
        got.append(m)

    sms.on("message", off).off("message", off)
    fake_goto.inbound.append(inbound("g-2", "2026-10-27T10:00:02Z", "two"))
    await sms.core.poller.tick()
    assert len(got) == 2

    await sms.opt_outs.add(PEER, "admin")
    assert await sms.opt_outs.has(PEER) is True
    assert [e["number"] for e in await sms.opt_outs.list()] == [PEER]
    assert (await sms.send(PEER, "nope")).status == "opted_out"
    await sms.opt_outs.remove(PEER)
    assert await sms.opt_outs.has(PEER) is False

    s = await sms.status()
    assert s["connected"] is True
    assert s["sources"]["clientSecret"] == "store"
    assert "csecret" not in json.dumps(s), "status never carries the secret"


async def test_second_instance_on_same_store_logs_the_one_process_warning(
    cleanups: list[Callable[[], Awaitable[None]]],
) -> None:
    store = MemoryStore()
    first = create_goto_sms(store=store, encryption_key="k", env={}, logger=False)
    cleanups.append(first.close)
    await first.start()
    logger, records = recorder()
    second = create_goto_sms(store=store, encryption_key="k", env={}, logger=logger)
    cleanups.append(second.close)
    await second.start()
    assert any(r.levelno == logging.WARNING and r.getMessage() == ONE_PROCESS for r in records)

    # Once the first one closes and releases the lock, a new instance gets it quietly.
    await first.close()
    await second.close()
    logger3, records3 = recorder()
    third = create_goto_sms(store=store, encryption_key="k", env={}, logger=logger3)
    cleanups.append(third.close)
    await third.start()
    assert not any(r.getMessage() == ONE_PROCESS for r in records3)


async def test_start_raises_not_configured_without_data_dir_store_or_key() -> None:
    # A custom store and no data_dir: there's nowhere to keep a keyfile.
    sms = bare()
    with pytest.raises(NotConfiguredError):
        await sms.start()
    # The failure is cached: start() raises it again without retrying.
    with pytest.raises(NotConfiguredError):
        await sms.start()
    # send() still answers with a result instead of raising.
    r = await sms.send(PEER, "hi")
    assert r.status == "not_connected"
    assert r.reason is not None and "GOTO_SMS_ENCRYPTION_KEY" in r.reason
    await sms.close()


async def test_default_file_store_in_data_dir_with_a_generated_keyfile(
    tmp_path: Path, cleanups: list[Callable[[], Awaitable[None]]]
) -> None:
    sms = create_goto_sms(data_dir=str(tmp_path), env={}, logger=False)
    cleanups.append(sms.close)
    await sms.start()
    assert sms.core.key_source == "keyfile"
    await sms.opt_outs.add(PEER, "admin")
    await sms.close()
    assert (tmp_path / "goto-sms.json").exists()
    assert (tmp_path / ".goto-sms.key").exists()
    again = create_goto_sms(data_dir=str(tmp_path), env={}, logger=False)
    cleanups.append(again.close)
    await again.start()
    assert await again.opt_outs.has(PEER) is True


async def test_async_handler_failure_is_logged_without_the_message_text(
    fake_goto: FakeGoTo, cleanups: list[Callable[[], Awaitable[None]]]
) -> None:
    logger, records = recorder()
    sms = await connected_sms(fake_goto, now=at_t0, logger=logger)
    cleanups.append(sms.close)
    later: list[InboundMessage] = []

    async def failing(m: InboundMessage) -> None:
        await asyncio.sleep(0)
        raise RuntimeError(f"could not handle {m.text}")

    def sync_failing(m: InboundMessage) -> None:
        raise ValueError(m.text)

    sms.on("message", failing)
    sms.on("message", sync_failing)
    sms.on("message", later.append)
    fake_goto.inbound.append(inbound("g-1", "2026-10-27T10:00:01Z", "secret words"))
    await sms.core.poller.tick()
    assert len(later) == 1, "a failing handler does not stop the others"
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert any("handler" in r.getMessage() for r in warnings)
    assert all("secret words" not in r.getMessage() for r in records)
    # The message was processed, not left pending.
    assert await sms.core.store.get("default", "inbound:pending:g-1") is None


async def test_polling_mode_starts_the_poller_on_start_logger_false_is_silent(
    fake_goto: FakeGoTo,
    cleanups: list[Callable[[], Awaitable[None]]],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    store = MemoryStore(at_t0)
    await store.set("default", "inbound:mode", "polling")
    sms = await connected_sms(fake_goto, store=store, now=at_t0, logger=False, poll_interval=60)
    cleanups.append(sms.close)
    # The first tick ran at start (before connect, so it found no from number); a manual tick
    # now that it's connected proves the poller is wired to the facade's handlers.
    got: list[InboundMessage] = []
    sms.on("message", got.append)

    # A raising handler makes the facade warn, which logger=False must swallow.
    def boom(_m: InboundMessage) -> None:
        raise RuntimeError("boom")

    sms.on("message", boom)
    fake_goto.inbound.append(inbound("g-1", "2026-10-27T10:00:01Z", "hi"))
    await sms.core.poller.tick()
    assert len(got) == 1
    assert sms.core.poller.running is True
    assert [r for r in caplog.records if r.name.startswith("aiogotoconnect")] == []
    assert capsys.readouterr().err == "", "logger=False writes nothing, not even lastResort"


async def test_default_logger_is_the_package_logger_and_logs_startup_failure_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="aiogotoconnect.sms")
    sms = create_goto_sms(store=MemoryStore(), env={})
    with pytest.raises(NotConfiguredError):
        await sms.start()
    with pytest.raises(NotConfiguredError):
        await sms.start()
    await sms.close()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, "logged once, not per caller"
    assert errors[0].name == "aiogotoconnect.sms"
    assert (
        errors[0].getMessage().startswith("goto-sms: startup failed (Set GOTO_SMS_ENCRYPTION_KEY")
    )


async def test_default_logger_is_handed_to_the_modules(
    cleanups: list[Callable[[], Awaitable[None]]],
) -> None:
    sms = create_goto_sms(store=MemoryStore(), encryption_key="k", env={})
    cleanups.append(sms.close)
    await sms.start()
    assert sms.core.logger is logging.getLogger("aiogotoconnect.sms")


async def test_status_tokens_sealed_with_an_old_key_read_as_not_connected_and_unreadable(
    fake_goto: FakeGoTo, cleanups: list[Callable[[], Awaitable[None]]]
) -> None:
    store = MemoryStore()
    a = await connected_sms(fake_goto, store=store)
    assert (await a.status())["ready"] is True
    await a.close()

    # The encryption key changed and the admin re-entered the client secret, but the stored
    # GoTo login was sealed with the old key: sends would fail, so status must not say ready.
    b = await connected_sms(fake_goto, store=store, encryption_key="another-key", connect=False)
    cleanups.append(b.close)
    await b.core.config.save({"clientId": "cid", "clientSecret": "csecret", "fromNumber": FROM})
    s = await b.status()
    assert s["configured"] is True
    assert s["connected"] is False
    assert s["credentialsUnreadable"] is True
    assert s["ready"] is False

    # Reconnecting (what the page asks for) replaces the login and clears the state.
    await b.core.auth.finish_connect("code-ok", state_of(await b.core.auth.start_connect()))
    after = await b.status()
    assert after["connected"] is True
    assert after["credentialsUnreadable"] is False
    assert after["ready"] is True


async def test_a_changeme_or_blank_public_url_counts_as_unset_the_env_value_then_applies(
    fake_goto: FakeGoTo, cleanups: list[Callable[[], Awaitable[None]]]
) -> None:
    a = await connected_sms(
        fake_goto, public_url="CHANGEME", env={"GOTO_SMS_PUBLIC_URL": "https://env.example"}
    )
    cleanups.append(a.close)
    assert (await a.status())["redirectUri"] == "https://env.example/goto-sms/callback"
    b = await connected_sms(fake_goto, public_url=" ", env={"GOTO_SMS_PUBLIC_URL": "changeme"})
    cleanups.append(b.close)
    assert (await b.status())["redirectUri"] == ""


def test_authorize_may_be_sync_or_async() -> None:
    # Node's check is compile-time (req.cookies without a cast); here, both shapes are accepted
    # and kept as given for the handlers, which read cookies from AuthorizeRequest.
    def sync_check(req: AuthorizeRequest) -> bool:
        return bool(req.cookies.get("session"))

    async def async_check(req: AuthorizeRequest) -> bool:
        return "host" in req.headers

    assert bare(authorize=sync_check)._options.authorize is sync_check
    assert bare(authorize=async_check)._options.authorize is async_check


def token_calls(fake: FakeGoTo) -> list[dict[str, str]]:
    return [
        {k: v[0] for k, v in parse_qs(r.body).items()}
        for r in fake.requests
        if r.pathname == "/oauth/token"
    ]


async def test_verify_connection_refreshes_now_even_when_the_access_token_is_fresh(
    fake_goto: FakeGoTo, cleanups: list[Callable[[], Awaitable[None]]]
) -> None:
    sms = await connected_sms(fake_goto)
    cleanups.append(sms.close)
    before = len(token_calls(fake_goto))
    assert await sms.verify_connection() is True
    after = token_calls(fake_goto)
    assert len(after) == before + 1
    assert after[-1]["grant_type"] == "refresh_token"


async def test_verify_connection_exchanges_a_seeded_refresh_token_then_reports_connected(
    fake_goto: FakeGoTo, cleanups: list[Callable[[], Awaitable[None]]]
) -> None:
    sms = await connected_sms(fake_goto, connect=False, refresh_token="refresh-1")
    cleanups.append(sms.close)
    await sms.core.config.save({"clientId": "cid", "clientSecret": "csecret"})
    assert (await sms.status())["connected"] is False
    assert await sms.verify_connection() is True
    s = await sms.status()
    assert s["connected"] is True
    assert s.get("refreshSource") == "seeded-from-env"
    assert token_calls(fake_goto)[-1]["refresh_token"] == "refresh-1"


async def test_verify_connection_returns_false_never_raises_for_login_problems(
    fake_goto: FakeGoTo, cleanups: list[Callable[[], Awaitable[None]]]
) -> None:
    # Not configured at all.
    unconfigured = await connected_sms(fake_goto, connect=False)
    cleanups.append(unconfigured.close)
    assert await unconfigured.verify_connection() is False
    # A refused seed.
    seeded = await connected_sms(fake_goto, connect=False, refresh_token="wrong")
    cleanups.append(seeded.close)
    await seeded.core.config.save({"clientId": "cid", "clientSecret": "csecret"})
    assert await seeded.verify_connection() is False
    # GoTo refuses the stored login: False, and status says the login expired.
    sms = await connected_sms(fake_goto)
    cleanups.append(sms.close)
    fake_goto.tokens.refresh = "rotated-elsewhere"
    assert await sms.verify_connection() is False
    assert (await sms.status())["loginExpired"] is True
    # Startup failed for lack of a key.
    nokey = bare()
    cleanups.append(nokey.close)
    assert await nokey.verify_connection() is False


async def test_verify_connection_returns_false_when_goto_cannot_be_reached(
    fake_goto: FakeGoTo, cleanups: list[Callable[[], Awaitable[None]]]
) -> None:
    sms = await connected_sms(fake_goto)
    cleanups.append(sms.close)
    await fake_goto.close()  # the token request now fails at the network level
    assert await sms.verify_connection() is False


def test_trim_trailing_slashes_trims_and_stays_linear_on_long_slash_runs() -> None:
    assert trim_trailing_slashes("/goto-sms///") == "/goto-sms"
    assert trim_trailing_slashes("https://x.example/") == "https://x.example"
    assert trim_trailing_slashes("/") == ""
    assert trim_trailing_slashes("no-slash") == "no-slash"
    nasty = "/" * 200_000 + "x"
    started = time.perf_counter()
    assert trim_trailing_slashes(nasty) == nasty
    assert time.perf_counter() - started < 0.1, "linear time"


# --- facade-level tests Task 10 deferred (handlers.test.ts) ---


async def test_status_answers_not_set_up_when_startup_failed_for_lack_of_a_key() -> None:
    sms = bare()
    with pytest.raises(NotConfiguredError):
        await sms.start()
    s = await sms.status()
    assert s["configured"] is False
    assert s["ready"] is False
    assert s["connected"] is False
    assert s["sources"]["clientId"] == "unset"
    assert "GOTO_SMS_ENCRYPTION_KEY" in s.get("startupError", "")
    assert "webhookUrl" not in s["incoming"]
    await sms.close()


async def test_close_resolves_only_after_a_slow_post_204_process_finishes(
    fake_goto: FakeGoTo, monkeypatch: pytest.MonkeyPatch
) -> None:
    sms = await connected_sms(fake_goto, authorize=lambda _r: True)
    core = sms.core
    handle = create_handlers(core, HTML)
    secret = "S" * 43
    await core.store.set(core.tenant, "inbound:mode", "webhook")
    await core.store.set(core.tenant, "inbound:webhookSecret", secret)
    original = core.processor.process
    gate = asyncio.Event()
    finished = False

    async def slow(id_: str) -> None:
        nonlocal finished
        await gate.wait()
        await original(id_)
        finished = True

    monkeypatch.setattr(core.processor, "process", slow)
    envelope = {
        "type": "INBOUND_MESSAGE",
        "content": {
            "payload": {
                "id": "g-close",
                "direction": "INBOUND",
                "authorTouchpoint": PEER,
                "ownerPhoneNumber": FROM,
                "body": "hello there",
                "timestamp": "2026-10-27T10:00:00Z",
            }
        },
    }
    r = await handle(
        HttpRequest(
            method="POST",
            path=f"/inbound/{secret}",
            query={},
            headers={"host": "app.example"},
            cookies={},
            body=envelope,
        )
    )
    assert r.status == 204
    closing = asyncio.ensure_future(sms.close())
    await asyncio.sleep(0.02)
    assert not closing.done(), "close() waits for the in-flight inbound text"
    gate.set()
    await closing
    assert finished is True
    assert len(core.inflight) == 0
    assert await core.store.get(core.tenant, "inbound:pending:g-close") is None


async def test_back_url_and_label_status_carries_back_label_defaults_to_the_app(
    fake_goto: FakeGoTo, cleanups: list[Callable[[], Awaitable[None]]]
) -> None:
    named = await connected_sms(fake_goto, back_url="/admin", back_label="Spring Ideas")
    cleanups.append(named.close)
    assert (await named.status()).get("back") == {"url": "/admin", "label": "Spring Ideas"}
    plain = await connected_sms(fake_goto, back_url="https://app.example/")
    cleanups.append(plain.close)
    assert (await plain.status()).get("back") == {
        "url": "https://app.example/",
        "label": "the app",
    }
    none = await connected_sms(fake_goto, back_label="ignored without a url")
    cleanups.append(none.close)
    assert "back" not in await none.status()
    # A placeholder label falls back too; a placeholder url is unset (no error, no link).
    assert bare(back_url="/x", back_label="CHANGEME")._options.back is not None
    assert bare(back_url="/x", back_label=" ")._options.back.label == "the app"  # type: ignore[union-attr]
    assert bare(back_url="CHANGEME", login_url="")._options.back is None
    assert bare(login_url="changeme")._options.login_url is None


BAD_LINKS = [
    "javascript:alert(1)",
    " JaVaScript:alert(1)",
    "java\tscript:alert(1)",
    "data:text/html,hi",
    "mailto:x@example.com",
    "//evil.example",
    "/\\evil.example",
    "https:evil.example",
    # Same scheme as a parser base, so "relative" to it, but absolute in a browser.
    "http:evil.example",
    "HTTP:/evil.example",
    "http:\\evil.example",
    # Node's literal Express 'back' (redirects to the Referer); refused for parity.
    "back",
    "BACK",
    # Python-side extras: backslashes are slashes in a browser, empty hosts don't parse.
    "\\\\evil.example",
    "\\/evil.example",
    "http://",
    "https:///",
    "http://[::1",
    "/x﻿y",
]
GOOD_LINKS = [
    "/",
    "/login?next=%2Fadmin",
    "../app",
    "home",
    "https://app.example/x",
    "http://localhost:3000/",
    "HTTPS://APP.example",
]


@pytest.mark.parametrize("url", BAD_LINKS)
@pytest.mark.parametrize("name", ["back_url", "login_url"])
def test_back_url_and_login_url_must_be_http_s_or_relative(name: str, url: str) -> None:
    with pytest.raises(
        ValueError, match=rf"goto-sms: {name} must be an http\(s\) URL or a relative"
    ):
        bare(encryption_key="k", **{name: url})


@pytest.mark.parametrize("url", GOOD_LINKS)
def test_good_links_are_accepted(url: str) -> None:
    sms = bare(encryption_key="k", back_url=url, login_url=url)
    assert sms._options.login_url == url
    assert sms._options.back is not None and sms._options.back.url == url


# --- C15 production half, construction-time validation ---


def test_c15_log_only_is_refused_in_production_at_construction() -> None:
    with pytest.raises(GoToSmsError) as err:
        create_goto_sms(log_only=True, env={"GOTO_SMS_ENV": "production"})
    assert err.value.code == "log_only_in_production"
    assert "GOTO_SMS_ENV=production" in str(err.value)
    # Elsewhere it's allowed.
    assert bare(log_only=True, env={"GOTO_SMS_ENV": "development"})._options.log_only is True


def test_allow_unauthenticated_setup_in_production_is_refused_by_assert_setup_auth() -> None:
    # Node accepts the option at construction; only router() (assertSetupAuth) refuses it in
    # production, and only without authorize. asgi_app() calls assert_setup_auth (Task 12).
    production = {"GOTO_SMS_ENV": "production"}
    sms = create_goto_sms(allow_unauthenticated_setup=True, env=production)
    with pytest.raises(RuntimeError, match="pass authorize"):
        assert_setup_auth(sms._options)
    guarded = create_goto_sms(
        allow_unauthenticated_setup=True, env=production, authorize=lambda _r: True
    )
    assert_setup_auth(guarded._options)
    # Outside production the escape hatch works.
    assert_setup_auth(bare(allow_unauthenticated_setup=True)._options)


def test_env_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOTO_SMS_PUBLIC_URL", "https://from-env.example/")
    monkeypatch.setenv("GOTO_SMS_ENV", "production")
    sms = create_goto_sms(store=MemoryStore(), logger=False)
    assert sms._options.redirect_uri == "https://from-env.example/goto-sms/callback"
    with pytest.raises(GoToSmsError):
        create_goto_sms(store=MemoryStore(), logger=False, log_only=True)


def test_root_mount_urls() -> None:
    for mount in ("", "/"):
        o = bare(public_url="https://x.example/", mount_path=mount)._options
        assert o.mount_path == ""
        assert o.redirect_uri == "https://x.example/callback"
    nested = bare(public_url="https://x.example/", mount_path="/admin/tools/sms/")._options
    assert nested.redirect_uri == "https://x.example/admin/tools/sms/callback"
    assert bare(public_url="https://x.example", mount_path="sms")._options.mount_path == "/sms"
    default = bare(public_url="https://x.example///")._options
    assert default.public_url == "https://x.example"
    assert default.redirect_uri == "https://x.example/goto-sms/callback"
    assert bare(public_url="///")._options.public_url is None
    assert bare()._options.redirect_uri is None


async def test_root_mount_webhook_url(cleanups: list[Callable[[], Awaitable[None]]]) -> None:
    sms = bare(encryption_key="k", public_url="https://x.example/", mount_path="/")
    cleanups.append(sms.close)
    await sms.start()
    assert sms.core.webhook_url("s3cret") == "https://x.example/inbound/s3cret"


def test_options_defaults_follow_node() -> None:
    o = bare()._options
    assert o.mount_path == "/goto-sms"
    assert o.data_dir is None, "a custom store and no data_dir: nowhere for a keyfile"
    assert create_goto_sms(env={}, logger=False)._options.data_dir == "./data"
    assert o.yes_is_opt_in is True
    assert (o.poll_interval_s, o.send_timeout_s) == (30.0, 10.0)
    assert (o.inbound_per_hour, o.log_retention_days) == (10, 90)
    assert o.auth_base_url == "https://authentication.logmeininc.com"
    assert o.api_base_url == "https://api.goto.com"
    merged = bare(replies={"help": "Call us"})._options.replies
    assert merged["help"] == "Call us"
    assert merged["stop"] and merged["start"], "replies merge over the defaults"


# --- lifecycle ---


async def test_start_is_idempotent_under_concurrency(
    cleanups: list[Callable[[], Awaitable[None]]],
) -> None:
    store = CountingStore()
    await store.set("default", "inbound:mode", "polling")

    async def slow(_n: int) -> None:
        await asyncio.sleep(0.01)

    store.before_claim = slow
    sms = bare(store=store, encryption_key="k")
    cleanups.append(sms.close)
    _, _, core = await asyncio.gather(sms.start(), sms.start(), sms._ready_core())
    assert store.claims == 1, "one start: one lock claim"
    assert core is sms.core
    assert sms.core.poller.running is True
    await sms.start()
    assert store.claims == 1


async def test_lifespan_start_and_deferred_handlers_share_one_start(
    cleanups: list[Callable[[], Awaitable[None]]],
) -> None:
    store = CountingStore()
    sms = bare(store=store, encryption_key="k", authorize=lambda _r: True)
    cleanups.append(sms.close)
    handle = create_deferred_handlers(sms._options, sms._logger, sms._ready_core, HTML)
    r1, r2, _ = await asyncio.gather(handle(get("/api/status")), handle(get("/")), sms.start())
    assert (r1.status, r2.status) == (200, 200)
    assert store.claims == 1


async def test_a_cancelled_start_is_not_cached_a_later_start_retries(
    monkeypatch: pytest.MonkeyPatch, cleanups: list[Callable[[], Awaitable[None]]]
) -> None:
    sessions = record_sessions(monkeypatch)
    store = CountingStore()

    async def cancel_first(n: int) -> None:
        if n == 1:
            raise asyncio.CancelledError

    store.before_claim = cancel_first
    sms = bare(store=store, encryption_key="k")
    cleanups.append(sms.close)
    with pytest.raises(asyncio.CancelledError):
        await sms.start()
    assert sessions and sessions[0].closed, "the cancelled start closed the session it made"
    # As in Node, the core is kept (close() can still release a lock it took); the retry
    # builds a fresh one on a fresh session.
    stale = sms.core
    await sms.start()
    assert store.claims == 2
    assert sms.core is not stale
    assert sms.core.options.session is sessions[1] and not sessions[1].closed


async def test_deferred_handlers_retry_after_a_cancelled_start(
    cleanups: list[Callable[[], Awaitable[None]]],
) -> None:
    store = CountingStore()

    async def cancel_first(n: int) -> None:
        if n == 1:
            raise asyncio.CancelledError

    store.before_claim = cancel_first
    sms = bare(store=store, encryption_key="k", authorize=lambda _r: True)
    cleanups.append(sms.close)
    handle = create_deferred_handlers(sms._options, sms._logger, sms._ready_core, HTML)
    with pytest.raises(asyncio.CancelledError):
        await handle(get("/api/status"))
    r = await handle(get("/api/status"))
    assert r.status == 200
    assert r.json is not None and "startupError" not in r.json


async def test_a_caller_giving_up_does_not_cancel_the_shared_start(
    cleanups: list[Callable[[], Awaitable[None]]],
) -> None:
    store = CountingStore()
    gate = asyncio.Event()

    async def wait(_n: int) -> None:
        await gate.wait()

    store.before_claim = wait
    sms = bare(store=store, encryption_key="k")
    cleanups.append(sms.close)
    impatient = asyncio.ensure_future(sms.start())
    patient = asyncio.ensure_future(sms.start())
    await asyncio.sleep(0)
    impatient.cancel()
    with pytest.raises(asyncio.CancelledError):
        await impatient
    gate.set()
    await patient
    assert store.claims == 1


async def test_a_failed_start_closes_the_session_it_made_and_status_reraises_foreign_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = record_sessions(monkeypatch)
    store = CountingStore()

    async def broken(_n: int) -> None:
        raise OSError("disk on fire")

    store.before_claim = broken
    sms = bare(store=store, encryption_key="k")
    with pytest.raises(OSError):
        await sms.start()
    assert sessions and sessions[0].closed
    # Node: status()/verify_connection() report only configuration failures; others propagate.
    with pytest.raises(OSError):
        await sms.status()
    with pytest.raises(OSError):
        await sms.verify_connection()
    # send() too (Node's coreOr rethrows anything but NotConfiguredError).
    with pytest.raises(OSError):
        await sms.send(PEER, "hi")
    await sms.close()


async def test_async_with_starts_and_closes_and_never_crashes_the_host_on_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = record_sessions(monkeypatch)
    store = MemoryStore()
    async with bare(store=store, encryption_key="k") as sms:
        assert sms.core.store is store
        assert (await store.get("default", "process-lock")) is not None
    assert await store.get("default", "process-lock") is None, "lock released on exit"
    assert sessions[0].closed

    # Startup fails for lack of a key: the host's lifespan still runs, status() says why.
    async with bare() as broken:
        s = await broken.status()
        assert "GOTO_SMS_ENCRYPTION_KEY" in s.get("startupError", "")


async def test_close_never_closes_a_session_it_was_given(fake_goto: FakeGoTo) -> None:
    async with aiohttp.ClientSession() as session:
        sms = await connected_sms(fake_goto, session=session)
        assert sms.core.options.session is session
        await sms.close()
        assert not session.closed


async def test_close_is_idempotent_and_a_no_op_before_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = record_sessions(monkeypatch)
    never = bare(encryption_key="k")
    await never.close()
    await never.close()
    assert sessions == []
    sms = bare(encryption_key="k")
    await sms.start()
    await sms.close()
    await sms.close()
    assert sessions[0].closed


async def test_close_during_start_never_starts_the_poller_or_the_lock_timer() -> None:
    store = CountingStore()
    await store.set("default", "inbound:mode", "polling")
    gate = asyncio.Event()

    async def wait(_n: int) -> None:
        await gate.wait()

    store.before_claim = wait
    sms = bare(store=store, encryption_key="k")
    starting = asyncio.ensure_future(sms.start())
    await asyncio.sleep(0)
    closing = asyncio.ensure_future(sms.close())
    await asyncio.sleep(0.01)
    assert not closing.done(), "close() waits for the start in flight"
    gate.set()
    await closing
    await starting
    assert sms.core.poller.running is False
    assert sms._lock_task is None
    assert await store.get("default", "process-lock") is None


async def test_core_raises_until_started() -> None:
    sms = bare(encryption_key="k")
    with pytest.raises(GoToSmsError) as err:
        _ = sms.core
    assert err.value.code == "not_ready"
    assert "await sms.start()" in str(err.value)


async def test_maintenance_refreshes_the_lock_and_retries_pending_rows(
    monkeypatch: pytest.MonkeyPatch, cleanups: list[Callable[[], Awaitable[None]]]
) -> None:
    monkeypatch.setattr(client_module, "LOCK_REFRESH_S", 0.01)
    logger, records = recorder()
    store = CountingStore()
    sms = bare(store=store, encryption_key="k", logger=logger)
    cleanups.append(sms.close)
    await sms.start()
    replays = 0
    original = sms.core.processor.replay_pending

    async def counting() -> None:
        nonlocal replays
        replays += 1
        await original()

    monkeypatch.setattr(sms.core.processor, "replay_pending", counting)
    await asyncio.sleep(0.05)
    assert store.claims >= 2, "the lock lease is renewed"
    assert replays >= 1, "pending rows are retried while not polling"

    async def broken(_n: int) -> None:
        raise OSError("store down")

    store.before_claim = broken
    await asyncio.sleep(0.03)
    assert any(
        r.levelno == logging.WARNING
        and r.getMessage() == "goto-sms: background maintenance failed (OSError)"
        for r in records
    )
    store.before_claim = None
    await sms.close()
    claims = store.claims
    await asyncio.sleep(0.03)
    assert store.claims == claims, "no maintenance after close()"


async def test_on_keeps_set_semantics_and_rejects_unknown_events(
    fake_goto: FakeGoTo, cleanups: list[Callable[[], Awaitable[None]]]
) -> None:
    sms = await connected_sms(fake_goto, now=at_t0)
    cleanups.append(sms.close)
    got: list[InboundMessage] = []
    assert sms.on("message", got.append).on("message", got.append) is sms
    fake_goto.inbound.append(inbound("g-1", "2026-10-27T10:00:01Z", "once"))
    await sms.core.poller.tick()
    assert len(got) == 1, "the same handler registered twice runs once (Node's Set)"
    with pytest.raises(ValueError):
        sms.on("mesage", got.append)  # type: ignore[arg-type]


async def test_send_forwards_only_dedupe_key(
    fake_goto: FakeGoTo, cleanups: list[Callable[[], Awaitable[None]]]
) -> None:
    sms = await connected_sms(fake_goto, now=at_t0)
    cleanups.append(sms.close)
    first = await sms.send(PEER, "once", dedupe_key="k-1")
    again = await sms.send_or_raise(PEER, "once", dedupe_key="k-1")
    assert first.status == again.status == "sent"
    assert again.deduped is True
    assert len(fake_goto.sent) == 1


async def test_send_or_raise_raises_send_failed_when_not_configured() -> None:
    from aiogotoconnect.sms.errors import SendFailedError

    sms = bare()
    with pytest.raises(SendFailedError) as err:
        await sms.send_or_raise(PEER, "hi")
    assert err.value.result.status == "not_connected"
    await sms.close()


def test_core_type_is_exported_for_handlers() -> None:
    # The deferred handlers take a zero-arg coroutine function returning Core.
    fn: Callable[[], Awaitable[Core]] = bare()._ready_core
    assert callable(fn)
