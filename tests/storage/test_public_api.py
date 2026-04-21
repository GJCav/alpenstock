from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from alpenstock.storage import FileNode, KeyNotFoundError, MappedRepo, Repo, TransactionStateError, define
from alpenstock.storage._blob_backend import BlobBackend
from alpenstock.storage._journal_backend import JournalTransaction
from alpenstock.storage.backends.fs import JsonlWalTransaction, RepoLayout
from tests.storage._fs_test_utils import FsRuntime, assert_no_transaction_artifacts, init_repo_metadata, open_repo, snapshot_repo_bytes


@define
class ChildRepo(Repo):
    value: FileNode


@define
class RootRepo(Repo):
    children: MappedRepo[ChildRepo]


def test_repo_open_binds_blob_and_journal_backends_and_does_not_start_transaction(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    runtime = FsRuntime()

    repo = Repo.open(str(repo_root), blob_backend=runtime.blob, journal_backend=runtime)

    assert repo.repo_locator == str(repo_root)
    assert repo.active_transaction is None
    assert runtime.begin_calls[str(repo_root)] == 0


def test_read_only_open_outside_transaction_uses_committed_view_without_begin(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    init_repo_metadata(repo_root)
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
    init_repo_metadata(repo_root)
    (repo_root / "alpha").parent.mkdir(parents=True, exist_ok=True)
    (repo_root / "alpha").write_bytes(b"old")
    repo, _backend = open_repo(repo_root)

    with pytest.raises(RuntimeError, match="boom"):
        with repo.transaction():
            repo.file("alpha").write_text("new")
            repo.file("beta").write_text("other")
            raise RuntimeError("boom")

    assert snapshot_repo_bytes(repo_root) == {"alpha": b"old"}


def test_caught_child_exception_rolls_back_only_child_and_allows_root_commit(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root))

    with repo.transaction():
        with pytest.raises(RuntimeError, match="child boom"):
            with repo.children["alice"].transaction():
                repo.children["alice"].value.write_text("discard")
                raise RuntimeError("child boom")
        repo.file("root.txt").write_text("root")

    assert snapshot_repo_bytes(repo_root) == {"root.txt": b"root"}


def test_uncaught_child_exception_cascades_into_root_rollback(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root))

    with pytest.raises(RuntimeError, match="child boom"):
        with repo.transaction():
            repo.file("root.txt").write_text("root")
            with repo.children["alice"].transaction():
                repo.children["alice"].value.write_text("discard")
                raise RuntimeError("child boom")

    assert snapshot_repo_bytes(repo_root) == {}


def test_explicit_child_rollback_is_local_to_child_participant(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root))

    with repo.transaction():
        with repo.children["alice"].transaction() as child_tx:
            repo.children["alice"].value.write_text("discard")
            child_tx.rollback()
        repo.file("root.txt").write_text("root")

    assert snapshot_repo_bytes(repo_root) == {"root.txt": b"root"}


def test_child_scope_exit_prepares_and_reopen_resumes_staged_state(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root))

    with repo.transaction():
        with repo.children["alice"].transaction() as child_tx:
            repo.children["alice"].value.write_text("one")
        assert child_tx.is_prepared is True
        assert cast(JsonlWalTransaction, child_tx.journal_tx).is_prepared is True
        assert repo.children["alice"].value.read_text() == "one"

        with repo.children["alice"].transaction() as reopened_child_tx:
            assert reopened_child_tx is child_tx
            repo.children["alice"].value.write_text("two")
        assert child_tx.is_prepared is True

    assert snapshot_repo_bytes(repo_root) == {"children/alice/value": b"two"}


def test_write_through_prepared_child_implicitly_reopens_and_reprepares(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root))

    with repo.transaction():
        with repo.children["alice"].transaction() as child_tx:
            repo.children["alice"].value.write_text("one")
        repo.children["alice"].value.write_text("two")
        assert child_tx.is_prepared is True
        assert repo.children["alice"].value.read_text() == "two"

    assert snapshot_repo_bytes(repo_root) == {"children/alice/value": b"two"}


