# Introduction

This document introduces the `storage` module at a high level.

It explains what problem the module exists to solve, what it deliberately does
not try to solve, and how application code is expected to use it.

This is an introduction document, not the formal contract. Later `v0.2`
documents define the exact transaction model, lifecycle, recovery rules, nested
repo semantics, and extensibility boundaries.

## 1. Motivation

The `storage` module exists to provide a thin application-facing layer over
ordinary files.

The module is meant to give applications three useful properties at the same
time:

- transactional updates
- crash consistency for accidental application exit
- user-transparent on-disk data

Transactional means the application can group a batch of writes and then choose
to either commit them all together or roll them back together.

Crash consistency means incomplete writes should not leave durable data in a
broken mixed state after accidental process termination such as interpreter
death, `kill`, or abrupt application exit.

User-transparent means committed data remains stored as normal files in the
repository tree. Users are allowed to inspect those files, open them with
default system applications, and edit them directly when the application is not
actively performing conflicting transactional work.

The intended position of `storage` is therefore:

- thinner and more transparent than a database
- safer than ad hoc direct file writes
- small enough to reason about as application infrastructure

## 2. Non-Goals

The module deliberately does not try to be a full storage platform.

Important non-goals are:

- multi-version history or version control
- system-crash consistency under sudden power loss
- complex concurrency models
- many-writer coordination
- full database isolation levels
- opaque storage formats that hide the real files from users

In particular, the target crash model is accidental application/process exit,
not full machine failure. If the system loses power at the wrong instant, this
module does not aim to provide a strongest-possible durability story.

Likewise, the concurrency target is intentionally simple. The module should be
easy to understand and safe in the intended operating envelope, not a highly
concurrent transactional database.

## 3. High-Level Shape

At a high level, the module presents application data as a schema-bound
repository.

The main concepts are:

- `Repo`: the root of an application data repository and the transaction domain
- `FileNode`: one logical file in the repo
- `Dir`: a statically declared directory subtree
- `MappedDir[T]`: a runtime-keyed directory subtree whose elements all share one shape

Applications define a repo schema in Python, then open a concrete repo instance
at some filesystem location.

Transactions provide the boundary for atomic multi-file updates. Reads and
writes go through schema-bound nodes rather than manual path manipulation.

## 4. Usage Level 1: Basic Repository Usage

The most basic usage is declaring a small repo schema and then reading or
writing files through that schema.

The schema should make the resulting folder structure easy to understand.
Attribute names normally map directly to storage names, but `storage.named(...)`
lets the application choose a different filename or directory name when that is
clearer or more compatible with existing data.

```python
from alpenstock import storage


@storage.define
class SettingsDir(storage.Dir):
    profile: storage.FileNode = storage.named("profile.toml")
    flags: storage.FileNode


@storage.define
class UserDir(storage.Dir):
    notes: storage.FileNode = storage.named("notes.txt")


@storage.define
class AppRepo(storage.Repo):
    config: storage.FileNode = storage.named("config.toml")
    settings: SettingsDir
    users: storage.MappedDir[UserDir] = storage.named("people")
```

If `repo = AppRepo.open("/path/to/app-data")`, then the schema
above describes a repository shaped like:

```text
/path/to/app-data/
  config.toml
  settings/
    profile.toml
    flags
  people/
    alice/
      notes.txt
    bob/
      notes.txt
```

So:

- `repo.config` maps to `config.toml`
- `repo.settings.profile` maps to `settings/profile.toml`
- `repo.users["alice"].notes` maps to `people/alice/notes.txt`

Open the repo:

```python
repo = AppRepo.open("/path/to/app-data")
```

In the intended `v0.2` API, the default backend is the filesystem backend, so
the basic examples do not need to spell it out.

Simple reads:

```python
config_text = repo.config.read_text()
notes_bytes = repo.users["alice"].notes.read_bytes()
```

Atomic batch update with an explicit transaction:

