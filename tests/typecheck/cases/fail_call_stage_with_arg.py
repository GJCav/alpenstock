from __future__ import annotations

from pathlib import Path

from alpenstock.pipeline import Spec, Transient, define_pipeline, stage_func


@define_pipeline(save_path_field="save_to", kw_only=True)
class BadStageCall:
    spec_a: int = Spec()
    save_to: str | Path | None = Transient(default=None)

    @stage_func(id="step", order=0)
    def step(self) -> None:
        return None


p = BadStageCall(spec_a=1, save_to=None)
p.step(123)
