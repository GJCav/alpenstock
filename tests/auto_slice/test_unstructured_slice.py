from alpenstock.auto_slice.unstructured import recursive_slice, NodePath
import pytest
import numpy as np


@pytest.fixture
def sample_data():
    return {
        "a": np.array([1, 2, 3, 4, 5]),
        "b": np.array([[10, 20], [30, 40], [50, 60], [70, 80], [90, 100]]),
        "nested": {"x": np.array([[1, 2, 3, 4, 5]])},
        "scalar": 42,
        "treat_as_scalar": np.array([1]),
        "treat_as_scalar_empty": np.array([]),
        "array": [np.array([100, 200, 300, 400, 500]), np.array([[[1, 2, 3, 4, 5]]])],
    }


def test_recursive_slice_basic(sample_data):
    data = sample_data

    sliced = recursive_slice(data, slice(1, 4), hint=5)

    assert np.allclose(sliced["a"], np.array([2, 3, 4]))
    assert np.allclose(sliced["b"], np.array([[30, 40], [50, 60], [70, 80]]))
    assert np.allclose(sliced["nested"]["x"], np.array([[2, 3, 4]]))
    assert sliced["scalar"] == 42
    assert np.allclose(sliced["treat_as_scalar"], np.array([1]))
    assert np.allclose(sliced["treat_as_scalar_empty"], np.array([]))
    assert np.allclose(sliced["array"][0], np.array([200, 300, 400]))
    assert np.allclose(sliced["array"][1], np.array([[[2, 3, 4]]]))


def test_custom_slicer(sample_data):
    data = sample_data

    # value at this path should be set to None
    p1 = NodePath() / "treat_as_scalar"

    # the first element in this array will be dropped entirely,
    # while the rest are sliced normally
    p2 = NodePath() / "array"

    def custom_slicer_predicator(ctx):
        if ctx.path in (p1, p2):
            return True
        return False

    def custom_slicer(ctx):
        if ctx.path == p1:
            return None
        elif ctx.path == p2:
            sliced_items = []
            for i, item in enumerate(ctx.item):
                if i == 0:
                    continue  # drop the first element
                sliced_item = recursive_slice(
                    item,
                    ctx.sl,
                    hint=ctx.hint,
                    _path=ctx.path / i,
                    custom_slicer_predicator=custom_slicer_predicator,
                    custom_slicer=custom_slicer,
                )
                sliced_items.append(sliced_item)
            return sliced_items

    sliced = recursive_slice(
        data,
        slice(1, 4),
        hint=5,
        custom_slicer_predicator=custom_slicer_predicator,
        custom_slicer=custom_slicer,
    )

    # The custom slicer should set this to None
    assert sliced["treat_as_scalar"] is None

    # The custom slicer should drop the first element in this array
    assert len(sliced["array"]) == 1
    assert np.allclose(sliced["array"][0], np.array([[[2, 3, 4]]]))

    # All other parts should be sliced normally
    assert np.allclose(sliced["a"], np.array([2, 3, 4]))
    assert np.allclose(sliced["b"], np.array([[30, 40], [50, 60], [70, 80]]))
    assert np.allclose(sliced["nested"]["x"], np.array([[2, 3, 4]]))
    assert sliced["scalar"] == 42
    assert np.allclose(sliced["treat_as_scalar_empty"], np.array([]))


def test_recursive_slice_errors():
    # No matching dimension
    arr = np.array([[1, 2], [3, 4]])
    with pytest.raises(ValueError) as excinfo:
        recursive_slice(arr, slice(0, 1), hint=5)
        assert "Cannot find a proper dimension" in str(excinfo.value)

    # Multiple matching dimensions
    arr = np.array([[1, 2], [3, 4]])
    with pytest.raises(ValueError) as excinfo:
        recursive_slice(arr, slice(0, 1), hint=2)
        assert "Multiple dimension candidates" in str(excinfo.value)
