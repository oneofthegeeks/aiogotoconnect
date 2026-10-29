import sys

import pytest


def test_import_without_cryptography(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate the [sms] extra being absent: block the module, re-import the package fresh.
    monkeypatch.setitem(sys.modules, "cryptography", None)
    for name in [m for m in sys.modules if m.startswith("aiogotoconnect")]:
        monkeypatch.delitem(sys.modules, name)
    import aiogotoconnect

    assert hasattr(aiogotoconnect, "GoToOAuth")
    with pytest.raises(ImportError, match=r"pip install aiogotoconnect\[sms\]"):
        aiogotoconnect.create_goto_sms()  # type: ignore[attr-defined]
