"""Port of packages/node/test/secrets.test.ts, plus a Node-sealed interop fixture."""

import base64
import hashlib
import json
import logging
import os
import re
import stat
from pathlib import Path

import pytest

from aiogotoconnect.sms.errors import DecryptError, GoToSmsError, NotConfiguredError
from aiogotoconnect.sms.secrets import SecretBox, resolve_encryption_key

FIXTURE = Path(__file__).parent / "fixtures" / "node_sealed.json"
LOGGER = logging.getLogger("test.secrets")


def sha(s: str) -> bytes:
    return hashlib.sha256(s.encode()).digest()


def test_c22_seal_open_round_trip() -> None:
    box = SecretBox(os.urandom(32))
    sealed = box.seal("hunter2 ✓")
    assert re.fullmatch(r"v1\.[^.]+\.[^.]+\.[^.]*", sealed)
    assert box.open(sealed) == "hunter2 ✓"


def test_c22_opens_node_sealed_value() -> None:
    fx = json.loads(FIXTURE.read_text())
    assert SecretBox(sha(fx["key"])).open(fx["sealed"]) == fx["plain"]


def test_two_seals_of_same_text_differ() -> None:
    box = SecretBox(os.urandom(32))
    assert box.seal("same") != box.seal("same")


def test_wrong_key_length_rejected() -> None:
    with pytest.raises(GoToSmsError) as e:
        SecretBox(b"short")
    assert e.value.code == "bad_key"


def test_tampered_ciphertext_raises_decrypt_error() -> None:
    box = SecretBox(os.urandom(32))
    v, iv, tag, ct = box.seal("secret value").split(".")
    raw = bytearray(base64.b64decode(ct))
    raw[0] ^= 0xFF
    with pytest.raises(DecryptError):
        box.open(".".join([v, iv, tag, base64.b64encode(bytes(raw)).decode()]))


def test_different_key_raises_decrypt_error() -> None:
    sealed = SecretBox(os.urandom(32)).seal("x")
    with pytest.raises(DecryptError):
        SecretBox(os.urandom(32)).open(sealed)


def test_malformed_sealed_strings_raise_decrypt_error() -> None:
    box = SecretBox(os.urandom(32))
    _, iv, tag, ct = box.seal("x").split(".")
    for bad in [
        "",
        "garbage",
        f"v2.{iv}.{tag}.{ct}",
        f"v1.{iv}.{tag}",
        f"v1.{iv}.{tag}.{ct}.extra",
        f"v1.!!!.{tag}.{ct}",
        f"v1.{iv}.AAAA.{ct}",  # wrong-length tag
        f"v1.AAAA.{tag}.{ct}",  # wrong-length iv
        f"v1.{iv}.{tag}.{ct}\n",  # trailing newline must not slip past the validation
        f"v1.{iv}.{tag}.é{ct}",
    ]:
        with pytest.raises(DecryptError):
            box.open(bad)


def test_decrypt_error_code() -> None:
    e = DecryptError()
    assert isinstance(e, GoToSmsError)
    assert e.code == "decrypt_failed"


async def test_option_and_env_keys_are_sha256_option_wins() -> None:
    env = {"GOTO_SMS_ENCRYPTION_KEY": "from-env"}
    key, source = await resolve_encryption_key("from-opt", env, None, LOGGER)
    assert source == "option"
    assert key == sha("from-opt")
    key, source = await resolve_encryption_key(None, env, None, LOGGER)
    assert source == "env"
    assert key == sha("from-env")


async def test_keyfile_created_once_0600_reused_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data_dir = tmp_path / "nested" / "data"  # does not exist yet
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        key, source = await resolve_encryption_key(None, {}, data_dir, LOGGER)
    assert source == "keyfile"
    assert len(key) == 32
    file = data_dir / ".goto-sms.key"
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert "set GOTO_SMS_ENCRYPTION_KEY in production" in caplog.text
    assert key.hex() not in caplog.text
    key2, _ = await resolve_encryption_key(None, {}, data_dir, LOGGER)
    assert key2 == key
    assert file.read_bytes() == key


async def test_concurrent_first_calls_converge(tmp_path: Path) -> None:
    import asyncio

    results = await asyncio.gather(
        *(resolve_encryption_key(None, {}, tmp_path, LOGGER) for _ in range(8))
    )
    assert all(r[0] == results[0][0] for r in results)


async def test_env_key_wins_over_existing_keyfile(tmp_path: Path) -> None:
    await resolve_encryption_key(None, {}, tmp_path, LOGGER)
    _, source = await resolve_encryption_key(
        None, {"GOTO_SMS_ENCRYPTION_KEY": "k"}, tmp_path, LOGGER
    )
    assert source == "env"


async def test_wrong_length_keyfile_is_error_not_regenerated(tmp_path: Path) -> None:
    file = tmp_path / ".goto-sms.key"
    file.write_bytes(bytes(5))
    with pytest.raises(GoToSmsError):
        await resolve_encryption_key(None, {}, tmp_path, LOGGER)
    assert len(file.read_bytes()) == 5


async def test_no_key_and_no_data_dir_is_not_configured() -> None:
    with pytest.raises(NotConfiguredError) as e:
        await resolve_encryption_key(None, {}, None, LOGGER)
    assert str(e.value) == (
        "Set GOTO_SMS_ENCRYPTION_KEY (or give a data_dir/store the package can keep a key file in)"
    )


async def test_damaged_keyfile_error_names_no_path_but_log_does(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    file = tmp_path / ".goto-sms.key"
    file.write_bytes(bytes(5))
    with caplog.at_level(logging.WARNING, logger=LOGGER.name), pytest.raises(GoToSmsError) as e:
        await resolve_encryption_key(None, {}, tmp_path, LOGGER)
    assert e.value.code == "bad_keyfile"
    assert str(tmp_path) not in str(e.value)
    assert re.search(r"encryption key file is damaged", str(e.value), re.IGNORECASE)
    assert str(file) in caplog.text


async def test_placeholder_key_is_not_used(tmp_path: Path) -> None:
    for placeholder in ["CHANGEME", "changeme", "  ", ""]:
        _, source = await resolve_encryption_key(
            placeholder, {"GOTO_SMS_ENCRYPTION_KEY": placeholder}, tmp_path, LOGGER
        )
        assert source == "keyfile"
    with pytest.raises(NotConfiguredError):
        await resolve_encryption_key(None, {"GOTO_SMS_ENCRYPTION_KEY": "CHANGEME"}, None, LOGGER)


async def test_real_key_hashed_untrimmed() -> None:
    key, _ = await resolve_encryption_key(None, {"GOTO_SMS_ENCRYPTION_KEY": " k "}, None, LOGGER)
    assert key == sha(" k ")


async def test_ignored_placeholder_warns_naming_no_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        await resolve_encryption_key(
            None, {"GOTO_SMS_ENCRYPTION_KEY": "CHANGEME"}, tmp_path, LOGGER
        )
    assert "GOTO_SMS_ENCRYPTION_KEY is a placeholder" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        await resolve_encryption_key("  ", {}, tmp_path, LOGGER)
    assert "encryption_key option is a placeholder" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        await resolve_encryption_key(None, {}, tmp_path, LOGGER)
    assert "placeholder" not in caplog.text
