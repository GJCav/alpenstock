from __future__ import annotations

from alpenstock.pipeline import Input, Output, Spec, Transient, define_pipeline, stage_func


@define_pipeline(save_path_field="save_path", kw_only=False)
class KwOnlyFalseOrderPipeline:
    order: int = Spec(default=2)
    x: float = Input(default=0.0)
    y: float | None = Output(default=None)
    save_path: str = Transient()

    @stage_func(id="step", order=0)
    def step(self) -> None:
        self.y = self.x + self.order
