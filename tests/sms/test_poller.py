"""Port of packages/node/test/poller.test.ts (C19 dedupe/ordering, cursor, paging, lookback)."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

from aiogotoconnect.sms.config import ConfigResolver
from aiogotoconnect.sms.http import GoToHttp, create_goto_http
from aiogotoconnect.sms.inbound.keywords import DEFAULT_REPLIES
from aiogotoconnect.sms.inbound.poller import Poller
from aiogotoconnect.sms.inbound.processor import InboundProcessor
from aiogotoconnect.sms.log import MessageLog, iso_ms
from aiogotoconnect.sms.oauth import GoToAuth
from aiogotoconnect.sms.optouts import OptOuts
from aiogotoconnect.sms.secrets import SecretBox
from aiogotoconnect.sms.send import Sender
from aiogotoconnect.sms.store import MemoryStore
from aiogotoconnect.sms.types import InboundMessage

from .fake_goto import FakeGoTo, start_fake_goto

T = "t1"
FROM = "+18015550100"
PEER = "+18015550123"
T0 = datetime(2026, 10, 27, 10, 0, tzinfo=UTC).timestamp() * 1000
DAY = 24 * 3_600_000
MIN = 60_000
LOGGER = logging.getLogger("aiogotoconnect.sms.test_poller")
ENV = {"GOTO_CLIENT_ID": "cid", "GOTO_CLIENT_SECRET": "csecret", "GOTO_FROM_NUMBER": FROM}


@dataclass
class Clock:
    t: float = T0


@dataclass
class Rig:
    fake: FakeGoTo
    clock: Clock
    store: MemoryStore
    http: GoToHttp
    config: ConfigResolver
    opt_outs: OptOuts
    proc: InboundProcessor
    poller: Poller
    got: list[InboundMessage] = field(default_factory=list)

    def new_poller(self, now: Callable[[], float] | None = None, interval: float = 30.0) -> Poller:
        """A fresh Poller over the same store, like a restarted process."""
        return Poller(
            http=self.http,
            config=self.config,
            processor=self.proc,
            store=self.store,
            tenant=T,
            poll_interval_s=interval,
            now=now or (lambda: self.clock.t),
            logger=LOGGER,
        )

    def queries(self) -> list[dict[str, list[str]]]:
        return [
            parse_qs(urlsplit(r.path).query)
            for r in self.fake.requests
            if r.method == "GET" and r.path.startswith("/messaging/v1/conversations")
        ]

    def oldest(self) -> str:
        return self.queries()[0]["oldestTimestamp"][0]

    async def cursor(self) -> Any:
        return await self.store.get(T, "poll:cursor")


RigFactory = Callable[..., Awaitable[Rig]]


def inbound(id_: str, ts: str, body: str = "hi", frm: str = PEER, **extra: Any) -> dict[str, Any]:
    return {"id": id_, "from": frm, "to": FROM, "body": body, "timestamp": ts, **extra}


@pytest.fixture
async def setup() -> AsyncIterator[RigFactory]:
    cleanups: list[Callable[[], Awaitable[None]]] = []

    async def make(*, connect: bool = True, env: dict[str, str] | None = None) -> Rig:
        fake = await start_fake_goto()
        cleanups.append(fake.close)
        session = aiohttp.ClientSession()
        cleanups.append(session.close)
        clock = Clock()

        def now() -> float:
            return clock.t

        store = MemoryStore(now)
        box = SecretBox(os.urandom(32))
        config = ConfigResolver(store, T, box, {}, dict(ENV if env is None else env))
        auth = GoToAuth(
            store=store,
            tenant=T,
            box=box,
            config=config,
            session=session,
            auth_base_url=fake.auth_base_url,
            redirect_uri="https://app.example/goto/callback",
            logger=LOGGER,
            now=now,
        )
        http = create_goto_http(
            session, fake.api_base_url, auth.get_access_token, auth.force_refresh, logger=LOGGER
        )
        opt_outs = OptOuts(store, T, now)
        log = MessageLog(store, T, now)
        sender = Sender(
            http=http,
            auth=auth,
            config=config,
            opt_outs=opt_outs,
            log=log,
            store=store,
            tenant=T,
            now=now,
            send_timeout_s=10.0,
            max_body=1600,
            log_only=False,
            env=dict(ENV),
            logger=LOGGER,
        )
        if connect:
            url = await auth.start_connect()
            await auth.finish_connect("code-ok", parse_qs(urlsplit(url).query)["state"][0])
        got: list[InboundMessage] = []
        proc = InboundProcessor(
            store=store,
            tenant=T,
            sender=sender,
            opt_outs=opt_outs,
            log=log,
            emit=got.append,
            replies=DEFAULT_REPLIES.copy(),
            yes_is_opt_in=True,
            per_hour=10,
            now=now,
            logger=LOGGER,
        )
        rig = Rig(fake, clock, store, http, config, opt_outs, proc, None, got)  # type: ignore[arg-type]
        rig.poller = rig.new_poller()
        fake.requests.clear()
        return rig

    try:
        yield make
    finally:
        while cleanups:
            await cleanups.pop()()


def ids(rig: Rig) -> list[str]:
    return [m.id for m in rig.got]


async def test_c19_polling_dedupes_across_ticks(setup: RigFactory) -> None:
    rig = await setup()
    rig.fake.inbound.append(inbound("g-1", "2026-10-27T10:00:01Z"))
    assert await rig.poller.tick() == 1
    # Same message still inside the overlap window, plus a new one.
    rig.fake.inbound.append(inbound("g-2", "2026-10-27T10:00:02Z", "again"))
    assert await rig.poller.tick() == 1
    assert await rig.poller.tick() == 0
    assert ids(rig) == ["g-1", "g-2"]
    assert rig.got[0].text == "hi"
    assert rig.got[0].from_ == PEER
    assert rig.got[0].to == FROM


async def test_c19_mixed_micro_and_milli_timestamps_ordered_by_parsed_instant(
    setup: RigFactory,
) -> None:
    rig = await setup()
    # The fake serves messages newest first; the poller must order them itself.
    rig.fake.inbound.append(inbound("micro", "2026-10-27T10:00:00.123456Z", "first"))
    rig.fake.inbound.append(inbound("milli", "2026-10-27T10:00:00.2Z", "second"))
    assert await rig.poller.tick() == 2
    assert ids(rig) == ["micro", "milli"]
    assert await rig.cursor() == "2026-10-27T10:00:00.200Z"
    # received_at keeps GoTo's own string.
    assert rig.got[0].received_at == "2026-10-27T10:00:00.123456Z"


async def test_cursor_survives_restart(setup: RigFactory) -> None:
    rig = await setup()
    rig.fake.inbound.append(inbound("g-1", "2026-10-27T10:00:05Z"))
    await rig.poller.tick()
    assert await rig.cursor() == "2026-10-27T10:00:05.000Z"

    # An hour later a new process starts: it polls from the stored cursor (minus the
    # overlap), not from its own clock, so nothing sent while it was down is missed.
    rig.clock.t = T0 + 3_600_000
    rig.fake.inbound.append(inbound("g-2", "2026-10-27T10:30:00Z", "while down"))
    rig.fake.requests.clear()
    assert await rig.new_poller().tick() == 1
    assert rig.oldest() == "2026-10-27T09:55:05.000Z"
    assert ids(rig) == ["g-1", "g-2"]


async def test_poller_skips_an_invalid_inbound_and_keeps_going(
    setup: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    rig = await setup()
    rig.fake.inbound.append(inbound("empty", "2026-10-27T10:00:01Z", ""))
    rig.fake.inbound.append(inbound("ok", "2026-10-27T10:00:02Z", "fine"))
    with caplog.at_level(logging.DEBUG, logger=LOGGER.name):
        assert await rig.new_poller(lambda: T0).tick() == 1
    assert ids(rig) == ["ok"]
    assert any(r.levelno == logging.WARNING and "skipped" in r.getMessage() for r in caplog.records)
    assert await rig.cursor() == "2026-10-27T10:00:02.000Z"


async def test_poller_retries_a_pending_row_whose_processing_failed(setup: RigFactory) -> None:
    rig = await setup()
    real_add = rig.opt_outs.add
    fail = True

    async def flaky_add(e164: str, source: Any) -> None:
        if fail:
            raise OSError("disk full")
        await real_add(e164, source)

    rig.opt_outs.add = flaky_add  # type: ignore[method-assign]
    rig.fake.inbound.append(inbound("g-1", "2026-10-27T10:00:01Z", "STOP"))
    await rig.poller.tick()
    assert rig.got == []
    fail = False
    await rig.poller.tick()
    assert len(rig.got) == 1
    assert await rig.opt_outs.has(PEER) is True


async def test_poller_without_a_from_number_does_nothing(setup: RigFactory) -> None:
    rig = await setup(connect=False, env={"GOTO_CLIENT_ID": "cid", "GOTO_CLIENT_SECRET": "s"})
    assert await rig.poller.tick() == 0
    assert rig.fake.requests == []


async def test_cursor_follows_our_own_sends_too_so_the_poll_window_stays_short(
    setup: RigFactory,
) -> None:
    rig = await setup()
    rig.fake.inbound.append(inbound("in", "2026-10-27T10:00:01Z"))
    rig.fake.inbound.append(inbound("out", "2026-10-27T10:20:00Z", "reply", direction="OUT"))
    assert await rig.poller.tick() == 1
    assert ids(rig) == ["in"]
    assert await rig.cursor() == "2026-10-27T10:20:00.000Z"
    rig.fake.requests.clear()
    await rig.poller.tick()
    assert rig.oldest() == "2026-10-27T10:15:00.000Z"


async def test_poller_follows_next_page_marker_stop_only_on_page_2_is_handled(
    setup: RigFactory,
) -> None:
    rig = await setup()
    rig.fake.poll_page_size = 2
    # Three contacts, so the conversations list spans two pages; the STOP is on the last one.
    for i, frm in enumerate(["+18015550201", "+18015550202", PEER]):
        rig.fake.inbound.append(
            inbound(f"g-{i}", f"2026-10-27T10:00:0{i + 1}Z", "STOP" if frm == PEER else "hi", frm)
        )
    # And two messages from PEER, so its message list pages too.
    rig.fake.inbound.append(inbound("g-a", "2026-10-27T10:00:04Z", "one"))
    rig.fake.inbound.append(inbound("g-b", "2026-10-27T10:00:05Z", "two"))
    assert await rig.poller.tick() == 5
    assert sorted(ids(rig)) == ["g-0", "g-1", "g-2", "g-a", "g-b"]
    assert await rig.opt_outs.has(PEER) is True
    assert [q.get("pageMarker") for q in rig.queries()] == [None, ["2"]]


async def test_poller_keeps_the_cursor_when_paging_loops(
    setup: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    rig = await setup()
    rig.fake.inbound.append(inbound("g-1", "2026-10-27T10:00:01Z"))
    # A marker that repeats: the poller stops paging and must not advance the cursor.
    loop = {
        "items": [{"contactPhoneNumbers": [PEER], "lastMessageTimestamp": "2026-10-27T10:00:01Z"}],
        "nextPageMarker": "same",
    }
    rig.fake.fail_next("GET", "/messaging/v1/conversations", 200, loop)
    rig.fake.fail_next("GET", "/messaging/v1/conversations", 200, loop)
    with caplog.at_level(logging.DEBUG, logger=LOGGER.name):
        assert await rig.new_poller(lambda: T0).tick() == 1
    assert await rig.cursor() is None
    assert any(r.levelno == logging.WARNING and "paging" in r.getMessage() for r in caplog.records)


async def test_lookback_is_capped_at_24h_logged_at_error_level_then_the_cursor_moves_on(
    setup: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    rig = await setup()
    # A cursor left from weeks ago (e.g. polling turned off and back on much later).
    await rig.store.set(T, "poll:cursor", iso_ms(T0 - 21 * DAY))
    with caplog.at_level(logging.DEBUG, logger=LOGGER.name):
        await rig.new_poller(lambda: T0).tick()
    assert rig.oldest() == iso_ms(T0 - DAY)
    assert any(r.levelno == logging.ERROR and "24 h" in r.getMessage() for r in caplog.records)
    # A complete tick moves the stale cursor up to now (minus the overlap).
    assert await rig.cursor() == iso_ms(T0 - 5 * MIN)


async def test_incomplete_paging_holds_the_cursor_but_the_window_never_grows_past_24h(
    setup: RigFactory,
) -> None:
    rig = await setup()
    await rig.store.set(T, "poll:cursor", iso_ms(T0 - 2 * DAY))
    loop = {"items": [], "nextPageMarker": "same"}

    def stuck() -> None:
        rig.fake.fail_next("GET", "/messaging/v1/conversations", 200, loop)
        rig.fake.fail_next("GET", "/messaging/v1/conversations", 200, loop)

    stuck()
    rig.fake.requests.clear()
    await rig.new_poller(lambda: T0).tick()
    assert rig.oldest() == iso_ms(T0 - DAY)
    assert await rig.cursor() == iso_ms(T0 - 2 * DAY)
    # An hour later, still incomplete: the window slides with now instead of growing.
    stuck()
    rig.fake.requests.clear()
    await rig.new_poller(lambda: T0 + 3_600_000).tick()
    assert rig.oldest() == iso_ms(T0 + 3_600_000 - DAY)


async def test_a_quiet_number_the_cursor_moves_up_once_it_is_an_hour_stale(
    setup: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    rig = await setup()
    await rig.store.set(T, "poll:cursor", iso_ms(T0 - 30 * MIN))
    with caplog.at_level(logging.DEBUG, logger=LOGGER.name):
        await rig.new_poller(lambda: T0).tick()
        # Under an hour stale: left alone (no store write every tick).
        assert await rig.cursor() == iso_ms(T0 - 30 * MIN)
        await rig.new_poller(lambda: T0 + 3_600_000).tick()
    assert await rig.cursor() == iso_ms(T0 + 3_600_000 - 5 * MIN)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


# --- Python-specific: loop lifecycle, single flight, resilience ---


async def test_first_poll_with_no_cursor_starts_from_now(setup: RigFactory) -> None:
    rig = await setup()
    rig.fake.inbound.append(inbound("old", "2026-10-27T09:00:00Z"))
    assert await rig.poller.tick() == 0
    assert rig.oldest() == iso_ms(T0 - 5 * MIN)


async def test_unparseable_timestamps_sort_last_and_get_now_as_received_at(
    setup: RigFactory,
) -> None:
    rig = await setup()
    rig.fake.inbound.append(inbound("bad", "not-a-date"))
    rig.fake.inbound.append(inbound("good", "2026-10-27T10:00:01Z"))
    # The fake itself drops "bad" (it compares timestamps as 0), so serve it by hand.
    rig.fake.inbound.clear()
    page = {
        "items": [
            {
                "id": "bad",
                "direction": "IN",
                "authorPhoneNumber": PEER,
                "ownerPhoneNumber": FROM,
                "body": "x",
                "timestamp": "not-a-date",
            },
            {
                "id": "good",
                "direction": "IN",
                "authorPhoneNumber": PEER,
                "ownerPhoneNumber": FROM,
                "body": "y",
                "timestamp": "2026-10-27T10:00:01Z",
            },
        ]
    }
    convs = {
        "items": [{"contactPhoneNumbers": [PEER], "lastMessageTimestamp": "2026-10-27T10:00:01Z"}]
    }
    rig.fake.fail_next("GET", "/messaging/v1/conversations", 200, convs)
    rig.fake.fail_next("GET", "/messaging/v1/messages", 200, page)
    # "bad" gets the current time as received_at; it is later than "good", so it replays last.
    await rig.store.set(T, "poll:cursor", iso_ms(T0))
    rig.clock.t = T0 + 3_600_000
    assert await rig.poller.tick() == 2
    assert ids(rig) == ["good", "bad"]
    assert rig.got[1].received_at == iso_ms(T0 + 3_600_000)


async def test_our_own_number_is_never_treated_as_inbound(setup: RigFactory) -> None:
    rig = await setup()
    rig.fake.inbound.append(inbound("self", "2026-10-27T10:00:01Z", frm=FROM))
    assert await rig.poller.tick() == 0
    assert rig.got == []


async def test_concurrent_ticks_share_one_poll(setup: RigFactory) -> None:
    rig = await setup()
    rig.fake.inbound.append(inbound("g-1", "2026-10-27T10:00:01Z"))
    a, b = await asyncio.gather(rig.poller.tick(), rig.poller.tick())
    assert (a, b) == (1, 1)
    assert len(rig.queries()) == 1


async def test_a_failing_tick_raises_and_the_next_one_recovers(setup: RigFactory) -> None:
    rig = await setup()
    rig.fake.inbound.append(inbound("g-1", "2026-10-27T10:00:01Z"))
    rig.fake.fail_next("GET", "/messaging/v1/conversations", 500, {"message": "boom"})
    with pytest.raises(Exception):  # noqa: B017 - any API error
        await rig.poller.tick()
    assert await rig.poller.tick() == 1


async def test_start_polls_immediately_survives_errors_and_stop_leaves_no_task(
    setup: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    rig = await setup()
    poller = rig.new_poller(interval=0.01)
    rig.fake.fail_next("GET", "/messaging/v1/conversations", 500, {"message": "boom"})
    rig.fake.inbound.append(inbound("g-1", "2026-10-27T10:00:01Z"))
    before = asyncio.all_tasks()
    with caplog.at_level(logging.DEBUG, logger=LOGGER.name):
        poller.start()
        poller.start()  # idempotent: still one loop
        assert poller.running
        for _ in range(300):
            if rig.got:
                break
            await asyncio.sleep(0.01)
    assert ids(rig) == ["g-1"]  # the first tick failed; the loop went on
    assert any("polling for inbound texts failed" in r.getMessage() for r in caplog.records)
    poller.stop()
    await poller.idle()
    assert not poller.running
    assert asyncio.all_tasks() <= before
    n = len(rig.queries())
    await asyncio.sleep(0.05)
    assert len(rig.queries()) == n  # nothing polls after stop


async def test_stop_before_start_and_restart_are_fine(setup: RigFactory) -> None:
    rig = await setup()
    poller = rig.new_poller(interval=0.01)
    poller.stop()
    poller.start()
    poller.stop()
    poller.start()
    assert poller.running
    poller.stop()
    await poller.idle()


async def test_reset_cursor_moves_the_cursor_to_now(setup: RigFactory) -> None:
    rig = await setup()
    await rig.poller.reset_cursor()
    assert await rig.cursor() == iso_ms(T0)
