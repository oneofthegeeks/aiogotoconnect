"""Port of packages/node/test/inbound.test.ts (keywords C20, rate limit C21, durability, dedupe)."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

from aiogotoconnect.sms.config import ConfigResolver
from aiogotoconnect.sms.errors import GoToSmsError
from aiogotoconnect.sms.http import create_goto_http
from aiogotoconnect.sms.inbound.keywords import DEFAULT_REPLIES, detect_keyword
from aiogotoconnect.sms.inbound.processor import InboundProcessor, call_handlers
from aiogotoconnect.sms.log import MessageLog, iso_ms
from aiogotoconnect.sms.oauth import GoToAuth
from aiogotoconnect.sms.optouts import OptOuts
from aiogotoconnect.sms.secrets import SecretBox
from aiogotoconnect.sms.send import Sender
from aiogotoconnect.sms.store import FileStore, MemoryStore
from aiogotoconnect.sms.store.types import GoToSmsStore, Json
from aiogotoconnect.sms.types import InboundMessage, LogEntry

from .fake_goto import FakeGoTo, start_fake_goto

T = "t1"
FROM = "+18015550100"  # our sending number
PEER = "+18015550123"  # the person texting in
HOUR = 3_600_000
DAY = 86_400_000
SILENT = logging.getLogger("aiogotoconnect.sms.test_inbound")
ENV = {"GOTO_CLIENT_ID": "cid", "GOTO_CLIENT_SECRET": "csecret", "GOTO_FROM_NUMBER": FROM}

Emit = Callable[[InboundMessage], Awaitable[None] | None]


@dataclass
class Clock:
    t: float = 1_000_000_000_000


@dataclass
class Rig:
    fake: FakeGoTo
    clock: Clock
    store: GoToSmsStore
    box: SecretBox
    opt_outs: OptOuts
    log: MessageLog
    sender: Sender
    proc: InboundProcessor
    emitted: list[InboundMessage] = field(default_factory=list)
    n: int = 0

    def msg(self, text: str, **over: Any) -> InboundMessage:
        self.n += 1
        fields: dict[str, Any] = {
            "id": f"g-{self.n}",
            "from_": PEER,
            "to": FROM,
            "text": text,
            "received_at": iso_ms(self.clock.t),
            "raw": {"goto": "payload", "text": text},
            **over,
        }
        return InboundMessage(**fields)

    async def receive(self, text: str, **over: Any) -> InboundMessage:
        """persist + process, the normal path."""
        m = self.msg(text, **over)
        assert await self.proc.persist(m) is True
        await self.proc.process(m.id)
        return m

    def replies_to(self, body: str) -> list[dict[str, Any]]:
        return [
            s for s in self.fake.sent if s["contactPhoneNumbers"][0] == PEER and s["body"] == body
        ]

    async def inbound_log(self) -> list[LogEntry]:
        return [e for e in await self.log.list(500) if e.direction == "inbound"]


RigFactory = Callable[..., Awaitable[Rig]]


@pytest.fixture
async def setup() -> AsyncIterator[RigFactory]:
    cleanups: list[Callable[[], Awaitable[None]]] = []

    async def make_rig(
        *,
        fake: FakeGoTo | None = None,
        box: SecretBox | None = None,
        store: GoToSmsStore | None = None,
        clock: Clock | None = None,
        emit: Emit | None = None,
        connect: bool = True,
        opt_outs_cls: type[OptOuts] = OptOuts,
    ) -> Rig:
        if fake is None:
            fake = await start_fake_goto()
            cleanups.append(fake.close)
        session = aiohttp.ClientSession()
        cleanups.append(session.close)
        clock_ = clock or Clock()

        def now() -> float:
            return clock_.t

        store_: GoToSmsStore = store if store is not None else MemoryStore(now)
        box_ = box or SecretBox(os.urandom(32))
        config = ConfigResolver(store_, T, box_, {}, dict(ENV))
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
            session, fake.api_base_url, auth.get_access_token, auth.force_refresh, logger=SILENT
        )
        opt_outs = opt_outs_cls(store_, T, now)
        log = MessageLog(store_, T, now)
        sender = Sender(
            http=http,
            auth=auth,
            config=config,
            opt_outs=opt_outs,
            log=log,
            store=store_,
            tenant=T,
            now=now,
            send_timeout_s=10.0,
            max_body=1600,
            log_only=False,
            env=dict(ENV),
            logger=SILENT,
        )
        if connect:
            url = await auth.start_connect()
            await auth.finish_connect("code-ok", parse_qs(urlsplit(url).query)["state"][0])
        emitted: list[InboundMessage] = []
        proc = InboundProcessor(
            store=store_,
            tenant=T,
            sender=sender,
            opt_outs=opt_outs,
            log=log,
            emit=emit or emitted.append,
            replies=DEFAULT_REPLIES.copy(),
            yes_is_opt_in=True,
            per_hour=10,
            now=now,
            logger=SILENT,
        )
        return Rig(fake, clock_, store_, box_, opt_outs, log, sender, proc, emitted)

    try:
        yield make_rig
    finally:
        while cleanups:
            await cleanups.pop()()


# --- detect_keyword (Review Focus 2 in Node) ---


@pytest.mark.parametrize("text", ["Stop.", " stop ", "STOP!", "stop?!", "Quit,", "unsubscribe"])
def test_detect_keyword_punctuation_and_padding_still_match_stop(text: str) -> None:
    assert detect_keyword(text, yes_is_opt_in=True) == "STOP"


@pytest.mark.parametrize(
    "text", ["Unsubscribe please", "please stop", "stop it", "STOP STOP", "", "."]
)
def test_detect_keyword_only_a_whole_message_match_counts(text: str) -> None:
    assert detect_keyword(text, yes_is_opt_in=True) is None


def test_detect_keyword_start_help_and_yes_only_when_yes_is_opt_in() -> None:
    assert detect_keyword("unstop", yes_is_opt_in=False) == "START"
    assert detect_keyword("Start!", yes_is_opt_in=False) == "START"
    assert detect_keyword("info", yes_is_opt_in=False) == "HELP"
    assert detect_keyword("Help?", yes_is_opt_in=False) == "HELP"
    assert detect_keyword("yes", yes_is_opt_in=True) == "START"
    assert detect_keyword("yes", yes_is_opt_in=False) is None


def test_default_replies_match_node() -> None:
    assert DEFAULT_REPLIES == {
        "stop": "You're unsubscribed and won't get more texts from this number. "
        "Reply START to resubscribe.",
        "start": "You're resubscribed. Reply STOP to unsubscribe.",
        "help": "Reply STOP to unsubscribe. Msg & data rates may apply.",
    }


# --- keywords ---


async def test_c20_stop_adds_opt_out_sends_one_confirmation_emits_with_keyword(
    setup: RigFactory,
) -> None:
    s = await setup()
    m = await s.receive("Stop.")
    assert await s.opt_outs.has(PEER) is True
    assert (await s.opt_outs.list())[0]["source"] == "keyword"
    assert len(s.replies_to(DEFAULT_REPLIES["stop"])) == 1
    assert len(s.fake.sent) == 1
    assert len(s.emitted) == 1
    assert s.emitted[0].keyword == "STOP"
    assert s.emitted[0].text == "Stop."
    assert s.emitted[0].raw == m.raw

    # Processing the same id again is a no-op: no second confirmation, no second emit.
    await s.proc.process(m.id)
    assert await s.proc.persist(m) is False
    assert len(s.fake.sent) == 1
    assert len(s.emitted) == 1

    # A regular send to the now opted-out number is refused.
    assert (await s.sender.send(PEER, "promo")).status == "opted_out"


async def test_stop_replayed_after_a_crash_past_the_reply_sends_no_second_confirmation(
    setup: RigFactory,
) -> None:
    # Fail the "seen" write once: the reply has gone out but the message stays pending.
    class FlakyStore(MemoryStore):
        fail_seen = True

        async def set(self, tenant: str, key: str, value: Json) -> None:
            if self.fail_seen and key.startswith("inbound:seen:"):
                self.fail_seen = False
                raise OSError("disk full")
            await super().set(tenant, key, value)

    clock = Clock()
    s = await setup(clock=clock, store=FlakyStore(lambda: clock.t))
    m = s.msg("STOP")
    await s.proc.persist(m)
    with pytest.raises(OSError, match="disk full"):
        await s.proc.process(m.id)
    assert await s.opt_outs.has(PEER) is True

    assert await s.proc.replay_pending() == 1
    assert len(s.replies_to(DEFAULT_REPLIES["stop"])) == 1
    assert await s.proc.persist(m) is False


async def test_c20_start_removes(setup: RigFactory) -> None:
    s = await setup()
    await s.opt_outs.add(PEER, "keyword")
    await s.receive("start")
    assert await s.opt_outs.has(PEER) is False
    assert len(s.replies_to(DEFAULT_REPLIES["start"])) == 1
    assert s.emitted[0].keyword == "START"


async def test_c20_help_replies_with_configured_text(setup: RigFactory) -> None:
    s = await setup()
    await s.opt_outs.add(PEER, "admin")
    help_text = "Acme Clinic: reply STOP to opt out. Call 801-555-0199."
    proc = InboundProcessor(
        store=s.store,
        tenant=T,
        sender=s.sender,
        opt_outs=s.opt_outs,
        log=s.log,
        emit=s.emitted.append,
        replies={**DEFAULT_REPLIES, "help": help_text},
        yes_is_opt_in=True,
        per_hour=10,
        now=lambda: s.clock.t,
        logger=SILENT,
    )
    m = s.msg("help")
    await proc.persist(m)
    await proc.process(m.id)
    # Answered even though the number opted out; HELP doesn't change the opt-out.
    assert len(s.replies_to(help_text)) == 1
    assert await s.opt_outs.has(PEER) is True
    assert s.emitted[0].keyword == "HELP"


async def test_plain_text_no_reply_emitted_without_keyword_logged_without_body(
    setup: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=SILENT.name)
    s = await setup()
    await s.receive("Is my appointment still on?")
    assert len(s.fake.sent) == 0
    assert len(s.emitted) == 1
    assert s.emitted[0].keyword is None
    entries = await s.inbound_log()
    assert len(entries) == 1
    assert entries[0].status == "received"
    assert entries[0].goto_id == "g-1"
    assert entries[0].from_ == PEER
    assert "appointment" not in repr(entries)
    assert "appointment" not in str(await s.store.list(T, "log:"))
    # Node's info line, without the body.
    assert "goto-sms: inbound from …0123: received" in caplog.messages
    assert not any("appointment" in r.getMessage() for r in caplog.records)


# --- rate limit (C21) ---


async def test_c21_11th_text_in_an_hour_is_logged_and_not_emitted(setup: RigFactory) -> None:
    s = await setup()
    for i in range(11):
        await s.receive(f"hello {i}")
    assert len(s.emitted) == 10
    statuses = [e.status for e in await s.inbound_log()]
    assert statuses.count("received") == 10
    assert statuses.count("rate_limited") == 1
    # The limited message is finished, not left pending.
    assert await s.proc.replay_pending() == 0

    # The next hour starts a fresh bucket.
    s.clock.t += HOUR
    await s.receive("new hour")
    assert len(s.emitted) == 11


async def test_stop_as_12th_text_in_the_hour_still_opts_out(setup: RigFactory) -> None:
    s = await setup()
    for i in range(11):
        await s.receive(f"spam {i}")
    await s.receive("STOP")
    assert await s.opt_outs.has(PEER) is True
    assert len(s.replies_to(DEFAULT_REPLIES["stop"])) == 1
    assert len(s.emitted) == 10
    assert not any(m.keyword == "STOP" for m in s.emitted)


async def test_rate_limit_is_per_number(setup: RigFactory) -> None:
    s = await setup()
    for i in range(11):
        await s.receive(f"a {i}")
    await s.receive("other person", from_="+18015550124")
    assert len(s.emitted) == 11
    assert s.emitted[-1].from_ == "+18015550124"


# --- durability and dedupe ---


async def test_pending_inbound_replayed_after_restart(
    setup: RigFactory, fake_goto: FakeGoTo, tmp_path: Path
) -> None:
    path = tmp_path / "store.json"
    box = SecretBox(os.urandom(32))

    a = await setup(fake=fake_goto, box=box, store=FileStore(path))
    m = a.msg("STOP")
    assert await a.proc.persist(m) is True
    # "Crash" before process(): nothing emitted, no opt-out yet.
    assert len(a.emitted) == 0

    b = await setup(fake=fake_goto, box=box, store=FileStore(path), connect=False)
    assert await b.proc.replay_pending() == 1
    assert len(b.emitted) == 1
    assert b.emitted[0].id == m.id
    assert b.emitted[0].keyword == "STOP"
    assert await b.opt_outs.has(PEER) is True
    assert len(b.replies_to(DEFAULT_REPLIES["stop"])) == 1

    # Nothing left: a second replay (or a third process) emits nothing more.
    assert await b.proc.replay_pending() == 0
    c = await setup(fake=fake_goto, box=box, store=FileStore(path), connect=False)
    assert await c.proc.replay_pending() == 0
    assert await c.proc.persist(m) is False
    assert len(b.emitted) + len(c.emitted) == 1


async def test_replay_applies_pending_keywords_in_arrival_order_not_id_order(
    setup: RigFactory,
) -> None:
    s = await setup()
    # 'g-10' sorts before 'g-2' by key, but the STOP arrived last and must win.
    await s.proc.persist(s.msg("START", id="g-2", received_at=iso_ms(s.clock.t)))
    await s.proc.persist(s.msg("STOP", id="g-10", received_at=iso_ms(s.clock.t + 1000)))
    assert await s.proc.replay_pending() == 2
    assert await s.opt_outs.has(PEER) is True
    assert [m.id for m in s.emitted] == ["g-2", "g-10"]


async def test_replay_puts_unreadable_received_at_last_with_key_tie_break(
    setup: RigFactory,
) -> None:
    s = await setup()
    await s.proc.persist(s.msg("b", id="g-b", received_at="not a date"))
    await s.proc.persist(s.msg("a", id="g-a", received_at="not a date"))
    await s.proc.persist(s.msg("c", id="g-c", received_at=iso_ms(s.clock.t + 5000)))
    assert await s.proc.replay_pending() == 3
    assert [m.id for m in s.emitted] == ["g-c", "g-a", "g-b"]


async def test_duplicate_goto_id_emitted_once(setup: RigFactory) -> None:
    s = await setup()
    m = s.msg("hi")
    assert await s.proc.persist(m) is True
    assert await s.proc.persist(m) is False  # still pending
    await asyncio.gather(s.proc.process(m.id), s.proc.process(m.id))
    assert await s.proc.persist(m) is False  # already seen
    await s.proc.process(m.id)
    assert len(s.emitted) == 1
    assert len(await s.inbound_log()) == 1


async def test_concurrent_duplicate_stop_sends_one_confirmation(setup: RigFactory) -> None:
    s = await setup()
    m = s.msg("STOP")
    await s.proc.persist(m)
    await asyncio.gather(s.proc.process(m.id), s.proc.process(m.id))
    assert len(s.replies_to(DEFAULT_REPLIES["stop"])) == 1
    # Python extra: the STOP's reply awaits real I/O, so the second call overlaps the first;
    # it must be a no-op (one emit, one log entry), not just deduped at the sender.
    assert len(s.emitted) == 1
    assert len(await s.inbound_log()) == 1


async def test_handler_that_throws_doesnt_stop_processing(setup: RigFactory) -> None:
    calls = 0
    seen: list[str] = []

    def emit(m: InboundMessage) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("handler bug")
        seen.append(m.id)

    s = await setup(emit=emit)
    first = await s.receive("first")
    # The throwing message is finished: marked seen, nothing pending.
    assert await s.proc.persist(first) is False
    assert await s.proc.replay_pending() == 0
    second = await s.receive("second")
    assert seen == [second.id]


@pytest.mark.parametrize("raiser_kind", ["sync", "async"])
async def test_handler_exception_is_logged_and_processing_continues(
    setup: RigFactory, caplog: pytest.LogCaptureFixture, raiser_kind: str
) -> None:
    """Review Focus 4: a sync or async raising handler is logged; others still run."""
    caplog.set_level(logging.DEBUG, logger=SILENT.name)
    received: list[str] = []

    def sync_raiser(m: InboundMessage) -> None:
        raise ValueError(f"bad handler saw {m.text}")

    async def async_raiser(m: InboundMessage) -> None:
        await asyncio.sleep(0)
        raise ValueError(f"bad handler saw {m.text}")

    async def good_async(m: InboundMessage) -> None:
        await asyncio.sleep(0)
        received.append(f"async:{m.id}")

    def good_sync(m: InboundMessage) -> None:
        received.append(f"sync:{m.id}")

    raiser: Emit = sync_raiser if raiser_kind == "sync" else async_raiser
    handlers: list[Emit] = [raiser, good_async, good_sync]

    async def emit(m: InboundMessage) -> None:
        await call_handlers(handlers, m, SILENT)

    s = await setup(emit=emit)
    first = await s.receive("secret words")
    assert received == [f"async:{first.id}", f"sync:{first.id}"]
    # Marked processed: seen, nothing pending, persisting again is a no-op.
    assert await s.proc.persist(first) is False
    assert await s.proc.replay_pending() == 0
    # Logged at WARNING with only the error's name (never the message text at INFO+).
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(r.getMessage() == "goto-sms: inbound handler threw (ValueError)" for r in warnings)
    assert not any("secret words" in r.getMessage() for r in warnings)
    assert not any(
        r.exc_info and "secret words" in str(r.exc_info[1])
        for r in caplog.records
        if r.levelno >= logging.INFO
    )
    # Full traceback is available at DEBUG for diagnosis.
    assert any(r.levelno == logging.DEBUG and r.exc_info for r in caplog.records)

    # Processing keeps going for the next message, and every handler still runs.
    second = await s.receive("next")
    assert received[-2:] == [f"async:{second.id}", f"sync:{second.id}"]


async def test_emit_that_raises_directly_is_logged_by_the_processor(
    setup: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Backstop as in Node: an emit (sync or async) that itself raises never fails processing."""
    caplog.set_level(logging.WARNING, logger=SILENT.name)

    async def emit(m: InboundMessage) -> None:
        raise KeyError(m.text)

    s = await setup(emit=emit)
    m = await s.receive("hello")
    assert await s.proc.persist(m) is False
    assert "goto-sms: inbound handler threw (KeyError)" in caplog.messages
    assert not any("hello" in msg for msg in caplog.messages)


