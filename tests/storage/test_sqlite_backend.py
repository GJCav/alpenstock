from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from alpenstock.storage import Repo, TransactionStateError, ValueContractError, WriteConflictError
from alpenstock.storage._handles import BinaryFileHandle, TextFileHandle
from alpenstock.storage._types import Put
from alpenstock.storage.backends.sqlite import SqliteBackend, SqliteConfig
from alpenstock.storage.backends.sqlite._support import connect, ensure_schema
from alpenstock.storage.backends.sqlite import backend as sqlite_backend_module
from alpenstock.storage.backends.sqlite.backend import SqliteStagedValueRef


def _connect(db_path: Path, *, config: SqliteConfig | None = None) -> sqlite3.Connection:
    return connect(db_path, config)


def _insert_object(db_path: Path, key: str, value: bytes, *, repo_path: str = "", config: SqliteConfig | None = None) -> None:
    names = (SqliteConfig() if config is None else config).schema_names
    connection = _connect(db_path, config=config)
    try:
        ensure_schema(connection, names)
        connection.execute(
            f'INSERT INTO "{names.objects}"(repo_path, key, value) VALUES (?, ?, ?)',
            (repo_path, key, value),
        )
        connection.commit()
    finally:
        connection.close()


def _fetch_objects(db_path: Path, *, repo_path: str = "", config: SqliteConfig | None = None) -> dict[str, bytes]:
    if not db_path.exists():
        return {}
    names = (SqliteConfig() if config is None else config).schema_names
    connection = _connect(db_path, config=config)
    try:
        ensure_schema(connection, names)
        return {
            str(row[0]): bytes(row[1])
            for row in connection.execute(
                f'SELECT key, value FROM "{names.objects}" WHERE repo_path = ? ORDER BY key',
                (repo_path,),
            )
        }
    finally:
        connection.close()


def _fetch_tx_meta(db_path: Path, *, config: SqliteConfig | None = None) -> list[tuple[str, str, str]]:
    if not db_path.exists():
        return []
    names = (SqliteConfig() if config is None else config).schema_names
    connection = _connect(db_path, config=config)
    try:
        ensure_schema(connection, names)
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                f'SELECT tx_id, repo_path, state FROM "{names.tx_meta}" ORDER BY repo_path, tx_id'
            )
        ]
    finally:
        connection.close()


def _fetch_tx_values(db_path: Path, *, config: SqliteConfig | None = None) -> list[tuple[str, str, str, int]]:
    if not db_path.exists():
        return []
    names = (SqliteConfig() if config is None else config).schema_names
    connection = _connect(db_path, config=config)
    try:
        ensure_schema(connection, names)
        return [
            (str(row[0]), str(row[1]), str(row[2]), int(row[3]))
            for row in connection.execute(
                f'SELECT tx_id, repo_path, key, is_delete FROM "{names.tx_values}" ORDER BY tx_id, repo_path, key'
            )
        ]
    finally:
        connection.close()


def _fetch_tx_value_rows(db_path: Path, *, config: SqliteConfig | None = None) -> list[tuple[int, str, str, str, int]]:
    if not db_path.exists():
        return []
    names = (SqliteConfig() if config is None else config).schema_names
    connection = _connect(db_path, config=config)
    try:
        ensure_schema(connection, names)
        return [
            (int(row[0]), str(row[1]), str(row[2]), str(row[3]), int(row[4]))
            for row in connection.execute(
                f'SELECT id, tx_id, repo_path, key, is_delete FROM "{names.tx_values}" ORDER BY id'
            )
        ]
    finally:
        connection.close()


def _write_bytes(tx, key: str, payload: bytes) -> None:
    with cast(BinaryFileHandle, tx.open_handle(key, "wb")) as handle:
        handle.write(payload)


def _simulate_sqlite_process_exit(tx) -> None:
    tx.connection.close()
    tx.lock.release()
    tx._released = True


def test_read_only_access_does_not_start_pending_transaction_state(tmp_path: Path) -> None:
    backend = SqliteBackend()
    db_path = tmp_path / "repo.db"
    _insert_object(db_path, "alpha", b"hello")

    repo = Repo.open(str(db_path), backend=backend)

    assert repo.file("alpha").read_text() == "hello"
    assert _fetch_tx_meta(db_path) == []


