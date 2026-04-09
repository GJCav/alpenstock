from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

import alpenstock.storage.backends.fs.backend as fs_backend_module
from alpenstock.storage import Repo, TransactionStateError, ValueContractError, WriteConflictError
from alpenstock.storage._handles import BinaryFileHandle, TextFileHandle
from alpenstock.storage._types import DELETE, Put
from alpenstock.storage.backends.fs import FilesystemBackend, RepoLayout, WriterLock
from alpenstock.storage.backends.fs.recovery import load_wal


def _write_bytes(tx, key: str, payload: bytes) -> None:
    with cast(BinaryFileHandle, tx.open_handle(key, "wb")) as handle:
        handle.write(payload)


def _read_wal_records(layout: RepoLayout) -> list[dict[str, object]]:
    return [
        cast(dict[str, object], json.loads(line))
        for line in layout.wal_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_read_only_access_does_not_create_transaction_state(tmp_path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    layout.committed_path("alpha").parent.mkdir(parents=True, exist_ok=True)
    layout.committed_path("alpha").write_text("hello", encoding="utf-8")
    repo = Repo.open(str(tmp_path), backend=backend)

    assert repo.file("alpha").read_text() == "hello"
    assert not layout.tx_root.exists()
    assert not layout.lock_path.exists()


def test_committed_backend_handles_reject_write_capable_modes(tmp_path: Path) -> None:
    backend = FilesystemBackend()

    with pytest.raises(TransactionStateError, match="Committed views are read-only"):
        backend.open_committed_handle(str(tmp_path), "alpha", "w")

    with pytest.raises(TransactionStateError, match="Committed views are read-only"):
        backend.open_committed_handle(str(tmp_path), "alpha", "rb+")


def test_transaction_directory_materializes_lazily_on_first_writable_open(tmp_path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    repo = Repo.open(str(tmp_path), backend=backend)

    tx = repo.transaction()
    tx.__enter__()
    assert not layout.tx_root.exists()

    handle = tx.open("alpha", "w")
    assert layout.tx_root.exists()
    assert not layout.wal_path.exists()
    handle.write("hello")
    handle.close()
    assert not layout.wal_path.exists()

    tx.rollback()
    tx.__exit__(None, None, None)

    assert not layout.tx_root.exists()


def test_second_writer_is_rejected_while_first_transaction_holds_lock(tmp_path) -> None:
    backend = FilesystemBackend()
    first = backend.begin(str(tmp_path))

    with pytest.raises(WriteConflictError, match="already owns repo lock"):
        backend.begin(str(tmp_path))

    first.rollback()


def test_begin_rejects_pending_unrecovered_transaction_state(tmp_path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    layout.ensure_tx_root()
    layout.wal_path.write_text('{"kind":"state","state":"open"}\n', encoding="utf-8")

    with pytest.raises(TransactionStateError, match="pending recovery state exists; run recover\\(\\) first"):
        backend.begin(str(tmp_path))


def test_empty_prepare_does_not_materialize_transaction_directory(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)

    tx = backend.begin(str(tmp_path))
    tx.prepare()

    assert not layout.tx_root.exists()

    tx.commit()


def test_delete_rejects_invalid_logical_key_before_writing_transaction_state(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    tx = backend.begin(str(tmp_path))

    with pytest.raises(ValueContractError, match="must not contain|relative path"):
        tx.delete("../bad")

    assert not layout.tx_root.exists()
    tx.rollback()


@pytest.mark.parametrize("key", [".repo_tx/wal.jsonl", ".repo_tx.lock"])
def test_internal_backend_paths_are_reserved_from_user_keys(tmp_path: Path, key: str) -> None:
    backend = FilesystemBackend()
    tx = backend.begin(str(tmp_path))

    with pytest.raises(ValueContractError, match="internal paths"):
        tx.open_handle(key, "wb")

    tx.rollback()


def test_reader_opened_while_writer_is_open_reads_live_staged_copy(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    alpha_path = layout.committed_path("alpha")
    alpha_path.parent.mkdir(parents=True, exist_ok=True)
    alpha_path.write_text("old", encoding="utf-8")

    tx = backend.begin(str(tmp_path))
    writer = cast(TextFileHandle, tx.open_handle("alpha", "w"))
    writer.write("new")

    with tx.open_handle("alpha", "r") as reader:
        assert reader.read() == "new"

    writer.close()
    tx.rollback()


def test_reopen_same_key_starts_from_current_transaction_visible_value(tmp_path: Path) -> None:
    backend = FilesystemBackend()

    tx = backend.begin(str(tmp_path))
    first = cast(TextFileHandle, tx.open_handle("alpha", "w"))
    first.write("one")
    first.close()

    second = cast(TextFileHandle, tx.open_handle("alpha", "a"))
    second.write(" two")
    second.close()

    with tx.open_handle("alpha", "r") as reader:
        assert reader.read() == "one two"

    tx.rollback()


def test_prepare_writes_recoverable_wal_and_staged_payloads(tmp_path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    beta_path = layout.committed_path("beta")
    beta_path.parent.mkdir(parents=True, exist_ok=True)
    beta_path.write_bytes(b"old")

    tx = backend.begin(str(tmp_path))
    _write_bytes(tx, "alpha", b"one")
    tx.delete("beta")
    tx.prepare()

    state, overlay, children = load_wal(layout)
    assert state == "prepared"
    assert children == {}
    assert isinstance(overlay["alpha"], Put)
    assert overlay["beta"] is DELETE
    alpha_entry = cast(Put[object], overlay["alpha"])
    staged_relpath = cast(str, alpha_entry.value.relpath)  # type: ignore[attr-defined]
    staged_path = layout.tx_root / staged_relpath
    assert staged_path.read_bytes() == b"one"

    tx.rollback()


def test_commit_validates_all_staged_puts_before_mutating_committed_state(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    beta_path = layout.committed_path("beta")
    beta_path.parent.mkdir(parents=True, exist_ok=True)
    beta_path.write_bytes(b"old")

    tx = backend.begin(str(tmp_path))
    with cast(BinaryFileHandle, tx.open_handle("alpha", "wb")) as handle:
        handle.write(b"one")
    tx.delete("beta")
    tx.prepare()

    _state, overlay, _children = load_wal(layout)
    alpha_entry = cast(Put[object], overlay["alpha"])
    staged_path = layout.tx_root / alpha_entry.value.relpath  # type: ignore[attr-defined]
    staged_path.unlink()

    with pytest.raises(TransactionStateError, match="Missing staged payload"):
        tx.commit()

    assert beta_path.read_bytes() == b"old"
    assert not layout.committed_path("alpha").exists()

    tx.rollback()
    assert not layout.tx_root.exists()


def test_commit_rejects_conflicting_ancestor_and_descendant_puts_before_publication(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    tx = backend.begin(str(tmp_path))

    with cast(BinaryFileHandle, tx.open_handle("a", "wb")) as handle:
        handle.write(b"root")
    with cast(BinaryFileHandle, tx.open_handle("a/b", "wb")) as handle:
        handle.write(b"child")
    tx.prepare()

    with pytest.raises(TransactionStateError, match="conflicting put keys"):
        tx.commit()

    assert not layout.committed_path("a").exists()
    tx.rollback()


def test_open_failure_does_not_leave_hidden_writer_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FilesystemBackend()
    tx = backend.begin(str(tmp_path))

    def fail_initialize(self, *args, **kwargs) -> None:
        del self, args, kwargs
        raise RuntimeError("initialize failed")

    with monkeypatch.context() as m:
        m.setattr(type(tx), "_initialize_work_path", fail_initialize)
        with pytest.raises(RuntimeError, match="initialize failed"):
            tx.open_handle("alpha", "w")

    assert not tx.has_open_writers
    assert tx.core.read_visible_ref("alpha") is None

    with cast(TextFileHandle, tx.open_handle("alpha", "w")) as retry:
        retry.write("ok")

    tx.rollback()


def test_open_state_delete_does_not_materialize_wal_before_prepare(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    alpha_path = layout.committed_path("alpha")
    alpha_path.parent.mkdir(parents=True, exist_ok=True)
    alpha_path.write_bytes(b"old")

    tx = backend.begin(str(tmp_path))
    tx.delete("alpha")

    assert layout.tx_root.exists()
    assert not layout.wal_path.exists()
    assert tx.core.read_visible_ref("alpha") is None
    tx.rollback()


def test_writable_handle_close_keeps_wal_absent_before_prepare(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    tx = backend.begin(str(tmp_path))
    with cast(TextFileHandle, tx.open_handle("alpha", "w")) as handle:
        handle.write("hello")

    assert layout.tx_root.exists()
    assert not layout.wal_path.exists()
    tx.rollback()


def test_wal_append_failure_during_writable_close_leaves_overlay_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FilesystemBackend()
    tx = backend.begin(str(tmp_path))
    tx.register_child_repo("children/alice")

    def fail_append(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("append failed")

    handle = cast(TextFileHandle, tx.open_handle("alpha", "w"))
    handle.write("hello")
    with monkeypatch.context() as m:
        m.setattr(fs_backend_module, "append_wal_record", fail_append)
        with pytest.raises(RuntimeError, match="append failed"):
            handle.close()

    assert "alpha" not in tx.core.overlay
    assert not tx.has_open_writers
    tx.rollback()


def test_wal_append_failure_during_delete_leaves_overlay_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FilesystemBackend()
    tx = backend.begin(str(tmp_path))
    tx.register_child_repo("children/alice")

    def fail_append(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("append failed")

    with monkeypatch.context() as m:
        m.setattr(fs_backend_module, "append_wal_record", fail_append)
        with pytest.raises(RuntimeError, match="append failed"):
            tx.delete("alpha")

    assert "alpha" not in tx.core.overlay
    tx.rollback()


def test_wal_append_failure_during_child_progress_leaves_memory_state_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FilesystemBackend()
    tx = backend.begin(str(tmp_path))
    tx.register_child_repo("children/alice")

    def fail_append(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("append failed")

    with monkeypatch.context() as m:
        m.setattr(fs_backend_module, "append_wal_record", fail_append)
        with pytest.raises(RuntimeError, match="append failed"):
            tx.mark_child_prepared("children/alice")

    assert tx._children["children/alice"] == {"prepared": False, "committed": False}

    tx.mark_child_prepared("children/alice")
    with monkeypatch.context() as m:
        m.setattr(fs_backend_module, "append_wal_record", fail_append)
        with pytest.raises(RuntimeError, match="append failed"):
            tx.mark_child_committed("children/alice")

    assert tx._children["children/alice"] == {"prepared": True, "committed": False}
    tx.rollback()


def test_prepare_writes_jsonl_wal_records(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    tx = backend.begin(str(tmp_path))
    with cast(TextFileHandle, tx.open_handle("alpha", "w")) as handle:
        handle.write("hello")
    tx.prepare()

    records = _read_wal_records(layout)
    assert records[-1] == {"kind": "state", "state": "prepared"}
    assert any(record == {"kind": "delete", "key": "alpha"} for record in records) is False
    assert any(record.get("kind") == "put" and record.get("key") == "alpha" for record in records)
    tx.rollback()


def test_invalid_delete_after_prepare_does_not_downgrade_prepared_wal(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)

    tx = backend.begin(str(tmp_path))
    _write_bytes(tx, "alpha", b"one")
    tx.prepare()

    assert _read_wal_records(layout)[-1] == {"kind": "state", "state": "prepared"}

    with pytest.raises(TransactionStateError, match="requires state 'open'"):
        tx.delete("alpha")

    assert _read_wal_records(layout)[-1] == {"kind": "state", "state": "prepared"}
    tx.rollback()


def test_recover_respects_writer_lock_before_cleaning_stale_tx_root(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    layout.ensure_repo_root()
    layout.ensure_tx_root()
    lock = WriterLock(layout.lock_path)
    lock.acquire()

    try:
        with pytest.raises(WriteConflictError, match="already owns repo lock"):
            backend.recover(str(tmp_path))
    finally:
        lock.release()

    assert layout.tx_root.exists()


def test_recover_discards_stale_tx_directory_without_wal(tmp_path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    layout.ensure_tx_root()
    (layout.tx_root / "staged").mkdir()

    backend.recover(str(tmp_path))

    assert not layout.tx_root.exists()


def test_recover_discards_open_transaction_state(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    base_path = layout.committed_path("alpha")
    base_path.parent.mkdir(parents=True, exist_ok=True)
    base_path.write_bytes(b"old")

    tx = backend.begin(str(tmp_path))
    _write_bytes(tx, "alpha", b"new")
    tx.lock.release()

    backend.recover(str(tmp_path))

    assert layout.committed_path("alpha").read_bytes() == b"old"
    assert not layout.tx_root.exists()


def test_recover_replays_prepared_wal_to_full_commit(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    beta_path = layout.committed_path("beta")
    beta_path.parent.mkdir(parents=True, exist_ok=True)
    beta_path.write_bytes(b"old")

    tx = backend.begin(str(tmp_path))
    _write_bytes(tx, "alpha", b"one")
    tx.delete("beta")
    tx.prepare()
    tx.lock.release()

    backend.recover(str(tmp_path))

    assert layout.committed_path("alpha").read_bytes() == b"one"
    assert not layout.committed_path("beta").exists()
    assert not layout.tx_root.exists()


def test_recover_completes_partial_publication_and_is_idempotent(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    gamma_path = layout.committed_path("gamma")
    gamma_path.parent.mkdir(parents=True, exist_ok=True)
    gamma_path.write_bytes(b"old")

    tx = backend.begin(str(tmp_path))
    _write_bytes(tx, "alpha", b"one")
    _write_bytes(tx, "beta", b"two")
    tx.delete("gamma")
    tx.prepare()
    layout.committed_path("alpha").parent.mkdir(parents=True, exist_ok=True)
    layout.committed_path("alpha").write_bytes(b"one")
    tx.lock.release()

    backend.recover(str(tmp_path))
    backend.recover(str(tmp_path))

    assert layout.committed_path("alpha").read_bytes() == b"one"
    assert layout.committed_path("beta").read_bytes() == b"two"
    assert not layout.committed_path("gamma").exists()
    assert not layout.tx_root.exists()


def test_commit_supports_subtree_to_file_replacement(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    nested = layout.committed_path("a/b")
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_bytes(b"old")

    tx = backend.begin(str(tmp_path))
    tx.delete("a/b")
    _write_bytes(tx, "a", b"root")
    tx.prepare()
    tx.commit()

    assert layout.committed_path("a").read_bytes() == b"root"
    assert not layout.committed_path("a/b").exists()


def test_recover_supports_subtree_to_file_replacement_after_partial_publication(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    nested = layout.committed_path("a/b")
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_bytes(b"old")

    tx = backend.begin(str(tmp_path))
    tx.delete("a/b")
    _write_bytes(tx, "a", b"root")
    tx.prepare()
    nested.unlink()
    tx.lock.release()

    backend.recover(str(tmp_path))
    backend.recover(str(tmp_path))

    assert layout.committed_path("a").read_bytes() == b"root"
    assert not layout.committed_path("a/b").exists()


def test_commit_supports_file_to_subtree_replacement(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    root_file = layout.committed_path("a")
    root_file.parent.mkdir(parents=True, exist_ok=True)
    root_file.write_bytes(b"old")

    tx = backend.begin(str(tmp_path))
    tx.delete("a")
    _write_bytes(tx, "a/b", b"child")
    tx.prepare()
    tx.commit()

    assert layout.committed_path("a/b").read_bytes() == b"child"
    assert not layout.committed_path("a").is_file()


def test_recover_supports_file_to_subtree_replacement_after_partial_publication(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    root_file = layout.committed_path("a")
    root_file.parent.mkdir(parents=True, exist_ok=True)
    root_file.write_bytes(b"old")

    tx = backend.begin(str(tmp_path))
    tx.delete("a")
    _write_bytes(tx, "a/b", b"child")
    tx.prepare()
    root_file.unlink()
    tx.lock.release()

    backend.recover(str(tmp_path))
    backend.recover(str(tmp_path))

    assert layout.committed_path("a/b").read_bytes() == b"child"
    assert not layout.committed_path("a").is_file()


def test_recover_rejects_staged_path_escaping_tx_root(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    layout.ensure_tx_root()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    layout.wal_path.write_text(
        '\n'.join(
            [
                '{"kind":"state","state":"prepared"}',
                '{"kind":"put","key":"alpha","staged_relpath":"../outside.bin"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TransactionStateError, match="Invalid staged payload path"):
        backend.recover(str(tmp_path))


def test_recover_rejects_invalid_logical_key_before_any_publication(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    layout.ensure_tx_root()
    staged_path = layout.staged_root / "alpha"
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b"good")
    layout.wal_path.write_text(
        '\n'.join(
            [
                '{"kind":"state","state":"prepared"}',
                '{"kind":"put","key":"alpha","staged_relpath":"staged/alpha"}',
                '{"kind":"delete","key":"../bad"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="logical key|relative path|must not contain"):
        backend.recover(str(tmp_path))

    assert not layout.committed_path("alpha").exists()


def test_recover_rejects_malformed_jsonl_record(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    layout.ensure_tx_root()
    layout.wal_path.write_text('{"kind":"state","state":"prepared"}\n{"kind"', encoding="utf-8")

    with pytest.raises(TransactionStateError, match="Invalid filesystem WAL JSON on line 2"):
        backend.recover(str(tmp_path))


def test_recover_rejects_malformed_child_boolean_fields(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    layout = RepoLayout(tmp_path)
    layout.ensure_tx_root()
    layout.wal_path.write_text(
        '{"kind":"child","repo_path":"users/alice","prepared":"false","committed":false}\n',
        encoding="utf-8",
    )

    with pytest.raises(TransactionStateError, match="boolean prepared/committed"):
        backend.recover(str(tmp_path))


def test_pending_ancestor_child_enrollment_blocks_independent_child_begin(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    root_tx = backend.begin(root_locator)
    root_tx.register_child_repo("users/alice")
    root_tx.lock.release()
    root_tx._released = True

    with pytest.raises(TransactionStateError, match="pending child coordination"):
        backend.begin(child_locator)

    backend.recover(root_locator)
    assert not RepoLayout(root_locator).tx_root.exists()


def test_recover_open_parent_with_unstarted_enrolled_child_treats_child_as_noop(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    root_tx = backend.begin(root_locator)
    root_tx.register_child_repo("users/alice")
    root_tx.lock.release()
    root_tx._released = True

    backend.recover(root_locator)

    assert not RepoLayout(root_locator).tx_root.exists()
    assert not RepoLayout(child_locator).tx_root.exists()


def test_recover_prepared_parent_with_unstarted_unprepared_child_treats_child_as_noop(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    root_tx = backend.begin(root_locator)
    root_tx.register_child_repo("users/alice")
    _write_bytes(root_tx, "config", b"root")
    root_tx.prepare()
    root_tx.lock.release()
    root_tx._released = True

    backend.recover(root_locator)

    assert (Path(root_locator) / "config").read_bytes() == b"root"
    assert not RepoLayout(root_locator).tx_root.exists()
    assert not RepoLayout(child_locator).tx_root.exists()


def test_recover_prepared_parent_with_missing_prepared_child_state_treats_child_as_already_finalized(
    tmp_path: Path,
) -> None:
    backend = FilesystemBackend()
    root_locator = str(tmp_path / "repo")
    root_tx = backend.begin(root_locator)
    root_tx.register_child_repo("users/alice")
    root_tx.mark_child_prepared("users/alice")
    _write_bytes(root_tx, "config", b"root")
    root_tx.prepare()
    root_tx.lock.release()
    root_tx._released = True

    backend.recover(root_locator)

    assert (Path(root_locator) / "config").read_bytes() == b"root"
    assert not RepoLayout(root_locator).tx_root.exists()


def test_failed_child_marker_setup_unenrolls_parent_wal_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FilesystemBackend()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    root_tx = backend.begin(root_locator)
    original_append_wal_record = fs_backend_module.append_wal_record

    def fail_child_marker(layout: RepoLayout, record: dict[str, object]) -> None:
        if record.get("kind") == "coordinated_child":
            raise RuntimeError("child marker failed")
        original_append_wal_record(layout, record)

    with monkeypatch.context() as m:
        m.setattr(fs_backend_module, "append_wal_record", fail_child_marker)
        with pytest.raises(RuntimeError, match="child marker failed"):
            backend.begin(child_locator, parent_tx=root_tx)

    root_layout = RepoLayout(root_locator)
    if root_layout.wal_path.exists():
        _state, _overlay, children = load_wal(root_layout)
        assert children == {}
    assert not RepoLayout(child_locator).tx_root.exists()
    root_tx.rollback()


def test_failed_child_marker_setup_still_rolls_back_child_when_parent_unenroll_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FilesystemBackend()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    root_tx = backend.begin(root_locator)
    original_append_wal_record = fs_backend_module.append_wal_record

    def fail_child_marker_and_unenroll(layout: RepoLayout, record: dict[str, object]) -> None:
        if record.get("kind") in {"coordinated_child", "child_unenrolled"}:
            raise RuntimeError(f"{record['kind']} failed")
        original_append_wal_record(layout, record)

    with monkeypatch.context() as m:
        m.setattr(fs_backend_module, "append_wal_record", fail_child_marker_and_unenroll)
        with pytest.raises(RuntimeError, match="coordinated_child failed") as exc_info:
            backend.begin(child_locator, parent_tx=root_tx)

    assert not RepoLayout(child_locator).tx_root.exists()
    assert any("child_unenrolled failed" in note for note in getattr(exc_info.value, "__notes__", []))
    root_tx.rollback()


def test_parent_transaction_cannot_write_or_delete_inside_enrolled_child_repo_boundary(tmp_path: Path) -> None:
    backend = FilesystemBackend()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    root_tx = backend.begin(root_locator)
    child_tx = backend.begin(child_locator, parent_tx=root_tx)

    with cast(TextFileHandle, child_tx.open_handle("value", "w")) as handle:
        handle.write("alice")

    with pytest.raises(TransactionStateError, match="overlaps enrolled child repo"):
        root_tx.open_handle("users/alice/value", "w")

    with pytest.raises(TransactionStateError, match="overlaps enrolled child repo"):
        root_tx.delete("users")

    child_tx.rollback()
    root_tx.rollback()
