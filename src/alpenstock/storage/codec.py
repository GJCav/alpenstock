from __future__ import annotations

from typing import Generic, Protocol, TypeVar, cast, overload

import attrs

from ._handles import BinaryFileHandle, FileHandle, ReadableBinaryMode, ReadableTextMode, TextFileHandle
from ._types import OpenMode

TValue = TypeVar("TValue")


class FileCodec(Protocol[TValue]):
    def loads(self, payload: bytes) -> TValue: ...
    def dumps(self, value: TValue) -> bytes: ...


@attrs.define(frozen=True, slots=True)
class CodecFile(Generic[TValue]):
    node: "FileNode" = attrs.field(repr=False)
    codec: FileCodec[TValue] = attrs.field(repr=False)

    def read(self) -> TValue:
        return self.codec.loads(self.node.read_bytes())

    def write(self, value: TValue) -> None:
        self.node.write_bytes(self.codec.dumps(value))

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
        if "b" in mode:
            return self.node.open(cast(ReadableBinaryMode, mode), encoding=None)
        return self.node.open(cast(ReadableTextMode, mode), encoding=encoding)

    def delete(self) -> None:
        self.node.delete()


from .file import FileNode

__all__ = ["CodecFile", "FileCodec", "TValue"]