def test_committed_handles_reject_write_modes(tmp_path: Path) -> None:
    backend = SqliteBackend()
    db_path = tmp_path / "repo.db"

    with pytest.raises(TransactionStateError, match="Committed views are read-only"):
        backend.open_committed_handle(str(db_path), "alpha", "w")


def test_second_writer_is_rejected_while_first_transaction_holds_lock(tmp_path: Path) -> None:
    backend = SqliteBackend()
    db_path = tmp_path / "repo.db"
    first = backend.begin(str(db_path))

    with pytest.raises(WriteConflictError, match="already owns repo lock"):
        backend.begin(str(db_path))

    first.rollback()


def test_begin_rejects_pending_unrecovered_transaction_state(tmp_path: Path) -> None:
    backend = SqliteBackend()
    db_path = tmp_path / "repo.db"
    connection = _connect(db_path)
    try:
        names = backend.config.schema_names
        ensure_schema(connection, names)
        connection.execute(
            f'INSERT INTO "{names.tx_meta}"(tx_id, repo_path, state) VALUES (\'stale\', \'\', \'open\')'
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(TransactionStateError, match="pending recovery state exists; run recover\\(\\) first"):
        backend.begin(str(db_path))


def test_empty_prepare_leaves_no_pending_transaction_state(tmp_path: Path) -> None:
    backend = SqliteBackend()
    db_path = tmp_path / "repo.db"

    tx = backend.begin(str(db_path))
    tx.prepare()
    tx.commit()

    assert _fetch_tx_meta(db_path) == []


def test_delete_rejects_invalid_logical_key_before_creating_metadata(tmp_path: Path) -> None:
    backend = SqliteBackend()
    db_path = tmp_path / "repo.db"
    tx = backend.begin(str(db_path))

    with pytest.raises(ValueContractError, match="must not contain|relative path"):
        tx.delete("../bad")

    assert _fetch_tx_meta(db_path) == []
    tx.rollback()


def test_reader_opened_while_writer_is_open_sees_live_working_copy(tmp_path: Path) -> None:
    backend = SqliteBackend()
    db_path = tmp_path / "repo.db"
    _insert_object(db_path, "alpha", b"old")

    tx = backend.begin(str(db_path))
    writer = cast(TextFileHandle, tx.open_handle("alpha", "w"))
    writer.write("new")

    with tx.open_handle("alpha", "r") as reader:
        assert reader.read() == "new"

    writer.close()
    tx.rollback()


def test_reopen_same_key_starts_from_current_transaction_visible_value(tmp_path: Path) -> None:
    backend = SqliteBackend()
    db_path = tmp_path / "repo.db"

    tx = backend.begin(str(db_path))
    first = cast(TextFileHandle, tx.open_handle("alpha", "w"))
    first.write("one")
    first.close()

    second = cast(TextFileHandle, tx.open_handle("alpha", "a"))
    second.write(" two")
    second.close()

    with tx.open_handle("alpha", "r") as reader:
        assert reader.read() == "one two"

    tx.rollback()


def test_resealing_same_key_creates_new_immutable_stage_ref_and_prunes_superseded_row(tmp_path: Path) -> None:
    backend = SqliteBackend()
    db_path = tmp_path / "repo.db"

    tx = backend.begin(str(db_path))
    first = cast(TextFileHandle, tx.open_handle("alpha", "w"))
    first.write("one")
    first.close()

    first_entry = tx.core.overlay["alpha"]
    assert isinstance(first_entry, Put)
    assert isinstance(first_entry.value, SqliteStagedValueRef)
    first_stage_id = first_entry.value.stage_id
    assert [row[0] for row in _fetch_tx_value_rows(db_path)] == [first_stage_id]

    second = cast(TextFileHandle, tx.open_handle("alpha", "a"))
    second.write(" two")
    second.close()

    second_entry = tx.core.overlay["alpha"]
    assert isinstance(second_entry, Put)
    assert isinstance(second_entry.value, SqliteStagedValueRef)
    second_stage_id = second_entry.value.stage_id
    assert second_stage_id != first_stage_id
    assert [(row[0], row[2], row[3], row[4]) for row in _fetch_tx_value_rows(db_path)] == [
        (second_stage_id, "", "alpha", 0)
    ]

    tx.rollback()


def test_prepare_persists_staged_rows_and_prepared_state(tmp_path: Path) -> None:
    backend = SqliteBackend()
    db_path = tmp_path / "repo.db"

    tx = backend.begin(str(db_path))
    _write_bytes(tx, "alpha", b"one")
    tx.delete("beta")
    tx.prepare()

    meta = _fetch_tx_meta(db_path)
    values = _fetch_tx_values(db_path)

    assert len(meta) == 1
    assert meta[0][1:] == ("", "prepared")
    assert len(values) == 2
    assert {(row[1], row[2]): row[3] for row in values} == {("", "alpha"): 0, ("", "beta"): 1}

    tx.rollback()


def test_commit_failure_from_missing_staged_row_can_still_be_rolled_back(tmp_path: Path) -> None:
    backend = SqliteBackend()
    db_path = tmp_path / "repo.db"
    _insert_object(db_path, "beta", b"old")

    tx = backend.begin(str(db_path))
    _write_bytes(tx, "alpha", b"one")
    tx.delete("beta")
    tx.prepare()

    connection = _connect(db_path)
    try:
        names = backend.config.schema_names
        connection.execute(f'DELETE FROM "{names.tx_values}" WHERE repo_path = \'\' AND key = \'alpha\'')
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(TransactionStateError, match="Missing staged value"):
        tx.commit()

    tx.rollback()
    assert _fetch_objects(db_path) == {"beta": b"old"}


def test_recover_discards_open_transaction(tmp_path: Path) -> None:
    backend = SqliteBackend()
    db_path = tmp_path / "repo.db"

    tx = backend.begin(str(db_path))
    _write_bytes(tx, "alpha", b"one")
    _simulate_sqlite_process_exit(tx)

    backend.recover(str(db_path))

    assert _fetch_objects(db_path) == {}
    assert _fetch_tx_meta(db_path) == []


def test_recover_replays_prepared_transaction(tmp_path: Path) -> None:
    backend = SqliteBackend()
    db_path = tmp_path / "repo.db"
    _insert_object(db_path, "beta", b"old")

    tx = backend.begin(str(db_path))
    _write_bytes(tx, "alpha", b"one")
    tx.delete("beta")
    tx.prepare()
    _simulate_sqlite_process_exit(tx)

    backend.recover(str(db_path))

    assert _fetch_objects(db_path) == {"alpha": b"one"}
    assert _fetch_tx_meta(db_path) == []


def test_recover_child_locator_routes_to_root_coordinator(tmp_path: Path) -> None:
    backend = SqliteBackend()
    db_path = tmp_path / "repo.db"
    child_locator = backend.child_repo_locator(str(db_path), "users/alice")

    root_tx = backend.begin(str(db_path))
    child_tx = backend.begin(child_locator, parent_tx=root_tx)
    _write_bytes(root_tx, "config", b"root")
    _write_bytes(child_tx, "profile", b"alice")
    child_tx.prepare()
    root_tx.mark_child_prepared("users/alice")
    root_tx.prepare()
    _simulate_sqlite_process_exit(root_tx)

    backend.recover(child_locator)

    assert _fetch_objects(db_path) == {"config": b"root"}
    assert _fetch_objects(db_path, repo_path="users/alice") == {"profile": b"alice"}
    assert _fetch_tx_meta(db_path) == []


def test_nested_begin_rejects_mismatched_sqlite_backend_config(tmp_path: Path) -> None:
    root_backend = SqliteBackend()
    child_backend = SqliteBackend(config=SqliteConfig(schema_prefix="_storage_"))
    db_path = tmp_path / "repo.db"
    child_locator = child_backend.child_repo_locator(str(db_path), "users/alice")

    root_tx = root_backend.begin(str(db_path))
    try:
        with pytest.raises(TransactionStateError, match="same backend config"):
            child_backend.begin(child_locator, parent_tx=root_tx)
    finally:
        root_tx.rollback()


def test_schema_prefix_namespaces_internal_tables_and_indexes(tmp_path: Path) -> None:
    config = SqliteConfig(schema_prefix="_storage_")
    backend = SqliteBackend(config=config)
    db_path = tmp_path / "repo.db"

    tx = backend.begin(str(db_path))
    _write_bytes(tx, "alpha", b"one")
    tx.prepare()
    tx.commit()

    connection = _connect(db_path, config=config)
    try:
        names = config.schema_names
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        indexes = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        rows = list(connection.execute(f'SELECT repo_path, key, value FROM "{names.objects}"'))
    finally:
        connection.close()

    assert names.objects in tables
    assert names.tx_meta in tables
    assert names.tx_values in tables
    assert names.tx_children in tables
    assert "objects" not in tables
    assert "tx_meta" not in tables
    assert "tx_values" not in tables
    assert "tx_children" not in tables
    assert names.tx_meta_repo_path_idx in indexes
    assert names.tx_values_tx_id_idx in indexes
    assert rows == [("", "alpha", b"one")]


def test_sqlite_config_applies_pragmas_and_connection_hook(tmp_path: Path) -> None:
    hook_calls: list[sqlite3.Connection] = []

    def _hook(connection: sqlite3.Connection) -> None:
        hook_calls.append(connection)

    config = SqliteConfig(
        schema_prefix="_storage_",
        journal_mode="WAL",
        synchronous="NORMAL",
        busy_timeout_ms=2500,
        foreign_keys=True,
        wal_autocheckpoint=128,
        locking_mode="NORMAL",
        temp_store="MEMORY",
        connection_hook=_hook,
    )
    connection = _connect(tmp_path / "repo.db", config=config)
    try:
        ensure_schema(connection, config.schema_names)
        foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
        wal_autocheckpoint = int(connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0])
        temp_store = int(connection.execute("PRAGMA temp_store").fetchone()[0])
    finally:
        connection.close()

    assert len(hook_calls) == 1
    assert foreign_keys == 1
    assert busy_timeout == 2500
    assert wal_autocheckpoint == 128
    assert temp_store == 2


