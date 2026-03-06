from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from alpenstock.pipeline import (
    define_pipeline,
    get_state_dict,
    input,
    load_spec,
    output,
    spec,
    stage_func,
    state,
    transient,
)


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
