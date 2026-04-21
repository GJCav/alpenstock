from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO, Literal

import attrs

from ..._errors import WriteConflictError

if os.name == "posix":
    import fcntl
else:
    fcntl = None

LockMode = Literal["shared", "exclusive"]


@attrs.define(slots=True)
class WriterLock:
    path: Path = attrs.field(converter=Path)
    mode: LockMode = attrs.field(default="exclusive")
    blocking: bool = attrs.field(default=False)
    _file: BinaryIO | None = attrs.field(default=None, init=False, repr=False)

    @property
    def acquired(self) -> bool:
        return self._file is not None

    def acquire(self) -> None:
        if self._file is not None:
            return
        if fcntl is None:
            raise RuntimeError("Filesystem writer locking currently supports POSIX only")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        flock_mode = fcntl.LOCK_SH if self.mode == "shared" else fcntl.LOCK_EX
        if not self.blocking:
            flock_mode |= fcntl.LOCK_NB
        try:
            fcntl.flock(lock_file.fileno(), flock_mode)
        except BlockingIOError as exc:
            lock_file.close()
            raise WriteConflictError(
                f"Another write transaction already owns repo lock {self.path} with an incompatible mode"
            ) from exc
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


@attrs.define(slots=True)
class HierarchicalLockSet:
    locks: list[WriterLock] = attrs.field(factory=list, repr=False)

    @classmethod
    def acquire(cls, lock_plan: list[tuple[Path, LockMode]]) -> HierarchicalLockSet:
        acquired: list[WriterLock] = []
        try:
            for path, mode in lock_plan:
                lock = WriterLock(path, mode=mode)
                lock.acquire()
                acquired.append(lock)
        except BaseException:
            for lock in reversed(acquired):
                lock.release()
            raise
        return cls(locks=acquired)

    @property
    def acquired(self) -> bool:
        return any(lock.acquired for lock in self.locks)

    def release(self) -> None:
        for lock in reversed(self.locks):
            lock.release()


__all__ = ["HierarchicalLockSet", "LockMode", "WriterLock"]
