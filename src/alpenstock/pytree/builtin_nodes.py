from types import SimpleNamespace
from typing import Callable, Type, override

import attrs
from .core import ChildrenAuxKey, TreeDef, TreePath, ItemKey, AttrKey, register_pytree_node

def wrap_tree_def(str_func: Callable) -> Type[TreeDef]:
    @attrs.define(frozen=True)
    class WrappedTreeDef(TreeDef):
        @override
        def __str__(self) -> str:
            return str_func(self)
    
    return WrappedTreeDef


register_pytree_node(
    type=list,
    flatten=lambda obj: (
        tuple(obj),
        None,
        tuple(ItemKey(i) for i in range(len(obj))),
    ),
    unflatten=lambda aux, children: list(children),
    tree_def=wrap_tree_def(str_func=lambda self: "[" + ", ".join(str(c) for c in self.children) + "]"),
)


register_pytree_node(
    type=tuple,
    flatten=lambda obj: (
        tuple(obj),
        None,
        tuple(ItemKey(i) for i in range(len(obj))),
    ),
    unflatten=lambda aux, children: tuple(children),
    tree_def=wrap_tree_def(str_func=lambda self: "(" + ", ".join(str(c) for c in self.children) + ")"),
)


def _flatten_dict(obj: dict) -> ChildrenAuxKey:
    keys = sorted(obj.keys())
    
    children = tuple(obj[k] for k in keys)
    aux = tuple(keys)
    keys = tuple(ItemKey(k) for k in keys)
    return children, aux, keys

register_pytree_node(
    type=dict,
    flatten=_flatten_dict,
    unflatten=lambda aux, children: {k: v for k, v in zip(aux, children)},
    tree_def=wrap_tree_def(str_func=lambda self: (
        "{" + ", ".join(f"{k!r}: {c}" for k, c in zip(self.aux, self.children)) + "}"
    )),
)


def _flatten_simplenamespace(obj: SimpleNamespace) -> ChildrenAuxKey:
    keys = sorted(obj.__dict__.keys())
    
    children = tuple(getattr(obj, k) for k in keys)
    aux = tuple(keys)
    keys = tuple(AttrKey(k) for k in keys)
    return children, aux, keys


register_pytree_node(
    type=SimpleNamespace,
    flatten=_flatten_simplenamespace,
    unflatten=lambda aux, children: SimpleNamespace(**{k: v for k, v in zip(aux, children)}),
    tree_def=wrap_tree_def(str_func=lambda self: ( 
        "SimpleNamespace(" + ", ".join(f"{k}={c}" for k, c in zip(self.aux, self.children)) + ")"
    )),
)