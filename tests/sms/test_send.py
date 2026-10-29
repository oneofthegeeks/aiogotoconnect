"""Port of packages/node/test/send.test.ts (C8-C15, dedupe, message log, opt-outs)."""

import asyncio
import dataclasses
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

from aiogotoconnect.sms.config import ConfigResolver
from aiogotoconnect.sms.errors import (
    GoToApiError,
    GoToSmsError,
    LoginExpiredError,
    NotConfiguredError,
    SendFailedError,
    TemporaryError,
)
from aiogotoconnect.sms.http import GoToHttp, create_goto_http
from aiogotoconnect.sms.log import PRUNE_EVERY, MessageLog, iso_ms
from aiogotoconnect.sms.oauth import GoToAuth
from aiogotoconnect.sms.optouts import OptOuts
from aiogotoconnect.sms.secrets import SecretBox
from aiogotoconnect.sms.send import Sender
from aiogotoconnect.sms.store import MemoryStore
from aiogotoconnect.sms.types import LogEntry, SendResult

from .fake_goto import FakeGoTo, start_fake_goto

T = "t1"
FROM = "+18015550100"
TO = "+18015550123"
DAY = 86_400_000
SEND_PATH = "/messaging/v1/messages"
SILENT = logging.getLogger("aiogotoconnect.sms.test_send")
HTTP_LOGGER = logging.getLogger("aiogotoconnect.sms.test_send.http")
BASE_ENV = {"GOTO_CLIENT_ID": "cid", "GOTO_CLIENT_SECRET": "csecret", "GOTO_FROM_NUMBER": FROM}


@dataclass
class Clock:
    t: float = 1_000_000_000_000


@dataclass
class Rig:
    fake: FakeGoTo
    clock: Clock
    store: MemoryStore
    box: SecretBox
    env: Mapping[str, str]
    config: ConfigResolver
    auth: GoToAuth
    http: GoToHttp
    opt_outs: OptOuts
    log: MessageLog
    defaults: dict[str, Any] = field(default_factory=dict)

    def make(self, **over: Any) -> Sender:
        return Sender(**{**self.defaults, **over})

    def posts(self) -> int:
        return sum(1 for r in self.fake.requests if r.method == "POST" and r.path == SEND_PATH)


RigFactory = Callable[..., Awaitable[Rig]]


@pytest.fixture
async def setup() -> AsyncIterator[RigFactory]:
    """Like Node's setup(): a fresh fake GoTo per call, everything closed after the test."""
    cleanups: list[Callable[[], Awaitable[None]]] = []

    async def make_rig(
        *,
        env: Mapping[str, str] | None = None,
        connect: bool = True,
        box: SecretBox | None = None,
        store: MemoryStore | None = None,
    ) -> Rig:
        fake = await start_fake_goto()
        cleanups.append(fake.close)
        session = aiohttp.ClientSession()
        cleanups.append(session.close)
        clock = Clock()

        def now() -> float:
            return clock.t

        store_ = store if store is not None else MemoryStore(now)
        box_ = box or SecretBox(os.urandom(32))
        env_ = dict(BASE_ENV) if env is None else env
        config = ConfigResolver(store_, T, box_, {}, env_)
        auth = GoToAuth(
            store=store_,
            tenant=T,
            box=box_,
            config=config,
            session=session,
            auth_base_url=fake.auth_base_url,
            redirect_uri="https://app.example/goto/callback",
            logger=SILENT,
            now=now,
        )
        http = create_goto_http(
            session,
            fake.api_base_url,
            auth.get_access_token,
            auth.force_refresh,
            logger=HTTP_LOGGER,
        )
        opt_outs = OptOuts(store_, T, now)
        log = MessageLog(store_, T, now)
        rig = Rig(fake, clock, store_, box_, env_, config, auth, http, opt_outs, log)
        rig.defaults = {
            "http": http,
            "auth": auth,
            "config": config,
            "opt_outs": opt_outs,
            "log": log,
            "store": store_,
            "tenant": T,
            "now": now,
            "send_timeout_s": 10.0,
            "max_body": 1600,
            "log_only": False,
            "env": env_,
            "logger": SILENT,
        }
        if connect:
            url = await auth.start_connect()
            await auth.finish_connect("code-ok", parse_qs(urlsplit(url).query)["state"][0])
        return rig

    try:
        yield make_rig
    finally:
        while cleanups:
            await cleanups.pop()()


