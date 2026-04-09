from __future__ import annotations

from functools import lru_cache
import sys
from typing import Any, Callable, TypeVar, get_args, get_origin, get_type_hints, overload

import attrs
from attr._make import _CountingAttr

from ._errors import ValueContractError

STORAGE_NAME_METADATA_KEY = "storage_name"
TClass = TypeVar("TClass", bound=type[Any])


def named(name: str, /, **kwargs: Any) -> Any:
    raw_metadata = kwargs.pop("metadata", None)
    metadata: dict[str, Any] = {}
    if raw_metadata is not None:
        metadata.update(dict(raw_metadata))
    metadata[STORAGE_NAME_METADATA_KEY] = name
    return attrs.field(
        init=False,
        metadata=metadata,
        **kwargs,
    )


@overload
def define(maybe_cls: TClass, /, **kwargs: Any) -> TClass: ...


@overload
def define(maybe_cls: None = None, /, **kwargs: Any) -> Callable[[TClass], TClass]: ...


def define(maybe_cls: TClass | None = None, /, **kwargs: Any) -> Any:
    def decorator(cls: TClass) -> TClass:
        reserved_public_names = {"backend", "repo_locator"}
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
                setattr(cls, attr_name, attrs.field(init=False))
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
    from ._backend import Backend

    globalns.setdefault("Backend", Backend)
    globalns.setdefault("Dir", Dir)
    globalns.setdefault("FileNode", FileNode)
    globalns.setdefault("MappedDir", MappedDir)
    globalns.setdefault("MappedRepo", MappedRepo)
    globalns.setdefault("Repo", Repo)
    globalns.setdefault("TransactionContext", TransactionContext)
    return get_type_hints(cls, globalns=globalns, include_extras=True)


def declared_schema_attributes(cls: type[Any]) -> tuple[attrs.Attribute[Any], ...]:
    return tuple(
        attribute
        for attribute in attrs.fields(cls)
        if not attribute.name.startswith("_") and attribute.name not in {"backend", "repo_locator"}
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


__all__ = [
    "STORAGE_NAME_METADATA_KEY",
    "classify_declared_type",
    "declared_schema_attributes",
    "define",
    "named",
    "resolved_schema_hints",
    "storage_name_for_attribute",
]
