# flake8: noqa: E402
from typing import Protocol


# 🎈 1. Basic Use of Protocols
# Define a protocol for a mesh-like structure
class MeshLike(Protocol):
    vertices: list[tuple[float, float, float]]
    faces: list[tuple[int, int, int]]

# A demonstration class that adheres to the MeshLike protocol
class MeshImplementation:
    def __init__(self, vertices: list[tuple[float, float, float]], faces: list[tuple[int, int, int]]):
        self.vertices = vertices
        self.faces = faces
        

# A demonstration function that accepts any MeshLike object Now developers enjoy
# the type hinting from the IDE.
def export_mesh(mesh: MeshLike) -> None:
    print("Vertices:", mesh.vertices)
    print("Faces:", mesh.faces)


## 🎈 2. Working with multi-dimentional arrays
from jaxtyping import Float, Int
from typing import Union
import numpy as np

# Add other array types as needed like:
# ```
# ArrayLike = Union[np.ndarray, jax.numpy.ndarray, torch.Tensor]
# ````
ArrayLike = Union[np.ndarray]

class MeshLikeV2(Protocol):
    vertices: Float[ArrayLike, "N 3"]  # noqa: F722
    faces: Int[ArrayLike, "M 3"]  # noqa: F722

def export_mesh_v2(mesh: MeshLikeV2) -> None:
    pass
