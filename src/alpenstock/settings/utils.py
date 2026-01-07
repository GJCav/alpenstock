from collections.abc import MutableMapping, MutableSequence, Collection
import os
from typing import Any
import re as regex


def match_env_var_placeholder(value: str) -> regex.Match | None:
    """match if the given string is an environment variable placeholder.
    
    The following formats are supported:
    - ${ENV_VAR}
    - $ENV_VAR
    
    The matching is designed to be strict, the entire string should
    contain only exactly one placeholder, with leading and trailing 
    whitespace allowed.

    Args:
        value (str): _description_

    Returns:
        regex.Match | None: _description_
    """
    
    patterns = [
        regex.compile(r'^\$\{(\w+)\}$'),
        regex.compile(r'^\$(\w+)$')
    ]
    
    value = value.strip()
    
    for pat in patterns:
        match = pat.match(value)
        if match:
            return match
    
    return None


def replace_env_vars(obj: Any, *, _path=None) -> Any:
    """
    Recursively replace environment variable placeholders in the given object.
    
    If a string holds exactly one environment variable placeholder, such as 
    `${ENV_VAR}` or `$ENV_VAR`, it will be replaced with the value of the
    corresponding environment variable. If the environment variable is not set,
    it will be replaced with an empty string.
    """
    
    if _path is None:
        _path = "#"
    
    if isinstance(obj, MutableMapping):
        for key, value in obj.items():
            obj[key] = replace_env_vars(value, _path=f"{_path}.{key}")
        return obj
    
    elif isinstance(obj, MutableSequence) and not isinstance(obj, str):
        for idx in range(len(obj)):
            obj[idx] = replace_env_vars(obj[idx], _path=f"{_path}[{idx}]")
        return obj
    
    elif isinstance(obj, str):
        m = match_env_var_placeholder(obj)
        if m:
            env_var_name = m.group(1)
            return os.getenv(env_var_name, "")
        else:
            return obj
        
    elif isinstance(obj, Collection):
        raise TypeError(f"Unsupported collection type at {_path}: {type(obj)}")
    
    else:
        return obj

