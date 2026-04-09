# Backend-Owned Handle and Value-Reference Refactor Plan

## Summary

Refactor the storage internals from a byte-buffered design to a backend-owned staged-value design, while keeping the public API stable.

The public surface stays:

- `Repo`
- `FileNode`
- `TransactionContext`
- `open(...)`
- `read_bytes/read_text/write_bytes/write_text/delete`

The internal model changes completely:

- remove the assumption that a writable handle is an in-memory buffer
- replace byte-valued overlay state with backend-owned immutable value references
- make the handle a unified proxy contract whose concrete read/write behavior is implemented by each backend
- make the filesystem backend use staged files from handle-open onward
- plan a later SQLite prototype phase with full mode parity using DB-native staging rows

## Phase-Wise Plan

### Phase 0. Semantic Freeze

Lock these semantics before changing code:

- Public API remains stable.
- Whole-value helpers remain as convenience helpers; they may still materialize full content and are not the large-blob path.
- `open("r")` / `open("rb")` outside a transaction reads committed state directly and does not start a transaction.
- Inside a transaction, if a key has an open writable handle, new reads of that key see the live working copy.
- Only one writable handle per key may be open at a time.
- `close()` seals the current writer session as the key's current transaction candidate, but the key may be reopened later in the same transaction.
- Reopening a key for writing in the same transaction starts from the current transaction-visible value.
- `delete(key)` while that key has an open writable handle raises; it does not implicitly discard or close the writer.
- `prepare()` still requires all writable handles to be closed.
- `commit()` / recovery publish the last sealed candidate value for each key.

### Phase 1. Redesign Shared Internal Contracts

Replace the byte-centric internals with artifact-centric contracts.

Implement these contract changes:

- Make `TransactionCore` generic over an opaque backend-owned `ValueRef` type instead of raw `bytes`.
- Keep the overlay math unchanged: `Put(ValueRef) | Delete`.
- Replace `read_visible(...) -> bytes | None` with `read_visible_ref(...) -> ValueRef | None`.
- Replace `put(key, value: bytes)` with `put_ref(key, value_ref)`.
- Keep lifecycle/state logic in `TransactionCore`; remove any data-materialization assumptions from it.
- Remove `BufferedFileHandle` and the `BytesIO/StringIO` editing model.
- Replace it with a thin handle proxy over backend-owned file/session objects.
- Move writable-handle session ownership to the backend transaction, not the shared handle layer.
- Keep shared mode parsing/validation in one place so all backends obey the same mode contract.

Internal types to introduce:

- `ValueRef`: immutable backend-owned candidate/committed value reference for one key
- `BackendFileHandle`: file-like object implementing read/write/seek/tell/close
- `BackendTransaction` methods for:
  - opening read handles
  - opening write handles
  - sealing the current writer session into a `ValueRef`
  - deleting keys
  - prepare/commit/rollback

Shared handle flow after refactor:

- `TransactionContext.open(...)` delegates to backend transaction handle creation.
- A backend writable handle owns the mutable working artifact for that key.
- On close, backend seals that working artifact into a `ValueRef` and updates the overlay through `TransactionCore.put_ref(...)`.
- In-transaction reads while a writer is open are backend-routed to the live working artifact.
- Reads when no writer is open are opened from `TransactionCore.read_visible_ref(...)`.

### Phase 2. Filesystem Backend Conversion

Rebuild the filesystem backend around staged files, not in-memory buffers.

Filesystem value model:

- `FsValueRef` is an immutable reference to either:
  - a committed file path
  - a sealed staged file path under `.repo_tx/staged/...`

Filesystem writable-handle model:

- A writable handle operates directly on a staged file on disk from `open(...)` onward.
- `w` / `wb`: create or truncate staged file immediately.
- `a` / `ab`: initialize staged file from current transaction-visible value, then seek to end.
- `r+` / `rb+`: initialize staged file from current transaction-visible value and open read/write.
- `r` / `rb` inside transaction:
  - if a writer is open for that key, open a reader against the live staged file
  - otherwise open from the visible `FsValueRef`
- `r` / `rb` outside transaction: open committed file directly

Filesystem staged-artifact rules:

- Use `.repo_tx/staged/<logical-key>` as the single staged artifact path for that key.
- Do not create a second “prepared copy” at `prepare()`.
- While a writer is open, `.repo_tx/staged/<key>` is mutable.
- On writer close, that staged file becomes the sealed candidate for the key.
- On reopen later in the same transaction, reopen from the current transaction-visible candidate:
  - reuse or recreate the staged file as needed, but behavior must be equivalent to starting from the current candidate value
- On `delete(key)`, remove any sealed staged artifact for that key and write `Delete` into the overlay.

Filesystem prepare/commit/recovery rules:

- `prepare()` does not copy data; it only freezes overlay state and persists WAL that references sealed staged file paths.
- `commit()` / recovery publish by ordered filesystem operations:
  - validate all logical keys and staged paths before mutating committed state
  - delete deeper paths first
  - then publish shallower puts
  - if a put target is currently a directory, remove that directory tree first
