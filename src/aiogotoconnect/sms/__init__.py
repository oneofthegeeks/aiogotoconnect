"""SMS over GoTo Connect: a Python port of @oneofthegeeks/goto-sms.

Nothing here may import `cryptography` at import time; it is an optional extra.
"""

# client.py imports `cryptography` only when a start runs; create_goto_sms checks for it first.
from aiogotoconnect.sms.client import GoToSms, create_goto_sms
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

__all__ = [
    "VERSION",
    "DecryptError",
    "FileStore",
    "GoToApiError",
    "GoToSms",
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
