from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from uuid import uuid4

import attrs

from ..._errors import ValueContractError
from ..._keys import validate_logical_key

TX_DIRNAME = ".tx"
LOCK_FILENAME = "lock"
META_FILENAME = "meta.json"
META_TMP_FILENAME = "meta.json.tmp"
META_LOCK_FILENAME = "meta.lock"
WAL_FILENAME = "wal.jsonl"
STAGED_DIRNAME = "staged"
FENCE_FILENAME = "fence.json"
FENCE_TMP_FILENAME = "fence.json.tmp"


@attrs.define(frozen=True, slots=True)
class RepoLayout:
    repo_root: Path = attrs.field(converter=Path)

    @property
    def tx_root(self) -> Path:
        return self.repo_root / TX_DIRNAME

    @property
    def lock_path(self) -> Path:
        return self.tx_root / LOCK_FILENAME

    @property
    def meta_path(self) -> Path:
        return self.tx_root / META_FILENAME

    @property
    def meta_tmp_path(self) -> Path:
        return self.tx_root / META_TMP_FILENAME

    @property
    def meta_lock_path(self) -> Path:
        return self.tx_root / META_LOCK_FILENAME

    def new_meta_tmp_path(self) -> Path:
        return self.tx_root / f"{META_FILENAME}.{os.getpid()}.{uuid4()}.tmp"

    @property
    def wal_path(self) -> Path:
        return self.tx_root / WAL_FILENAME

    @property
    def fence_path(self) -> Path:
        return self.tx_root / FENCE_FILENAME

    @property
    def fence_tmp_path(self) -> Path:
        return self.tx_root / FENCE_TMP_FILENAME

    @property
    def staged_root(self) -> Path:
        return self.tx_root / STAGED_DIRNAME

    def committed_path(self, key: str) -> Path:
        return self.repo_root.joinpath(*self._key_parts(key))

    def staged_path(self, key: str) -> Path:
        return self.staged_root / quote(validate_logical_key(key), safe="")

    def snapshot_keys(self) -> list[str]:
        if not self.repo_root.exists():
            return []

        keys: list[str] = []
        for path in sorted(self.repo_root.rglob("*")):
            if not path.is_file():
                continue
            if self._is_internal_path(path):
                continue
            keys.append(path.relative_to(self.repo_root).as_posix())
        return keys

    def ensure_repo_root(self) -> None:
        self.repo_root.mkdir(parents=True, exist_ok=True)

    def ensure_tx_root(self) -> None:
        self.tx_root.mkdir(parents=True, exist_ok=True)

    def cleanup_tx_root(self) -> None:
        self.cleanup_transaction_artifacts()

    def cleanup_transaction_artifacts(self) -> None:
        if not self.tx_root.exists():
            return
        for path in (self.wal_path, self.fence_path, self.fence_tmp_path, self.meta_tmp_path):
            path.unlink(missing_ok=True)
        for path in self.tx_root.glob(f"{META_FILENAME}.*.tmp"):
            path.unlink(missing_ok=True)
        if self.staged_root.exists():
            for path in sorted(self.staged_root.rglob("*"), reverse=True):
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    path.rmdir()
            self.staged_root.rmdir()
        self._remove_empty_tx_dirs()

    def _remove_empty_tx_dirs(self) -> None:
        if not self.tx_root.exists():
            return
        for path in sorted(self.tx_root.rglob("*"), reverse=True):
            if path == self.tx_root:
                continue
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
        try:
            self.tx_root.rmdir()
        except OSError:
            pass

    def _key_parts(self, key: str) -> tuple[str, ...]:
        normalized = PurePosixPath(validate_logical_key(key))
        parts = normalized.parts
        if any(part == TX_DIRNAME for part in parts):
            raise ValueContractError(
                f"logical key {key!r} conflicts with filesystem blob internal paths"
            )
        return parts

    def _is_internal_path(self, path: Path) -> bool:
        relative = path.relative_to(self.repo_root)
        return any(part == TX_DIRNAME for part in relative.parts)


__all__ = [
    "LOCK_FILENAME",
    "FENCE_FILENAME",
    "FENCE_TMP_FILENAME",
    "META_FILENAME",
    "META_LOCK_FILENAME",
    "META_TMP_FILENAME",
    "RepoLayout",
    "STAGED_DIRNAME",
    "TX_DIRNAME",
    "WAL_FILENAME",
]
