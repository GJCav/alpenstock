from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from alpenstock.storage import FileNode, KeyNotFoundError, MappedRepo, Repo, TransactionStateError, define
from alpenstock.storage._backend import BackendTransaction
from alpenstock.storage.backends.fs import FilesystemBackend, FilesystemBackendTransaction, RepoLayout
from tests.storage._fs_test_utils import SpyFilesystemBackend, open_repo, snapshot_repo_bytes


@define
class ChildRepo(Repo):
    value: FileNode


@define
class RootRepo(Repo):
    children: MappedRepo[ChildRepo]


def test_repo_open_binds_backend_and_does_not_start_transaction(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    backend = SpyFilesystemBackend()

    repo = Repo.open(str(repo_root), backend=backend)

    assert repo.repo_locator == str(repo_root)
    assert repo.active_transaction is None
    assert backend.begin_calls[str(repo_root)] == 0


def test_read_only_open_outside_transaction_uses_committed_view_without_begin(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "alpha").parent.mkdir(parents=True, exist_ok=True)
    (repo_root / "alpha").write_bytes(b"hello")
    repo, backend = open_repo(repo_root)

    with repo.file("alpha").open("r") as handle:
        assert handle.read() == "hello"

    assert backend.begin_calls[str(repo_root)] == 0
    assert repo.active_transaction is None


def test_implicit_single_file_write_commits_on_close(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo, backend = open_repo(repo_root)

    with repo.file("alpha").open("w") as handle:
        handle.write("hello")

    assert snapshot_repo_bytes(repo_root) == {"alpha": b"hello"}
    assert repo.active_transaction is None
    assert backend.begin_calls[str(repo_root)] == 1


def test_repo_file_normalizes_equivalent_logical_key_spellings(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo, _backend = open_repo(repo_root)

    repo.file("alpha//beta").write_text("hello")

    assert snapshot_repo_bytes(repo_root) == {"alpha/beta": b"hello"}


def test_explicit_transaction_commits_multiple_files_atomically(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo, _backend = open_repo(repo_root)

    with repo.transaction():
        repo.file("alpha").write_text("one")
        repo.file("beta").write_bytes(b"two")

    assert snapshot_repo_bytes(repo_root) == {"alpha": b"one", "beta": b"two"}


def test_exception_inside_transaction_rolls_back_all_changes(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "alpha").parent.mkdir(parents=True, exist_ok=True)
    (repo_root / "alpha").write_bytes(b"old")
    repo, _backend = open_repo(repo_root)

    with pytest.raises(RuntimeError, match="boom"):
        with repo.transaction():
            repo.file("alpha").write_text("new")
            repo.file("beta").write_text("other")
            raise RuntimeError("boom")

    assert snapshot_repo_bytes(repo_root) == {"alpha": b"old"}


def test_nested_helper_calls_participate_in_active_transaction(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo, backend = open_repo(repo_root)

    with repo.transaction() as tx:
        repo.file("alpha").write_text("one")
        with tx.open("alpha", "r") as handle:
            assert handle.read() == "one"
        repo.file("beta").write_text("two")

    assert snapshot_repo_bytes(repo_root) == {"alpha": b"one", "beta": b"two"}
    assert backend.begin_calls[str(repo_root)] == 1


def test_delete_helper_uses_same_transaction_machinery(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "alpha").parent.mkdir(parents=True, exist_ok=True)
    (repo_root / "alpha").write_bytes(b"old")
    (repo_root / "beta").write_bytes(b"keep")
    repo, _backend = open_repo(repo_root)

    repo.file("alpha").delete()

    assert snapshot_repo_bytes(repo_root) == {"beta": b"keep"}


def test_transaction_context_rejects_nested_transaction_contexts(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo, _backend = open_repo(repo_root)

    with repo.transaction():
        with pytest.raises(TransactionStateError, match="same repo while one is active is prohibited"):
            with repo.transaction():
                pass


def test_begin_failure_does_not_leave_repo_bound_to_broken_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    backend = SpyFilesystemBackend()
    repo = Repo.open(str(repo_root), backend=backend)

    def fail_begin(
        self: SpyFilesystemBackend,
        repo_locator: str,
        parent_tx: BackendTransaction | None = None,
    ):
        del self, parent_tx
        raise RuntimeError(f"begin failed for {repo_locator}")

    with monkeypatch.context() as m:
        m.setattr(SpyFilesystemBackend, "begin", fail_begin)

        with pytest.raises(RuntimeError, match="begin failed"):
            with repo.transaction():
                pass

    assert repo.active_transaction is None


def test_rollback_method_discards_changes_and_releases_writer_tracking(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo, _backend = open_repo(repo_root)

    with repo.transaction() as tx:
        with tx.open("alpha", "w") as handle:
            handle.write("temp")
        tx.rollback()

    assert snapshot_repo_bytes(repo_root) == {}


def test_transaction_exit_with_open_writer_raises_and_rolls_back_cleanly(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "alpha").parent.mkdir(parents=True, exist_ok=True)
    (repo_root / "alpha").write_bytes(b"old")
    repo, _backend = open_repo(repo_root)

    with pytest.raises(Exception, match="Writable handles must be closed before prepare"):
        with repo.transaction() as tx:
            tx.open("alpha", "w")

    assert repo.active_transaction is None
    assert snapshot_repo_bytes(repo_root) == {"alpha": b"old"}

    repo.file("alpha").write_text("new")
    assert snapshot_repo_bytes(repo_root) == {"alpha": b"new"}


def test_commit_failure_does_not_trigger_automatic_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo, backend = open_repo(repo_root)
    tx = repo.transaction()
    rollback_calls = 0
    original_rollback = FilesystemBackendTransaction.rollback

    def fail_commit(self: FilesystemBackendTransaction) -> None:
        del self
        raise RuntimeError("commit failed")

    def count_rollback(self: FilesystemBackendTransaction) -> None:
        nonlocal rollback_calls
        rollback_calls += 1
        original_rollback(self)

    tx.__enter__()
    repo.file("alpha").write_text("hello")

    with monkeypatch.context() as m:
        m.setattr(FilesystemBackendTransaction, "commit", fail_commit)
        m.setattr(FilesystemBackendTransaction, "rollback", count_rollback)

        with pytest.raises(RuntimeError, match="commit failed"):
            tx.__exit__(None, None, None)

        assert rollback_calls == 0
        with pytest.raises(TransactionStateError, match="rollback\\(\\) is invalid after commit finalization failed"):
            tx.rollback()

    assert repo.active_transaction is None
    assert snapshot_repo_bytes(repo_root) == {}

    backend.recover(str(repo_root))
    assert snapshot_repo_bytes(repo_root) == {"alpha": b"hello"}


def test_root_rollback_continues_cleanup_after_child_rollback_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root), backend=FilesystemBackend())
    root_backend: FilesystemBackendTransaction | None = None
    child_backend: FilesystemBackendTransaction | None = None
    original_rollback = FilesystemBackendTransaction.rollback

    def patched_rollback(self: FilesystemBackendTransaction) -> None:
        if self is child_backend:
            raise RuntimeError("child rollback failed")
        original_rollback(self)

    with pytest.raises(RuntimeError, match="boom") as exc_info:
        with repo.transaction() as root_tx:
            child_tx = repo.children["alice"].transaction()
            child_tx.__enter__()
            repo.children["alice"].value.write_text("alice")
            root_backend = cast(FilesystemBackendTransaction, root_tx.backend_tx)
            child_backend = cast(FilesystemBackendTransaction, child_tx.backend_tx)
            monkeypatch.setattr(FilesystemBackendTransaction, "rollback", patched_rollback)
            raise RuntimeError("boom")

    notes = getattr(exc_info.value, "__notes__", [])
    assert any("child rollback failed" in note for note in notes)
    assert root_backend is not None
    assert root_backend._released is True
    assert repo.active_transaction is None
    assert not RepoLayout(repo_root).tx_root.exists()


def test_root_detach_continues_cleanup_after_child_detach_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root), backend=FilesystemBackend())
    tx = repo.transaction()
    root_backend: FilesystemBackendTransaction | None = None
    child_backend: FilesystemBackendTransaction | None = None
    root_detach_called = False
    original_commit = FilesystemBackendTransaction.commit
    original_detach = FilesystemBackendTransaction.detach_for_recovery

    def patched_commit(self: FilesystemBackendTransaction) -> None:
        if self is root_backend:
            raise RuntimeError("root commit failed")
        original_commit(self)

    def patched_detach(self: FilesystemBackendTransaction) -> None:
        nonlocal root_detach_called
        if self is child_backend:
            raise RuntimeError("child detach failed")
        if self is root_backend:
            root_detach_called = True
        original_detach(self)

    tx.__enter__()
    with repo.children["alice"].transaction() as child_tx:
        repo.file("root.txt").write_text("root")
        repo.children["alice"].value.write_text("alice")
        child_backend = cast(FilesystemBackendTransaction, child_tx.backend_tx)
    root_backend = cast(FilesystemBackendTransaction, tx.backend_tx)

    with monkeypatch.context() as m:
        m.setattr(FilesystemBackendTransaction, "commit", patched_commit)
        m.setattr(FilesystemBackendTransaction, "detach_for_recovery", patched_detach)
        with pytest.raises(RuntimeError, match="root commit failed") as exc_info:
            tx.__exit__(None, None, None)

    notes = getattr(exc_info.value, "__notes__", [])
    assert any("child detach failed" in note for note in notes)
    assert root_backend is not None
    assert root_backend._released is True
    assert root_detach_called is True
    assert repo.active_transaction is None

    with pytest.raises(TransactionStateError, match="Coordinated child transaction is no longer active"):
        child_tx.__enter__()


def test_root_transaction_cannot_finish_while_child_scope_is_still_open(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root), backend=FilesystemBackend())
    root_tx = repo.transaction()

    root_tx.__enter__()
    child_tx = repo.children["alice"].transaction()
    child_tx.__enter__()
    repo.children["alice"].value.write_text("alice")

    with pytest.raises(
        TransactionStateError,
        match="All coordinated child transaction scopes must exit before the root transaction can finish",
    ):
        root_tx.__exit__(None, None, None)

    assert repo.active_transaction is None
    assert not (repo_root / "children" / "alice" / "value").exists()
    with pytest.raises(TransactionStateError, match="Coordinated child transaction is no longer active"):
        child_tx.__enter__()


def test_finished_transactions_reject_rollback(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root), backend=FilesystemBackend())

    with repo.transaction() as root_tx:
        with repo.children["alice"].transaction() as child_tx:
            repo.children["alice"].value.write_text("alice")

    for tx in (root_tx, child_tx):
        with pytest.raises(TransactionStateError, match="rollback\\(\\) is invalid after the transaction has finished"):
            tx.rollback()


def test_finished_transactions_reject_backend_access_and_helpers(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root), backend=FilesystemBackend())

    with repo.transaction() as root_tx:
        with repo.children["alice"].transaction() as child_tx:
            repo.children["alice"].value.write_text("alice")

    for tx in (root_tx, child_tx):
        with pytest.raises(TransactionStateError, match="already finished"):
            tx.backend_tx
        with pytest.raises(TransactionStateError, match="already finished"):
            tx.open("alpha", "r")
        with pytest.raises(TransactionStateError, match="already finished"):
            tx.delete("alpha")


