from __future__ import annotations

import ast
from dataclasses import dataclass
import pickle
from pathlib import Path

import attrs
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
import alpenstock.pipeline._state_io as state_io


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


def test_read_only_load_pipeline_uses_cache_without_executing_stages(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p1.run()

    p2 = load_pipeline(cls=TwoStagePipeline, cache_dir=cache)(x=100, y=200)
    p2.run()

    assert p2.execution_log == []
    assert p2.stage1_value == 10
    assert p2.stage2_value == 11
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


def test_read_only_load_pipeline_missing_stage_snapshot_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p1.run()
    (cache / "stage2.pkl").unlink()

    p2 = load_pipeline(cls=TwoStagePipeline, cache_dir=cache)()
    with pytest.raises(FileNotFoundError, match="read_only=False"):
        p2.run()

    assert p2.execution_log == []


def test_read_only_load_pipeline_incomplete_stage_snapshot_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p1.run()

    stage2_path = cache / "stage2.pkl"
    payload = pickle.loads(stage2_path.read_bytes())
    del payload["__stage_finished_stage2"]
    stage2_path.write_bytes(pickle.dumps(payload))

    p2 = load_pipeline(cls=TwoStagePipeline, cache_dir=cache)()
    with pytest.raises(ValueError, match="incomplete or unfinished"):
        p2.run()

    assert p2.execution_log == []


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


def test_load_pipeline_read_write_mode_can_rerun_from_first_missing_stage(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p1.run()
    (cache / "stage1.pkl").unlink()
    (cache / "stage2.pkl").unlink()

    p2 = load_pipeline(cls=TwoStagePipeline, cache_dir=cache, read_only=False)(x=100, y=200)
    p2.run()

    assert p2.execution_log == ["stage1", "stage2"]
    assert p2.stage1_value == 600
    assert p2.final_output == 6010


def test_load_pipeline_read_write_mode_requires_real_inputs(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = TwoStagePipeline(spec_scale=2, x=2, y=3, save_to=cache)
    p1.run()

    loader = load_pipeline(cls=TwoStagePipeline, cache_dir=cache, read_only=False)
    with pytest.raises(TypeError):
        loader()


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


@define_pipeline(save_path_field="save_to", kw_only=True)
class MultiFieldPipeline:
    spec_a: int = spec()
    spec_b: dict[str, int] = spec()

    x: int = input()
    y: int = input()

    acc: int = state(default=0)
    calls: int = state(default=0)
    result: int = output(default=0)

    save_to: str | Path | None = transient(default=None)

    def run(self) -> None:
        self.compute()

    @stage_func(id="compute", order=0)
    def compute(self) -> None:
        self.calls += 1
        self.acc = self.x + self.y + self.spec_a + self.spec_b["k"]
        self.result = self.acc * 2


@attrs.define
class ReloadAliasedSpec:
    alpha: int = attrs.field(alias="alpha_value")
    beta: list[int] = attrs.field(alias="beta_values")


def test_multifield_spec_and_stage_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = MultiFieldPipeline(spec_a=1, spec_b={"k": 2}, x=10, y=3, save_to=cache)
    p1.run()
    assert p1.calls == 1
    assert p1.result == 32

    p2 = MultiFieldPipeline(spec_a=1, spec_b={"k": 2}, x=100, y=200, save_to=cache)
    p2.run()

    assert p2.calls == 1
    assert p2.acc == 16
    assert p2.result == 32
    assert p2.x == 100
    assert p2.y == 200


def test_multifield_spec_mismatch_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = MultiFieldPipeline(spec_a=1, spec_b={"k": 2}, x=1, y=2, save_to=cache)
    p1.run()

    p2 = MultiFieldPipeline(spec_a=1, spec_b={"k": 999}, x=1, y=2, save_to=cache)
    with pytest.raises(ValueError, match="Spec mismatch"):
        p2.run()


def test_spec_file_uses_block_style_for_nested_mapping(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    p = MultiFieldPipeline(spec_a=1, spec_b={"k": 2}, x=10, y=3, save_to=cache)
    p.run()

    text = (cache / "spec.yaml").read_text(encoding="utf-8")
    assert "spec_b:\n" in text
    assert "spec_b: {k: 2}" not in text
    assert "k: 2" in text


def test_multifield_field_schema_mismatch_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = MultiFieldPipeline(spec_a=1, spec_b={"k": 2}, x=1, y=2, save_to=cache)
    p1.run()

    @define_pipeline(save_path_field="save_to", kw_only=True)
    class SchemaChangedPipeline:
        spec_a: int = spec()
        spec_b: dict[str, int] = spec()

        x: int = input()
        y: int = input()

        acc: int = input(default=0)
        calls: int = state(default=0)
        result: int = output(default=0)

        save_to: str | Path | None = transient(default=None)

        def run(self) -> None:
            self.compute()

        @stage_func(id="compute", order=0)
        def compute(self) -> None:
            self.calls += 1

    p2 = SchemaChangedPipeline(spec_a=1, spec_b={"k": 2}, x=1, y=2, save_to=cache)
    with pytest.raises(ValueError, match="Field schema mismatch"):
        p2.run()


def test_atomic_write_failure_keeps_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "target.txt"
    path.write_text("old", encoding="utf-8")

    def boom(src, dst):
        raise RuntimeError("replace failed")

    monkeypatch.setattr(state_io.os, "replace", boom)

    with pytest.raises(RuntimeError, match="replace failed"):
        state_io.atomic_write_text(path, "new")

    assert path.read_text(encoding="utf-8") == "old"


def test_spec_supports_dataclass(tmp_path: Path) -> None:
    @dataclass
    class ExtraSpec:
        alpha: int
        beta: list[int]

    @define_pipeline(save_path_field="save_to", kw_only=True)
    class DataclassSpecPipeline:
        main_spec: ExtraSpec = spec()
        x: int = input()
        v: int = state(default=0)
        save_to: str | Path | None = transient(default=None)

        def run(self) -> None:
            self.work()

        @stage_func(id="work", order=0)
        def work(self) -> None:
            self.v = self.main_spec.alpha + sum(self.main_spec.beta) + self.x

    cache = tmp_path / "cache"
    p1 = DataclassSpecPipeline(main_spec=ExtraSpec(alpha=1, beta=[2, 3]), x=5, save_to=cache)
    p1.run()
    assert p1.v == 11

    p2 = DataclassSpecPipeline(main_spec=ExtraSpec(alpha=1, beta=[2, 3]), x=100, save_to=cache)
    p2.run()
    assert p2.v == 11


def test_load_pipeline_read_write_mode_reconstructs_aliased_attrs_spec_for_rerun(
    tmp_path: Path,
) -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class AliasedAttrsSpecPipeline:
        main_spec: ReloadAliasedSpec = spec()
        x: int = input()
        stage1_value: int = state(default=0)
        final_value: int = output(default=0)
        save_to: str | Path | None = transient(default=None)

        def run(self) -> None:
            self.stage1()
            self.stage2()

        @stage_func(id="stage1", order=0)
        def stage1(self) -> None:
            self.stage1_value = self.main_spec.alpha + sum(self.main_spec.beta) + self.x

        @stage_func(id="stage2", order=1)
        def stage2(self) -> None:
            self.final_value = self.stage1_value + self.main_spec.alpha

    cache = tmp_path / "cache"
    p1 = AliasedAttrsSpecPipeline(
        main_spec=ReloadAliasedSpec(alpha_value=1, beta_values=[2, 3]),
        x=5,
        save_to=cache,
    )
    p1.run()
    (cache / "stage1.pkl").unlink()
    (cache / "stage2.pkl").unlink()

    p2 = load_pipeline(cls=AliasedAttrsSpecPipeline, cache_dir=cache, read_only=False)(x=100)
    p2.run()

    assert isinstance(p2.main_spec, ReloadAliasedSpec)
    assert p2.stage1_value == 106
    assert p2.final_value == 107
