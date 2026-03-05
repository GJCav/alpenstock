from typing import Any, Callable, TypeVar, dataclass_transform, overload

from ._fields import Input as Input
from ._fields import Output as Output
from ._fields import Spec as Spec
from ._fields import State as State
from ._fields import Transient as Transient

_C = TypeVar("_C", bound=type)

@overload
@dataclass_transform(field_specifiers=(Spec, State, Output, Input, Transient))
def define_pipeline(
    *,
    save_path_field: str,
    kw_only: bool = False,
    **attrs_define_kwargs: Any,
) -> Callable[[_C], _C]: ...

@overload
@dataclass_transform(field_specifiers=(Spec, State, Output, Input, Transient))
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
    "Spec",
    "State",
    "Output",
    "Input",
    "Transient",
]
