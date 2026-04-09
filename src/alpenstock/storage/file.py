from __future__ import annotations

from typing import TYPE_CHECKING, cast, overload

import attrs

from ._handles import BinaryFileHandle, FileHandle, ReadableBinaryMode, ReadableTextMode, TextFileHandle
from .transaction import TransactionContext
from ._types import OpenMode

if TYPE_CHECKING:
    from .repo import Repo


_READ_ONLY_MODES: frozenset[OpenMode] = frozenset({"r", "rb"})


@attrs.define(slots=True)
class FileNode:
    repo: Repo = attrs.field(repr=False)
    key: str = attrs.field()

    @overload
    def open(
        self,
        mode: ReadableTextMode = "r",
        *,
        encoding: str | None = None,
    ) -> TextFileHandle: ...

    @overload
    def open(
        self,
        mode: ReadableBinaryMode,
        *,
        encoding: None = None,
    ) -> BinaryFileHandle: ...

    def open(self, mode: OpenMode = "r", *, encoding: str | None = None) -> FileHandle:
        active_tx = self.repo.active_transaction
        if active_tx is not None:
            return cast(FileHandle, active_tx.backend_tx.open_handle(self.key, mode, encoding=encoding))

        if mode in _READ_ONLY_MODES:
            return self.repo.backend.open_committed_handle(
                self.repo.repo_locator,
                self.key,
                mode,
                encoding=encoding,
            )

        return cast(FileHandle, _ImplicitTransactionHandle.start(self.repo, self.key, mode, encoding=encoding))

    def read_bytes(self) -> bytes:
        with self.open("rb") as handle:
            payload = handle.read()
        assert isinstance(payload, bytes)
        return payload

    def write_bytes(self, data: bytes) -> None:
        with self.open("wb") as handle:
            handle.write(data)

    def read_text(self, encoding: str = "utf-8") -> str:
        with self.open("r", encoding=encoding) as handle:
            payload = handle.read()
        assert isinstance(payload, str)
        return payload

    def write_text(self, text: str, encoding: str = "utf-8") -> None:
        with self.open("w", encoding=encoding) as handle:
            handle.write(text)

    def delete(self) -> None:
        active_tx = self.repo.active_transaction
        if active_tx is not None:
            active_tx.delete(self.key)
            return

        preexisting_tx = self.repo.active_transaction
        tx = self.repo.transaction()
        if tx.parent is None:
            with tx:
                tx.delete(self.key)
            return

        tx.__enter__()
        try:
            tx.delete(self.key)
        except Exception:
            tx._cancel_child_setup_failure(remove_participant=preexisting_tx is None)
            raise
        tx.__exit__(None, None, None)

    def as_codec(self, codec: FileCodec[TValue]) -> CodecFile[TValue]:
        return CodecFile(node=self, codec=codec)


@attrs.define(slots=True)
class _ImplicitTransactionHandle:
    tx: TransactionContext = attrs.field(repr=False)
    handle: FileHandle = attrs.field(repr=False)
    _closed: bool = attrs.field(default=False, init=False, repr=False)

    @classmethod
    def start(
        cls,
        repo: Repo,
        key: str,
        mode: OpenMode,
        *,
        encoding: str | None = None,
    ) -> _ImplicitTransactionHandle:
        preexisting_tx = repo.active_transaction
        tx = repo.transaction()
        tx.__enter__()
        try:
            handle = cast(FileHandle, tx.backend_tx.open_handle(key, mode, encoding=encoding))
        except Exception as exc:
            if tx.parent is None:
                tx.__exit__(type(exc), exc, exc.__traceback__)
            else:
                tx._cancel_child_setup_failure(remove_participant=preexisting_tx is None)
            raise
        return cls(tx=tx, handle=handle)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def encoding(self) -> str | None:
        return self.handle.encoding

    def read(self, size: int = -1) -> str | bytes:
        return self.handle.read(size)

    def write(self, data: str | bytes) -> int:
        if isinstance(data, str):
            return cast(TextFileHandle, self.handle).write(data)
        return cast(BinaryFileHandle, self.handle).write(cast(bytes, data))

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.handle.seek(offset, whence)

    def tell(self) -> int:
        return self.handle.tell()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.handle.close()
        except Exception as exc:
            try:
                self.tx.__exit__(type(exc), exc, exc.__traceback__)
            finally:
                self._closed = True
            raise
        try:
            self.tx.__exit__(None, None, None)
        finally:
            self._closed = True

    def __enter__(self) -> _ImplicitTransactionHandle:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._closed:
            return None
        if exc_type is None:
            self.close()
        else:
            try:
                self.tx.__exit__(exc_type, exc, tb)
            finally:
                self._closed = True
        return None


__all__ = ["FileNode"]


from .codec import CodecFile, FileCodec, TValue
