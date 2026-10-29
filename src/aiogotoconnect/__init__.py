import importlib
from typing import Any

from aiogotoconnect.auth import GoToOAuth, TokenProvider
from aiogotoconnect.exceptions import (
    GoToApiError,
    GoToAuthError,
    GoToCallError,
    GoToError,
    GoToSipError,
)
from aiogotoconnect.messaging import GoToMessaging
from aiogotoconnect.sip.call import CallResult, GoToVoice

# SMS names are resolved lazily (PEP 562) so importing the package never loads sms/ eagerly.
_LAZY_SMS = {
    "create_goto_sms": "aiogotoconnect.sms",
    "GoToSms": "aiogotoconnect.sms",
    "normalize_phone": "aiogotoconnect.sms.phone",
    "SendResult": "aiogotoconnect.sms.types",
    "SendStatus": "aiogotoconnect.sms.types",
    "InboundMessage": "aiogotoconnect.sms.types",
    "Keyword": "aiogotoconnect.sms.types",
    "LogEntry": "aiogotoconnect.sms.types",
    "GoToSmsError": "aiogotoconnect.sms.errors",
    "LoginExpiredError": "aiogotoconnect.sms.errors",
    "TemporaryError": "aiogotoconnect.sms.errors",
    "NotConfiguredError": "aiogotoconnect.sms.errors",
    "SendFailedError": "aiogotoconnect.sms.errors",
    "DecryptError": "aiogotoconnect.sms.errors",
    "MemoryStore": "aiogotoconnect.sms.store",
    "FileStore": "aiogotoconnect.sms.store",
    "SqliteStore": "aiogotoconnect.sms.store",
    # Public sms types (none collides with a top-level name; the base-URL constants stay in sms).
    "GoToSmsStore": "aiogotoconnect.sms.store",
    "Json": "aiogotoconnect.sms.store",
    "Authorize": "aiogotoconnect.sms.core",
    "AuthorizeRequest": "aiogotoconnect.sms.core",
    "IncomingMode": "aiogotoconnect.sms.core",
    "StatusPayload": "aiogotoconnect.sms.server.status",
    "IncomingError": "aiogotoconnect.sms.server.status",
    "ConfigField": "aiogotoconnect.sms.config",
    "Source": "aiogotoconnect.sms.config",
    "OptOutSource": "aiogotoconnect.sms.optouts",
    "KeywordReplies": "aiogotoconnect.sms.inbound.keywords",
}


def __getattr__(name: str) -> Any:
    module = _LAZY_SMS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module), name)


__all__ = [
    "CallResult",
    "GoToApiError",
    "GoToAuthError",
    "GoToCallError",
    "GoToError",
    "GoToMessaging",
    "GoToOAuth",
    "GoToSipError",
    "GoToVoice",
    "TokenProvider",
    *_LAZY_SMS,
]
