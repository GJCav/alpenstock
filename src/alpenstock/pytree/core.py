from attrs import define, field
from typing import Any, Protocol, Type, override, Union
import abc

import attrs

@define(frozen=True)
class PathKey(abc.ABC):
    @abc.abstractmethod
    def get(self, obj: Any) -> Any: ...


@define(frozen=True)
class AttrKey(PathKey):
    key: str = field()

    @override
    def get(self, obj: Any) -> Any:
        return getattr(obj, self.key)

    def __str__(self) -> str:
        return f".{self.key}"


@define(frozen=True)
class ItemKey(PathKey):
    key: Any = field()

    @override
    def get(self, obj: Any) -> Any:
        return obj[self.key]

    def __str__(self) -> str:
        return f"[{self.key!r}]"
    

@define(frozen=True)
class TreePath:
    paths: tuple[PathKey, ...] = field(factory=tuple)

    def __str__(self) -> str:
        return "#" + "".join(str(p) for p in self.paths)

    def __truediv__(self, key: PathKey) -> "TreePath":
        return TreePath(paths=self.paths + (key,))

    def __getitem__(self, key: int | slice) -> Union[PathKey, "TreePath"]:
        if isinstance(key, int):
            return self.paths[key]

        else:
            return TreePath(paths=self.paths[key])

    def get(self, obj: Any) -> Any:
        r = obj
        for i in range(len(self.paths)):
            try:
                r = self.paths[i].get(r)
            except Exception as e:
                # TODO: make the error more informative
                raise RuntimeError(f"Error on path {self[: i + 1]}: {e}") from e
        return r

ChildrenAuxKey = tuple[tuple[Any, ...], Any, tuple[PathKey, ...]]

class FlattenFunc(Protocol):
    def __call__(self, obj: Any) -> ChildrenAuxKey:
        """The function to flatten a pytree node.

        Args:
            obj (Any): the pytree node to flatten

        Returns: 
            ChildrenAuxKey: A tuple of (children, aux, path_keys), where
                children (tuple[Any, ...]): A tuple of child nodes.
                aux (Any): auxiliary data needed to reconstruct the node.
                path_keys (tuple[PathKey, ...]): A tuple of PathKey objects representing the path to each child.
        """
        ...


class UnflattenFunc(Protocol):
    def __call__(self, aux: Any, children: tuple[Any, ...]) -> Any:
        """The function to unflatten a pytree node.

        Args:
            aux (Any): the auxiliary data needed to reconstruct the node.
            children (tuple[Any, ...]): A tuple of child nodes.

        Returns:
            Any: The reconstructed pytree node.
        """
        ...


class LeafType:
    pass


@define(frozen=True)
class TreeDef:
    type: Type[Any] = field()
    children: tuple["TreeDef", ...] = field(factory=tuple)
    aux: Any = field(default=None)
    
    @property
    def is_leaf(self) -> bool:
        return self.type is LeafType
    
    @property
    def is_none(self) -> bool:
        return self.type is type(None)
    
    def __str__(self) -> str:
        """The generic string representation of a TreeDef.
        
        Developers are encouraged to override this method for custom pytree nodes.
        """
        if self.type is type(None):
            return "None"
        
        elif self.type is LeafType:
            return "*"

        else:
            children_str = ", ".join(str(c) for c in self.children)
            return f"{self.type.__name__}({children_str})"


@attrs.define(frozen=True)
class Registration:
    type: Type[Any] = attrs.field()
    flatten: FlattenFunc = attrs.field()
    unflatten: UnflattenFunc = attrs.field()
    tree_def: Type[TreeDef] = attrs.field()


PYTREE_REGISTRATIONS: dict[Type[Any], Registration] = {}

def register_pytree_node(
    type: Type[Any],
    flatten: FlattenFunc,
    unflatten: UnflattenFunc,
    tree_def: Type[TreeDef] = TreeDef,
) -> None:
    """Register a custom pytree node type.

    Args:
        type (Type[Any]): The custom pytree node type to register.
        flatten (FlattenFunc): The function to flatten the custom pytree node.
        unflatten (UnflattenFunc): The function to unflatten the custom pytree node.
        tree_def (Type[TreeDef], optional): The TreeDef class for the custom pytree node. Defaults to TreeDef.
    """
    if type in PYTREE_REGISTRATIONS:
        raise ValueError(f"Pytree node type {type} is already registered.")
    
    PYTREE_REGISTRATIONS[type] = Registration(
        type=type,
        flatten=flatten,
        unflatten=unflatten,
        tree_def=tree_def,
    )
    

def unregister_pytree_node(type: Type[Any]) -> None:
    """Unregister a custom pytree node type.

    Args:
        type (Type[Any]): The custom pytree node type to unregister.
    """
    if type not in PYTREE_REGISTRATIONS:
        raise ValueError(f"Pytree node type {type} is not registered.")
    
    del PYTREE_REGISTRATIONS[type]


def is_leaf(node: Any, path: TreePath) -> bool:
    if type(node) in PYTREE_REGISTRATIONS:
        return False
    elif node is None:
        return False # like jax, treat None as non-leaf
    return True


def tree_flatten_with_path(tree: Any) -> tuple[list[TreePath], list[Any], TreeDef]:
    leaves = []
    leaf_paths = []

    def _visit(h: Any, path: TreePath):
        if is_leaf(h, path):
            leaves.append(h)
            leaf_paths.append(path)
            return TreeDef(type=LeafType)
        
        elif h is None:
            return TreeDef(type=type(None))

        elif (reg := PYTREE_REGISTRATIONS.get(type(h))) is not None:
            children, aux, path_keys = reg.flatten(h)
            
            children_defs = []
            for child, path_key in zip(children, path_keys):
                child_def = _visit(child, path / path_key)
                children_defs.append(child_def)
            
            return reg.tree_def(
                type=reg.type,
                children=tuple(children_defs),
                aux=aux,
            )
        
        else:
            raise TypeError(f"Unsupported type: {type(h)} at path {path}")

    tree_def = _visit(tree, TreePath())
    return leaf_paths, leaves, tree_def


def tree_unflatten(leaves: list[Any], tree_def: TreeDef) -> Any:
    it = iter(leaves)
    
    def rebuild(td: TreeDef) -> Any:
        if td.is_leaf:
            return next(it)
        
        elif td.is_none:
            return None
        
        else:
            reg = PYTREE_REGISTRATIONS.get(td.type)
            if reg is None:
                raise TypeError(f"Unregistered pytree node type: {td.type}")
            
            children = [rebuild(child_def) for child_def in td.children]
            return reg.unflatten(td.aux, tuple(children))
            
    return rebuild(tree_def)