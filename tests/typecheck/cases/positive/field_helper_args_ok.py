from __future__ import annotations

from pathlib import Path

from alpenstock.pipeline import spec, state, transient, define_pipeline


@define_pipeline(save_path_field="save_to", kw_only=True)
class GoodFieldArgs:
    spec_a: int = spec(repr=False, kw_only=True)
    state_a: int = state(hash=None, init=True, eq=True, order=False)
    save_to: str | Path | None = transient(default=None)
