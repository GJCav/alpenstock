# Backend-Neutral Transactional Storage Implementation Plan

Date: 2026-04-09

## 1. Goal

Implement a new module at `src/alpenstock/storage` that realizes the Phase 2 backend-neutral transaction semantics from [01 - Initial Design.md](./01%20-%20Initial%20Design.md), while keeping the design small, typed, and extensible.

The implementation must preserve the semantic model:

- committed state `S : K -> V ∪ {⊥}`
- transaction `Tx = (S0, Δ, state)`
- overlay lookup `SΔ`
- `prepare()` as overlay freeze + commit intent
- `commit()` as exactly one application of `Δ`
- recovery outcome constrained to either `S0` or `S0 ⊕ Δ`

This plan intentionally prioritizes:

- semantic correctness over backend cleverness
- an internal architecture that supports multiple backends
- a direct-file backend as the first concrete realization
- strong type checking and editor support
- small modules, with a soft cap of 800 lines per Python file

## 2. Non-Goals for the First Delivery

The first implementation should not attempt all future capabilities at once.

Explicitly defer:

- MySQL/PostgreSQL backends
- distributed transactions
- async APIs
- file watching
- high-volume performance tuning
- large-scale WAL retention tooling
- schema-driven user hierarchy binding from `proposal.py`

The first delivery should prove the contract, not the full product surface.

## 3. Guiding Design Choices

### 3.1 Keep the semantic core backend-neutral

The transaction engine should be written in terms of logical keys, overlays, lifecycle states, and recovery obligations.

No direct-file assumptions should leak into the core transaction contract.

### 3.2 Make the direct-file backend the reference backend

The direct-file backend is the best first implementation because it matches Phase 1 directly and exercises the hardest recovery rules.

If the direct-file backend can satisfy the contract cleanly, the abstraction is likely sound.

### 3.3 Prefer new dependencies last

Use the Python standard library unless an external dependency clearly removes significant risk.
For the MVP, do not add new runtime dependencies beyond packages the project already carries unless correctness work proves they are necessary.

Recommended initial dependency choices:

- stdlib only by default for the core implementation
- `attrs` may be used sparingly for small typed runtime records only if it clearly improves clarity without changing the architecture
- `typing_extensions` only if needed for a typing feature missing in Python 3.11
- `sqlite3` from the stdlib for a later backend prototype

Rationale for keeping `attrs`:

- the project already depends on it
- the planned user-facing storage schema layer also prefers it
- it keeps internal state records concise without introducing a new dependency

Filesystem backend portability note:

- the MVP filesystem backend should target one clearly documented locking model
- the MVP target is POSIX only unless cross-platform support is explicitly promoted into scope
- if cross-platform filesystem locking becomes an MVP requirement, pause and reevaluate whether a lock library is necessary before proceeding

## 4. Package Layout

Create `src/alpenstock/storage` with common user-facing API at the top level, shared semantic core in internal modules, and backend implementations isolated in dedicated subpackages:

```text
src/alpenstock/storage/
  __init__.py
  repo.py
  file.py
  transaction.py
  backends/
    __init__.py
    fs/
      __init__.py
      backend.py
      layout.py
      locking.py
      recovery.py
  _types.py
  _errors.py
  _overlay.py
  _handles.py
  _backend.py
  _tx_core.py
```

Target responsibilities:

- `_types.py`: typed aliases, enums, small immutable records
- `_errors.py`: domain-specific exceptions
- `_overlay.py`: overlay logic and overlay application helpers
- `_tx_core.py`: backend-neutral transaction state machine
- `_handles.py`: buffered file-handle semantics over logical keys
- `_backend.py`: backend protocol and backend transaction protocol
- `repo.py`: public `Repo` API and repo-level transaction entry points
- `file.py`: public `FileNode` API and helper methods
- `transaction.py`: public transaction context and lightweight orchestration glue
- `backends/fs/layout.py`: filesystem directory/WAL/staging paths
- `backends/fs/locking.py`: writer ownership and process locking helpers
- `backends/fs/backend.py`: direct-file backend implementation
- `backends/fs/recovery.py`: direct-file recovery logic

If a file approaches 800 lines, split before adding more features.

Dependency direction rule:

