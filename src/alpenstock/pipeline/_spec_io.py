from __future__ import annotations

from dataclasses import asdict as dataclass_asdict
from dataclasses import is_dataclass
from io import StringIO
from pathlib import Path
from typing import Any

import attrs
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
