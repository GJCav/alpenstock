# Transactional Direct-File Repository Library Design Plan

## 1. Purpose and Scope

This document specifies a filesystem-native transactional storage library for ordinary files and folders, with a Python-first API design.

The library is intended for applications that need:

- atomic multi-file updates
- crash-consistent state after process interruption or forced termination
- directly accessible, human-readable filesystem contents
- a simpler concurrency model than a database
- transaction semantics for ordinary files, not opaque DB blobs

### Non-goals

This design intentionally does **not** target:

- full database isolation levels
- concurrent multi-writer access
- system-crash or sudden power-loss durability guarantees
- distributed coordination
- arbitrary path-based dynamic schemas by default
- transparent cross-repo writes that ignore transactional boundaries

The target is a **single-process / multi-process safe, single active writer per repo** system with **read-committed semantics** and strong recovery from **program crash / kill / Ctrl-C**.

---

## 2. Core Design Principles

### 2.1 Files remain ordinary files
Committed data remains stored as normal files and folders in the live repository tree. Users can inspect and debug them directly with ordinary filesystem tools.

### 2.2 One active write transaction per repo
At most one active write transaction may exist for a repo at a time.

### 2.3 Read-committed visibility
Read-only access always reads committed live files. Write-capable access works on staged copies and does not affect committed state until commit.

### 2.4 WAL-driven recovery
A write-ahead log (WAL) inside the repo records transaction progress and recovery state. The WAL is the authority for incomplete transactions.

### 2.5 Explicit transaction boundary for multi-file atomicity
Single-file writes may use implicit transactions for convenience. Multi-file atomicity is expressed with an explicit transaction block.

### 2.6 Repo is a transaction domain
A `Repo` is not only a filesystem folder. It is also a transaction boundary with its own WAL, recovery rules, and writer ownership.

### 2.7 Nested repos are explicit
Nested repos are supported, but they are explicit schema elements, not ordinary folders. They remain separate transaction domains that can optionally participate in an ancestor transaction through coordinated enrollment.

---

## 3. Filesystem Layout

For each repo root:

```text
repo/
  .repo_tx/
    wal.json
    staged/
```

### Meaning

- `.repo_tx/` absent:
  - no active write transaction
  - no pending recovery work

- `.repo_tx/` present:
  - a write transaction is active, or
  - a crashed/interrupted write transaction must be recovered

### Important simplification

Because at most one active write transaction exists per repo, `.repo_tx/` itself acts as the writer ownership marker. A separate `writer.lock` file is not required.

Atomic creation of `.repo_tx/` is the write-ownership claim step.

---

## 4. Primitives and Schema Model

### 4.1 Core primitives

The schema is declaration-first and mostly static.

Primary primitives:

- `File`
- `Dir`
- `MappedDir`
- `NestedRepo`
- `Repo`

### 4.2 `File`
Represents a concrete file path in the current repo transaction domain.

### 4.3 `Dir`
Represents a statically declared ordinary folder in the current repo domain.

### 4.4 `MappedDir`
Represents a dynamic keyed subtree, but only where intentionally declared. This is the controlled escape hatch for runtime-keyed structures.

Example:

```python
repo.users["alice"].profile
```

### 4.5 `NestedRepo`
Represents a folder that is also the root of another repo type.

This is not treated as an ordinary directory for transaction ownership purposes.

### 4.6 `Repo`
Represents the root of a transaction domain.

A repo owns:

- its own `.repo_tx/`
- its own WAL state machine
- its own staged files
- its own commit/recovery logic
- optionally, child participant coordination when nested repos are enrolled into an ancestor transaction

---

## 5. Example Schema

```python
class UserRepo(Repo):
    profile = File("profile.json")
    notes = File("notes.txt")


class WorkspaceRepo(Repo):
    config = File("config.json")
    users = MappedNestedRepo("users", repo_type=UserRepo)
    docs = Dir("docs")
```

Example usage:

