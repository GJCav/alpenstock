from __future__ import annotations

from typing import Any, Protocol

from ._handles import FileHandle
from ._types import OpenMode


RepoId = str
Key = str
FenceState = dict[str, object]


class BlobBackend(Protocol):
    def child_repo_locator(self, repo_locator: RepoId, child_repo_path: str) -> RepoId: ...
    def read_committed_ref(self, repo_locator: RepoId, key: Key) -> Any | None: ...

    def open_committed_handle(
        self,
        repo_locator: RepoId,
        key: Key,
        mode: OpenMode = "r",
        *,
        encoding: str | None = None,
    ) -> FileHandle: ...

    def open_working_handle(
        self,
        tx: object,
        repo_locator: RepoId,
        key: Key,
        visible_ref: Any | None,
        mode: OpenMode,
        *,
        encoding: str | None = None,
    ) -> FileHandle: ...
    def seal_working_state(self, working_state: object) -> Any: ...
    def ensure_prepared(self, repo_locator: RepoId, overlay: dict[str, Any]) -> None: ...
    def publish_prepared(self, repo_locator: RepoId, overlay: dict[str, Any]) -> None: ...
    def discard_staged(self, repo_locator: RepoId, overlay: dict[str, Any]) -> None: ...
    def read_fence(self, repo_locator: RepoId) -> FenceState | None: ...
    def write_fence(self, repo_locator: RepoId, fence: FenceState) -> None: ...
    def clear_fence(self, repo_locator: RepoId) -> None: ...


__all__ = [
    "BlobBackend",
    "FenceState",
    "Key",
    "RepoId",
]
