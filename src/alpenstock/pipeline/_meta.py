from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints
import inspect
import types

import attrs

from ._fields import PIPELINE_KIND_METADATA_KEY, PipelineFieldKind


STAGE_FN_ATTR = "__alpenstock_pipeline_stage_id__"
STAGE_ORDER_ATTR = "__alpenstock_pipeline_stage_order__"
STAGE_ORIGINAL_FN_ATTR = "__alpenstock_pipeline_original_fn__"
PIPELINE_INTERNAL_FIELD_METADATA_KEY = "alpenstock.pipeline.internal"


@dataclass(frozen=True)
class PipelineMeta:
    save_path_field: str
    fields_by_kind: dict[PipelineFieldKind, tuple[str, ...]]
    field_schema: dict[str, PipelineFieldKind]
    stage_sequence: tuple[str, ...]
    stage_order_by_id: dict[str, int]


@dataclass(frozen=True)
class StageSpec:
    stage_id: str
    stage_order: int
    original_fn: Any



def get_field_kind(field: attrs.Attribute[Any]) -> PipelineFieldKind:
    kind = field.metadata.get(PIPELINE_KIND_METADATA_KEY, "transient")
    if kind not in {"spec", "state", "output", "input", "transient"}:
        raise ValueError(f"Unknown pipeline field kind {kind!r} for field {field.name!r}")
    return kind



def build_pipeline_meta(cls: type, *, save_path_field: str) -> PipelineMeta:
    if not attrs.has(cls):
        raise TypeError("define_pipeline can only be applied to attrs classes")

    attrs_fields = attrs.fields(cls)
    field_names = {f.name for f in attrs_fields}
    if save_path_field not in field_names:
        raise ValueError(f"save_path_field={save_path_field!r} is not a field on {cls.__name__}")

    field_schema: dict[str, PipelineFieldKind] = {}
    grouped: dict[PipelineFieldKind, list[str]] = {
        "spec": [],
        "state": [],
        "output": [],
        "input": [],
        "transient": [],
    }

    for field in attrs_fields:
        if field.metadata.get(PIPELINE_INTERNAL_FIELD_METADATA_KEY) is True:
            continue
        kind = get_field_kind(field)
        field_schema[field.name] = kind
        grouped[kind].append(field.name)
        if kind in {"spec", "input"} and not field.init:
            raise ValueError(
                f"Field {field.name!r} is marked as {kind} but has init=False. "
                f"{kind.capitalize()} fields must be constructor-initialized."
            )

    if field_schema[save_path_field] != "transient":
        raise ValueError(
            f"save_path_field={save_path_field!r} must be marked as transient(), "
            f"but got kind={field_schema[save_path_field]!r}"
        )

    _validate_save_path_annotation(cls, save_path_field)

    stage_specs = _collect_stage_specs(cls)
    stage_sequence = tuple(spec.stage_id for spec in stage_specs)
    stage_order_by_id = {spec.stage_id: spec.stage_order for spec in stage_specs}

    return PipelineMeta(
        save_path_field=save_path_field,
        fields_by_kind={k: tuple(v) for k, v in grouped.items()},
        field_schema=field_schema,
        stage_sequence=stage_sequence,
        stage_order_by_id=stage_order_by_id,
    )



def _collect_stage_specs(cls: type) -> tuple[StageSpec, ...]:
    stage_specs: list[StageSpec] = []
    seen_stage_ids: set[str] = set()
    seen_stage_orders: set[int] = set()

    for _, value in _iter_visible_stage_methods(cls):
        stage_id = getattr(value, STAGE_FN_ATTR, None)
        stage_order = getattr(value, STAGE_ORDER_ATTR, None)

        if not isinstance(stage_id, str):
            raise TypeError(
                f"Stage method on {cls.__name__} has invalid stage id metadata {stage_id!r}"
            )
        if not isinstance(stage_order, int) or isinstance(stage_order, bool):
            raise TypeError(
                f"Stage {stage_id!r} must declare integer order, got {stage_order!r}"
            )
        if stage_order < 0:
            raise ValueError(f"Stage {stage_id!r} order must be >= 0, got {stage_order}")

        if stage_id in seen_stage_ids:
            raise ValueError(
                f"Duplicated stage id {stage_id!r} in class hierarchy of {cls.__name__}"
            )
        if stage_order in seen_stage_orders:
            raise ValueError(
                f"Duplicated stage order {stage_order!r} in class hierarchy of {cls.__name__}"
            )

        seen_stage_ids.add(stage_id)
        seen_stage_orders.add(stage_order)
        original_fn = getattr(value, STAGE_ORIGINAL_FN_ATTR, value)
        _validate_stage_signature(original_fn, stage_id=stage_id)
        stage_specs.append(
            StageSpec(
                stage_id=stage_id,
                stage_order=stage_order,
                original_fn=original_fn,
            )
        )

    stage_specs.sort(key=lambda item: item.stage_order)
    return tuple(stage_specs)


def _iter_visible_stage_methods(cls: type):
    seen_method_names: set[str] = set()
    for owner in cls.__mro__:
        if owner is object:
            continue
        for method_name, value in owner.__dict__.items():
            if method_name in seen_method_names:
                continue
            seen_method_names.add(method_name)
            if getattr(value, STAGE_FN_ATTR, None) is not None:
                yield method_name, value



def _validate_stage_signature(fn: Any, *, stage_id: str) -> None:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if len(params) != 1:
        raise TypeError(
            f"Stage {stage_id!r} must accept no arguments except self, got signature {sig}"
        )

    param = params[0]
    if param.kind not in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
        raise TypeError(f"Stage {stage_id!r} has invalid self parameter in signature {sig}")



def _validate_save_path_annotation(cls: type, field_name: str) -> None:
    try:
        hints = get_type_hints(cls, include_extras=True)
    except Exception:
        hints = {}

    annotation = hints.get(field_name)
    if annotation is None:
        raw_annotations = inspect.get_annotations(cls, eval_str=False)
        annotation = raw_annotations.get(field_name)
    if annotation is None:
        for field in attrs.fields(cls):
            if field.name == field_name:
                annotation = field.type
                break

    if annotation is None or not _is_valid_save_path_type(annotation):
        raise TypeError(
            f"Field {field_name!r} must be annotated as str | Path | None (or Optional variants)"
        )



def _is_valid_save_path_type(tp: Any) -> bool:
    if isinstance(tp, str):
        normalized = tp.replace(" ", "")
        token_set = set(normalized.replace("|", " ").split())
        allowed = {"str", "Path", "None", "NoneType"}
        if not token_set or not token_set.issubset(allowed):
            return False
        return "str" in token_set or "Path" in token_set

    origin = get_origin(tp)

    if str(origin) == "<class 'typing.Annotated'>":
        return _is_valid_save_path_type(get_args(tp)[0])

    if origin in (types.UnionType, Union):
        return all(_is_valid_save_path_type(arg) for arg in get_args(tp))

    if tp in (str, type(None)):
        return True

    if isinstance(tp, type) and issubclass(tp, Path):
        return True

    return False
