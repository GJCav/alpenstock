from __future__ import annotations

from collections.abc import Mapping
from typing import TypeVar

from ._errors import OverlayInvariantError
from ._types import CommittedState, DELETE, Delete, LogicalKey, OverlayEntry, Put

ValueRef = TypeVar("ValueRef")


def read_visible(
    base_state: Mapping[LogicalKey, ValueRef],
    overlay: Mapping[LogicalKey, OverlayEntry[ValueRef]],
    key: LogicalKey,
) -> ValueRef | None:
    entry = overlay.get(key)
    if entry is None:
        return base_state.get(key)
    normalized = _normalize_overlay_entry(entry)
    if isinstance(normalized, Put):
        return normalized.value
    return None


def apply_overlay(
    base_state: Mapping[LogicalKey, ValueRef],
    overlay: Mapping[LogicalKey, OverlayEntry[ValueRef]],
) -> CommittedState[ValueRef]:
    committed = dict(base_state)
    for key, entry in overlay.items():
        normalized = _normalize_overlay_entry(entry)
        if isinstance(normalized, Put):
            committed[key] = normalized.value
        else:
            committed.pop(key, None)
    return committed


def put_entry(value: ValueRef) -> Put[ValueRef]:
    return Put(value=value)


def delete_entry() -> Delete:
    return DELETE


def _normalize_overlay_entry(entry: OverlayEntry[ValueRef]) -> OverlayEntry[ValueRef]:
    if isinstance(entry, Put):
        return entry
    if isinstance(entry, Delete):
        return entry
    raise OverlayInvariantError("overlay entries must be Put(value) or Delete")
