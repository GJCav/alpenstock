from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import attrs
import pytest

from alpenstock.storage import Dir, FileNode, MappedDir, MappedRepo, Repo, TransactionStateError, define, named
from alpenstock.storage.backends.fs import FilesystemBackend, RepoLayout
from alpenstock.storage.backends.sqlite import SqliteBackend, SqliteConfig


@define
class ProjectDir(Dir):
    key: FileNode = named("key.txt")
    docs: MappedDir[FileNode]


@define
class UserRepo(Repo):
    profile: FileNode
    projects: MappedDir[ProjectDir]


@define
class SettingsDir(Dir):
    theme: FileNode = named("theme.toml")
    server: FileNode = named("server.json")


@define
class AppRepo(Repo):
    settings: SettingsDir
    users: MappedRepo[UserRepo]
    readme: FileNode = named("README.md")


def _repo_and_backend(tmp_path: Path, backend_name: str) -> tuple[AppRepo, Any, Path]:
    if backend_name == "fs":
        repo_root = tmp_path / "repo"
        backend = FilesystemBackend()
        return AppRepo.open(str(repo_root), backend=backend), backend, repo_root
    if backend_name == "sqlite":
        db_path = tmp_path / "repo.db"
        backend = SqliteBackend()
        return AppRepo.open(str(db_path), backend=backend), backend, db_path
    raise AssertionError(f"Unknown backend {backend_name!r}")


@pytest.mark.parametrize("backend_name", ["fs", "sqlite"])
def test_schema_open_binds_declared_children_with_expected_runtime_types(
    tmp_path: Path,
    backend_name: str,
) -> None:
    repo, _backend, _repo_path = _repo_and_backend(tmp_path, backend_name)

    assert isinstance(repo.settings, SettingsDir)
    assert isinstance(repo.readme, FileNode)
    assert isinstance(repo.users, MappedRepo)
    assert isinstance(repo.users["alice"], UserRepo)
    assert repo.readme.key == "README.md"
    assert repo.settings.theme.key == "settings/theme.toml"
    assert repo.settings.server.key == "settings/server.json"
    assert repo.users["alice"].profile.key == "profile"
    assert repo.users["alice"]._parent_repo is repo


@pytest.mark.parametrize("backend_name", ["fs", "sqlite"])
def test_schema_bound_repo_commit_persists_root_and_child_repo_writes(
    tmp_path: Path,
    backend_name: str,
) -> None:
    repo, _backend, repo_path = _repo_and_backend(tmp_path, backend_name)

    with repo.transaction():
        repo.readme.write_text("root")
        repo.settings.theme.write_text("light")
        repo.users["alice"].profile.write_text("alice")
        repo.users["alice"].projects["p1"].key.write_text("project")
        repo.users["alice"].projects["p1"].docs["note.txt"].write_text("note")

    if backend_name == "fs":
        layout = RepoLayout(repo_path)
        assert layout.committed_path("README.md").read_text(encoding="utf-8") == "root"
        assert layout.committed_path("settings/theme.toml").read_text(encoding="utf-8") == "light"
        assert (repo_path / "users" / "alice" / "profile").read_text(encoding="utf-8") == "alice"
        assert (repo_path / "users" / "alice" / "projects" / "p1" / "key.txt").read_text(encoding="utf-8") == "project"
        assert (repo_path / "users" / "alice" / "projects" / "p1" / "docs" / "note.txt").read_text(encoding="utf-8") == "note"
    else:
        names = SqliteConfig().schema_names
        connection = sqlite3.connect(str(repo_path))
        try:
            rows = list(connection.execute(f'SELECT repo_path, key, value FROM "{names.objects}" ORDER BY repo_path, key'))
        finally:
            connection.close()
        assert rows == [
            ("", "README.md", b"root"),
            ("", "settings/theme.toml", b"light"),
            ("users/alice", "profile", b"alice"),
            ("users/alice", "projects/p1/docs/note.txt", b"note"),
            ("users/alice", "projects/p1/key.txt", b"project"),
        ]


@pytest.mark.parametrize("backend_name", ["fs", "sqlite"])
def test_explicit_child_transaction_joins_active_outer_transaction(
    tmp_path: Path,
    backend_name: str,
) -> None:
    repo, _backend, repo_path = _repo_and_backend(tmp_path, backend_name)

    with repo.transaction() as outer_tx:
        with repo.users["alice"].transaction() as child_tx:
            assert child_tx.parent is outer_tx
            assert child_tx.root is outer_tx
            repo.users["alice"].profile.write_text("alice")
        repo.readme.write_text("root")

    if backend_name == "fs":
        assert (repo_path / "README.md").read_text(encoding="utf-8") == "root"
        assert (repo_path / "users" / "alice" / "profile").read_text(encoding="utf-8") == "alice"
    else:
        names = SqliteConfig().schema_names
        connection = sqlite3.connect(str(repo_path))
        try:
            rows = list(connection.execute(f'SELECT repo_path, key, value FROM "{names.objects}" ORDER BY repo_path, key'))
        finally:
            connection.close()
        assert rows == [
            ("", "README.md", b"root"),
            ("users/alice", "profile", b"alice"),
        ]


