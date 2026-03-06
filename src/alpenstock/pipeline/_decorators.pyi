from typing import Any, Callable, TypeVar, dataclass_transform, overload

import attrs

from ._fields import input, output, spec, state, transient

_C = TypeVar("_C", bound=type)
_F = TypeVar("_F", bound=Callable[..., None])

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


def stage_func(*, id: str, order: int) -> Callable[[_F], _F]: ...
