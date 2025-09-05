from alpenstock.auto_slice import AutoSliceMixin, SliceHint
from typing import Annotated
import attrs
import pytest

@attrs.define
class PrivateAttributeClass(AutoSliceMixin):
    pub_var: list[int]
    _priv_var: Annotated[list[int], SliceHint(func='copy')]
    alias_var: list[int] = attrs.field(alias='other_name')


@pytest.fixture
def default_data():
    return PrivateAttributeClass(
        pub_var=[1, 2, 3],
        priv_var=[4, 5, 6],
        other_name=[7, 8, 9]
    )


def test_private_names_and_alias(default_data: PrivateAttributeClass):
    data = default_data

    subset = data[1:]
    assert subset.pub_var == [2, 3]
    assert subset._priv_var == [4, 5, 6]  # copied
    assert subset.alias_var == [8, 9]