async def test_sends_to_a_normalized_number_and_returns_the_goto_message_id(
    setup: RigFactory,
) -> None:
    rig = await setup()
    r = await rig.make().send("(801) 555-0123", "hello")
    assert r == SendResult(status="sent", to=TO, message_id="msg-1")
    assert rig.fake.sent == [
        {"ownerPhoneNumber": FROM, "contactPhoneNumbers": [TO], "body": "hello"}
    ]


async def test_c8_invalid_number_no_network_call(setup: RigFactory) -> None:
    rig = await setup()
    before = len(rig.fake.requests)
    r = await rig.make().send("555-0100", "hello")
    assert r.status == "invalid_number"
    assert r.to is None
    assert len(rig.fake.requests) == before


async def test_c9_owner_number_refused_no_network(setup: RigFactory) -> None:
    rig = await setup()
    r = await rig.make().send("801.555.0100", "hello")
    assert r == SendResult(
        status="rejected", reason="GoTo can't text the sending number itself", to=FROM
    )
    assert rig.posts() == 0


async def test_c10_opted_out_refused_no_network(setup: RigFactory) -> None:
    rig = await setup()
    await rig.opt_outs.add(TO, "keyword")
    sender = rig.make()
    r = await sender.send("801-555-0123", "hello")
    assert r.status == "opted_out"
    assert rig.posts() == 0
    # The one allowed send: the STOP confirmation.
    confirm = await sender.send(TO, "You are unsubscribed", bypass_opt_out=True)
    assert confirm.status == "sent"
    assert len(rig.fake.sent) == 1


async def test_c11_same_dedupe_key_sends_once_sequentially_and_concurrently(
    setup: RigFactory,
) -> None:
    rig = await setup()
    sender = rig.make()

    # Sequential.
    first = await sender.send(TO, "code 1234", dedupe_key="k1")
    second = await sender.send(TO, "code 1234", dedupe_key="k1")
    assert first.status == "sent"
    assert first.deduped is None
    assert second == dataclasses.replace(first, deduped=True)
    assert len(rig.fake.sent) == 1

    # Concurrent: both callers await one in-flight send.
    a, b = await asyncio.gather(
        sender.send(TO, "code 5678", dedupe_key="k2"),
        sender.send(TO, "code 5678", dedupe_key="k2"),
    )
    assert a.status == "sent"
    assert a.deduped is None
    assert b == dataclasses.replace(a, deduped=True)
    assert len(rig.fake.sent) == 2

    # Persisted: a new Sender (a restart) still knows the key.
    again = await rig.make().send(TO, "code 1234", dedupe_key="k1")
    assert again == dataclasses.replace(first, deduped=True)
    assert rig.posts() == 2

    # Rejected results aren't stored: the same key may be retried.
    rig.fake.fail_next("POST", SEND_PATH, 503)
    busy = await sender.send(TO, "later", dedupe_key="k3")
    assert busy.status == "rejected"
    assert busy.retryable is True
    retried = await sender.send(TO, "later", dedupe_key="k3")
    assert retried.status == "sent"
    assert retried.deduped is None


async def test_c11_dedupe_key_locked_by_another_process_is_refused_as_retryable(
    setup: RigFactory,
) -> None:
    rig = await setup()
    assert await rig.store.claim(T, "dedupe-lock:k", 60_000, "other-process") is True
    r = await rig.make().send(TO, "hi", dedupe_key="k")
    assert r == SendResult(
        status="rejected",
        reason="A send with this dedupeKey is already in progress",
        retryable=True,
        to=TO,
    )
    assert rig.posts() == 0


