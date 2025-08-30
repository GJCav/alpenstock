import numpy as np

def rolling_max(array: np.ndarray, wnd_sz: int, axis=-1) -> np.ndarray:
    """matlab movmax alternative for numpy arrays.
    
    Args:
        array (np.ndarray): Input array.
        wnd_sz (int): Window size for the rolling max.
        axis (int): Axis along which to compute the rolling max.
    
    Returns:
        np.ndarray: Array with the same shape as input.
    """
    rolling_max = np.lib.stride_tricks.sliding_window_view(array, wnd_sz, axis=axis).max(axis=-1)
    N = array.shape[axis]
    
    pad_left_len = wnd_sz // 2
    if pad_left_len > 0:
        pad_left_value = rolling_max[..., 0] if axis == -1 else np.take(rolling_max, 0, axis=axis)
        pad_left = np.stack([pad_left_value] * pad_left_len, axis=axis)
    else:
        pad_left = np.empty((0,) * (rolling_max.ndim - 1) + (0,), dtype=array.dtype)
    
    pad_right_len = N - pad_left_len - rolling_max.shape[axis]
    if pad_right_len > 0:
        pad_right_value = rolling_max[..., -1] if axis == -1 else np.take(rolling_max, -1, axis=axis)
        pad_right = np.stack([pad_right_value] * pad_right_len, axis=axis)
    else:
        pad_right = np.empty((0,) * (rolling_max.ndim - 1) + (0,), dtype=array.dtype)
    
    rst = np.concatenate([pad_left, rolling_max, pad_right], axis=axis)
    return rst


