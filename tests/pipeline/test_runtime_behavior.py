from __future__ import annotations

import ast
import pickle
from pathlib import Path

import pytest

from alpenstock.pipeline import input, output, spec, state, transient, define_pipeline, stage_func


@define_pipeline(save_path_field="save_to", kw_only=True)
class TwoStagePipeline:
    spec_scale: int = spec()

    x: int = input()
    y: int = input()

    stage1_value: int = state(default=0)
    stage2_value: int = state(default=0)
    final_output: int = output(default=0)

    execution_log: list[str] = transient(factory=list)
    save_to: str | Path | None = transient(default=None)

    def run(self) -> None:
        self.stage1()
        self.stage2()

    @stage_func(id="stage1", order=0)
    def stage1(self) -> None:
        self.execution_log.append("stage1")
        self.stage1_value = (self.x + self.y) * self.spec_scale

    @stage_func(id="stage2", order=1)
    def stage2(self) -> None:
        self.execution_log.append("stage2")
        self.stage2_value = self.stage1_value + 1
        self.final_output = self.stage2_value * 10


def test_first_run_creates_spec_and_stage_snapshots(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p.run()

    assert p.execution_log == ["stage1", "stage2"]
    assert (cache / "spec.yaml").exists()
    assert (cache / "stage1.pkl").exists()
    assert (cache / "stage2.pkl").exists()


def test_cache_hit_skips_stage_execution(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p1.run()
    assert p1.final_output == 110

    p2 = TwoStagePipeline(spec_scale=2, x=100, y=200, save_to=cache)
    p2.run()

    assert p2.execution_log == []
    assert p2.final_output == 110
    assert p2.x == 100
    assert p2.y == 200


def test_missing_last_stage_snapshot_reruns_only_missing_stage(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p1.run()

    (cache / "stage2.pkl").unlink()

    p2 = TwoStagePipeline(spec_scale=2, x=100, y=200, save_to=cache)
    p2.run()

    assert p2.execution_log == ["stage2"]
    assert p2.stage1_value == 10
    assert p2.final_output == 110


def test_stage_without_finished_marker_gets_rerun(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p1.run()

    stage2_path = cache / "stage2.pkl"
    payload = pickle.loads(stage2_path.read_bytes())
    del payload["__stage_finished_stage2"]
    stage2_path.write_bytes(pickle.dumps(payload))

    p2 = TwoStagePipeline(spec_scale=2, x=9, y=9, save_to=cache)
    p2.run()

    assert p2.execution_log == ["stage2"]
    assert p2.final_output == 110


def test_missing_previous_stage_with_later_snapshot_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p1.run()

    (cache / "stage1.pkl").unlink()

    p2 = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    with pytest.raises(ValueError, match="Invalid stage cache layout"):
        p2.run()


def test_missing_spec_with_existing_stage_snapshots_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p1.run()

    (cache / "spec.yaml").unlink()

    p2 = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    with pytest.raises(ValueError, match="spec.yaml is missing"):
        p2.run()


def test_missing_spec_without_stage_snapshots_bootstraps_as_fresh_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "notes.txt").write_text("placeholder", encoding="utf-8")

    p = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p.run()

    assert p.execution_log == ["stage1", "stage2"]
    assert (cache / "spec.yaml").exists()
    assert (cache / "stage1.pkl").exists()
    assert (cache / "stage2.pkl").exists()


def test_calling_later_stage_directly_raises_order_error(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p1.stage1()

    p2 = TwoStagePipeline(spec_scale=2, x=100, y=200, save_to=cache)
    with pytest.raises(ValueError, match="Invalid stage call order"):
        p2.stage2()


def test_calling_finished_stage_again_raises_order_error(tmp_path: Path) -> None:
    p = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=tmp_path / "cache")
    p.stage1()
    with pytest.raises(ValueError, match="Invalid stage call order"):
        p.stage1()


def test_order_guard_applies_when_cache_is_disabled() -> None:
    p = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=None)
    with pytest.raises(ValueError, match="Invalid stage call order"):
        p.stage2()


def test_name_mangled_saver_loader_are_used(tmp_path: Path) -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class MangledSerializerPipeline:
        spec_bias: int = spec()
        x: int = input()
        value: int = state(default=0)
        save_to: str | Path | None = transient(default=None)
        saver_calls: int = transient(default=0)
        loader_calls: int = transient(default=0)

        def run(self) -> None:
            self.step()

        @stage_func(id="step", order=0)
        def step(self) -> None:
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
    p1.run()
    assert p1.value == 3
    assert p1.saver_calls == 1
    assert p1.loader_calls == 0

    stage_file = cache / "step.pkl"
    assert stage_file.read_text(encoding="utf-8").startswith("{")

    p2 = MangledSerializerPipeline(spec_bias=1, x=100, save_to=cache)
    p2.run()
    assert p2.value == 3
    assert p2.saver_calls == 0
    assert p2.loader_calls == 1


def test_save_to_none_disables_persistence(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=None)
    p.run()

    assert p.execution_log == ["stage1", "stage2"]
    assert not cache.exists()


def test_input_and_transient_are_not_overridden_on_restore(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p1.run()

    p2 = TwoStagePipeline(spec_scale=2, x=77, y=88, save_to=cache)
    p2.run()

    assert p2.execution_log == []
    assert p2.x == 77
    assert p2.y == 88
    assert p2.final_output == 110


def test_spec_mismatch_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p1.run()

    p2 = TwoStagePipeline(spec_scale=99, x=2, y=3, save_to=cache)
    with pytest.raises(ValueError, match="Spec mismatch"):
        p2.run()


def test_field_schema_mismatch_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p1.run()

    @define_pipeline(save_path_field="save_to", kw_only=True)
    class SchemaChangedPipeline:
        spec_scale: int = spec()

        x: int = input()
        y: int = input()

        stage1_value: int = input(default=0)  # changed kind
        stage2_value: int = state(default=0)
        final_output: int = output(default=0)

        save_to: str | Path | None = transient(default=None)

        def run(self) -> None:
            self.stage1()

        @stage_func(id="stage1", order=0)
        def stage1(self) -> None:
            self.stage2_value = self.x + self.y

    p2 = SchemaChangedPipeline(spec_scale=2, x=2, y=3, save_to=cache)
    with pytest.raises(ValueError, match="Field schema mismatch"):
        p2.run()


def test_stage_order_is_driven_by_order_not_definition_order(tmp_path: Path) -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class ReorderedPipeline:
        spec_scale: int = spec()
        x: int = input()
        y: int = input()
        stage1_value: int = state(default=0)
        stage2_value: int = state(default=0)
        execution_log: list[str] = transient(factory=list)
        save_to: str | Path | None = transient(default=None)

        def run(self) -> None:
            self.stage1()
            self.stage2()

        @stage_func(id="stage2", order=1)
        def stage2(self) -> None:
            self.execution_log.append("stage2")
            self.stage2_value = self.stage1_value + 1

        @stage_func(id="stage1", order=0)
        def stage1(self) -> None:
            self.execution_log.append("stage1")
            self.stage1_value = (self.x + self.y) * self.spec_scale

    cache = tmp_path / "cache"
    p1 = ReorderedPipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p1.stage1()
    assert (cache / "stage1.pkl").exists()
    assert not (cache / "stage2.pkl").exists()

    p2 = ReorderedPipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p2.run()
    assert p2.execution_log == ["stage2"]
    assert p2.stage1_value == 10
    assert p2.stage2_value == 11


def test_inheritance_partial_cache_continuity_uses_order(tmp_path: Path) -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class BasePipeline:
        spec_scale: int = spec()
        x: int = input()
        y: int = input()
        stage1_value: int = state(default=0)
        stage2_value: int = state(default=0)
        save_to: str | Path | None = transient(default=None)

        def run(self) -> None:
            self.stage1()
            self.stage2()

        @stage_func(id="stage1", order=0)
        def stage1(self) -> None:
            self.stage1_value = (self.x + self.y) * self.spec_scale

        @stage_func(id="stage2", order=1)
        def stage2(self) -> None:
            self.stage2_value = self.stage1_value + 1

    @define_pipeline(save_path_field="save_to", kw_only=True)
    class ChildPipeline(BasePipeline):
        stage3_value: int = state(default=0)
        execution_log: list[str] = transient(factory=list)

        def run(self) -> None:
            self.stage1()
            self.stage2()
            self.stage3()

        @stage_func(id="stage3", order=2)
        def stage3(self) -> None:
            self.execution_log.append("stage3")
            self.stage3_value = self.stage2_value + 1

    cache = tmp_path / "cache"
    p1 = ChildPipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p1.stage1()
    p1.stage2()

    p2 = ChildPipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p2.run()
    assert p2.execution_log == ["stage3"]
    assert p2.stage1_value == 10
    assert p2.stage2_value == 11
    assert p2.stage3_value == 12


def test_runtime_state_is_isolated_per_instance_even_without_weakref_slot(tmp_path: Path) -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True, weakref_slot=False)
    class NoWeakrefPipeline:
        spec_scale: int = spec()
        calls: int = state(default=0)
        save_to: str | Path | None = transient(default=None)

        @stage_func(id="step", order=0)
        def step(self) -> None:
            self.calls += self.spec_scale

    p1 = NoWeakrefPipeline(spec_scale=1, save_to=None)
    p2 = NoWeakrefPipeline(spec_scale=1, save_to=tmp_path / "cache")

    runtime1 = object.__getattribute__(p1, "_alpenstock_runtime_state")
    runtime2 = object.__getattribute__(p2, "_alpenstock_runtime_state")
    assert runtime1 is not runtime2

    p1.step()
    p2.step()
    assert (tmp_path / "cache" / "step.pkl").exists()