```python
ws = WorkspaceRepo("/path/to/workspace")

with ws.config.open("r") as f:
    text = f.read()

alice = ws.users["alice"].repo()

with alice.profile.open("w") as f:
    f.write('{"name": "Alice"}')
```

---

## 6. Access and I/O Semantics

### 6.1 Read-only open
Read-only opens do **not** create transaction state and do **not** enter WAL states.

Example:

```python
with repo.config.open("r") as f:
    text = f.read()
```

Semantics:

- reads committed live file
- does not create `.repo_tx/`
- does not create staged file
- does not participate in transaction recovery

### 6.2 Write-capable open
Write-capable opens operate on staged copies.

General rule:

- `r`, `rb`: open committed live file
- `w`, `wb`: open a fresh staged file
- `a`, `ab`: open staged copy initialized from committed file and positioned at end
- `r+`, `rb+`: open staged copy initialized from committed file

### 6.3 Staged-copy rule
For write-capable modes, the user never writes directly to the committed live file. The library stages a copy and operates on that copy.

### 6.4 Pythonic context usage
File access is intended to be used via `with`.

Example:

```python
with repo.config.open("w") as f:
    f.write("new config")
```

---

## 7. Transaction API

### 7.1 Implicit single-file transaction
If `.open()` is called in write-capable mode outside any active transaction context, the library creates an implicit transaction for that file.

On context exit:

- the staged file is closed
- the transaction is prepared
- commit proceeds
- transaction finishes

Example:

```python
with repo.config.open("w") as f:
    f.write("new config")
```

### 7.2 Explicit multi-file transaction
Multi-file atomicity is expressed with an explicit transaction block.

Example:

```python
with repo.transaction() as tx:
    with repo.config.open("w") as f:
        f.write("new config")

    with repo.notes.open("w") as f:
        f.write("new notes")
```

Semantics:

- first write causes the repo transaction to materialize on disk
- file opens attach to the active transaction
- file context exit closes the staged file only
- commit occurs only when the outer transaction exits successfully

### 7.3 Failure in explicit transaction
If an exception escapes the transaction block before prepare, the transaction is discarded.

---

## 8. WAL Model

## 8.1 Role of the WAL
The WAL is not a manifest of committed repository state. It is a transaction journal and recovery authority for incomplete write activity.

Its existence indicates:

- a write transaction exists, or
- a crashed/interrupted write transaction requires recovery

### 8.2 WAL storage
Recommended format for v1: normal file, specifically JSON.

Rationale:

- simpler than SQLite
- directly inspectable
- fits small metadata size
- aligns with filesystem-native debugging goals
- avoids reintroducing a mini-database for tiny transaction metadata

SQLite may be considered later if metadata requirements grow substantially, but it is not recommended for v1.

### 8.3 WAL update method
`wal.json` must itself be rewritten via temp-and-replace.

Example:

- write `wal.json.tmp`
- close it
- atomically replace `wal.json`

---

## 9. WAL States

Recommended transaction states:

- `open`
- `prepared`
- `committing`

### 9.1 `open`
A write transaction exists, but staged files may still be open for user modification.

Crash policy:
- rollback by discarding the transaction

### 9.2 `prepared`
All staged files are closed, the file set is frozen, and the transaction is ready to commit.

Crash policy:
- recovery must continue with commit

### 9.3 `committing`
Promotion of staged files into live paths has started.

Crash policy:
- recovery must resume and complete commit

### 9.4 Read-only access and WAL state
Read-only access is **not** treated as `open`. WAL states apply only to write transactions.

---

## 10. WAL Schema

A minimal local WAL schema:

```json
{
  "state": "open",
  "files": [
    {
      "path": "config.json",
      "staged": "staged/config.json",
      "original_exists": true,
      "handle_open": false,
      "applied": false
    }
  ],
  "children": []
}
```

### 10.1 Transaction-level fields
Recommended:

- `state`
- `files`
- `children`

Optional but useful:

- `txid`
- `created_at`
- `pid`

### 10.2 Per-file fields

