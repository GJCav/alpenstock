from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

import attrs

from ..._errors import WriteConflictError

if os.name == "posix":
    import fcntl
else:
    fcntl = None


@attrs.define(slots=True)
class WriterLock:
    path: Path = attrs.field(converter=Path)
    _file: BinaryIO | None = attrs.field(default=None, init=False, repr=False)

    @property
    def acquired(self) -> bool:
        return self._file is not None

    def acquire(self) -> None:
        if self._file is not None:
            return
        if fcntl is None:
            raise RuntimeError("Filesystem backend writer locking currently supports POSIX only")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise WriteConflictError(f"Another write transaction already owns repo lock {self.path}") from exc
        except Exception:
            lock_file.close()
            raise
        self._file = lock_file

    def release(self) -> None:
        if self._file is None:
            return
        lock_file = self._file
        self._file = None
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


__all__ = ["WriterLock"]
