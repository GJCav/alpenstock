from __future__ import annotations

import asyncio
import ast
import pickle
from pathlib import Path

import pytest

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


pytestmark = pytest.mark.asyncio


@define_pipeline(save_path_field="save_to", kw_only=True)
class AsyncTwoStagePipeline:
    spec_scale: int = spec()

    x: int = input()
    y: int = input()

    stage1_value: int = state(default=0)
    stage2_value: int = state(default=0)
    final_output: int = output(default=0)

    execution_log: list[str] = transient(factory=list)
    save_to: str | Path | None = transient(default=None)

    async def run(self) -> None:
        await self.stage1()
        await self.stage2()

    @stage_func(id="stage1", order=0)
    async def stage1(self) -> None:
        self.execution_log.append("stage1")
        await asyncio.sleep(0)
        self.stage1_value = (self.x + self.y) * self.spec_scale

    @stage_func(id="stage2", order=1)
    async def stage2(self) -> None:
        self.execution_log.append("stage2")
        await asyncio.sleep(0)
        self.stage2_value = self.stage1_value + 1
        self.final_output = self.stage2_value * 10


def _remove_finished_marker(stage_file: Path, marker_name: str) -> None:
    payload = pickle.loads(stage_file.read_bytes())
    del payload[marker_name]
    stage_file.write_bytes(pickle.dumps(payload))


