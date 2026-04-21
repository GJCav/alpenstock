from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from uuid import uuid4

import attrs

from ..._errors import TransactionStateError
from ..._keys import validate_logical_key
from .layout import RepoLayout
from .locking import WriterLock


@attrs.define(frozen=True, slots=True)
class RepoTreeMetadata:
    tree_id: str
    journal_backend: str
    tree_root_relpath: str
    repo_path: str
    parent_repo_path: str | None

    def tree_root(self, layout: RepoLayout) -> Path:
        return (layout.repo_root / self.tree_root_relpath).resolve()


def _path_to_posix_relpath(path: Path, start: Path) -> str:
    return Path(os.path.relpath(path.resolve(), start=start.resolve())).as_posix()


def _join_repo_paths(parent_repo_path: str, child_repo_path: str) -> str:
    child_repo_path = validate_logical_key(child_repo_path)
    if parent_repo_path == ".":
        return child_repo_path
    return PurePosixPath(parent_repo_path, child_repo_path).as_posix()


def _repo_path_layout(tree_root: Path, repo_path: str) -> RepoLayout:
    if repo_path == ".":
        return RepoLayout(tree_root)
    return RepoLayout(tree_root.joinpath(*PurePosixPath(repo_path).parts))


def _validate_repo_path(value: object, *, field_name: str, allow_root: bool) -> str:
    if not isinstance(value, str) or not value:
        raise TransactionStateError(f"Malformed filesystem repo metadata: {field_name!r} must be a non-empty string")
    if allow_root and value == ".":
        return value
    try:
        return validate_logical_key(value)
    except Exception as exc:
        raise TransactionStateError(
            f"Malformed filesystem repo metadata: {field_name!r} is not a valid repo path"
        ) from exc


