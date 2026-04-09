from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import IO, Callable, Literal, TypeAlias

import attrs

from ..._errors import KeyNotFoundError, TransactionStateError
from ..._keys import join_logical_key, validate_logical_key
from ..._types import OpenMode
from ..._backend import RecoveryIntent

_BLOB_CHUNK_SIZE = 1024 * 1024
_REPO_MARKER = "::repo::"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VALID_JOURNAL_MODES = frozenset({"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"})
_VALID_SYNC_MODES = frozenset({"OFF", "NORMAL", "FULL", "EXTRA"})
_VALID_LOCKING_MODES = frozenset({"NORMAL", "EXCLUSIVE"})
_VALID_TEMP_STORE = frozenset({"DEFAULT", "FILE", "MEMORY"})
_VALID_SYNC_INTS = frozenset({0, 1, 2, 3})
_VALID_TEMP_STORE_INTS = frozenset({0, 1, 2})


@attrs.define(frozen=True, slots=True)
class SqliteSchemaNames:
    prefix: str = attrs.field(default="")

    @property
    def objects(self) -> str:
        return f"{self.prefix}objects"

    @property
    def tx_meta(self) -> str:
        return f"{self.prefix}tx_meta"

    @property
    def tx_values(self) -> str:
        return f"{self.prefix}tx_values"

    @property
    def tx_children(self) -> str:
        return f"{self.prefix}tx_children"

    @property
    def tx_meta_repo_path_idx(self) -> str:
        return f"{self.prefix}tx_meta_repo_path_idx"

    @property
    def tx_values_tx_id_idx(self) -> str:
        return f"{self.prefix}tx_values_tx_id_idx"


@attrs.define(frozen=True, slots=True)
class SqliteConfig:
    schema_prefix: str = attrs.field(default="")
    journal_mode: Literal["DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"] = attrs.field(default="WAL")
    synchronous: Literal["OFF", "NORMAL", "FULL", "EXTRA"] | int | None = attrs.field(default=None)
    busy_timeout_ms: int | None = attrs.field(default=None)
    foreign_keys: bool = attrs.field(default=True)
    wal_autocheckpoint: int | None = attrs.field(default=None)
    locking_mode: Literal["NORMAL", "EXCLUSIVE"] | None = attrs.field(default=None)
    temp_store: Literal["DEFAULT", "FILE", "MEMORY"] | int | None = attrs.field(default=None)
    connection_hook: Callable[[sqlite3.Connection], None] | None = attrs.field(default=None, repr=False, eq=False)

    def __attrs_post_init__(self) -> None:
        prefix = self.schema_prefix
        if prefix:
            if not prefix.endswith("_"):
                raise ValueError("SQLite schema_prefix must end with '_' when non-empty")
            if not _IDENTIFIER_RE.fullmatch(prefix):
                raise ValueError("SQLite schema_prefix must be ASCII identifier-like")
            if prefix.lower().startswith("sqlite_"):
                raise ValueError("SQLite schema_prefix must not start with reserved 'sqlite_'")
        if self.journal_mode not in _VALID_JOURNAL_MODES:
            raise ValueError(f"Unsupported SQLite journal_mode: {self.journal_mode!r}")
        if isinstance(self.synchronous, str) and self.synchronous not in _VALID_SYNC_MODES:
            raise ValueError(f"Unsupported SQLite synchronous mode: {self.synchronous!r}")
        if self.synchronous is not None and not isinstance(self.synchronous, str):
            if isinstance(self.synchronous, bool) or self.synchronous not in _VALID_SYNC_INTS:
                raise ValueError("SQLite synchronous integer mode must be one of 0, 1, 2, or 3")
        if self.busy_timeout_ms is not None and self.busy_timeout_ms < 0:
            raise ValueError("SQLite busy_timeout_ms must be >= 0")
        if self.wal_autocheckpoint is not None and self.wal_autocheckpoint < 0:
            raise ValueError("SQLite wal_autocheckpoint must be >= 0")
        if self.locking_mode is not None and self.locking_mode not in _VALID_LOCKING_MODES:
            raise ValueError(f"Unsupported SQLite locking_mode: {self.locking_mode!r}")
        if isinstance(self.temp_store, str) and self.temp_store not in _VALID_TEMP_STORE:
            raise ValueError(f"Unsupported SQLite temp_store mode: {self.temp_store!r}")
        if self.temp_store is not None and not isinstance(self.temp_store, str):
            if isinstance(self.temp_store, bool) or self.temp_store not in _VALID_TEMP_STORE_INTS:
                raise ValueError("SQLite temp_store integer mode must be one of 0, 1, or 2")

    @property
    def schema_names(self) -> SqliteSchemaNames:
        return SqliteSchemaNames(prefix=self.schema_prefix)


