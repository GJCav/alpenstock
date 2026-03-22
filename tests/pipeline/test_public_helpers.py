from __future__ import annotations

from dataclasses import dataclass
import importlib
from pathlib import Path

import attrs
from pydantic import BaseModel
import pytest

from alpenstock.pipeline import (
    define_pipeline,
    get_state_dict,
    input,
    load_pipeline,
    load_spec,
    output,
    spec,
    stage_func,
    state,
    transient,
)
from alpenstock.pipeline._fields import PIPELINE_KIND_METADATA_KEY


@define_pipeline(save_path_field="save_to", kw_only=True)
class HelperPipeline:
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


@dataclass
class HelperDataSpec:
    alpha: int
    beta: list[int]


@attrs.define
class HelperAttrsSpec:
    alpha: int
    beta: list[int]


@attrs.define
class HelperAliasedAttrsSpec:
    alpha: int = attrs.field(alias="alpha_value")
    beta: list[int] = attrs.field(alias="beta_values")


class HelperModelSpec(BaseModel):
    alpha: int
    beta: list[int]


def test_define_pipeline_exposes_dataclass_transform() -> None:
    transform_meta = getattr(define_pipeline, "__dataclass_transform__", None)
    assert isinstance(transform_meta, dict)
    specifiers = transform_meta.get("field_specifiers", ())
    names = {getattr(item, "__name__", "") for item in specifiers}
    assert {"spec", "state", "output", "input", "transient", "field"}.issubset(names)


def test_spec_docstring_mentions_pipeline_restrictions() -> None:
    doc = spec.__doc__
    assert isinstance(doc, str)
    assert "init=False" in doc
    assert "on_setattr" in doc


def test_input_docstring_mentions_pipeline_restrictions() -> None:
    doc = input.__doc__
    assert isinstance(doc, str)
    assert "init=False" in doc


def test_pipeline_exports_lowercase_helpers_only() -> None:
    pipeline_module = importlib.import_module("alpenstock.pipeline")
    assert {
        "spec",
        "state",
        "output",
        "input",
        "transient",
        "load_pipeline",
    }.issubset(set(dir(pipeline_module)))
    assert not {"Spec", "State", "Output", "Input", "Transient"} & set(dir(pipeline_module))


@pytest.mark.parametrize("name", ["Spec", "State", "Output", "Input", "Transient"])
def test_uppercase_helpers_are_not_importable(name: str) -> None:
    with pytest.raises(ImportError):
        exec(f"from alpenstock.pipeline import {name}", {})


def test_get_state_dict_defaults_to_state_and_output() -> None:
    p = HelperPipeline(spec_a=1, spec_b={"k": 2}, x=10, y=3, save_to=None)
    p.run()

    payload = get_state_dict(p)
    assert set(payload) == {"state", "output"}
    assert payload["state"] == {"acc": 16, "calls": 1}
    assert payload["output"] == {"result": 32}


def test_get_state_dict_can_include_all_kinds_and_normalizes_spec() -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class KindPipeline:
        cfg: tuple[int, int] = spec()
        x: int = input(default=3)
        v: int = state(default=4)
        y: int = output(default=5)
        tag: str = transient(default="tmp")
        save_to: str | Path | None = transient(default=None)

    p = KindPipeline(cfg=(1, 2), save_to=None)
    payload = get_state_dict(
        p,
        spec=True,
        input=True,
        state=True,
        transient=True,
        output=True,
    )
    assert set(payload) == {"spec", "input", "state", "transient", "output"}
    assert payload["spec"]["cfg"] == [1, 2]
    assert payload["input"] == {"x": 3}
    assert payload["state"] == {"v": 4}
    assert payload["output"] == {"y": 5}
    assert payload["transient"]["tag"] == "tmp"
    assert payload["transient"]["save_to"] is None


def test_get_state_dict_can_include_finished_markers() -> None:
    p = HelperPipeline(spec_a=1, spec_b={"k": 2}, x=10, y=3, save_to=None)
    p.run()

    payload = get_state_dict(p, include_finished_markers=True)
    assert payload["finished_markers"] == {"compute": True}


def test_get_state_dict_rejects_non_pipeline_instance() -> None:
    with pytest.raises(TypeError, match="not a pipeline class"):
        get_state_dict(object())


