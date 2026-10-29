"""Port of packages/node/test/config.test.ts."""

import os
from collections.abc import Mapping

import pytest

from aiogotoconnect.sms.config import ConfigResolver, config_value
from aiogotoconnect.sms.errors import GoToSmsError
from aiogotoconnect.sms.secrets import SecretBox
from aiogotoconnect.sms.store import MemoryStore

T = "t1"


def make(
    store: MemoryStore | None = None,
    box: SecretBox | None = None,
    options: Mapping[str, str | None] | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[MemoryStore, SecretBox, ConfigResolver]:
    store = store or MemoryStore()
    box = box or SecretBox(os.urandom(32))
    return store, box, ConfigResolver(store, T, box, options or {}, env or {})


def test_config_value() -> None:
    assert config_value(None) is None
    assert config_value("  ") is None
    assert config_value(" ChangeMe ") is None
    assert config_value(" x ") == " x "


async def test_nothing_set_resolves_all_unset() -> None:
    r = await make()[2].resolve()
    assert r.sources == {
        "clientId": "unset",
        "clientSecret": "unset",
        "accountKey": "unset",
        "fromNumber": "unset",
    }
    assert r.credentials_unreadable is False
    assert r.client_id is None


async def test_option_beats_env_beats_store_with_sources() -> None:
    store = MemoryStore()
    await store.set(T, "config:clientId", "from-store")
    await store.set(T, "config:accountKey", "acct-store")
    await store.set(T, "config:fromNumber", "+18015550123")
    _, _, cfg = make(
        store,
        options={"clientId": "from-option"},
        env={"GOTO_CLIENT_ID": "from-env", "GOTO_ACCOUNT_KEY": "acct-env"},
    )
    r = await cfg.resolve()
    assert r.client_id == "from-option"
    assert r.sources["clientId"] == "options"
    assert r.account_key == "acct-env"
    assert r.sources["accountKey"] == "env"
    assert r.from_number == "+18015550123"
    assert r.sources["fromNumber"] == "store"
    assert r.sources["clientSecret"] == "unset"


async def test_empty_option_and_env_count_as_unset() -> None:
    store = MemoryStore()
    await store.set(T, "config:clientId", "from-store")
    _, _, cfg = make(store, options={"clientId": ""}, env={"GOTO_CLIENT_ID": ""})
    r = await cfg.resolve()
    assert r.client_id == "from-store"
    assert r.sources["clientId"] == "store"


async def test_from_number_from_env_is_normalized() -> None:
    r = await make(env={"GOTO_FROM_NUMBER": "(801) 555-0123"})[2].resolve()
    assert r.from_number == "+18015550123"
    assert r.invalid_from_number is False


async def test_invalid_env_from_number_flagged() -> None:
    r = await make(env={"GOTO_FROM_NUMBER": "nope"})[2].resolve()
    assert r.from_number is None
    assert r.sources["fromNumber"] == "env"
    assert r.invalid_from_number is True


async def test_save_stores_plain_fields_and_sealed_secret() -> None:
    store, _, cfg = make()
    await cfg.save(
        {
            "clientId": "cid",
            "clientSecret": "super-secret-value",
            "accountKey": "ak",
            "fromNumber": "801 555 0123",
        }
    )
    assert await store.get(T, "config:clientId") == "cid"
    assert await store.get(T, "config:fromNumber") == "+18015550123"
    raw = await store.get(T, "config:clientSecret")
    assert isinstance(raw, str)
    assert "super-secret-value" not in raw
    r = await cfg.resolve()
    assert r.client_secret == "super-secret-value"
    assert r.sources["clientSecret"] == "store"


async def test_blank_client_secret_save_keeps_stored_value() -> None:
    _, _, cfg = make()
    await cfg.save({"clientSecret": "keepme"})
    await cfg.save({"clientSecret": "", "clientId": "new-id"})
    r = await cfg.resolve()
    assert r.client_secret == "keepme"
    assert r.client_id == "new-id"


async def test_save_of_locked_field_raises_locked_naming_field() -> None:
    _, _, cfg = make(env={"GOTO_CLIENT_ID": "x"}, options={"accountKey": "y"})
    with pytest.raises(GoToSmsError) as e:
        await cfg.save({"clientId": "z"})
    assert e.value.code == "locked"
    assert str(e.value) == "clientId is set by config (env) and can't be changed here"
    with pytest.raises(GoToSmsError) as e2:
        await cfg.save({"accountKey": "z"})
    assert e2.value.code == "locked"
    assert "options" in str(e2.value)


async def test_patch_with_one_locked_field_writes_nothing() -> None:
    store, _, cfg = make(env={"GOTO_CLIENT_ID": "x"})
    with pytest.raises(GoToSmsError) as e:
        await cfg.save({"accountKey": "ak", "clientId": "z"})
    assert e.value.code == "locked"
    assert await store.get(T, "config:accountKey") is None


async def test_blank_value_for_locked_field_is_skipped() -> None:
    store, _, cfg = make(env={"GOTO_CLIENT_ID": "x"})
    await cfg.save({"clientId": "", "accountKey": "ak"})
    assert await store.get(T, "config:accountKey") == "ak"
    assert (await cfg.resolve()).client_id == "x"


async def test_invalid_from_number_raises_and_writes_nothing() -> None:
    store, _, cfg = make()
    with pytest.raises(GoToSmsError) as e:
        await cfg.save({"clientId": "cid", "fromNumber": "abc"})
    assert e.value.code == "invalid_number"
    assert await store.get(T, "config:clientId") is None


async def test_blank_non_secret_value_deletes_stored_field() -> None:
    store, _, cfg = make()
    await cfg.save({"clientId": "cid"})
    await cfg.save({"clientId": ""})
    assert await store.get(T, "config:clientId") is None


async def test_secret_sealed_with_key_a_resolved_with_key_b_is_unreadable() -> None:
    store = MemoryStore()
    await make(store)[2].save({"clientId": "cid", "clientSecret": "sekret"})
    r = await make(store)[2].resolve()
    assert r.credentials_unreadable is True
    assert r.client_secret is None
    assert r.client_id == "cid"


async def test_non_decrypt_errors_from_box_are_rethrown() -> None:
    store = MemoryStore()
    box = SecretBox(os.urandom(32))
    await store.set(T, "config:clientSecret", box.seal("x"))

    class Boom:
        def seal(self, plain: str) -> str:
            return box.seal(plain)

        def open(self, sealed: str) -> str:
            raise TypeError("boom")

    cfg = ConfigResolver(store, T, Boom(), {}, {})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await cfg.resolve()


def test_seed_refresh_token_prefers_option_then_env_ignoring_blanks() -> None:
    assert (
        make(options={"refreshToken": "o"}, env={"GOTO_REFRESH_TOKEN": "e"})[2].seed_refresh_token()
        == "o"
    )
    assert make(env={"GOTO_REFRESH_TOKEN": "e"})[2].seed_refresh_token() == "e"
    assert (
        make(options={"refreshToken": ""}, env={"GOTO_REFRESH_TOKEN": ""})[2].seed_refresh_token()
        is None
    )


async def test_blank_and_changeme_count_as_unset() -> None:
    store = MemoryStore()
    await store.set(T, "config:clientId", "from-store")
    _, _, cfg = make(
        store,
        options={"clientId": "CHANGEME", "accountKey": "   "},
        env={
            "GOTO_CLIENT_ID": " changeme ",
            "GOTO_ACCOUNT_KEY": "\t",
            "GOTO_CLIENT_SECRET": "ChangeMe",
        },
    )
    r = await cfg.resolve()
    assert r.client_id == "from-store"
    assert r.sources["clientId"] == "store"
    assert r.sources["accountKey"] == "unset"
    assert r.sources["clientSecret"] == "unset"
    # A placeholder isn't "set by config", so the setup page can save a real value.
    await cfg.save({"clientId": "real-id"})
    assert (await cfg.resolve()).client_id == "real-id"


async def test_real_values_are_kept_untrimmed() -> None:
    r = await make(env={"GOTO_CLIENT_ID": " id-with-spaces "})[2].resolve()
    assert r.client_id == " id-with-spaces "


def test_seed_refresh_token_ignores_changeme() -> None:
    assert (
        make(options={"refreshToken": "CHANGEME"}, env={"GOTO_REFRESH_TOKEN": "e"})[
            2
        ].seed_refresh_token()
        == "e"
    )
    assert make(env={"GOTO_REFRESH_TOKEN": "changeme"})[2].seed_refresh_token() is None