- top-level public modules (`repo.py`, `file.py`, `transaction.py`, `__init__.py`) may depend on internal core modules plus backend protocols, but must not depend directly on a specific backend implementation
- `_types.py`, `_errors.py`, `_overlay.py`, `_backend.py` must not import backend packages
- `_tx_core.py` and `_handles.py` may depend on core modules only
- `backends/fs/*` may depend on core modules, but core modules and public top-level API modules must not depend on `backends/fs/*`

## 5. Public Surface for the First Delivery

The first usable API should be intentionally small:

```python
class Repo:
    @classmethod
    def open(cls, path: str, backend: Backend) -> Self: ...
    def transaction(self) -> TransactionContext: ...
    def file(self, key: str) -> FileNode: ...


class FileNode:
    def open(self, mode: OpenMode = "r", *, encoding: str | None = None): ...
    def read_bytes(self) -> bytes: ...
    def write_bytes(self, data: bytes) -> None: ...
    def read_text(self, encoding: str = "utf-8") -> str: ...
    def write_text(self, text: str, encoding: str = "utf-8") -> None: ...
    def delete(self) -> None: ...
```

This is enough to validate:

- implicit single-file transactions
- explicit multi-file transactions
- buffered handle semantics
- backend neutrality at the transaction layer

Schema-driven hierarchy binding can be layered on later.

Notes:

- backend protocol objects still exist internally and should closely follow the Phase 2 backend contract
- `Repo.open(...)` and `Repo.file(...)` are thin MVP conveniences for repo binding and logical-key lookup, not new semantic concepts beyond the Phase 2 contract
- `FileNode.delete()` belongs in the MVP because `Delete` is already a first-class Phase 2 operation and should be validated through the public surface as well as the backend contract

## 6. Phase-Wise Delivery Plan

### Phase 0. Design Freeze and Test Matrix

Deliverables:

- define exact invariants from Phase 2 as executable acceptance criteria
- define the allowed open modes for the first release
- define behavior for reads while a writable handle is still open
- define delete semantics as a first-class contract case
- define the initial error taxonomy

Decisions to freeze:

- overlay becomes authoritative only on successful writable handle close
- a writable handle reads and writes its own local buffer while it is open
- a writable handle's own reads come from its local buffer, not from `SΔ`, until successful close materializes the final value into the overlay
- cursor movement and buffer mutation follow Python-style file semantics within the handle
- while a writable handle is open and not yet closed, other reads for that key within the same transaction should read the last committed overlay-visible value, not the unclosed buffer
- one writable handle per key per transaction
- `prepare()` requires all writable handles to be closed
- the first backend should support rollback from `prepared` before commit publication begins
- recovery from `prepared` in the filesystem backend completes commit
- first-release public `open(...)` supports `r`, `rb`, `w`, `wb`, `a`, `ab`, `r+`, `rb+`
- text mode uses `encoding=None` at the signature layer and resolves defaults in helpers/runtime
- `delete(k)` is represented as `Δ[k] := Delete` and must participate in commit, rollback, and recovery exactly like `Put(...)`
- successful close makes the handle immutable to user code
- handle failure before close leaves `Δ` unchanged and the failed handle unusable

Why this phase matters:

Without these decisions, the implementation will drift into backend-specific behavior.

### Phase 1. Core Semantic Engine

Implement:

- logical key and value types
- transaction state enum: `open`, `prepared`, `committed`, `aborted`
- overlay entry model: `Put(value)` and `Delete`
- `read_visible`, `put`, `delete`, overlay application
- lifecycle guardrails around `prepare`, `commit`, `rollback`
- invariant checks

Tests:

- pure unit tests for overlay lookup `SΔ`
- pure unit tests for `S0 ⊕ Δ`
- delete overlay tests for read-visible absence and overlay application
- delete-then-put and put-then-delete tests to confirm the overlay's latest entry is authoritative
- invalid state transition tests
- rollback from `prepared` returns to `S0` and clears pending overlay state
- rollback leaves committed state unchanged
- commit applies exactly one frozen overlay
- successful commit changes only keys named in `Δ`

Exit criteria:

- transaction state machine is correct without any filesystem code
- core tests are fast and deterministic

### Phase 2. Buffered Handle Layer

Implement:

- buffered reader/writer handle abstraction over a logical key
- text and binary modes for the limited first-mode set
- close semantics that materialize a final full value into `Δ`
- enforcement of one writable handle per key
- helper methods that route through the same transaction machinery

