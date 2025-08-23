from alpenstock.auto_slice import AutoSliceMixin, SliceHint
import attrs
from typing import Annotated, Any
import numpy as np
import pytest


def fancy_slice_for_str(value: str, key: Any, hint: SliceHint = None):
    if isinstance(key, slice):
        return value[key]
    
    value = np.asarray(list(value))
    rst = "".join(value[key])
    return rst


@attrs.define
class Weather(AutoSliceMixin):
    # treated as a scalar
    city: str
    
    # shape (T,) array, slicing enabled
    temperatures: list[float]
    
    # shape (T,) string, slicing enabled manually
    raining: Annotated[str, SliceHint(func=fancy_slice_for_str)]
    
    # shape (H, W) array, copied to the slicing result
    site_image: Annotated[np.ndarray, SliceHint(func="copy")]


@pytest.fixture
def default_weather():
    return Weather(
        city="ga kuen to shi",
        temperatures=[15, 20, 57, 15],
        raining="RSWW", # raining, sunny, windy, windy
        site_image=np.array([[0, 1, 2], [4, 5, 6]])
    )


def test_customization(default_weather):
    # preparing variables
    data: Weather = default_weather
    site_image = np.array([[0, 1, 2], [4, 5, 6]])
    
    # Python builtin slicing
    subset = data[1:-1]
    assert subset.city == "ga kuen to shi"
    assert subset.temperatures == [20, 57]
    assert subset.raining == "SW"
    assert np.allclose(subset.site_image, site_image)

    # Fancy slicing (list of indices)
    subset = data[[1, -2]]
    assert subset.city == "ga kuen to shi"
    assert subset.temperatures == [20, 57]
    assert subset.raining == "SW"
    assert np.allclose(subset.site_image, site_image)
    
    # Fancy slicing (mask)
    subset = data[[False, True, True, False]]
    assert subset.city == "ga kuen to shi"
    assert subset.temperatures == [20, 57]
    assert subset.raining == "SW"
    assert np.allclose(subset.site_image, site_image)