def test_implicit_descendant_open_failure_does_not_abort_outer_transaction(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root), backend=FilesystemBackend())

    with repo.transaction() as root_tx:
        with pytest.raises(KeyNotFoundError, match="Logical key 'value' does not exist"):
            repo.children["alice"].value.open("r+")
        assert root_tx is repo.active_transaction
        assert repo.children["alice"].active_transaction is None
        repo.file("root.txt").write_text("root")

    assert snapshot_repo_bytes(repo_root) == {"root.txt": b"root"}


def test_implicit_close_failure_rolls_back_and_clears_active_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo, _backend = open_repo(repo_root)

    def fail_seal(self: FilesystemBackendTransaction, handle: object) -> None:
        del self, handle
        raise RuntimeError("seal failed")

    with monkeypatch.context() as m:
        m.setattr(FilesystemBackendTransaction, "_seal_writer", fail_seal)

        handle = repo.file("alpha").open("w")
        handle.write("hello")
        with pytest.raises(RuntimeError, match="seal failed"):
            handle.close()

    assert repo.active_transaction is None
    assert snapshot_repo_bytes(repo_root) == {}

    with repo.file("alpha").open("w") as retry:
        retry.write("ok")

    assert snapshot_repo_bytes(repo_root) == {"alpha": b"ok"}