- `path`: repo-relative live path
- `staged`: staged path relative to `.repo_tx/`
- `original_exists`: whether the committed live file existed before the transaction
- `handle_open`: whether the staged file is currently open for user writes
- `applied`: whether this file has already been promoted into the live repo during commit

### 10.3 `children`
Used only when nested repos are enrolled into the current logical transaction.

---

## 11. Write Lifecycle

Given a write transaction, the stages are:

1. create or join transaction
2. create staged copy
3. user writes staged file
4. user closes staged file
5. transaction prepares
6. transaction commits

### 11.1 First write
On the first write-capable open within a repo:

- atomically create `.repo_tx/`
- create `.repo_tx/staged/`
- create initial `wal.json`
- state becomes `open`

### 11.2 Opening a file for write
When a file is first opened in write mode:

- create its staged path under `.repo_tx/staged/...`
- initialize staged content according to Python mode semantics
- add file record to `wal.json`
- mark `handle_open = true`

### 11.3 While user writes
The transaction remains in `open`.

Crash rule:
- discard the transaction

### 11.4 File close
On file context exit:

- flush and close staged file
- mark `handle_open = false`
- remain in `open` if the outer transaction is still active

### 11.5 Prepare
When the owning transaction block exits successfully:

- verify no file record still has `handle_open = true`
- freeze file set
- rewrite WAL with `state = "prepared"`

### 11.6 Commit
Before promoting the first file:

- set `state = "committing"`

Then promote files in deterministic order.

---

## 12. Commit Algorithm

The chosen commit primitive is:

- copy staged file to a sibling temp path beside the live file
- close the temp file
- atomically rename sibling temp to the live path

This gives per-file atomic replacement.

### 12.1 Why not copy directly over the live file
Direct overwrite risks exposing half-written live files. The sibling-temp-and-rename method guarantees that each individual file replacement is atomic.

### 12.2 Per-file commit steps

For each file with `applied = false`, in deterministic path order:

1. ensure target parent directories exist
2. copy staged file to a sibling temp file in the live directory
3. close and flush that temp file
4. atomically replace the live file with the sibling temp
5. update WAL to mark `applied = true`

### 12.3 Sibling temp naming
Recommended pattern:

```text
<filename>.__repo_tx_<shortid>.tmp
```

The final promotion temp must be created in the same live directory as the target file.

### 12.4 Ordering
Files should be committed in deterministic lexicographic path order.

### 12.5 Why `applied` is updated after rename
Rename is the true file-level commit point.

Safe ordering:

1. create sibling temp
2. rename sibling temp to live path
3. mark `applied = true` in WAL

If crash happens after rename but before WAL update, recovery may re-apply the same staged file, which is acceptable and simpler than trying to infer live-file equivalence.

---

## 13. Recovery Rules

### 13.1 No `.repo_tx/`
No active or recoverable write transaction exists.

### 13.2 `state == "open"`
Discard the transaction by deleting `.repo_tx/`.

Reason:
- staged files may be partially written
- transaction was not finalized
- safest rule is rollback

### 13.3 `state == "prepared"`
Begin commit.

Reason:
- all staged files were closed
- transaction was finalized and must be completed

### 13.4 `state == "committing"`
Resume commit.

Use per-file `applied` flags to continue promotion from the first unapplied file.

### 13.5 Cleanup
After all files are committed:

- delete `.repo_tx/`

---

## 14. Nested Repos

## 14.1 Fundamental rule
A nested repo is a separate transaction domain, not an ordinary directory for transaction ownership.

### 14.2 Parent treatment
The parent repo treats nested repos as opaque directories for ownership purposes.

The parent does not directly own or stage files inside a child repo.

### 14.3 Why nested repos exist
Nested repos are useful for:

- modular schema decomposition
- separating transactional hot zones
- scaling large trees without turning one repo into an oversized monolith
- independent logical ownership when appropriate

---

## 15. Coordinated Nested Transactions

### 15.1 Chosen semantics
The design adopts **Option A** semantics:

An outer transaction may coordinate nested repo transactions as participants in one logical transaction. After the logical transaction has prepared, crash recovery must finish the whole enrolled repo tree.

