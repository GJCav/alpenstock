from __future__ import annotations

from pathlib import Path

from alpenstock.pipeline import spec, state, transient, define_pipeline


@define_pipeline(save_path_field="save_to", kw_only=True)
class BadFieldArgType:
    spec_a: int = spec()
    state_a: int = state(hash="oops")
    save_to: str | Path | None = transient(default=None)