- Keep directory `fsync()` discipline for:
  - WAL writes
  - staged file writes
  - committed publication
  - deletes
  - transaction-root cleanup

### Phase 3. Public API Rewire

Keep the public API behavior stable while rewiring internals.

Implementation intent:

- `FileNode.open(...)` still returns a file-like handle.
- `FileNode.read_*` and `write_*` continue to be wrappers over `open(...)`.
- Implicit single-file transactions continue to exist.
- The implicit-handle path must use the same backend-owned staging model as explicit transactions.
- The public API must not expose `ValueRef`, staged paths, or backend session objects.

Acceptance for this phase:

- Existing user-facing usage patterns continue to work unchanged.
- Public API tests should pass after the refactor with only internal/test-fixture changes.
- No shared storage module should use `BytesIO` or `StringIO` as the writable transaction implementation.

### Phase 4. Shared Contract Tests and Migration

Rewrite the shared tests around the new internal model.

Update tests to cover:

- `TransactionCore` over opaque references rather than raw bytes
- one open writer per key
- in-transaction reads seeing the live working copy while writer is open
- writer close sealing a candidate value
- reopening the same key in the same transaction starting from the current candidate
- `delete(key)` rejection when writer for that key is open
- `prepare()` rejection with open writers
- no auto-rollback after failed finalization unless backend contract says so

Migration rule for this refactor:

- big-bang internal refactor
- no transitional byte-buffer path remains after the redesign lands
- the public API stays stable across the refactor

### Phase 5. Filesystem Acceptance and Recovery Tests

Add filesystem-specific tests that prove the new model, not the old one.

Required cases:

- writable open creates/uses staged file on disk, not in-memory buffer
- `a` and `r+` seed from current transaction-visible value
- in-transaction reads while writer is open see live staged-file changes
- closing and reopening the same key composes correctly
- delete after sealed candidate works
- delete while writer is open raises
- `prepare()` persists WAL pointing to staged files already created during handle lifetime
- crash/recovery from `open` discards unfinished work
- crash/recovery from `prepared` completes commit
- repeated recovery is idempotent
- prefix-conflict publication works for both:
  - subtree -> file
  - file -> subtree

### Phase 6. Later SQLite Prototype Phase

Implement a later prototype phase using the new contracts, with full mode parity.

SQLite prototype contract:

- backend is a single SQLite database
- public API and mode semantics match filesystem:
  - `r`, `rb`, `w`, `wb`, `a`, `ab`, `r+`, `rb+`
  - `seek/tell`
  - one open writer per key
  - live in-transaction reads from current working state
  - reopen-from-current-candidate semantics

SQLite storage design to lock in:

- committed table:
  - `objects(key TEXT PRIMARY KEY, value BLOB NOT NULL)`
- stage metadata table for current candidate state per key
- stage chunk table for growable/random-access writer state
- writer sessions operate against staged rows/chunks, not temp files
- in-transaction reads resolve from:
  - open live staged writer for that key, if present
  - otherwise sealed staged rows
  - otherwise committed `objects`
- writer close seals the staged rows as the current candidate
- `prepare()` freezes the overlay and refuses open writers
- `commit()` applies staged rows to `objects` inside the SQLite transaction
- `rollback()` discards staged rows by rolling back the DB transaction
- no external WAL/recovery layer beyond SQLite's transaction durability

This phase is later, but it is not optional in the architecture:

- the shared contracts in phases 1–5 must be shaped so this backend can fit without another redesign

## Test Plan

Shared contract tests:

- generic `TransactionCore[ValueRef]` state-machine tests
- handle lifecycle tests with fake backend-owned refs and fake backend handles
- reopen and live-read visibility tests
- error-path tests for close, rollback, prepare, and finalization failures

Filesystem tests:

- staged-file-backed write path
- no in-memory writable buffer in shared handle implementation
- WAL references staged paths created before prepare
- recovery from `open` and `prepared`
- prefix-conflict publication
- directory `fsync()` path coverage via helper-level unit tests where practical

SQLite prototype tests for its later phase:

- full mode parity against the same behavioral contract suite used for filesystem
- large-value streaming behavior without full-value materialization in shared code
- rollback/commit behavior entirely through SQLite transaction semantics

## Assumptions and Defaults

- Public API stability is required.
- The redesign is internal and phase-wise, and implementation should also proceed phase-wise in the same order above.
- Whole-value helper methods remain, but they are convenience helpers rather than the large-blob path.
- The large-blob path is `open(...)` with backend-owned staged artifacts.
- Shared code remains backend-neutral and must not assume raw `bytes` values.
- Filesystem backend uses copy-write-rename semantics with staged files created at handle-open time.
- SQLite prototype uses DB-native staging rows with full mode parity in a later dedicated phase.