async def test_first_async_run_creates_spec_and_stage_snapshots(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p = AsyncTwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    await p.run()

    assert p.execution_log == ["stage1", "stage2"]
    assert (cache / "spec.yaml").exists()
    assert (cache / "stage1.pkl").exists()
    assert (cache / "stage2.pkl").exists()


async def test_cache_hit_skips_async_stage_execution(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = AsyncTwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    await p1.run()
    assert p1.final_output == 110

    p2 = AsyncTwoStagePipeline(spec_scale=2, x=100, y=200, save_to=cache)
    await p2.run()

    assert p2.execution_log == []
    assert p2.stage1_value == 10
    assert p2.stage2_value == 11
    assert p2.final_output == 110
    assert p2.x == 100
    assert p2.y == 200


async def test_read_only_load_pipeline_uses_async_cache_without_executing_stages(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"

    p1 = AsyncTwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    await p1.run()

    p2 = load_pipeline(cls=AsyncTwoStagePipeline, save_to=cache)(x=100, y=200)
    await p2.run()

    assert p2.execution_log == []
    assert p2.stage1_value == 10
    assert p2.stage2_value == 11
    assert p2.final_output == 110
    assert p2.x == 100
    assert p2.y == 200


async def test_missing_last_stage_snapshot_reruns_only_missing_stage(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = AsyncTwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    await p1.run()

    (cache / "stage2.pkl").unlink()

    p2 = AsyncTwoStagePipeline(spec_scale=2, x=100, y=200, save_to=cache)
    await p2.run()

    assert p2.execution_log == ["stage2"]
    assert p2.stage1_value == 10
    assert p2.stage2_value == 11
    assert p2.final_output == 110


async def test_stage_without_finished_marker_gets_rerun(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = AsyncTwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    await p1.run()

    stage2_path = cache / "stage2.pkl"
    _remove_finished_marker(stage2_path, "__stage_finished_stage2")

    p2 = AsyncTwoStagePipeline(spec_scale=2, x=9, y=9, save_to=cache)
    await p2.run()

    assert p2.execution_log == ["stage2"]
    assert p2.stage1_value == 10
    assert p2.final_output == 110
    assert stage2_path.exists()


async def test_read_only_async_load_pipeline_missing_stage_snapshot_raises(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"

    p1 = AsyncTwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    await p1.run()
    (cache / "stage2.pkl").unlink()

    p2 = load_pipeline(cls=AsyncTwoStagePipeline, save_to=cache)()
    with pytest.raises(FileNotFoundError, match="read_only=False"):
        await p2.run()

    assert p2.execution_log == []


async def test_read_only_async_load_pipeline_incomplete_stage_snapshot_raises(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"

    p1 = AsyncTwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    await p1.run()

    stage2_path = cache / "stage2.pkl"
    _remove_finished_marker(stage2_path, "__stage_finished_stage2")

    p2 = load_pipeline(cls=AsyncTwoStagePipeline, save_to=cache)()
    with pytest.raises(ValueError, match="incomplete or unfinished"):
        await p2.run()

    assert p2.execution_log == []


async def test_load_pipeline_read_write_mode_can_rerun_from_first_missing_async_stage(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"

    p1 = AsyncTwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    await p1.run()
    (cache / "stage1.pkl").unlink()
    (cache / "stage2.pkl").unlink()

    p2 = load_pipeline(cls=AsyncTwoStagePipeline, save_to=cache, read_only=False)(x=100, y=200)
    await p2.run()

    assert p2.execution_log == ["stage1", "stage2"]
    assert p2.stage1_value == 600
    assert p2.final_output == 6010


async def test_calling_later_async_stage_directly_raises_order_error(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p = AsyncTwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    with pytest.raises(ValueError, match="Invalid stage call order"):
        await p.stage2()


async def test_calling_finished_async_stage_again_raises_order_error(tmp_path: Path) -> None:
    p = AsyncTwoStagePipeline(spec_scale=2, x=2, y=3, save_to=tmp_path / "cache")
    await p.stage1()

    with pytest.raises(ValueError, match="Invalid stage call order"):
        await p.stage1()


async def test_async_stage_returning_non_none_raises_type_error_after_await(tmp_path: Path) -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class InvalidAsyncReturnPipeline:
        spec_a: int = spec()
        x: int = input()
        value: int = state(default=0)
        save_to: str | Path | None = transient(default=None)

        async def run(self) -> None:
            await self.step()

        @stage_func(id="step", order=0)
        async def step(self) -> None:
            self.value = self.x
            await asyncio.sleep(0)
            return 1  # type: ignore[return-value]

    cache = tmp_path / "cache"
    p = InvalidAsyncReturnPipeline(spec_a=1, x=2, save_to=cache)

    with pytest.raises(TypeError, match="must return None"):
        await p.run()

    assert (cache / "spec.yaml").exists()
    assert not (cache / "step.pkl").exists()


async def test_async_stage_exception_does_not_write_snapshot(tmp_path: Path) -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class FailingAsyncPipeline:
        spec_a: int = spec()
        value: int = state(default=0)
        save_to: str | Path | None = transient(default=None)

        async def run(self) -> None:
            await self.step()

        @stage_func(id="step", order=0)
        async def step(self) -> None:
            await asyncio.sleep(0)
            raise RuntimeError("boom")

    cache = tmp_path / "cache"
    p = FailingAsyncPipeline(spec_a=1, save_to=cache)

    with pytest.raises(RuntimeError, match="boom"):
        await p.run()

    assert (cache / "spec.yaml").exists()
    assert not (cache / "step.pkl").exists()


async def test_async_stage_cancellation_does_not_write_snapshot(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    @define_pipeline(save_path_field="save_to", kw_only=True)
    class CancelableAsyncPipeline:
        spec_a: int = spec()
        value: int = state(default=0)
        save_to: str | Path | None = transient(default=None)

        async def run(self) -> None:
            await self.step()

        @stage_func(id="step", order=0)
        async def step(self) -> None:
            entered.set()
            await release.wait()
            self.value = self.spec_a

    cache = tmp_path / "cache"
    p = CancelableAsyncPipeline(spec_a=1, save_to=cache)

    task = asyncio.create_task(p.run())
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert (cache / "spec.yaml").exists()
    assert not (cache / "step.pkl").exists()

    release.set()
    await p.run()
    assert (cache / "step.pkl").exists()


async def test_same_instance_concurrent_start_is_rejected(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    @define_pipeline(save_path_field="save_to", kw_only=True)
    class ConcurrentAsyncPipeline:
        spec_a: int = spec()
        value: int = state(default=0)
        save_to: str | Path | None = transient(default=None)

        async def run(self) -> None:
            await self.step()

        @stage_func(id="step", order=0)
        async def step(self) -> None:
            entered.set()
            await release.wait()
            self.value += self.spec_a

    cache = tmp_path / "cache"
    p = ConcurrentAsyncPipeline(spec_a=1, save_to=cache)

    task = asyncio.create_task(p.step())
    await entered.wait()

    with pytest.raises(ValueError, match="running|in-flight|Invalid stage call order"):
        await p.step()

    release.set()
    await task

    assert (cache / "step.pkl").exists()


async def test_name_mangled_saver_loader_are_used_for_async_pipeline(tmp_path: Path) -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class MangledSerializerPipeline:
        spec_bias: int = spec()
        x: int = input()
        value: int = state(default=0)
        save_to: str | Path | None = transient(default=None)
        saver_calls: int = transient(default=0)
        loader_calls: int = transient(default=0)

        async def run(self) -> None:
            await self.step()

        @stage_func(id="step", order=0)
        async def step(self) -> None:
            await asyncio.sleep(0)
            self.value = self.spec_bias + self.x

        def __saver(self, path: Path, payload: dict[str, object]) -> None:
            self.saver_calls += 1
            path.write_text(repr(payload), encoding="utf-8")

        def __loader(self, path: Path) -> dict[str, object]:
            self.loader_calls += 1
            data = ast.literal_eval(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError(f"expected dict, got {type(data)!r}")
            return data

    cache = tmp_path / "cache"
    p1 = MangledSerializerPipeline(spec_bias=1, x=2, save_to=cache)
    await p1.run()

    assert p1.value == 3
    assert p1.saver_calls == 1
    assert p1.loader_calls == 0

    stage_file = cache / "step.pkl"
    assert stage_file.read_text(encoding="utf-8").startswith("{")

    p2 = MangledSerializerPipeline(spec_bias=1, x=100, save_to=cache)
    await p2.run()

    assert p2.value == 3
    assert p2.saver_calls == 0
    assert p2.loader_calls == 1


async def test_async_saver_hook_is_rejected(tmp_path: Path) -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class AsyncSaverPipeline:
        spec_a: int = spec()
        value: int = state(default=0)
        save_to: str | Path | None = transient(default=None)

        async def run(self) -> None:
            await self.step()

        @stage_func(id="step", order=0)
        async def step(self) -> None:
            self.value = self.spec_a

        async def __saver(self, path: Path, payload: dict[str, object]) -> None:
            _ = (path, payload)

    cache = tmp_path / "cache"
    p = AsyncSaverPipeline(spec_a=1, save_to=cache)

    with pytest.raises(TypeError, match="async|coroutine|synchronous"):
        await p.run()

    assert (cache / "spec.yaml").exists()
    assert not (cache / "step.pkl").exists()


async def test_async_loader_hook_is_rejected(tmp_path: Path) -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class AsyncLoaderPipeline:
        spec_a: int = spec()
        value: int = state(default=0)
        save_to: str | Path | None = transient(default=None)

        async def run(self) -> None:
            await self.step()

        @stage_func(id="step", order=0)
        async def step(self) -> None:
            self.value = self.spec_a

        def __saver(self, path: Path, payload: dict[str, object]) -> None:
            path.write_text(repr(payload), encoding="utf-8")

        async def __loader(self, path: Path) -> dict[str, object]:
            _ = path
            return {}

    cache = tmp_path / "cache"
    p1 = AsyncLoaderPipeline(spec_a=1, save_to=cache)
    await p1.run()
    assert (cache / "step.pkl").exists()

    p2 = AsyncLoaderPipeline(spec_a=1, save_to=cache)
    with pytest.raises(TypeError, match="async|coroutine|synchronous"):
        await p2.run()