async def test_c11_two_senders_sharing_a_store_send_once(setup: RigFactory) -> None:
    """Python extra: the cross-process seam, two Senders racing on one store and one key."""
    rig = await setup()
    a, b = await asyncio.gather(
        rig.make().send(TO, "hi", dedupe_key="shared"),
        rig.make().send(TO, "hi", dedupe_key="shared"),
    )
    assert rig.posts() == 1
    statuses = sorted([(a.status, a.retryable, a.deduped), (b.status, b.retryable, b.deduped)])
    # The loser either saw the lock (retryable rejection) or the stored result (deduped).
    assert ("sent", None, None) in statuses
    assert statuses != [("sent", None, None), ("sent", None, None)]
    # The lock is released afterwards.
    assert await rig.store.get(T, "dedupe-lock:shared") is None


async def test_prune_dedupe_drops_dedupe_records_older_than_the_log_retention(
    setup: RigFactory,
) -> None:
    rig = await setup()
    sender = rig.make()
    await sender.send(TO, "old", dedupe_key="old")
    rig.clock.t += 89 * DAY
    await sender.send(TO, "new", dedupe_key="new")
    rig.clock.t += 2 * DAY  # 'old' is now 91 days old, 'new' 2 days
    await sender.prune_dedupe()
    assert [k for k, _ in await rig.store.list(T, "dedupe:")] == ["dedupe:new"]
    # The pruned key sends again (after a routine token refresh).
    r = await sender.send(TO, "old", dedupe_key="old")
    assert r.deduped is None
    assert len(rig.fake.sent) == 3


async def test_prune_dedupe_drops_records_without_a_numeric_at(setup: RigFactory) -> None:
    rig = await setup()
    await rig.store.set(T, "dedupe:bool", {"result": {"status": "sent"}, "at": True})
    await rig.store.set(T, "dedupe:str", {"result": {"status": "sent"}, "at": "now"})
    await rig.store.set(T, "dedupe:junk", "x")
    await rig.store.set(T, "dedupe:ok", {"result": {"status": "sent"}, "at": rig.clock.t})
    await rig.make().prune_dedupe()
    assert [k for k, _ in await rig.store.list(T, "dedupe:")] == ["dedupe:ok"]


async def test_c12_hang_next_uncertain_fake_saw_exactly_one_post(setup: RigFactory) -> None:
    rig = await setup()
    rig.fake.hang_next("POST", SEND_PATH)
    sender = rig.make(send_timeout_s=0.2)
    r = await sender.send(TO, "hello", dedupe_key="h")
    assert r == SendResult(
        status="uncertain",
        reason="GoTo didn't answer in time — the text may or may not have been sent; not retried",
        to=TO,
    )
    assert rig.posts() == 1
    # Uncertain is stored: the same key never sends a second copy.
    again = await sender.send(TO, "hello", dedupe_key="h")
    assert again.deduped is True
    assert rig.posts() == 1


async def test_c12_a_cancelled_caller_does_not_cancel_the_post_in_flight(
    setup: RigFactory,
) -> None:
    """Python extra (Node has no cancellation): the dedupe record must still be written."""
    rig = await setup()
    rig.fake.hang_next("POST", SEND_PATH)
    sender = rig.make(send_timeout_s=0.3)
    first = asyncio.create_task(sender.send(TO, "hello", dedupe_key="c"))
    for _ in range(200):
        if rig.posts() == 1:
            break
        await asyncio.sleep(0.01)
    assert rig.posts() == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    # Joins the still-running send instead of posting a second copy.
    again = await sender.send(TO, "hello", dedupe_key="c")
    assert again.status == "uncertain"
    assert again.deduped is True
    assert rig.posts() == 1
    assert await rig.store.get(T, "dedupe:c") is not None


async def test_a_2xx_goto_answer_that_is_not_json_is_uncertain_never_rejected(
    setup: RigFactory,
) -> None:
    rig = await setup()
    rig.fake.fail_next("POST", SEND_PATH, 200, "<html>oops</html>")
    r = await rig.make().send(TO, "hello")
    assert r.status == "uncertain"
    assert r.retryable is None


