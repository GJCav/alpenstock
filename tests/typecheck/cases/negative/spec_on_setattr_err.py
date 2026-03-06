from __future__ import annotations

from pathlib import Path

from alpenstock.pipeline import spec, transient, define_pipeline


@define_pipeline(save_path_field="save_to", kw_only=True)
class BadSpecOnSetattr:
    spec_a: int = spec(on_setattr=None)
    save_to: str | Path | None = transient(default=None)
