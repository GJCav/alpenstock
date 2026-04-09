from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path
from typing import IO, cast

import attrs

from ..._backend import BackendTransaction, RecoveryIntent
from ..._errors import HandleStateError, KeyNotFoundError, TransactionStateError
from ..._handles import BinaryFileHandle, FileHandle, TextFileHandle, _OpenSpec, validate_open_request, wrap_raw_handle
from ..._keys import validate_logical_key
from ..._tx_core import TransactionCore
from ..._types import DELETE, LogicalKey, OpenMode, OverlayEntry, Put, TransactionState
from ..fs.locking import WriterLock
from ._support import (
    SqliteCommittedValueRef,
    SqliteConfig,
    SqliteSchemaNames,
    SqliteStagedValueRef,
    SqliteValueRef,
    apply_prepared_transaction,
    compose_child_repo_locator,
    connect,
    copy_value_ref_to_path,
    delete_stage_row,
    ensure_schema,
    find_recovery_root_repo_path,
    fetch_object_rowid,
    fetch_repo_tx,
    fetch_stage_row,
    new_temp_path,
    open_path_handle,
    parse_repo_locator,
    recover_repo_path,
    remove_path_if_exists,
    sqlite_configs_compatible,
    sqlite_lock_path,
    stage_delete,
    stage_value_from_path,
    validate_unique_staged_keys,
)


@attrs.define(slots=True)
class _CleanupPathHandle:
    inner: FileHandle = attrs.field(repr=False)
    cleanup_path: Path = attrs.field(repr=False)
    _closed: bool = attrs.field(default=False, init=False, repr=False)

    @property
    def closed(self) -> bool:
        return self._closed or self.inner.closed

    @property
    def encoding(self) -> str | None:
        return self.inner.encoding

    def read(self, size: int = -1) -> str | bytes:
        return self.inner.read(size)

    def write(self, data: str | bytes) -> int:
        if isinstance(data, str):
            return cast(TextFileHandle, self.inner).write(data)
        return cast(BinaryFileHandle, self.inner).write(cast(bytes, data))

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.inner.seek(offset, whence)

    def tell(self) -> int:
        return self.inner.tell()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.inner.close()
        finally:
            remove_path_if_exists(self.cleanup_path)
            self._closed = True

    def __enter__(self) -> _CleanupPathHandle:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
        return None


@attrs.define(slots=True)
class _SqliteWritableHandle:
    tx: SqliteBackendTransaction = attrs.field(repr=False)
    key: LogicalKey = attrs.field()
    path: Path = attrs.field(repr=False)
    spec: _OpenSpec = attrs.field(repr=False)
    encoding_name: str | None = attrs.field(default=None, repr=False)
    _handle: IO[str] | IO[bytes] = attrs.field(init=False, repr=False)
    _closed: bool = attrs.field(default=False, init=False, repr=False)

    def __attrs_post_init__(self) -> None:
        self._handle = open_path_handle(self.path, self.spec.mode, encoding=self.encoding_name)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def encoding(self) -> str | None:
        if self.spec.binary:
            return None
        return "utf-8" if self.encoding_name is None else self.encoding_name

    def read(self, size: int = -1) -> str | bytes:
        self._require_open()
        if not self.spec.readable:
            raise HandleStateError(f"Handle for key {self.key!r} is not open for reading")
        return self._handle.read(size)

    def write(self, data: str | bytes) -> int:
        self._require_open()
        if not self.spec.writable:
            raise HandleStateError(f"Handle for key {self.key!r} is not open for writing")
        if self.spec.binary:
            if not isinstance(data, bytes):
                raise TypeError("Binary handles require bytes writes")
            return cast(BinaryFileHandle, self._handle).write(data)
        if not isinstance(data, str):
            raise TypeError("Text handles require str writes")
        return cast(TextFileHandle, self._handle).write(data)

    def seek(self, offset: int, whence: int = 0) -> int:
        self._require_open()
        return self._handle.seek(offset, whence)

    def tell(self) -> int:
        self._require_open()
        return self._handle.tell()

    def flush_for_external_read(self) -> None:
        self._require_open()
        self._handle.flush()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._handle.flush()
            if hasattr(self._handle, "fileno"):
                os.fsync(self._handle.fileno())
            self._handle.close()
            self.tx._seal_writer(self)
        except Exception:
            self.tx._discard_writer(self)
            self._finalize_close()
            raise
        self._finalize_close()

    def __enter__(self) -> _SqliteWritableHandle:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
        else:
            self._discard()
        return None

    def _discard(self) -> None:
        if self._closed:
            return
        self.tx._discard_writer(self)
        self._finalize_close()

    def _finalize_close(self) -> None:
        self._closed = True
        if not self._handle.closed:
            self._handle.close()

    def _require_open(self) -> None:
        if self._closed:
            raise HandleStateError(f"Handle for key {self.key!r} is already closed")


