from __future__ import annotations

from pathlib import Path

from alpenstock.pipeline import define_pipeline, spec, stage_func, transient


@define_pipeline(save_path_field="save_to", kw_only=True)
class SyncBadReturnPipeline:
    spec_a: int = spec()
    save_to: str | Path | None = transient(default=None)

    @stage_func(id="step", order=0)
    def step(self) -> int:
        return 1
