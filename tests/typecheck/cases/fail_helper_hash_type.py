from __future__ import annotations

from pathlib import Path

from alpenstock.pipeline import Spec, State, Transient, define_pipeline


@define_pipeline(save_path_field="save_to", kw_only=True)
class BadFieldArgType:
    spec_a: int = Spec()
    state_a: int = State(hash="oops")
    save_to: str | Path | None = Transient(default=None)
