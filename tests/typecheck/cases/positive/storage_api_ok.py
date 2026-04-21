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
    field,
)
from alpenstock.storage.backends.fs import FilesystemBlobBackend, JsonlWalJournalBackend


blob_backend = FilesystemBlobBackend()
journal_backend = JsonlWalJournalBackend()
repo = Repo.open(
    "/tmp/alpenstock-storage-typecheck",
    blob_backend=blob_backend,
    journal_backend=journal_backend,
)
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
    theme: FileNode = field(name="theme.toml")


@define
class UserRepo(Repo):
    profile: FileNode


@define
class AppRepo(Repo):
    settings: SettingsDir
    users: MappedRepo[UserRepo]
    readme: FileNode = field(name="README.md")


typed_repo = AppRepo.open(
    "/tmp/alpenstock-storage-typecheck-schema",
    blob_backend=blob_backend,
    journal_backend=journal_backend,
)
assert_type(typed_repo, AppRepo)
assert_type(typed_repo.settings, SettingsDir)
assert_type(typed_repo.users["alice"], UserRepo)
assert_type(typed_repo.readme.open("rb"), BinaryFileHandle)
assert_type(typed_repo.readme.open("rb").read(), bytes)
