from typing import Any, Callable
from collections.abc import Mapping, Sequence
from attrs import define, field
from ._types import COMMON_ND_ARRAYS
import inspect
import wadler_lindig as wl


@define(frozen=True)
class NodePath:
    # The first element is the name of the variable in the outermost scope
    parts: tuple[Any, ...] = field(factory=lambda: ("<root>", ))

    def __truediv__(self, next: Any) -> "NodePath":
        return NodePath(self.parts + (next,))

    def __str__(self) -> str:
        var_name = self.parts[0]
        traversal = "".join(f"[{p:r}]" for p in self.parts[1:])
        return f"{var_name}{traversal}"
    
    def __eq__(self, value: object) -> bool:
        """Two paths are equal if their parts, except the first one, are equal."""
        if not isinstance(value, NodePath):
            return False
        return self.parts[1:] == value.parts[1:]


@define
class SlicingCtx:
    item: Any
    sl: slice
    hint: int
    path: NodePath


def recursive_slice(
    obj: Any,
    sl: slice,
    hint: int,
    _path: NodePath | None = None,
    custom_slicer_predicator: Callable[[SlicingCtx], bool] | None = None,
    custom_slicer: Callable[[SlicingCtx], Any] | None = None,
) -> Any:
    """Recursive slice a dict of ndarrays or nested dicts.

    The following rules are applied sequentially:

    - If a custom slicer is provided and the predicator returns True,
        the custom slicer is used to process the object.

    - If the object is dict-like (Mapping), each value is sliced recursively.

    - If the object is ndarray-like, such as numpy.ndarray, torch.Tensor, or
        jax.Array, the array is sliced along the dimension whose size matches `hint`.
        For example, if an array has the shape (3, 3, 5000, 256, ...) and `hint=5000`,
        the array will be sliced along `axis=2`. If there are multiple dimensions
        or none matched, an error is raised. Specially, shapes of (1,) or (0,) are treated
        as scalars and not sliced.

    - If the object is a sequence (list, tuple, etc.), each element is sliced
        recursively.

    - Other types are not sliced and returned as is.

    Args:
        obj (Any): nested structure to slice.
        sl (slice): slice object.
        hint (int): dimension hint.
        _path (NodePath, optional): internal use only.

    Returns:
        Any: sliced structure.
    """

    if (custom_slicer is None) ^ (custom_slicer_predicator is None):
        raise ValueError(
            "Both custom_slicer and custom_slicer_predicator should be provided or neither."
        )

    if _path is None:
        # Do some magic to get the variable name of `obj` in the
        # caller's frame. This is best-effort and may not always work.
        frame = inspect.currentframe().f_back  # type: ignore
        var_name = None
        if frame is not None:
            for name, val in frame.f_locals.items():
                if id(val) == id(obj):
                    if var_name is None:
                        var_name = name
                    else:
                        # The trick cannot uniquely identify the variable name
                        # so just give up.
                        #
                        # Icecream uses a better but more complex approach to do
                        # this. In future we may consider adopting that.
                        var_name = None
                        break
        if var_name is None:
            var_name = "<obj_to_be_sliced>"
        _path = NodePath(parts=(var_name,))

    ctx = SlicingCtx(item=obj, sl=sl, hint=hint, path=_path)
    if (custom_slicer_predicator is not None) and custom_slicer_predicator(ctx):
        assert custom_slicer is not None
        return custom_slicer(ctx)

    elif isinstance(obj, Mapping):
        sliced_obj = {}
        for k in obj:
            sliced_obj[k] = recursive_slice(
                obj[k], sl, hint=hint, _path=_path / k,
                custom_slicer_predicator=custom_slicer_predicator,
                custom_slicer=custom_slicer,
            )
        return sliced_obj

    elif isinstance(obj, COMMON_ND_ARRAYS):
        if obj.shape == (1,) or obj.shape == (0,):
            return obj  # treat as scalar, do not slice

        # Find the dimension that has the size `hint`
        count = 0
        dim = -1
        for i, sz in enumerate(obj.shape):
            if sz == hint:
                dim = i
                count += 1

        if count == 0:
            raise ValueError(
                f"Cannot find a proper dimension for {_path}, shape {obj.shape}"
            )
        if count >= 2:
            raise ValueError(
                f"Multiple dimension candidates are found for {_path}, the shape {obj.shape}"
            )

        # Slicing
        slc = [slice(None)] * len(obj.shape)
        slc[dim] = sl

        sliced_obj = obj[tuple(slc)]
        return sliced_obj

    elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
        sliced_obj = []
        for i, item in enumerate(obj):
            sliced_item = recursive_slice(
                item, sl, hint=hint, _path=_path / i,
                custom_slicer_predicator=custom_slicer_predicator,
                custom_slicer=custom_slicer,
            )
            sliced_obj.append(sliced_item)
        if isinstance(obj, tuple):
            return tuple(sliced_obj)
        return sliced_obj

    else:
        return obj
