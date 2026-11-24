"""SMS over GoTo Connect: a Python port of @oneofthegeeks/goto-sms.

Nothing here may import `cryptography` at import time; it is an optional extra.
"""

# client.py imports `cryptography` only when a start runs; create_goto_sms checks for it first.
from aiogotoconnect.sms.client import GoToSms, create_goto_sms
from aiogotoconnect.sms.config import ConfigField, Source
from aiogotoconnect.sms.core import (
    DEFAULT_API_BASE,
    DEFAULT_AUTH_BASE,
    Authorize,
    AuthorizeRequest,
    IncomingMode,
)
from aiogotoconnect.sms.errors import (
    DecryptError,
    GoToApiError,
    GoToSmsError,
    LoginExpiredError,
    NotConfiguredError,
    SendFailedError,
    TemporaryError,
)
from aiogotoconnect.sms.inbound.keywords import KeywordReplies
from aiogotoconnect.sms.optouts import OptOutSource
from aiogotoconnect.sms.phone import normalize_phone
from aiogotoconnect.sms.server.status import IncomingError, StatusPayload
from aiogotoconnect.sms.store import FileStore, GoToSmsStore, Json, MemoryStore, SqliteStore
from aiogotoconnect.sms.types import (
    VERSION,
    InboundMessage,
    Keyword,
    LogEntry,
    SendResult,
    SendStatus,
)

__all__ = [
    "DEFAULT_API_BASE",
    "DEFAULT_AUTH_BASE",
    "VERSION",
    "Authorize",
    "AuthorizeRequest",
    "ConfigField",
    "DecryptError",
    "FileStore",
    "GoToApiError",
    "GoToSms",
    "GoToSmsError",
    "GoToSmsStore",
    "InboundMessage",
    "IncomingError",
    "IncomingMode",
    "Json",
    "Keyword",
    "KeywordReplies",
    "LogEntry",
    "LoginExpiredError",
    "MemoryStore",
    "NotConfiguredError",
    "OptOutSource",
    "SendFailedError",
    "SendResult",
    "SendStatus",
    "Source",
    "SqliteStore",
    "StatusPayload",
    "TemporaryError",
    "create_goto_sms",
    "normalize_phone",
]