### 15.2 Lazy child enrollment
Child repo transactions are created lazily.

A child repo is enrolled only if the active ancestor transaction actually performs a write inside that child repo.

Read-only access does not enroll child repos.

### 15.3 Consequence
Nested repos remain independent by default. They join an outer logical transaction only when written under that active outer transaction.

---

## 16. Parent/Child Coordination Model

### 16.1 Parent is coordinator
When nested repos are enrolled, the outer repo transaction becomes a coordinator.

### 16.2 Child is participant
Each child repo maintains its own local `.repo_tx/` and its own local file-level WAL details.

### 16.3 Parent WAL tracks participation
The parent WAL must record enrolled child repos and their progress, but it does not need to mirror child file-level details.

Suggested child entry:

```json
{
  "repo_path": "users/alice",
  "prepared": true,
  "committed": false
}
```

### 16.4 Important division of responsibility

- child WAL:
  - local file staging and promotion
  - local recovery details

- parent WAL:
  - participant enrollment
  - hierarchical coordination state

---

## 17. Nested Commit Flow

### 17.1 Lazy enrollment flow

Example:

```python
with outer.transaction():
    with outer.config.open("w") as f:
        f.write("...")

    with outer.users["alice"].repo().profile.open("w") as f:
        f.write("...")
```

Behavior:

- opening `outer.config` creates or joins outer repo tx
- opening `alice.profile` for write under that active outer tx:
  - creates child repo tx lazily
  - registers child in parent tx
  - records child in parent WAL

### 17.2 Prepare order
Prepare recursively, deepest-first:

1. prepare enrolled child repos
2. mark child prepared in parent WAL
3. prepare parent repo

### 17.3 Commit order
Commit recursively, deepest-first, parent last:

1. commit enrolled child repos
2. mark child committed in parent WAL
3. commit parent repo
4. cleanup all `.repo_tx/`

This ordering publishes descendant states before publishing the outermost coordinating state.

---

## 18. Nested Recovery Rules

If outer recovery finds an unfinished coordinator WAL:

1. inspect parent WAL
2. look only at the child repos explicitly enrolled in that WAL
3. recursively recover or continue those children according to coordinator intent
4. finish parent commit
5. cleanup all repo transaction directories

### Important rule
Recovery must not guess or scan all possible nested repos. It should operate only on child repos explicitly enrolled in the current logical transaction.

---

## 19. API Recommendations

### 19.1 Preferred file API
Basic file access:

```python
with repo.config.open("r") as f:
    ...
```

```python
with repo.config.open("w") as f:
    ...
```

### 19.2 Preferred transaction API
Explicit transaction for multi-file atomicity:

```python
with repo.transaction():
    with repo.config.open("w") as f:
        ...
    with repo.notes.open("w") as f:
        ...
```

### 19.3 Nested transaction API
User-facing API should remain simple. Nested participation should usually be implicit under an active ancestor transaction.

Example:

```python
with workspace.transaction():
    with workspace.config.open("w") as f:
        f.write("root")

    alice = workspace.users["alice"].repo()
    with alice.profile.open("w") as f:
        f.write("child")
```

The child joins lazily under the current outer transaction.

---

## 20. Convenience APIs

The library should expose higher-level helpers in addition to raw `.open()`.

Recommended helpers:

- `read_text()`
- `write_text()`
- `read_bytes()`
- `write_bytes()`
- `read_json()`
- `write_json()`

All write helpers must go through the same transaction engine.

Example:

```python
repo.config.write_text("new config")
obj = repo.profile.read_json()
```

### Additional debugging helpers
Useful introspection APIs:

- `path`
- `abspath`
- `debug_status()`

---

## 21. Error and Abort Semantics

### 21.1 Before prepare
If an error occurs before prepare completes:

- rollback by deleting `.repo_tx/`

### 21.2 After prepare
If all participants have prepared and a crash or interruption occurs, recovery must complete commit.

### 21.3 During child enrollment
If child enrollment or child prepare fails before all-participants-prepared:

