from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from alpenstock.storage import FileNode, MappedRepo, Repo, TransactionStateError, define
from alpenstock.storage._handles import TextFileHandle
from alpenstock.storage.backends.fs import FilesystemBackend, RepoLayout
from alpenstock.storage.backends.fs.recovery import load_wal
from alpenstock.storage.backends.sqlite import SqliteBackend, SqliteConfig


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


def _case(tmp_path: Path, backend_name: str) -> tuple[Any, WorkspaceRepo, str, str]:
    if backend_name == "fs":
        backend = FilesystemBackend()
        root_locator = str(tmp_path / "repo")
    elif backend_name == "sqlite":
        backend = SqliteBackend()
        root_locator = str(tmp_path / "repo.db")
    else:
        raise AssertionError(f"Unknown backend {backend_name!r}")
    repo = WorkspaceRepo.open(root_locator, backend=backend)
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    return backend, repo, root_locator, child_locator


def _write_text(tx: Any, key: str, text: str) -> None:
    with cast(TextFileHandle, tx.open_handle(key, "w")) as handle:
        handle.write(text)


def _fetch_sqlite_rows(db_path: str) -> list[tuple[str, str, bytes]]:
    names = SqliteConfig().schema_names
    connection = sqlite3.connect(db_path)
    try:
        return list(connection.execute(f'SELECT repo_path, key, value FROM "{names.objects}" ORDER BY repo_path, key'))
    finally:
        connection.close()


def _simulate_process_exit(backend_name: str, *txs: Any) -> None:
    if backend_name == "fs":
        for tx in txs:
            tx.lock.release()
            tx._released = True
        return
    if backend_name == "sqlite":
        if not txs:
            return
        root_tx = txs[0]
        root_tx.connection.close()
        root_tx.lock.release()
        for tx in txs:
            tx._released = True
        return
    raise AssertionError(f"Unknown backend {backend_name!r}")


@pytest.mark.parametrize("backend_name", ["fs", "sqlite"])
def test_enrollment_records_only_written_child_repo(tmp_path: Path, backend_name: str) -> None:
    backend, repo, root_locator, _child_locator = _case(tmp_path, backend_name)

    with repo.transaction() as tx:
        repo.users["bob"].profile  # bind but do not write
        repo.users["alice"].profile.write_text("alice")

        if backend_name == "fs":
            layout = RepoLayout(root_locator)
            _state, _overlay, children = load_wal(layout)
            assert sorted(children) == ["users/alice"]
        else:
            names = SqliteConfig().schema_names
            connection = sqlite3.connect(root_locator)
            try:
                children = list(
                    connection.execute(
                        f'SELECT child_repo_path FROM "{names.tx_children}" WHERE parent_tx_id = ? ORDER BY child_repo_path',
                        (cast(Any, tx.backend_tx).tx_id,),
                    )
                )
            finally:
                connection.close()
            assert children == [("users/alice",)]


@pytest.mark.parametrize("backend_name", ["fs", "sqlite"])
def test_deep_nested_write_preserves_parent_child_transaction_tree(tmp_path: Path, backend_name: str) -> None:
    if backend_name == "fs":
        backend = FilesystemBackend()
        root_locator = str(tmp_path / "repo")
    else:
        backend = SqliteBackend()
        root_locator = str(tmp_path / "repo.db")

    repo = RootRepo.open(root_locator, backend=backend)

    with repo.transaction() as root_tx:
        repo.children["a"].grands["g"].leaf.write_text("x")

        child_repo = repo.children["a"]
        grand_repo = child_repo.grands["g"]
        assert sorted(root_tx._children) == [child_repo.repo_locator]
        child_tx = root_tx._children[child_repo.repo_locator]
        assert sorted(child_tx._children) == [grand_repo.repo_locator]

        if backend_name == "fs":
            _root_state, _root_overlay, root_children = load_wal(RepoLayout(root_locator))
            _child_state, _child_overlay, child_children = load_wal(RepoLayout(child_repo.repo_locator))
            assert sorted(root_children) == ["children/a"]
            assert sorted(child_children) == ["grands/g"]
        else:
            names = SqliteConfig().schema_names
            connection = sqlite3.connect(root_locator)
            try:
                root_children = list(
                    connection.execute(
                        f'SELECT child_repo_path FROM "{names.tx_children}" WHERE parent_tx_id = ? ORDER BY child_repo_path',
                        (cast(Any, root_tx.backend_tx).tx_id,),
                    )
                )
                child_children = list(
                    connection.execute(
                        f'SELECT child_repo_path FROM "{names.tx_children}" WHERE parent_tx_id = ? ORDER BY child_repo_path',
                        (cast(Any, child_tx.backend_tx).tx_id,),
                    )
                )
            finally:
                connection.close()
            assert root_children == [("children/a",)]
            assert child_children == [("grands/g",)]


