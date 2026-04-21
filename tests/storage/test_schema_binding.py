from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alpenstock.storage import Dir, FileNode, MappedDir, MappedRepo, Repo, TransactionStateError, define, field
from alpenstock.storage.backends.fs import JsonlWalTransaction, RepoLayout


@define
class ProjectDir(Dir):
    key: FileNode = field(name="key.txt")
    docs: MappedDir[FileNode]


@define
class UserRepo(Repo):
    profile: FileNode
    projects: MappedDir[ProjectDir]


@define
class SettingsDir(Dir):
    theme: FileNode = field(name="theme.toml")
    server: FileNode = field(name="server.json")


@define
class AppRepo(Repo):
    settings: SettingsDir
    users: MappedRepo[UserRepo]
    readme: FileNode = field(name="README.md")


def _repo(tmp_path: Path) -> tuple[AppRepo, Path]:
    repo_root = tmp_path / "repo"
    return AppRepo.open(str(repo_root)), repo_root


def test_schema_open_binds_declared_children_with_expected_runtime_types(tmp_path: Path) -> None:
    repo, _repo_path = _repo(tmp_path)

    assert isinstance(repo.settings, SettingsDir)
    assert isinstance(repo.readme, FileNode)
    assert isinstance(repo.users, MappedRepo)
    assert isinstance(repo.users["alice"], UserRepo)
    assert repo.readme.key == "README.md"
    assert repo.settings.theme.key == "settings/theme.toml"
    assert repo.settings.server.key == "settings/server.json"
    assert repo.users["alice"].profile.key == "profile"
    assert repo.users["alice"]._parent_repo is repo
    assert repo.users["alice"].coordination_root_locator == repo.repo_locator
    assert RepoLayout(repo.repo_locator).meta_path.exists()
    assert RepoLayout(repo.users["alice"].repo_locator).meta_path.exists()


def test_schema_bound_repo_commit_persists_root_and_child_repo_writes(tmp_path: Path) -> None:
    repo, repo_path = _repo(tmp_path)

    with repo.transaction():
        repo.readme.write_text("root")
        repo.settings.theme.write_text("light")
        repo.users["alice"].profile.write_text("alice")
        repo.users["alice"].projects["p1"].key.write_text("project")
        repo.users["alice"].projects["p1"].docs["note.txt"].write_text("note")

    layout = RepoLayout(repo_path)
    assert layout.committed_path("README.md").read_text(encoding="utf-8") == "root"
    assert layout.committed_path("settings/theme.toml").read_text(encoding="utf-8") == "light"
    assert (repo_path / "users" / "alice" / "profile").read_text(encoding="utf-8") == "alice"
    assert (repo_path / "users" / "alice" / "projects" / "p1" / "key.txt").read_text(encoding="utf-8") == "project"
    assert (repo_path / "users" / "alice" / "projects" / "p1" / "docs" / "note.txt").read_text(encoding="utf-8") == "note"


def test_explicit_child_transaction_joins_active_outer_transaction(tmp_path: Path) -> None:
    repo, repo_path = _repo(tmp_path)

    with repo.transaction() as outer_tx:
        with repo.users["alice"].transaction() as child_tx:
            assert child_tx.parent is outer_tx
            assert child_tx.root is outer_tx
            repo.users["alice"].profile.write_text("alice")
        repo.readme.write_text("root")

    assert (repo_path / "README.md").read_text(encoding="utf-8") == "root"
    assert (repo_path / "users" / "alice" / "profile").read_text(encoding="utf-8") == "alice"


def test_reentering_same_child_transaction_reuses_enrolled_participant(tmp_path: Path) -> None:
    repo, repo_path = _repo(tmp_path)

    with repo.transaction():
        with repo.users["alice"].transaction() as first:
            repo.users["alice"].profile.write_text("one")
        with repo.users["alice"].transaction() as second:
            assert second is first
            repo.users["alice"].profile.write_text("two")

    assert (repo_path / "users" / "alice" / "profile").read_text(encoding="utf-8") == "two"


def test_child_transaction_object_cannot_be_reentered_after_root_finishes(tmp_path: Path) -> None:
    repo, _repo_path = _repo(tmp_path)

    with repo.transaction():
        child_tx = repo.users["alice"].transaction()
        with child_tx:
            repo.users["alice"].profile.write_text("alice")

    with pytest.raises(TransactionStateError, match="Coordinated child transaction is no longer active"):
        child_tx.__enter__()


def test_read_only_child_access_does_not_enroll_child_transaction(tmp_path: Path) -> None:
    repo, repo_path = _repo(tmp_path)
    child_repo = repo.users["alice"]
    child_path = repo_path / "users" / "alice" / "profile"
    child_path.parent.mkdir(parents=True, exist_ok=True)
    child_path.write_text("old", encoding="utf-8")

    with repo.transaction():
        assert child_repo.profile.read_text() == "old"
        assert child_repo.active_transaction is None


def test_untyped_escape_hatch_interoperates_with_schema_bound_children(tmp_path: Path) -> None:
    repo, repo_path = _repo(tmp_path)

    with repo.transaction():
        repo.file("raw/data.txt").write_text("raw")
        repo.users["alice"].profile.write_text("alice")

    assert (repo_path / "raw" / "data.txt").read_text(encoding="utf-8") == "raw"
    assert (repo_path / "users" / "alice" / "profile").read_text(encoding="utf-8") == "alice"


def test_child_transaction_exception_preserves_primary_error_when_root_rollback_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo = AppRepo.open(str(repo_root))
    child_journal_tx: Any | None = None
    from alpenstock.storage.backends.fs import JsonlWalTransaction

    original_rollback = JsonlWalTransaction.rollback

    def patched_rollback(self: JsonlWalTransaction) -> None:
        if self is child_journal_tx:
            raise RuntimeError("child rollback failed")
        original_rollback(self)

    with pytest.raises(RuntimeError, match="boom") as exc_info:
        with repo.transaction():
            with repo.users["alice"].transaction() as child_tx:
                repo.users["alice"].profile.write_text("alice")
                child_journal_tx = child_tx.journal_tx
                monkeypatch.setattr(JsonlWalTransaction, "rollback", patched_rollback)
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
            blob_backend: FileNode


def test_field_merges_with_existing_metadata() -> None:
    renamed = field(name="renamed.txt", metadata={"extra": "value"})

    assert renamed.metadata["storage_name"] == "renamed.txt"
    assert renamed.metadata["extra"] == "value"


def test_field_without_name_is_schema_declarator() -> None:
    declared = field(metadata={"extra": "value"})

    assert "storage_name" not in declared.metadata
    assert declared.metadata["extra"] == "value"


def test_field_rejects_init_true() -> None:
    with pytest.raises(Exception, match="init=False"):
        field(init=True)
