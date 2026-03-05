from __future__ import annotations

from pathlib import Path

from alpenstock.pipeline import Spec, Transient, define_pipeline


@define_pipeline(save_path_field="save_to", kw_only=True)
class BadCtorArg:
    spec_a: int = Spec()
    save_to: str | Path | None = Transient(default=None)


BadCtorArg(spec_a=1, save_to=None, unknown=3)