- abort the whole logical transaction
- delete any repo transaction directories that are still only in `open`

---

## 22. Why JSON over SQLite for the WAL

### Chosen recommendation
Use normal files, specifically JSON, for `wal.json`.

### Reasons

- WAL metadata is small
- simpler implementation
- easier manual debugging
- aligns with direct-file philosophy
- avoids extra DB locking/format complexity

### When SQLite might become attractive later
Only if the library later gains:

- large transaction history retention
- heavy metadata querying
- richer indexing and analytics
- significantly more complex coordinator metadata

For the current design, SQLite is unnecessary.

---

## 23. Scaling Considerations

### 23.1 Large trees
The model scales by allowing decomposition:

- small projects: one repo
- medium projects: one repo with `Dir` and `MappedDir`
- large projects: split some subtrees into `NestedRepo`

### 23.2 Static-schema bias
Static schema is preferred to reduce bugs and improve tooling.

Dynamic behavior is allowed only through explicitly declared `MappedDir` or mapped nested repos.

### 23.3 Transaction cost
Only repos that are actually written under an active transaction create `.repo_tx/`. This lazy enrollment avoids unnecessary WAL and recovery clutter in large trees.

---

## 24. Notes and Tradeoffs

### 24.1 Read-only access is simple by design
Read-only open does not interact with the WAL or transaction state.

### 24.2 Multi-file atomicity is logical, not a single filesystem primitive
The filesystem only gives per-file atomic rename. Multi-file atomicity is provided by WAL-guided recovery.

### 24.3 Process-crash resistance only
The target guarantee is resistance to process interruption and ordinary abrupt termination, not sudden power failure.

### 24.4 One writer keeps the system simple
Single active writer per repo is the primary simplification that makes the entire design practical and understandable.

### 24.5 Nested repos are powerful but not free
They improve scaling and modularity, but coordinated hierarchical commit requires the parent WAL to track child participation for Option A semantics.

---

## 25. Summary of Final Decisions

### Storage model
- direct files in live repo tree
- hidden `.repo_tx/` for active/pending write transactions
- `wal.json` + `staged/`

### Writer model
- one active write transaction per repo
- `.repo_tx/` presence is the ownership and recovery marker

### Read semantics
- read-committed
- read-only opens ignore WAL and staging

### Write semantics
- copy-on-write staging
- `.open("w")` works on staged copy
- implicit single-file transaction allowed

### WAL
- JSON file, not SQLite
- states: `open`, `prepared`, `committing`

### Commit primitive
- staged file -> sibling temp beside live file -> atomic rename

### Recovery
- `open` => discard
- `prepared` => start commit
- `committing` => resume commit

### Nested repos
- explicit schema nodes
- independent transaction domains by default
- may be lazily enrolled in an active ancestor transaction

### Nested coordinated semantics
- Option A selected
- prepare deepest-first
- commit deepest-first, parent last
- recursive recovery driven by parent coordinator WAL

---

## 26. Minimal End-to-End Example

```python
class UserRepo(Repo):
    profile = File("profile.json")


class WorkspaceRepo(Repo):
    config = File("config.json")
    users = MappedNestedRepo("users", repo_type=UserRepo)


ws = WorkspaceRepo("/data/ws")

with ws.transaction():
    with ws.config.open("w") as f:
        f.write("root config")

    alice = ws.users["alice"].repo()
    with alice.profile.open("w") as f:
        f.write('{"name": "Alice"}')
```

Resulting behavior:

1. outer transaction becomes active
2. root repo stages `config.json`
3. child repo `users/alice` is enrolled lazily on first child write
4. child stages `profile.json`
5. on outer transaction exit:
   - child prepares
   - parent records child prepared
   - parent prepares
   - child commits
   - parent records child committed
   - parent commits
6. if crash occurs after prepare:
   - recovery completes the whole logical transaction

---

## 27. Recommended Next Implementation Steps

1. define concrete Python class interfaces for:
   - `Repo`
   - `File`
   - `Dir`
   - `MappedDir`
   - `NestedRepo`
   - transaction context objects

