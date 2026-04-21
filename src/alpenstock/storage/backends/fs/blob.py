from __future__ import annotations

import json
import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, IO, cast

import attrs

from ..._blob_backend import FenceState
from ..._errors import HandleStateError, KeyNotFoundError, TransactionStateError
from ..._handles import FileHandle, _OpenSpec, validate_open_request, wrap_raw_handle
from ..._keys import validate_logical_key
from ..._tx_core import TransactionCore
from ..._types import DELETE, LogicalKey, OpenMode, OverlayEntry, Put, TransactionState
from .layout import RepoLayout
from .recovery import apply_overlay_to_repo, validate_overlay_publication
from .refs import FsCommittedValueRef, FsStagedValueRef, FsValueRef, resolve_value_ref_path


def open_path_handle(path: Path, mode: OpenMode, *, encoding: str | None = None) -> IO[str] | IO[bytes]:
    if "b" in mode:
        return path.open(mode)
    return path.open(mode, encoding="utf-8" if encoding is None else encoding)


def copy_file_contents(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, destination.open("wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)


@attrs.define(slots=True)
class FilesystemCommittedState(Mapping[str, FsValueRef]):
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
class FilesystemWritableHandle:
    tx: Any = attrs.field(repr=False)
    key: LogicalKey = attrs.field()
    path: Path = attrs.field(repr=False)
    value_ref: FsStagedValueRef = attrs.field(repr=False)
    spec: _OpenSpec = attrs.field(repr=False)
    encoding_name: str | None = attrs.field(default=None, repr=False)
    _handle: IO[str] | IO[bytes] = attrs.field(init=False, repr=False)
    _closed: bool = attrs.field(default=False, init=False, repr=False)
    _sealed: bool = attrs.field(default=False, init=False, repr=False)

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
            return cast(IO[bytes], self._handle).write(data)
        if not isinstance(data, str):
            raise TypeError("Text handles require str writes")
        return cast(IO[str], self._handle).write(data)

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

    def __enter__(self) -> FilesystemWritableHandle:
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


@attrs.define(frozen=True, slots=True)
class FilesystemBlobBackend:
    def _layout(self, repo_locator: str | RepoLayout) -> RepoLayout:
        if isinstance(repo_locator, RepoLayout):
            return repo_locator
        return RepoLayout(repo_locator)

    def child_repo_locator(self, repo_locator: str, child_repo_path: str) -> str:
        RepoLayout(repo_locator).committed_path(child_repo_path)
        return str(Path(repo_locator) / child_repo_path)

    def committed_state(self, layout: RepoLayout) -> FilesystemCommittedState:
        return FilesystemCommittedState(layout)

    def read_committed_ref(self, repo_locator: str, key: str) -> FsValueRef | None:
        layout = RepoLayout(repo_locator)
        path = layout.committed_path(key)
        return FsCommittedValueRef(validate_logical_key(key)) if path.is_file() else None

    def open_committed_handle(
        self,
        repo_locator: str,
        key: str,
        mode: OpenMode = "r",
        *,
        encoding: str | None = None,
    ) -> FileHandle:
        layout = RepoLayout(repo_locator)
        spec = validate_open_request(key, mode, encoding=encoding)
        if spec.writable:
            raise TransactionStateError(
                "Committed views are read-only; open a transaction for writable access"
            )
        path = layout.committed_path(key)
        if spec.require_exists and not path.exists():
            raise KeyNotFoundError(f"Logical key {key!r} does not exist")
        return wrap_raw_handle(key, spec, open_path_handle(path, spec.mode, encoding=encoding), encoding=encoding)

    def open_working_handle(
        self,
        tx: Any,
        repo_locator: str,
        key: str,
        visible_ref: FsValueRef | None,
        mode: OpenMode,
        *,
        encoding: str | None = None,
    ) -> FileHandle:
        layout = RepoLayout(repo_locator)
        spec = validate_open_request(key, mode, encoding=encoding)
        staged_path = layout.staged_path(key)
        value_ref = FsStagedValueRef(staged_path.relative_to(layout.tx_root).as_posix())
        return cast(
            FileHandle,
            self.open_writable_handle(
                tx=tx,
                layout=layout,
                key=validate_logical_key(key),
                source_ref=visible_ref,
                value_ref=value_ref,
                spec=spec,
                encoding=encoding,
            ),
        )

    def seal_working_state(self, working_state: object) -> FsValueRef:
        if not isinstance(working_state, FilesystemWritableHandle):
            raise TransactionStateError("Filesystem working state must be a FilesystemWritableHandle")
        return working_state.value_ref

    def open_read_handle(
        self,
        layout: RepoLayout,
        core: TransactionCore[FsValueRef],
        open_writers: Mapping[LogicalKey, FilesystemWritableHandle],
        key: str,
        spec: _OpenSpec,
        *,
        encoding: str | None = None,
    ) -> FileHandle:
        writer = open_writers.get(key)
        if writer is not None:
            writer.flush_for_external_read()
            return wrap_raw_handle(
                key,
                spec,
                open_path_handle(writer.path, spec.mode, encoding=encoding),
                encoding=encoding,
            )

        value_ref = core.read_visible_ref(key)
        if spec.require_exists and value_ref is None:
            raise KeyNotFoundError(f"Logical key {key!r} does not exist")
        if value_ref is None:
            raise KeyNotFoundError(f"Logical key {key!r} does not exist")
        path = resolve_value_ref_path(layout, value_ref)
        return wrap_raw_handle(key, spec, open_path_handle(path, spec.mode, encoding=encoding), encoding=encoding)

    def open_writable_handle(
        self,
        tx: Any,
        layout: RepoLayout,
        key: str,
        source_ref: FsValueRef | None,
        value_ref: FsStagedValueRef,
        spec: _OpenSpec,
        *,
        encoding: str | None = None,
    ) -> FilesystemWritableHandle:
        work_path = resolve_value_ref_path(layout, value_ref)
        self.initialize_work_path(layout, work_path, source_ref=source_ref, spec=spec)
        return FilesystemWritableHandle(
            tx=tx,
            key=key,
            path=work_path,
            value_ref=value_ref,
            spec=spec,
            encoding_name=encoding if not spec.binary else None,
        )

    def initialize_work_path(
        self,
        layout: RepoLayout,
        path: Path,
        *,
        source_ref: FsValueRef | None,
        spec: _OpenSpec,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if spec.truncate or source_ref is None:
            path.write_bytes(b"")
            return
        source_path = resolve_value_ref_path(layout, source_ref)
        if source_path == path:
            return
        copy_file_contents(source_path, path)

    def ensure_prepared(
        self,
        repo_locator: str | RepoLayout,
        overlay: dict[str, OverlayEntry[FsValueRef]],
        *,
        child_repo_paths: set[str] | None = None,
        allow_missing_published_puts: bool = False,
    ) -> None:
        layout = self._layout(repo_locator)
        validate_overlay_publication(
            layout,
            overlay,
            allow_missing_published_puts=allow_missing_published_puts,
            child_repo_paths=child_repo_paths,
        )

    def publish_prepared(
        self,
        repo_locator: str | RepoLayout,
        overlay: dict[str, OverlayEntry[FsValueRef]],
        *,
        child_repo_paths: set[str] | None = None,
        allow_missing_published_puts: bool = False,
    ) -> None:
        layout = self._layout(repo_locator)
        self.ensure_prepared(
            layout,
            overlay,
            child_repo_paths=child_repo_paths,
            allow_missing_published_puts=allow_missing_published_puts,
        )
        apply_overlay_to_repo(
            layout,
            overlay,
            allow_missing_published_puts=allow_missing_published_puts,
            prevalidated=True,
            child_repo_paths=child_repo_paths,
        )

    def discard_staged(self, repo_locator: str | RepoLayout, overlay: dict[str, OverlayEntry[FsValueRef]]) -> None:
        layout = self._layout(repo_locator)
        for entry in overlay.values():
            if entry is DELETE:
                continue
            assert isinstance(entry, Put)
            path = resolve_value_ref_path(layout, entry.value)
            if path.exists() and layout.tx_root in path.parents:
                path.unlink()

    def read_fence(self, repo_locator: str | RepoLayout) -> FenceState | None:
        layout = self._layout(repo_locator)
        if not layout.fence_path.exists():
            return None
        with layout.fence_path.open("r", encoding="utf-8") as handle:
            try:
                payload = json.load(handle)
            except json.JSONDecodeError as exc:
                raise TransactionStateError(f"Invalid filesystem fence JSON at {layout.fence_path}: {exc.msg}") from exc
        if not isinstance(payload, dict):
            raise TransactionStateError(f"Invalid filesystem fence payload at {layout.fence_path}")
        return cast(FenceState, payload)

    def write_fence(self, repo_locator: str | RepoLayout, fence: FenceState) -> None:
        layout = self._layout(repo_locator)
        layout.ensure_tx_root()
        tmp_path = layout.fence_tmp_path
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(fence, handle, sort_keys=True)
            handle.write("\n")
        tmp_path.replace(layout.fence_path)

    def clear_fence(self, repo_locator: str | RepoLayout) -> None:
        layout = self._layout(repo_locator)
        layout.fence_path.unlink(missing_ok=True)


__all__ = [
    "FilesystemBlobBackend",
    "FilesystemCommittedState",
    "FilesystemWritableHandle",
    "copy_file_contents",
    "open_path_handle",
]
