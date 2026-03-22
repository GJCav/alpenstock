from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar, cast, dataclass_transform, get_type_hints, overload
import inspect
import re

import attrs
from ruamel.yaml import YAML

from ._fields import input, output, spec, state, transient
from ._meta import (
    PIPELINE_INTERNAL_FIELD_METADATA_KEY,
    STAGE_FN_ATTR,
    STAGE_ORDER_ATTR,
    STAGE_ORIGINAL_FN_ATTR,
    PipelineMeta,
    build_pipeline_meta,
)
from ._spec_io import deserialize_spec_value, load_spec_file, normalize_spec_value
from ._state_io import default_loader, default_saver


PIPELINE_META_ATTR = "__alpenstock_pipeline_meta__"
STAGE_FINISHED_PREFIX = "__stage_finished_"
SPEC_FILE_NAME = "spec.yaml"
_VALID_STAGE_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
_RUNTIME_FIELD_NAME = "_alpenstock_runtime_state"

_yaml = YAML(typ="safe")
_yaml.default_flow_style = False


@dataclass
class RuntimeState:
    bootstrapped: bool = False
    cache_enabled: bool = False
    save_path: Path | None = None
    finished_stages: set[str] = field(default_factory=set)
    running_stage_id: str | None = None
    read_only: bool = False


_C = TypeVar("_C", bound=type)
_P = TypeVar("_P")


@overload
@dataclass_transform(field_specifiers=(spec, state, output, input, transient, attrs.field))
def define_pipeline(
    maybe_cls: _C,
    *,
    save_path_field: str,
    kw_only: bool = False,
    **attrs_define_kwargs: Any,
) -> _C:
    ...


@overload
@dataclass_transform(field_specifiers=(spec, state, output, input, transient, attrs.field))
def define_pipeline(
    maybe_cls: None = ...,
    *,
    save_path_field: str,
    kw_only: bool = False,
    **attrs_define_kwargs: Any,
) -> Callable[[_C], _C]:
    ...


@dataclass_transform(field_specifiers=(spec, state, output, input, transient, attrs.field))
def define_pipeline(
    maybe_cls: _C | None = None,
    *,
    save_path_field: str,
    kw_only: bool = False,
    **attrs_define_kwargs: Any,
) -> _C | Callable[[_C], _C]:
    def decorator(cls: _C) -> _C:
        _inject_runtime_field(cls)
        wrapped_cls = cast(_C, attrs.define(kw_only=kw_only, **attrs_define_kwargs)(cls))
        meta = build_pipeline_meta(wrapped_cls, save_path_field=save_path_field)
        setattr(wrapped_cls, PIPELINE_META_ATTR, meta)
        return wrapped_cls

    if maybe_cls is not None:
        return decorator(maybe_cls)
    return decorator



def _inject_runtime_field(cls: type) -> None:
    annotations = dict(getattr(cls, "__annotations__", {}))
    if _RUNTIME_FIELD_NAME not in annotations:
        annotations[_RUNTIME_FIELD_NAME] = RuntimeState
        setattr(cls, "__annotations__", annotations)
    if _RUNTIME_FIELD_NAME in cls.__dict__:
        return
    setattr(
        cls,
        _RUNTIME_FIELD_NAME,
        attrs.field(
            init=False,
            factory=RuntimeState,
            repr=False,
            eq=False,
            hash=False,
            metadata={PIPELINE_INTERNAL_FIELD_METADATA_KEY: True},
        ),
    )



def stage_func(*, id: str, order: int):
    if not isinstance(id, str) or not id:
        raise ValueError("stage_func requires a non-empty string id")
    if _VALID_STAGE_ID_RE.fullmatch(id) is None:
        raise ValueError(
            "stage_func id must match ^[A-Za-z0-9_]+$ (letters, digits, underscore)"
        )
    if not isinstance(order, int) or isinstance(order, bool):
        raise TypeError(f"stage_func order must be int (excluding bool), got {type(order)!r}")
    if order < 0:
        raise ValueError(f"stage_func order must be >= 0, got {order}")

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(inspect.unwrap(fn)):
            @wraps(fn)
            async def wrapped(self: Any, *args: Any, **kwargs: Any) -> None:
                if args or kwargs:
                    raise TypeError(
                        f"Stage {id!r} does not accept arguments; pass data through input/state/output fields"
                    )
                return await _run_stage_async(self, stage_id=id, stage_order=order, fn=fn)
        else:
            @wraps(fn)
            def wrapped(self: Any, *args: Any, **kwargs: Any) -> None:
                if args or kwargs:
                    raise TypeError(
                        f"Stage {id!r} does not accept arguments; pass data through input/state/output fields"
                    )
                return _run_stage(self, stage_id=id, stage_order=order, fn=fn)

        setattr(wrapped, STAGE_FN_ATTR, id)
        setattr(wrapped, STAGE_ORDER_ATTR, order)
        setattr(wrapped, STAGE_ORIGINAL_FN_ATTR, fn)
        return wrapped

    return decorator