2. define exact `wal.json` dataclasses / schema

3. implement local repo state machine:
   - create tx
   - stage file
   - prepare
   - commit
   - recover

4. implement nested enrollment:
   - parent coordinator state
   - child participant registration
   - recursive prepare/commit/recover

5. add convenience helpers:
   - text/json/bytes helpers
   - debug/status tooling

6. write failure-injection tests:
   - crash before prepare
   - crash after prepare
   - crash mid-commit
   - crash in nested child commit
   - crash after child commit but before parent commit

---

End of design plan.

## Phase 2 Goal: Backend-Neutral Transaction API Contract

This phase formalizes the transaction layer as a backend-neutral semantic contract.

The purpose is to preserve the user-facing Python file-style API, especially `open(...)`, while allowing different implementation techniques beneath it:

- direct filesystem staging
- single-file SQLite storage
- full SQL database backends such as MySQL or PostgreSQL

The key rule is that **transaction semantics are defined independently of storage mechanics**.

### 28. Abstract State Model

A repo transaction domain is modeled as a partial mapping:

```text
S : K -> V ∪ {⊥}
```

Where:

- `K` = logical object keys
- `V` = full object values as bytes
- `⊥` = object absent / not present

Examples of logical keys:

- `config.json`
- `users/alice/profile.json`

A logical key is part of the semantic model only. It is not required to be a literal filesystem path or a literal SQL row key in every backend.

### 29. Transaction Overlay Model

A transaction is modeled as:

```text
Tx = (S0, Δ, state)
```

Where:

- `S0` = committed base state visible when the transaction begins
- `Δ` = finite overlay of pending modifications
- `state` = transaction lifecycle state

The overlay is a partial mapping:

```text
Δ : K -> Put(V) | Delete
```

The transaction-visible state is defined by overlay lookup:

```text
SΔ(k) =
  Δ(k).value          if Δ(k) = Put(value)
  ⊥                   if Δ(k) = Delete
  S0(k)               if k not in dom(Δ)
```

This overlay rule is the universal read semantics for all backends.

### 30. Universal Transaction Semantics

The transaction layer must implement these abstract operations:

- `read(k)`
- `write(k, value)`
- `delete(k)`
- `prepare()`
- `commit()`
- `rollback()`
- `recover()`

Their semantics are:

#### 30.1 `read(k)`
Returns the transaction-visible value of `k` from `SΔ`.

#### 30.2 `write(k, value)`
Sets:

```text
Δ[k] := Put(value)
```

#### 30.3 `delete(k)`
Sets:

```text
Δ[k] := Delete
```

#### 30.4 `rollback()`
Discards `Δ` and ends the transaction without changing committed state.

#### 30.5 `commit()`
Applies the overlay to committed state:

```text
S1 = S0 ⊕ Δ
```

Where overlay application means:

- `Put(v)` replaces the committed value of that key with `v`
- `Delete` removes that key from committed state
- keys absent from `Δ` remain unchanged

The backend is free to realize `⊕` using any correct mechanism.

### 31. Transaction Lifecycle Contract

The backend-neutral lifecycle states are:

- `open`
- `prepared`
- `committed`
- `aborted`

An implementation may internally expose more states such as `committing`, but those are refinements of the same contract.

#### 31.1 `open`
- transaction exists
- overlay `Δ` may still change
- writable handles may still be open

#### 31.2 `prepared`
- all writable handles are closed
- `Δ` is frozen and may no longer change
- commit intent has been established
- recovery must have enough information to resolve the transaction according to backend durability rules

#### 31.3 `committed`
- `Δ` has been fully applied to committed state

#### 31.4 `aborted`
- `Δ` has been discarded

### 32. Buffered File-Handle Model

The user-facing `open(...)` API is defined as a buffered editing view over one logical key.

A writable handle is **not** the committed object and is **not** the backend storage primitive.
It is an editor that eventually produces one final full value for a logical key.

Each writable handle has at least:

- `key`
- `mode`
- `buffer`
- `cursor`
- `closed`
- `tx`

