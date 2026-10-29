"""Credential resolution (port of Node config.ts): options > env > store; only the store is writable."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from aiogotoconnect.sms.errors import DecryptError, GoToSmsError
from aiogotoconnect.sms.phone import normalize_phone
from aiogotoconnect.sms.store.types import GoToSmsStore

if TYPE_CHECKING:  # keeps `cryptography` (the optional [sms] extra) out of this module's imports
    from aiogotoconnect.sms.secrets import SecretBox

# Wire names (camelCase, as in Node), so they match the JSON the setup page speaks.
ConfigField = Literal["clientId", "clientSecret", "accountKey", "fromNumber"]
Source = Literal["options", "env", "store", "unset"]

FIELDS: tuple[ConfigField, ...] = ("clientId", "clientSecret", "accountKey", "fromNumber")
ENV_NAMES: dict[ConfigField, str] = {
    "clientId": "GOTO_CLIENT_ID",
    "clientSecret": "GOTO_CLIENT_SECRET",
    "accountKey": "GOTO_ACCOUNT_KEY",
    "fromNumber": "GOTO_FROM_NUMBER",
}


def _store_key(name: ConfigField) -> str:
    return f"config:{name}"


def config_value(v: str | None) -> str | None:
    """An option or env value, or None when effectively unset: blank (Docker .env files commonly
    contain `GOTO_CLIENT_ID=`) or the literal `CHANGEME` placeholder secret managers use for keys
    not filled in yet. Real values are returned untrimmed: the encryption key is hashed as given,
    so trimming it would change the key."""
    if v is None:
        return None
    t = v.strip()
    return None if t == "" or t.lower() == "changeme" else v


@dataclass
class ResolvedConfig:
    client_id: str | None = None
    client_secret: str | None = None
    account_key: str | None = None
    from_number: str | None = None
    sources: dict[ConfigField, Source] = field(default_factory=dict)
    # The stored client secret exists but can't be decrypted (wrong/changed encryption key).
    credentials_unreadable: bool = False
    # An options/env from_number was set but isn't a valid phone number (value is then None).
    invalid_from_number: bool = False


class ConfigResolver:
    def __init__(
        self,
        store: GoToSmsStore,
        tenant: str,
        box: "SecretBox",
        options: Mapping[str, str | None],
        env: Mapping[str, str],
    ) -> None:
        self._store = store
        self._tenant = tenant
        self._box = box
        # Keyed by wire name; "refreshToken" is also read, for seed_refresh_token.
        self._options = options
        self._env = env

    def _fixed_value(self, name: ConfigField) -> tuple[Literal["options", "env"], str] | None:
        """Which non-store layer (if any) supplies `name`, with its raw value."""
        from_option = config_value(self._options.get(name))
        if from_option:
            return "options", from_option
        from_env = config_value(self._env.get(ENV_NAMES[name]))
        if from_env:
            return "env", from_env
        return None

    async def _stored_string(self, name: ConfigField) -> str | None:
        v = await self._store.get(self._tenant, _store_key(name))
        return v if isinstance(v, str) and v != "" else None

    async def resolve(self) -> ResolvedConfig:
        values: dict[ConfigField, str] = {}
        sources: dict[ConfigField, Source] = {}
        unreadable = False
        invalid_from = False

        for name in FIELDS:
            fixed = self._fixed_value(name)
            value: str | None
            if fixed:
                sources[name] = fixed[0]
                value = fixed[1]
            else:
                value = await self._stored_string(name)
                sources[name] = "unset" if value is None else "store"

            if value is not None and name == "fromNumber" and fixed:
                # Stored numbers were normalized on save; config-supplied ones need it now.
                normalized = normalize_phone(value)
                if normalized is None:
                    invalid_from = True
                value = normalized
            elif value is not None and name == "clientSecret" and not fixed:
                try:
                    value = self._box.open(value)
                except DecryptError:
                    unreadable = True
                    value = None
            if value is not None:
                values[name] = value

        return ResolvedConfig(
            client_id=values.get("clientId"),
            client_secret=values.get("clientSecret"),
            account_key=values.get("accountKey"),
            from_number=values.get("fromNumber"),
            sources=sources,
            credentials_unreadable=unreadable,
            invalid_from_number=invalid_from,
        )

    async def save(self, patch: Mapping[ConfigField, str]) -> None:
        """Persist `patch` to the store. The whole patch is validated before anything is written.
        An empty clientSecret means "keep the stored one" (the UI never echoes it back), and an
        empty value for a field set by options/env is skipped; any other value for one is `locked`."""
        writes: list[tuple[ConfigField, str]] = []
        for name in FIELDS:
            raw = patch.get(name)
            if raw is None:
                continue
            if name == "clientSecret" and raw == "":
                continue

            fixed = self._fixed_value(name)
            # A blank for a config-set field is "not editable here", not an attempt to change it:
            # the setup page leaves locked inputs empty.
            if fixed and raw == "":
                continue
            if fixed:
                raise GoToSmsError(
                    f"{name} is set by config ({fixed[0]}) and can't be changed here", "locked"
                )

            value = raw
            if name == "fromNumber" and raw != "":
                normalized = normalize_phone(raw)
                if normalized is None:
                    raise GoToSmsError(f'"{raw}" is not a valid phone number', "invalid_number")
                value = normalized
            writes.append((name, value))

        for name, value in writes:
            if value == "":
                await self._store.delete(self._tenant, _store_key(name))
            else:
                stored = self._box.seal(value) if name == "clientSecret" else value
                await self._store.set(self._tenant, _store_key(name), stored)

    def seed_refresh_token(self) -> str | None:
        return config_value(self._options.get("refreshToken")) or config_value(
            self._env.get("GOTO_REFRESH_TOKEN")
        )
