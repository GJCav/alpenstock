from __future__ import annotations

from pathlib import PurePosixPath

from ._errors import ValueContractError


def validate_logical_key(key: str) -> str:
    if not isinstance(key, str):
        raise ValueContractError(f"logical keys must be str, got {type(key)!r}")
    if key == "":
        raise ValueContractError("logical keys must not be empty")
    if "\\" in key:
        raise ValueContractError("logical keys must use '/' separators, not '\\\\'")

    normalized = PurePosixPath(key)
    parts = normalized.parts
    if normalized.is_absolute() or not parts:
        raise ValueContractError(f"logical key {key!r} must be a relative path")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueContractError(f"logical key {key!r} must not contain '.', '..', or empty segments")
    canonical = PurePosixPath(*parts).as_posix()
    if canonical == "":
        raise ValueContractError(f"logical key {key!r} must not be empty")
    return canonical


def join_logical_key(prefix: str, key: str) -> str:
    normalized_key = validate_logical_key(key)
    if prefix == "":
        return normalized_key
    normalized_prefix = validate_logical_key(prefix)
    return validate_logical_key(f"{normalized_prefix}/{normalized_key}")


__all__ = ["join_logical_key", "validate_logical_key"]