The handle reads from and writes to its local buffer according to Python-style file semantics.
On handle close, the final buffer state is materialized into the transaction overlay.

### 33. `open(...)` Contract

Let `k` be a logical key in transaction `Tx`.

#### 33.1 Read-only open
`open(k, "r")` and `open(k, "rb")`:

- require `SΔ(k)` to exist
- expose the current transaction-visible value
- do not modify `Δ`
- do not by themselves force transaction materialization if the implementation supports lazy write transactions

#### 33.2 Truncating write open
`open(k, "w")` and `open(k, "wb")`:

- create a writable handle with empty initial buffer
- do not modify committed state directly
- on successful handle close:

```text
Δ[k] := Put(final_buffer)
```

#### 33.3 Append open
`open(k, "a")` and `open(k, "ab")`:

- initialize the buffer from `SΔ(k)` if present, otherwise empty
- initial cursor position is end-of-buffer
- on successful handle close:

```text
Δ[k] := Put(final_buffer)
```

#### 33.4 Read/write open
`open(k, "r+")`, `open(k, "rb+")`, and corresponding read/write modes:

- require `SΔ(k)` to exist unless Python mode rules would create the file
- initialize the buffer from `SΔ(k)`
- allow reads and writes against the buffer
- on successful handle close:

```text
Δ[k] := Put(final_buffer)
```

### 34. Full-Value Replacement Rule

For backend neutrality, the transaction contract is defined in terms of **whole-object replacement**, not in-place backend mutation.

That is, writable handle close produces a final full value:

```text
Δ[k] := Put(v_final)
```

This rule is intentionally chosen because it maps naturally onto:

- staged files in a filesystem backend
- blob/text replacement in SQLite
- row/value replacement in SQL backends
- codec-based structured values such as JSON

Backends may internally optimize writes, but the semantic contract remains whole-object replacement.

### 35. Visibility Rules

The transaction layer must preserve these visibility rules.

#### 35.1 Read-committed outside a transaction
Reads outside an active write transaction see only committed state.

#### 35.2 Overlay visibility inside a transaction
Reads inside a transaction see the overlayed state `SΔ`.
A transaction must read its own writes.

#### 35.3 No direct committed mutation
A writable handle must never expose partially updated committed state to ordinary readers.

### 36. Handle Close and Abort Semantics

#### 36.1 Handle close
On successful close of a writable handle:

- the handle buffer becomes immutable to user code
- the transaction overlay is updated with the final full value
- the handle is marked closed

#### 36.2 Handle failure before close
If a writable handle fails before successful close, the transaction overlay is not required to reflect partially written buffer state.

#### 36.3 Transaction abort before prepare
If the transaction aborts before prepare completes, all writable handles and their pending overlay effects are discarded.

### 37. Multiple Handle Rules

To keep semantics simple and portable across backends, the contract should adopt these rules.

#### 37.1 Single active writable handle per key per transaction
At most one writable handle for the same logical key may be open at a time within one transaction.

Reason:
- avoids ambiguous merge semantics
- matches the design goal of a simple, understandable transaction layer
- is easy to enforce for all backends

#### 37.2 Read handles while a key has a pending write
A read handle opened after a key has a pending overlay entry should read from `SΔ`, not from `S0`.

#### 37.3 Reopen after writable close
After a writable handle for key `k` closes successfully, later reads or writable opens for `k` within the same transaction must use the current overlay-visible value.

### 38. Prepare Contract

`prepare()` is defined abstractly as:

> freeze the transaction overlay and establish commit intent.

The semantic effects are:

- no writable handle remains open
- `Δ` becomes immutable
- recovery metadata, if any, must be sufficient to resolve the transaction consistently

`prepare()` is not defined in terms of any particular mechanism such as a JSON WAL file, SQLite savepoint, or SQL two-phase commit.
Those are backend implementation choices.

### 39. Commit Contract

`commit()` is defined abstractly as the transition:

```text
(S0, Δ, prepared) -> (S1, ∅, committed)
```

Where:

