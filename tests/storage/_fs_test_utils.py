from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import attrs

from alpenstock.storage import Repo
from alpenstock.storage._blob_backend import BlobBackend
from alpenstock.storage.backends.fs import FilesystemBlobBackend, JsonlWalJournalBackend, JsonlWalTransaction, RepoLayout
from alpenstock.storage._journal_backend import JournalTransaction, RecoveryIntent
from alpenstock.storage._types import OpenMode, OverlayEntry
from alpenstock.storage.backends.fs.recovery import WalState
from alpenstock.storage.backends.fs.refs import FsValueRef


@attrs.define(slots=True)
class FsRuntime:
    blob: FilesystemBlobBackend = attrs.field(factory=FilesystemBlobBackend, repr=False)
    journal: JsonlWalJournalBackend = attrs.field(factory=JsonlWalJournalBackend, repr=False)
    begin_calls: dict[str, int] = attrs.field(factory=lambda: defaultdict(int), repr=False)

    @property
    def identity(self) -> str:
        return self.journal.identity

    def prepare_repo_open(
        self,
        repo_locator: str,
        blob_backend: BlobBackend,
        *,
        parent_repo_locator: str | None = None,
        child_repo_path: str | None = None,
    ) -> str:
        if blob_backend is not self.blob:
            raise AssertionError("FsRuntime must be used with its paired filesystem blob backend")
        return self.journal.prepare_repo_open(
            repo_locator,
            self.blob,
            parent_repo_locator=parent_repo_locator,
            child_repo_path=child_repo_path,
        )

    def append_overlay(self, repo, key: str, entry: OverlayEntry[FsValueRef]) -> None:
        self.journal.append_overlay(repo, key, entry)

    def mark_state(self, repo, state: WalState) -> None:
        self.journal.mark_state(repo, state)

    def enroll_child(self, repo, child_repo_path: str, child_state) -> None:
        self.journal.enroll_child(repo, child_repo_path, child_state)

    def unenroll_child(self, repo, child_repo_path: str) -> None:
        self.journal.unenroll_child(repo, child_repo_path)

    def mark_coordinated_child(self, repo, parent_repo_locator: str) -> None:
        self.journal.mark_coordinated_child(repo, parent_repo_locator)

    def open_committed_handle(
        self,
        repo_locator: str,
        key: str,
        mode: OpenMode = "r",
        *,
        encoding: str | None = None,
    ):
        return self.blob.open_committed_handle(repo_locator, key, mode, encoding=encoding)

    def begin(
        self,
        repo_locator: str,
        blob_backend: BlobBackend | None = None,
        parent_tx: JournalTransaction | None = None,
        coordination_root_locator: str | None = None,
    ) -> JsonlWalTransaction:
        if blob_backend is not None and blob_backend is not self.blob:
            raise AssertionError("FsRuntime must be used with its paired filesystem blob backend")
        self.begin_calls[repo_locator] += 1
        return self.journal.begin(
            repo_locator,
            self.blob,
            parent_tx=parent_tx,
            coordination_root_locator=coordination_root_locator,
        )

    def child_repo_locator(self, repo_locator: str, child_repo_path: str) -> str:
        return self.blob.child_repo_locator(repo_locator, child_repo_path)

    def recover(
        self,
        repo_locator: str,
        blob_backend: BlobBackend | None = None,
        *,
        intent: RecoveryIntent = "auto",
        coordination_root_locator: str | None = None,
    ) -> None:
        if blob_backend is not None and blob_backend is not self.blob:
            raise AssertionError("FsRuntime must be used with its paired filesystem blob backend")
        self.journal.recover(
            repo_locator,
            self.blob,
            intent=intent,
            coordination_root_locator=coordination_root_locator,
        )

    def require_clear_for_read(self, repo_locator: str, blob_backend: BlobBackend | None = None) -> None:
        if blob_backend is not None and blob_backend is not self.blob:
            raise AssertionError("FsRuntime must be used with its paired filesystem blob backend")
        self.journal.require_clear_for_read(repo_locator, self.blob)

    def debug_status(self, repo_locator: str, blob_backend: BlobBackend | None = None) -> object:
        if blob_backend is not None and blob_backend is not self.blob:
            raise AssertionError("FsRuntime must be used with its paired filesystem blob backend")
        return self.journal.debug_status(repo_locator, self.blob)


def snapshot_repo_bytes(repo_root: Path) -> dict[str, bytes]:
    layout = RepoLayout(repo_root)
    return {
        key: layout.committed_path(key).read_bytes()
        for key in layout.snapshot_keys()
    }


def init_repo_metadata(repo_root: Path, *, runtime: FsRuntime | None = None) -> FsRuntime:
    chosen_runtime = FsRuntime() if runtime is None else runtime
    chosen_runtime.prepare_repo_open(str(repo_root), chosen_runtime.blob)
    return chosen_runtime


def assert_no_transaction_artifacts(repo_root: Path) -> None:
    layout = RepoLayout(repo_root)
    assert layout.meta_path.exists()
    assert layout.meta_lock_path.exists()
    assert not layout.meta_tmp_path.exists()
    assert not list(layout.tx_root.glob("meta.json.*.tmp"))
    assert not layout.wal_path.exists()
    assert not layout.fence_path.exists()
    assert not layout.fence_tmp_path.exists()
    assert not layout.staged_root.exists()


def open_repo(repo_root: Path, *, runtime: FsRuntime | None = None) -> tuple[Repo, FsRuntime]:
    chosen_runtime = FsRuntime() if runtime is None else runtime
    return (
        Repo.open(
            str(repo_root),
            blob_backend=chosen_runtime.blob,
            journal_backend=chosen_runtime,
        ),
        chosen_runtime,
    )
