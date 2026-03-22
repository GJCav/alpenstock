from __future__ import annotations

from dataclasses import asdict as dataclass_asdict
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from io import StringIO
from pathlib import Path
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints
import types

import attrs
from pydantic import BaseModel
from ruamel.yaml import YAML


YamlScalar = str | int | float | bool | None
YamlValue = YamlScalar | list["YamlValue"] | dict[str, "YamlValue"]


_yaml = YAML(typ="safe")
_yaml.default_flow_style = False



def _normalize_mapping(mapping: dict[Any, Any], *, path: str) -> dict[str, YamlValue]:
    normalized: dict[str, YamlValue] = {}
    for key, value in mapping.items():
        if not isinstance(key, str):
            raise TypeError(f"Spec key at {path} must be str, got {type(key)!r}")
        normalized[key] = normalize_spec_value(value, path=f"{path}.{key}")
    return normalized



def normalize_spec_value(value: Any, *, path: str = "spec") -> YamlValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, BaseModel):
        return _normalize_mapping(value.model_dump(mode="python"), path=path)

    if attrs.has(type(value)):
        attrs_data = attrs.asdict(value, recurse=False)
        return _normalize_mapping(attrs_data, path=path)

    if is_dataclass(value) and not isinstance(value, type):
        return _normalize_mapping(dataclass_asdict(value), path=path)

    if isinstance(value, dict):
        return _normalize_mapping(value, path=path)

    if isinstance(value, (list, tuple)):
        return [normalize_spec_value(item, path=f"{path}[]") for item in value]

    raise TypeError(
        f"Unsupported spec value at {path}: {type(value)!r}. "
        "Supported: attrs/dataclass/dict/list/tuple/scalars"
    )



def write_spec_file(path: Path, payload: dict[str, Any]) -> None:
    from ._state_io import atomic_write_text

    buf = StringIO()
    _yaml.dump(payload, buf)
    text = buf.getvalue()
    atomic_write_text(path, text)



def load_spec_file(path: Path) -> dict[str, Any]:
    data = _yaml.load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid spec file content in {path}")
    return data


def deserialize_spec_value(value: Any, annotation: Any, *, path: str = "spec") -> Any:
    if annotation is Any or annotation is None:
        return value

    if isinstance(annotation, str):
        return value

    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        if args:
            return deserialize_spec_value(value, args[0], path=path)
        return value

    if origin in (Union, types.UnionType):
        last_error: Exception | None = None
        for candidate in get_args(annotation):
            try:
                return deserialize_spec_value(value, candidate, path=path)
            except (TypeError, ValueError) as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        return value

    if annotation is type(None):
        if value is not None:
            raise TypeError(f"Expected None at {path}, got {type(value)!r}")
        return None

    if annotation in (str, int, float, bool):
        return _deserialize_scalar(value, annotation, path=path)

    if origin is list:
        if not isinstance(value, list):
            raise TypeError(f"Expected list at {path}, got {type(value)!r}")
        args = get_args(annotation)
        item_annotation = args[0] if args else Any
        return [
            deserialize_spec_value(item, item_annotation, path=f"{path}[]")
            for item in value
        ]

    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"Expected list/tuple at {path}, got {type(value)!r}")
        items = list(value)
        args = get_args(annotation)
        if not args:
            return tuple(items)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(
                deserialize_spec_value(item, args[0], path=f"{path}[]")
                for item in items
            )
        if len(items) != len(args):
            raise TypeError(
                f"Expected tuple of length {len(args)} at {path}, got {len(items)}"
            )
        return tuple(
            deserialize_spec_value(item, item_annotation, path=f"{path}[{index}]")
            for index, (item, item_annotation) in enumerate(zip(items, args))
        )

    if origin is dict:
        if not isinstance(value, dict):
            raise TypeError(f"Expected dict at {path}, got {type(value)!r}")
        args = get_args(annotation)
        key_annotation = args[0] if len(args) >= 1 else Any
        value_annotation = args[1] if len(args) >= 2 else Any

        result: dict[Any, Any] = {}
        for key, item in value.items():
            if key_annotation in (Any, str):
                new_key = key
            else:
                new_key = deserialize_spec_value(key, key_annotation, path=f"{path}.<key>")
            result[new_key] = deserialize_spec_value(
                item,
                value_annotation,
                path=f"{path}.{key}",
            )
        return result

    if _is_pydantic_model(annotation):
        if not isinstance(value, dict):
            raise TypeError(f"Expected dict for pydantic model at {path}, got {type(value)!r}")
        return annotation.model_validate(value)

    if _is_attrs_class(annotation):
        if not isinstance(value, dict):
            raise TypeError(f"Expected dict for attrs class at {path}, got {type(value)!r}")
        return annotation(**_build_attrs_ctor_kwargs(annotation, value))

    if _is_dataclass_type(annotation):
        if not isinstance(value, dict):
            raise TypeError(f"Expected dict for dataclass at {path}, got {type(value)!r}")

        hints = _safe_get_type_hints(annotation)
        kwargs: dict[str, Any] = {}
        for field in dataclass_fields(annotation):
            if not field.init or field.name not in value:
                continue
            field_annotation = hints.get(field.name, field.type)
            kwargs[field.name] = deserialize_spec_value(
                value[field.name],
                field_annotation,
                path=f"{path}.{field.name}",
            )
        return annotation(**kwargs)

    return value


def _deserialize_scalar(value: Any, annotation: type[Any], *, path: str) -> Any:
    if annotation is bool:
        if not isinstance(value, bool):
            raise TypeError(f"Expected bool at {path}, got {type(value)!r}")
        return value

    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"Expected int at {path}, got {type(value)!r}")
        return value

    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"Expected float at {path}, got {type(value)!r}")
        return float(value)

    if annotation is str:
        if not isinstance(value, str):
            raise TypeError(f"Expected str at {path}, got {type(value)!r}")
        return value

    return value


def _is_dataclass_type(annotation: Any) -> bool:
    return isinstance(annotation, type) and is_dataclass(annotation)


def _is_attrs_class(annotation: Any) -> bool:
    return isinstance(annotation, type) and attrs.has(annotation)


def _is_pydantic_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _build_attrs_ctor_kwargs(annotation: type[Any], value: dict[str, Any]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    for field in attrs.fields(annotation):
        field_alias = getattr(field, "alias", None)
        source_key: str | None = None
        if field.name in value:
            source_key = field.name
        elif isinstance(field_alias, str) and field_alias in value:
            source_key = field_alias

        if source_key is None or not field.init:
            continue

        ctor_key = field_alias if isinstance(field_alias, str) else field.name
        kwargs[ctor_key] = value[source_key]
    return kwargs


def _safe_get_type_hints(obj: Any) -> dict[str, Any]:
    try:
        return get_type_hints(obj, include_extras=True)
    except Exception:
        return {}
