# Pipeline (Decorator-Only)

This guide introduces Alpenstock's lightweight pipeline utility for stage-based execution with disk cache and resume support.

## Design Summary

- No base class is required.
- You define an `attrs` class and decorate it with `@define_pipeline(...)`.
- Stage methods are decorated with `@stage_func(id="...", order=<int>)` and may be sync or async.
- Cache files are saved under a user field bound via `save_path_field`.
- Stage payload uses pickle by default, with optional `__saver/__loader` hooks.

## Field Kinds

Use the helper fields to declare semantics:

- `spec()`
- `input()`
- `state()`
- `output()`
- `transient()`

All kinds can appear multiple times, including `spec`.

Unmarked fields are treated as `transient`.

`spec()` fields are frozen after construction, and `on_setattr` cannot be overridden.

## Minimal Example

```python
from pathlib import Path
from alpenstock.pipeline import (
    define_pipeline,
    stage_func,
    spec,
    input,
    state,
    output,
    transient,
)

@define_pipeline(save_path_field="save_to", kw_only=True)
class ToyPipeline:
    spec_lr: float = spec()
    spec_steps: int = spec(default=10)

    x: float = input()
    y: float = input()

    w: float = state(default=0.0)
    loss: float = output(default=0.0)

    save_to: str | Path | None = transient(default=None)

    def run(self) -> None:
        self.init_stage()
        self.train_stage()

    @stage_func(id="init", order=0)
    def init_stage(self) -> None:
        self.w = self.x + self.y

    @stage_func(id="train", order=1)
    def train_stage(self) -> None:
        self.w = self.w - self.spec_lr * self.spec_steps
        self.loss = self.w * self.w
```

## Async Example

```python
import asyncio
from pathlib import Path

from alpenstock.pipeline import define_pipeline, stage_func, spec, input, state, output, transient

@define_pipeline(save_path_field="save_to", kw_only=True)
class AsyncToyPipeline:
    spec_lr: float = spec()
    x: float = input()
    y: float = input()

    w: float = state(default=0.0)
    loss: float = output(default=0.0)

    save_to: str | Path | None = transient(default=None)

    async def run(self) -> None:
        await self.init_stage()
        await self.train_stage()

    @stage_func(id="init", order=0)
    async def init_stage(self) -> None:
        await asyncio.sleep(0)
        self.w = self.x + self.y

    @stage_func(id="train", order=1)
    async def train_stage(self) -> None:
        await asyncio.sleep(0)
        self.w = self.w - self.spec_lr
        self.loss = self.w * self.w
```

## Runtime Behavior

- If `save_to` is `None`, no cache is used.
- Stage calls are strictly ordered by ascending `order`; only the next stage can be called.
- Async stages must be awaited from async entry methods.
- Same-instance concurrent stage execution is unsupported and may fail fast.
- Sync stages remain sync; if you call them from async code, they may block the event loop.
- If `save_to` is set, bootstrap happens on the first stage call (not before the entry method):
  - validate `spec.yaml` (`spec_fields` + `field_schema`)
  - if `spec.yaml` is missing while any `*.pkl` snapshot exists, raise an error and require manual cleanup
  - validate stage snapshot continuity by stage `order`
  - write `spec.yaml` only for a fresh cache (no stage snapshots yet)
- Each completed stage writes `<stage_id>.pkl`.
- On stage cache hit:
  - stage body is skipped when its finished marker exists
  - `state` and `output` are restored from snapshot
  - `input` and `transient` keep current instance values
- In read-only mode, after the instance has been constructed:
  - bootstrap does not create cache directories or write `spec.yaml`
  - stage calls only restore from existing snapshots and do not execute the stage body
  - missing or incomplete stage snapshots raise an error immediately

## Validation Rules

- `save_path_field` must point to a `transient` field.
- `save_path_field` annotation must be compatible with `str | Path` (optionally `None`).
- `spec` and `input` fields must be constructor-initialized (`init=False` is not allowed).
- Stage IDs must be unique in one class.
- Stage IDs must match `^[A-Za-z0-9_]+$`.
- Stage `order` must be an integer (`bool` is rejected), `>= 0`, and unique in one class hierarchy.
- Stage methods must take only `self`.
- Sync stage methods must return `None`.
- Async stage methods must resolve to `None` when awaited.
- Stage calls must be strictly sequential by `order`; calling a future or already-finished stage raises an error.
- On resume, `spec_fields` and field schema (`name -> kind`) must match `spec.yaml`.
- If `spec.yaml` is missing and any `*.pkl` exists, bootstrap treats cache as corrupted and raises.
- If an earlier stage snapshot is missing but a later snapshot exists, bootstrap raises an error and asks users to clean cache manually.

## Persistence Model

`spec.yaml` stores:

- `spec_fields`: normalized values of all `spec` fields
- `field_schema`: all fields with kind labels

Each stage snapshot stores:

- full `state + output`
- stage completion markers `__stage_finished_<id>`

Writes use atomic replace semantics.

## Serialization Hooks

