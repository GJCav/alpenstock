from __future__ import annotations

from enum import StrEnum
from typing import Generic, Literal, TypeAlias, TypeVar

import attrs


LogicalKey: TypeAlias = str
ValueRef = TypeVar("ValueRef")


class TransactionState(StrEnum):
    OPEN = "open"
    PREPARED = "prepared"
    COMMITTED = "committed"
    ABORTED = "aborted"


@attrs.define(frozen=True, slots=True)
class Put(Generic[ValueRef]):
    value: ValueRef = attrs.field()


@attrs.define(frozen=True, slots=True)
class Delete:
    pass


DELETE = Delete()
OverlayEntry: TypeAlias = Put[ValueRef] | Delete
OverlayMap: TypeAlias = dict[LogicalKey, OverlayEntry[ValueRef]]
CommittedState: TypeAlias = dict[LogicalKey, ValueRef]
OpenMode: TypeAlias = Literal["r", "rb", "w", "wb", "a", "ab", "r+", "rb+"]
