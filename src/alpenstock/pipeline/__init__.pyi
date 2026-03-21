from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar, dataclass_transform, overload

import attrs

from ._fields import input as input
from ._fields import output as output
from ._fields import spec as spec
from ._fields import state as state
from ._fields import transient as transient

_C = TypeVar("_C", bound=type)

@overload
@dataclass_transform(field_specifiers=(spec, state, output, input, transient, attrs.field))
def define_pipeline(
    *,
    save_path_field: str,
    kw_only: bool = False,
    **attrs_define_kwargs: Any,
) -> Callable[[_C], _C]: ...

@overload
@dataclass_transform(field_specifiers=(spec, state, output, input, transient, attrs.field))
def define_pipeline(
    maybe_cls: _C,
    *,
    save_path_field: str,
    kw_only: bool = False,
    **attrs_define_kwargs: Any,
) -> _C: ...

_F = TypeVar("_F", Callable[[Any], None], Callable[[Any], Awaitable[None]])
def stage_func(*, id: str, order: int) -> Callable[[_F], _F]: ...

def get_state_dict(
    ins: Any,
    *,
    spec: bool = False,
    input: bool = False,
    state: bool = True,
    transient: bool = False,
    output: bool = True,
    include_finished_markers: bool = False,
) -> dict[str, Any]: ...

def load_spec(
    cls: type[Any],
    save_to: str | Path,
    *,
    include_field_schema: bool = False,
) -> dict[str, Any] | None: ...

__all__ = [
    "define_pipeline",
    "stage_func",
    "get_state_dict",
    "load_spec",
    "spec",
    "state",
    "output",
    "input",
    "transient",
]
