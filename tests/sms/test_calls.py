"""Calls to the sending number as events: on("call"), CallWatcher, status (no Node counterpart yet)."""

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

from aiogotoconnect.sms import InboundCall, MemoryStore, create_goto_sms
from aiogotoconnect.sms.client import GoToSms
from aiogotoconnect.sms.config import ConfigResolver
from aiogotoconnect.sms.http import GoToHttp, create_goto_http
from aiogotoconnect.sms.inbound.calls import REPORTS, SUMMARIES, CallWatcher
from aiogotoconnect.sms.log import iso_ms
from aiogotoconnect.sms.oauth import GoToAuth
from aiogotoconnect.sms.secrets import SecretBox
from aiogotoconnect.sms.server.status import build_status

from .fake_goto import FakeGoTo, FakeResponse, start_fake_goto
from .helpers import connected_sms, recorder

FROM = "+18015550100"
PEER = "+18015550123"


def _sms(logger: logging.Logger | None = None, **opts: Any) -> GoToSms:
    return create_goto_sms(
        encryption_key="test-key",
        public_url="https://app.example",
        env={},
        store=MemoryStore(),
        logger=logger or logging.getLogger("aiogotoconnect.sms.test_calls"),
        **opts,
    )


def _call(**over: Any) -> InboundCall:
    fields: dict[str, Any] = {
        "id": "c1",
        "from_": PEER,
        "to": FROM,
        "at": "2026-10-27T10:00:00.000Z",
        "outcome": "MISSED",
        "raw": {},
        "caller_name": "Pat Example",
    }
    return InboundCall(**{**fields, **over})


# --- Task 2: the event type, the options and on("call") ---


async def test_on_call_registers_and_emits_in_order() -> None:
    sms = _sms(watch_calls=True)
    got: list[tuple[str, InboundCall]] = []

    def first(c: InboundCall) -> None:
        got.append(("sync", c))

    async def second(c: InboundCall) -> None:
        got.append(("async", c))

    assert sms.on("call", first).on("call", second) is sms
    call = _call()
    await sms._emit_call(call)
    assert got == [("sync", call), ("async", call)]


async def test_call_handler_exception_does_not_stop_the_next() -> None:
    logger, records = recorder()
    sms = _sms(logger, watch_calls=True)
    got: list[InboundCall] = []

    def boom(c: InboundCall) -> None:
        raise RuntimeError(f"failed for {c.from_} {c.caller_name}")

    sms.on("call", boom).on("call", got.append)
    await sms._emit_call(_call())
    assert len(got) == 1
    loud = [r.getMessage() for r in records if r.levelno >= logging.INFO]
    assert len(loud) == 1
    assert PEER not in loud[0] and "Pat Example" not in loud[0]


async def test_message_handlers_do_not_get_calls() -> None:
    sms = _sms(watch_calls=True)
    got: list[Any] = []
    sms.on("message", got.append)
    await sms._emit_call(_call())
    assert got == []


def test_unknown_event_still_raises() -> None:
    sms = _sms()
    with pytest.raises(ValueError, match=r"'message'.*'call'"):
        sms.on("ring", lambda _c: None)  # type: ignore[call-overload]
    with pytest.raises(ValueError):
        sms.off("ring", lambda _c: None)  # type: ignore[call-overload]


