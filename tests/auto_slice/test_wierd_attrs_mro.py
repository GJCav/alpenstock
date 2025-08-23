from alpenstock.auto_slice import AutoSliceMixin
import attrs

@attrs.define
class BaseDataclass(AutoSliceMixin):
    a: int = 10
    b: list[float] = [1, 2, 3]


@attrs.define
class CorrectSubclass(BaseDataclass):
    c: list[float] = [10, 20, 30]


# Ops, we forget to add `@attrs.define` here
class ForgetAttrsDefine(BaseDataclass):
    d: list[float] = [-10, -20, -30]


def test_forget_attrs_define():
    correct = CorrectSubclass()
    wrong = ForgetAttrsDefine()
    
    # correct outcome
    subset = correct[:2]
    assert subset.a == 10
    assert subset.b == [1, 2]
    assert subset.c == [10, 20]
    
    # wierd behavior of we forget `@attrs.define`
    subset = wrong[:2]
    assert isinstance(subset, ForgetAttrsDefine)
    
    # The expected value should be [-10, -20], but the slicing just skips the
    # variable "d".
    assert subset.d == [-10, -20, -30]


