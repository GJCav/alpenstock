from __future__ import annotations

from pathlib import Path, PurePosixPath

import attrs

from ..._errors import ValueContractError
from ..._keys import validate_logical_key

TX_DIRNAME = ".repo_tx"
LOCK_FILENAME = ".repo_tx.lock"
WAL_FILENAME = "wal.jsonl"
STAGED_DIRNAME = "staged"


@attrs.define(frozen=True, slots=True)
class RepoLayout:
    repo_root: Path = attrs.field(converter=Path)

    @property
    def tx_root(self) -> Path:
        return self.repo_root / TX_DIRNAME

    @property
    def lock_path(self) -> Path:
        return self.repo_root / LOCK_FILENAME

    @property
    def wal_path(self) -> Path:
        return self.tx_root / WAL_FILENAME

    @property
    def staged_root(self) -> Path:
        return self.tx_root / STAGED_DIRNAME

    def committed_path(self, key: str) -> Path:
        return self.repo_root.joinpath(*self._key_parts(key))

    def staged_path(self, key: str) -> Path:
        return self.staged_root.joinpath(*self._key_parts(key))

    def staged_session_path(self, session_id: int, key: str) -> Path:
        return self.staged_root / f"{session_id:08d}" / Path(*self._key_parts(key))

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
        if not self.tx_root.exists():
            return
        for path in sorted(self.tx_root.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                path.rmdir()
        self.tx_root.rmdir()

    def _key_parts(self, key: str) -> tuple[str, ...]:
        normalized = PurePosixPath(validate_logical_key(key))
        parts = normalized.parts
        if any(part in {TX_DIRNAME, LOCK_FILENAME} for part in parts):
            raise ValueContractError(
                f"logical key {key!r} conflicts with filesystem backend internal paths"
            )
        return parts

    def _is_internal_path(self, path: Path) -> bool:
        relative = path.relative_to(self.repo_root)
        return any(part in {LOCK_FILENAME, TX_DIRNAME} for part in relative.parts)


__all__ = [
    "LOCK_FILENAME",
    "RepoLayout",
    "STAGED_DIRNAME",
    "TX_DIRNAME",
    "WAL_FILENAME",
]