def test_on_call_without_watch_calls_warns_once() -> None:
    logger, records = recorder()
    sms = _sms(logger)
    sms.on("call", lambda _c: None)
    sms.on("call", lambda _c: None)
    warnings = [r.getMessage() for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "watch_calls=True" in warnings[0]


def test_on_call_with_watch_calls_does_not_warn() -> None:
    logger, records = recorder()
    _sms(logger, watch_calls=True).on("call", lambda _c: None)
    assert [r for r in records if r.levelno >= logging.WARNING] == []


async def test_off_call_removes_the_handler() -> None:
    sms = _sms(watch_calls=True)
    got: list[InboundCall] = []
    sms.on("call", got.append)
    sms.off("call", got.append)
    await sms._emit_call(_call())
    assert got == []


def test_call_poll_interval_option_is_resolved_in_seconds() -> None:
    assert _sms()._options.watch_calls is False
    assert _sms()._options.call_poll_interval_s == 120.0
    sms = _sms(watch_calls=True, call_poll_interval=45.0)
    assert sms._options.watch_calls is True
    assert sms._options.call_poll_interval_s == 45.0


# --- Task 3: CallWatcher, one look at the call reports ---

T = "t1"
OTHER_LINE = "+18015550177"
T0 = datetime(2026, 10, 27, 10, 0, tzinfo=UTC).timestamp() * 1000
MIN = 60_000
LOGGER = logging.getLogger("aiogotoconnect.sms.test_calls")
ENV = {"GOTO_CLIENT_ID": "cid", "GOTO_CLIENT_SECRET": "csecret", "GOTO_ACCOUNT_KEY": "acct-1"}


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
    auth: GoToAuth
    logger: logging.Logger
    got: list[InboundCall] = field(default_factory=list)
    watcher: CallWatcher = None  # type: ignore[assignment]
    emit_raises: bool = False

    async def emit(self, call: InboundCall) -> None:
        self.got.append(call)
        if self.emit_raises:
            raise RuntimeError("handler layer failed")

    def new_watcher(self, interval: float = 120.0) -> CallWatcher:
        """A fresh watcher over the same store, like a restarted process."""
        return CallWatcher(
            http=self.http,
            config=self.config,
            store=self.store,
            tenant=T,
            emit=self.emit,
            interval_s=interval,
            now=lambda: self.clock.t,
            logger=self.logger,
        )

    def call(self, id_: str, minutes_ago: float = 0.0, **over: Any) -> dict[str, Any]:
        """Adds a call to the fake: inbound, missed, from PEER to our number, created at
        clock - minutes_ago."""
        c: dict[str, Any] = {
            "id": id_,
            "created": iso_ms(self.clock.t - minutes_ago * MIN),
            "dialled": FROM,
            "caller_number": PEER,
            "caller_name": "Pat Example",
            **over,
        }
        self.fake.calls.append(c)
        return c

    async def started(self) -> "Rig":
        """The first look only sets the cursor; move two minutes on, as the loop would."""
        assert await self.watcher.tick() == 0
        self.clock.t += 2 * MIN
        return self

    async def look(self, minutes_later: float = 2.0) -> int:
        n = await self.watcher.tick()
        self.clock.t += minutes_later * MIN
        return n

    def summaries(self) -> list[dict[str, list[str]]]:
        return [
            parse_qs(urlsplit(h).query)
            for h in self.fake.call_report_hits
            if h.startswith(SUMMARIES)
        ]

    def report_fetches(self) -> int:
        return sum(1 for h in self.fake.call_report_hits if h.startswith(REPORTS))


RigFactory = Callable[..., Awaitable[Rig]]


@pytest.fixture
async def setup() -> AsyncIterator[RigFactory]:
    cleanups: list[Callable[[], Awaitable[None]]] = []

    async def make(
        *, env: dict[str, str] | None = None, logger: logging.Logger | None = None
    ) -> Rig:
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
        await config.save({"fromNumber": FROM})
        url = await auth.start_connect()
        await auth.finish_connect("code-ok", parse_qs(urlsplit(url).query)["state"][0])
        rig = Rig(fake, clock, store, http, config, auth, logger or LOGGER)
        rig.watcher = rig.new_watcher()
        fake.requests.clear()
        return rig

    try:
        yield make
    finally:
        while cleanups:
            await cleanups.pop()()


def ids(rig: Rig) -> list[str]:
    return [c.id for c in rig.got]


async def test_first_tick_imports_nothing(setup: RigFactory) -> None:
    rig = await setup()
    rig.call("old", minutes_ago=1)
    assert await rig.watcher.tick() == 0
    assert rig.got == []
    assert await rig.store.get(T, "calls:cursor") == iso_ms(T0)
    assert await rig.store.get(T, "calls:since") == iso_ms(T0)


async def test_inbound_call_to_the_sending_number_is_delivered_once(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    created = rig.call("c1", minutes_ago=1, ended=iso_ms(rig.clock.t))["created"]
    assert await rig.look() == 1
    call = rig.got[0]
    assert (call.id, call.from_, call.to, call.outcome) == ("c1", PEER, FROM, "MISSED")
    assert call.caller_name == "Pat Example"
    assert call.at == created
    assert call.ended_at is not None
    assert call.raw["conversationSpaceId"] == "c1"
    for _ in range(3):
        assert await rig.look() == 0
    assert ids(rig) == ["c1"]
    # The summary named the dialled number, so the full report was never needed.
    assert rig.report_fetches() == 0


async def test_summaries_are_asked_for_the_account_with_a_15_minute_reach_back(
    setup: RigFactory,
) -> None:
    rig = await (await setup()).started()
    await rig.look()
    q = rig.summaries()[-1]
    assert q["accountKey"] == ["acct-1"]
    assert q["startTime"] == ["2026-10-27T09:45:00Z"]  # cursor (10:00) - 15 min
    assert q["endTime"] == ["2026-10-27T10:02:00Z"]


async def test_call_to_another_number_on_the_account_is_not_delivered(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.call("c1", minutes_ago=1, dialled=OTHER_LINE)
    assert await rig.look() == 0
    assert rig.got == []


async def test_outbound_call_is_not_delivered(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.call("c1", minutes_ago=1, direction="OUTBOUND", summary_has_dialled=False)
    assert await rig.look() == 0
    assert rig.got == []
    assert rig.report_fetches() == 0


async def test_double_ring_in_one_tick_is_one_event(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.call("c1", minutes_ago=1)
    rig.call("c2", minutes_ago=0.9)
    assert await rig.look() == 1
    assert ids(rig) == ["c1"]


async def test_double_ring_across_two_ticks_is_one_event(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.call("c1", minutes_ago=0.5)
    assert await rig.look(minutes_later=0.5) == 1
    rig.call("c2", minutes_ago=0)  # 60 s after c1
    assert await rig.look() == 0
    assert await rig.new_watcher().tick() == 0  # nor after a restart
    assert ids(rig) == ["c1"]


async def test_same_caller_three_minutes_later_is_a_second_event(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.call("c1", minutes_ago=1)
    assert await rig.look() == 1
    rig.call("c2", minutes_ago=0)  # 3 min after c1
    assert await rig.look() == 1
    assert ids(rig) == ["c1", "c2"]


async def test_a_ring_on_another_line_does_not_hide_the_call_to_ours(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.call("other", minutes_ago=1, dialled=OTHER_LINE)
    rig.call("ours", minutes_ago=0.5)
    assert await rig.look() == 1
    assert ids(rig) == ["ours"]


async def test_withheld_number_is_delivered_with_empty_from(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.call("c1", minutes_ago=1, caller_number=None, caller_name=None)
    assert await rig.look() == 1
    assert rig.got[0].from_ == ""
    assert rig.got[0].caller_name is None


async def test_two_withheld_callers_a_minute_apart_both_deliver(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.call("c1", minutes_ago=1.5, caller_number=None)
    rig.call("c2", minutes_ago=0.5, caller_number=None)
    assert await rig.look() == 2
    assert ids(rig) == ["c1", "c2"]


async def test_call_started_before_since_is_not_delivered(setup: RigFactory) -> None:
    rig = await setup()
    assert await rig.watcher.tick() == 0  # since = T0
    rig.fake.calls.append(
        {"id": "early", "created": iso_ms(T0 - 5 * MIN), "dialled": FROM, "caller_number": PEER}
    )
    rig.clock.t += 2 * MIN
    assert await rig.look() == 0
    assert await rig.look() == 0
    assert rig.got == []


async def test_missing_dialled_number_in_the_summary_falls_back_to_the_report(
    setup: RigFactory,
) -> None:
    rig = await (await setup()).started()
    rig.call("c1", minutes_ago=1, summary_has_dialled=False)
    rig.call("c2", minutes_ago=0.5, summary_has_dialled=False, dialled=OTHER_LINE)
    assert await rig.look() == 1
    assert ids(rig) == ["c1"]
    assert rig.got[0].raw["participants"]  # the report, since it had to be read
    assert rig.report_fetches() == 2


async def test_failed_report_fetch_is_retried_next_tick(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.call("c1", minutes_ago=1, summary_has_dialled=False)
    rig.fake.fail_next("GET", REPORTS, 500)
    assert await rig.look() == 0
    assert await rig.look() == 1
    assert ids(rig) == ["c1"]


async def test_caller_name_is_collapsed_and_cut(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.call("c1", minutes_ago=1, caller_name="  Evil \n  Name " + "x" * 60)
    await rig.look()
    name = rig.got[0].caller_name
    assert name is not None
    assert name.startswith("Evil Name x")
    assert len(name) == 40


async def test_unnormalisable_caller_number_is_cut_to_20(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.call("c1", minutes_ago=1, caller_number="anonymous@" + "9" * 40)
    await rig.look()
    assert rig.got[0].from_ == ("anonymous@" + "9" * 40)[:20]


async def test_loosely_formatted_caller_number_is_normalised(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.call("c1", minutes_ago=1, caller_number="(801) 555-0123", dialled="18015550100")
    await rig.look()
    assert (rig.got[0].from_, rig.got[0].to) == (PEER, FROM)


async def test_handler_error_in_emit_does_not_redeliver(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.emit_raises = True
    rig.call("c1", minutes_ago=1)
    rig.call("c2", minutes_ago=0.5, caller_number="+18015550144")
    assert await rig.look() == 2
    assert await rig.look() == 0
    assert ids(rig) == ["c1", "c2"]


async def test_transferred_call_with_two_phone_participants_is_delivered_once(
    setup: RigFactory,
) -> None:
    rig = await (await setup()).started()
    elsewhere = {
        "type": {
            "value": "PHONE_NUMBER",
            "number": OTHER_LINE,
            "caller": {"name": "Someone Else", "number": "+18015550155"},
        }
    }
    rig.call("c1", minutes_ago=1, summary_has_dialled=False, extra_participants=[elsewhere])
    assert await rig.look() == 1
    assert await rig.look() == 0
    assert (rig.got[0].from_, rig.got[0].to) == (PEER, FROM)


async def test_odd_shapes_never_raise_and_cursor_advances(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.fake.call_summaries_raw = [
        None,
        "text",
        {"direction": "INBOUND"},  # no id
        {"conversationSpaceId": "odd-1", "direction": "INBOUND", "caller": "nope"},
        {
            "conversationSpaceId": "odd-2",
            "direction": "INBOUND",
            "callCreated": "nope",
            "caller": {"number": PEER, "type": {"value": "PHONE_NUMBER", "number": FROM}},
        },
    ]
    assert await rig.look() == 1
    assert ids(rig) == ["odd-2"]
    assert rig.got[0].at == iso_ms(T0 + 2 * MIN)  # unparseable start time: now
    assert rig.got[0].outcome == ""
    assert await rig.store.get(T, "calls:cursor") == iso_ms(T0 + 2 * MIN)
    # An answer with no items at all is an empty look, not an error.
    rig.fake.fail_next("GET", SUMMARIES, 200, {})
    assert await rig.look() == 0
    assert await rig.store.get(T, "calls:cursor") == iso_ms(T0 + 4 * MIN)


async def test_many_calls_to_other_lines_do_not_evict_recent_seen(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    for i in range(450):
        rig.call(f"o{i}", minutes_ago=1, dialled=OTHER_LINE, caller_number=None)
    rig.call("ours", minutes_ago=1.5)
    assert await rig.look() == 1
    assert await rig.look() == 0
    assert await rig.look() == 0
    assert ids(rig) == ["ours"]


async def test_old_seen_entries_are_dropped_down_to_the_cap(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    for i in range(450):
        rig.call(f"o{i}", minutes_ago=1, dialled=OTHER_LINE)
    await rig.look()
    seen = await rig.store.get(T, "calls:seen")
    assert isinstance(seen, list) and len(seen) == 450
    rig.clock.t += 30 * MIN  # all of them are now older than the reach-back
    await rig.look()
    seen = await rig.store.get(T, "calls:seen")
    assert isinstance(seen, list) and len(seen) == 400


async def test_every_page_of_summaries_is_read(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.fake.poll_page_size = 2
    for i in range(5):
        rig.call(f"c{i}", minutes_ago=1, caller_number=f"+1801555020{i}")
    before = len(rig.summaries())
    assert await rig.look() == 5
    assert len(rig.summaries()) - before == 3


async def test_changing_the_sending_number_takes_effect_next_tick(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.call("c1", minutes_ago=1)
    assert await rig.look() == 1
    await rig.config.save({"fromNumber": OTHER_LINE})
    rig.call("to-old", minutes_ago=0, caller_number="+18015550144")
    rig.call("to-new", minutes_ago=0, caller_number="+18015550155", dialled=OTHER_LINE)
    assert await rig.look() == 1
    assert ids(rig) == ["c1", "to-new"]
    assert rig.got[1].to == OTHER_LINE


async def test_no_account_key_or_number_makes_no_requests(setup: RigFactory) -> None:
    rig = await setup(env={"GOTO_CLIENT_ID": "cid", "GOTO_CLIENT_SECRET": "csecret"})
    assert await rig.watcher.tick() == 0
    rig.clock.t += 2 * MIN
    assert await rig.watcher.tick() == 0
    assert rig.fake.call_report_hits == []
    assert await rig.store.get(T, "calls:cursor") is None


async def test_reach_back_is_clamped_to_24_hours(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.clock.t += 3 * 24 * 60 * MIN
    await rig.look()
    assert rig.summaries()[-1]["startTime"] == ["2026-10-29T10:02:00Z"]


async def test_caller_details_never_reach_info_logs(setup: RigFactory) -> None:
    logger, records = recorder()
    rig = await (await setup(logger=logger)).started()
    rig.emit_raises = True
    rig.call("c1", minutes_ago=1)
    await rig.look()
    for r in records:
        if r.levelno >= logging.INFO:
            assert PEER not in r.getMessage() and "Pat Example" not in r.getMessage()


# --- Task 4: refusals, back-off and status ---

SCOPE_MESSAGE = (
    "GoTo refused the call reports. Enable the scope cr.v1.read on the OAuth client, "
    "then reconnect."
)


def refuse_summaries(rig: Rig, status: int) -> None:
    """Every summaries request from now on answers `status`, whatever the token."""
    _refuse(rig.fake, status)


def _refuse(fake: FakeGoTo, status: int) -> None:
    fake._routes[:] = [r for r in fake._routes if r.prefix != SUMMARIES]

    def refused(r: Any) -> FakeResponse:
        fake.call_report_hits.append(r.path)
        return FakeResponse(status, {})

    fake.route("GET", SUMMARIES, refused, auth=False)


def token_requests(rig: Rig) -> int:
    return sum(1 for r in rig.fake.requests if r.path.startswith("/oauth/token"))


async def test_first_look_checks_access_but_delivers_nothing(setup: RigFactory) -> None:
    rig = await setup()
    rig.call("old", minutes_ago=1)
    assert await rig.watcher.tick() == 0
    q = rig.summaries()
    assert len(q) == 1
    assert q[0]["startTime"] == q[0]["endTime"] == ["2026-10-27T10:00:00Z"]


async def test_403_sets_calls_scope_and_a_good_look_clears_it(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    cursor = await rig.store.get(T, "calls:cursor")
    rig.call("c1", minutes_ago=1)
    rig.fake.fail_next("GET", SUMMARIES, 403, {"error": "forbidden"})
    assert await rig.look() == 0
    assert await rig.store.get(T, "calls:error") == {
        "error": "calls_scope",
        "message": SCOPE_MESSAGE,
        "gotoStatus": 403,
    }
    assert await rig.store.get(T, "calls:cursor") == cursor
    rig.clock.t += 60 * MIN  # past the back-off
    assert await rig.look() == 1
    assert await rig.store.get(T, "calls:error") is None


async def test_repeated_401_never_marks_login_expired_and_backs_off(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    refuse_summaries(rig, 401)
    tokens_before, looks_before = token_requests(rig), len(rig.summaries())
    for _ in range(5):
        assert await rig.look() == 0
    # One look: the request, one forced refresh, the retry. Then nothing for an hour.
    assert len(rig.summaries()) - looks_before == 2
    assert token_requests(rig) - tokens_before == 1
    assert await rig.store.get(T, "login_expired") is not True
    assert (await rig.auth.status()).connected is True
    error = await rig.store.get(T, "calls:error")
    assert isinstance(error, dict)
    assert (error["error"], error["message"], error["gotoStatus"]) == (
        "calls_scope",
        SCOPE_MESSAGE,
        401,
    )
    rig.clock.t += 60 * MIN
    await rig.look()
    assert len(rig.summaries()) - looks_before == 4


async def test_a_restart_looks_once_while_refused(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    refuse_summaries(rig, 403)
    await rig.look()
    before = len(rig.summaries())
    again = rig.new_watcher()
    await again.tick()
    await again.tick()
    assert len(rig.summaries()) - before == 1


async def test_500_sets_goto_api_and_keeps_the_normal_interval(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    rig.fake.fail_next("GET", SUMMARIES, 500)
    assert await rig.look() == 0
    assert await rig.store.get(T, "calls:error") == {
        "error": "goto_api",
        "message": "GoTo API error 500",
        "gotoStatus": 500,
    }
    rig.call("c1", minutes_ago=1)
    assert await rig.look() == 1  # two minutes later, not an hour
    assert await rig.store.get(T, "calls:error") is None


async def test_network_failure_sets_goto_unreachable(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    await rig.fake.close()
    assert await rig.look() == 0
    assert await rig.store.get(T, "calls:error") == {
        "error": "goto_unreachable",
        "message": "GoTo could not be reached",
    }


async def test_a_really_expired_login_is_not_called_a_scope_problem(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    await rig.store.set(T, "login_expired", True)
    before = len(rig.summaries())
    assert await rig.look() == 0
    assert await rig.look() == 0
    assert len(rig.summaries()) == before  # no request leaves without a login
    error = await rig.store.get(T, "calls:error")
    assert isinstance(error, dict) and error["error"] == "login_expired"


async def test_scope_warning_is_logged_once(setup: RigFactory) -> None:
    logger, records = recorder()
    rig = await (await setup(logger=logger)).started()
    refuse_summaries(rig, 403)
    for _ in range(3):
        await rig.look(minutes_later=61)
    warnings = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "cr.v1.read" in warnings[0]


async def test_texting_still_works_while_calls_are_refused(setup: RigFactory) -> None:
    rig = await (await setup()).started()
    refuse_summaries(rig, 401)
    for _ in range(3):
        await rig.look(minutes_later=61)
    # The same login still reaches the rest of the API.
    await rig.http.json(
        "POST",
        "/messaging/v1/messages",
        {"ownerPhoneNumber": FROM, "contactPhoneNumbers": [PEER], "body": "still here"},
    )
    assert rig.fake.sent[-1]["body"] == "still here"


async def _watching_sms(fake: FakeGoTo, **opts: Any) -> tuple[GoToSms, CallWatcher]:
    """A started client with watch_calls on, and a watcher over its core driven by hand."""
    sms = await connected_sms(fake, watch_calls=True, call_poll_interval=3600.0, **opts)
    core = sms.core
    await core.config.save({"accountKey": "acct-1"})

    async def emit(_c: InboundCall) -> None:
        return None

    watcher = CallWatcher(
        http=core.http,
        config=core.config,
        store=core.store,
        tenant=core.tenant,
        emit=emit,
        interval_s=3600.0,
        now=core.options.now,
        logger=LOGGER,
    )
    return sms, watcher


async def test_status_has_no_calls_key_when_off() -> None:
    fake = await start_fake_goto()
    sms = await connected_sms(fake)
    try:
        assert "calls" not in await sms.status()
        assert "calls" not in await build_status(sms._options, sms.core, include_webhook_url=True)
    finally:
        await sms.close()
        await fake.close()


async def test_status_calls_watching_true_after_a_good_look() -> None:
    fake = await start_fake_goto()
    sms, watcher = await _watching_sms(fake)
    try:
        await sms.core.store.delete(sms.core.tenant, "calls:cursor")
        assert (await sms.status())["calls"] == {"watching": False}
        await watcher.tick()
        assert (await sms.status())["calls"] == {"watching": True}
    finally:
        await sms.close()
        await fake.close()


async def test_status_calls_carries_the_error() -> None:
    fake = await start_fake_goto()
    sms, watcher = await _watching_sms(fake)
    try:
        await watcher.tick()
        _refuse(fake, 403)
        await watcher.tick()
        expected = {
            "watching": False,
            "error": {"error": "calls_scope", "message": SCOPE_MESSAGE, "gotoStatus": 403},
        }
        assert (await sms.status())["calls"] == expected
        via_route = await build_status(sms._options, sms.core, include_webhook_url=True)
        assert via_route["calls"] == expected
    finally:
        await sms.close()
        await fake.close()
