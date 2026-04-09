from __future__ import annotations

import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import IO, cast

import attrs

from ..._backend import BackendTransaction, RecoveryIntent
from ..._errors import HandleStateError, KeyNotFoundError, TransactionStateError
from ..._handles import FileHandle, _OpenSpec, validate_open_request, wrap_raw_handle
from ..._keys import validate_logical_key
from ..._tx_core import TransactionCore
from ..._types import DELETE, LogicalKey, OpenMode, OverlayEntry, Put, TransactionState
from .layout import RepoLayout
from .locking import WriterLock
from .recovery import (
    WalState,
    append_wal_record,
    apply_overlay_to_repo,
    load_wal_replay,
    validate_overlay_publication,
    wal_child_record,
    wal_child_unenrolled_record,
    wal_coordinated_child_record,
    wal_overlay_record,
    wal_state_record,
)
from .refs import FsCommittedValueRef, FsStagedValueRef, FsValueRef, resolve_value_ref_path


def _open_path_handle(path: Path, mode: OpenMode, *, encoding: str | None = None) -> IO[str] | IO[bytes]:
    if "b" in mode:
        return path.open(mode)
    return path.open(mode, encoding="utf-8" if encoding is None else encoding)


def _copy_file_contents(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, destination.open("wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)


@attrs.define(slots=True)
class _FilesystemCommittedState(Mapping[str, FsValueRef]):
    layout: RepoLayout = attrs.field(repr=False)
    _cache: dict[str, FsValueRef | None] = attrs.field(factory=dict, init=False, repr=False)
    _snapshot: dict[str, FsValueRef] | None = attrs.field(default=None, init=False, repr=False)

    def __getitem__(self, key: str) -> FsValueRef:
        normalized_key = validate_logical_key(key)
        if self._snapshot is not None and normalized_key in self._snapshot:
            return self._snapshot[normalized_key]
        value = self._lookup(normalized_key)
        if value is None:
            raise KeyError(normalized_key)
        return value

    def __iter__(self) -> Iterator[str]:
        return iter(self._materialize())

    def __len__(self) -> int:
        return len(self._materialize())

    def _lookup(self, key: str) -> FsValueRef | None:
        normalized_key = validate_logical_key(key)
        cached = self._cache.get(normalized_key, attrs.NOTHING)
        if cached is not attrs.NOTHING:
            return None if cached is None else cached
        path = self.layout.committed_path(normalized_key)
        value = FsCommittedValueRef(normalized_key) if path.is_file() else None
        self._cache[normalized_key] = value
        return value

    def _materialize(self) -> dict[str, FsValueRef]:
        if self._snapshot is None:
            self._snapshot = {
                key: FsCommittedValueRef(key)
                for key in self.layout.snapshot_keys()
            }
            self._cache = dict(self._snapshot)
        return self._snapshot


@attrs.define(slots=True)
class _FilesystemWritableHandle:
    tx: FilesystemBackendTransaction = attrs.field(repr=False)
    key: LogicalKey = attrs.field()
    path: Path = attrs.field(repr=False)
    value_ref: FsStagedValueRef = attrs.field(repr=False)
    spec: _OpenSpec = attrs.field(repr=False)
    encoding_name: str | None = attrs.field(default=None, repr=False)
    _handle: IO[str] | IO[bytes] = attrs.field(init=False, repr=False)
    _closed: bool = attrs.field(default=False, init=False, repr=False)
    _sealed: bool = attrs.field(default=False, init=False, repr=False)

    def __attrs_post_init__(self) -> None:
        self._handle = _open_path_handle(self.path, self.spec.mode, encoding=self.encoding_name)

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
            binary_handle = cast(IO[bytes], self._handle)
            written = binary_handle.write(data)
        else:
            if not isinstance(data, str):
                raise TypeError("Text handles require str writes")
            text_handle = cast(IO[str], self._handle)
            written = text_handle.write(data)
        return written

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
            self._handle.close()
            self.tx._seal_writer(self)
            self._sealed = True
        except Exception:
            if not self._sealed:
                self.tx._discard_writer(self)
            self._finalize_close()
            raise

        self._finalize_close()

    def __enter__(self) -> _FilesystemWritableHandle:
        self._require_open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
        else:
            self._discard()
        return None

    def _require_open(self) -> None:
        if self._closed:
            raise HandleStateError(f"Handle for key {self.key!r} is already closed")

    def _discard(self) -> None:
        if self._closed:
            return
        self.tx._discard_writer(self)
        self._finalize_close()

    def _finalize_close(self) -> None:
        self._closed = True
        if not self._handle.closed:
            self._handle.close()


@attrs.define(slots=True)
class FilesystemBackendTransaction:
    layout: RepoLayout = attrs.field(repr=False)
    lock: WriterLock = attrs.field(repr=False)
    core: TransactionCore[FsValueRef] = attrs.field(repr=False)
    _children: dict[str, dict[str, bool]] = attrs.field(factory=dict, init=False, repr=False)
    _open_writers: dict[LogicalKey, _FilesystemWritableHandle] = attrs.field(factory=dict, init=False, repr=False)
    _next_session_id: int = attrs.field(default=1, init=False, repr=False)
    _tx_root_initialized: bool = attrs.field(default=False, init=False, repr=False)
    _wal_initialized: bool = attrs.field(default=False, init=False, repr=False)
    _coordinated_parent_locator: str | None = attrs.field(default=None, init=False, repr=False)
    _publication_started: bool = attrs.field(default=False, init=False, repr=False)
    _released: bool = attrs.field(default=False, init=False, repr=False)

    @property
    def has_open_writers(self) -> bool:
        return bool(self._open_writers)

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
        if current_state is not None and current_state["committed"]:
            raise TransactionStateError(f"Child repo {child_repo_path!r} is already committed")
        next_state = dict(current_state) if current_state is not None else {"prepared": False, "committed": False}
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

    def mark_child_prepared(self, child_repo_path: str) -> None:
        child_repo_path = validate_logical_key(child_repo_path)
        self.layout.committed_path(child_repo_path)
        current_state = self._children.get(child_repo_path, {"prepared": False, "committed": False})
        next_state = dict(current_state)
        next_state["prepared"] = True
        self._ensure_wal_started(state="open")
        self._append_child_state_record(child_repo_path, next_state)
        self._wal_initialized = True
        self._children[child_repo_path] = next_state

    def mark_child_committed(self, child_repo_path: str) -> None:
        child_repo_path = validate_logical_key(child_repo_path)
        self.layout.committed_path(child_repo_path)
        current_state = self._children.get(child_repo_path, {"prepared": False, "committed": False})
        next_state = dict(current_state)
        next_state["prepared"] = True
        next_state["committed"] = True
        self._ensure_wal_started(state="open")
        self._append_child_state_record(child_repo_path, next_state)
        self._wal_initialized = True
        self._children[child_repo_path] = next_state

    def prepare(self) -> None:
        self._validate_child_boundaries(self.core.overlay)
        self.core.prepare()
        wal_already_initialized = self._wal_initialized
        self._ensure_wal_started(state="prepared")
        if wal_already_initialized:
            append_wal_record(self.layout, wal_state_record("prepared"))

    def commit(self) -> None:
        prepared_overlay = self.core.prepared_overlay
        if prepared_overlay is None:
            raise TransactionStateError("Filesystem backend commit() requires a prepared overlay")

        validate_overlay_publication(
            self.layout,
            prepared_overlay,
            allow_missing_published_puts=False,
            child_repo_paths=set(self._children),
        )
        self._publication_started = True
        try:
            apply_overlay_to_repo(
                self.layout,
                prepared_overlay,
                allow_missing_published_puts=False,
                prevalidated=True,
                child_repo_paths=set(self._children),
            )
            self.core.commit()
            self._cleanup_transaction_artifacts()
        finally:
            self._release_lock()

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

    def _open_read_handle(self, key: str, spec: _OpenSpec, *, encoding: str | None = None) -> FileHandle:
        writer = self._open_writers.get(key)
        if writer is not None:
            writer.flush_for_external_read()
            return wrap_raw_handle(
                key,
                spec,
                _open_path_handle(writer.path, spec.mode, encoding=encoding),
                encoding=encoding,
            )

        value_ref = self.core.read_visible_ref(key)
        if spec.require_exists and value_ref is None:
            raise KeyNotFoundError(f"Logical key {key!r} does not exist")
        if value_ref is None:
            raise KeyNotFoundError(f"Logical key {key!r} does not exist")
        path = resolve_value_ref_path(self.layout, value_ref)
        return wrap_raw_handle(key, spec, _open_path_handle(path, spec.mode, encoding=encoding), encoding=encoding)

    def _open_writable_handle(self, key: str, spec: _OpenSpec, *, encoding: str | None = None) -> _FilesystemWritableHandle:
        self._assert_key_outside_child_boundaries(key)
        source_ref = self.core.read_visible_ref(key)
        if spec.require_exists and source_ref is None:
            raise KeyNotFoundError(f"Logical key {key!r} does not exist")

        self.core.register_writable_handle(key)
        handle: _FilesystemWritableHandle | None = None
        value_ref = self._new_staged_value_ref(key)
        work_path = resolve_value_ref_path(self.layout, value_ref)
        try:
            self._initialize_work_path(work_path, source_ref=source_ref, spec=spec)
            handle = _FilesystemWritableHandle(
                tx=self,
                key=key,
                path=work_path,
                value_ref=value_ref,
                spec=spec,
                encoding_name=encoding if not spec.binary else None,
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

    def _initialize_work_path(self, path: Path, *, source_ref: FsValueRef | None, spec: _OpenSpec) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if spec.truncate or source_ref is None:
            path.write_bytes(b"")
            return
        source_path = resolve_value_ref_path(self.layout, source_ref)
        _copy_file_contents(source_path, path)

    def _new_staged_value_ref(self, key: str) -> FsStagedValueRef:
        self._ensure_tx_root()
        session_id = self._next_session_id
        self._next_session_id += 1
        path = self.layout.staged_session_path(session_id, key)
        relpath = path.relative_to(self.layout.tx_root).as_posix()
        return FsStagedValueRef(relpath)

    def _seal_writer(self, handle: _FilesystemWritableHandle) -> None:
        try:
            if self._wal_initialized:
                self._append_overlay_record(handle.key, Put(handle.value_ref))
            self.core.put_ref(handle.key, handle.value_ref)
        finally:
            self._open_writers.pop(handle.key, None)
            self.core.unregister_writable_handle(handle.key)

    def _discard_writer(self, handle: _FilesystemWritableHandle) -> None:
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
        overlay = self.core.prepared_overlay if state == "prepared" else self.core.overlay
        if overlay is None:
            overlay = {}
        if not overlay and not self._children:
            return
        self._ensure_tx_root()
        for key, entry in sorted(overlay.items()):
            self._append_overlay_record(key, entry)
        for child_repo_path in sorted(self._children):
            self._append_child_record(child_repo_path)
        append_wal_record(self.layout, wal_state_record(state))
        self._wal_initialized = True

    def _mark_coordinated_child(self, parent_repo_locator: str) -> None:
        self._ensure_tx_root()
        append_wal_record(self.layout, wal_coordinated_child_record(parent_repo_locator))
        self._coordinated_parent_locator = parent_repo_locator
        self._wal_initialized = True

    def _append_overlay_record(self, key: str, entry: OverlayEntry[FsValueRef]) -> None:
        self._ensure_tx_root()
        append_wal_record(self.layout, wal_overlay_record(self.layout, key, entry))

    def _append_child_record(self, child_repo_path: str) -> None:
        self._append_child_state_record(child_repo_path, self._children[child_repo_path])

    def _append_child_state_record(self, child_repo_path: str, child_state: Mapping[str, bool]) -> None:
        self._ensure_tx_root()
        append_wal_record(
            self.layout,
            wal_child_record(
                self.layout,
                child_repo_path,
                prepared=child_state["prepared"],
                committed=child_state["committed"],
            ),
        )

    def _append_child_unenrolled_record(self, child_repo_path: str) -> None:
        self._ensure_tx_root()
        append_wal_record(self.layout, wal_child_unenrolled_record(self.layout, child_repo_path))

    def _cleanup_transaction_artifacts(self) -> None:
        self.layout.cleanup_tx_root()
        self._tx_root_initialized = False
        self._wal_initialized = False
        self._coordinated_parent_locator = None
        self._children.clear()

    def _cleanup_metadata_if_empty(self) -> None:
        if (
            self.core.state is TransactionState.OPEN
            and self._tx_root_initialized
            and not self._open_writers
            and not self.core.overlay
            and not self._children
        ):
            self._cleanup_transaction_artifacts()

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


@attrs.define(frozen=True, slots=True)
class FilesystemBackend:
    def child_repo_locator(self, repo_locator: str, child_repo_path: str) -> str:
        RepoLayout(repo_locator).committed_path(child_repo_path)
        return str(Path(repo_locator) / child_repo_path)

    def open_committed_handle(
        self,
        repo_locator: str,
        key: str,
        mode: OpenMode = "r",
        *,
        encoding: str | None = None,
    ) -> FileHandle:
        spec = validate_open_request(key, mode, encoding=encoding)
        if spec.writable:
            raise TransactionStateError(
                "Committed views are read-only; open a transaction for writable access"
            )
        layout = RepoLayout(repo_locator)
        path = layout.committed_path(key)
        if spec.require_exists and not path.exists():
            raise KeyNotFoundError(f"Logical key {key!r} does not exist")
        return wrap_raw_handle(key, spec, _open_path_handle(path, spec.mode, encoding=encoding), encoding=encoding)

    def begin(
        self,
        repo_locator: str,
        parent_tx: BackendTransaction | None = None,
    ) -> FilesystemBackendTransaction:
        layout = RepoLayout(repo_locator)
        layout.ensure_repo_root()
        if parent_tx is None:
            self._assert_no_pending_ancestor_coordination(layout)
        lock = WriterLock(layout.lock_path)
        lock.acquire()
        if layout.tx_root.exists():
            lock.release()
            raise TransactionStateError(
                "Cannot begin a filesystem transaction while pending recovery state exists; run recover() first"
            )
        tx = FilesystemBackendTransaction(
            layout=layout,
            lock=lock,
            core=TransactionCore(base_state=_FilesystemCommittedState(layout)),
        )
        if parent_tx is not None:
            if not isinstance(parent_tx, FilesystemBackendTransaction):
                tx.rollback()
                raise TransactionStateError("Filesystem nested repo transactions require a filesystem parent transaction")
            try:
                child_repo_path = Path(repo_locator).relative_to(parent_tx.layout.repo_root).as_posix()
            except ValueError as exc:
                tx.rollback()
                raise TransactionStateError(
                    f"Child repo {repo_locator!r} must be inside parent repo {str(parent_tx.layout.repo_root)!r}"
                ) from exc
            try:
                parent_tx.register_child_repo(child_repo_path)
                tx._mark_coordinated_child(str(parent_tx.layout.repo_root))
            except BaseException as original_error:
                try:
                    parent_tx.unregister_child_repo(child_repo_path)
                except BaseException as cleanup_error:
                    original_error.add_note(f"Additional child enrollment cleanup error: {cleanup_error!r}")
                try:
                    tx.rollback()
                except BaseException as cleanup_error:
                    original_error.add_note(f"Additional child transaction rollback error: {cleanup_error!r}")
                raise
        return tx

    def recover(self, repo_locator: str, *, intent: RecoveryIntent = "auto") -> None:
        self._recover(repo_locator, intent=intent, allow_coordinated_child=False)

    def _recover(self, repo_locator: str, *, intent: RecoveryIntent, allow_coordinated_child: bool) -> None:
        layout = RepoLayout(repo_locator)
        if not layout.tx_root.exists():
            return

        layout.ensure_repo_root()
        lock = WriterLock(layout.lock_path)
        lock.acquire()
        try:
            if not layout.tx_root.exists():
                return
            if not layout.wal_path.exists():
                layout.cleanup_tx_root()
                return
            replay = load_wal_replay(layout)
            if replay.parent_repo_locator is not None and not allow_coordinated_child:
                raise TransactionStateError(
                    f"Cannot recover coordinated filesystem child repo {repo_locator!r} directly; "
                    f"recover coordinator repo {replay.parent_repo_locator!r}"
                )
            if intent == "abort" or replay.state == "open":
                for child_repo_path, child_state in replay.children.items():
                    self._recover_child_participant(repo_locator, child_repo_path, child_state, intent="abort")
                layout.cleanup_tx_root()
                return
            for child_repo_path, child_state in replay.children.items():
                if child_state.get("committed", False):
                    continue
                self._recover_child_participant(repo_locator, child_repo_path, child_state, intent="auto")
            apply_overlay_to_repo(
                layout,
                replay.overlay,
                allow_missing_published_puts=True,
                child_repo_paths=set(replay.children),
            )
            layout.cleanup_tx_root()
        finally:
            lock.release()

    def _recover_child_participant(
        self,
        parent_repo_locator: str,
        child_repo_path: str,
        child_state: Mapping[str, bool],
        *,
        intent: RecoveryIntent,
    ) -> None:
        child_locator = self.child_repo_locator(parent_repo_locator, child_repo_path)
        child_layout = RepoLayout(child_locator)
        if not child_layout.tx_root.exists():
            return
        self._recover(child_locator, intent=intent, allow_coordinated_child=True)

    def _assert_no_pending_ancestor_coordination(self, layout: RepoLayout) -> None:
        repo_root = layout.repo_root
        ancestor = repo_root.parent
        while ancestor != ancestor.parent:
            ancestor_layout = RepoLayout(ancestor)
            if ancestor_layout.wal_path.exists():
                replay = load_wal_replay(ancestor_layout)
                repo_path = PurePosixPath(repo_root.relative_to(ancestor_layout.repo_root).as_posix())
                for child_repo_path in replay.children:
                    child_path = PurePosixPath(child_repo_path)
                    if repo_path == child_path or child_path in repo_path.parents:
                        raise TransactionStateError(
                            f"Cannot begin filesystem transaction at {str(repo_root)!r}; "
                            f"ancestor coordinator {str(ancestor_layout.repo_root)!r} has pending child "
                            f"coordination for {child_repo_path!r}. Recover the coordinator repo first."
                        )
            ancestor = ancestor.parent

    def debug_status(self, repo_locator: str) -> object:
        layout = RepoLayout(repo_locator)
        return {
            "repo_root": str(layout.repo_root),
            "tx_root_exists": layout.tx_root.exists(),
            "wal_exists": layout.wal_path.exists(),
            "lock_exists": layout.lock_path.exists(),
            "keys": sorted(layout.snapshot_keys()),
        }


__all__ = ["FilesystemBackend", "FilesystemBackendTransaction"]
