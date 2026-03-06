from typing import Any, Callable, TypeVar, dataclass_transform, overload

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

_F = TypeVar("_F", bound=Callable[..., None])
def stage_func(*, id: str, order: int) -> Callable[[_F], _F]: ...

__all__ = [
    "define_pipeline",
    "stage_func",
    "spec",
    "state",
    "output",
    "input",
    "transient",
]
