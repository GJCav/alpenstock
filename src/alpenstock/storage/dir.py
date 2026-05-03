from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

import attrs

from ._keys import join_logical_key, validate_logical_key
from ._schema import (
    classify_declared_type,
    declared_schema_attributes,
    resolved_schema_hints,
    storage_name_for_attribute,
)
from .file import FileNode

if TYPE_CHECKING:
    from .repo import Repo

TNode = TypeVar("TNode")
TRepo = TypeVar("TRepo", bound="Repo")


def _validate_component(name: str) -> str:
    normalized = validate_logical_key(name)
    if len(PurePosixPath(normalized).parts) != 1:
        raise ValueError(f"Mapped names must be a single path component, got {name!r}")
    return normalized


@attrs.define(slots=True)
class Dir:
    _repo: Repo = attrs.field(init=False, repr=False)
    _prefix: str = attrs.field(default="", init=False, repr=False)

    def file(self, key: str) -> FileNode:
        """Return a raw file node under this directory for internal/debug/testing use.

        Prefer schema-declared ``FileNode`` attributes in application code.
        This raw escape hatch only enforces repo-boundary ownership checks; it
        does not make arbitrary filesystem shape conflicts part of the public
        schema contract.
        """
        return FileNode(repo=self._repo, key=self._repo._assert_raw_key_allowed(self._join(key)))

    def _bind(self, repo: Repo, prefix: str) -> Dir:
        self._repo = repo
        self._prefix = prefix
        self._bind_declared_children()
        return self

    def _join(self, key: str) -> str:
        return join_logical_key(self._prefix, key) if self._prefix else validate_logical_key(key)

    def _bind_declared_children(self) -> None:
        attributes = declared_schema_attributes(type(self))
        if not attributes:
            return
        hints = resolved_schema_hints(type(self))
        for attribute in attributes:
            storage_name = storage_name_for_attribute(attribute)
            declared_type = hints[attribute.name]
            kind, target = classify_declared_type(declared_type)
            child_prefix = self._join(storage_name)
            bound_value = self._bind_declared_child(kind, target, child_prefix)
            object.__setattr__(self, attribute.name, bound_value)

    def _bind_declared_child(self, kind: str, target: Any, child_prefix: str) -> Any:
        from .repo import Repo

        if kind == "file":
            file_type = cast(type[FileNode], target)
            return file_type(repo=self._repo, key=child_prefix)
        if kind == "dir":
            dir_type = cast(type[Dir], target)
            return dir_type()._bind(repo=self._repo, prefix=child_prefix)
        if kind == "repo":
            repo_type = cast(type[Repo], target)
            parent_repo = self._repo
            child_locator = parent_repo.blob_backend.child_repo_locator(parent_repo.repo_locator, child_prefix)
            coordination_root = parent_repo.journal_backend.prepare_repo_open(
                child_locator,
                parent_repo.blob_backend,
                parent_repo_locator=parent_repo.repo_locator,
                child_repo_path=child_prefix,
            )
            child_repo = repo_type(
                blob_backend=parent_repo.blob_backend,
                journal_backend=parent_repo.journal_backend,
                repo_locator=child_locator,
                coordination_root_locator=coordination_root,
            )
            child_repo._bind_schema_runtime(parent_repo=parent_repo, child_repo_path=child_prefix)
            return child_repo
        if kind == "mapped_dir":
            mapped = MappedDir[Any]()
            return mapped._bind(repo=self._repo, prefix=child_prefix, node_type=target)
        if kind == "mapped_repo":
            mapped_repo = MappedRepo[Any]()
            return mapped_repo._bind(repo=self._repo, prefix=child_prefix, repo_type=target)
        raise AssertionError(f"Unknown schema child kind {kind!r}")


@attrs.define(slots=True)
class MappedDir(Generic[TNode]):
    _repo: Repo = attrs.field(init=False, repr=False)
    _prefix: str = attrs.field(init=False, repr=False)
    _node_type: Any = attrs.field(init=False, repr=False)
    _cache: dict[str, TNode] = attrs.field(factory=dict, init=False, repr=False)

    def _bind(self, repo: Repo, prefix: str, node_type: Any) -> MappedDir[TNode]:
        self._repo = repo
        self._prefix = prefix
        self._node_type = node_type
        return self

    def __getitem__(self, key: str) -> TNode:
        component = _validate_component(key)
        cached = self._cache.get(component)
        if cached is not None:
            return cached

        joined = join_logical_key(self._prefix, component)
        kind, target = classify_declared_type(self._node_type)
        if kind == "repo":
            raise ValueError("MappedDir cannot contain repo transaction domains; use MappedRepo instead")

        if kind == "file":
            bound = cast(TNode, cast(type[FileNode], target)(repo=self._repo, key=joined))
        elif kind == "dir":
            bound = cast(TNode, cast(type[Dir], target)()._bind(repo=self._repo, prefix=joined))
        else:
            raise ValueError(f"MappedDir does not support nested mapped declarations, got {self._node_type!r}")

        self._cache[component] = bound
        return bound


@attrs.define(slots=True)
class MappedRepo(Generic[TRepo]):
    _repo: Repo = attrs.field(init=False, repr=False)
    _prefix: str = attrs.field(init=False, repr=False)
    _repo_type: type[TRepo] = attrs.field(init=False, repr=False)
    _cache: dict[str, TRepo] = attrs.field(factory=dict, init=False, repr=False)

    def _bind(self, repo: Repo, prefix: str, repo_type: type[TRepo]) -> MappedRepo[TRepo]:
        self._repo = repo
        self._prefix = prefix
        self._repo_type = repo_type
        return self

    def __getitem__(self, key: str) -> TRepo:
        component = _validate_component(key)
        cached = self._cache.get(component)
        if cached is not None:
            return cached

        child_repo_path = join_logical_key(self._prefix, component)
        child_locator = self._repo.blob_backend.child_repo_locator(self._repo.repo_locator, child_repo_path)
        coordination_root = self._repo.journal_backend.prepare_repo_open(
            child_locator,
            self._repo.blob_backend,
            parent_repo_locator=self._repo.repo_locator,
            child_repo_path=child_repo_path,
        )
        child_repo = self._repo_type(
            blob_backend=self._repo.blob_backend,
            journal_backend=self._repo.journal_backend,
            repo_locator=child_locator,
            coordination_root_locator=coordination_root,
        )
        child_repo._bind_schema_runtime(parent_repo=self._repo, child_repo_path=child_repo_path)
        self._cache[component] = child_repo
        return child_repo


__all__ = ["Dir", "MappedDir", "MappedRepo"]
