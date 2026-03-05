from typing import Any, Literal, Mapping

PIPELINE_KIND_METADATA_KEY: str
PipelineFieldKind = Literal["spec", "state", "output", "input", "transient"]


def Spec(
    *,
    default: Any = ...,
    validator: Any = None,
    repr: Any = True,
    hash: bool | None = None,
    init: bool = True,
    metadata: Mapping[Any, Any] | None = None,
    type: type | None = None,
    converter: Any = None,
    factory: Any = None,
    kw_only: bool | None = None,
    eq: bool | None = None,
    order: bool | None = None,
    alias: str | None = None,
) -> Any: ...


def State(
    *,
    default: Any = ...,
    validator: Any = None,
    repr: Any = True,
    hash: bool | None = None,
    init: bool = True,
    metadata: Mapping[Any, Any] | None = None,
    type: type | None = None,
    converter: Any = None,
    factory: Any = None,
    kw_only: bool | None = None,
    eq: bool | None = None,
    order: bool | None = None,
    on_setattr: Any = None,
    alias: str | None = None,
) -> Any: ...


def Output(
    *,
    default: Any = ...,
    validator: Any = None,
    repr: Any = True,
    hash: bool | None = None,
    init: bool = True,
    metadata: Mapping[Any, Any] | None = None,
    type: type | None = None,
    converter: Any = None,
    factory: Any = None,
    kw_only: bool | None = None,
    eq: bool | None = None,
    order: bool | None = None,
    on_setattr: Any = None,
    alias: str | None = None,
) -> Any: ...


def Input(
    *,
    default: Any = ...,
    validator: Any = None,
    repr: Any = True,
    hash: bool | None = None,
    init: bool = True,
    metadata: Mapping[Any, Any] | None = None,
    type: type | None = None,
    converter: Any = None,
    factory: Any = None,
    kw_only: bool | None = None,
    eq: bool | None = None,
    order: bool | None = None,
    on_setattr: Any = None,
    alias: str | None = None,
) -> Any: ...


def Transient(
    *,
    default: Any = ...,
    validator: Any = None,
    repr: Any = True,
    hash: bool | None = None,
    init: bool = True,
    metadata: Mapping[Any, Any] | None = None,
    type: type | None = None,
    converter: Any = None,
    factory: Any = None,
    kw_only: bool | None = None,
    eq: bool | None = None,
    order: bool | None = None,
    on_setattr: Any = None,
    alias: str | None = None,
) -> Any: ...