@attrs.define(slots=True)
class SqliteBackendTransaction:
    db_path: Path = attrs.field(converter=Path, repr=False)
    repo_path: str = attrs.field()
    lock: WriterLock = attrs.field(repr=False)
    connection: sqlite3.Connection = attrs.field(repr=False)
    tx_id: str = attrs.field()
    config: SqliteConfig = attrs.field(repr=False)
    schema_names: SqliteSchemaNames = attrs.field(repr=False)
    core: TransactionCore[SqliteValueRef] = attrs.field(repr=False)
    owns_resources: bool = attrs.field(default=True, repr=False)
    _open_writers: dict[LogicalKey, _SqliteWritableHandle] = attrs.field(factory=dict, init=False, repr=False)
    _children: dict[str, dict[str, bool]] = attrs.field(factory=dict, init=False, repr=False)
    _metadata_initialized: bool = attrs.field(default=False, init=False, repr=False)
    _released: bool = attrs.field(default=False, init=False, repr=False)

    @property
    def has_open_writers(self) -> bool:
        return bool(self._open_writers)

    def open_handle(self, key: str, mode: OpenMode = "r", *, encoding: str | None = None) -> FileHandle:
        key = validate_logical_key(key)
        spec = validate_open_request(key, mode, encoding=encoding)
        if spec.writable:
            return self._open_writable_handle(key, spec, encoding=encoding)
        return self._open_read_handle(key, spec, encoding=encoding)

    def delete(self, key: str) -> None:
        validated_key = validate_logical_key(key)
        previous_entry = self.core.overlay.get(validated_key)
        if self.core.state is not TransactionState.OPEN or validated_key in self._open_writers:
            self.core.delete(validated_key)
            return
        self._ensure_open_metadata()
        self.core.delete(validated_key)
        self._prune_superseded_entry(previous_entry)

    def assert_all_writers_closed(self) -> None:
        if not self._open_writers:
            return
        keys = ", ".join(repr(key) for key in sorted(self._open_writers))
        raise HandleStateError(f"Writable handles must be closed before prepare: {keys}")

    def discard_open_writers(self) -> None:
        for handle in list(self._open_writers.values()):
            handle._discard()
        self._open_writers.clear()

    def register_child_repo(self, child_repo_path: str) -> None:
        self._ensure_open_metadata()
        self.connection.execute(
            f"""
            INSERT INTO "{self.schema_names.tx_children}" (parent_tx_id, child_repo_path, prepared, committed)
            VALUES (?, ?, 0, 0)
            ON CONFLICT(parent_tx_id, child_repo_path) DO NOTHING
            """,
            (self.tx_id, child_repo_path),
        )
        self.connection.commit()
        self._children.setdefault(child_repo_path, {"prepared": False, "committed": False})

    def unregister_child_repo(self, child_repo_path: str) -> None:
        self._children.pop(child_repo_path, None)
        self.connection.execute(
            f'DELETE FROM "{self.schema_names.tx_children}" WHERE parent_tx_id = ? AND child_repo_path = ?',
            (self.tx_id, child_repo_path),
        )
        self.connection.commit()
        self._cleanup_metadata_if_empty()

    def mark_child_prepared(self, child_repo_path: str) -> None:
        self._ensure_open_metadata()
        self.connection.execute(
            f'UPDATE "{self.schema_names.tx_children}" SET prepared = 1 WHERE parent_tx_id = ? AND child_repo_path = ?',
            (self.tx_id, child_repo_path),
        )
        self.connection.commit()
        self._children.setdefault(child_repo_path, {"prepared": False, "committed": False})["prepared"] = True

    def mark_child_committed(self, child_repo_path: str) -> None:
        self._ensure_open_metadata()
        self.connection.execute(
            f'UPDATE "{self.schema_names.tx_children}" SET prepared = 1, committed = 1 WHERE parent_tx_id = ? AND child_repo_path = ?',
            (self.tx_id, child_repo_path),
        )
        self.connection.commit()
        child_state = self._children.setdefault(child_repo_path, {"prepared": False, "committed": False})
        child_state["prepared"] = True
        child_state["committed"] = True

    def prepare(self) -> None:
        self.core.prepare()
        assert self.core.prepared_overlay is not None
        self._ensure_open_metadata()
        self._persist_prepared_overlay(self.core.prepared_overlay)
        self.connection.execute(f'UPDATE "{self.schema_names.tx_meta}" SET state = \'prepared\' WHERE tx_id = ?', (self.tx_id,))
        self.connection.commit()

    def commit(self) -> None:
        prepared_overlay = self.core.prepared_overlay
        if prepared_overlay is None:
            raise TransactionStateError("SQLite backend commit() requires a prepared overlay")
        self._validate_prepared_overlay(prepared_overlay)
        apply_prepared_transaction(self.connection, self.schema_names, self.tx_id)
        self.core.commit()
        self._metadata_initialized = False
        self._release_resources()

    def rollback(self) -> None:
        self.core.rollback()
        self.connection.execute(f'DELETE FROM "{self.schema_names.tx_meta}" WHERE tx_id = ?', (self.tx_id,))
        self.connection.commit()
        self._metadata_initialized = False
        self._release_resources()

    def detach_for_recovery(self) -> None:
        self.discard_open_writers()
        self._release_resources()

    def _open_read_handle(self, key: str, spec: _OpenSpec, *, encoding: str | None = None) -> FileHandle:
        writer = self._open_writers.get(key)
        if writer is not None:
            writer.flush_for_external_read()
            return wrap_raw_handle(key, spec, open_path_handle(writer.path, spec.mode, encoding=encoding), encoding=encoding)

        value_ref = self.core.read_visible_ref(key)
        if value_ref is None:
            raise KeyNotFoundError(f"Logical key {key!r} does not exist")
        temp_path = new_temp_path()
        try:
            copy_value_ref_to_path(self.connection, self.schema_names, value_ref, temp_path)
        except Exception:
            remove_path_if_exists(temp_path)
            raise
        return cast(
            FileHandle,
            _CleanupPathHandle(
                inner=wrap_raw_handle(key, spec, open_path_handle(temp_path, spec.mode, encoding=encoding), encoding=encoding),
                cleanup_path=temp_path,
            ),
        )

    def _open_writable_handle(self, key: str, spec: _OpenSpec, *, encoding: str | None = None) -> FileHandle:
        source_ref = self.core.read_visible_ref(key)
        if spec.require_exists and source_ref is None:
            raise KeyNotFoundError(f"Logical key {key!r} does not exist")

        self._ensure_open_metadata()
        self.core.register_writable_handle(key)
        path = new_temp_path()
        try:
            self._initialize_work_path(path, source_ref=source_ref, spec=spec)
            handle = _SqliteWritableHandle(
                tx=self,
                key=key,
                path=path,
                spec=spec,
                encoding_name=encoding if not spec.binary else None,
            )
        except Exception:
            self.core.unregister_writable_handle(key)
            remove_path_if_exists(path)
            self._cleanup_metadata_if_empty()
            raise

        self._open_writers[key] = handle
        return cast(FileHandle, handle)

    def _initialize_work_path(self, path: Path, *, source_ref: SqliteValueRef | None, spec: _OpenSpec) -> None:
        if spec.truncate or source_ref is None:
            path.write_bytes(b"")
            return
        copy_value_ref_to_path(self.connection, self.schema_names, source_ref, path)

    def _seal_writer(self, handle: _SqliteWritableHandle) -> None:
        previous_entry = self.core.overlay.get(handle.key)
        try:
            value_ref = stage_value_from_path(
                self.connection,
                self.schema_names,
                self.tx_id,
                self.repo_path,
                handle.key,
                handle.path,
            )
            self.core.put_ref(handle.key, value_ref)
            self._prune_superseded_entry(previous_entry)
        finally:
            self._open_writers.pop(handle.key, None)
            self.core.unregister_writable_handle(handle.key)
            remove_path_if_exists(handle.path)

    def _discard_writer(self, handle: _SqliteWritableHandle) -> None:
        self._open_writers.pop(handle.key, None)
        self.core.unregister_writable_handle(handle.key)
        remove_path_if_exists(handle.path)
        self._cleanup_metadata_if_empty()

    def _ensure_open_metadata(self) -> None:
        if self._metadata_initialized:
            return
        self.connection.execute(
            f"""
            INSERT INTO "{self.schema_names.tx_meta}" (tx_id, repo_path, state)
            VALUES (?, ?, 'open')
            ON CONFLICT(tx_id) DO NOTHING
            """,
            (self.tx_id, self.repo_path),
        )
        self.connection.commit()
        self._metadata_initialized = True

    def _cleanup_metadata_if_empty(self) -> None:
        if self._metadata_initialized and not self._open_writers and not self.core.overlay and not self._children:
            self.connection.execute(f'DELETE FROM "{self.schema_names.tx_meta}" WHERE tx_id = ?', (self.tx_id,))
            self.connection.commit()
            self._metadata_initialized = False

    def _persist_prepared_overlay(self, overlay: dict[str, OverlayEntry[SqliteValueRef]]) -> None:
        keep_stage_ids: set[int] = set()
        delete_keys: list[str] = []

        for key, entry in overlay.items():
            if entry is DELETE:
                delete_keys.append(key)
                continue
            assert isinstance(entry, Put)
            value = entry.value
            if not isinstance(value, SqliteStagedValueRef):
                raise TransactionStateError(
                    f"SQLite prepared overlay requires staged value refs, got {value!r} for key {key!r}"
                )
            if value.tx_id != self.tx_id or value.repo_path != self.repo_path:
                raise TransactionStateError(f"Unexpected staged value owner for key {key!r}: {value!r}")
            keep_stage_ids.add(value.stage_id)

        if keep_stage_ids:
            placeholders = ", ".join("?" for _ in keep_stage_ids)
            params = (self.tx_id, *sorted(keep_stage_ids))
            self.connection.execute(
                f'DELETE FROM "{self.schema_names.tx_values}" WHERE tx_id = ? AND id NOT IN ({placeholders})',
                params,
            )
        else:
            self.connection.execute(f'DELETE FROM "{self.schema_names.tx_values}" WHERE tx_id = ?', (self.tx_id,))

        for key in delete_keys:
            stage_delete(self.connection, self.schema_names, self.tx_id, self.repo_path, key)
        self.connection.commit()

    def _validate_prepared_overlay(self, overlay: dict[str, OverlayEntry[SqliteValueRef]]) -> None:
        validate_unique_staged_keys(self.connection, self.schema_names, self.tx_id)
        staged_rows = {
            (str(row[0]), str(row[1]), str(row[2]), int(row[3]), int(row[4]))
            for row in self.connection.execute(
                f'SELECT tx_id, repo_path, key, id, is_delete FROM "{self.schema_names.tx_values}" WHERE tx_id = ?',
                (self.tx_id,),
            )
        }
        expected_keys: set[str] = set()
        for key, entry in overlay.items():
            expected_keys.add(key)
            if entry is DELETE:
                matching_delete = any(
                    row_tx_id == self.tx_id and row_repo_path == self.repo_path and row_key == key and is_delete == 1
                    for row_tx_id, row_repo_path, row_key, _row_id, is_delete in staged_rows
                )
                if not matching_delete:
                    raise TransactionStateError(f"Missing staged delete for key {key!r}")
                continue
            assert isinstance(entry, Put)
            value = entry.value
            if not isinstance(value, SqliteStagedValueRef):
                raise TransactionStateError(f"Unexpected value ref for key {key!r}: {value!r}")
            record = fetch_stage_row(self.connection, self.schema_names, value.stage_id)
            if record is None:
                raise TransactionStateError(f"Missing staged value for key {key!r}")
            record_tx_id, record_repo_path, record_key, is_delete = record
            if (
                record_tx_id != self.tx_id
                or record_repo_path != self.repo_path
                or record_key != key
                or is_delete != 0
            ):
                raise TransactionStateError(f"Missing staged value for key {key!r}")

        unexpected_keys = {
            row_key
            for row_tx_id, row_repo_path, row_key, _row_id, _is_delete in staged_rows
            if row_tx_id == self.tx_id and row_repo_path == self.repo_path and row_key not in expected_keys
        }
        if unexpected_keys:
            unexpected = ", ".join(sorted(unexpected_keys))
            raise TransactionStateError(f"Unexpected staged rows found for prepared keys: {unexpected}")

    def _prune_superseded_entry(self, entry: OverlayEntry[SqliteValueRef] | None) -> None:
        if entry is None or entry is DELETE:
            return
        assert isinstance(entry, Put)
        value = entry.value
        if isinstance(value, SqliteStagedValueRef):
            delete_stage_row(self.connection, self.schema_names, value.stage_id)

    def _release_resources(self) -> None:
        if self._released:
            return
        if self.owns_resources:
            self.lock.release()
            self.connection.close()
        self._released = True