@pytest.mark.parametrize("backend_name", ["fs", "sqlite"])
def test_recover_open_parent_with_prepared_child_aborts_whole_tree(tmp_path: Path, backend_name: str) -> None:
    backend, _repo, root_locator, child_locator = _case(tmp_path, backend_name)

    root_tx = backend.begin(root_locator)
    child_tx = backend.begin(child_locator, parent_tx=root_tx)
    _write_text(child_tx, "profile", "alice")
    child_tx.prepare()
    _simulate_process_exit(backend_name, root_tx, child_tx)

    backend.recover(root_locator)

    if backend_name == "fs":
        assert not (Path(child_locator) / "profile").exists()
        assert not RepoLayout(root_locator).tx_root.exists()
    else:
        assert _fetch_sqlite_rows(root_locator) == []
        names = SqliteConfig().schema_names
        connection = sqlite3.connect(root_locator)
        try:
            pending = list(connection.execute(f'SELECT tx_id FROM "{names.tx_meta}"'))
        finally:
            connection.close()
        assert pending == []


@pytest.mark.parametrize("backend_name", ["fs", "sqlite"])
def test_recover_prepared_nested_tree_commits_parent_and_child(tmp_path: Path, backend_name: str) -> None:
    backend, _repo, root_locator, child_locator = _case(tmp_path, backend_name)

    root_tx = backend.begin(root_locator)
    child_tx = backend.begin(child_locator, parent_tx=root_tx)
    _write_text(root_tx, "config", "root")
    _write_text(child_tx, "profile", "alice")
    child_tx.prepare()
    root_tx.mark_child_prepared("users/alice")
    root_tx.prepare()
    _simulate_process_exit(backend_name, root_tx, child_tx)

    backend.recover(root_locator)

    if backend_name == "fs":
        assert (Path(root_locator) / "config").read_text(encoding="utf-8") == "root"
        assert (Path(child_locator) / "profile").read_text(encoding="utf-8") == "alice"
        assert not RepoLayout(root_locator).tx_root.exists()
    else:
        assert _fetch_sqlite_rows(root_locator) == [
            ("", "config", b"root"),
            ("users/alice", "profile", b"alice"),
        ]


def test_filesystem_direct_child_recovery_is_rejected_for_coordinated_transaction(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")

    root_tx = backend.begin(root_locator)
    child_tx = backend.begin(child_locator, parent_tx=root_tx)
    _write_text(root_tx, "config", "root")
    _write_text(child_tx, "profile", "alice")
    child_tx.prepare()
    root_tx.mark_child_prepared("users/alice")
    root_tx.prepare()
    _simulate_process_exit("fs", root_tx, child_tx)

    with pytest.raises(TransactionStateError, match="coordinated filesystem child repo"):
        backend.recover(child_locator)

    backend.recover(root_locator)

    assert (Path(root_locator) / "config").read_text(encoding="utf-8") == "root"
    assert (Path(child_locator) / "profile").read_text(encoding="utf-8") == "alice"


@pytest.mark.parametrize("backend_name", ["fs", "sqlite"])
def test_recover_after_child_commit_before_parent_commit_finishes_parent(tmp_path: Path, backend_name: str) -> None:
    backend, _repo, root_locator, child_locator = _case(tmp_path, backend_name)

    root_tx = backend.begin(root_locator)
    child_tx = backend.begin(child_locator, parent_tx=root_tx)
    _write_text(root_tx, "config", "root")
    _write_text(child_tx, "profile", "alice")
    child_tx.prepare()
    root_tx.mark_child_prepared("users/alice")
    root_tx.prepare()
    child_tx.commit()
    _simulate_process_exit(backend_name, root_tx, child_tx)

    backend.recover(root_locator)

    if backend_name == "fs":
        assert (Path(root_locator) / "config").read_text(encoding="utf-8") == "root"
        assert (Path(child_locator) / "profile").read_text(encoding="utf-8") == "alice"
    else:
        assert _fetch_sqlite_rows(root_locator) == [
            ("", "config", b"root"),
            ("users/alice", "profile", b"alice"),
        ]


@pytest.mark.parametrize("backend_name", ["fs", "sqlite"])
def test_recover_prepared_deep_nested_tree_commits_all_levels(tmp_path: Path, backend_name: str) -> None:
    if backend_name == "fs":
        backend = FilesystemBackend()
        root_locator = str(tmp_path / "repo")
    else:
        backend = SqliteBackend()
        root_locator = str(tmp_path / "repo.db")

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
    _simulate_process_exit(backend_name, root_tx, child_tx, grand_tx)

    backend.recover(root_locator)

    if backend_name == "fs":
        assert (Path(root_locator) / "root.txt").read_text(encoding="utf-8") == "root"
        assert (Path(child_locator) / "child.txt").read_text(encoding="utf-8") == "child"
        assert (Path(grand_locator) / "grand.txt").read_text(encoding="utf-8") == "grand"
    else:
        assert _fetch_sqlite_rows(root_locator) == [
            ("", "root.txt", b"root"),
            ("children/a", "child.txt", b"child"),
            ("children/a/grands/g", "grand.txt", b"grand"),
        ]
