from __future__ import annotations

from dataclasses import dataclass
import importlib
from pathlib import Path

import pytest

from alpenstock.pipeline import input, output, spec, state, transient, define_pipeline, stage_func
import alpenstock.pipeline._state_io as state_io


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


def test_multifield_spec_and_stage_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = MultiFieldPipeline(spec_a=1, spec_b={"k": 2}, x=10, y=3, save_to=cache)
    p1.run()
    assert p1.calls == 1
    assert p1.result == 32

    p2 = MultiFieldPipeline(spec_a=1, spec_b={"k": 2}, x=100, y=200, save_to=cache)
    p2.run()

    assert p2.calls == 1  # restored from cache
    assert p2.acc == 16
    assert p2.result == 32
    assert p2.x == 100
    assert p2.y == 200


def test_spec_mismatch_raises(tmp_path: Path) -> None:
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


def test_field_schema_mismatch_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    p1 = MultiFieldPipeline(spec_a=1, spec_b={"k": 2}, x=1, y=2, save_to=cache)
    p1.run()

    @define_pipeline(save_path_field="save_to", kw_only=True)
    class SchemaChangedPipeline:
        spec_a: int = spec()
        spec_b: dict[str, int] = spec()

        x: int = input()
        y: int = input()

        acc: int = input(default=0)  # changed kind
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


def test_save_path_field_must_exist() -> None:
    with pytest.raises(ValueError, match="save_path_field"):

        @define_pipeline(save_path_field="missing", kw_only=True)
        class InvalidPipeline:
            spec_a: int = spec()
            save_to: str | Path | None = transient(default=None)



def test_save_path_field_type_validation() -> None:
    with pytest.raises(TypeError, match=r"str \| Path \| None"):

        @define_pipeline(save_path_field="save_to", kw_only=True)
        class InvalidPipeline:
            spec_a: int = spec()
            save_to: int = transient(default=0)


def test_save_path_field_kind_validation() -> None:
    with pytest.raises(ValueError, match="must be marked as transient"):

        @define_pipeline(save_path_field="save_to", kw_only=True)
        class InvalidPipeline:
            spec_a: int = spec()
            save_to: str | Path | None = state(default=None)


def test_stage_return_none_contract(tmp_path: Path) -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class InvalidStageReturn:
        spec_a: int = spec()
        x: int = input()
        v: int = state(default=0)
        save_to: str | Path | None = transient(default=None)

        def run(self) -> None:
            self.bad_stage()

        @stage_func(id="bad", order=0)
        def bad_stage(self) -> None:
            self.v = self.x
            return 1  # type: ignore[return-value]

    p = InvalidStageReturn(spec_a=1, x=2, save_to=tmp_path / "cache")
    with pytest.raises(TypeError, match="must return None"):
        p.run()


def test_stage_signature_validation() -> None:
    with pytest.raises(TypeError, match="must accept no arguments"):

        @define_pipeline(save_path_field="save_to", kw_only=True)
        class InvalidStageSignature:
            spec_a: int = spec()
            save_to: str | Path | None = transient(default=None)

            @stage_func(id="bad", order=0)
            def bad(self, x: int) -> None:
                return None


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


def test_define_pipeline_exposes_dataclass_transform() -> None:
    transform_meta = getattr(define_pipeline, "__dataclass_transform__", None)
    assert isinstance(transform_meta, dict)
    specifiers = transform_meta.get("field_specifiers", ())
    names = {getattr(item, "__name__", "") for item in specifiers}
    assert {"spec", "state", "output", "input", "transient", "field"}.issubset(names)


def test_pipeline_exports_lowercase_helpers_only() -> None:
    pipeline_module = importlib.import_module("alpenstock.pipeline")
    assert {"spec", "state", "output", "input", "transient"}.issubset(set(dir(pipeline_module)))
    assert not {"Spec", "State", "Output", "Input", "Transient"} & set(dir(pipeline_module))


@pytest.mark.parametrize("name", ["Spec", "State", "Output", "Input", "Transient"])
def test_uppercase_helpers_are_not_importable(name: str) -> None:
    with pytest.raises(ImportError):
        exec(f"from alpenstock.pipeline import {name}", {})


def test_spec_rejects_on_setattr_override() -> None:
    with pytest.raises(ValueError, match="does not allow overriding on_setattr"):
        spec(on_setattr=None)


def test_define_pipeline_kw_only_allows_required_field_after_defaults(tmp_path: Path) -> None:
    @define_pipeline(save_path_field="save_path", kw_only=True)
    class KwOnlyPipeline:
        order: int = spec(default=2)
        x: float = input(default=0.0)
        y: float | None = output(default=None)
        save_path: str = transient()

        @stage_func(id="step", order=0)
        def step(self) -> None:
            self.y = self.x + self.order

    p = KwOnlyPipeline(save_path=str(tmp_path / "cache"))
    p.step()
    assert p.y == 2.0