def get_state_dict(
    ins: Any,
    *,
    spec: bool = False,
    input: bool = False,
    state: bool = True,
    transient: bool = False,
    output: bool = True,
    include_finished_markers: bool = False,
) -> dict[str, Any]:
    meta = _require_meta(type(ins))
    result: dict[str, Any] = {}

    if spec:
        payload: dict[str, Any] = {}
        for name in meta.fields_by_kind["spec"]:
            payload[name] = normalize_spec_value(getattr(ins, name), path=f"spec.{name}")
        result["spec"] = payload

    if input:
        result["input"] = {
            name: getattr(ins, name)
            for name in meta.fields_by_kind["input"]
        }

    if state:
        result["state"] = {
            name: getattr(ins, name)
            for name in meta.fields_by_kind["state"]
        }

    if transient:
        result["transient"] = {
            name: getattr(ins, name)
            for name in meta.fields_by_kind["transient"]
        }

    if output:
        result["output"] = {
            name: getattr(ins, name)
            for name in meta.fields_by_kind["output"]
        }

    if include_finished_markers:
        runtime = _get_runtime_state(ins)
        _validate_finished_stage_markers(meta=meta, finished=runtime.finished_stages)
        markers: dict[str, bool] = {}
        for stage_id in meta.stage_sequence:
            if stage_id in runtime.finished_stages:
                markers[stage_id] = True
        result["finished_markers"] = markers

    return result


def load_spec(
    cls: type[Any],
    save_to: str | Path,
    *,
    include_field_schema: bool = False,
) -> dict[str, Any] | None:
    """Load saved spec values for a pipeline class from ``<save_to>/spec.yaml``."""
    if not isinstance(cls, type):
        raise TypeError(f"load_spec() expects a pipeline class, got {type(cls)!r}")
    meta = _require_meta(cls)

    spec_file = Path(save_to) / SPEC_FILE_NAME
    if not spec_file.exists():
        return None

    payload = load_spec_file(spec_file)
    file_schema = payload.get("field_schema")
    if not isinstance(file_schema, dict):
        raise ValueError(f"Invalid spec file content in {spec_file}: missing field_schema mapping")
    if file_schema != meta.field_schema:
        raise ValueError(
            "Field schema mismatch: current fields/kinds differ from cached spec.yaml"
        )

    spec_fields = payload.get("spec_fields")
    if not isinstance(spec_fields, dict):
        raise ValueError(f"Invalid spec file content in {spec_file}: missing spec_fields mapping")
    deserialized_spec_fields = _deserialize_pipeline_spec_fields(
        cls,
        meta=meta,
        spec_fields=spec_fields,
    )
    if include_field_schema:
        full_payload = dict(payload)
        full_payload["spec_fields"] = deserialized_spec_fields
        return full_payload
    return deserialized_spec_fields


