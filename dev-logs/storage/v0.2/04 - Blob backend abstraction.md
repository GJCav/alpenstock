# Blob backend abstraction

This document defines the abstract contract of the blob backend in `storage
v0.2`.

It is derived from the transaction model in `02 - Txn & nested txn.md` and the
backend split in `03 - Backend architecture.md`.

The blob backend is repo-scoped. It realizes committed values, copy-on-write
working state, durable prepared state, repo-local publication, and the per-repo
blob-side fence. It does not own transaction authority or recovery authority.

## 1. Proposed abstract interface

```python
from __future__ import annotations

from typing import Protocol


RepoId = str
Key = str
OpenMode = str

ValueRef = object
WorkingState = object
FenceState = object


class BlobBackend(Protocol):
    def read_committed_ref(self, repo: RepoId, key: Key) -> ValueRef | None:
        """Return the committed value ref for one key, or None if absent."""

    def open_working_state(
        self,
        repo: RepoId,
        key: Key,
        visible_ref: ValueRef | None,
        mode: OpenMode,
    ) -> WorkingState:
        """Create mutable CoW working state for one key."""

    def seal_working_state(
        self,
        repo: RepoId,
        key: Key,
        working: WorkingState,
    ) -> ValueRef:
        """Seal working state into one opaque value ref."""

    def ensure_prepared(
        self,
        repo: RepoId,
        overlay: dict[Key, ValueRef | None],
    ) -> None:
        """Ensure every pending put ref is durable enough for prepared state."""

    def publish_prepared(
        self,
        repo: RepoId,
        overlay: dict[Key, ValueRef | None],
    ) -> None:
        """Publish the prepared repo-local overlay as new committed state."""

    def discard_staged(
        self,
        repo: RepoId,
        overlay: dict[Key, ValueRef | None],
    ) -> None:
        """Discard or detach staged state that will not be published."""

    def read_fence(self, repo: RepoId) -> FenceState | None:
        """Read the per-repo blob-side fence."""

    def write_fence(self, repo: RepoId, fence: FenceState) -> None:
        """Write the per-repo blob-side fence."""

    def clear_fence(self, repo: RepoId) -> None:
        """Clear the per-repo blob-side fence when the repo becomes clear."""
```

## 2. Interface meaning

`ValueRef` is the implementation refinement of the logical value in the
transaction overlay:

```text
Δ : K -> Put(ValueRef) | Delete
```

It is backend-owned and opaque. The journal backend may carry it, but does not
interpret it.

`WorkingState` is mutable state for one open writer. It may be a repo-local
staged file, a temporary local file, or another backend-specific form. It is
not required to be durable across crash.

`FenceState` is the durable per-repo fence stored with the blob backend, as
required by `03 - Backend architecture.md`.

The overlay argument uses:

- `ref` for `Put(ref)`
- `None` for `Delete`

This is pseudo-code, not a final serialized shape.

## 3. Semantic obligations

The interface above is constrained by the transaction model.

### 3.1 Committed view

`read_committed_ref(...)` supports the rule:

- read-only open outside a transaction sees committed state

### 3.2 Open transaction state

`open_working_state(...)` and `seal_working_state(...)` support copy-on-write
mutation:

- working state starts from the current transaction-visible value when required
- committed state remains unchanged while the writer is open
- successful close yields one `ValueRef` for the overlay

### 3.3 Prepared state

`ensure_prepared(...)` is the critical method.

Before the journal backend may mark a transaction `prepared`, every pending put
must denote durable staged state recoverable after accidental process exit.

So after:

```python
backend.ensure_prepared(repo, overlay)
```

the following must hold:

- every non-`None` value ref in `overlay` is durable
- every non-`None` value ref in `overlay` is publishable
- the backend can still resolve those refs during recovery

### 3.4 Root commit

`publish_prepared(...)` is repo-local publication.

Its semantic meaning is:

```text
publish_prepared(R, Δ_prepared)
```

makes the committed state of repo `R` observationally equivalent to applying
`Δ_prepared` to the repo's old committed state.

The concrete publication primitive is backend-specific.

### 3.5 Abort

`discard_staged(...)` must ensure aborted staged state cannot later be mistaken
for committed state.

The backend may delete it immediately or make it unreachable and clean it up
later.

### 3.6 Fence

`read_fence(...)`, `write_fence(...)`, and `clear_fence(...)` maintain the
per-repo blob-side fence used for coordination discoverability and split-brain
prevention.

The fence is not the transaction authority, but it is part of the required blob
backend contract.

## 4. Derived invariants

Any concrete blob backend satisfying this abstraction must preserve:

- committed state is never modified in place by open writers
- `ValueRef` is opaque outside the blob backend
- after `ensure_prepared(...)`, all pending put refs are durable and
  backend-recoverable
- `publish_prepared(...)` acts on one repo domain only
- the per-repo fence is durable and stored with the repo's blob storage

## 5. Filesystem and OSS under the same interface

For a filesystem backend:

- `ValueRef` may point to committed files or staged files
- `WorkingState` may be a repo-local staged file
- `ensure_prepared(...)` may validate staged files
- `publish_prepared(...)` may use rename-style publication
- the fence may live in the repo control directory

For an OSS backend:

- `ValueRef` may point to committed objects or durable staged objects
- `WorkingState` may be a local temporary file while open
- `ensure_prepared(...)` may upload or validate durable staged remote objects
- `publish_prepared(...)` may update manifest/head state
- the fence may live in a repo control prefix in object storage

The uniformity comes from semantics, not from identical mechanics.

## 6. Deferred details

This document does not define:

- final Python runtime interfaces
- exact structures of `ValueRef`, `WorkingState`, or `FenceState`
- exact publication algorithms
- exact staged-state garbage collection

Those belong to later implementation-oriented documents.

## 7. Final formulation

A blob backend is a repo-scoped storage engine that provides committed value
references, mutable working state for copy-on-write editing, durable staged
state for `prepared`, repo-local publication of prepared overlays, and the
per-repo blob-side fence.