async def test_c13_401_then_success_sent_after_one_refresh(setup: RigFactory) -> None:
    rig = await setup()
    rig.fake.tokens.valid.clear()  # the cached access token is now refused

    def token_calls() -> int:
        return sum(1 for r in rig.fake.requests if r.path == "/oauth/token")

    before = token_calls()
    r = await rig.make().send(TO, "hello")
    assert r.status == "sent"
    assert token_calls() - before == 1
    assert rig.posts() == 2
    assert len(rig.fake.sent) == 1


async def test_c13_401_after_the_refresh_too_not_connected_login_expired(
    setup: RigFactory,
) -> None:
    rig = await setup()
    rig.fake.fail_next("POST", SEND_PATH, 401)
    rig.fake.fail_next("POST", SEND_PATH, 401)
    r = await rig.make().send(TO, "hello")
    assert r == SendResult(status="not_connected", reason="GoTo login expired — reconnect", to=TO)


async def test_c14_403_and_10dlc_reasons(setup: RigFactory) -> None:
    rig = await setup()
    sender = rig.make()
    fake = rig.fake

    fake.fail_next("POST", SEND_PATH, 403, {"message": "Forbidden"})
    assert await sender.send(TO, "x") == SendResult(
        status="rejected",
        reason="GoTo refused (403): the login lacks messaging.v1.send, or this number isn't "
        "SMS-enabled in GoTo Admin",
        to=TO,
    )

    ten_dlc = "This number isn't registered to a 10DLC texting campaign"
    fake.fail_next("POST", SEND_PATH, 400, {"message": "Sender not registered with a 10DLC brand"})
    assert await sender.send(TO, "x") == SendResult(status="rejected", reason=ten_dlc, to=TO)
    # The 10DLC check wins over the status mapping.
    fake.fail_next("POST", SEND_PATH, 403, {"message": "No approved Campaign for this number"})
    assert await sender.send(TO, "x") == SendResult(status="rejected", reason=ten_dlc, to=TO)

    for status in (408, 429, 500, 503):
        fake.fail_next("POST", SEND_PATH, status)
        r = await sender.send(TO, "x")
        assert r.status == "rejected"
        assert r.retryable is True, f"HTTP {status} is retryable"

    fake.fail_next("POST", SEND_PATH, 422, {"message": "Invalid destination " + "z" * 300})
    r422 = await sender.send(TO, "x")
    assert r422.status == "rejected"
    assert r422.retryable is None
    assert r422.reason is not None
    assert "422" in r422.reason
    assert "Invalid destination" in r422.reason
    assert "z" * 201 not in r422.reason, "GoTo message cut to 200 chars"

    # A body that isn't JSON is never echoed.
    fake.fail_next("POST", SEND_PATH, 400, "raw secret-ish text")
    r400 = await sender.send(TO, "x")
    assert r400.status == "rejected"
    assert r400.reason is not None
    assert "raw secret-ish" not in r400.reason
    assert len(fake.sent) == 0


@pytest.mark.parametrize(
    ("name", "env", "connect", "reason"),
    [
        ("no client", {"GOTO_FROM_NUMBER": FROM}, False, "GoTo client ID and secret aren't set"),
        (
            "no from number",
            {"GOTO_CLIENT_ID": "cid", "GOTO_CLIENT_SECRET": "csecret"},
            True,
            "No sending number chosen",
        ),
        (
            "bad from number",
            {**BASE_ENV, "GOTO_FROM_NUMBER": "call me"},
            True,
            "The configured sending number isn't a valid phone number",
        ),
        (
            "never connected",
            BASE_ENV,
            False,
            "Not connected to GoTo — open the setup page and click Connect",
        ),
    ],
)
async def test_c15_unconfigured_not_connected_fake_saw_0_posts(
    setup: RigFactory, name: str, env: dict[str, str], connect: bool, reason: str
) -> None:
    rig = await setup(env=env, connect=connect)
    before = len(rig.fake.requests)
    r = await rig.make().send(TO, "hello")
    assert r == SendResult(status="not_connected", reason=reason, to=TO), name
    assert len(rig.fake.requests) == before, f"{name}: no network"