Tests:

- `w` starts from empty buffer
- `a` starts from `SΔ(k)` or empty
- `r` requires existence
- `rb`, `wb`, `ab`, and `rb+` behave consistently with the text-mode equivalents
- `r+` initializes from `SΔ(k)`
- same-handle reads after writes observe the handle-local buffer
- cursor movement follows Python-style expectations for the supported mode set
- opening a second writable handle for the same key is rejected
- a read handle opened after a pending overlay entry reads from `SΔ`, not `S0`
- reads after successful close observe the overlay
- closing a writable handle makes further user mutation invalid
- handle failure before close does not update overlay
- delete through the transaction/backend path is reflected in `SΔ`
- delete-then-write and write-then-delete for the same key within one transaction behave according to final overlay authority

Exit criteria:

- file-style semantics are implemented without binding to filesystem staging details

### Phase 3. Minimal Public API and Transaction Contexts

Implement:

- `Repo`
- `FileNode`
- explicit transaction context manager
- implicit single-file transaction helper path
- transaction-local routing so helpers and `open(...)` share one engine
- thin bootstrapping helpers outside the semantic contract, if needed to construct a repo and bind logical keys

Tests:

- `Repo.open(...)` binds the backend and repo locator without starting a write transaction
- single helper write commits implicitly
- multi-file write commits atomically through explicit transaction
- exception inside transaction causes rollback
- nested helper calls use the active transaction rather than bypassing it
- `FileNode.delete()` uses the same transaction machinery as `open(...)` and helper writes
- add a small pyright smoke test for the public API as soon as this phase lands

Exit criteria:

- a user can manipulate logical files using the public API without touching backend internals

### Phase 4. Direct-File Backend Layout and Locking

Implement:

- repo locator based on a filesystem directory
- `.repo_tx/` layout
- staged file location scheme
- JSON WAL metadata shape
- exclusive writer ownership for one active write transaction per repo

Implementation preference:

- for the MVP, document the filesystem backend as POSIX-first unless cross-platform locking is made an explicit requirement
- if cross-platform locking becomes an MVP requirement, reevaluate the locking strategy before implementation rather than improvising mid-phase

Tests:

- transaction directory materializes lazily on first write
- second writer is rejected while one write transaction is active
- read-only access does not create transaction state
- WAL contains enough data to recover `prepared`

Exit criteria:

- the backend can persist overlay and lifecycle information safely enough for crash recovery testing

### Phase 5. Direct-File Prepare / Commit / Recovery

Implement:

- prepare freeze logic
- publication of staged values to committed files
- delete publication
- cleanup of finished transaction state
- recovery logic for `open` and `prepared`

Tests:

- crash simulation before prepare results in discard or rollback-compatible outcome
- crash simulation after prepare but before commit publication is still recoverable from the frozen overlay
- crash simulation after prepare results in full commit
- deleted keys remain deleted after successful recovery
- no mixed partial publication after recovery
- repeated recovery is idempotent

Exit criteria:

- the direct-file backend satisfies the core recovery contract from Phase 2

### Phase 6. Typing and Developer-Experience Hardening

Implement:

- full public type annotations
- `.pyi` stubs only if runtime typing alone is not enough
- editor-friendly overloads for text vs binary modes where useful
- stable exception messages for common misuse cases

Tests:

- `pyright` check in CI
- dedicated typing samples using `assert_type(...)`
- API examples in tests to guard VS Code-friendly inference

Exit criteria:

- static type checking is part of the acceptance bar, not optional cleanup

### Phase 7. Post-MVP Extension: Nested Repo Coordination

Implement:

- explicit nested repo transaction domain concept
- lazy child enrollment under an active outer transaction
- parent participant metadata
- ordered prepare and recovery coordination across enrolled repos

Tests:

- child repo is not enrolled unless written
- parent prepare freezes the logical transaction tree
- crash after global prepare recovers the whole enrolled set consistently
- unenrolled child repos remain independent

Exit criteria:

- nested repo semantics match the Phase 1/Phase 2 design without forcing global scanning

This phase is intentionally post-MVP.
It is not part of the first-delivery acceptance bar for the backend-neutral single-domain transaction layer.

### Phase 8. Post-MVP Extension: SQLite Backend Spike