```text
S1 = S0 ⊕ Δ
```

The contract requires:

- once `commit()` reports success, all effects of `Δ` are part of committed state
- no effect outside `Δ` may be introduced
- a successful commit must correspond to exactly one overlay application

### 40. Rollback Contract

`rollback()` is defined abstractly as the transition:

```text
(S0, Δ, open|prepared) -> (S0, ∅, aborted)
```

The contract requires:

- committed state remains `S0`
- no pending overlay state survives

An implementation may restrict rollback after `prepared` if it chooses commit-on-prepare semantics, but such behavior must be explicitly documented by that backend.
The preferred default is that `prepare` freezes and establishes intent, while final publication remains the responsibility of `commit` or `recover`.

### 41. Recovery Contract

The universal recovery rule is based on lifecycle state, not storage technology.

#### 41.1 Recovery from `open`
If the transaction was only `open`, the implementation may discard it.

#### 41.2 Recovery from `prepared`
If the transaction had reached `prepared`, recovery must resolve it according to the backend's durability contract.
For the direct-file design, the preferred rule remains: complete commit.

#### 41.3 Recovery correctness requirement
After recovery, observable state must be equivalent to one of:

- full rollback to `S0`, or
- full commit to `S0 ⊕ Δ`

A partially published mixed state is not a valid final recovery outcome.

### 42. Backend Contract Surface

A backend that supports this transaction layer must provide enough capability to realize the semantics above.
A minimal conceptual contract is:

```python
class Backend:
    def begin(self, repo_locator, parent_tx=None) -> BackendTransaction: ...
    def recover(self, repo_locator) -> None: ...
    def debug_status(self, repo_locator) -> object: ...


class BackendTransaction:
    def read_visible(self, key: str) -> bytes | None: ...
    def put(self, key: str, value: bytes) -> None: ...
    def delete(self, key: str) -> None: ...
    def prepare(self) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
```

The exact internal representation of state, staging, and recovery metadata is backend-specific.
The semantic behavior is not.

### 43. Public Python API Contract

The repo-facing API should preserve file-style ergonomics while being defined over the abstract model.

Recommended public contract:

```python
class Repo:
    def transaction(self): ...


class FileNode:
    def open(self, mode: str = "r", *, encoding=None): ...
    def read_bytes(self) -> bytes: ...
    def write_bytes(self, data: bytes) -> None: ...
    def read_text(self, encoding: str = "utf-8") -> str: ...
    def write_text(self, text: str, encoding: str = "utf-8") -> None: ...
```

Semantics:

- helper methods such as `write_text()` are shorthand for a writable buffered handle followed by close and overlay update
- helper methods participate in the same transaction machinery as raw `open(...)`
- helper methods must not bypass the transaction layer

### 44. Required Invariants

The implementation should preserve these invariants across all backends.

#### 44.1 Overlay authority
Within an active transaction, the authoritative pending state is `Δ`, not any individual handle.

#### 44.2 Read-your-writes
Within one transaction, reads must observe prior successful writes from that same transaction.

#### 44.3 One final pending value per key
At any moment, a transaction has at most one authoritative pending overlay entry for a given key.

#### 44.4 No partial publication
Committed readers must never observe an invalid partial publication of a single logical object.

#### 44.5 Commit equivalence
A successfully committed transaction must be observationally equivalent to applying the overlay `Δ` to the base state `S0`.

### 45. Relationship to Phase 1 Direct-File Design

Phase 1 remains the reference implementation for the direct-file backend.
Its staged copies, JSON WAL, prepare/commit lifecycle, and nested participant coordination are valid realizations of this Phase 2 contract.

Phase 2 does not replace the Phase 1 design.
Instead, it extracts and formalizes the backend-neutral semantics that Phase 1 already suggests:

- committed state vs staged state
- transaction overlay semantics
- buffered file-handle editing
- prepare as overlay freeze + commit intent
- commit as publication of a frozen write set
- recovery based on lifecycle state

This Phase 2 contract is the basis for supporting alternative persistence backends without changing user-facing transaction semantics.
