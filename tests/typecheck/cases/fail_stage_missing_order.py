from __future__ import annotations

from pathlib import Path

from alpenstock.pipeline import Spec, Transient, define_pipeline, stage_func


@define_pipeline(save_path_field="save_to", kw_only=True)
class MissingOrderPipeline:
    spec_a: int = Spec()
    save_to: str | Path | None = Transient(default=None)

    @stage_func(id="step")
    def step(self) -> None:
        return None
