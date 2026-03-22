from __future__ import annotations

from pathlib import Path

from alpenstock.pipeline import (
    define_pipeline,
    input,
    load_pipeline,
    output,
    spec,
    stage_func,
    state,
    transient,
)


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


loader = load_pipeline(cls=GoodPipeline, save_to=Path("./cache"))
p1 = loader()
p2 = loader(x=1)
p1.run()
p2.run()