def test_transaction_read_failure_cleans_temp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = SqliteBackend()
    db_path = tmp_path / "repo.db"
    temp_path = tmp_path / "read-failure.bin"

    tx = backend.begin(str(db_path))
    _write_bytes(tx, "alpha", b"one")
    entry = tx.core.overlay["alpha"]
    assert isinstance(entry, Put)
    assert isinstance(entry.value, SqliteStagedValueRef)
    stage_id = entry.value.stage_id

    def fixed_temp_path() -> Path:
        return temp_path

    monkeypatch.setattr(sqlite_backend_module, "new_temp_path", fixed_temp_path)

    connection = _connect(db_path)
    try:
        names = backend.config.schema_names
        connection.execute(f'DELETE FROM "{names.tx_values}" WHERE id = ?', (stage_id,))
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(TransactionStateError, match="Missing staged value"):
        tx.open_handle("alpha", "rb")

    assert not temp_path.exists()
    tx.rollback()


def test_committed_read_failure_cleans_temp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = SqliteBackend()
    db_path = tmp_path / "repo.db"
    temp_path = tmp_path / "committed-read-failure.bin"
    _insert_object(db_path, "alpha", b"one")

    def fixed_temp_path() -> Path:
        return temp_path

    def fail_copy(*args, **kwargs) -> None:
        del args, kwargs
        raise RuntimeError("copy failed")

    monkeypatch.setattr(sqlite_backend_module, "new_temp_path", fixed_temp_path)
    monkeypatch.setattr("alpenstock.storage.backends.sqlite._support.copy_blob_to_path", fail_copy)

    with pytest.raises(RuntimeError, match="copy failed"):
        backend.open_committed_handle(str(db_path), "alpha", "rb")

    assert not temp_path.exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"schema_prefix": "bad"},
        {"schema_prefix": "bad-prefix_"},
        {"schema_prefix": "sqlite_"},
        {"busy_timeout_ms": -1},
        {"wal_autocheckpoint": -1},
        {"locking_mode": "BAD"},
        {"temp_store": "BAD"},
        {"temp_store": 3},
        {"temp_store": True},
        {"synchronous": "BAD"},
        {"synchronous": 4},
        {"synchronous": True},
    ],
)
def test_sqlite_config_validation_rejects_invalid_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SqliteConfig(**kwargs)
