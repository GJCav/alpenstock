from __future__ import annotations

from typing import assert_type

import attrs

from alpenstock.storage import (
    BinaryFileHandle,
    CodecFile,
    Dir,
    FileCodec,
    FileNode,
    MappedRepo,
    Repo,
    TextFileHandle,
    TransactionContext,
    define,
    named,
)
from alpenstock.storage.backends.fs import FilesystemBackend
from alpenstock.storage.backends.sqlite import SqliteBackend, SqliteConfig


backend = FilesystemBackend()
repo = Repo.open("/tmp/alpenstock-storage-typecheck", backend=backend)
assert_type(repo, Repo)

file_node = repo.file("alpha")
assert_type(file_node, FileNode)
assert_type(file_node.read_text(), str)
assert_type(file_node.read_bytes(), bytes)
assert_type(file_node.open("r"), TextFileHandle)
assert_type(file_node.open("rb"), BinaryFileHandle)
assert_type(file_node.open("r").read(), str)
assert_type(file_node.open("rb").read(), bytes)


@attrs.define(frozen=True, slots=True)
class IntCodec:
    def loads(self, payload: bytes) -> int:
        return int(payload.decode("utf-8"))

    def dumps(self, value: int) -> bytes:
        return str(value).encode("utf-8")


codec: FileCodec[int] = IntCodec()
typed_file = file_node.as_codec(codec)
assert_type(typed_file, CodecFile[int])
assert_type(typed_file.read(), int)
assert_type(typed_file.open("r"), TextFileHandle)
assert_type(typed_file.open("rb"), BinaryFileHandle)
assert_type(typed_file.open("r").read(), str)
assert_type(typed_file.open("rb").read(), bytes)

with repo.transaction() as tx:
    assert_type(tx, TransactionContext)
    assert_type(tx.open("alpha", "r"), TextFileHandle)
    assert_type(tx.open("alpha", "rb"), BinaryFileHandle)
    file_node.write_text("hello")
    file_node.delete()


@define
class SettingsDir(Dir):
    theme: FileNode = named("theme.toml")


@define
class UserRepo(Repo):
    profile: FileNode


@define
class AppRepo(Repo):
    settings: SettingsDir
    users: MappedRepo[UserRepo]
    readme: FileNode = named("README.md")


typed_repo = AppRepo.open("/tmp/alpenstock-storage-typecheck-schema", backend=backend)
assert_type(typed_repo, AppRepo)
assert_type(typed_repo.settings, SettingsDir)
assert_type(typed_repo.users["alice"], UserRepo)
assert_type(typed_repo.readme.open("rb"), BinaryFileHandle)
assert_type(typed_repo.readme.open("rb").read(), bytes)

sqlite_backend = SqliteBackend(config=SqliteConfig(schema_prefix="_storage_", busy_timeout_ms=1000))
assert_type(sqlite_backend, SqliteBackend)