def test_delete_through_prepared_child_implicitly_reopens_and_reprepares(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root))

    with repo.transaction():
        with repo.children["alice"].transaction() as child_tx:
            repo.children["alice"].value.write_text("one")
        repo.children["alice"].value.delete()
        assert child_tx.is_prepared is True
        with pytest.raises(KeyNotFoundError):
            repo.children["alice"].value.read_text()

    assert snapshot_repo_bytes(repo_root) == {}


def test_coordinated_commit_publishes_parent_before_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root))
    publish_order: list[str] = []
    original_publish = JsonlWalTransaction.publish_prepared

    def record_publish(self: JsonlWalTransaction) -> None:
        publish_order.append(str(self.layout.repo_root))
        original_publish(self)

    with monkeypatch.context() as m:
        m.setattr(JsonlWalTransaction, "publish_prepared", record_publish)
        with repo.transaction():
            repo.file("root.txt").write_text("root")
            repo.children["alice"].value.write_text("alice")

    assert publish_order == [
        str(repo_root),
        str(repo_root / "children" / "alice"),
    ]


def test_raw_file_escape_hatch_rejects_declared_repo_boundaries(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root))

    with pytest.raises(TransactionStateError, match="crosses nested repo boundary"):
        repo.file("children/alice/value")

    repo.file("ordinary/value").write_text("ok")
    assert snapshot_repo_bytes(repo_root) == {"ordinary/value": b"ok"}


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
    init_repo_metadata(repo_root)
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
    runtime = FsRuntime()
    repo = Repo.open(str(repo_root), blob_backend=runtime.blob, journal_backend=runtime)

    def fail_begin(
        self: FsRuntime,
        repo_locator: str,
        blob_backend: BlobBackend,
        parent_tx: JournalTransaction | None = None,
        coordination_root_locator: str | None = None,
    ):
        del self, blob_backend, parent_tx, coordination_root_locator
        raise RuntimeError(f"begin failed for {repo_locator}")

    with monkeypatch.context() as m:
        m.setattr(FsRuntime, "begin", fail_begin)

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
    init_repo_metadata(repo_root)
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
    original_rollback = JsonlWalTransaction.rollback

    def fail_publish(self: JsonlWalTransaction) -> None:
        del self
        raise RuntimeError("commit failed")

    def count_rollback(self: JsonlWalTransaction) -> None:
        nonlocal rollback_calls
        rollback_calls += 1
        original_rollback(self)

    tx.__enter__()
    repo.file("alpha").write_text("hello")

    with monkeypatch.context() as m:
        m.setattr(JsonlWalTransaction, "publish_prepared", fail_publish)
        m.setattr(JsonlWalTransaction, "rollback", count_rollback)

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
    repo = RootRepo.open(str(repo_root))
    root_journal_tx: JsonlWalTransaction | None = None
    child_journal_tx: JsonlWalTransaction | None = None
    original_rollback = JsonlWalTransaction.rollback

    def patched_rollback(self: JsonlWalTransaction) -> None:
        if self is child_journal_tx:
            raise RuntimeError("child rollback failed")
        original_rollback(self)

    with pytest.raises(RuntimeError, match="boom") as exc_info:
        with repo.transaction() as root_tx:
            child_tx = repo.children["alice"].transaction()
            child_tx.__enter__()
            repo.children["alice"].value.write_text("alice")
            root_journal_tx = cast(JsonlWalTransaction, root_tx.journal_tx)
            child_journal_tx = cast(JsonlWalTransaction, child_tx.journal_tx)
            monkeypatch.setattr(JsonlWalTransaction, "rollback", patched_rollback)
            raise RuntimeError("boom")

    notes = getattr(exc_info.value, "__notes__", [])
    assert any("child rollback failed" in note for note in notes)
    assert root_journal_tx is not None
    assert root_journal_tx._released is True
    assert repo.active_transaction is None
    assert_no_transaction_artifacts(repo_root)


