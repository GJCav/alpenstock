from __future__ import annotations

from pathlib import Path

import pytest

from alpenstock.storage import Repo, TransactionStateError
from tests.storage._fs_test_utils import open_repo, snapshot_repo_bytes


def _repo_with_backend(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo, backend = open_repo(repo_root)
    return repo, backend, repo_root


def test_read_helper_outside_transaction_uses_committed_state_without_beginning_write_tx(tmp_path: Path) -> None:
    repo, backend, repo_root = _repo_with_backend(tmp_path)
    alpha_path = repo_root / "alpha"
    alpha_path.parent.mkdir(parents=True, exist_ok=True)
    alpha_path.write_bytes(b"hello")

    assert repo.file("alpha").read_text() == "hello"
    assert backend.begin_calls[repo.repo_locator] == 0


def test_single_helper_write_commits_implicitly(tmp_path: Path) -> None:
    repo, backend, repo_root = _repo_with_backend(tmp_path)

    repo.file("alpha").write_text("hello")

    assert snapshot_repo_bytes(repo_root) == {"alpha": b"hello"}
    assert backend.begin_calls[repo.repo_locator] == 1
    assert repo.active_transaction is None


def test_explicit_transaction_commits_multiple_files_atomically(tmp_path: Path) -> None:
    repo, _backend, repo_root = _repo_with_backend(tmp_path)

    with repo.transaction():
        repo.file("alpha").write_text("hello")
        repo.file("beta").write_bytes(b"world")
        assert snapshot_repo_bytes(repo_root) == {}

    assert snapshot_repo_bytes(repo_root) == {
        "alpha": b"hello",
        "beta": b"world",
    }


def test_exception_inside_explicit_transaction_rolls_back(tmp_path: Path) -> None:
    repo, _backend, repo_root = _repo_with_backend(tmp_path)

    with pytest.raises(RuntimeError, match="boom"):
        with repo.transaction():
            repo.file("alpha").write_text("hello")
            raise RuntimeError("boom")

    assert snapshot_repo_bytes(repo_root) == {}
    assert repo.active_transaction is None


def test_helpers_inside_active_transaction_reuse_same_backend_transaction(tmp_path: Path) -> None:
    repo, backend, repo_root = _repo_with_backend(tmp_path)

    with repo.transaction():
        repo.file("alpha").write_text("hello")
        repo.file("beta").write_text("world")
        assert backend.begin_calls[repo.repo_locator] == 1
        assert snapshot_repo_bytes(repo_root) == {}


def test_delete_uses_same_transaction_flow(tmp_path: Path) -> None:
    repo, _backend, repo_root = _repo_with_backend(tmp_path)
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "alpha").write_bytes(b"hello")
    (repo_root / "beta").write_bytes(b"world")

    repo.file("alpha").delete()
    assert snapshot_repo_bytes(repo_root) == {"beta": b"world"}

    with repo.transaction():
        repo.file("beta").delete()
        assert snapshot_repo_bytes(repo_root) == {"beta": b"world"}

    assert snapshot_repo_bytes(repo_root) == {}


def test_implicit_handle_context_rolls_back_on_exception(tmp_path: Path) -> None:
    repo, _backend, repo_root = _repo_with_backend(tmp_path)

    with pytest.raises(RuntimeError, match="boom"):
        with repo.file("alpha").open("w") as handle:
            handle.write("hello")
            raise RuntimeError("boom")

    assert snapshot_repo_bytes(repo_root) == {}
    assert repo.active_transaction is None


def test_manual_rollback_discards_pending_writes(tmp_path: Path) -> None:
    repo, _backend, repo_root = _repo_with_backend(tmp_path)

    with repo.transaction() as tx:
        repo.file("alpha").write_text("hello")
        tx.rollback()

    assert snapshot_repo_bytes(repo_root) == {}


def test_nested_explicit_transaction_contexts_are_rejected(tmp_path: Path) -> None:
    repo, _backend, _repo_root = _repo_with_backend(tmp_path)

    with repo.transaction():
        with pytest.raises(TransactionStateError, match="same repo while one is active is prohibited"):
            with repo.transaction():
                pass