@attrs.define(frozen=True, slots=True)
class SqliteCommittedValueRef:
    repo_path: str = attrs.field()
    key: str = attrs.field()


@attrs.define(frozen=True, slots=True)
class SqliteStagedValueRef:
    tx_id: str = attrs.field()
    repo_path: str = attrs.field()
    key: str = attrs.field()
    stage_id: int = attrs.field()


SqliteValueRef: TypeAlias = SqliteCommittedValueRef | SqliteStagedValueRef


def parse_repo_locator(repo_locator: str) -> tuple[Path, str]:
    if _REPO_MARKER not in repo_locator:
        return Path(repo_locator), ""
    db_path, repo_path = repo_locator.split(_REPO_MARKER, 1)
    return Path(db_path), validate_logical_key(repo_path)


def compose_repo_locator(db_path: Path, repo_path: str) -> str:
    if repo_path == "":
        return str(db_path)
    return f"{db_path}{_REPO_MARKER}{validate_logical_key(repo_path)}"


def compose_root_repo_locator(repo_locator: str) -> str:
    db_path, _repo_path = parse_repo_locator(repo_locator)
    return compose_repo_locator(db_path, "")


def compose_child_repo_locator(repo_locator: str, child_repo_path: str) -> str:
    db_path, repo_path = parse_repo_locator(repo_locator)
    combined_repo_path = join_logical_key(repo_path, child_repo_path) if repo_path else validate_logical_key(child_repo_path)
    return compose_repo_locator(db_path, combined_repo_path)


def sqlite_configs_compatible(left: SqliteConfig, right: SqliteConfig) -> bool:
    return (
        left.schema_prefix == right.schema_prefix
        and left.journal_mode == right.journal_mode
        and left.synchronous == right.synchronous
        and left.busy_timeout_ms == right.busy_timeout_ms
        and left.foreign_keys == right.foreign_keys
        and left.wal_autocheckpoint == right.wal_autocheckpoint
        and left.locking_mode == right.locking_mode
        and left.temp_store == right.temp_store
        and left.connection_hook is right.connection_hook
    )


def _quote_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"Unsafe SQLite identifier: {name!r}")
    return f'"{name}"'


def connect(db_path: Path, config: SqliteConfig | None = None) -> sqlite3.Connection:
    effective_config = SqliteConfig() if config is None else config
    connection = sqlite3.connect(str(db_path))
    connection.execute(f"PRAGMA foreign_keys = {'ON' if effective_config.foreign_keys else 'OFF'}")
    connection.execute(f"PRAGMA journal_mode = {effective_config.journal_mode}")
    if effective_config.synchronous is not None:
        connection.execute(f"PRAGMA synchronous = {effective_config.synchronous}")
    if effective_config.busy_timeout_ms is not None:
        connection.execute(f"PRAGMA busy_timeout = {effective_config.busy_timeout_ms}")
    if effective_config.wal_autocheckpoint is not None:
        connection.execute(f"PRAGMA wal_autocheckpoint = {effective_config.wal_autocheckpoint}")
    if effective_config.locking_mode is not None:
        connection.execute(f"PRAGMA locking_mode = {effective_config.locking_mode}")
    if effective_config.temp_store is not None:
        connection.execute(f"PRAGMA temp_store = {effective_config.temp_store}")
    if effective_config.connection_hook is not None:
        effective_config.connection_hook(connection)
    return connection


