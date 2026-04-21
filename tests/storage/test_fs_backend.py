from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from textwrap import dedent
from typing import cast

import pytest

import alpenstock.storage.backends.fs.journal as fs_journal_module
from alpenstock.storage import Repo, TransactionStateError, ValueContractError, WriteConflictError
from alpenstock.storage._handles import BinaryFileHandle, TextFileHandle
from alpenstock.storage._types import DELETE, Put
from alpenstock.storage.backends.fs import RepoLayout, WriterLock
from tests.storage._fs_test_utils import FsRuntime, assert_no_transaction_artifacts, init_repo_metadata
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


def _read_meta(layout: RepoLayout) -> dict[str, object]:
    return cast(dict[str, object], json.loads(layout.meta_path.read_text(encoding="utf-8")))


def _run_python_processes(scripts: Sequence[str], start_file: Path) -> None:
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for script in scripts
    ]
    start_file.write_text("go", encoding="utf-8")
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, f"rc={proc.returncode} stdout={stdout!r} stderr={stderr!r}"


def _wait_for_file(path: Path, proc: subprocess.Popen[str]) -> None:
    for _ in range(500):
        if path.exists():
            return
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            raise AssertionError(f"holder exited early rc={proc.returncode} stdout={stdout!r} stderr={stderr!r}")
        time.sleep(0.01)
    proc.kill()
    stdout, stderr = proc.communicate()
    raise AssertionError(f"timed out waiting for {path}; stdout={stdout!r} stderr={stderr!r}")