def test_load_spec_reads_spec_fields(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    p = HelperPipeline(spec_a=1, spec_b={"k": 2}, x=10, y=3, save_to=cache)
    p.run()

    payload = load_spec(HelperPipeline, cache)
    assert payload == {"spec_a": 1, "spec_b": {"k": 2}}


def test_load_spec_reconstructs_supported_typed_spec_values(tmp_path: Path) -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class TypedSpecPipeline:
        tuple_spec: tuple[int, int] = spec()
        data_spec: HelperDataSpec = spec()
        attrs_spec: HelperAttrsSpec = spec()
        model_spec: HelperModelSpec = spec()
        value: int = output(default=0)
        save_to: str | Path | None = transient(default=None)

        def run(self) -> None:
            self.step()

        @stage_func(id="step", order=0)
        def step(self) -> None:
            self.value = (
                sum(self.tuple_spec)
                + self.data_spec.alpha
                + self.attrs_spec.alpha
                + self.model_spec.alpha
            )

    cache = tmp_path / "cache"
    p = TypedSpecPipeline(
        tuple_spec=(1, 2),
        data_spec=HelperDataSpec(alpha=3, beta=[4, 5]),
        attrs_spec=HelperAttrsSpec(alpha=6, beta=[7, 8]),
        model_spec=HelperModelSpec(alpha=9, beta=[10, 11]),
        save_to=cache,
    )
    p.run()

    payload = load_spec(TypedSpecPipeline, cache)
    assert payload is not None
    assert payload["tuple_spec"] == (1, 2)
    assert payload["data_spec"] == HelperDataSpec(alpha=3, beta=[4, 5])
    assert payload["attrs_spec"] == HelperAttrsSpec(alpha=6, beta=[7, 8])
    assert isinstance(payload["model_spec"], HelperModelSpec)
    assert payload["model_spec"].model_dump() == {"alpha": 9, "beta": [10, 11]}


def test_load_spec_reconstructs_attrs_spec_with_aliases(tmp_path: Path) -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class AliasedAttrsSpecPipeline:
        attrs_spec: HelperAliasedAttrsSpec = spec()
        value: int = output(default=0)
        save_to: str | Path | None = transient(default=None)

        def run(self) -> None:
            self.step()

        @stage_func(id="step", order=0)
        def step(self) -> None:
            self.value = self.attrs_spec.alpha + len(self.attrs_spec.beta)

    cache = tmp_path / "cache"
    p = AliasedAttrsSpecPipeline(
        attrs_spec=HelperAliasedAttrsSpec(alpha_value=3, beta_values=[4, 5]),
        save_to=cache,
    )
    p.run()

    payload = load_spec(AliasedAttrsSpecPipeline, cache)
    assert payload is not None
    assert payload["attrs_spec"] == HelperAliasedAttrsSpec(alpha_value=3, beta_values=[4, 5])


def test_load_pipeline_can_restore_cached_pipeline_without_input_overrides(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    p1 = HelperPipeline(spec_a=1, spec_b={"k": 2}, x=10, y=3, save_to=cache)
    p1.run()

    loader = load_pipeline(cls=HelperPipeline, save_to=cache)
    p2 = loader()

    assert p2.x is None
    assert p2.y is None

    p2.run()

    assert p2.calls == 1
    assert p2.acc == 16
    assert p2.result == 32


def test_load_pipeline_preserves_explicit_input_overrides_on_cache_hit(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    p1 = HelperPipeline(spec_a=1, spec_b={"k": 2}, x=10, y=3, save_to=cache)
    p1.run()

    loader = load_pipeline(cls=HelperPipeline, save_to=cache)
    p2 = loader(x=77, y=88)
    p2.run()

    assert p2.x == 77
    assert p2.y == 88
    assert p2.acc == 16
    assert p2.result == 32


def test_load_pipeline_missing_spec_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="requires an existing cache directory"):
        load_pipeline(cls=HelperPipeline, save_to=tmp_path / "missing")


def test_load_pipeline_rejects_overrides_for_spec_and_save_path_fields(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    p1 = HelperPipeline(spec_a=1, spec_b={"k": 2}, x=10, y=3, save_to=cache)
    p1.run()

    loader = load_pipeline(cls=HelperPipeline, save_to=cache)

    with pytest.raises(TypeError, match="spec fields"):
        loader(spec_a=99)

    with pytest.raises(TypeError, match="outer `save_to=` argument"):
        loader(save_to=tmp_path / "other-cache")


def test_load_pipeline_does_not_auto_fill_required_transient_fields(tmp_path: Path) -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class ExtraTransientPipeline:
        spec_a: int = spec()
        x: int = input()
        token: str = transient()
        value: int = output(default=0)
        save_to: str | Path | None = transient(default=None)

        def run(self) -> None:
            self.step()

        @stage_func(id="step", order=0)
        def step(self) -> None:
            self.value = self.spec_a + self.x + len(self.token)

    cache = tmp_path / "cache"
    p1 = ExtraTransientPipeline(spec_a=1, x=2, token="abc", save_to=cache)
    p1.run()

    loader = load_pipeline(cls=ExtraTransientPipeline, save_to=cache)
    with pytest.raises(TypeError):
        loader()


def test_load_pipeline_does_not_backfill_init_false_save_path_field(tmp_path: Path) -> None:
    @define_pipeline(save_path_field="save_to", kw_only=True)
    class InitFalseSavePathPipeline:
        spec_a: int = spec()
        value: int = output(default=0)
        save_to: str | Path | None = transient(default=None, init=False)

        def run(self) -> None:
            self.step()

        @stage_func(id="step", order=0)
        def step(self) -> None:
            self.value = self.spec_a

    cache = tmp_path / "cache"
    p = InitFalseSavePathPipeline(spec_a=1)
    object.__setattr__(p, "save_to", cache)
    p.run()

    loader = load_pipeline(cls=InitFalseSavePathPipeline, save_to=cache)
    with pytest.raises(TypeError, match="cannot set init=False save path fields"):
        loader()


def test_spec_rejects_init_false() -> None:
    with pytest.raises(ValueError, match="does not allow init=False"):

        @define_pipeline(save_path_field="save_to", kw_only=True)
        class InvalidSpecInitPipeline:
            bad_spec: int = spec(init=False)
            save_to: str | Path | None = transient(default=None)


def test_input_rejects_init_false() -> None:
    with pytest.raises(ValueError, match="does not allow init=False"):

        @define_pipeline(save_path_field="save_to", kw_only=True)
        class InvalidInputInitPipeline:
            spec_a: int = spec()
            bad_input: int = input(init=False)
            save_to: str | Path | None = transient(default=None)


def test_pipeline_meta_rejects_init_false_spec_even_without_spec_helper() -> None:
    with pytest.raises(ValueError, match="constructor-initialized"):

        @define_pipeline(save_path_field="save_to", kw_only=True)
        class InvalidMetadataSpecPipeline:
            bad_spec: int = attrs.field(
                init=False,
                default=1,
                metadata={PIPELINE_KIND_METADATA_KEY: "spec"},
            )
            save_to: str | Path | None = transient(default=None)


def test_pipeline_meta_rejects_init_false_input_even_without_input_helper() -> None:
    with pytest.raises(ValueError, match="constructor-initialized"):

        @define_pipeline(save_path_field="save_to", kw_only=True)
        class InvalidMetadataInputPipeline:
            spec_a: int = spec()
            bad_input: int = attrs.field(
                init=False,
                default=1,
                metadata={PIPELINE_KIND_METADATA_KEY: "input"},
            )
            save_to: str | Path | None = transient(default=None)


def test_load_spec_can_return_full_payload_with_field_schema(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    p = HelperPipeline(spec_a=1, spec_b={"k": 2}, x=10, y=3, save_to=cache)
    p.run()

    payload = load_spec(HelperPipeline, cache, include_field_schema=True)
    assert payload is not None
    assert payload["spec_fields"] == {"spec_a": 1, "spec_b": {"k": 2}}
    assert payload["field_schema"]["save_to"] == "transient"


def test_load_spec_field_schema_mismatch_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    p = HelperPipeline(spec_a=1, spec_b={"k": 2}, x=10, y=3, save_to=cache)
    p.run()

    @define_pipeline(save_path_field="save_to", kw_only=True)
    class MismatchSchemaPipeline:
        spec_a: int = spec()
        x: int = input(default=0)
        value: int = state(default=0)
        save_to: str | Path | None = transient(default=None)

    with pytest.raises(ValueError, match="Field schema mismatch"):
        load_spec(MismatchSchemaPipeline, cache)


def test_load_spec_invalid_payload_missing_field_schema_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    p = HelperPipeline(spec_a=1, spec_b={"k": 2}, x=10, y=3, save_to=cache)
    p.run()

    (cache / "spec.yaml").write_text(
        "spec_fields:\n  spec_a: 1\n  spec_b:\n    k: 2\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing field_schema mapping"):
        load_spec(HelperPipeline, cache)


def test_load_spec_returns_none_when_spec_file_missing(tmp_path: Path) -> None:
    payload = load_spec(HelperPipeline, tmp_path / "missing")
    assert payload is None


def test_load_spec_rejects_non_pipeline_class(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="not a pipeline class"):
        load_spec(object, tmp_path)


def test_get_state_dict_finished_markers_is_memory_runtime_only(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    p1 = HelperPipeline(spec_a=1, spec_b={"k": 2}, x=10, y=3, save_to=cache)
    p1.run()

    p2 = HelperPipeline(spec_a=1, spec_b={"k": 2}, x=10, y=3, save_to=cache)
    payload = get_state_dict(p2, include_finished_markers=True)
    assert payload["finished_markers"] == {}
