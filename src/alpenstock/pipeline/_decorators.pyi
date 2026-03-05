from typing import Any, Callable, TypeVar, dataclass_transform, overload

from ._fields import Input, Output, Spec, State, Transient

_C = TypeVar("_C", bound=type)
_F = TypeVar("_F", bound=Callable[..., None])

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


def stage_func(*, id: str, order: int) -> Callable[[_F], _F]: ...