async def test_c15_login_expired_not_connected_no_network(setup: RigFactory) -> None:
    rig = await setup()
    await rig.store.set(T, "login_expired", True)
    before = len(rig.fake.requests)
    r = await rig.make().send(TO, "hello")
    assert r == SendResult(status="not_connected", reason="GoTo login expired — reconnect", to=TO)
    assert len(rig.fake.requests) == before


async def test_c15_secret_sealed_with_another_key_not_connected_no_network(
    setup: RigFactory,
) -> None:
    store = MemoryStore()
    env = {"GOTO_CLIENT_ID": "cid", "GOTO_FROM_NUMBER": FROM}
    old_box = SecretBox(os.urandom(32))
    await ConfigResolver(store, T, old_box, {}, env).save({"clientSecret": "csecret"})
    rig = await setup(env=env, store=store, connect=False)
    r = await rig.make().send(TO, "hello")
    assert r == SendResult(
        status="not_connected",
        reason="Saved GoTo credentials can't be decrypted — re-enter the client secret and "
        "reconnect",
        to=TO,
    )
    assert len(rig.fake.requests) == 0


async def test_c15_log_only_refused_in_production_at_construction(setup: RigFactory) -> None:
    # The facade-level half (create_goto_sms with GOTO_SMS_ENV=production) is Task 11's test.
    rig = await setup(connect=False)
    with pytest.raises(GoToSmsError) as exc:
        rig.make(log_only=True, env={"GOTO_SMS_ENV": "production"})
    assert exc.value.code == "log_only_in_production"
    assert "GOTO_SMS_ENV=production" in str(exc.value)
    # Not production: allowed.
    rig.make(log_only=True, env={"GOTO_SMS_ENV": "development"})


async def test_a_seeded_goto_refresh_token_with_no_stored_login_can_still_send(
    setup: RigFactory,
) -> None:
    rig = await setup(env={**BASE_ENV, "GOTO_REFRESH_TOKEN": "refresh-1"}, connect=False)
    r = await rig.make().send(TO, "hello")
    assert r.status == "sent"
    assert len(rig.fake.sent) == 1


async def test_a_garbled_2xx_token_answer_is_rejected_not_uncertain_and_not_deduped(
    setup: RigFactory,
) -> None:
    rig = await setup()
    rig.clock.t += 2 * 3600_000  # the next send must refresh first
    rig.fake.fail_next("POST", "/oauth/token", 200, "not json")
    sender = rig.make()
    r = await sender.send(TO, "hello", dedupe_key="g")
    assert r.status == "rejected"
    assert r.retryable is True
    assert rig.posts() == 0
    assert await rig.store.get(T, "dedupe:g") is None
    retried = await sender.send(TO, "hello", dedupe_key="g")
    assert retried.status == "sent"
    assert retried.deduped is None
    assert len(rig.fake.sent) == 1


async def test_token_fetch_failing_before_the_send_rejected_and_retryable_nothing_posted(
    setup: RigFactory,
) -> None:
    rig = await setup()
    rig.clock.t += 2 * 3600_000  # access token expired: the next send must refresh first
    rig.fake.fail_next("POST", "/oauth/token", 503)
    r = await rig.make().send(TO, "hello")
    assert r.status == "rejected"
    assert r.retryable is True
    assert rig.posts() == 0


class FailingHttp:
    """A stub GoToHttp whose every call raises `err`."""

    def __init__(self, err: BaseException) -> None:
        self.err = err

    async def json(self, *_a: Any, **_k: Any) -> Any:
        raise self.err


