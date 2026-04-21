from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, cast

import attrs

from ..._errors import HandleStateError, KeyNotFoundError, TransactionStateError
from ..._handles import FileHandle, validate_open_request
from ..._keys import validate_logical_key
from ..._tx_core import TransactionCore
from ..._types import DELETE, LogicalKey, OpenMode, OverlayEntry, Put, TransactionState
from .layout import RepoLayout
from .locking import HierarchicalLockSet
from .blob import FilesystemBlobBackend, FilesystemCommittedState, FilesystemWritableHandle
from .recovery import (
    WalState,
    load_wal_replay,
)
from .refs import FsStagedValueRef, FsValueRef, resolve_value_ref_path


@attrs.define(slots=True)
class JsonlWalTransaction:
    layout: RepoLayout = attrs.field(repr=False)
    lock: HierarchicalLockSet = attrs.field(repr=False)
    core: TransactionCore[FsValueRef] = attrs.field(repr=False)
    blob: FilesystemBlobBackend = attrs.field(repr=False)
    journal: Any = attrs.field(repr=False)
    root_tx_id: str = attrs.field()
    coordination_root_locator: str = attrs.field()
    _children: dict[str, dict[str, bool]] = attrs.field(factory=dict, init=False, repr=False)
    _open_writers: dict[LogicalKey, FilesystemWritableHandle] = attrs.field(factory=dict, init=False, repr=False)
    _tx_root_initialized: bool = attrs.field(default=False, init=False, repr=False)
    _wal_initialized: bool = attrs.field(default=False, init=False, repr=False)
    _coordinated_parent_locator: str | None = attrs.field(default=None, init=False, repr=False)
    _root_commit_authorized: bool = attrs.field(default=False, init=False, repr=False)
    _publication_started: bool = attrs.field(default=False, init=False, repr=False)
    _released: bool = attrs.field(default=False, init=False, repr=False)

    @property
    def has_open_writers(self) -> bool:
        return bool(self._open_writers)

    @property
    def is_prepared(self) -> bool:
        return self.core.state is TransactionState.PREPARED

    def open_handle(self, key: str, mode: OpenMode = "r", *, encoding: str | None = None) -> FileHandle:
        key = validate_logical_key(key)
        self.layout.committed_path(key)
        spec = validate_open_request(key, mode, encoding=encoding)

        if spec.writable:
            return cast(FileHandle, self._open_writable_handle(key, spec, encoding=encoding))
        return self._open_read_handle(key, spec, encoding=encoding)

    def delete(self, key: str) -> None:
        key = validate_logical_key(key)
        self.layout.committed_path(key)
        self._assert_key_outside_child_boundaries(key)
        if self.core.state is not TransactionState.OPEN or key in self._open_writers:
            self.core.delete(key)
            return
        self._ensure_open_metadata()
        if self._wal_initialized:
            self._append_overlay_record(key, DELETE)
        self.core.delete(key)

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
        child_repo_path = validate_logical_key(child_repo_path)
        self.layout.committed_path(child_repo_path)
        self._assert_child_repo_boundary_unused(child_repo_path)
        current_state = self._children.get(child_repo_path)
        next_state = dict(current_state) if current_state is not None else {"prepared": False}
        self._ensure_open_metadata()
        self._ensure_wal_started(state="open")
        self._append_child_state_record(child_repo_path, next_state)
        self._wal_initialized = True
        self._children[child_repo_path] = next_state

    def unregister_child_repo(self, child_repo_path: str) -> None:
        child_repo_path = validate_logical_key(child_repo_path)
        self.layout.committed_path(child_repo_path)
        if self._wal_initialized and child_repo_path in self._children:
            self._append_child_unenrolled_record(child_repo_path)
        self._children.pop(child_repo_path, None)
        self._cleanup_metadata_if_empty()

    def mark_child_open(self, child_repo_path: str) -> None:
        child_repo_path = validate_logical_key(child_repo_path)
        self.layout.committed_path(child_repo_path)
        current_state = self._children.get(child_repo_path, {"prepared": False})
        next_state = dict(current_state)
        next_state["prepared"] = False
        self._ensure_wal_started(state="open")
        self._append_child_state_record(child_repo_path, next_state)
        self._wal_initialized = True
        self._children[child_repo_path] = next_state

    def mark_child_prepared(self, child_repo_path: str) -> None:
        child_repo_path = validate_logical_key(child_repo_path)
        self.layout.committed_path(child_repo_path)
        current_state = self._children.get(child_repo_path, {"prepared": False})
        next_state = dict(current_state)
        next_state["prepared"] = True
        self._ensure_wal_started(state="open")
        self._append_child_state_record(child_repo_path, next_state)
        self._wal_initialized = True
        self._children[child_repo_path] = next_state

    def prepare(self) -> None:
        self._validate_child_boundaries(self.core.overlay)
        self.core.prepare()
        prepared_overlay = self._prepared_overlay()
        self.blob.ensure_prepared(
            self.layout,
            prepared_overlay,
            child_repo_paths=set(self._children),
        )
        wal_already_initialized = self._wal_initialized
        self._ensure_wal_started(state="prepared")
        if wal_already_initialized:
            self.journal.mark_state(self.layout, "prepared")

    def authorize_root_publication(self) -> None:
        self._root_commit_authorized = True

    def reopen_prepared(self) -> None:
        self.core.reopen_prepared()
        if self._wal_initialized:
            self.journal.mark_state(self.layout, "open")

    def mark_committing(self) -> None:
        if self._coordinated_parent_locator is not None and not self._root_commit_authorized:
            raise TransactionStateError(
                "Coordinated child publication requires root commit authorization"
            )
        wal_already_initialized = self._wal_initialized
        self._ensure_wal_started(state="committing")
        if wal_already_initialized:
            self.journal.mark_state(self.layout, "committing")
        self._publication_started = True

    def publish_prepared(self) -> None:
        prepared_overlay = self._prepared_overlay()
        self.blob.publish_prepared(
            self.layout,
            prepared_overlay,
            child_repo_paths=set(self._children),
        )
        self.core.commit()

    def clear_committed(self) -> None:
        try:
            self._cleanup_transaction_artifacts()
        finally:
            self._release_lock()

    def commit(self) -> None:
        self.mark_committing()
        try:
            self.publish_prepared()
        except BaseException:
            self._release_lock()
            raise
        self.clear_committed()

    def _prepared_overlay(self) -> dict[str, OverlayEntry[FsValueRef]]:
        prepared_overlay = self.core.prepared_overlay
        if prepared_overlay is None:
            raise TransactionStateError("JSONL WAL transaction commit() requires a prepared overlay")
        return prepared_overlay

    def rollback(self) -> None:
        if self._publication_started:
            raise TransactionStateError(
                "rollback() is invalid once commit publication has started; finish commit or run recover()"
            )
        try:
            self.core.rollback()
            self._cleanup_transaction_artifacts()
        finally:
            self._release_lock()

    def detach_for_recovery(self) -> None:
        self.discard_open_writers()
        self._release_lock()

    def _open_read_handle(self, key: str, spec, *, encoding: str | None = None) -> FileHandle:
        return self.blob.open_read_handle(
            self.layout,
            self.core,
            self._open_writers,
            key,
            spec,
            encoding=encoding,
        )

    def _open_writable_handle(self, key: str, spec, *, encoding: str | None = None) -> FilesystemWritableHandle:
        self._assert_key_outside_child_boundaries(key)
        source_ref = self.core.read_visible_ref(key)
        if spec.require_exists and source_ref is None:
            raise KeyNotFoundError(f"Logical key {key!r} does not exist")

        self.core.register_writable_handle(key)
        handle: FilesystemWritableHandle | None = None
        value_ref = self._new_staged_value_ref(key)
        work_path = resolve_value_ref_path(self.layout, value_ref)
        try:
            handle = self.blob.open_writable_handle(
                tx=self,
                layout=self.layout,
                key=key,
                source_ref=source_ref,
                value_ref=value_ref,
                spec=spec,
                encoding=encoding,
            )
            self._ensure_open_metadata()
        except Exception:
            if handle is None:
                self.core.unregister_writable_handle(key)
                if work_path.exists():
                    work_path.unlink()
            else:
                handle._discard()
            raise

        self._open_writers[key] = handle
        return handle

    def _new_staged_value_ref(self, key: str) -> FsStagedValueRef:
        self._ensure_tx_root()
        path = self.layout.staged_path(key)
        relpath = path.relative_to(self.layout.tx_root).as_posix()
        return FsStagedValueRef(relpath)

    def _seal_writer(self, handle: FilesystemWritableHandle) -> None:
        try:
            if self._wal_initialized:
                self._append_overlay_record(handle.key, Put(handle.value_ref))
            self.core.put_ref(handle.key, handle.value_ref)
        finally:
            self._open_writers.pop(handle.key, None)
            self.core.unregister_writable_handle(handle.key)

    def _discard_writer(self, handle: FilesystemWritableHandle) -> None:
        self._open_writers.pop(handle.key, None)
        self.core.unregister_writable_handle(handle.key)
        if handle.path.exists():
            handle.path.unlink()
        self._cleanup_metadata_if_empty()

    def _ensure_open_metadata(self) -> None:
        self._ensure_tx_root()

    def _ensure_tx_root(self) -> None:
        if self._tx_root_initialized:
            return
        self.layout.ensure_tx_root()
        self._tx_root_initialized = True

    def _ensure_wal_started(self, *, state: WalState) -> None:
        if self._wal_initialized:
            return
        overlay = self.core.prepared_overlay if state in {"prepared", "committing"} else self.core.overlay
        if overlay is None:
            overlay = {}
        if not overlay and not self._children:
            return
        self._ensure_tx_root()
        for key, entry in sorted(overlay.items()):
            self._append_overlay_record(key, entry)
        for child_repo_path in sorted(self._children):
            self._append_child_record(child_repo_path)
        self.journal.mark_state(self.layout, state)
        self._wal_initialized = True

    def _mark_coordinated_child(self, parent_repo_locator: str) -> None:
        self._ensure_tx_root()
        self.journal.mark_coordinated_child(self.layout, parent_repo_locator)
        self._coordinated_parent_locator = parent_repo_locator
        self._wal_initialized = True

    def _append_overlay_record(self, key: str, entry: OverlayEntry[FsValueRef]) -> None:
        self._ensure_tx_root()
        self.journal.append_overlay(self.layout, key, entry)

    def _append_child_record(self, child_repo_path: str) -> None:
        self._append_child_state_record(child_repo_path, self._children[child_repo_path])

    def _append_child_state_record(self, child_repo_path: str, child_state: Mapping[str, bool]) -> None:
        self._ensure_tx_root()
        self.journal.enroll_child(self.layout, child_repo_path, child_state)

    def _append_child_unenrolled_record(self, child_repo_path: str) -> None:
        self._ensure_tx_root()
        self.journal.unenroll_child(self.layout, child_repo_path)

    def _cleanup_transaction_artifacts(self) -> None:
        self.blob.clear_fence(self.layout)
        self.layout.cleanup_tx_root()
        self._tx_root_initialized = False
        self._wal_initialized = False
        self._coordinated_parent_locator = None
        self._root_commit_authorized = False
        self._children.clear()

    def _cleanup_metadata_if_empty(self) -> None:
        # The v0.2 blob-side fence stays durable for the whole active transaction,
        # even before the first WAL record is needed.
        return

    def _assert_key_outside_child_boundaries(self, key: str) -> None:
        key_path = PurePosixPath(key)
        for child_repo_path in self._children:
            child_path = PurePosixPath(child_repo_path)
            if key_path == child_path or child_path in key_path.parents or key_path in child_path.parents:
                raise TransactionStateError(
                    f"Parent transaction key {key!r} overlaps enrolled child repo {child_repo_path!r}"
                )

    def _assert_child_repo_boundary_unused(self, child_repo_path: str) -> None:
        child_path = PurePosixPath(child_repo_path)
        for key in self.core.overlay:
            key_path = PurePosixPath(key)
            if key_path == child_path or child_path in key_path.parents or key_path in child_path.parents:
                raise TransactionStateError(
                    f"Cannot enroll child repo {child_repo_path!r}; parent overlay already overlaps that path"
                )
        for key in self._open_writers:
            key_path = PurePosixPath(key)
            if key_path == child_path or child_path in key_path.parents or key_path in child_path.parents:
                raise TransactionStateError(
                    f"Cannot enroll child repo {child_repo_path!r}; parent writable handle already overlaps that path"
                )

    def _validate_child_boundaries(self, overlay: dict[str, OverlayEntry[FsValueRef]]) -> None:
        for key in overlay:
            self._assert_key_outside_child_boundaries(key)

    def _release_lock(self) -> None:
        if self._released:
            return
        self.lock.release()
        self._released = True

    def _write_active_fence(self, *, role: str, parent_repo_locator: str | None = None) -> None:
        self._ensure_tx_root()
        self.blob.write_fence(
            self.layout,
            {
                "journal_backend": self.journal.identity,
                "parent_repo_locator": parent_repo_locator,
                "repo_locator": str(self.layout.repo_root),
                "role": role,
                "root_tx_id": self.root_tx_id,
                "state": "active",
                "recovery_required": True,
                "version": 1,
            },
        )

__all__ = ["JsonlWalTransaction"]