def test_root_detach_continues_cleanup_after_child_detach_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root))
    tx = repo.transaction()
    root_journal_tx: JsonlWalTransaction | None = None
    child_journal_tx: JsonlWalTransaction | None = None
    root_detach_called = False
    original_publish = JsonlWalTransaction.publish_prepared
    original_detach = JsonlWalTransaction.detach_for_recovery

    def patched_publish(self: JsonlWalTransaction) -> None:
        if self is root_journal_tx:
            raise RuntimeError("root commit failed")
        original_publish(self)

    def patched_detach(self: JsonlWalTransaction) -> None:
        nonlocal root_detach_called
        if self is child_journal_tx:
            raise RuntimeError("child detach failed")
        if self is root_journal_tx:
            root_detach_called = True
        original_detach(self)

    tx.__enter__()
    with repo.children["alice"].transaction() as child_tx:
        repo.file("root.txt").write_text("root")
        repo.children["alice"].value.write_text("alice")
        child_journal_tx = cast(JsonlWalTransaction, child_tx.journal_tx)
    root_journal_tx = cast(JsonlWalTransaction, tx.journal_tx)

    with monkeypatch.context() as m:
        m.setattr(JsonlWalTransaction, "publish_prepared", patched_publish)
        m.setattr(JsonlWalTransaction, "detach_for_recovery", patched_detach)
        with pytest.raises(RuntimeError, match="root commit failed") as exc_info:
            tx.__exit__(None, None, None)

    notes = getattr(exc_info.value, "__notes__", [])
    assert any("child detach failed" in note for note in notes)
    assert root_journal_tx is not None
    assert root_journal_tx._released is True
    assert root_detach_called is True
    assert repo.active_transaction is None

    with pytest.raises(TransactionStateError, match="Coordinated child transaction is no longer active"):
        child_tx.__enter__()


def test_root_transaction_cannot_finish_while_child_scope_is_still_open(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root))
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
    repo = RootRepo.open(str(repo_root))

    with repo.transaction() as root_tx:
        with repo.children["alice"].transaction() as child_tx:
            repo.children["alice"].value.write_text("alice")

    for tx in (root_tx, child_tx):
        with pytest.raises(TransactionStateError, match="rollback\\(\\) is invalid after the transaction has finished"):
            tx.rollback()


def test_finished_transactions_reject_journal_access_and_helpers(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root))

    with repo.transaction() as root_tx:
        with repo.children["alice"].transaction() as child_tx:
            repo.children["alice"].value.write_text("alice")

    for tx in (root_tx, child_tx):
        with pytest.raises(TransactionStateError, match="already finished"):
            tx.journal_tx
        with pytest.raises(TransactionStateError, match="already finished"):
            tx.open("alpha", "r")
        with pytest.raises(TransactionStateError, match="already finished"):
            tx.delete("alpha")


def test_implicit_descendant_open_failure_does_not_abort_outer_transaction(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo = RootRepo.open(str(repo_root))

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

    def fail_seal(self: JsonlWalTransaction, handle: object) -> None:
        del self, handle
        raise RuntimeError("seal failed")

    with monkeypatch.context() as m:
        m.setattr(JsonlWalTransaction, "_seal_writer", fail_seal)

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

    def fail_seal(self: JsonlWalTransaction, handle: object) -> None:
        del self, handle
        raise RuntimeError("seal failed")

    def fail_rollback(self: JsonlWalTransaction) -> None:
        self.layout.cleanup_tx_root()
        self.lock.release()
        raise RuntimeError("rollback failed")

    with monkeypatch.context() as m:
        m.setattr(JsonlWalTransaction, "_seal_writer", fail_seal)
        m.setattr(JsonlWalTransaction, "rollback", fail_rollback)

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

    def fail_rollback(self: JsonlWalTransaction) -> None:
        self.layout.cleanup_tx_root()
        self.lock.release()
        raise RuntimeError("rollback failed")

    with monkeypatch.context() as m:
        m.setattr(JsonlWalTransaction, "rollback", fail_rollback)

        with pytest.raises(RuntimeError, match="boom") as exc_info:
            with repo.file("alpha").open("w") as handle:
                handle.write("hello")
                raise RuntimeError("boom")

    notes = getattr(exc_info.value, "__notes__", [])
    assert any("rollback failed" in note for note in notes)
    assert repo.active_transaction is None
    assert snapshot_repo_bytes(repo_root) == {}
