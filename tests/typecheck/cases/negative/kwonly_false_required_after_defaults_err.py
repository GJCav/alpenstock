from __future__ import annotations

from alpenstock.pipeline import input, output, spec, transient, define_pipeline, stage_func


@define_pipeline(save_path_field="save_path", kw_only=False)
class KwOnlyFalseOrderPipeline:
    order: int = spec(default=2)
    x: float = input(default=0.0)
    y: float | None = output(default=None)
    save_path: str = transient()

    @stage_func(id="step", order=0)
    def step(self) -> None:
        self.y = self.x + self.order
