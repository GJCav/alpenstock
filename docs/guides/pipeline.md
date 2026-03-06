# Pipeline (Decorator-Only)

This guide introduces Alpenstock's lightweight pipeline utility for stage-based execution with disk cache and resume support.

## Design Summary

- No base class is required.
- You define an `attrs` class and decorate it with `@define_pipeline(...)`.
- Stage methods are decorated with `@stage_func(id="...", order=<int>)`.
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

## Runtime Behavior

- If `save_to` is `None`, no cache is used.
- Stage calls are strictly ordered by ascending `order`; only the next stage can be called.
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

## Validation Rules

- `save_path_field` must point to a `transient` field.
- `save_path_field` annotation must be compatible with `str | Path` (optionally `None`).
- Stage IDs must be unique in one class.
- Stage IDs must match `^[A-Za-z0-9_]+$`.
- Stage `order` must be an integer (`bool` is rejected), `>= 0`, and unique in one class hierarchy.
- Stage methods must take only `self` and return `None`.
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
- Custom loader must return a `dict`.

## Notes and Limitations

- Cache key is effectively `save_to + stage_id`.
- No automatic cache invalidation is performed for code/input changes.
- Different pipelines should not share the same `save_to` directory.
- No concurrency guarantees are provided for sharing one cache path across processes.
- The framework rejects direct calls to non-next stages; users must call stages in strict `order`.
- `spec` freezing does not prevent in-place mutation of nested mutable objects (for example, `dict`/`list`).
