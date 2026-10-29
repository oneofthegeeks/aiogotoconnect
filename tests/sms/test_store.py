"""Port of packages/node/test/store.test.ts (store conformance + FileStore specifics)."""

import asyncio
import logging
import os
import sqlite3
from pathlib import Path

import pytest

from aiogotoconnect.sms import FileStore, MemoryStore, SqliteStore
from aiogotoconnect.sms.store.types import GoToSmsStore, Json, json_equal, next_claim
from aiogotoconnect.sms.testing import run_store_conformance


async def test_memory_store_conformance() -> None:
    async def make() -> GoToSmsStore:
        return MemoryStore()

    await run_store_conformance(make)


async def test_file_store_conformance(tmp_path: Path) -> None:
    count = 0

    async def make() -> GoToSmsStore:
        nonlocal count
        count += 1
        return FileStore(tmp_path / f"s{count}" / "goto-sms.json")

    await run_store_conformance(make)


async def test_sqlite_store_conformance(tmp_path: Path) -> None:
    stores: list[SqliteStore] = []

    async def make() -> GoToSmsStore:
        store = SqliteStore(tmp_path / f"s{len(stores)}.db")
        stores.append(store)
        return store

    await run_store_conformance(make)
    for store in stores:
        store.close()


async def test_sqlite_store_conformance_on_caller_connection() -> None:
    async def make() -> GoToSmsStore:
        return SqliteStore(sqlite3.connect(":memory:"))  # legacy implicit-transaction mode

    await run_store_conformance(make)


async def test_sqlite_store_uses_wal_and_node_schema(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "x.db")
    await store.set("t", "k", {"v": 1})
    store.close()
    raw = sqlite3.connect(tmp_path / "x.db")
    assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert raw.execute("SELECT tenant, key, value FROM goto_sms_kv").fetchall() == [
        ("t", "k", '{"v": 1}')
    ]
    raw.close()


async def test_sqlite_store_rejects_bad_table_name() -> None:
    with pytest.raises(ValueError, match="invalid table name"):
        SqliteStore(sqlite3.connect(":memory:"), table="x; DROP TABLE y")


async def test_sqlite_store_custom_table_and_two_processes_view(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    a, b = SqliteStore(path, table="other"), SqliteStore(path, table="other")
    assert await a.compare_and_set("t", "k", None, 1) is True
    assert await b.compare_and_set("t", "k", None, 2) is False
    a.close()
    b.close()


async def test_memory_store_values_are_copied() -> None:
    store = MemoryStore()
    value: Json = {"a": [1]}
    await store.set("t", "k", value)
    assert isinstance(value, dict)
    value["a"] = []
    got = await store.get("t", "k")
    assert got == {"a": [1]}
    assert isinstance(got, dict)
    got["a"] = "mutated"
    assert await store.get("t", "k") == {"a": [1]}


def test_json_equal_distinguishes_bool_from_number() -> None:
    assert json_equal({"a": 1, "b": [True]}, {"b": [True], "a": 1})
    assert not json_equal(True, 1)
    assert not json_equal([0], [False])
    assert not json_equal(None, 0)
    assert not json_equal({"a": 1}, {"a": 1, "b": 2})
    assert not json_equal([1], {"0": 1})


def test_next_claim_treats_non_claim_values_as_free() -> None:
    assert next_claim("junk", "a", 10, 100) == {"owner": "a", "expiresAt": 110}
    assert next_claim({"owner": "b", "expiresAt": True}, "a", 10, 100) == {
        "owner": "a",
        "expiresAt": 110,
    }
    assert next_claim({"owner": "b", "expiresAt": 101}, "a", 10, 100) is None
    assert next_claim({"owner": "b", "expiresAt": 100}, "a", 10, 100) is not None


async def test_file_store_new_instance_sees_earlier_writes(tmp_path: Path) -> None:
    path = tmp_path / "goto-sms.json"
    await FileStore(path).set("t", "k", {"v": 1})
    assert await FileStore(path).get("t", "k") == {"v": 1}


async def test_file_store_atomic_write_leaves_no_tmp_file(tmp_path: Path) -> None:
    store = FileStore(tmp_path / "goto-sms.json")
    await store.set("t", "a", 1)
    await store.set("t", "b", 2)
    await asyncio.gather(store.set("t", "c", 3), store.delete("t", "a"))
    assert os.listdir(tmp_path) == ["goto-sms.json"]
    assert await FileStore(tmp_path / "goto-sms.json").list("t", "") == [("b", 2), ("c", 3)]


async def test_file_store_ignores_symlink_planted_at_legacy_tmp_path(tmp_path: Path) -> None:
    path = tmp_path / "goto-sms.json"
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    os.symlink(victim, f"{path}.tmp")
    store = FileStore(path)
    await store.set("t", "k", 1)
    await store.set("t", "k", 2)
    assert victim.read_text() == "untouched"
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == ["goto-sms.json.tmp"]
    assert await FileStore(path).get("t", "k") == 2


async def test_file_store_file_is_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "goto-sms.json"
    await FileStore(path).set("t", "k", 1)
    assert path.stat().st_mode & 0o777 == 0o600


async def test_file_store_corrupt_file_is_moved_aside_and_warned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "goto-sms.json"
    path.write_text("{not json")
    logger = logging.getLogger("test.file_store")
    with caplog.at_level(logging.WARNING, logger="test.file_store"):
        store = FileStore(path, now=lambda: 12345, logger=logger)
    assert await store.list("t", "") == []
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert sorted(os.listdir(tmp_path)) == ["goto-sms.json.corrupt-12345"]
    assert (tmp_path / "goto-sms.json.corrupt-12345").read_text() == "{not json"
    await store.set("t", "k", 1)  # usable afterwards
    assert await FileStore(path).get("t", "k") == 1


async def test_file_store_wrong_shape_is_treated_as_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "goto-sms.json"
    path.write_text("[1, 2]")
    store = FileStore(path, now=lambda: 7)  # no logger: silent
    assert await store.list("t", "") == []
    assert (tmp_path / "goto-sms.json.corrupt-7").exists()
