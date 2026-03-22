from __future__ import annotations

import inspect
from typing import Any, Literal

import attrs

PIPELINE_KIND_METADATA_KEY = "alpenstock.pipeline.kind"
PipelineFieldKind = Literal["spec", "state", "output", "input", "transient"]
_ATTRS_FIELD_SIGNATURE = inspect.signature(attrs.field)


def _merge_metadata(
    metadata: dict[str, Any] | None,
    *,
    kind: PipelineFieldKind,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if metadata is not None:
        merged.update(metadata)
    merged[PIPELINE_KIND_METADATA_KEY] = kind
    return merged


def _pipeline_field(kind: PipelineFieldKind, **kwargs: Any) -> Any:
    metadata = _merge_metadata(kwargs.pop("metadata", None), kind=kind)
    kwargs["metadata"] = metadata
    return attrs.field(**kwargs)


def _expose_attrs_field_api(fn: Any) -> Any:
    """
    Copy public API metadata from `attrs.field` for better IDE tooltips.
    """
    if fn.__doc__ is None:
        fn.__doc__ = attrs.field.__doc__
    fn.__signature__ = _ATTRS_FIELD_SIGNATURE
    fn.__wrapped__ = attrs.field
    return fn


@_expose_attrs_field_api
def spec(**kwargs: Any) -> Any:
    """
    Like ``attrs.field()``, but marks the field as pipeline ``spec``.

    Additional restrictions:
    - ``on_setattr`` cannot be overridden; spec fields are always frozen.
    - ``init=False`` is not allowed; spec fields must be constructor-initialized.
    """
    if "on_setattr" in kwargs:
        raise ValueError(
            "spec() does not allow overriding on_setattr; spec fields are always frozen"
        )
    if kwargs.get("init") is False:
        raise ValueError(
            "spec() does not allow init=False; "
            "spec fields must be constructor-initialized"
        )
    kwargs["on_setattr"] = attrs.setters.frozen
    return _pipeline_field("spec", **kwargs)


@_expose_attrs_field_api
def state(**kwargs: Any) -> Any:
    return _pipeline_field("state", **kwargs)


@_expose_attrs_field_api
def output(**kwargs: Any) -> Any:
    return _pipeline_field("output", **kwargs)


@_expose_attrs_field_api
def input(**kwargs: Any) -> Any:
    """
    Like ``attrs.field()``, but marks the field as pipeline ``input``.

    Additional restrictions:
    - ``init=False`` is not allowed; input fields must be constructor-initialized.
    """
    if kwargs.get("init") is False:
        raise ValueError(
            "input() does not allow init=False; "
            "input fields must be constructor-initialized"
        )
    return _pipeline_field("input", **kwargs)


@_expose_attrs_field_api
def transient(**kwargs: Any) -> Any:
    return _pipeline_field("transient", **kwargs)
