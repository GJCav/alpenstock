from __future__ import annotations

from io import SEEK_SET
from typing import IO, Literal, Protocol, Self, TypeAlias, cast

import attrs

from ._errors import HandleStateError
from ._keys import validate_logical_key
from ._types import LogicalKey, OpenMode

_SUPPORTED_OPEN_MODES: frozenset[OpenMode] = frozenset({"r", "rb", "w", "wb", "a", "ab", "r+", "rb+"})


@attrs.define(frozen=True, slots=True)
class _OpenSpec:
    mode: OpenMode = attrs.field()
    readable: bool = attrs.field()
    writable: bool = attrs.field()
    binary: bool = attrs.field()
    append: bool = attrs.field()
    truncate: bool = attrs.field()
    require_exists: bool = attrs.field()


def _parse_mode(mode: str) -> _OpenSpec:
    if mode not in _SUPPORTED_OPEN_MODES:
        raise ValueError(f"Unsupported open mode {mode!r}")

    return _OpenSpec(
        mode=cast(OpenMode, mode),
        readable=mode in {"r", "rb", "r+", "rb+"},
        writable=mode in {"w", "wb", "a", "ab", "r+", "rb+"},
        binary="b" in mode,
        append=mode in {"a", "ab"},
        truncate=mode in {"w", "wb"},
        require_exists=mode in {"r", "rb", "r+", "rb+"},
    )


def validate_open_request(
    key: LogicalKey,
    mode: OpenMode,
    *,
    encoding: str | None = None,
) -> _OpenSpec:
    validate_logical_key(key)
    spec = _parse_mode(mode)
    if spec.binary and encoding is not None:
        raise ValueError("Binary modes do not accept an encoding")
    return spec


class TextFileHandle(Protocol):
    @property
    def closed(self) -> bool: ...

    @property
    def encoding(self) -> str: ...

    def read(self, size: int = -1) -> str: ...
    def write(self, data: str) -> int: ...
    def seek(self, offset: int, whence: int = SEEK_SET) -> int: ...
    def tell(self) -> int: ...
    def close(self) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type, exc, tb) -> None: ...


class BinaryFileHandle(Protocol):
    @property
    def closed(self) -> bool: ...

    @property
    def encoding(self) -> None: ...

    def read(self, size: int = -1) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def seek(self, offset: int, whence: int = SEEK_SET) -> int: ...
    def tell(self) -> int: ...
    def close(self) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type, exc, tb) -> None: ...


ReadableTextMode: TypeAlias = Literal["r", "w", "a", "r+"]
ReadableBinaryMode: TypeAlias = Literal["rb", "wb", "ab", "rb+"]
FileHandle: TypeAlias = TextFileHandle | BinaryFileHandle


@attrs.define(slots=True)
class _GuardedRawHandle:
    key: LogicalKey = attrs.field()
    spec: _OpenSpec = attrs.field(repr=False)
    raw_handle: IO[str] | IO[bytes] = attrs.field(repr=False)
    encoding_name: str | None = attrs.field(default=None, repr=False)

    @property
    def closed(self) -> bool:
        return self.raw_handle.closed

    @property
    def encoding(self) -> str | None:
        if self.spec.binary:
            return None
        return "utf-8" if self.encoding_name is None else self.encoding_name

    def read(self, size: int = -1) -> str | bytes:
        self._require_open()
        if not self.spec.readable:
            raise HandleStateError(f"Handle for key {self.key!r} is not open for reading")
        if self.spec.binary:
            return cast(IO[bytes], self.raw_handle).read(size)
        return cast(IO[str], self.raw_handle).read(size)

    def write(self, data: str | bytes) -> int:
        self._require_open()
        if not self.spec.writable:
            raise HandleStateError(f"Handle for key {self.key!r} is not open for writing")
        if self.spec.binary:
            if not isinstance(data, bytes):
                raise TypeError("Binary handles require bytes writes")
            return cast(IO[bytes], self.raw_handle).write(data)
        if not isinstance(data, str):
            raise TypeError("Text handles require str writes")
        return cast(IO[str], self.raw_handle).write(data)

    def seek(self, offset: int, whence: int = SEEK_SET) -> int:
        self._require_open()
        return self.raw_handle.seek(offset, whence)

    def tell(self) -> int:
        self._require_open()
        return self.raw_handle.tell()

    def close(self) -> None:
        self.raw_handle.close()

    def __enter__(self) -> _GuardedRawHandle:
        self._require_open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
        return None

    def _require_open(self) -> None:
        if self.closed:
            raise HandleStateError(f"Handle for key {self.key!r} is already closed")


def wrap_raw_handle(
    key: LogicalKey,
    spec: _OpenSpec,
    raw_handle: IO[str] | IO[bytes],
    *,
    encoding: str | None = None,
) -> FileHandle:
    return cast(
        FileHandle,
        _GuardedRawHandle(
            key=validate_logical_key(key),
            spec=spec,
            raw_handle=raw_handle,
            encoding_name=encoding,
        ),
    )


__all__ = [
    "BinaryFileHandle",
    "FileHandle",
    "ReadableBinaryMode",
    "ReadableTextMode",
    "TextFileHandle",
    "validate_open_request",
    "wrap_raw_handle",
]