def _validate_tree_root_relpath(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TransactionStateError("Malformed filesystem repo metadata: 'tree_root_relpath' must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise TransactionStateError("Malformed filesystem repo metadata: 'tree_root_relpath' must be relative")
    if any(part == "" for part in path.parts):
        raise TransactionStateError("Malformed filesystem repo metadata: invalid 'tree_root_relpath'")
    return value


def _repo_is_empty_or_missing(layout: RepoLayout) -> bool:
    if not layout.repo_root.exists():
        return True
    if not layout.repo_root.is_dir():
        return False
    for path in layout.repo_root.iterdir():
        if path != layout.tx_root:
            return False
        if not _tx_root_contains_only_metadata_bootstrap_artifacts(layout):
            return False
    return True


def _tx_root_contains_only_metadata_bootstrap_artifacts(layout: RepoLayout) -> bool:
    if not layout.tx_root.exists():
        return True
    allowed_files = {layout.meta_lock_path, layout.meta_tmp_path}
    for path in layout.tx_root.rglob("*"):
        if path.is_dir():
            return False
        if path in allowed_files:
            continue
        if path.parent == layout.tx_root and path.name.startswith("meta.json.") and path.name.endswith(".tmp"):
            continue
        return False
    return True


def _write_metadata(layout: RepoLayout, metadata: RepoTreeMetadata) -> None:
    layout.ensure_tx_root()
    tmp_path = layout.new_meta_tmp_path()
    payload: dict[str, object] = {
        "journal_backend": metadata.journal_backend,
        "parent_repo_path": metadata.parent_repo_path,
        "repo_path": metadata.repo_path,
        "tree_id": metadata.tree_id,
        "tree_root_relpath": metadata.tree_root_relpath,
        "version": 1,
    }
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(layout.meta_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _with_metadata_lock(layout: RepoLayout, callback: Callable[[], RepoTreeMetadata]) -> RepoTreeMetadata:
    lock = WriterLock(layout.meta_lock_path, mode="exclusive", blocking=True)
    lock.acquire()
    try:
        return callback()
    finally:
        lock.release()


def read_metadata(layout: RepoLayout, journal_backend: str) -> RepoTreeMetadata | None:
    if not layout.meta_path.exists():
        return None
    try:
        with layout.meta_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise TransactionStateError(f"Invalid filesystem repo metadata JSON at {layout.meta_path}: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise TransactionStateError(f"Invalid filesystem repo metadata payload at {layout.meta_path}")
    if raw.get("version") != 1:
        raise TransactionStateError(f"Invalid filesystem repo metadata version at {layout.meta_path}")
    raw_journal = raw.get("journal_backend")
    if raw_journal != journal_backend:
        raise TransactionStateError(
            f"Filesystem repo metadata at {layout.meta_path} belongs to journal backend "
            f"{raw_journal!r}, not configured backend {journal_backend!r}"
        )
    tree_id = raw.get("tree_id")
    if not isinstance(tree_id, str) or not tree_id:
        raise TransactionStateError("Malformed filesystem repo metadata: 'tree_id' must be a non-empty string")
    repo_path = _validate_repo_path(raw.get("repo_path"), field_name="repo_path", allow_root=True)
    parent_repo_path_raw = raw.get("parent_repo_path")
    if parent_repo_path_raw is None:
        parent_repo_path = None
    else:
        parent_repo_path = _validate_repo_path(parent_repo_path_raw, field_name="parent_repo_path", allow_root=True)
    tree_root_relpath = _validate_tree_root_relpath(raw.get("tree_root_relpath"))
    metadata = RepoTreeMetadata(
        tree_id=tree_id,
        journal_backend=journal_backend,
        tree_root_relpath=tree_root_relpath,
        repo_path=repo_path,
        parent_repo_path=parent_repo_path,
    )
    _validate_metadata_matches_layout(layout, metadata)
    return metadata


def _validate_metadata_matches_layout(layout: RepoLayout, metadata: RepoTreeMetadata) -> None:
    tree_root = metadata.tree_root(layout)
    expected_repo_root = _repo_path_layout(tree_root, metadata.repo_path).repo_root.resolve()
    if expected_repo_root != layout.repo_root.resolve():
        raise TransactionStateError(
            f"Filesystem repo metadata at {layout.meta_path} maps repo path "
            f"{metadata.repo_path!r} to {str(expected_repo_root)!r}, not {str(layout.repo_root.resolve())!r}"
        )
    if metadata.repo_path == ".":
        if metadata.parent_repo_path is not None:
            raise TransactionStateError("Malformed filesystem repo metadata: root repo must not have parent_repo_path")
    elif metadata.parent_repo_path is None:
        raise TransactionStateError("Malformed filesystem repo metadata: child repo requires parent_repo_path")


def ensure_root_metadata(layout: RepoLayout, journal_backend: str) -> RepoTreeMetadata:
    existing = read_metadata(layout, journal_backend)
    if existing is not None:
        return existing

    def create_or_read() -> RepoTreeMetadata:
        existing_under_lock = read_metadata(layout, journal_backend)
        if existing_under_lock is not None:
            return existing_under_lock
        if not _repo_is_empty_or_missing(layout):
            raise TransactionStateError(
                f"Filesystem repo {str(layout.repo_root)!r} is missing .tx/meta.json; "
                "non-empty repos without metadata are considered corrupted or uninitialized"
            )
        metadata = RepoTreeMetadata(
            tree_id=str(uuid4()),
            journal_backend=journal_backend,
            tree_root_relpath=".",
            repo_path=".",
            parent_repo_path=None,
        )
        layout.ensure_repo_root()
        _write_metadata(layout, metadata)
        return metadata

    return _with_metadata_lock(layout, create_or_read)


def ensure_child_metadata(
    parent_layout: RepoLayout,
    child_layout: RepoLayout,
    child_repo_path: str,
    journal_backend: str,
) -> RepoTreeMetadata:
    parent_metadata = ensure_root_or_existing_metadata(parent_layout, journal_backend)
    expected_repo_path = _join_repo_paths(parent_metadata.repo_path, child_repo_path)
    existing = read_metadata(child_layout, journal_backend)
    if existing is not None:
        return _validate_child_metadata(existing, parent_metadata, expected_repo_path)

    def create_or_read() -> RepoTreeMetadata:
        existing_under_lock = read_metadata(child_layout, journal_backend)
        if existing_under_lock is not None:
            return _validate_child_metadata(existing_under_lock, parent_metadata, expected_repo_path)
        if not _repo_is_empty_or_missing(child_layout):
            raise TransactionStateError(
                f"Filesystem child repo {str(child_layout.repo_root)!r} is missing .tx/meta.json; "
                "non-empty repos without metadata are considered corrupted or uninitialized"
            )
        tree_root = parent_metadata.tree_root(parent_layout)
        metadata = RepoTreeMetadata(
            tree_id=parent_metadata.tree_id,
            journal_backend=journal_backend,
            tree_root_relpath=_path_to_posix_relpath(tree_root, child_layout.repo_root),
            repo_path=expected_repo_path,
            parent_repo_path=parent_metadata.repo_path,
        )
        child_layout.ensure_repo_root()
        _write_metadata(child_layout, metadata)
        return metadata

    return _with_metadata_lock(child_layout, create_or_read)


def _validate_child_metadata(
    existing: RepoTreeMetadata,
    parent_metadata: RepoTreeMetadata,
    expected_repo_path: str,
) -> RepoTreeMetadata:
    if existing.tree_id != parent_metadata.tree_id:
        raise TransactionStateError("Filesystem child repo metadata belongs to a different repo tree")
    if existing.repo_path != expected_repo_path:
        raise TransactionStateError(
            f"Filesystem child repo metadata has repo_path {existing.repo_path!r}, "
            f"expected {expected_repo_path!r}"
        )
    if existing.parent_repo_path != parent_metadata.repo_path:
        raise TransactionStateError(
            f"Filesystem child repo metadata has parent_repo_path {existing.parent_repo_path!r}, "
            f"expected {parent_metadata.repo_path!r}"
        )
    return existing


def ensure_root_or_existing_metadata(layout: RepoLayout, journal_backend: str) -> RepoTreeMetadata:
    existing = read_metadata(layout, journal_backend)
    if existing is not None:
        return existing
    return ensure_root_metadata(layout, journal_backend)


def read_required_metadata(layout: RepoLayout, journal_backend: str) -> RepoTreeMetadata:
    metadata = read_metadata(layout, journal_backend)
    if metadata is None:
        raise TransactionStateError(
            f"Filesystem repo {str(layout.repo_root)!r} is missing .tx/meta.json; "
            "repo tree metadata is required for hierarchical locking"
        )
    return metadata


def metadata_chain_root_to_target(layout: RepoLayout, journal_backend: str) -> list[tuple[RepoLayout, RepoTreeMetadata]]:
    target_metadata = ensure_root_or_existing_metadata(layout, journal_backend)
    tree_root = target_metadata.tree_root(layout)
    chain: list[tuple[RepoLayout, RepoTreeMetadata]] = []
    current_metadata = target_metadata
    while True:
        current_layout = _repo_path_layout(tree_root, current_metadata.repo_path)
        current_metadata = read_required_metadata(current_layout, journal_backend)
        if current_metadata.tree_id != target_metadata.tree_id:
            raise TransactionStateError("Filesystem repo metadata chain crosses repo tree identities")
        chain.append((current_layout, current_metadata))
        parent_repo_path = current_metadata.parent_repo_path
        if parent_repo_path is None:
            break
        parent_layout = _repo_path_layout(tree_root, parent_repo_path)
        parent_metadata = read_required_metadata(parent_layout, journal_backend)
        if parent_metadata.repo_path != parent_repo_path:
            raise TransactionStateError("Filesystem repo metadata parent chain is inconsistent")
        current_metadata = parent_metadata
    chain.reverse()
    return chain


def coordination_root_locator(layout: RepoLayout, journal_backend: str) -> str:
    metadata = ensure_root_or_existing_metadata(layout, journal_backend)
    return str(metadata.tree_root(layout))


__all__ = [
    "RepoTreeMetadata",
    "coordination_root_locator",
    "ensure_child_metadata",
    "ensure_root_metadata",
    "ensure_root_or_existing_metadata",
    "metadata_chain_root_to_target",
    "read_metadata",
    "read_required_metadata",
]
