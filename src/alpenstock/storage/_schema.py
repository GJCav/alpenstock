from __future__ import annotations

from functools import lru_cache
import sys
from collections.abc import Mapping
from typing import Any, Callable, TypeVar, get_args, get_origin, get_type_hints, overload

import attrs
from attr._make import _CountingAttr

from ._errors import ValueContractError
from ._keys import join_logical_key

STORAGE_NAME_METADATA_KEY = "storage_name"
TClass = TypeVar("TClass", bound=type[Any])


def field(
    *,
    name: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    if kwargs.pop("init", False) is not False:
        raise ValueContractError("storage.field() always uses init=False")
    raw_metadata = metadata
    merged_metadata: dict[str, Any] = {}
    if raw_metadata is not None:
        merged_metadata.update(dict(raw_metadata))
    if name is not None:
        merged_metadata[STORAGE_NAME_METADATA_KEY] = name
    return attrs.field(
        init=False,
        metadata=merged_metadata,
        **kwargs,
    )


@overload
def define(maybe_cls: TClass, /, **kwargs: Any) -> TClass: ...


@overload
def define(maybe_cls: None = None, /, **kwargs: Any) -> Callable[[TClass], TClass]: ...


def define(maybe_cls: type[Any] | None = None, /, **kwargs: Any) -> Any:
    def decorator(cls: TClass) -> TClass:
        reserved_public_names = {
            "backend",
            "blob_backend",
            "coordination_root_locator",
            "journal_backend",
            "repo_locator",
        }
        for attr_name in getattr(cls, "__annotations__", {}):
            if attr_name in reserved_public_names:
                raise ValueContractError(
                    f"Public storage schema field name {attr_name!r} is reserved for runtime internals"
                )
        for attr_name in getattr(cls, "__annotations__", {}):
            if attr_name.startswith("_"):
                continue
            existing = cls.__dict__.get(attr_name, attrs.NOTHING)
            if existing is attrs.NOTHING:
                setattr(cls, attr_name, field())
                continue
            if isinstance(existing, _CountingAttr):
                existing.init = False
        wrapped_cls = attrs.define(slots=True, **kwargs)(cls)
        hints = resolved_schema_hints(wrapped_cls)
        for attribute in declared_schema_attributes(wrapped_cls):
            classify_declared_type(hints[attribute.name])
        return wrapped_cls

    if maybe_cls is None:
        return decorator
    return decorator(maybe_cls)


def storage_name_for_attribute(attribute: attrs.Attribute[Any]) -> str:
    raw_name = attribute.metadata.get(STORAGE_NAME_METADATA_KEY, attribute.name)
    if not isinstance(raw_name, str) or raw_name == "":
        raise ValueContractError(f"Invalid storage name metadata for field {attribute.name!r}")
    return raw_name


@lru_cache(maxsize=None)
def resolved_schema_hints(cls: type[Any]) -> dict[str, Any]:
    globalns = dict(vars(sys.modules[cls.__module__]))
    from .dir import Dir, MappedDir, MappedRepo
    from .file import FileNode
    from .repo import Repo
    from .transaction import TransactionContext
    from ._blob_backend import BlobBackend
    from ._journal_backend import JournalBackend

    globalns.setdefault("BlobBackend", BlobBackend)
    globalns.setdefault("Dir", Dir)
    globalns.setdefault("FileNode", FileNode)
    globalns.setdefault("MappedDir", MappedDir)
    globalns.setdefault("MappedRepo", MappedRepo)
    globalns.setdefault("Repo", Repo)
    globalns.setdefault("JournalBackend", JournalBackend)
    globalns.setdefault("TransactionContext", TransactionContext)
    return get_type_hints(cls, globalns=globalns, include_extras=True)


def declared_schema_attributes(cls: type[Any]) -> tuple[attrs.Attribute[Any], ...]:
    return tuple(
        attribute
        for attribute in attrs.fields(cls)
        if not attribute.name.startswith("_")
        and attribute.name
        not in {"backend", "blob_backend", "coordination_root_locator", "journal_backend", "repo_locator"}
    )


def classify_declared_type(tp: Any) -> tuple[str, Any]:
    from .dir import Dir, MappedDir, MappedRepo
    from .file import FileNode
    from .repo import Repo

    origin = get_origin(tp)
    args = get_args(tp)

    if origin is MappedDir:
        if len(args) != 1:
            raise ValueContractError("MappedDir declarations require exactly one type argument")
        return "mapped_dir", args[0]
    if origin is MappedRepo:
        if len(args) != 1:
            raise ValueContractError("MappedRepo declarations require exactly one type argument")
        return "mapped_repo", args[0]

    if isinstance(tp, type):
        if issubclass(tp, Repo):
            return "repo", tp
        if issubclass(tp, Dir):
            return "dir", tp
        if issubclass(tp, FileNode):
            return "file", tp

    raise ValueContractError(f"Unsupported storage schema declaration type: {tp!r}")


def repo_boundary_prefixes(cls: type[Any], prefix: str = "") -> tuple[str, ...]:
    prefixes: list[str] = []
    hints = resolved_schema_hints(cls)
    for attribute in declared_schema_attributes(cls):
        storage_name = storage_name_for_attribute(attribute)
        child_prefix = join_logical_key(prefix, storage_name) if prefix else storage_name
        kind, target = classify_declared_type(hints[attribute.name])
        if kind in {"repo", "mapped_repo"}:
            prefixes.append(child_prefix)
        elif kind == "dir":
            prefixes.extend(repo_boundary_prefixes(target, child_prefix))
        elif kind == "mapped_dir" and _type_has_repo_boundary(target):
            prefixes.append(child_prefix)
    return tuple(prefixes)


def _type_has_repo_boundary(tp: Any) -> bool:
    kind, target = classify_declared_type(tp)
    if kind in {"repo", "mapped_repo"}:
        return True
    if kind == "dir":
        return bool(repo_boundary_prefixes(target))
    return False


__all__ = [
    "STORAGE_NAME_METADATA_KEY",
    "classify_declared_type",
    "declared_schema_attributes",
    "define",
    "field",
    "repo_boundary_prefixes",
    "resolved_schema_hints",
    "storage_name_for_attribute",
]