- Default stage payload serialization uses pickle.
- You can override stage payload I/O by implementing `__saver(path, payload)` and `__loader(path)`.
- Hook lookup supports Python name mangling, so `def __saver(...)` and `def __loader(...)` work as expected.
- In async stage execution, the sync hooks are invoked in worker threads so they do not block the event loop.
- Custom loader must return a `dict`.

## Helper Functions

The module also exposes three global helper functions:

- `get_state_dict(ins, *, spec=False, input=False, state=True, transient=False, output=True, include_finished_markers=False)`
- `load_pipeline(*, cls, save_to, read_only=True)(**overrides)`
- `load_spec(cls, save_to, *, include_field_schema=False)`

`get_state_dict` returns grouped runtime data by kind. Example:

```python
from pathlib import Path
from alpenstock.pipeline import get_state_dict, load_spec

p = ToyPipeline(
    spec_lr=0.1,
    spec_steps=10,
    x=1.0,
    y=2.0,
    save_to=Path("./cache"),
)
p.run()

snapshot = get_state_dict(
    p,
    spec=True,
    input=True,
    state=True,
    output=True,
    include_finished_markers=True,
)
# {
#   "spec": {...}, "input": {...}, "state": {...}, "output": {...},
#   "finished_markers": {"init": True, "train": True}
# }
```

When `include_finished_markers=True`, markers come from the current instance runtime memory only.
The function does not bootstrap or read cache files from disk.

`load_pipeline` is a convenience helper for reopening an existing cache:

```python
from pathlib import Path
from alpenstock.pipeline import load_pipeline

p = load_pipeline(
    cls=ToyPipeline,
    save_to=Path("./cache"),
)()
p.run()  # only restores cached stages; does not execute stage bodies
print(p.loss)

p_rw = load_pipeline(
    cls=ToyPipeline,
    save_to=Path("./cache"),
    read_only=False,
)(x=1.0, y=2.0)
```

Behavior:

- The outer call controls loader behavior (`cls`, `save_to`, `read_only`).
- The inner call forwards keyword overrides to the pipeline constructor.
- Saved `spec` fields are always loaded from `spec.yaml`; constructor overrides cannot replace them.
- The configured `save_path_field` is always bound from the outer `save_to` argument; do not pass it again in the inner overrides.
- In read-only mode, missing required `input` fields are auto-filled with `None` if the constructor still needs them.
- In read-only mode, the returned instance reuses existing cache on stage calls; missing `spec.yaml`, a missing stage snapshot, or an incomplete stage snapshot raise clear errors instead of rebuilding cache through normal stage execution.
- In read-write mode, normal cache/resume behavior is preserved, so callers should pass real input values.
- `load_pipeline` does not backfill `init=False` fields after construction. If `save_path_field` is `init=False`, the constructor itself must initialize it to the provided `save_to`.
- `load_pipeline` type hints are intentionally approximate. Runtime checks and this guide define the exact calling constraints.

`load_spec` reads `<save_to>/spec.yaml` for a pipeline class:

```python
spec_fields = load_spec(ToyPipeline, Path("./cache"))
full_spec = load_spec(ToyPipeline, Path("./cache"), include_field_schema=True)
```

If `spec.yaml` does not exist, `load_spec` returns `None`.
If `spec.yaml` exists, `load_spec` validates cached `field_schema` against the provided pipeline class.
For supported annotations, `load_spec` also reconstructs spec values back into Python objects:

- container types such as `list[...]`, `tuple[...]`, and `dict[str, ...]` are rebuilt recursively
- Python `dataclass` types are rebuilt recursively from their field annotations
- `attrs.define` classes and pydantic models receive the cached mapping and handle their own construction for constructor-initialized fields
- unsupported or unconstrained annotations fall back to the raw YAML-loaded value
- `spec(init=False)` and `input(init=False)` are unsupported by design and rejected when defining the pipeline

## Best Practices

- Keep `__attrs_post_init__` lightweight. It is fine for setup work such as allocating buffers or preparing lightweight runtime objects.
- Put real data loading, expensive initialization, and any logic that depends on `input` into the first stage whenever possible.
- `load_pipeline(..., read_only=True)` is a best-effort reopen mode. Constructor hooks such as `__attrs_post_init__` still run during instance creation, so strict read-only behavior only applies once stage wrappers take over.
- This keeps cache, resume, and read-only reopening behavior aligned: `load_pipeline(..., read_only=True)` works best when constructor hooks do not perform stage-like work.

## Notes and Limitations

- Cache key is effectively `save_to + stage_id`.
- No automatic cache invalidation is performed for code/input changes.
- Different pipelines should not share the same `save_to` directory.
- No concurrency guarantees are provided for sharing one cache path across processes.
- The framework rejects direct calls to non-next stages; users must call stages in strict `order`.
- In async pipelines, `__saver/__loader` stay sync hooks and are run in worker threads; do not rely on them being awaitable.
- `spec` freezing does not prevent in-place mutation of nested mutable objects (for example, `dict`/`list`).
- `load_pipeline` is best suited for inspecting or reopening an existing cache. If a class validates or consumes `input` during construction, callers may still need to provide those fields explicitly even in read-only mode.
