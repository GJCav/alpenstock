from __future__ import annotations

import inspect

import attrs

from alpenstock.pipeline import input, output, spec, state, transient


def test_field_wrappers_expose_attrs_field_docs_and_signature() -> None:
    expected_signature = inspect.signature(attrs.field)

    for wrapper in (spec, state, output, input, transient):
        assert inspect.signature(wrapper) == expected_signature
        assert wrapper.__doc__ == attrs.field.__doc__
        assert getattr(wrapper, "__wrapped__", None) is attrs.field
