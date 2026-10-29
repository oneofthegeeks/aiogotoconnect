"""AES-256-GCM secret box and encryption-key resolution (port of Node secrets.ts).

Sealed format is byte-compatible with Node: "v1.<iv>.<tag>.<ciphertext>", base64 parts.
Imports `cryptography` (the optional [sms] extra), so only import this module when it's needed.
"""

import asyncio
import base64
import binascii
import hashlib
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from aiogotoconnect.sms.config import config_value
from aiogotoconnect.sms.errors import DecryptError, GoToSmsError, NotConfiguredError

__all__ = ["DecryptError", "SecretBox", "resolve_encryption_key"]

_VERSION = "v1"
_IV_BYTES = 12  # standard GCM nonce size
_TAG_BYTES = 16
_KEY_BYTES = 32
# fullmatch (not `$`) so a trailing newline is rejected; [A-Za-z0-9+/] is plain ASCII base64.
_B64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")


def _b64decode(part: str) -> bytes:
    # Node's decoder accepts unpadded input; Python's needs padding, so add it.
    try:
        return base64.b64decode(
            part.rstrip("=") + "=" * (-len(part.rstrip("=")) % 4), validate=True
        )
    except binascii.Error:
        raise DecryptError() from None


class SecretBox:
    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_BYTES:
            raise GoToSmsError("Encryption key must be 32 bytes", "bad_key")
        self._aes = AESGCM(key)

    def seal(self, plain: str) -> str:
        iv = os.urandom(_IV_BYTES)
        out = self._aes.encrypt(iv, plain.encode("utf-8"), None)  # ciphertext || 16-byte tag
        ct, tag = out[:-_TAG_BYTES], out[-_TAG_BYTES:]
        return ".".join([_VERSION, *(base64.b64encode(p).decode("ascii") for p in (iv, tag, ct))])

    def open(self, sealed: str) -> str:
        """Raises DecryptError for any malformed input or auth failure; never a raw crypto error."""
        parts = sealed.split(".")
        if len(parts) != 4 or parts[0] != _VERSION:
            raise DecryptError()
        if not all(_B64.fullmatch(p) for p in parts[1:]):
            raise DecryptError()
        iv, tag, ct = (_b64decode(p) for p in parts[1:])
        if len(iv) != _IV_BYTES or len(tag) != _TAG_BYTES:
            raise DecryptError()
        try:
            return self._aes.decrypt(iv, ct + tag, None).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError):
            raise DecryptError() from None


def _sha256(s: str) -> bytes:
    return hashlib.sha256(s.encode("utf-8")).digest()


async def resolve_encryption_key(
    option_key: str | None,
    env: Mapping[str, str],
    data_dir: str | Path | None,
    logger: logging.Logger,
) -> tuple[bytes, Literal["option", "env", "keyfile"]]:
    # 0.1.x of the Node package hashed a CHANGEME or blank key as a real key; say so when one is
    # now ignored, since secrets saved under it become unreadable. The value is never logged.
    def placeholder(raw: str | None, name: str) -> None:
        if raw is not None and raw != "" and config_value(raw) is None:
            logger.warning(
                "%s is a placeholder (blank or CHANGEME) and is ignored; secrets saved under it "
                "in goto-sms 0.1.x must be re-entered",
                name,
            )

    env_raw = env.get("GOTO_SMS_ENCRYPTION_KEY")
    placeholder(option_key, "The encryption_key option")
    placeholder(env_raw, "GOTO_SMS_ENCRYPTION_KEY")
    if opt := config_value(option_key):
        return _sha256(opt), "option"
    if env_key := config_value(env_raw):
        return _sha256(env_key), "env"
    if not data_dir:
        raise NotConfiguredError(
            "Set GOTO_SMS_ENCRYPTION_KEY (or give a data_dir/store the package can keep a key "
            "file in)"
        )

    directory = Path(data_dir)
    file = directory / ".goto-sms.key"
    directory.mkdir(parents=True, exist_ok=True)
    fresh = os.urandom(_KEY_BYTES)
    try:
        # O_EXCL fails if the file exists, so concurrent callers never overwrite each other's key.
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as f:
            f.write(fresh)
        logger.warning(
            "Created encryption keyfile %s; set GOTO_SMS_ENCRYPTION_KEY in production", file
        )
        return fresh, "keyfile"

    # Another caller created it. O_EXCL creates the file before the bytes land, so a concurrent
    # reader can briefly see it empty; retry a few times before giving up.
    key = file.read_bytes()
    for _ in range(20):
        if key:
            break
        await asyncio.sleep(0.01)
        key = file.read_bytes()
    if len(key) != _KEY_BYTES:
        # The message reaches the setup page, so the path goes to the log only.
        logger.warning("Encryption keyfile %s is %d bytes, expected %d", file, len(key), _KEY_BYTES)
        raise GoToSmsError(
            "The encryption key file is damaged, so saved secrets can't be read. Set "
            "GOTO_SMS_ENCRYPTION_KEY, or delete the key file only if no saved secrets need it "
            "(the server log names the file)",
            "bad_keyfile",
        )
    return key, "keyfile"