def test_implicit_close_cleanup_unbinds_repo_even_if_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo, _backend = open_repo(repo_root)

    def fail_seal(self: FilesystemBackendTransaction, handle: object) -> None:
        del self, handle
        raise RuntimeError("seal failed")

    def fail_rollback(self: FilesystemBackendTransaction) -> None:
        self.layout.cleanup_tx_root()
        self.lock.release()
        raise RuntimeError("rollback failed")

    with monkeypatch.context() as m:
        m.setattr(FilesystemBackendTransaction, "_seal_writer", fail_seal)
        m.setattr(FilesystemBackendTransaction, "rollback", fail_rollback)

        handle = repo.file("alpha").open("w")
        handle.write("hello")
        with pytest.raises(RuntimeError, match="seal failed") as exc_info:
            handle.close()

    notes = getattr(exc_info.value, "__notes__", [])
    assert any("rollback failed" in note for note in notes)
    assert repo.active_transaction is None
    assert snapshot_repo_bytes(repo_root) == {}

    with repo.file("alpha").open("w") as retry:
        retry.write("ok")

    assert snapshot_repo_bytes(repo_root) == {"alpha": b"ok"}


def test_implicit_context_preserves_body_error_when_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo, _backend = open_repo(repo_root)

    def fail_rollback(self: FilesystemBackendTransaction) -> None:
        self.layout.cleanup_tx_root()
        self.lock.release()
        raise RuntimeError("rollback failed")

    with monkeypatch.context() as m:
        m.setattr(FilesystemBackendTransaction, "rollback", fail_rollback)

        with pytest.raises(RuntimeError, match="boom") as exc_info:
            with repo.file("alpha").open("w") as handle:
                handle.write("hello")
                raise RuntimeError("boom")

    notes = getattr(exc_info.value, "__notes__", [])
    assert any("rollback failed" in note for note in notes)
    assert repo.active_transaction is None
    assert snapshot_repo_bytes(repo_root) == {}
