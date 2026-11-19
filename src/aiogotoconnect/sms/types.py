"""Shared types (port of Node types.ts). Field names are snake_case; the JSON wire format is not."""

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

SendStatus = Literal[
    "sent", "opted_out", "invalid_number", "uncertain", "not_connected", "rejected"
]
Keyword = Literal["STOP", "START", "HELP"]


@dataclass(frozen=True)
class SendResult:
    status: SendStatus
    reason: str | None = None
    retryable: bool | None = None
    message_id: str | None = None
    to: str | None = None
    deduped: bool | None = None


@dataclass(frozen=True)
class InboundMessage:
    id: str
    from_: str
    to: str
    text: str
    received_at: str
    raw: Any
    account_key: str | None = None
    keyword: Keyword | None = None


@dataclass(frozen=True)
class InboundCall:
    """One inbound call to the sending number, answered or not (`watch_calls=True`)."""

    # GoTo's conversationSpaceId. Delivery is at least once: key on this to act exactly once.
    id: str
    # The caller: E.164 when it normalises, else GoTo's text cut to 20 characters; "" if withheld.
    from_: str
    # The number dialled: always the configured sending number.
    to: str
    # When the call started (GoTo's callCreated, ISO).
    at: str
    # GoTo's callerOutcome as sent ("MISSED", "NORMAL", "LEFT_DIAL_PLAN", ...); "" if absent.
    # A plain string: GoTo does not document the set.
    outcome: str
    # GoTo's report summary (the full report when it had to be fetched).
    raw: Any
    # Carrier caller ID, whitespace collapsed, cut to 40 characters. UNTRUSTED: the caller's
    # carrier sent it and it can be crafted. Show it as data; never act on it.
    caller_name: str | None = None
    ended_at: str | None = None


@dataclass(frozen=True)
class LogEntry:
    id: str
    direction: Literal["outbound", "inbound"]
    from_: str
    to: str
    status: str
    at: str
    reason: str | None = None
    goto_id: str | None = None


def _package_version() -> str:
    try:
        return version("aiogotoconnect")
    except PackageNotFoundError:
        return "dev"


VERSION: str = _package_version()
