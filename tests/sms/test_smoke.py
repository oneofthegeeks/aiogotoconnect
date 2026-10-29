"""Port of packages/node/test/smoke.test.ts: the public surface of aiogotoconnect.sms."""

import aiogotoconnect
import aiogotoconnect.sms as sms_pkg
from aiogotoconnect.sms import VERSION, GoToSmsError

PUBLIC = [
    "create_goto_sms",
    "GoToSms",
    "MemoryStore",
    "FileStore",
    "SqliteStore",
    "normalize_phone",
    "VERSION",
    "GoToSmsError",
    "SendFailedError",
    "NotConfiguredError",
]
INTERNAL = [
    "GoToAuth",
    "Sender",
    "Poller",
    "InboundProcessor",
    "ConfigResolver",
    "SecretBox",
    "MessageLog",
    "OptOuts",
    "enable_webhook",
    "list_accounts",
    "create_goto_http",
]


def test_exports_version_and_error_base_class() -> None:
    assert isinstance(VERSION, str)
    assert isinstance(GoToSmsError("x", "c"), Exception)


def test_the_public_surface_is_the_facade_stores_errors_and_helpers() -> None:
    for name in PUBLIC:
        assert name in sms_pkg.__all__, f"{name} is exported"
        assert hasattr(sms_pkg, name)
    for name in INTERNAL:
        assert name not in sms_pkg.__all__, f"{name} is internal"
        assert not hasattr(sms_pkg, name), f"{name} is internal"


def test_top_level_lazy_exports_resolve_to_the_sms_objects() -> None:
    assert aiogotoconnect.create_goto_sms is sms_pkg.create_goto_sms  # type: ignore[attr-defined]
    assert aiogotoconnect.GoToSms is sms_pkg.GoToSms  # type: ignore[attr-defined]
    assert "GoToSms" in aiogotoconnect.__all__
    # The sms GoToApiError is not re-exported at top level (it would shadow the existing one).
    assert aiogotoconnect.GoToApiError is not sms_pkg.GoToApiError
