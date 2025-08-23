from alpenstock.auto_slice import AutoSliceMixin, SliceHint
import attrs
from typing import Annotated
import pytest
import numpy as np


@attrs.define
class Weather(AutoSliceMixin):
    city: str
    postcode: int
    temperatures: list[float]      # shape (T,)
    humidities: np.ndarray         # shape (T,)
    sitewise_temperatures: Annotated[np.ndarray, SliceHint(axis=1)]  # shape (N, T), where N is number of sites


@pytest.fixture
def default_weather():
    return Weather(
        city="Gotham",
        postcode=12345,
        temperatures=[1, 1, 4, 5, 1, 4],
        humidities=np.array([10, 10, 40, 50, 10, 40]),
        sitewise_temperatures=np.array([[3, 1, 4, 1, 5, 9], [2, 7, 1, 8, 2, 8]])
    )


def test_python_slice(default_weather):
    data = default_weather
    
    # start, stop
    subset = data[1:4]
    assert subset.city == "Gotham"
    assert subset.postcode == 12345
    assert subset.temperatures == [1, 4, 5]
    assert np.allclose(subset.humidities, np.array([10, 40, 50]))
    assert np.allclose(subset.sitewise_temperatures, np.array([[1, 4, 1], [7, 1, 8]]))
    
    # stop only
    subset = data[:2]
    assert subset.city == "Gotham"
    assert subset.postcode == 12345
    assert subset.temperatures == [1, 1]
    assert np.allclose(subset.humidities, np.array([10, 10]))
    assert np.allclose(subset.sitewise_temperatures, np.array([[3, 1], [2, 7]]))
    
    # start only
    subset = data[3:]
    assert subset.city == "Gotham"
    assert subset.postcode == 12345
    assert subset.temperatures == [5, 1, 4]
    assert np.allclose(subset.humidities, np.array([50, 10, 40]))
    assert np.allclose(subset.sitewise_temperatures, np.array([[1, 5, 9], [8, 2, 8]]))

    # negative start only
    subset = data[-1:]
    assert subset.city == "Gotham"
    assert subset.postcode == 12345
    assert subset.temperatures == [4]
    assert np.allclose(subset.humidities, np.array([40]))
    assert np.allclose(subset.sitewise_temperatures, np.array([[9], [8]]))
    
    # negative end only
    subset = data[:-3]
    assert subset.city == "Gotham"
    assert subset.postcode == 12345
    assert subset.temperatures == [1, 1, 4]
    assert np.allclose(subset.humidities, np.array([10, 10, 40]))
    assert np.allclose(subset.sitewise_temperatures, np.array([[3, 1, 4], [2, 7, 1]]))
    
    # steps
    subset = data[::2]
    assert subset.city == "Gotham"
    assert subset.postcode == 12345
    assert subset.temperatures == [1, 4, 1]
    assert np.allclose(subset.humidities, np.array([10, 40, 10]))
    assert np.allclose(subset.sitewise_temperatures, np.array([[3, 4, 5], [2, 1, 2]]))
    
    # reverse
    subset = data[::-1]
    assert subset.city == "Gotham"
    assert subset.postcode == 12345
    assert subset.temperatures == [4, 1, 5, 4, 1, 1]
    assert np.allclose(subset.humidities, np.array([40, 10, 50, 40, 10, 10]))
    assert np.allclose(subset.sitewise_temperatures, np.array([[9, 5, 1, 4, 1, 3], [8, 2, 8, 1, 7, 2]]))
    
    # range
    subset = data[range(0, 2)]
    assert subset.city == "Gotham"
    assert subset.postcode == 12345
    assert subset.temperatures == [1, 1]
    assert np.allclose(subset.humidities, np.array([10, 10]))
    assert np.allclose(subset.sitewise_temperatures, np.array([[3, 1], [2, 7]]))
    


def test_fancy_slice(default_weather):
    data = default_weather
    
    # key type: list of indicies
    subset = data[[0, 3, 2, -1]]
    assert subset.city == "Gotham"
    assert subset.postcode == 12345
    assert subset.temperatures == [1, 5, 4, 4]
    assert np.allclose(subset.humidities, np.array([10, 50, 40, 40]))
    assert np.allclose(subset.sitewise_temperatures, np.array([[3, 1, 4, 9], [2, 8, 1, 8]]))
    
    # key type: numpy array of indices
    subset = data[np.array([0, 3, 2, -1])]
    assert subset.city == "Gotham"
    assert subset.postcode == 12345
    assert subset.temperatures == [1, 5, 4, 4]
    assert np.allclose(subset.humidities, np.array([10, 50, 40, 40]))
    assert np.allclose(subset.sitewise_temperatures, np.array([[3, 1, 4, 9], [2, 8, 1, 8]]))
    
    # key type: boolean mask (list)
    subset = data[[True, False, True, False, False, True]]
    assert subset.city == "Gotham"
    assert subset.postcode == 12345
    assert subset.temperatures == [1, 4, 4]
    assert np.allclose(subset.humidities, np.array([10, 40, 40]))
    assert np.allclose(subset.sitewise_temperatures, np.array([[3, 4, 9], [2, 1, 8]]))
    
    # key type: boolean mask (numpy array)
    mask = np.array([True, False, True, False, False, True])
    subset = data[mask]
    assert subset.city == "Gotham"
    assert subset.postcode == 12345
    assert subset.temperatures == [1, 4, 4]
    assert np.allclose(subset.humidities, np.array([10, 40, 40]))
    assert np.allclose(subset.sitewise_temperatures, np.array([[3, 4, 9], [2, 1, 8]]))


def test_prohibit_indexing(default_weather):
    data = default_weather
    
    with pytest.raises(TypeError, match=r".*only supports slicing semantics.*") as excinfo:
        subset = data[0]
    assert excinfo.type is TypeError
    
    with pytest.raises(TypeError, match=r".*only supports slicing semantics.*") as excinfo:
        subset = data[np.int64(2)]
    assert excinfo.type is TypeError

if __name__ == "__main__":
    test_python_slice()