@pytest.mark.parametrize(
    ("err", "expected"),
    [
        (
            NotConfiguredError("Saved GoTo login can't be decrypted — reconnect to GoTo"),
            {
                "status": "not_connected",
                "reason": "Saved GoTo login can't be decrypted — reconnect to GoTo",
            },
        ),
        (
            LoginExpiredError(),
            {"status": "not_connected", "reason": "GoTo login expired — reconnect"},
        ),
        (TemporaryError("x", request_sent=True), {"status": "uncertain"}),
        (TemporaryError("x"), {"status": "rejected", "retryable": True}),
        # A 2xx error not marked as from the token endpoint: the POST was accepted.
        (GoToApiError(200, "anything"), {"status": "uncertain"}),
        (GoToApiError(201, "Malformed JSON response"), {"status": "uncertain"}),
        # Token endpoint errors happen before the POST: never sent.
        (
            GoToApiError(200, "Malformed token response", token_endpoint=True),
            {"status": "rejected", "retryable": True},
        ),
        (
            GoToApiError(404, "GoTo token endpoint returned 404", token_endpoint=True),
            {"status": "rejected", "retryable": None},
        ),
    ],
)
async def test_token_and_error_mapping_from_a_stub_http(
    setup: RigFactory, err: BaseException, expected: dict[str, Any]
) -> None:
    rig = await setup()
    r = await rig.make(http=FailingHttp(err)).send(TO, "hello")
    for k, v in expected.items():
        assert getattr(r, k) == v, f"{err!r}: {k}"
    assert r.to == TO


async def test_an_unexpected_error_from_http_propagates(setup: RigFactory) -> None:
    # Node rethrows anything translate() doesn't know; it is a bug, not a send result.
    rig = await setup()
    with pytest.raises(RuntimeError, match="boom"):
        await rig.make(http=FailingHttp(RuntimeError("boom"))).send(TO, "hello")


async def test_log_only_needs_only_a_sending_number_runs_the_gates_and_never_posts(
    setup: RigFactory,
) -> None:
    rig = await setup(env={"GOTO_FROM_NUMBER": FROM}, connect=False)
    sender = rig.make(log_only=True)
    r = await sender.send(TO, "hello")
    assert r == SendResult(status="sent", reason="log-only mode", to=TO)
    await rig.opt_outs.add(TO, "admin")
    assert (await sender.send(TO, "hello")).status == "opted_out"
    assert (await sender.send(FROM, "hello")).status == "rejected"
    assert len(rig.fake.requests) == 0
    logged = next(e for e in await rig.log.list() if e.status == "sent")
    assert logged.reason == "log-only mode"

    no_from = await setup(env={}, connect=False)
    assert (await no_from.make(log_only=True).send(TO, "hi")).status == "not_connected"


async def test_send_or_raise_ignores_a_smuggled_bypass_opt_out(setup: RigFactory) -> None:
    # Python has no options bag to smuggle through: the keyword is not accepted at all.
    rig = await setup()
    await rig.opt_outs.add(TO, "keyword")
    with pytest.raises(TypeError):
        await cast(Any, rig.make()).send_or_raise(TO, "hello", bypass_opt_out=True)
    with pytest.raises(SendFailedError) as exc:
        await rig.make().send_or_raise(TO, "hello")
    assert exc.value.result.status == "opted_out"
    assert len(rig.fake.sent) == 0


async def test_log_only_never_persists_dedupe_records_so_turning_it_off_sends_for_real(
    setup: RigFactory,
) -> None:
    rig = await setup()
    fake1 = await rig.make(log_only=True).send(TO, "hello", dedupe_key="lo")
    assert fake1.reason == "log-only mode"
    assert await rig.store.get(T, "dedupe:lo") is None
    real = await rig.make().send(TO, "hello", dedupe_key="lo")
    assert real.status == "sent"
    assert real.deduped is None
    assert len(rig.fake.sent) == 1


async def test_every_100th_recorded_send_prunes_old_dedupe_records(setup: RigFactory) -> None:
    rig = await setup()
    sender = rig.make()
    await sender.send(TO, "old", dedupe_key="old")  # recorded send 1
    rig.clock.t += 91 * DAY
    # Gate rejections are recorded sends too, and need no network.
    for _ in range(2, PRUNE_EVERY):
        await sender.send(TO, "")
    assert await rig.store.get(T, "dedupe:old") is not None, "not pruned before the 100th"
    await sender.send(TO, "")  # recorded send 100
    assert await rig.store.get(T, "dedupe:old") is None


