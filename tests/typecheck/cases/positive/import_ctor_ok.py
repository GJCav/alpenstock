from __future__ import annotations

from pathlib import Path

from alpenstock.pipeline import input, output, spec, state, transient, define_pipeline, stage_func


@define_pipeline(save_path_field="save_to", kw_only=True)
class GoodPipeline:
    spec_a: int = spec()
    x: int = input()
    calls: int = state(default=0)
    out: int = output(default=0)
    save_to: str | Path | None = transient(default=None)

    def run(self) -> None:
        self.step()

    @stage_func(id="step", order=0)
    def step(self) -> None:
        self.calls += 1
        self.out = self.spec_a + self.x


p = GoodPipeline(spec_a=1, x=2, save_to=None)
p.run()