Implement:

- a minimal SQLite backend prototype using the same backend protocol
- whole-object replacement semantics in a table-based storage model
- prepare/commit/recovery behavior mapped to SQLite transaction mechanics

Purpose:

- validate that the core design is truly backend-neutral
- discover abstractions that are still filesystem-shaped

Tests:

- the same backend-agnostic contract tests used for the filesystem backend

Exit criteria:

- at least one second backend can pass the core contract tests with limited extra code

This phase is also post-MVP.
It validates backend neutrality after the first direct-file backend is already correct.

## 7. Testing Strategy

Use three layers of tests.

### 7.1 Semantic unit tests

Fast pure-Python tests for overlay, states, handle semantics, and lifecycle transitions.

### 7.2 Backend contract tests

Backend-agnostic tests parameterized over a backend factory. These should verify:

- read-your-writes
- delete visibility
- delete/write ordering within one transaction
- no partial publication
- correct rollback
- rollback from `prepared` for backends that support it
- commit equivalence
- recovery outcomes

### 7.3 Filesystem crash/recovery tests

Use subprocess-driven tests where needed to simulate interrupted execution around:

- before first write materialization
- after WAL creation
- after prepare
- during commit publication
- after delete is staged but before publication

Crash tests should be deterministic and targeted, not large randomized end-to-end suites.

## 8. Suggested Internal Types

Use `attrs` for internal domain objects where it materially improves clarity over plain classes.

Recommended examples:

- `OverlayEntry`
- `Put`
- `Delete`
- `TransactionSnapshot`
- `PreparedOverlay`
- `WalRecord`
- `FsTransactionLayout`
- `RecoveryPlan`

Benefits:

- compact code
- better repr/debugging
- better field-level reasoning
- consistent typing across layers

## 9. Error Taxonomy

Define explicit storage exceptions early:

- `StorageError`
- `TransactionStateError`
- `HandleStateError`
- `KeyNotFoundError`
- `WriteConflictError`
- `RecoveryError`
- `BackendCapabilityError`

This avoids leaking raw `OSError` or backend-native exceptions into the API as the primary contract.

## 10. Recommended Milestone Order

Recommended commit-sized milestones:

1. scaffold package and core types
2. overlay engine + unit tests
3. transaction state machine + unit tests
4. buffered handles + unit tests
5. minimal public API
6. filesystem layout + WAL serialization
7. filesystem commit/recovery
8. typing hardening
9. nested repo coordination as a post-MVP extension
10. SQLite spike as a post-MVP extension

This order keeps the most reusable abstractions stable before recovery complexity arrives.

## 11. Risks and Mitigations

### Risk: filesystem details leak into the core model

Mitigation:

- keep backend protocols small
- run backend-agnostic tests before filesystem-specific tests

### Risk: handle semantics become ambiguous

Mitigation:

- freeze the close/visibility rules in Phase 0
- enforce them with pure unit tests

### Risk: delete behavior is less exercised than put behavior

Mitigation:

- add delete-specific unit, contract, and recovery tests from the beginning

### Risk: nested repos explode complexity too early

Mitigation:

- keep nested repos out of the first-delivery acceptance bar
- defer nested repo coordination until the single-domain backend is already correct

### Risk: type hints look good in code but fail in editors

Mitigation:

- add pyright-based typing tests before widening the public surface

### Risk: lock behavior becomes platform-specific and fragile

Mitigation:

- start with the smallest correct approach
- allow adoption of `portalocker` if cross-platform correctness is not maintainable with stdlib-only code

## 12. Definition of Done

The storage layer is ready for broader adoption when all of the following are true:

- the direct-file backend passes backend-neutral contract tests
- recovery from `prepared` is deterministic and idempotent
- implicit and explicit transaction APIs both work
- `pyright` passes on representative public usage examples
- module boundaries remain small and readable
- no core Python file exceeds 800 lines

Post-MVP milestones:

- nested repo coordination
- SQLite backend validation

## 13. Recommended Immediate Next Step

Start with Phase 0 and Phase 1 only.

That means:

- freeze the semantic edge cases in writing
- scaffold `src/alpenstock/storage`
- implement the pure overlay/state engine
- write its tests first

Do not start with WAL I/O or nested repo recovery before the semantic core is executable and tested.
