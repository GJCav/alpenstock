from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from alpenstock.storage import FileNode, MappedRepo, Repo, TransactionStateError, define
from alpenstock.storage._handles import TextFileHandle
from alpenstock.storage.backends.fs import RepoLayout
from tests.storage._fs_test_utils import FsRuntime, assert_no_transaction_artifacts
from alpenstock.storage.backends.fs.recovery import load_wal


@define
class UserRepo(Repo):
    profile: FileNode


@define
class WorkspaceRepo(Repo):
    config: FileNode
    users: MappedRepo[UserRepo]


@define
class GrandRepo(Repo):
    leaf: FileNode


@define
class ChildRepo(Repo):
    grands: MappedRepo[GrandRepo]


@define
class RootRepo(Repo):
    children: MappedRepo[ChildRepo]


def _case(tmp_path: Path) -> tuple[FsRuntime, WorkspaceRepo, str, str]:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    repo = WorkspaceRepo.open(root_locator, blob_backend=backend.blob, journal_backend=backend.journal)
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    return backend, repo, root_locator, child_locator


def _write_text(tx: Any, key: str, text: str) -> None:
    with cast(TextFileHandle, tx.open_handle(key, "w")) as handle:
        handle.write(text)


def _simulate_process_exit(*txs: Any) -> None:
    for tx in txs:
        tx.lock.release()
        tx._released = True


def test_enrollment_records_only_written_child_repo(tmp_path: Path) -> None:
    _backend, repo, root_locator, _child_locator = _case(tmp_path)

    with repo.transaction():
        repo.users["bob"].profile
        repo.users["alice"].profile.write_text("alice")

        layout = RepoLayout(root_locator)
        _state, _overlay, children = load_wal(layout)
        assert sorted(children) == ["users/alice"]


def test_deep_nested_write_preserves_parent_child_transaction_tree(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    repo = RootRepo.open(root_locator, blob_backend=backend.blob, journal_backend=backend.journal)

    with repo.transaction() as root_tx:
        repo.children["a"].grands["g"].leaf.write_text("x")

        child_repo = repo.children["a"]
        grand_repo = child_repo.grands["g"]
        assert sorted(root_tx._children) == [child_repo.repo_locator]
        child_tx = root_tx._children[child_repo.repo_locator]
        assert sorted(child_tx._children) == [grand_repo.repo_locator]

        _root_state, _root_overlay, root_children = load_wal(RepoLayout(root_locator))
        _child_state, _child_overlay, child_children = load_wal(RepoLayout(child_repo.repo_locator))
        assert sorted(root_children) == ["children/a"]
        assert sorted(child_children) == ["grands/g"]


def test_recover_open_parent_with_prepared_child_aborts_whole_tree(tmp_path: Path) -> None:
    backend, _repo, root_locator, child_locator = _case(tmp_path)

    root_tx = backend.begin(root_locator)
    child_tx = backend.begin(child_locator, parent_tx=root_tx)
    _write_text(child_tx, "profile", "alice")
    child_tx.prepare()
    _simulate_process_exit(root_tx, child_tx)

    backend.recover(root_locator)

    assert not (Path(child_locator) / "profile").exists()
    assert_no_transaction_artifacts(Path(root_locator))


def test_recover_prepared_nested_tree_commits_parent_and_child(tmp_path: Path) -> None:
    backend, _repo, root_locator, child_locator = _case(tmp_path)

    root_tx = backend.begin(root_locator)
    child_tx = backend.begin(child_locator, parent_tx=root_tx)
    _write_text(root_tx, "config", "root")
    _write_text(child_tx, "profile", "alice")
    child_tx.prepare()
    root_tx.mark_child_prepared("users/alice")
    root_tx.prepare()
    _simulate_process_exit(root_tx, child_tx)

    backend.recover(root_locator)

    assert (Path(root_locator) / "config").read_text(encoding="utf-8") == "root"
    assert (Path(child_locator) / "profile").read_text(encoding="utf-8") == "alice"
    assert_no_transaction_artifacts(Path(root_locator))


def test_coordinated_child_journal_tx_commit_requires_root_authorization(tmp_path: Path) -> None:
    backend, _repo, root_locator, child_locator = _case(tmp_path)

    root_tx = backend.begin(root_locator)
    child_tx = backend.begin(child_locator, parent_tx=root_tx)
    _write_text(child_tx, "profile", "alice")
    child_tx.prepare()
    root_tx.mark_child_prepared("users/alice")
    root_tx.prepare()

    with pytest.raises(TransactionStateError, match="root commit authorization"):
        child_tx.commit()

    child_tx.rollback()
    root_tx.rollback()


def test_filesystem_direct_child_recovery_is_rejected_for_coordinated_transaction(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")

    root_tx = backend.begin(root_locator)
    child_tx = backend.begin(child_locator, parent_tx=root_tx)
    _write_text(root_tx, "config", "root")
    _write_text(child_tx, "profile", "alice")
    child_tx.prepare()
    root_tx.mark_child_prepared("users/alice")
    root_tx.prepare()
    _simulate_process_exit(root_tx, child_tx)

    with pytest.raises(TransactionStateError, match="coordinated filesystem child repo"):
        backend.recover(child_locator)

    backend.recover(root_locator)

    assert (Path(root_locator) / "config").read_text(encoding="utf-8") == "root"
    assert (Path(child_locator) / "profile").read_text(encoding="utf-8") == "alice"


def test_recover_prepared_deep_nested_tree_commits_all_levels(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "children/a")
    grand_locator = backend.child_repo_locator(child_locator, "grands/g")

    root_tx = backend.begin(root_locator)
    child_tx = backend.begin(child_locator, parent_tx=root_tx)
    grand_tx = backend.begin(grand_locator, parent_tx=child_tx)
    _write_text(root_tx, "root.txt", "root")
    _write_text(child_tx, "child.txt", "child")
    _write_text(grand_tx, "grand.txt", "grand")
    grand_tx.prepare()
    child_tx.mark_child_prepared("grands/g")
    child_tx.prepare()
    root_tx.mark_child_prepared("children/a")
    root_tx.prepare()
    _simulate_process_exit(root_tx, child_tx, grand_tx)

    backend.recover(root_locator)

    assert (Path(root_locator) / "root.txt").read_text(encoding="utf-8") == "root"
    assert (Path(child_locator) / "child.txt").read_text(encoding="utf-8") == "child"
    assert (Path(grand_locator) / "grand.txt").read_text(encoding="utf-8") == "grand"
