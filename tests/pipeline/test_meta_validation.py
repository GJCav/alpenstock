from __future__ import annotations

from pathlib import Path

import pytest

from alpenstock.pipeline import input, output, spec, state, transient, define_pipeline, stage_func


def test_duplicate_stage_id_in_same_class_raises() -> None:
    with pytest.raises(ValueError, match="Duplicated stage id"):

        @define_pipeline(save_path_field="save_to", kw_only=True)
        class InvalidPipeline:
            spec_a: int = spec()
            save_to: str | Path | None = transient(default=None)

            @stage_func(id="dup", order=0)
            def first(self) -> None:
                return None

            @stage_func(id="dup", order=1)
            def second(self) -> None:
                return None


def test_duplicate_stage_id_across_inheritance_raises() -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class BasePipeline:
        spec_a: int = spec()
        save_to: str | Path | None = transient(default=None)

        @stage_func(id="dup", order=0)
        def base_stage(self) -> None:
            return None

    with pytest.raises(ValueError, match="Duplicated stage id"):

        @define_pipeline(save_path_field="save_to", kw_only=True)
        class ChildPipeline(BasePipeline):
            @stage_func(id="dup", order=1)
            def child_stage(self) -> None:
                return None


def test_duplicate_stage_order_in_same_class_raises() -> None:
    with pytest.raises(ValueError, match="Duplicated stage order"):

        @define_pipeline(save_path_field="save_to", kw_only=True)
        class InvalidPipeline:
            spec_a: int = spec()
            save_to: str | Path | None = transient(default=None)

            @stage_func(id="a", order=0)
            def first(self) -> None:
                return None

            @stage_func(id="b", order=0)
            def second(self) -> None:
                return None


def test_duplicate_stage_order_across_inheritance_raises() -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class BasePipeline:
        spec_a: int = spec()
        save_to: str | Path | None = transient(default=None)

        @stage_func(id="base", order=0)
        def base_stage(self) -> None:
            return None

    with pytest.raises(ValueError, match="Duplicated stage order"):

        @define_pipeline(save_path_field="save_to", kw_only=True)
        class ChildPipeline(BasePipeline):
            @stage_func(id="child", order=0)
            def child_stage(self) -> None:
                return None


def test_stage_override_is_allowed_and_effective() -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class BasePipeline:
        spec_a: int = spec()
        value: int = state(default=0)
        save_to: str | Path | None = transient(default=None)

        def run(self) -> None:
            self.step()

        @stage_func(id="step", order=0)
        def step(self) -> None:
            self.value = 1

    @define_pipeline(save_path_field="save_to", kw_only=True)
    class ChildPipeline(BasePipeline):
        @stage_func(id="step", order=0)
        def step(self) -> None:
            self.value = 2

    p = ChildPipeline(spec_a=1, save_to=None)
    p.run()
    assert p.value == 2


def test_inherited_stage_executes_without_redeclaration() -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class BasePipeline:
        spec_a: int = spec()
        value: int = state(default=0)
        save_to: str | Path | None = transient(default=None)

        def run(self) -> None:
            self.step()

        @stage_func(id="step", order=0)
        def step(self) -> None:
            self.value = self.spec_a

    @define_pipeline(save_path_field="save_to", kw_only=True)
    class ChildPipeline(BasePipeline):
        pass

    p = ChildPipeline(spec_a=42, save_to=None)
    p.run()
    assert p.value == 42


def test_stage_call_on_non_pipeline_class_raises() -> None:
    class PlainClass:
        @stage_func(id="plain", order=0)
        def stage(self) -> None:
            return None

    p = PlainClass()
    with pytest.raises(TypeError, match="not a pipeline class"):
        p.stage()


def test_stage_call_with_runtime_arguments_raises(tmp_path: Path) -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class Pipeline:
        spec_a: int = spec()
        save_to: str | Path | None = transient(default=None)

        @stage_func(id="step", order=0)
        def step(self) -> None:
            return None

    p = Pipeline(spec_a=1, save_to=tmp_path / "cache")
    with pytest.raises(TypeError, match="does not accept arguments"):
        p.step(123)  # type: ignore[call-arg]


@pytest.mark.parametrize("bad_id", ["../escape", "a/b", "a-b"])
def test_stage_id_rejects_unsafe_characters(bad_id: str) -> None:
    with pytest.raises(ValueError, match="must match"):

        @define_pipeline(save_path_field="save_to", kw_only=True)
        class InvalidStageIdPipeline:
            spec_a: int = spec()
            save_to: str | Path | None = transient(default=None)

            @stage_func(id=bad_id, order=0)
            def step(self) -> None:
                return None


def test_stage_order_rejects_negative_value() -> None:
    with pytest.raises(ValueError, match="order must be >= 0"):

        @define_pipeline(save_path_field="save_to", kw_only=True)
        class InvalidStageOrderPipeline:
            spec_a: int = spec()
            save_to: str | Path | None = transient(default=None)

            @stage_func(id="step", order=-1)
            def step(self) -> None:
                return None


@pytest.mark.parametrize("bad_order", [True, 1.5, "1"])
def test_stage_order_rejects_non_int_values(bad_order: object) -> None:
    with pytest.raises(TypeError, match="order must be int"):

        @define_pipeline(save_path_field="save_to", kw_only=True)
        class InvalidStageOrderTypePipeline:
            spec_a: int = spec()
            save_to: str | Path | None = transient(default=None)

            @stage_func(id="step", order=bad_order)  # type: ignore[arg-type]
            def step(self) -> None:
                return None
