# Storage Rev 2 Implementation Record

Date: 2026-04-10

This document records what has been implemented after [04 - Design Rev 1.md](./04%20-%20Design%20Rev%201.md).

It is an implementation-status note, not a full design rewrite. For the historical design rationale, still read:

- [01 - Initial Design.md](./01%20-%20Initial%20Design.md)
- [02 - Backend-Neutral Transaction Implementation Plan.md](./02%20-%20Backend-Neutral%20Transaction%20Implementation%20Plan.md)
- [03 - Backend-Owned Handle Refactor Plan.md](./03%20-%20Backend-Owned%20Handle%20Refactor%20Plan.md)
- [04 - Design Rev 1.md](./04%20-%20Design%20Rev%201.md)

## 1. Summary

Rev 2 implementation work is now landed for the two big deferred targets:

1. coordinated nested repo transactions
2. schema-bound attrs-based storage API

The current storage layer now supports:

- backend-neutral transaction overlay semantics
- filesystem backend
- SQLite backend
- coordinated nested repo transactions on both backends
- schema-bound `Repo` / `Dir` / `MappedDir` / `MappedRepo` / `FileNode`
- continued support for the untyped escape hatch such as `repo.file("...")`
- crash/recovery tests for the accidental-exit safety target

## 2. Implemented Rev 2 Features

### 2.1 Coordinated nested repo transactions

Implemented behavior:

- nested repos are separate transaction domains
- same-repo nested transactions remain prohibited
- child repos are enrolled lazily
- read-only child access does not enroll the child
- `child_repo.transaction()` inside an active ancestor transaction joins the outer logical transaction
- prepare order is deepest-first
- commit order is deepest-first, parent last
- recovery follows only explicitly enrolled child repos recorded by the parent

This matches the Option A coordination model chosen in the historical design.

### 2.2 Schema-bound storage API

Implemented public schema-facing types:

- `Repo`
- `Dir`
- `MappedDir[T]`
- `MappedRepo[TRepo]`
- `FileNode`

Implemented declaration helpers:

- `storage.define`
- `storage.named(...)`

Implemented binding behavior:

- `AppRepo.open(...)` returns a bound instance of `AppRepo`
- declared children are bound automatically
- fixed child repos bind to direct repo subclass instances
- `MappedRepo[UserRepo]` returns directly bound `UserRepo` instances
- `MappedDir[...]` returns directly bound directory or file children

The low-level API remains available:

- `repo.file("relative/key")`

## 3. Main Runtime Structure

### 3.1 Shared API and schema/runtime binding

Main files:

- [src/alpenstock/storage/repo.py](../../src/alpenstock/storage/repo.py)
- [src/alpenstock/storage/dir.py](../../src/alpenstock/storage/dir.py)
- [src/alpenstock/storage/file.py](../../src/alpenstock/storage/file.py)
- [src/alpenstock/storage/transaction.py](../../src/alpenstock/storage/transaction.py)
- [src/alpenstock/storage/_schema.py](../../src/alpenstock/storage/_schema.py)

Key outcomes:

- `Repo` is now both the repo transaction boundary and the schema root
- `Dir` is schema-bound structure inside a repo domain
- nested repos carry ancestry information
- active ancestor transactions can coordinate child repo transactions

### 3.2 Filesystem backend coordination

Main files:

- [src/alpenstock/storage/backends/fs/backend.py](../../src/alpenstock/storage/backends/fs/backend.py)
- [src/alpenstock/storage/backends/fs/recovery.py](../../src/alpenstock/storage/backends/fs/recovery.py)

Key outcomes:

- parent WAL now records enrolled child repos and child progress flags
- child repos keep their own local `.repo_tx/` state
- root recovery drives only the enrolled child repo set from the parent WAL

### 3.3 SQLite backend coordination

Main files:

- [src/alpenstock/storage/backends/sqlite/backend.py](../../src/alpenstock/storage/backends/sqlite/backend.py)
- [src/alpenstock/storage/backends/sqlite/_support.py](../../src/alpenstock/storage/backends/sqlite/_support.py)

Key outcomes:

- SQLite now supports repo-path-scoped transaction domains inside one DB file
- child repos in one database are coordinated under the same outer logical transaction
- parent/child progress is stored in SQLite metadata tables
- recovery replays only the recorded child participant tree

## 4. Verification Status

Current verification after Rev 2 implementation:

- `pixi run pytest -q tests/storage`
- `pixi run pyright src/alpenstock/storage tests/storage tests/typecheck/cases/positive/storage_api_ok.py`
- `pixi run pytest -q tests/typecheck/test_pipeline_typing_contracts.py`

Observed result at record time:

- storage runtime tests: passing
- positive typecheck suite: passing
- pipeline typing contract tests: passing

## 5. New Test Coverage Added

Main new or substantially revised coverage:

- [tests/storage/test_schema_binding.py](../../tests/storage/test_schema_binding.py)
- [tests/storage/test_nested_coordination.py](../../tests/storage/test_nested_coordination.py)
- [tests/storage/test_sqlite_backend.py](../../tests/storage/test_sqlite_backend.py)

These cover:

- schema-bound repo opening
- declared child binding
- mapped repo and mapped dir access
- outer/inner coordinated transactions
- lazy child enrollment
- nested recovery
- SQLite nested repo persistence model

## 6. Notes

### 6.1 Safety target remains unchanged

Rev 2 still targets:

- accidental process exit
- interpreter death
- abrupt application termination

It does not attempt full power-loss durability guarantees.

### 6.2 One implementation cleanup already applied

The SQLite backend implementation was split so that no single source file exceeds the preferred per-file size target.

### 6.3 One small typing note remains

`pyright` still reports a non-failing warning around the generic decorator typing shape in `storage.define`, but there are no type errors in the storage module or its positive typing fixtures.

## 7. Effective Status

Relative to Rev 1, the major deferred items that are now implemented are:

- nested repo coordinated transactions
- schema-bound storage UI layer

So the storage module now has both:

- a low-level backend-neutral transactional file API
- a higher-level attrs-bound schema API built on the same transaction semantics
