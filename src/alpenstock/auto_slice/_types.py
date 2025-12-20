from typing import Any, Type

_COMMON_ND_ARRAYS: set[Type[Any]] = set()

try:
    import numpy as np  # type: ignore

    _COMMON_ND_ARRAYS.add(np.ndarray)
except ImportError:
    np = None

try:
    import torch  # type: ignore

    _COMMON_ND_ARRAYS.add(torch.Tensor)
except ImportError:
    torch = None

try:
    import jax.numpy as jnp  # type: ignore

    _COMMON_ND_ARRAYS.add(jnp.ndarray)
except ImportError:
    jnp = None

COMMON_ND_ARRAYS: tuple[Type[Any], ...] = tuple(_COMMON_ND_ARRAYS)