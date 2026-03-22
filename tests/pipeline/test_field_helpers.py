from __future__ import annotations

import inspect

import attrs

from alpenstock.pipeline import input, output, spec, state, transient


def test_field_wrappers_expose_attrs_field_docs_and_signature() -> None:
    expected_signature = inspect.signature(attrs.field)

    for wrapper in (spec, state, output, input, transient):
        assert inspect.signature(wrapper) == expected_signature
        assert getattr(wrapper, "__wrapped__", None) is attrs.field

    assert state.__doc__ == attrs.field.__doc__
    assert output.__doc__ == attrs.field.__doc__
    assert transient.__doc__ == attrs.field.__doc__

    assert isinstance(spec.__doc__, str)
    assert "on_setattr" in spec.__doc__
    assert "init=False" in spec.__doc__

    assert isinstance(input.__doc__, str)
    assert "init=False" in input.__doc__
