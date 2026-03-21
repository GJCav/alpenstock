from __future__ import annotations

from pathlib import Path
from typing import Awaitable

from alpenstock.pipeline import define_pipeline, spec, stage_func, transient


@define_pipeline(save_path_field="save_to", kw_only=True)
class SyncStagePipeline:
    spec_a: int = spec()
    save_to: str | Path | None = transient(default=None)

    @stage_func(id="step", order=0)
    def step(self) -> None:
        return None


p = SyncStagePipeline(spec_a=1, save_to=None)
value: Awaitable[None] = p.step()