def load_pipeline(
    *,
    cls: type[_P],
    save_to: str | Path,
    read_only: bool = True,
) -> Callable[..., _P]:
    """Build a loader that reopens a pipeline instance from an existing cache directory."""
    if not isinstance(cls, type):
        raise TypeError(f"load_pipeline() expects a pipeline class, got {type(cls)!r}")

    meta = _require_meta(cls)
    spec_fields = load_spec(cls, save_to)
    if spec_fields is None:
        spec_file = Path(save_to) / SPEC_FILE_NAME
        raise FileNotFoundError(
            "load_pipeline() requires an existing cache directory with a saved spec file. "
            f"Expected {spec_file}. Run the pipeline once first, or pass a valid cache path."
        )

    attrs_fields = _get_public_attrs_fields(cls)
    fields_by_name = {field.name: field for field in attrs_fields}
    spec_field_names = set(meta.fields_by_kind["spec"])
    spec_override_keys = _build_reserved_override_keys(
        fields_by_name[field_name] for field_name in spec_field_names
    )
    save_field = fields_by_name[meta.save_path_field]
    save_override_keys = _build_reserved_override_keys((save_field,))

    def factory(**overrides: Any) -> _P:
        invalid_keys = sorted(spec_override_keys.intersection(overrides))
        if invalid_keys:
            names = ", ".join(repr(item) for item in invalid_keys)
            raise TypeError(
                "load_pipeline() does not accept overrides for saved spec fields. "
                f"These values come from {Path(save_to) / SPEC_FILE_NAME}: {names}"
            )

        invalid_save_keys = sorted(save_override_keys.intersection(overrides))
        if invalid_save_keys:
            names = ", ".join(repr(item) for item in invalid_save_keys)
            raise TypeError(
                "load_pipeline() always binds the pipeline save path from the outer "
                f"`save_to=` argument, so these overrides are not allowed: {names}"
            )

        ctor_kwargs: dict[str, Any] = {}

        for field_name, value in spec_fields.items():
            field = fields_by_name[field_name]
            if field.init:
                ctor_kwargs[_field_ctor_key(field)] = value

        if save_field.init:
            ctor_kwargs[_field_ctor_key(save_field)] = save_to

        ctor_kwargs.update(overrides)

        if read_only:
            for field_name in meta.fields_by_kind["input"]:
                field = fields_by_name[field_name]
                if not field.init:
                    continue
                ctor_key = _field_ctor_key(field)
                if ctor_key in ctor_kwargs or field.default is not attrs.NOTHING:
                    continue
                ctor_kwargs[ctor_key] = None

        instance = cls(**ctor_kwargs)
        if not save_field.init:
            _validate_loaded_save_path(
                instance,
                meta=meta,
                expected_save_to=save_to,
            )

        runtime = _get_runtime_state(instance)
        runtime.read_only = read_only
        return instance

    return factory


def _run_stage(self: Any, *, stage_id: str, stage_order: int, fn: Callable[[Any], Any]) -> None:
    meta = _require_meta(type(self))
    runtime = _get_runtime_state(self)
    _enter_stage_execution(runtime=runtime, stage_id=stage_id)
    try:
        _bootstrap_if_needed(self, meta=meta, runtime=runtime)
        _enforce_stage_call_order(
            meta=meta,
            runtime=runtime,
            stage_id=stage_id,
            stage_order=stage_order,
        )

        if _restore_stage_from_cache(self, meta=meta, runtime=runtime, stage_id=stage_id):
            return None

        if runtime.read_only:
            raise RuntimeError(
                f"Read-only pipeline cannot execute stage body for stage {stage_id!r}"
            )

        result = fn(self)
        if result is not None:
            raise TypeError(f"Stage {stage_id!r} must return None, got {type(result)!r}")

        runtime.finished_stages.add(stage_id)
        if runtime.cache_enabled and runtime.save_path is not None:
            stage_file = runtime.save_path / f"{stage_id}.pkl"
            payload = _build_payload(self, meta=meta, runtime=runtime)
            _save_payload(self, stage_file, payload)

        return None
    finally:
        _exit_stage_execution(runtime=runtime, stage_id=stage_id)


async def _run_stage_async(
    self: Any,
    *,
    stage_id: str,
    stage_order: int,
    fn: Callable[[Any], Any],
) -> None:
    meta = _require_meta(type(self))
    runtime = _get_runtime_state(self)
    _enter_stage_execution(runtime=runtime, stage_id=stage_id)
    try:
        await asyncio.to_thread(_bootstrap_if_needed, self, meta=meta, runtime=runtime)
        _enforce_stage_call_order(
            meta=meta,
            runtime=runtime,
            stage_id=stage_id,
            stage_order=stage_order,
        )

        restored = await asyncio.to_thread(
            _restore_stage_from_cache,
            self,
            meta=meta,
            runtime=runtime,
            stage_id=stage_id,
        )
        if restored:
            return None

        if runtime.read_only:
            raise RuntimeError(
                f"Read-only pipeline cannot execute stage body for stage {stage_id!r}"
            )

        result = await fn(self)
        if result is not None:
            raise TypeError(f"Stage {stage_id!r} must return None, got {type(result)!r}")

        runtime.finished_stages.add(stage_id)
        if runtime.cache_enabled and runtime.save_path is not None:
            stage_file = runtime.save_path / f"{stage_id}.pkl"
            payload = _build_payload(self, meta=meta, runtime=runtime)
            await asyncio.to_thread(_save_payload, self, stage_file, payload)

        return None
    finally:
        _exit_stage_execution(runtime=runtime, stage_id=stage_id)



