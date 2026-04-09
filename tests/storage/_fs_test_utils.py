from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import attrs

from alpenstock.storage import Repo
from alpenstock.storage.backends.fs import FilesystemBackend, FilesystemBackendTransaction, RepoLayout
from alpenstock.storage._backend import BackendTransaction, RecoveryIntent
from alpenstock.storage._types import OpenMode


@attrs.define(slots=True)
class SpyFilesystemBackend:
    inner: FilesystemBackend = attrs.field(factory=FilesystemBackend, repr=False)
    begin_calls: dict[str, int] = attrs.field(factory=lambda: defaultdict(int), repr=False)

    def open_committed_handle(
        self,
        repo_locator: str,
        key: str,
        mode: OpenMode = "r",
        *,
        encoding: str | None = None,
    ):
        return self.inner.open_committed_handle(repo_locator, key, mode, encoding=encoding)

    def begin(
        self,
        repo_locator: str,
        parent_tx: BackendTransaction | None = None,
    ) -> FilesystemBackendTransaction:
        self.begin_calls[repo_locator] += 1
        return self.inner.begin(repo_locator, parent_tx=parent_tx)

    def child_repo_locator(self, repo_locator: str, child_repo_path: str) -> str:
        return self.inner.child_repo_locator(repo_locator, child_repo_path)

    def recover(self, repo_locator: str, *, intent: RecoveryIntent = "auto") -> None:
        self.inner.recover(repo_locator, intent=intent)

    def debug_status(self, repo_locator: str) -> object:
        return self.inner.debug_status(repo_locator)


def snapshot_repo_bytes(repo_root: Path) -> dict[str, bytes]:
    layout = RepoLayout(repo_root)
    return {
        key: layout.committed_path(key).read_bytes()
        for key in layout.snapshot_keys()
    }


def open_repo(repo_root: Path, *, backend: SpyFilesystemBackend | None = None) -> tuple[Repo, SpyFilesystemBackend]:
    chosen_backend = SpyFilesystemBackend() if backend is None else backend
    return Repo.open(str(repo_root), backend=chosen_backend), chosen_backend
