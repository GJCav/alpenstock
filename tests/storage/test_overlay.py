from __future__ import annotations

import pytest

from alpenstock.storage._errors import OverlayInvariantError
from alpenstock.storage._overlay import apply_overlay, read_visible
from alpenstock.storage._types import DELETE, Put


class OpaqueValueRef:
    def __init__(self, label: str) -> None:
        self.label = label

    def __eq__(self, other: object) -> bool:
        return isinstance(other, OpaqueValueRef) and self.label == other.label


def test_apply_overlay_replaces_and_removes_keys() -> None:
    base_state = {"alpha": b"1", "beta": b"2"}
    overlay = {
        "alpha": Put(b"updated"),
        "beta": DELETE,
        "gamma": Put(b"3"),
    }

    result = apply_overlay(base_state, overlay)

    assert result == {"alpha": b"updated", "gamma": b"3"}


def test_read_visible_falls_back_to_base_state() -> None:
    assert read_visible({"alpha": b"1"}, {}, "alpha") == b"1"
    assert read_visible({"alpha": b"1"}, {}, "missing") is None


def test_read_visible_rejects_malformed_overlay_entries() -> None:
    with pytest.raises(OverlayInvariantError, match="Put\\(value\\) or Delete"):
        read_visible({"alpha": b"1"}, {"alpha": object()}, "alpha")  # type: ignore[arg-type]


def test_apply_overlay_rejects_malformed_overlay_entries() -> None:
    with pytest.raises(OverlayInvariantError, match="Put\\(value\\) or Delete"):
        apply_overlay({"alpha": b"1"}, {"alpha": object()})  # type: ignore[arg-type]


def test_overlay_supports_opaque_value_refs() -> None:
    base_ref = OpaqueValueRef("base")
    next_ref = OpaqueValueRef("next")

    assert read_visible({"alpha": base_ref}, {}, "alpha") == base_ref
    assert apply_overlay({"alpha": base_ref}, {"alpha": Put(next_ref)}) == {"alpha": next_ref}