def _bootstrap_if_needed(self: Any, *, meta: PipelineMeta, runtime: RuntimeState) -> None:
    if runtime.bootstrapped:
        return

    raw_save_path = getattr(self, meta.save_path_field)
    if raw_save_path is None:
        if runtime.read_only:
            raise ValueError(
                "Read-only pipeline requires a cache directory. "
                f"Field {meta.save_path_field!r} is None."
            )
        runtime.cache_enabled = False
        runtime.bootstrapped = True
        return

    runtime.cache_enabled = True
    runtime.save_path = Path(raw_save_path)
    if runtime.read_only:
        if not runtime.save_path.exists():
            raise FileNotFoundError(
                "Read-only pipeline expected an existing cache directory at "
                f"{runtime.save_path}. Run the pipeline once first, or use read_only=False."
            )
    else:
        runtime.save_path.mkdir(parents=True, exist_ok=True)

    current_spec = _collect_spec_fields(self, meta)
    current_schema = {k: v for k, v in meta.field_schema.items()}

    spec_file = runtime.save_path / SPEC_FILE_NAME
    if spec_file.exists():
        existing = load_spec_file(spec_file)
        if existing.get("spec_fields") != current_spec:
            raise ValueError(
                "Spec mismatch: current spec_fields differ from cached spec.yaml"
            )
        if existing.get("field_schema") != current_schema:
            raise ValueError(
                "Field schema mismatch: current fields/kinds differ from cached spec.yaml"
            )
    else:
        if _has_any_stage_snapshot_files(runtime.save_path):
            raise ValueError(
                "Invalid cache layout: spec.yaml is missing but stage snapshot files (*.pkl) exist. "
                "Cache appears corrupted; please clean cache directory manually."
            )
        if runtime.read_only:
            raise FileNotFoundError(
                "Read-only pipeline expected a saved spec file at "
                f"{spec_file}. Run the pipeline once first, or use read_only=False."
            )
        _write_spec_file(spec_file, {
            "spec_fields": current_spec,
            "field_schema": current_schema,
        })

    _validate_stage_snapshot_continuity(meta=meta, save_path=runtime.save_path)
    runtime.bootstrapped = True



def _validate_stage_snapshot_continuity(*, meta: PipelineMeta, save_path: Path) -> None:
    first_missing_stage_id: str | None = None
    for stage_id in meta.stage_sequence:
        stage_file = save_path / f"{stage_id}.pkl"
        exists = stage_file.exists()
        if exists:
            if first_missing_stage_id is not None:
                raise ValueError(
                    "Invalid stage cache layout: "
                    f"found snapshot for stage {stage_id!r} after missing earlier stage "
                    f"{first_missing_stage_id!r}. Please clean cache directory manually."
                )
            continue
        if first_missing_stage_id is None:
            first_missing_stage_id = stage_id



def _has_any_stage_snapshot_files(save_path: Path) -> bool:
    for path in save_path.glob("*.pkl"):
        if path.is_file():
            return True
    return False



def _collect_spec_fields(self: Any, meta: PipelineMeta) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name in meta.fields_by_kind["spec"]:
        payload[name] = normalize_spec_value(getattr(self, name), path=f"spec.{name}")
    return payload



def _build_payload(self: Any, *, meta: PipelineMeta, runtime: RuntimeState) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field_name in meta.fields_by_kind["state"] + meta.fields_by_kind["output"]:
        payload[field_name] = getattr(self, field_name)

    for stage_id in meta.stage_sequence:
        if stage_id not in runtime.finished_stages:
            continue
        payload[f"{STAGE_FINISHED_PREFIX}{stage_id}"] = True

    return payload


def _restore_stage_from_cache(
    self: Any,
    *,
    meta: PipelineMeta,
    runtime: RuntimeState,
    stage_id: str,
) -> bool:
    if not runtime.cache_enabled or runtime.save_path is None:
        return False

    stage_file = runtime.save_path / f"{stage_id}.pkl"
    if not stage_file.exists():
        if runtime.read_only:
            raise FileNotFoundError(
                "Read-only pipeline cannot continue because the cache is missing the "
                f"snapshot for stage {stage_id!r}: {stage_file}. "
                "Re-run with read_only=False to rebuild missing stages."
            )
        return False

    payload = _load_payload(self, stage_file)
    _apply_payload(self, meta=meta, runtime=runtime, payload=payload)
    if stage_id in runtime.finished_stages:
        return True

    if runtime.read_only:
        raise ValueError(
            "Read-only pipeline found a snapshot for stage "
            f"{stage_id!r}, but it is incomplete or unfinished. "
            "Re-run with read_only=False to rebuild that stage."
        )
    return False



