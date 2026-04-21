from __future__ import annotations

from pathlib import Path
from typing import cast

import attrs

from alpenstock.storage import BinaryFileHandle, Repo, TextFileHandle
from tests.storage._fs_test_utils import init_repo_metadata, open_repo, snapshot_repo_bytes


@attrs.define(frozen=True, slots=True)
class IntCodec:
    def loads(self, payload: bytes) -> int:
        return int(payload.decode("utf-8"))

    def dumps(self, value: int) -> bytes:
        return str(value).encode("utf-8")


def test_codec_file_round_trips_through_underlying_file_node(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo, _backend = open_repo(repo_root)
    typed_file = repo.file("count").as_codec(IntCodec())

    typed_file.write(42)

    assert typed_file.read() == 42
    assert snapshot_repo_bytes(repo_root) == {"count": b"42"}


def test_codec_file_delete_delegates_to_underlying_file_node(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    init_repo_metadata(repo_root)
    (repo_root / "count").parent.mkdir(parents=True, exist_ok=True)
    (repo_root / "count").write_bytes(b"7")
    repo, _backend = open_repo(repo_root)
    typed_file = repo.file("count").as_codec(IntCodec())

    typed_file.delete()

    assert snapshot_repo_bytes(repo_root) == {}


def test_codec_file_open_preserves_underlying_handle_types(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    init_repo_metadata(repo_root)
    (repo_root / "count").parent.mkdir(parents=True, exist_ok=True)
    (repo_root / "count").write_bytes(b"7")
    repo, _backend = open_repo(repo_root)
    typed_file = repo.file("count").as_codec(IntCodec())

    with typed_file.open("r") as handle:
        assert cast(TextFileHandle, handle).read() == "7"

    with typed_file.open("rb") as handle:
        assert cast(BinaryFileHandle, handle).read() == b"7"