def _start_holding_transaction(repo_locator: str, ready_file: Path, release_file: Path) -> subprocess.Popen[str]:
    script = dedent(
        f"""
        import time
        from pathlib import Path
        from alpenstock.storage.backends.fs import FilesystemBlobBackend, JsonlWalJournalBackend

        blob = FilesystemBlobBackend()
        journal = JsonlWalJournalBackend()
        tx = journal.begin({repo_locator!r}, blob)
        Path({str(ready_file)!r}).write_text("ready", encoding="utf-8")
        try:
            while not Path({str(release_file)!r}).exists():
                time.sleep(0.01)
        finally:
            tx.rollback()
        """
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_read_only_access_does_not_create_transaction_state(tmp_path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
    layout.committed_path("alpha").parent.mkdir(parents=True, exist_ok=True)
    layout.committed_path("alpha").write_text("hello", encoding="utf-8")
    repo = Repo.open(str(tmp_path), blob_backend=backend.blob, journal_backend=backend.journal)

    assert repo.file("alpha").read_text() == "hello"
    assert_no_transaction_artifacts(tmp_path)


def test_committed_backend_handles_reject_write_capable_modes(tmp_path: Path) -> None:
    backend = FsRuntime()

    with pytest.raises(TransactionStateError, match="Committed views are read-only"):
        backend.open_committed_handle(str(tmp_path), "alpha", "w")

    with pytest.raises(TransactionStateError, match="Committed views are read-only"):
        backend.open_committed_handle(str(tmp_path), "alpha", "rb+")


def test_transaction_directory_materializes_lazily_on_first_writable_open(tmp_path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    repo = Repo.open(str(tmp_path), blob_backend=backend.blob, journal_backend=backend.journal)

    tx = repo.transaction()
    tx.__enter__()
    assert layout.tx_root.exists()
    assert layout.fence_path.exists()
    assert not layout.wal_path.exists()

    handle = tx.open("alpha", "w")
    assert layout.tx_root.exists()
    assert not layout.wal_path.exists()
    handle.write("hello")
    handle.close()
    assert not layout.wal_path.exists()

    tx.rollback()
    tx.__exit__(None, None, None)

    assert_no_transaction_artifacts(tmp_path)


def test_second_writer_is_rejected_while_first_transaction_holds_lock(tmp_path) -> None:
    backend = FsRuntime()
    first = backend.begin(str(tmp_path))

    with pytest.raises(WriteConflictError, match="already owns repo lock"):
        backend.begin(str(tmp_path))

    first.rollback()


def test_repo_open_empty_path_creates_root_tree_metadata(tmp_path: Path) -> None:
    backend = FsRuntime()
    repo_root = tmp_path / "repo"

    Repo.open(str(repo_root), blob_backend=backend.blob, journal_backend=backend.journal)

    meta = _read_meta(RepoLayout(repo_root))
    assert meta["version"] == 1
    assert meta["journal_backend"] == "jsonl-wal"
    assert meta["repo_path"] == "."
    assert meta["parent_repo_path"] is None
    assert meta["tree_root_relpath"] == "."
    assert isinstance(meta["tree_id"], str) and meta["tree_id"]


def test_repo_open_nonempty_path_without_metadata_is_refused(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "alpha").parent.mkdir(parents=True, exist_ok=True)
    (repo_root / "alpha").write_bytes(b"legacy")

    with pytest.raises(TransactionStateError, match="missing \\.tx/meta\\.json"):
        Repo.open(str(repo_root))


def test_mismatched_tree_metadata_is_refused(tmp_path: Path) -> None:
    backend = FsRuntime()
    repo_root = tmp_path / "repo"
    Repo.open(str(repo_root), blob_backend=backend.blob, journal_backend=backend.journal)
    layout = RepoLayout(repo_root)
    meta = _read_meta(layout)
    meta["repo_path"] = "wrong"
    layout.meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(TransactionStateError, match="maps repo path"):
        Repo.open(str(repo_root), blob_backend=backend.blob, journal_backend=backend.journal)


def test_concurrent_root_metadata_initialization_produces_one_valid_metadata(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    start_file = tmp_path / "start"
    script = dedent(
        f"""
        import time
        from pathlib import Path
        from alpenstock.storage import Repo

        start = Path({str(start_file)!r})
        while not start.exists():
            time.sleep(0.01)
        Repo.open({str(repo_root)!r})
        """
    )

    _run_python_processes([script for _ in range(8)], start_file)

    layout = RepoLayout(repo_root)
    meta = _read_meta(layout)
    assert meta["repo_path"] == "."
    assert meta["parent_repo_path"] is None
    assert meta["tree_root_relpath"] == "."
    assert layout.meta_lock_path.exists()
    assert not list(layout.tx_root.glob("meta.json.*.tmp"))


def test_metadata_initializer_rechecks_existing_metadata_after_meta_lock(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    layout = RepoLayout(repo_root)
    layout.ensure_repo_root()
    layout.ensure_tx_root()
    lock = WriterLock(layout.meta_lock_path, blocking=True)
    lock.acquire()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            dedent(
                f"""
                from alpenstock.storage import Repo

                Repo.open({str(repo_root)!r})
                """
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        layout.meta_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "journal_backend": "jsonl-wal",
                    "tree_id": "winner",
                    "tree_root_relpath": ".",
                    "repo_path": ".",
                    "parent_repo_path": None,
                }
            ),
            encoding="utf-8",
        )
    finally:
        lock.release()
    stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 0, f"rc={proc.returncode} stdout={stdout!r} stderr={stderr!r}"

    assert _read_meta(layout)["tree_id"] == "winner"


def test_concurrent_child_metadata_materialization_produces_one_valid_metadata(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    backend.prepare_repo_open(root_locator, backend.blob)
    start_file = tmp_path / "start-child"
    script = dedent(
        f"""
        import time
        from pathlib import Path
        from alpenstock.storage.backends.fs import FilesystemBlobBackend, JsonlWalJournalBackend

        start = Path({str(start_file)!r})
        while not start.exists():
            time.sleep(0.01)
        blob = FilesystemBlobBackend()
        journal = JsonlWalJournalBackend()
        journal.prepare_repo_open(
            {child_locator!r},
            blob,
            parent_repo_locator={root_locator!r},
            child_repo_path="users/alice",
        )
        """
    )

    _run_python_processes([script for _ in range(8)], start_file)

    root_meta = _read_meta(RepoLayout(root_locator))
    child_layout = RepoLayout(child_locator)
    child_meta = _read_meta(child_layout)
    assert child_meta["tree_id"] == root_meta["tree_id"]
    assert child_meta["repo_path"] == "users/alice"
    assert child_meta["parent_repo_path"] == "."
    assert child_layout.meta_lock_path.exists()
    assert not list(child_layout.tx_root.glob("meta.json.*.tmp"))


def test_stale_unique_metadata_temp_is_removed_by_transaction_cleanup(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
    stale_tmp = layout.tx_root / "meta.json.1234.deadbeef.tmp"
    stale_tmp.write_text("{", encoding="utf-8")

    layout.cleanup_transaction_artifacts()

    assert layout.meta_path.exists()
    assert layout.meta_lock_path.exists()
    assert not stale_tmp.exists()


def test_direct_child_and_sibling_transactions_can_overlap_with_intention_locks(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    b_locator = backend.child_repo_locator(root_locator, "b")
    c_locator = backend.child_repo_locator(b_locator, "c")
    d_locator = backend.child_repo_locator(root_locator, "d")
    backend.prepare_repo_open(root_locator, backend.blob)
    backend.prepare_repo_open(b_locator, backend.blob, parent_repo_locator=root_locator, child_repo_path="b")
    backend.prepare_repo_open(c_locator, backend.blob, parent_repo_locator=b_locator, child_repo_path="c")
    backend.prepare_repo_open(d_locator, backend.blob, parent_repo_locator=root_locator, child_repo_path="d")

    c_tx = backend.begin(c_locator)
    d_tx = backend.begin(d_locator)

    d_tx.rollback()
    c_tx.rollback()


def test_direct_child_intention_locks_block_wider_ancestor_writers(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    b_locator = backend.child_repo_locator(root_locator, "b")
    c_locator = backend.child_repo_locator(b_locator, "c")
    backend.prepare_repo_open(root_locator, backend.blob)
    backend.prepare_repo_open(b_locator, backend.blob, parent_repo_locator=root_locator, child_repo_path="b")
    backend.prepare_repo_open(c_locator, backend.blob, parent_repo_locator=b_locator, child_repo_path="c")

    c_tx = backend.begin(c_locator)
    try:
        with pytest.raises(WriteConflictError, match="already owns repo lock"):
            backend.begin(b_locator)
        with pytest.raises(WriteConflictError, match="already owns repo lock"):
            backend.begin(root_locator)
    finally:
        c_tx.rollback()


def test_root_exclusive_lock_blocks_direct_child_begin(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    backend.prepare_repo_open(root_locator, backend.blob)
    backend.prepare_repo_open(
        child_locator,
        backend.blob,
        parent_repo_locator=root_locator,
        child_repo_path="users/alice",
    )

    root_tx = backend.begin(root_locator)
    try:
        with pytest.raises(WriteConflictError, match="already owns repo lock"):
            backend.begin(child_locator)
    finally:
        root_tx.rollback()


def test_cross_process_root_exclusive_lock_blocks_direct_child_begin(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    backend.prepare_repo_open(root_locator, backend.blob)
    backend.prepare_repo_open(
        child_locator,
        backend.blob,
        parent_repo_locator=root_locator,
        child_repo_path="users/alice",
    )
    ready_file = tmp_path / "holder-ready"
    release_file = tmp_path / "holder-release"
    proc = _start_holding_transaction(root_locator, ready_file, release_file)
    try:
        _wait_for_file(ready_file, proc)
        with pytest.raises(WriteConflictError, match="already owns repo lock"):
            backend.begin(child_locator)
    finally:
        release_file.write_text("release", encoding="utf-8")
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, f"rc={proc.returncode} stdout={stdout!r} stderr={stderr!r}"


def test_cross_process_direct_child_intention_locks_block_wider_ancestor_writer(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    b_locator = backend.child_repo_locator(root_locator, "b")
    c_locator = backend.child_repo_locator(b_locator, "c")
    backend.prepare_repo_open(root_locator, backend.blob)
    backend.prepare_repo_open(b_locator, backend.blob, parent_repo_locator=root_locator, child_repo_path="b")
    backend.prepare_repo_open(c_locator, backend.blob, parent_repo_locator=b_locator, child_repo_path="c")
    ready_file = tmp_path / "holder-ready"
    release_file = tmp_path / "holder-release"
    proc = _start_holding_transaction(c_locator, ready_file, release_file)
    try:
        _wait_for_file(ready_file, proc)
        with pytest.raises(WriteConflictError, match="already owns repo lock"):
            backend.begin(root_locator)
    finally:
        release_file.write_text("release", encoding="utf-8")
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, f"rc={proc.returncode} stdout={stdout!r} stderr={stderr!r}"


def test_cross_process_sibling_direct_child_transactions_can_overlap(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    b_locator = backend.child_repo_locator(root_locator, "b")
    c_locator = backend.child_repo_locator(b_locator, "c")
    d_locator = backend.child_repo_locator(root_locator, "d")
    backend.prepare_repo_open(root_locator, backend.blob)
    backend.prepare_repo_open(b_locator, backend.blob, parent_repo_locator=root_locator, child_repo_path="b")
    backend.prepare_repo_open(c_locator, backend.blob, parent_repo_locator=b_locator, child_repo_path="c")
    backend.prepare_repo_open(d_locator, backend.blob, parent_repo_locator=root_locator, child_repo_path="d")
    ready_file = tmp_path / "holder-ready"
    release_file = tmp_path / "holder-release"
    proc = _start_holding_transaction(c_locator, ready_file, release_file)
    d_tx = None
    try:
        _wait_for_file(ready_file, proc)
        d_tx = backend.begin(d_locator)
    finally:
        if d_tx is not None:
            d_tx.rollback()
        release_file.write_text("release", encoding="utf-8")
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, f"rc={proc.returncode} stdout={stdout!r} stderr={stderr!r}"


def test_failed_cross_process_lock_acquisition_releases_previous_ancestor_locks(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    b_locator = backend.child_repo_locator(root_locator, "b")
    c_locator = backend.child_repo_locator(b_locator, "c")
    d_locator = backend.child_repo_locator(root_locator, "d")
    backend.prepare_repo_open(root_locator, backend.blob)
    backend.prepare_repo_open(b_locator, backend.blob, parent_repo_locator=root_locator, child_repo_path="b")
    backend.prepare_repo_open(c_locator, backend.blob, parent_repo_locator=b_locator, child_repo_path="c")
    backend.prepare_repo_open(d_locator, backend.blob, parent_repo_locator=root_locator, child_repo_path="d")
    ready_file = tmp_path / "holder-ready"
    release_file = tmp_path / "holder-release"
    proc = _start_holding_transaction(b_locator, ready_file, release_file)
    d_tx = None
    try:
        _wait_for_file(ready_file, proc)
        with pytest.raises(WriteConflictError, match="already owns repo lock"):
            backend.begin(c_locator)
        d_tx = backend.begin(d_locator)
    finally:
        if d_tx is not None:
            d_tx.rollback()
        release_file.write_text("release", encoding="utf-8")
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, f"rc={proc.returncode} stdout={stdout!r} stderr={stderr!r}"


def test_hierarchical_root_lock_closes_begin_race_before_root_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    backend.prepare_repo_open(root_locator, backend.blob)
    backend.prepare_repo_open(
        child_locator,
        backend.blob,
        parent_repo_locator=root_locator,
        child_repo_path="users/alice",
    )
    attempted_child_begin = False
    original_check = fs_journal_module.JsonlWalJournalBackend._assert_independent_begin_is_safe

    def checked_with_racing_child(self, layout, blob_backend) -> None:
        nonlocal attempted_child_begin
        original_check(self, layout, blob_backend)
        if layout.repo_root == Path(root_locator) and not attempted_child_begin:
            attempted_child_begin = True
            with pytest.raises(WriteConflictError, match="already owns repo lock"):
                backend.begin(child_locator, coordination_root_locator=root_locator)

    with monkeypatch.context() as m:
        m.setattr(
            fs_journal_module.JsonlWalJournalBackend,
            "_assert_independent_begin_is_safe",
            checked_with_racing_child,
        )
        root_tx = backend.begin(root_locator, coordination_root_locator=root_locator)

    assert attempted_child_begin is True
    assert RepoLayout(root_locator).fence_path.exists()
    root_tx.rollback()


def test_begin_rejects_pending_unrecovered_transaction_state(tmp_path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
    layout.ensure_tx_root()
    layout.wal_path.write_text('{"kind":"state","state":"open"}\n', encoding="utf-8")

    with pytest.raises(TransactionStateError, match="pending recovery state exists; run recover\\(\\) first"):
        backend.begin(str(tmp_path))


def test_failed_begin_releases_locks_and_preserves_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)

    def fail_safety_check(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("safety check failed")

    with monkeypatch.context() as m:
        m.setattr(
            fs_journal_module.JsonlWalJournalBackend,
            "_assert_independent_begin_is_safe",
            fail_safety_check,
        )
        with pytest.raises(RuntimeError, match="safety check failed") as exc_info:
            backend.begin(str(tmp_path))
        held_error = exc_info.value

    assert_no_transaction_artifacts(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            dedent(
                f"""
                from alpenstock.storage.backends.fs import FilesystemBlobBackend, JsonlWalJournalBackend

                blob = FilesystemBlobBackend()
                journal = JsonlWalJournalBackend()
                tx = journal.begin({str(tmp_path)!r}, blob)
                tx.rollback()
                print("lock released")
                """
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert proc.stdout.strip() == "lock released"
    assert held_error is not None


def test_fence_tmp_is_pending_state_and_blocks_begin(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
    layout.ensure_tx_root()
    layout.fence_tmp_path.write_text('{"partial": true}\n', encoding="utf-8")

    with pytest.raises(TransactionStateError, match="pending recovery state exists"):
        backend.begin(str(tmp_path))


def test_recover_cleans_lone_fence_tmp_artifact(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
    layout.ensure_tx_root()
    layout.fence_tmp_path.write_text('{"partial": true}\n', encoding="utf-8")

    backend.recover(str(tmp_path))

    assert_no_transaction_artifacts(tmp_path)


def test_recover_cleans_fence_tmp_artifacts_and_preserves_metadata(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
    layout.ensure_tx_root()
    layout.fence_tmp_path.write_text('{"partial": true}\n', encoding="utf-8")

    backend.recover(str(tmp_path))

    assert_no_transaction_artifacts(tmp_path)


def test_empty_prepare_keeps_only_fence_metadata_until_commit(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)

    tx = backend.begin(str(tmp_path))
    tx.prepare()

    assert layout.tx_root.exists()
    assert layout.fence_path.exists()
    assert not layout.wal_path.exists()

    tx.commit()


def test_delete_rejects_invalid_logical_key_before_writing_transaction_state(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    tx = backend.begin(str(tmp_path))

    with pytest.raises(ValueContractError, match="must not contain|relative path"):
        tx.delete("../bad")

    assert layout.fence_path.exists()
    tx.rollback()


@pytest.mark.parametrize("key", [".tx/wal.jsonl", ".tx/lock", ".tx/fence.json"])
def test_internal_backend_paths_are_reserved_from_user_keys(tmp_path: Path, key: str) -> None:
    backend = FsRuntime()
    tx = backend.begin(str(tmp_path))

    with pytest.raises(ValueContractError, match="internal paths"):
        tx.open_handle(key, "wb")

    tx.rollback()


def test_reader_opened_while_writer_is_open_reads_live_staged_copy(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
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
    backend = FsRuntime()

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
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
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
    assert staged_relpath == "staged/alpha"
    staged_path = layout.tx_root / staged_relpath
    assert staged_path.read_bytes() == b"one"

    tx.rollback()


def test_prepare_does_not_write_prepared_wal_when_blob_prepare_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    tx = backend.begin(str(tmp_path))
    _write_bytes(tx, "alpha", b"one")

    def fail_ensure_prepared(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("blob prepare failed")

    with monkeypatch.context() as m:
        m.setattr(type(backend.blob), "ensure_prepared", fail_ensure_prepared)
        with pytest.raises(RuntimeError, match="blob prepare failed"):
            tx.prepare()

    assert not layout.wal_path.exists()
    tx.rollback()


def test_staged_payload_path_is_key_based_without_session_directory(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    tx = backend.begin(str(tmp_path))

    _write_bytes(tx, "nested/alpha", b"one")
    tx.prepare()

    _state, overlay, _children = load_wal(layout)
    entry = cast(Put[object], overlay["nested/alpha"])

    assert entry.value.relpath == "staged/nested%2Falpha"  # type: ignore[attr-defined]
    assert (layout.tx_root / "staged" / "nested%2Falpha").read_bytes() == b"one"

    tx.rollback()


def test_wal_replay_truncates_torn_final_record(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    layout.ensure_tx_root()
    layout.wal_path.write_bytes(
        b'{"kind":"state","state":"prepared"}\n{"kind":"state","state":"comm'
    )

    state, overlay, children = load_wal(layout)

    assert state == "prepared"
    assert overlay == {}
    assert children == {}
    assert layout.wal_path.read_bytes() == b'{"kind":"state","state":"prepared"}\n'


def test_wal_replay_rejects_malformed_complete_record(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    layout.ensure_tx_root()
    layout.wal_path.write_bytes(b'{"kind":"state","state":"prepared"}\n{"kind":\n')

    with pytest.raises(TransactionStateError, match="Invalid filesystem WAL JSON"):
        load_wal(layout)


def test_commit_validates_all_staged_puts_before_mutating_committed_state(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
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

    with pytest.raises(TransactionStateError, match="publication has started"):
        tx.rollback()
    tx.detach_for_recovery()


def test_prepare_rejects_conflicting_ancestor_and_descendant_puts_before_prepared_wal(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    tx = backend.begin(str(tmp_path))

    with cast(BinaryFileHandle, tx.open_handle("a", "wb")) as handle:
        handle.write(b"root")
    with cast(BinaryFileHandle, tx.open_handle("a/b", "wb")) as handle:
        handle.write(b"child")

    with pytest.raises(TransactionStateError, match="conflicting put keys"):
        tx.prepare()

    assert not layout.committed_path("a").exists()
    tx.rollback()


def test_open_failure_does_not_leave_hidden_writer_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FsRuntime()
    tx = backend.begin(str(tmp_path))

    def fail_initialize(self, *args, **kwargs) -> None:
        del self, args, kwargs
        raise RuntimeError("initialize failed")

    with monkeypatch.context() as m:
        m.setattr(type(tx.blob), "initialize_work_path", fail_initialize)
        with pytest.raises(RuntimeError, match="initialize failed"):
            tx.open_handle("alpha", "w")

    assert not tx.has_open_writers
    assert tx.core.read_visible_ref("alpha") is None

    with cast(TextFileHandle, tx.open_handle("alpha", "w")) as retry:
        retry.write("ok")

    tx.rollback()


def test_open_state_delete_does_not_materialize_wal_before_prepare(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
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
    backend = FsRuntime()
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
    backend = FsRuntime()
    tx = backend.begin(str(tmp_path))
    tx.register_child_repo("children/alice")

    def fail_append(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("append failed")

    handle = cast(TextFileHandle, tx.open_handle("alpha", "w"))
    handle.write("hello")
    with monkeypatch.context() as m:
        m.setattr(fs_journal_module, "append_wal_record", fail_append)
        with pytest.raises(RuntimeError, match="append failed"):
            handle.close()

    assert "alpha" not in tx.core.overlay
    assert not tx.has_open_writers
    tx.rollback()


def test_wal_append_failure_during_delete_leaves_overlay_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FsRuntime()
    tx = backend.begin(str(tmp_path))
    tx.register_child_repo("children/alice")

    def fail_append(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("append failed")

    with monkeypatch.context() as m:
        m.setattr(fs_journal_module, "append_wal_record", fail_append)
        with pytest.raises(RuntimeError, match="append failed"):
            tx.delete("alpha")

    assert "alpha" not in tx.core.overlay
    tx.rollback()


def test_wal_append_failure_during_child_progress_leaves_memory_state_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FsRuntime()
    tx = backend.begin(str(tmp_path))
    tx.register_child_repo("children/alice")

    def fail_append(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("append failed")

    with monkeypatch.context() as m:
        m.setattr(fs_journal_module, "append_wal_record", fail_append)
        with pytest.raises(RuntimeError, match="append failed"):
            tx.mark_child_prepared("children/alice")

    assert tx._children["children/alice"] == {"prepared": False}

    tx.rollback()


def test_prepare_writes_jsonl_wal_records(tmp_path: Path) -> None:
    backend = FsRuntime()
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
    backend = FsRuntime()
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
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
    layout.ensure_repo_root()
    layout.ensure_tx_root()
    layout.wal_path.write_text('{"kind":"state","state":"open"}\n', encoding="utf-8")
    lock = WriterLock(layout.lock_path)
    lock.acquire()

    try:
        with pytest.raises(WriteConflictError, match="already owns repo lock"):
            backend.recover(str(tmp_path))
    finally:
        lock.release()

    assert layout.tx_root.exists()


def test_recover_discards_stale_tx_directory_without_wal(tmp_path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
    layout.ensure_tx_root()
    (layout.tx_root / "staged").mkdir()

    backend.recover(str(tmp_path))

    assert_no_transaction_artifacts(tmp_path)


def test_recover_discards_open_transaction_state(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
    base_path = layout.committed_path("alpha")
    base_path.parent.mkdir(parents=True, exist_ok=True)
    base_path.write_bytes(b"old")

    tx = backend.begin(str(tmp_path))
    _write_bytes(tx, "alpha", b"new")
    tx.lock.release()

    backend.recover(str(tmp_path))

    assert layout.committed_path("alpha").read_bytes() == b"old"
    assert_no_transaction_artifacts(tmp_path)


def test_recover_replays_prepared_wal_to_full_commit(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
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
    assert_no_transaction_artifacts(tmp_path)


def test_public_abort_intent_is_rejected_for_prepared_root(tmp_path: Path) -> None:
    backend = FsRuntime()
    tx = backend.begin(str(tmp_path))
    _write_bytes(tx, "alpha", b"one")
    tx.prepare()
    tx.lock.release()
    tx._released = True

    with pytest.raises(TransactionStateError, match="must complete publication"):
        backend.recover(str(tmp_path), intent="abort")

    backend.recover(str(tmp_path))
    assert (tmp_path / "alpha").read_bytes() == b"one"


def test_public_abort_intent_is_rejected_for_committing_root(tmp_path: Path) -> None:
    backend = FsRuntime()
    tx = backend.begin(str(tmp_path))
    _write_bytes(tx, "alpha", b"one")
    tx.prepare()
    tx.mark_committing()
    tx.lock.release()
    tx._released = True

    with pytest.raises(TransactionStateError, match="must complete publication"):
        backend.recover(str(tmp_path), intent="abort")

    backend.recover(str(tmp_path))
    assert (tmp_path / "alpha").read_bytes() == b"one"


def test_recover_completes_partial_publication_and_is_idempotent(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
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
    assert_no_transaction_artifacts(tmp_path)


def test_read_only_open_refuses_target_repo_with_pending_state(tmp_path: Path) -> None:
    backend = FsRuntime()
    repo = Repo.open(str(tmp_path), blob_backend=backend.blob, journal_backend=backend.journal)
    tx = backend.begin(str(tmp_path))
    _write_bytes(tx, "alpha", b"one")
    tx.prepare()
    tx.lock.release()
    tx._released = True

    with pytest.raises(TransactionStateError, match="pending transaction state"):
        repo.file("alpha").read_bytes()

    backend.recover(str(tmp_path))


def test_read_only_open_refuses_when_ancestor_repo_has_pending_state(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    backend.prepare_repo_open(root_locator, backend.blob)
    backend.prepare_repo_open(
        child_locator,
        backend.blob,
        parent_repo_locator=root_locator,
        child_repo_path="users/alice",
    )
    child_path = Path(child_locator) / "profile"
    child_path.parent.mkdir(parents=True, exist_ok=True)
    child_path.write_bytes(b"old")
    root_tx = backend.begin(root_locator)
    root_tx.lock.release()
    root_tx._released = True
    child_repo = Repo.open(child_locator, blob_backend=backend.blob, journal_backend=backend.journal)

    with pytest.raises(TransactionStateError, match="ancestor repo"):
        child_repo.file("profile").read_bytes()

    backend.recover(root_locator)


def test_read_only_open_refuses_when_descendant_repo_has_pending_state(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    backend.prepare_repo_open(root_locator, backend.blob)
    root_path = Path(root_locator) / "config"
    root_path.parent.mkdir(parents=True, exist_ok=True)
    root_path.write_bytes(b"old")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    backend.prepare_repo_open(
        child_locator,
        backend.blob,
        parent_repo_locator=root_locator,
        child_repo_path="users/alice",
    )
    child_tx = backend.begin(child_locator)
    child_tx.lock.release()
    child_tx._released = True
    root_repo = Repo.open(root_locator, blob_backend=backend.blob, journal_backend=backend.journal)

    with pytest.raises(TransactionStateError, match="descendant repo"):
        root_repo.file("config").read_bytes()

    backend.recover(child_locator)


def test_read_inside_active_transaction_ignores_committed_read_clear_check(tmp_path: Path) -> None:
    backend = FsRuntime()
    repo = Repo.open(str(tmp_path), blob_backend=backend.blob, journal_backend=backend.journal)

    with repo.transaction():
        repo.file("alpha").write_text("one")
        assert repo.file("alpha").read_text() == "one"


def test_commit_supports_subtree_to_file_replacement(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
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
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
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
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
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
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
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
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
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
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
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
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
    layout.ensure_tx_root()
    layout.wal_path.write_text('{"kind":"state","state":"prepared"}\n{"kind"\n', encoding="utf-8")

    with pytest.raises(TransactionStateError, match="Invalid filesystem WAL JSON on line 2"):
        backend.recover(str(tmp_path))


def test_recover_rejects_malformed_child_boolean_fields(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
    layout.ensure_tx_root()
    layout.wal_path.write_text(
        '{"kind":"child","repo_path":"users/alice","prepared":"false"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TransactionStateError, match="boolean prepared"):
        backend.recover(str(tmp_path))


def test_pending_ancestor_child_enrollment_blocks_independent_child_begin(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    backend.prepare_repo_open(root_locator, backend.blob)
    backend.prepare_repo_open(
        child_locator,
        backend.blob,
        parent_repo_locator=root_locator,
        child_repo_path="users/alice",
    )
    root_tx = backend.begin(root_locator)
    root_tx.register_child_repo("users/alice")
    root_tx.lock.release()
    root_tx._released = True

    with pytest.raises(TransactionStateError, match="pending child coordination"):
        backend.begin(child_locator)

    backend.recover(root_locator)
    assert_no_transaction_artifacts(Path(root_locator))


def test_active_root_fence_blocks_independent_child_begin_before_wal_exists(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    backend.prepare_repo_open(root_locator, backend.blob)
    backend.prepare_repo_open(
        child_locator,
        backend.blob,
        parent_repo_locator=root_locator,
        child_repo_path="users/alice",
    )
    root_tx = backend.begin(root_locator)
    root_tx.lock.release()
    root_tx._released = True

    with pytest.raises(TransactionStateError, match="non-clear blob-side fence"):
        backend.begin(child_locator)

    backend.recover(root_locator)


def test_active_child_state_blocks_independent_parent_begin(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    backend.prepare_repo_open(root_locator, backend.blob)
    backend.prepare_repo_open(
        child_locator,
        backend.blob,
        parent_repo_locator=root_locator,
        child_repo_path="users/alice",
    )
    child_tx = backend.begin(child_locator)
    child_tx.lock.release()
    child_tx._released = True

    with pytest.raises(TransactionStateError, match="descendant repo"):
        backend.begin(root_locator)

    backend.recover(child_locator)


def test_fence_journal_mismatch_blocks_begin_and_recovery(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
    backend.blob.write_fence(
        layout,
        {
            "journal_backend": "other-journal",
            "parent_repo_locator": None,
            "repo_locator": str(layout.repo_root),
            "role": "root",
            "root_tx_id": "tx-1",
            "state": "active",
            "recovery_required": True,
            "version": 1,
        },
    )

    with pytest.raises(TransactionStateError, match="belongs to journal backend"):
        backend.begin(str(tmp_path))
    with pytest.raises(TransactionStateError, match="belongs to journal backend"):
        backend.recover(str(tmp_path))

    layout.cleanup_tx_root()


def test_malformed_fence_blocks_begin(tmp_path: Path) -> None:
    backend = FsRuntime()
    layout = RepoLayout(tmp_path)
    init_repo_metadata(tmp_path, runtime=backend)
    layout.ensure_tx_root()
    layout.fence_path.write_text('{"journal_backend":"jsonl-wal"}\n', encoding="utf-8")

    with pytest.raises(TransactionStateError, match="Malformed blob-side fence|Invalid blob-side fence"):
        backend.begin(str(tmp_path))

    layout.cleanup_tx_root()


def test_direct_recovery_of_subordinated_child_is_refused_from_fence(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    root_tx = backend.begin(root_locator)
    child_tx = backend.begin(child_locator, parent_tx=root_tx)
    root_tx.lock.release()
    root_tx._released = True

    with pytest.raises(TransactionStateError, match="Cannot recover coordinated filesystem child repo"):
        backend.recover(child_locator)

    child_tx.rollback()
    root_tx.rollback()


def test_recover_open_parent_with_unstarted_enrolled_child_treats_child_as_noop(tmp_path: Path) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    root_tx = backend.begin(root_locator)
    root_tx.register_child_repo("users/alice")
    root_tx.lock.release()
    root_tx._released = True

    backend.recover(root_locator)

    assert_no_transaction_artifacts(Path(root_locator))
    assert not RepoLayout(child_locator).tx_root.exists()


def test_recover_prepared_parent_with_unstarted_unprepared_child_treats_child_as_noop(tmp_path: Path) -> None:
    backend = FsRuntime()
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
    assert_no_transaction_artifacts(Path(root_locator))
    assert not RepoLayout(child_locator).tx_root.exists()


def test_recover_prepared_parent_with_missing_prepared_child_state_treats_child_as_already_finalized(
    tmp_path: Path,
) -> None:
    backend = FsRuntime()
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
    assert_no_transaction_artifacts(Path(root_locator))


def test_failed_child_marker_setup_unenrolls_parent_wal_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    root_tx = backend.begin(root_locator)
    original_append_wal_record = fs_journal_module.append_wal_record

    def fail_child_marker(layout: RepoLayout, record: dict[str, object]) -> None:
        if record.get("kind") == "coordinated_child":
            raise RuntimeError("child marker failed")
        original_append_wal_record(layout, record)

    with monkeypatch.context() as m:
        m.setattr(fs_journal_module, "append_wal_record", fail_child_marker)
        with pytest.raises(RuntimeError, match="child marker failed"):
            backend.begin(child_locator, parent_tx=root_tx)

    root_layout = RepoLayout(root_locator)
    if root_layout.wal_path.exists():
        _state, _overlay, children = load_wal(root_layout)
        assert children == {}
    assert_no_transaction_artifacts(Path(child_locator))
    root_tx.rollback()


def test_failed_child_marker_setup_still_rolls_back_child_when_parent_unenroll_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FsRuntime()
    root_locator = str(tmp_path / "repo")
    child_locator = backend.child_repo_locator(root_locator, "users/alice")
    root_tx = backend.begin(root_locator)
    original_append_wal_record = fs_journal_module.append_wal_record

    def fail_child_marker_and_unenroll(layout: RepoLayout, record: dict[str, object]) -> None:
        if record.get("kind") in {"coordinated_child", "child_unenrolled"}:
            raise RuntimeError(f"{record['kind']} failed")
        original_append_wal_record(layout, record)

    with monkeypatch.context() as m:
        m.setattr(fs_journal_module, "append_wal_record", fail_child_marker_and_unenroll)
        with pytest.raises(RuntimeError, match="coordinated_child failed") as exc_info:
            backend.begin(child_locator, parent_tx=root_tx)

    assert_no_transaction_artifacts(Path(child_locator))
    assert any("child_unenrolled failed" in note for note in getattr(exc_info.value, "__notes__", []))
    root_tx.rollback()


def test_parent_transaction_cannot_write_or_delete_inside_enrolled_child_repo_boundary(tmp_path: Path) -> None:
    backend = FsRuntime()
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