def _apply_payload(
    self: Any,
    *,
    meta: PipelineMeta,
    runtime: RuntimeState,
    payload: dict[str, Any],
) -> None:
    for field_name in meta.fields_by_kind["state"] + meta.fields_by_kind["output"]:
        if field_name in payload:
            object.__setattr__(self, field_name, payload[field_name])

    finished: set[str] = set()
    for key, value in payload.items():
        if key.startswith(STAGE_FINISHED_PREFIX) and value is True:
            finished.add(key[len(STAGE_FINISHED_PREFIX):])

    _validate_finished_stage_markers(meta=meta, finished=finished)
    runtime.finished_stages = finished



def _save_payload(self: Any, path: Path, payload: dict[str, Any]) -> None:
    saver = _resolve_custom_hook(self, "__saver")
    if saver is None:
        default_saver(path, payload)
        return

    _invoke_hook(
        saver,
        hook_name="__saver",
        expected_call="__saver(path, payload)",
        args=(path, payload),
    )



def _load_payload(self: Any, path: Path) -> dict[str, Any]:
    loader = _resolve_custom_hook(self, "__loader")
    if loader is None:
        data = default_loader(path)
    else:
        data = _invoke_hook(
            loader,
            hook_name="__loader",
            expected_call="__loader(path)",
            args=(path,),
        )

    if not isinstance(data, dict):
        raise TypeError(f"Loaded payload from {path} must be dict, got {type(data)!r}")
    return data



def _write_spec_file(path: Path, payload: dict[str, Any]) -> None:
    from ._state_io import atomic_write_via

    def writer(stream: Any) -> None:
        _yaml.dump(payload, stream)

    atomic_write_via(path, writer, binary=False)



def _iter_hook_candidate_names(cls: type, hook_name: str):
    yield hook_name
    for owner in inspect.getmro(cls):
        if owner is object:
            continue
        # Try Python name-mangled private method names across the class hierarchy.
        owner_name = owner.__name__.lstrip("_")
        yield f"_{owner_name}{hook_name}"


def _resolve_custom_hook(self: Any, hook_name: str) -> Callable[..., Any] | None:
    cls = type(self)
    seen: set[str] = set()
    for attr_name in _iter_hook_candidate_names(cls, hook_name):
        if attr_name in seen:
            continue
        seen.add(attr_name)
        try:
            hook = getattr(self, attr_name)
        except AttributeError:
            continue
        if not callable(hook):
            raise TypeError(
                f"Custom hook {attr_name!r} on {cls.__name__} must be callable, "
                f"got {type(hook)!r}"
            )
        if inspect.iscoroutinefunction(inspect.unwrap(hook)):
            raise TypeError(
                f"Custom hook {attr_name!r} on {cls.__name__} must be synchronous"
            )
        return hook

    return None



def _invoke_hook(
    hook: Callable[..., Any],
    *,
    hook_name: str,
    expected_call: str,
    args: tuple[Any, ...],
) -> Any:
    try:
        sig = inspect.signature(hook)
    except (TypeError, ValueError):
        sig = None

    if sig is not None:
        try:
            sig.bind(*args)
        except TypeError as exc:
            raise TypeError(
                f"Custom hook {hook_name} has invalid signature; expected {expected_call}"
            ) from exc

    return hook(*args)



def _validate_finished_stage_markers(*, meta: PipelineMeta, finished: set[str]) -> None:
    known_stage_ids = set(meta.stage_order_by_id)
    unknown_stage_ids = finished.difference(known_stage_ids)
    if unknown_stage_ids:
        names = ", ".join(sorted(repr(item) for item in unknown_stage_ids))
        raise ValueError(
            f"Invalid stage payload: found unknown finished stage markers: {names}"
        )

    missing_seen = False
    for stage_id in meta.stage_sequence:
        if stage_id in finished:
            if missing_seen:
                raise ValueError(
                    "Invalid stage payload: finished stage markers must form a prefix of stage order"
                )
            continue
        missing_seen = True



def _next_expected_stage_id(*, meta: PipelineMeta, runtime: RuntimeState) -> str | None:
    _validate_finished_stage_markers(meta=meta, finished=runtime.finished_stages)
    for stage_id in meta.stage_sequence:
        if stage_id not in runtime.finished_stages:
            return stage_id
    return None