async def test_keyword_replies_are_capped_at_one_per_keyword_per_number_per_hour(
    setup: RigFactory,
) -> None:
    s = await setup()
    for _ in range(5):
        await s.receive("HELP")
    for _ in range(5):
        await s.receive("STOP")
    assert len(s.replies_to(DEFAULT_REPLIES["help"])) == 1
    assert len(s.replies_to(DEFAULT_REPLIES["stop"])) == 1
    assert len(s.fake.sent) == 2
    # All ten were processed and logged; the opt-out still applied.
    assert len(await s.inbound_log()) == 10
    assert len(s.emitted) == 10
    assert await s.opt_outs.has(PEER) is True

    # A new hour allows a new confirmation.
    s.clock.t += HOUR
    await s.receive("STOP")
    assert len(s.replies_to(DEFAULT_REPLIES["stop"])) == 2


async def test_a_stop_persisted_without_received_at_or_to_still_opts_out(
    setup: RigFactory,
) -> None:
    s = await setup()
    m = cast(Any, SimpleNamespace(id="bare-1", from_=PEER, text="STOP", raw={}))
    assert await s.proc.persist(m) is True
    await s.proc.process("bare-1")
    assert await s.opt_outs.has(PEER) is True
    assert s.emitted[0].to == ""
    assert s.emitted[0].received_at == iso_ms(s.clock.t)


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "x", "text": "STOP"},
        {"id": "x", "from_": "", "text": "STOP"},
        {"from_": PEER, "text": "STOP"},
        {"id": "x", "from_": PEER},
    ],
)
async def test_persist_rejects_a_message_without_id_from_or_text(
    setup: RigFactory, bad: dict[str, Any]
) -> None:
    s = await setup()
    with pytest.raises(GoToSmsError) as exc:
        await s.proc.persist(cast(Any, SimpleNamespace(**bad)))
    assert exc.value.code == "invalid_inbound"
    assert await s.store.list(T, "inbound:pending:") == []