def ensure_schema(connection: sqlite3.Connection, names: SqliteSchemaNames | None = None) -> None:
    effective_names = SqliteSchemaNames() if names is None else names
    connection.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {_quote_identifier(effective_names.objects)} (
            repo_path TEXT NOT NULL,
            key TEXT NOT NULL,
            value BLOB NOT NULL,
            PRIMARY KEY(repo_path, key)
        );

        CREATE TABLE IF NOT EXISTS {_quote_identifier(effective_names.tx_meta)} (
            tx_id TEXT PRIMARY KEY,
            repo_path TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('open', 'prepared'))
        );

        CREATE INDEX IF NOT EXISTS {_quote_identifier(effective_names.tx_meta_repo_path_idx)}
        ON {_quote_identifier(effective_names.tx_meta)}(repo_path);

        CREATE TABLE IF NOT EXISTS {_quote_identifier(effective_names.tx_values)} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_id TEXT NOT NULL,
            repo_path TEXT NOT NULL,
            key TEXT NOT NULL,
            value BLOB,
            is_delete INTEGER NOT NULL CHECK(is_delete IN (0, 1)),
            FOREIGN KEY(tx_id) REFERENCES {_quote_identifier(effective_names.tx_meta)}(tx_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS {_quote_identifier(effective_names.tx_values_tx_id_idx)}
        ON {_quote_identifier(effective_names.tx_values)}(tx_id);

        CREATE TABLE IF NOT EXISTS {_quote_identifier(effective_names.tx_children)} (
            parent_tx_id TEXT NOT NULL,
            child_repo_path TEXT NOT NULL,
            prepared INTEGER NOT NULL CHECK(prepared IN (0, 1)),
            committed INTEGER NOT NULL CHECK(committed IN (0, 1)),
            PRIMARY KEY(parent_tx_id, child_repo_path),
            FOREIGN KEY(parent_tx_id) REFERENCES {_quote_identifier(effective_names.tx_meta)}(tx_id) ON DELETE CASCADE
        );
        """
    )
    connection.commit()


def sqlite_lock_path(db_path: Path) -> Path:
    return Path(f"{db_path}.repo_tx.lock")


def open_path_handle(path: Path, mode: OpenMode, *, encoding: str | None = None) -> IO[str] | IO[bytes]:
    if "b" in mode:
        return path.open(mode)
    return path.open(mode, encoding="utf-8" if encoding is None else encoding)


def new_temp_path(prefix: str = "alpenstock-sqlite-") -> Path:
    fd, raw_path = tempfile.mkstemp(prefix=prefix)
    os.close(fd)
    return Path(raw_path)


def remove_path_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def copy_blob_to_path(connection: sqlite3.Connection, table: str, rowid: int, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle, connection.blobopen(table, "value", rowid, readonly=True) as blob:
        while True:
            chunk = blob.read(_BLOB_CHUNK_SIZE)
            if not chunk:
                break
            handle.write(chunk)


def copy_path_to_blob(connection: sqlite3.Connection, table: str, rowid: int, source: Path) -> None:
    with source.open("rb") as handle, connection.blobopen(table, "value", rowid, readonly=False) as blob:
        while True:
            chunk = handle.read(_BLOB_CHUNK_SIZE)
            if not chunk:
                break
            blob.write(chunk)


def fetch_object_rowid(connection: sqlite3.Connection, names: SqliteSchemaNames, repo_path: str, key: str) -> int | None:
    row = connection.execute(
        f"SELECT rowid FROM {_quote_identifier(names.objects)} WHERE repo_path = ? AND key = ?",
        (repo_path, key),
    ).fetchone()
    if row is None:
        return None
    return int(row[0])


def fetch_stage_row(connection: sqlite3.Connection, names: SqliteSchemaNames, stage_id: int) -> tuple[str, str, str, int] | None:
    row = connection.execute(
        f"SELECT tx_id, repo_path, key, is_delete FROM {_quote_identifier(names.tx_values)} WHERE id = ?",
        (stage_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1]), str(row[2]), int(row[3])


def copy_value_ref_to_path(
    connection: sqlite3.Connection,
    names: SqliteSchemaNames,
    value_ref: SqliteValueRef,
    destination: Path,
) -> None:
    if isinstance(value_ref, SqliteCommittedValueRef):
        rowid = fetch_object_rowid(connection, names, value_ref.repo_path, value_ref.key)
        if rowid is None:
            raise KeyNotFoundError(f"Logical key {value_ref.key!r} does not exist")
        copy_blob_to_path(connection, names.objects, rowid, destination)
        return

    record = fetch_stage_row(connection, names, value_ref.stage_id)
    if record is None:
        raise TransactionStateError(f"Missing staged value for key {value_ref.key!r}")
    record_tx_id, record_repo_path, record_key, is_delete = record
    if (
        record_tx_id != value_ref.tx_id
        or record_repo_path != value_ref.repo_path
        or record_key != value_ref.key
        or is_delete != 0
    ):
        raise TransactionStateError(f"Missing staged value for key {value_ref.key!r}")
    copy_blob_to_path(connection, names.tx_values, value_ref.stage_id, destination)


def stage_value_from_path(
    connection: sqlite3.Connection,
    names: SqliteSchemaNames,
    tx_id: str,
    repo_path: str,
    key: str,
    source: Path,
) -> SqliteStagedValueRef:
    size = source.stat().st_size
    try:
        cursor = connection.execute(
            f"""
            INSERT INTO {_quote_identifier(names.tx_values)} (tx_id, repo_path, key, value, is_delete)
            VALUES (?, ?, ?, zeroblob(?), 0)
            """,
            (tx_id, repo_path, key, size),
        )
        if cursor.lastrowid is None:
            raise TransactionStateError(f"Failed to stage value for key {key!r}")
        stage_id = int(cursor.lastrowid)
        copy_path_to_blob(connection, names.tx_values, stage_id, source)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return SqliteStagedValueRef(tx_id=tx_id, repo_path=repo_path, key=key, stage_id=stage_id)


def stage_delete(connection: sqlite3.Connection, names: SqliteSchemaNames, tx_id: str, repo_path: str, key: str) -> int:
    cursor = connection.execute(
        f"""
        INSERT INTO {_quote_identifier(names.tx_values)} (tx_id, repo_path, key, value, is_delete)
        VALUES (?, ?, ?, NULL, 1)
        """,
        (tx_id, repo_path, key),
    )
    if cursor.lastrowid is None:
        raise TransactionStateError(f"Failed to stage delete for key {key!r}")
    connection.commit()
    return int(cursor.lastrowid)


def delete_stage_row(connection: sqlite3.Connection, names: SqliteSchemaNames, stage_id: int) -> None:
    connection.execute(f"DELETE FROM {_quote_identifier(names.tx_values)} WHERE id = ?", (stage_id,))
    connection.commit()


def validate_unique_staged_keys(connection: sqlite3.Connection, names: SqliteSchemaNames, tx_id: str) -> None:
    duplicate = connection.execute(
        f"""
        SELECT key
        FROM {_quote_identifier(names.tx_values)}
        WHERE tx_id = ?
        GROUP BY key
        HAVING COUNT(*) > 1
        LIMIT 1
        """,
        (tx_id,),
    ).fetchone()
    if duplicate is not None:
        raise TransactionStateError(
            f"Prepared sqlite transaction contains duplicate staged rows for key {duplicate[0]!r}"
        )


def apply_prepared_transaction(connection: sqlite3.Connection, names: SqliteSchemaNames, tx_id: str) -> None:
    validate_unique_staged_keys(connection, names, tx_id)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            f"""
            INSERT INTO {_quote_identifier(names.objects)} (repo_path, key, value)
            SELECT repo_path, key, value
            FROM {_quote_identifier(names.tx_values)}
            WHERE tx_id = ? AND is_delete = 0
            ON CONFLICT(repo_path, key) DO UPDATE SET value = excluded.value
            """,
            (tx_id,),
        )
        connection.execute(
            f"""
            DELETE FROM {_quote_identifier(names.objects)}
            WHERE (repo_path, key) IN (
                SELECT repo_path, key FROM {_quote_identifier(names.tx_values)} WHERE tx_id = ? AND is_delete = 1
            )
            """,
            (tx_id,),
        )
        connection.execute(f"DELETE FROM {_quote_identifier(names.tx_meta)} WHERE tx_id = ?", (tx_id,))
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def fetch_repo_tx(connection: sqlite3.Connection, names: SqliteSchemaNames, repo_path: str) -> tuple[str, str] | None:
    row = connection.execute(
        f"SELECT tx_id, state FROM {_quote_identifier(names.tx_meta)} WHERE repo_path = ? ORDER BY tx_id LIMIT 1",
        (repo_path,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1])


def fetch_child_rows(connection: sqlite3.Connection, names: SqliteSchemaNames, parent_tx_id: str) -> list[tuple[str, bool, bool]]:
    return [
        (str(row[0]), bool(row[1]), bool(row[2]))
        for row in connection.execute(
            f"""
            SELECT child_repo_path, prepared, committed
            FROM {_quote_identifier(names.tx_children)}
            WHERE parent_tx_id = ?
            ORDER BY child_repo_path
            """,
            (parent_tx_id,),
        )
    ]


def find_recovery_root_repo_path(
    connection: sqlite3.Connection,
    names: SqliteSchemaNames,
    requested_repo_path: str,
) -> str:
    if requested_repo_path == "":
        return ""
    ancestor_paths = [""]
    current = ""
    for part in requested_repo_path.split("/"):
        current = part if current == "" else f"{current}/{part}"
        ancestor_paths.append(current)
    for candidate in ancestor_paths[:-1]:
        if _ancestor_tree_contains_repo(connection, names, candidate, requested_repo_path):
            return candidate
    return requested_repo_path


def _ancestor_tree_contains_repo(
    connection: sqlite3.Connection,
    names: SqliteSchemaNames,
    ancestor_repo_path: str,
    target_repo_path: str,
) -> bool:
    pending = fetch_repo_tx(connection, names, ancestor_repo_path)
    if pending is None:
        return False
    tx_id, _state = pending
    for child_repo_path, _prepared, _committed in fetch_child_rows(connection, names, tx_id):
        child_full_repo_path = join_logical_key(ancestor_repo_path, child_repo_path) if ancestor_repo_path else child_repo_path
        if child_full_repo_path == target_repo_path:
            return True
        if target_repo_path.startswith(f"{child_full_repo_path}/") and _ancestor_tree_contains_repo(
            connection,
            names,
            child_full_repo_path,
            target_repo_path,
        ):
            return True
    return False


def recover_repo_path(
    connection: sqlite3.Connection,
    names: SqliteSchemaNames,
    repo_path: str,
    *,
    intent: RecoveryIntent,
) -> None:
    pending = fetch_repo_tx(connection, names, repo_path)
    if pending is None:
        return

    tx_id, state = pending
    children = fetch_child_rows(connection, names, tx_id)
    if intent == "abort" or state == "open":
        for child_repo_path, _prepared, _committed in children:
            child_full_repo_path = join_logical_key(repo_path, child_repo_path) if repo_path else child_repo_path
            recover_repo_path(connection, names, child_full_repo_path, intent="abort")
        connection.execute(f"DELETE FROM {_quote_identifier(names.tx_meta)} WHERE tx_id = ?", (tx_id,))
        connection.commit()
        return

    for child_repo_path, _prepared, committed in children:
        if committed:
            continue
        child_full_repo_path = join_logical_key(repo_path, child_repo_path) if repo_path else child_repo_path
        recover_repo_path(connection, names, child_full_repo_path, intent="auto")
    apply_prepared_transaction(connection, names, tx_id)


__all__ = [
    "SqliteCommittedValueRef",
    "SqliteConfig",
    "SqliteSchemaNames",
    "SqliteStagedValueRef",
    "SqliteValueRef",
    "apply_prepared_transaction",
    "compose_child_repo_locator",
    "compose_repo_locator",
    "compose_root_repo_locator",
    "connect",
    "copy_value_ref_to_path",
    "delete_stage_row",
    "ensure_schema",
    "fetch_child_rows",
    "find_recovery_root_repo_path",
    "fetch_object_rowid",
    "fetch_repo_tx",
    "fetch_stage_row",
    "new_temp_path",
    "open_path_handle",
    "parse_repo_locator",
    "recover_repo_path",
    "remove_path_if_exists",
    "sqlite_configs_compatible",
    "sqlite_lock_path",
    "stage_delete",
    "stage_value_from_path",
    "validate_unique_staged_keys",
]
