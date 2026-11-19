"""Calls to the sending number as events: on("call"), CallWatcher, status (no Node counterpart yet)."""

import logging
from typing import Any

import pytest

from aiogotoconnect.sms import InboundCall, MemoryStore, create_goto_sms
from aiogotoconnect.sms.client import GoToSms

from .helpers import recorder

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