@pytest.mark.parametrize("backend_name", ["fs", "sqlite"])
def test_reentering_same_child_transaction_reuses_enrolled_participant(
    tmp_path: Path,
    backend_name: str,
) -> None:
    repo, _backend, repo_path = _repo_and_backend(tmp_path, backend_name)

    with repo.transaction():
        with repo.users["alice"].transaction() as first:
            repo.users["alice"].profile.write_text("one")
        with repo.users["alice"].transaction() as second:
            assert second is first
            repo.users["alice"].profile.write_text("two")

    if backend_name == "fs":
        assert (repo_path / "users" / "alice" / "profile").read_text(encoding="utf-8") == "two"
    else:
        names = SqliteConfig().schema_names
        connection = sqlite3.connect(str(repo_path))
        try:
            rows = list(connection.execute(f'SELECT repo_path, key, value FROM "{names.objects}" ORDER BY repo_path, key'))
        finally:
            connection.close()
        assert rows == [("users/alice", "profile", b"two")]


@pytest.mark.parametrize("backend_name", ["fs", "sqlite"])
def test_child_transaction_object_cannot_be_reentered_after_root_finishes(
    tmp_path: Path,
    backend_name: str,
) -> None:
    repo, _backend, _repo_path = _repo_and_backend(tmp_path, backend_name)

    with repo.transaction():
        child_tx = repo.users["alice"].transaction()
        with child_tx:
            repo.users["alice"].profile.write_text("alice")

    with pytest.raises(TransactionStateError, match="Coordinated child transaction is no longer active"):
        child_tx.__enter__()


@pytest.mark.parametrize("backend_name", ["fs", "sqlite"])
def test_read_only_child_access_does_not_enroll_child_transaction(
    tmp_path: Path,
    backend_name: str,
) -> None:
    repo, _backend, repo_path = _repo_and_backend(tmp_path, backend_name)

    if backend_name == "fs":
        child_path = repo_path / "users" / "alice" / "profile"
        child_path.parent.mkdir(parents=True, exist_ok=True)
        child_path.write_text("old", encoding="utf-8")
    else:
        names = SqliteConfig().schema_names
        connection = sqlite3.connect(str(repo_path))
        try:
            connection.execute(
                f'CREATE TABLE IF NOT EXISTS "{names.objects}" '
                "(repo_path TEXT NOT NULL, key TEXT NOT NULL, value BLOB NOT NULL, PRIMARY KEY(repo_path, key))"
            )
            connection.execute(
                f'INSERT INTO "{names.objects}"(repo_path, key, value) VALUES (?, ?, ?)',
                ("users/alice", "profile", b"old"),
            )
            connection.commit()
        finally:
            connection.close()

    with repo.transaction():
        assert repo.users["alice"].profile.read_text() == "old"
        assert repo.users["alice"].active_transaction is None


@pytest.mark.parametrize("backend_name", ["fs", "sqlite"])
def test_untyped_escape_hatch_interoperates_with_schema_bound_children(
    tmp_path: Path,
    backend_name: str,
) -> None:
    repo, _backend, repo_path = _repo_and_backend(tmp_path, backend_name)

    with repo.transaction():
        repo.file("raw/data.txt").write_text("raw")
        repo.users["alice"].profile.write_text("alice")

    if backend_name == "fs":
        assert (repo_path / "raw" / "data.txt").read_text(encoding="utf-8") == "raw"
        assert (repo_path / "users" / "alice" / "profile").read_text(encoding="utf-8") == "alice"
    else:
        names = SqliteConfig().schema_names
        connection = sqlite3.connect(str(repo_path))
        try:
            rows = list(connection.execute(f'SELECT repo_path, key, value FROM "{names.objects}" ORDER BY repo_path, key'))
        finally:
            connection.close()
        assert rows == [
            ("", "raw/data.txt", b"raw"),
            ("users/alice", "profile", b"alice"),
        ]


def test_child_transaction_exception_preserves_primary_error_when_root_rollback_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo = AppRepo.open(str(repo_root), backend=FilesystemBackend())
    child_backend: Any | None = None
    from alpenstock.storage.backends.fs import FilesystemBackendTransaction

    original_rollback = FilesystemBackendTransaction.rollback

    def patched_rollback(self: FilesystemBackendTransaction) -> None:
        if self is child_backend:
            raise RuntimeError("child rollback failed")
        original_rollback(self)

    with pytest.raises(RuntimeError, match="boom") as exc_info:
        with repo.transaction():
            with repo.users["alice"].transaction() as child_tx:
                repo.users["alice"].profile.write_text("alice")
                child_backend = child_tx.backend_tx
                monkeypatch.setattr(FilesystemBackendTransaction, "rollback", patched_rollback)
                raise RuntimeError("boom")

    notes = getattr(exc_info.value, "__notes__", [])
    assert any("child rollback failed" in note for note in notes)


def test_define_rejects_invalid_declared_schema_types_at_decoration_time() -> None:
    with pytest.raises(Exception, match="Unsupported storage schema declaration type"):
        @define
        class BadRepo(Repo):
            bad: int


def test_define_rejects_reserved_public_runtime_field_names() -> None:
    with pytest.raises(Exception, match="reserved for runtime internals"):
        @define
        class BadRepo(Repo):
            backend: FileNode


def test_named_merges_with_existing_metadata() -> None:
    renamed = named("renamed.txt", metadata={"extra": "value"})

    assert renamed.metadata["storage_name"] == "renamed.txt"
    assert renamed.metadata["extra"] == "value"
