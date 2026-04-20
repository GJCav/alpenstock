# Storage Design Rev 1

Date: 2026-04-09

This document is the authoritative storage design for the current implementation line.

It consolidates and supersedes the relevant parts of:

- [01 - Initial Design.md](./01%20-%20Initial%20Design.md)
- [02 - Backend-Neutral Transaction Implementation Plan.md](./02%20-%20Backend-Neutral%20Transaction%20Implementation%20Plan.md)
- [03 - Backend-Owned Handle Refactor Plan.md](./03%20-%20Backend-Owned%20Handle%20Refactor%20Plan.md)

When earlier and later documents disagree, later decisions take precedence.

## 1. Scope

Rev 1 covers:

- backend-neutral transaction semantics
- the public `Repo` / `FileNode` / `TransactionContext` API
- a direct-file filesystem backend
- a SQLite backend
- static typing and IDE-facing API hints for common usage
- a codec-oriented extension hook for future typed file kinds

Rev 1 explicitly defers:

- nested repo coordination
- distributed or cross-repo transactions
- async storage APIs
- power-loss or hardware-failure durability guarantees
- schema-bound `Dir` hierarchy binding from the storage UI proposal

## 2. Safety Target

Rev 1 is designed to be safe for accidental process exit:

- process kill
- interpreter crash
- abrupt application exit

Rev 1 is not designed to guarantee recovery under full system failure conditions such as:

- power loss
- storage controller failure
- CPU removal
- kernel/filesystem dishonesty after acknowledged writes

This means the implementation should defend consistency for ordinary process interruption, but it does not need to pursue maximal durability at the cost of large complexity.

## 3. Mathematical Contract

The committed repo state is modeled as a partial map:

```text
S : K -> V ∪ {⊥}
```

Where:

- `K` = logical keys
- `V` = full logical object values
- `⊥` = absent

For Rev 1, user-visible logical values are bytes.

The internal implementation may represent pending or committed values through backend-owned immutable references, but the observable transaction contract is still whole-object replacement over logical values.

A transaction is modeled as:

```text
Tx = (S0, Δ, state)
```

Where:

- `S0` = committed state visible at begin
- `Δ` = finite overlay
- `state` ∈ {`open`, `prepared`, `committed`, `aborted`}

The overlay is:

```text
Δ : K -> Put(V) | Delete
```

Transaction-visible lookup is:

```text
SΔ(k) =
  Δ(k).value          if Δ(k) = Put(value)
  ⊥                   if Δ(k) = Delete
  S0(k)               if k not in dom(Δ)
```

Commit publishes exactly:

```text
S1 = S0 ⊕ Δ
```

Where:

- `Put(v)` replaces the committed value for `k`
- `Delete` removes `k`
- untouched keys remain unchanged

## 4. Logical Key Contract

For Rev 1, logical keys are backend-neutral but use one common format across all backends:

- keys are `str`
- keys are non-empty
- keys use `/` as separator
- keys are relative
- keys do not contain empty segments
- keys do not contain `.` or `..`
- keys do not contain backslashes

Examples:

- `config.json`
- `users/alice/profile.toml`

This keeps key semantics portable across filesystem and SQLite backends.

## 5. Lifecycle Contract

The public lifecycle states are:

- `open`
- `prepared`
- `committed`
- `aborted`

### 5.1 `open`

- the transaction exists
- `Δ` may change
- writable handles may still be open

### 5.2 `prepared`

- all writable handles are closed
- `Δ` is frozen
- no further user mutation is allowed
- enough backend metadata must exist for recovery according to that backend's Rev 1 safety rules

### 5.3 `committed`

- `S0 ⊕ Δ` has been fully published

### 5.4 `aborted`

- `Δ` has been discarded

## 6. Handle Contract

The user-facing `open(...)` API is preserved, but writable handles are not specified as in-memory buffers anymore.

A writable handle is:

- a transactional editing session for one logical key
- isolated from committed state
- backed by a backend-owned mutable working artifact

The backend may realize that artifact as:

- a staged file
- staged database rows
- another backend-specific structure

On successful close, the handle seals one final candidate value for the key:

```text
Δ[k] := Put(v_final)
```

### 6.1 Read visibility while a writer is still open

Later design revisions take precedence here.

Rev 1 rule:

- if a writable handle for `k` is still open
- and another read for `k` is opened in the same transaction
- that read sees the live working copy, not the last sealed candidate and not committed state

