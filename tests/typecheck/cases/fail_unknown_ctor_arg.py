from __future__ import annotations

from pathlib import Path

from alpenstock.pipeline import spec, transient, define_pipeline


@define_pipeline(save_path_field="save_to", kw_only=True)
class BadCtorArg:
    spec_a: int = spec()
    save_to: str | Path | None = transient(default=None)


BadCtorArg(spec_a=1, save_to=None, unknown=3)