@attrs.define(frozen=True, slots=True)
class SqliteBackend:
    config: SqliteConfig = attrs.field(factory=SqliteConfig)

    @property
    def schema_names(self) -> SqliteSchemaNames:
        return self.config.schema_names

    def child_repo_locator(self, repo_locator: str, child_repo_path: str) -> str:
        return compose_child_repo_locator(repo_locator, child_repo_path)

    def open_committed_handle(
        self,
        repo_locator: str,
        key: str,
        mode: OpenMode = "r",
        *,
        encoding: str | None = None,
    ) -> FileHandle:
        key = validate_logical_key(key)
        spec = validate_open_request(key, mode, encoding=encoding)
        if spec.writable:
            raise TransactionStateError("Committed views are read-only; open a transaction for writable access")
        db_path, repo_path = parse_repo_locator(repo_locator)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = connect(db_path, self.config)
        try:
            ensure_schema(connection, self.schema_names)
            rowid = fetch_object_rowid(connection, self.schema_names, repo_path, key)
            if rowid is None:
                raise KeyNotFoundError(f"Logical key {key!r} does not exist")
            temp_path = new_temp_path()
            from ._support import copy_blob_to_path

            try:
                copy_blob_to_path(connection, self.schema_names.objects, rowid, temp_path)
            except Exception:
                remove_path_if_exists(temp_path)
                raise
        finally:
            connection.close()
        return cast(
            FileHandle,
            _CleanupPathHandle(
                inner=wrap_raw_handle(key, spec, open_path_handle(temp_path, spec.mode, encoding=encoding), encoding=encoding),
                cleanup_path=temp_path,
            ),
        )

    def begin(
        self,
        repo_locator: str,
        parent_tx: BackendTransaction | None = None,
    ) -> SqliteBackendTransaction:
        db_path, repo_path = parse_repo_locator(repo_locator)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        if parent_tx is not None:
            if not isinstance(parent_tx, SqliteBackendTransaction):
                raise TransactionStateError("SQLite nested repo transactions require a sqlite parent transaction")
            if parent_tx.db_path != db_path:
                raise TransactionStateError("SQLite coordinated child repos must live in the same database file")
            if not sqlite_configs_compatible(parent_tx.config, self.config):
                raise TransactionStateError("SQLite coordinated child repos must use the same backend config")
            child_tx = SqliteBackendTransaction(
                db_path=db_path,
                repo_path=repo_path,
                lock=parent_tx.lock,
                connection=parent_tx.connection,
                tx_id=str(uuid.uuid4()),
                config=self.config,
                schema_names=self.schema_names,
                core=TransactionCore(
                    base_state={
                        key: SqliteCommittedValueRef(repo_path=repo_path, key=key)
                        for key in self._snapshot_keys(parent_tx.connection, repo_path)
                    }
                ),
                owns_resources=False,
            )
            if parent_tx.repo_path == "":
                child_repo_path = validate_logical_key(repo_path)
            else:
                prefix = f"{parent_tx.repo_path}/"
                if not repo_path.startswith(prefix):
                    raise TransactionStateError(
                        f"Child repo {repo_path!r} must be nested under parent repo {parent_tx.repo_path!r}"
                    )
                child_repo_path = validate_logical_key(repo_path[len(prefix):])
            parent_tx.register_child_repo(child_repo_path)
            return child_tx

        lock = WriterLock(sqlite_lock_path(db_path))
        lock.acquire()
        connection = connect(db_path, self.config)
        try:
            ensure_schema(connection, self.schema_names)
            pending = connection.execute(f'SELECT COUNT(*) FROM "{self.schema_names.tx_meta}"').fetchone()
            if pending is not None and int(pending[0]) > 0:
                raise TransactionStateError(
                    "Cannot begin a sqlite transaction while pending recovery state exists; run recover() first"
                )
            return SqliteBackendTransaction(
                db_path=db_path,
                repo_path=repo_path,
                lock=lock,
                connection=connection,
                tx_id=str(uuid.uuid4()),
                config=self.config,
                schema_names=self.schema_names,
                core=TransactionCore(
                    base_state={
                        key: SqliteCommittedValueRef(repo_path=repo_path, key=key)
                        for key in self._snapshot_keys(connection, repo_path)
                    }
                ),
            )
        except Exception:
            connection.close()
            lock.release()
            raise

    def recover(self, repo_locator: str, *, intent: RecoveryIntent = "auto") -> None:
        db_path, repo_path = parse_repo_locator(repo_locator)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        lock = WriterLock(sqlite_lock_path(db_path))
        lock.acquire()
        connection = connect(db_path, self.config)
        try:
            ensure_schema(connection, self.schema_names)
            recovery_root = find_recovery_root_repo_path(connection, self.schema_names, repo_path)
            recover_repo_path(connection, self.schema_names, recovery_root, intent=intent)
        finally:
            connection.close()
            lock.release()

    def debug_status(self, repo_locator: str) -> object:
        db_path, repo_path = parse_repo_locator(repo_locator)
        if not db_path.exists():
            return {"db_path": str(db_path), "repo_path": repo_path, "pending_transactions": [], "keys": []}
        connection = connect(db_path, self.config)
        try:
            ensure_schema(connection, self.schema_names)
            pending_transactions = list(
                connection.execute(
                    f'SELECT tx_id, repo_path, state FROM "{self.schema_names.tx_meta}" ORDER BY repo_path, tx_id'
                )
            )
            return {
                "db_path": str(db_path),
                "repo_path": repo_path,
                "lock_path": str(sqlite_lock_path(db_path)),
                "pending_transactions": pending_transactions,
                "keys": self._snapshot_keys(connection, repo_path),
            }
        finally:
            connection.close()

    def _snapshot_keys(self, connection: sqlite3.Connection, repo_path: str) -> list[str]:
        return [
            str(row[0])
            for row in connection.execute(
                f'SELECT key FROM "{self.schema_names.objects}" WHERE repo_path = ? ORDER BY key',
                (repo_path,),
            )
        ]


__all__ = [
    "SqliteBackend",
    "SqliteBackendTransaction",
    "SqliteCommittedValueRef",
    "SqliteConfig",
    "SqliteStagedValueRef",
    "SqliteValueRef",
]
