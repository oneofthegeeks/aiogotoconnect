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
