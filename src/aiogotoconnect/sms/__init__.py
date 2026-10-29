"""SMS over GoTo Connect: a Python port of @oneofthegeeks/goto-sms.

Nothing here may import `cryptography` at import time; it is an optional extra.
"""

from typing import Any

from aiogotoconnect.sms.errors import (
    DecryptError,
    GoToApiError,
    GoToSmsError,
    LoginExpiredError,
    NotConfiguredError,
    SendFailedError,
    TemporaryError,
)
from aiogotoconnect.sms.phone import normalize_phone
from aiogotoconnect.sms.store import FileStore, MemoryStore, SqliteStore
from aiogotoconnect.sms.types import (
    VERSION,
    InboundMessage,
    Keyword,
    LogEntry,
    SendResult,
    SendStatus,
)


def create_goto_sms(*args: Any, **kwargs: Any) -> Any:
    """Placeholder until Task 11 wires the real factory."""
    try:
        import cryptography  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "aiogotoconnect SMS needs the optional 'cryptography' package: "
            "pip install aiogotoconnect[sms]"
        ) from exc
    raise NotImplementedError("create_goto_sms is not implemented yet")


__all__ = [
    "VERSION",
    "DecryptError",
    "FileStore",
    "GoToApiError",
    "GoToSmsError",
    "InboundMessage",
    "Keyword",
    "LogEntry",
    "LoginExpiredError",
    "MemoryStore",
    "NotConfiguredError",
    "SendFailedError",
    "SendResult",
    "SendStatus",
    "SqliteStore",
    "TemporaryError",
    "create_goto_sms",
    "normalize_phone",
]