```python
with repo.transaction():
    repo.config.write_text("version = 2\n")
    repo.settings.flags.write_text("feature_x = true\n")
    repo.users["alice"].notes.write_text("hello\n")
```

If the block exits normally, the batch is committed.

If an exception escapes the block, the batch is rolled back:

```python
try:
    with repo.transaction():
        repo.config.write_text("version = 3\n")
        repo.users["alice"].notes.write_text("updating...\n")
        raise RuntimeError("stop here")
except RuntimeError:
    pass
```

After the exception, neither change should become committed state.

For single-file updates, the API should also support the convenient shortcut of
opening an implicit transaction for just that one modification. In practice,
helpers such as `write_bytes(...)` and `write_text(...)` are the short form for
that case:

```python
repo.config.write_text("version = 4\n")
repo.users["alice"].notes.write_bytes(b"hello")
```

This is meant for the common case where the application only needs to change
one logical file and does not need to spell out an explicit transaction block.

## 5. Usage Level 2: Extending a Simple File Type

Applications often want a few typed file helpers without changing the
transaction model itself.

For example, a simple `TomlFile` can be built as a small `FileNode` subclass:

```python
from __future__ import annotations

import tomllib

from alpenstock import storage


class TomlFile(storage.FileNode):
    def read_toml(self) -> dict[str, object]:
        return tomllib.loads(self.read_text())

    def write_toml(self, value: dict[str, object]) -> None:
        lines: list[str] = []
        for key, item in value.items():
            lines.append(f"{key} = {item!r}")
        self.write_text("\n".join(lines) + "\n")


@storage.define
class AppRepo(storage.Repo):
    config: TomlFile
```

Usage stays simple:

```python
repo = AppRepo.open("/path/to/app-data")

config = repo.config.read_toml()
config["enabled"] = True
repo.config.write_toml(config)
```

The important idea is that file-type extension should remain thin. A custom
file class may provide typed parsing and formatting helpers, but transaction and
recovery semantics still belong to the storage layer rather than to each file
type.

## 6. Usage Level 3: Nested Repositories

Large applications may want to split one big repository into nested
sub-repositories with their own boundaries and structure.

A minimal example:

```python
from alpenstock import storage


@storage.define
class UserRepo(storage.Repo):
    profile: storage.FileNode
    notes: storage.FileNode


@storage.define
class AppRepo(storage.Repo):
    config: storage.FileNode
    users: storage.MappedRepo[UserRepo]
```

Usage:

```python
repo = AppRepo.open("/path/to/app-data")

with repo.transaction():
    repo.config.write_text("schema = 1\n")
    repo.users["alice"].profile.write_text("name = 'Alice'\n")
```

Nested repos are useful when the application wants stronger structural
boundaries than a plain directory tree provides.

The exact semantics of nested repositories are intentionally deferred to later
documents. In `v0.2`, the target model is closed nested repo coordination, and
that contract deserves its own dedicated design document rather than being
compressed into this introduction.

## 7. What Later Documents Define

This introduction is only the entry point.

Subsequent `v0.2` documents define the details that matter for a correct
implementation:

- the mathematical model of transactions
- lifecycle states and commit/rollback rules
- crash-recovery rules and journal obligations
- the exact semantics of closed nested repos
- repo coordination across nested boundaries
- backend architecture and future extensibility boundaries

Those later documents should answer questions such as:

- what exactly a transaction sees while it is open
- what must be durable before a transaction is considered prepared
- how nested child repos relate to parent transactions
- whether backend responsibilities should split into pieces such as blob storage
  and journal storage

## 8. Relationship to `v0.1`

The `v0.2` line is a redesign.

Historical material from the existing implementation line is preserved under:

- `dev-logs/storage/v0.1/`

That older material remains useful as background, but `v0.2` documents should
state the new intended design directly and should not be constrained by old
implementation choices that the refactor plans to remove.