def _enforce_stage_call_order(
    *,
    meta: PipelineMeta,
    runtime: RuntimeState,
    stage_id: str,
    stage_order: int,
) -> None:
    expected_stage_id = _next_expected_stage_id(meta=meta, runtime=runtime)
    if expected_stage_id is None:
        raise ValueError(
            f"Invalid stage call order: all stages are already finished, got stage {stage_id!r}"
        )
    if stage_id == expected_stage_id:
        return

    expected_order = meta.stage_order_by_id[expected_stage_id]
    raise ValueError(
        "Invalid stage call order: "
        f"expected next stage {expected_stage_id!r} (order={expected_order}), "
        f"got {stage_id!r} (order={stage_order}). "
        "Stages must be called in strictly increasing order."
    )



def _get_runtime_state(instance: Any) -> RuntimeState:
    runtime = getattr(instance, _RUNTIME_FIELD_NAME, None)
    if runtime is None:
        runtime = RuntimeState()
        object.__setattr__(instance, _RUNTIME_FIELD_NAME, runtime)
    if not isinstance(runtime, RuntimeState):
        raise TypeError(
            f"Internal runtime storage {_RUNTIME_FIELD_NAME!r} must be RuntimeState, got {type(runtime)!r}"
        )
    return runtime


def _get_public_attrs_fields(cls: type[Any]) -> tuple[attrs.Attribute[Any], ...]:
    return tuple(
        field
        for field in attrs.fields(cls)
        if field.metadata.get(PIPELINE_INTERNAL_FIELD_METADATA_KEY) is not True
    )


def _field_ctor_key(field: attrs.Attribute[Any]) -> str:
    alias = getattr(field, "alias", None)
    if isinstance(alias, str):
        return alias
    return field.name


def _build_reserved_override_keys(
    fields: Any,
) -> set[str]:
    reserved_keys: set[str] = set()
    for field in fields:
        reserved_keys.add(field.name)
        reserved_keys.add(_field_ctor_key(field))
    return reserved_keys


def _validate_loaded_save_path(
    instance: Any,
    *,
    meta: PipelineMeta,
    expected_save_to: str | Path,
) -> None:
    actual_save_to = getattr(instance, meta.save_path_field)
    if actual_save_to is None:
        raise TypeError(
            "load_pipeline() cannot set init=False save path fields after construction. "
            f"Constructor field {meta.save_path_field!r} must initialize itself to "
            f"{Path(expected_save_to)!r}."
        )
    if Path(actual_save_to) != Path(expected_save_to):
        raise TypeError(
            "load_pipeline() expected the constructor to initialize "
            f"{meta.save_path_field!r} to {Path(expected_save_to)!r}, "
            f"but got {actual_save_to!r}."
        )


def _deserialize_pipeline_spec_fields(
    cls: type[Any],
    *,
    meta: PipelineMeta,
    spec_fields: dict[str, Any],
) -> dict[str, Any]:
    type_hints = _safe_get_type_hints(cls)
    attrs_fields = {field.name: field for field in _get_public_attrs_fields(cls)}

    result: dict[str, Any] = {}
    for field_name in meta.fields_by_kind["spec"]:
        if field_name not in spec_fields:
            continue
        field = attrs_fields[field_name]
        annotation = type_hints.get(field_name, field.type)
        result[field_name] = deserialize_spec_value(
            spec_fields[field_name],
            annotation,
            path=f"spec.{field_name}",
        )
    return result


def _safe_get_type_hints(cls: type[Any]) -> dict[str, Any]:
    try:
        return get_type_hints(cls, include_extras=True)
    except Exception:
        return {}


def _enter_stage_execution(*, runtime: RuntimeState, stage_id: str) -> None:
    running_stage_id = runtime.running_stage_id
    if running_stage_id is not None:
        raise ValueError(
            "Invalid stage call order: "
            f"stage {running_stage_id!r} is already running, so stage {stage_id!r} "
            "cannot start. Concurrent stage execution on one pipeline instance is not supported."
        )
    runtime.running_stage_id = stage_id


def _exit_stage_execution(*, runtime: RuntimeState, stage_id: str) -> None:
    if runtime.running_stage_id == stage_id:
        runtime.running_stage_id = None


def _require_meta(cls: type) -> PipelineMeta:
    meta = getattr(cls, PIPELINE_META_ATTR, None)
    if meta is None:
        raise TypeError(
            f"Class {cls.__name__} is not a pipeline class. Use @define_pipeline(...)."
        )
    return meta