This is the authoritative rule for Rev 1.

### 6.2 Writer close

On successful writable close:

- the working artifact is sealed as the transaction's current candidate for that key
- later reads and later writable opens for the same key use that current candidate value
- the closed handle becomes unusable

### 6.3 Writer failure before close

If a writable handle fails before successful close:

- the transaction is not required to reflect partial writes from that handle
- the failed handle becomes unusable

### 6.4 Single active writer rule

At most one writable handle per key may be open in one transaction.

### 6.5 Delete while writer is open

`delete(k)` while a writable handle for `k` is still open raises.

## 7. Public API

Rev 1 public API remains intentionally small:

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
    def as_codec(self, codec: FileCodec[T]) -> CodecFile[T]: ...
```

Guidance:

- `read_text` / `write_text` and `read_bytes` / `write_bytes` are convenience helpers
- large-object workflows should prefer `open(...)`
- read-only `open("r")` and `open("rb")` outside a transaction read committed state directly and do not start a write transaction

## 8. Internal Core Contract

The shared transaction core is backend-neutral and stores:

- committed snapshot `S0`
- overlay `Δ`
- lifecycle state
- open-writer tracking

The core does not own:

- filesystem paths
- database connections
- temporary files
- WAL files

The core may store backend-owned immutable value references rather than raw bytes, but it must still preserve the overlay math exactly.

## 9. Filesystem Backend

The filesystem backend is the reference backend.

### 9.1 Working model

- committed values live at repo-relative paths
- transaction metadata lives under `.repo_tx/`
- writable handles write directly to staged files from `open(...)` onward
- close seals the staged file as the candidate value

### 9.2 Prepare / commit / recover

- `prepare()` freezes the overlay and writes recoverable WAL metadata
- `commit()` validates staged inputs before mutating committed state
- publication uses ordered delete/replace operations
- `recover()` resolves:
  - `open` -> discard
  - `prepared` -> complete commit

### 9.3 Failure target

The filesystem backend should be consistent under accidental process exit.
It should not attempt to provide a full power-loss-grade durability story.

## 10. SQLite Backend

The SQLite backend must satisfy the same external contract as the filesystem backend.

### 10.1 Storage model

The backend uses a single SQLite database with:

- committed object table
- transaction metadata table
- staged candidate table

### 10.2 Working model

- committed objects live in the committed table
- a writable handle owns a backend-managed working artifact during editing
- on close, the candidate value is sealed into staged transaction state in the database
- `prepare()` freezes the overlay and marks transaction intent in database metadata
- `commit()` atomically applies staged rows to committed rows
- `recover()` resolves:
  - `open` -> discard
  - `prepared` -> complete commit

### 10.3 Design note

Rev 1 does not require the SQLite backend to optimize every read/write path for very large blobs.
What it must avoid is a shared in-memory transaction design that forces every writable handle to materialize the full candidate in the storage core.

## 11. Typing Contract

Rev 1 typing goals are:

- all public APIs are annotated
- pyright passes on the storage package and its tests
- common editor hints work for helper APIs
- `open(...)` exposes mode-sensitive typing for text vs binary use where practical

The acceptance bar is not “perfect static modeling of every file mode edge case”.
The acceptance bar is “common user code receives reliable and unsurprising hints”.

## 12. Test Contract

Rev 1 tests should cover:

- pure overlay and lifecycle semantics
- handle semantics
- public API behavior
- backend-specific behavior
- accidental-exit recovery around all transaction stages that matter for each backend

For Rev 1, deterministic subprocess crash tests are preferred for the accidental-exit model.

## 13. Extensibility Contract

Rev 1 should make it straightforward to add future typed file surfaces such as:

- `JsonFile`
- `TomlFile`
- `YamlFile`

without changing transaction semantics.

The preferred extension mechanism is:

- keep transaction/lifecycle logic in `Repo` / `FileNode` / backends
- layer typed value handling through codecs or thin wrappers over `FileNode`

Nested directory-schema binding remains out of scope for Rev 1.

## 14. Rev 1 Acceptance Summary

Rev 1 is considered complete when:

- the storage docs are consistent with the later backend-owned-handle decisions
- the public API remains stable
- the shared core matches the transaction math
- the filesystem backend is correct for the accidental-exit model
- the SQLite backend satisfies the same external contract
- typing and tests meet the Rev 1 bar
- nested repos remain explicitly deferred rather than half-implemented