async def test_send_or_raise_raises_send_failed_error_carrying_result(setup: RigFactory) -> None:
    rig = await setup()
    sender = rig.make()
    ok = await sender.send_or_raise(TO, "hello")
    assert ok.status == "sent"
    await rig.opt_outs.add(TO, "keyword")
    with pytest.raises(SendFailedError) as exc:
        await sender.send_or_raise(TO, "hello")
    assert exc.value.result.status == "opted_out"


async def test_send_or_raise_forwards_the_dedupe_key(setup: RigFactory) -> None:
    rig = await setup()
    sender = rig.make()
    await sender.send_or_raise(TO, "hello", dedupe_key="d")
    again = await sender.send_or_raise(TO, "hello", dedupe_key="d")
    assert again.deduped is True
    assert len(rig.fake.sent) == 1


async def test_1601_chars_rejected_1600_sent(setup: RigFactory) -> None:
    rig = await setup()
    sender = rig.make()
    too_long = await sender.send(TO, "a" * 1601)
    assert too_long.status == "rejected"
    assert too_long.reason is not None
    assert re.search(r"1601.*1600", too_long.reason)
    assert (await sender.send(TO, "a" * 1600)).status == "sent"

    # Code points, not UTF-16 units: 1600 emoji are 3200 units but 1600 characters.
    assert (await sender.send(TO, "😀" * 1600)).status == "sent"
    assert (await sender.send(TO, "😀" * 1601)).status == "rejected"

    assert await sender.send(TO, "  \n ") == SendResult(
        status="rejected", reason="Message text is empty", to=TO
    )
    assert len(rig.fake.sent) == 2


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def capture_logger(name: str) -> tuple[logging.Logger, ListHandler]:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = ListHandler()
    logger.handlers = [handler]
    return logger, handler


async def test_log_has_entry_with_status_reason_and_no_body(setup: RigFactory) -> None:
    rig = await setup()
    capture, handler = capture_logger("aiogotoconnect.sms.test_send.capture")
    sender = rig.make(logger=capture)
    body = "secret code 998877"
    # Advance the clock between sends: same-ms log entries have no defined order.
    await sender.send(TO, body)
    rig.clock.t += 1000
    rig.fake.fail_next("POST", SEND_PATH, 403)
    await sender.send(TO, body)
    rig.clock.t += 1000
    await sender.send("nope", body)

    entries = await rig.log.list()
    assert len(entries) == 3
    assert entries[0].status == "invalid_number"
    assert entries[0].to == "nope"
    assert entries[1].status == "rejected"
    assert entries[1].reason is not None
    assert re.search("403", entries[1].reason)
    assert entries[2] == LogEntry(
        id=entries[2].id,
        direction="outbound",
        from_=FROM,
        to=TO,
        status="sent",
        at=entries[2].at,
        goto_id="msg-1",
    )
    stored = dict(await rig.store.list(T, "log:"))
    sent_raw = stored["log:" + entries[2].id]
    # The wire form is Node's: camelCase keys, absent fields omitted.
    assert sent_raw == {
        "id": entries[2].id,
        "direction": "outbound",
        "from": FROM,
        "to": TO,
        "status": "sent",
        "gotoId": "msg-1",
        "at": entries[2].at,
    }
    assert body not in repr(stored), "body never stored in the log"

    lines = [r.getMessage() for r in handler.records]
    assert len(lines) == 3, "one log line per send"
    for line in lines:
        assert body not in line, "no body in logs"
        assert TO not in line and "8015550123" not in line, "no full number in logs"
    assert "0123" in lines[0] and "sent" in lines[0]
    assert lines[0] == "goto-sms: send to …0123: sent"
    assert all(r.levelno == logging.INFO for r in handler.records)


