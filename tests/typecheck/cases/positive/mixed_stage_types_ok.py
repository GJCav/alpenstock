from __future__ import annotations

import asyncio
from pathlib import Path

from alpenstock.pipeline import define_pipeline, input, output, spec, stage_func, state, transient


@define_pipeline(save_path_field="save_to", kw_only=True)
class MixedPipeline:
    spec_a: int = spec()
    x: int = input()
    sync_calls: int = state(default=0)
    async_calls: int = state(default=0)
    out: int = output(default=0)
    save_to: str | Path | None = transient(default=None)

    def run_sync(self) -> None:
        result: None = self.sync_step()
        _ = result

    async def run_async(self) -> None:
        await self.async_step()
        result: None = await self.async_step()
        _ = result

    @stage_func(id="sync_step", order=0)
    def sync_step(self) -> None:
        self.sync_calls += 1

    @stage_func(id="async_step", order=1)
    async def async_step(self) -> None:
        self.async_calls += 1
        self.out = self.spec_a + self.x


async def main() -> None:
    p = MixedPipeline(spec_a=1, x=2, save_to=None)
    p.run_sync()
    await p.run_async()


asyncio.run(main())
