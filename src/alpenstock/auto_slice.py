from typing import Any, Literal, get_type_hints, Self, Protocol
from collections.abc import Sequence
import functools
import attrs
import copy

import pydantic

## Define supported scalar and array types
SCALAR_TYPES = {int, float, str, bool, type(None)}
ARRAY_TYPES = set([list, tuple])

try:
    import numpy as np

    ARRAY_TYPES.add(np.ndarray)
except ImportError:
    np = None

try:
    import torch

    ARRAY_TYPES.add(torch.Tensor)
except ImportError:
    torch = None

try:
    import jax.numpy as jnp

    ARRAY_TYPES.add(jnp.ndarray)
except ImportError:
    jnp = None

SCALAR_TYPES = tuple(SCALAR_TYPES)
ARRAY_TYPES = tuple(ARRAY_TYPES)


## Special type hints
class SliceFunc(Protocol):
    def __call__(self, value: Any, key: Any, hint: "SliceHint") -> Any: ...


BuiltinSliceFunc = Literal["default", "native", "copy"]


@attrs.define(frozen=True)
class SliceHint:
    axis: int = 0

    # If provided, use this function to slice the field. The function should
    # take two arguments: the value and the key, the SliceHint and return the
    # sliced value.
    func: SliceFunc | BuiltinSliceFunc = "default"


## Utility functions
def can_handle(value: Any) -> bool:
    if isinstance(value, SCALAR_TYPES):
        return True
    if isinstance(value, ARRAY_TYPES):
        return True
    if hasattr(value, "__getitem__"):
        return True
    return False


def fancy_slice_for_builtin_list(arr, key, hint: SliceHint = None):
    if isinstance(key, slice):
        return arr[key]
    
    # NumPy / Torch / Jax array-like support .tolist()
    if hasattr(key, "tolist"):
        key = key.tolist()
        
    if isinstance(key, Sequence) and not isinstance(key, (str, bytes)):
        if all(isinstance(i, bool) for i in key): 
            # boolean mask
            return [x for x, m in zip(arr, key) if m]
        else:
            # assume index-array
            return [arr[i] for i in key]
    
    return np.take(arr, key, axis=0)


def slice_array(arr, key, hint: SliceHint = None):
    if hint is None:
        hint = SliceHint()

    axis = hint.axis
    
    if isinstance(arr, list):
        if axis != 0:
            raise ValueError(f"SliceHint(axis={axis}) is invalid for Python built-in list")
        return fancy_slice_for_builtin_list(arr, key)

    if np is not None and isinstance(arr, np.ndarray):
        sl = [slice(None)] * arr.ndim
        sl[axis] = key
        return arr[tuple(sl)]

    if torch is not None and isinstance(arr, torch.Tensor):
        sl = [slice(None)] * arr.ndim
        if isinstance(key, ARRAY_TYPES):
            key = torch.as_tensor(key, device=arr.device)
        sl[axis] = key
        return arr[tuple(sl)]

    if jnp is not None and isinstance(arr, jnp.ndarray):
        sl = [slice(None)] * arr.ndim
        sl[axis] = key
        return arr[tuple(sl)]

    raise TypeError(f"Unsupported array type: {type(arr)}")


def take_slice_hint(annotation) -> SliceHint | None:
    for meta in getattr(annotation, "__metadata__", []):
        if isinstance(meta, SliceHint):
            return meta
    return None


def default_slice_func(value: Any, key: Any, hint: SliceHint = None):
    if not can_handle(value):
        raise TypeError(
            f"default auto-slicing does not support type {type(value)!r}"
        )
    
    if isinstance(value, SCALAR_TYPES):
        return copy.copy(value)
    elif isinstance(value, ARRAY_TYPES):
        return slice_array(value, key, hint=hint)
    elif hasattr(value, "__getitem__"):
        return value[key]
    else:
        raise RuntimeError("Unreachable")


def native_slice_func(value: Any, key: Any, hint: SliceHint = None):
    return value[key]

def copy_slice_func(value: Any, key: Any, hint: SliceHint = None):
    return copy.copy(value)

def _getitem_impl_for_attrs(self, key: Any):
    cls = type(self)
    cls_type_hints = get_type_hints(cls, include_extras=True)
    fields = attrs.fields(cls)

    new_values = {}
    for field in fields:
        value = getattr(self, field.name)

        slice_hint = take_slice_hint(cls_type_hints.get(field.name, None))
        slice_hint = slice_hint or SliceHint()

        slice_func = slice_hint.func or default_slice_func
        if isinstance(slice_func, str):
            if slice_func == "default":
                slice_func = default_slice_func
            elif slice_func == "native":
                slice_func = native_slice_func
            elif slice_func == "copy":
                slice_func = copy_slice_func
            else:
                raise ValueError(f"Unknown built-in slice function: {slice_func!r}")

        try:
            new_values[field.alias] = slice_func(value, key, hint=slice_hint)
        except Exception as e:
            raise Exception(f"Unable to slice {field.name!r} of type {type(value)!r} in {cls!r}") from e
    return cls(**new_values)


class AutoSliceMixin:
    def __init_subclass__(cls):
        # For the following code,
        #
        # ```
        # @attrs.define
        # class A(AutoSliceMixin):
        #     pass
        # ```
        #
        # `AutoSliceMixin.__init_subclass__` is called twice:
        #  1. When `A` is being defined, with `old_getitem` being `None`
        #  2. When `attrs.define` processes `A`, with `old_getitem` being the
        #     `AutoSliceMixin.__getitem__`
        #
        # So only raise an error if `old_getitem` is neither `None` nor
        # `AutoSliceMixin.__getitem__`.
        #
        # When using `pydantic.BaseModel` instead of `attrs`, the
        # `AutoSliceMixin.__init_subclass__` is called only once, with
        # `old_getitem` being `None`.
        #
        # If there is a new class that inherits from class `A`, this code works
        # as expected, too.
        old_getitem = getattr(cls, "__getitem__", None)
        if old_getitem is not None and old_getitem is not AutoSliceMixin.__getitem__:
            raise TypeError(
                f"`AutoSliceMixin` only supports classes without __getitem__, but {cls!r} already has it, which is {old_getitem!r}"
            )

    def __getitem__(self: Self, key: Any) -> Self:
        # Only allow slicing semantics and raise error for indexing
        if not isinstance(key, ARRAY_TYPES + (slice, range)):
            raise TypeError(
                f"`AutoSliceMixin` only supports slicing semantics, but key type of {type(key)!r} implies indexing"
            )

        cls = type(self)
        if attrs.has(cls):
            return _getitem_impl_for_attrs(self, key)
        elif isinstance(self, pydantic.BaseModel):
            raise NotImplementedError("Pydantic support is not implemented yet")
        else:
            raise TypeError(
                f"`AutoSliceMixin` only supports classes defined with `attrs` or inheriting from `pydantic.BaseModel`, but got {cls!r}"
            )


__all__ = ["AutoSliceMixin", "SliceHint", "SliceFunc"]