async def test_message_log_lists_newest_first_with_a_limit() -> None:
    store = MemoryStore()
    clock = Clock(1_000_000)
    log = MessageLog(store, T, lambda: clock.t)
    for i in range(60):
        clock.t += 1000
        await log.append(direction="outbound", from_=FROM, to=TO, status=f"s{i}")
    page = await log.list()
    assert len(page) == 50
    assert page[0].status == "s59"
    assert page[49].status == "s10"
    assert [e.status for e in await log.list(2)] == ["s59", "s58"]
    assert page[0].at == iso_ms(clock.t)
    assert page[0].at == "1970-01-01T00:17:40.000Z"
    keys = await store.list(T, "log:")
    assert re.fullmatch(r"log:\d{15}:[0-9a-f]{8}", keys[-1][0])


async def test_message_log_with_a_fractional_clock_uses_whole_ms() -> None:
    # Python's wall clock has fractional ms; Node's Date.now() never does.
    store = MemoryStore()
    log = MessageLog(store, T, lambda: 1_730_000_000_123.789)
    e = await log.append(direction="inbound", from_=TO, to=FROM, status="received")
    assert re.fullmatch(r"001730000000123:[0-9a-f]{8}", e.id)
    assert e.at == "2024-10-27T03:33:20.123Z"
    ((key, _),) = await store.list(T, "log:")
    assert key == "log:" + e.id


async def test_message_log_skips_malformed_entries_and_keeps_malformed_keys() -> None:
    store = MemoryStore()
    clock = Clock(1_000_000_000_000)
    log = MessageLog(store, T, lambda: clock.t)
    await store.set(T, "log:junk", "not an entry")
    await log.append(direction="outbound", from_=FROM, to=TO, status="sent")
    assert [e.status for e in await log.list()] == ["sent"]
    clock.t += 365 * DAY
    await log.prune()
    # Node: keyTime() is NaN for a malformed key, so it never counts as expired.
    assert [k for k, _ in await store.list(T, "log:")] == ["log:junk"]


async def test_message_log_prune_removes_old_and_over_max_entries() -> None:
    store = MemoryStore()
    clock = Clock(1_000_000_000_000)
    log = MessageLog(store, T, lambda: clock.t, max_entries=5)

    async def add(status: str) -> None:
        await log.append(direction="outbound", from_=FROM, to=TO, status=status)

    await add("ancient")
    clock.t += 91 * DAY
    for i in range(7):
        clock.t += 1000
        await add(f"r{i}")
    await log.prune()
    assert [e.status for e in await log.list()] == ["r6", "r5", "r4", "r3", "r2"]

    # Default retention: 90 days, and append prunes on every 100th call.
    store2 = MemoryStore()
    log2 = MessageLog(store2, T, lambda: clock.t)
    assert log2.retention_days == 90
    await log2.append(direction="outbound", from_=FROM, to=TO, status="old")
    clock.t += 91 * DAY
    for _ in range(98):
        await log2.append(direction="outbound", from_=FROM, to=TO, status="new")
    assert len(await store2.list(T, "log:")) == 99, "no prune before the 100th append"
    await log2.append(direction="outbound", from_=FROM, to=TO, status="new")
    assert len(await store2.list(T, "log:")) == 99, "100th append pruned the old entry"
    assert all(e.status == "new" for e in await log2.list(200))


async def test_opt_outs_add_has_remove_list() -> None:
    store = MemoryStore()
    clock = Clock(1_000_000)
    o = OptOuts(store, T, lambda: clock.t)
    other = "+18015550124"
    assert await o.has(TO) is False
    await o.add(TO, "keyword")
    clock.t += 1000
    await o.add(other, "admin")
    assert await o.has(TO) is True
    assert await store.get(T, f"optout:{TO}") is not None
    assert await o.list() == [
        {"number": other, "at": iso_ms(clock.t), "source": "admin"},
        {"number": TO, "at": iso_ms(clock.t - 1000), "source": "keyword"},
    ]
    await o.remove(TO)
    assert await o.has(TO) is False
    assert [e["number"] for e in await o.list()] == [other]
    with pytest.raises(GoToSmsError) as e1:
        await o.add("call me", "admin")
    assert e1.value.code == "invalid_number"
    with pytest.raises(GoToSmsError) as e2:
        await o.remove("123")
    assert e2.value.code == "invalid_number"
    assert await o.has("junk") is False
