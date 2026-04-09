from __future__ import annotations

from pathlib import Path
from typing import TypeAlias

import attrs

from .layout import RepoLayout


@attrs.define(frozen=True, slots=True)
class FsCommittedValueRef:
    key: str = attrs.field()


@attrs.define(frozen=True, slots=True)
class FsStagedValueRef:
    relpath: str = attrs.field()


FsValueRef: TypeAlias = FsCommittedValueRef | FsStagedValueRef


def resolve_value_ref_path(layout: RepoLayout, value_ref: FsValueRef) -> Path:
    if isinstance(value_ref, FsCommittedValueRef):
        return layout.committed_path(value_ref.key)
    return layout.tx_root / value_ref.relpath


__all__ = [
    "FsCommittedValueRef",
    "FsStagedValueRef",
    "FsValueRef",
    "resolve_value_ref_path",
]