async def test_start_reply_does_not_bypass_an_opt_out_that_came_back_before_the_reply(
    setup: RigFactory,
) -> None:
    # A STOP lands between START's opt-out removal and its reply.
    class RacyOptOuts(OptOuts):
        async def remove(self, e164: str) -> None:
            await super().remove(e164)
            await super().add(e164, "keyword")

    s = await setup(opt_outs_cls=RacyOptOuts)
    await s.opt_outs.add(PEER, "keyword")
    await s.receive("START")
    assert len(s.replies_to(DEFAULT_REPLIES["start"])) == 0
    assert s.emitted[0].keyword == "START"


async def test_a_failed_reply_does_not_block_the_opt_out_or_the_emit(setup: RigFactory) -> None:
    s = await setup(connect=False)  # not connected: the reply can't be sent
    await s.receive("STOP")
    assert await s.opt_outs.has(PEER) is True
    assert len(s.fake.sent) == 0
    assert s.emitted[0].keyword == "STOP"


async def test_keyword_from_an_unusable_number_is_ignored_but_processed(
    setup: RigFactory,
) -> None:
    s = await setup()
    m = await s.receive("STOP", from_="12")
    assert len(s.fake.sent) == 0
    assert s.emitted[0].keyword == "STOP"
    assert await s.proc.persist(m) is False


async def test_a_malformed_pending_row_is_dropped(setup: RigFactory) -> None:
    s = await setup()
    await s.store.set(T, "inbound:pending:junk", {"id": "junk"})
    assert await s.proc.replay_pending() == 1
    assert await s.store.list(T, "inbound:pending:") == []
    assert s.emitted == []


# --- prune ---


async def test_prune_drops_old_rate_buckets_and_expired_seen_ids(setup: RigFactory) -> None:
    s = await setup()
    await s.receive("old")
    s.clock.t += 91 * DAY
    await s.receive("new")
    await s.proc.prune()
    keys = [k for k, _ in await s.store.list(T, "rate:")] + [
        k for k, _ in await s.store.list(T, "inbound:seen:")
    ]
    bucket = int(s.clock.t // HOUR)
    assert sorted(keys) == sorted(["inbound:seen:g-2", f"rate:{PEER}:{bucket}"])
