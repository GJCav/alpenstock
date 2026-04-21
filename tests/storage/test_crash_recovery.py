from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from alpenstock.storage import Repo, TransactionStateError
from alpenstock.storage.backends.fs import FilesystemBlobBackend, JsonlWalJournalBackend, RepoLayout
from tests.storage._fs_test_utils import assert_no_transaction_artifacts


def _run_crash_script(script: str, *, cwd: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0, f"Expected crash exit, got rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"


def test_filesystem_recover_discards_open_state_after_process_exit(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    script = f"""
import os
from alpenstock.storage.backends.fs import FilesystemBlobBackend, JsonlWalJournalBackend

blob = FilesystemBlobBackend()
journal = JsonlWalJournalBackend()
tx = journal.begin({str(repo_path)!r}, blob)
handle = tx.open_handle("alpha", "wb")
handle.write(b"one")
os._exit(17)
"""

    _run_crash_script(script, cwd=tmp_path)

    blob = FilesystemBlobBackend()
    journal = JsonlWalJournalBackend()
    layout = RepoLayout(repo_path)
    journal.recover(str(repo_path), blob)

    assert not layout.committed_path("alpha").exists()
    assert_no_transaction_artifacts(repo_path)


def test_filesystem_recover_commits_prepared_state_after_process_exit(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    script = f"""
import os
from alpenstock.storage.backends.fs import FilesystemBlobBackend, JsonlWalJournalBackend

blob = FilesystemBlobBackend()
journal = JsonlWalJournalBackend()
tx = journal.begin({str(repo_path)!r}, blob)
with tx.open_handle("alpha", "wb") as handle:
    handle.write(b"one")
tx.prepare()
os._exit(17)
"""

    _run_crash_script(script, cwd=tmp_path)

    blob = FilesystemBlobBackend()
    journal = JsonlWalJournalBackend()
    layout = RepoLayout(repo_path)
    journal.recover(str(repo_path), blob)

    assert layout.committed_path("alpha").read_bytes() == b"one"
    assert_no_transaction_artifacts(repo_path)


def test_filesystem_recover_completes_commit_after_process_exit_during_publication(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    script = f"""
import os
from alpenstock.storage.backends.fs import FilesystemBlobBackend, JsonlWalJournalBackend, RepoLayout

repo_path = {str(repo_path)!r}
blob = FilesystemBlobBackend()
journal = JsonlWalJournalBackend()
journal.prepare_repo_open(repo_path, blob)
layout = RepoLayout(repo_path)
gamma_path = layout.committed_path("gamma")
gamma_path.parent.mkdir(parents=True, exist_ok=True)
gamma_path.write_bytes(b"old")

tx = journal.begin(repo_path, blob)
with tx.open_handle("alpha", "wb") as handle:
    handle.write(b"one")
with tx.open_handle("beta", "wb") as handle:
    handle.write(b"two")
tx.delete("gamma")
tx.prepare()
os.environ["ALPENSTOCK_FS_CRASH_AFTER_PUBLICATION_OPS"] = "2"
tx.commit()
"""

    _run_crash_script(script, cwd=tmp_path)

    blob = FilesystemBlobBackend()
    journal = JsonlWalJournalBackend()
    layout = RepoLayout(repo_path)

    assert layout.committed_path("alpha").read_bytes() == b"one"
    assert not layout.committed_path("beta").exists()
    assert not layout.committed_path("gamma").exists()
    repo = Repo.open(str(repo_path), blob_backend=blob, journal_backend=journal)
    with pytest.raises(TransactionStateError, match="pending transaction state"):
        repo.file("alpha").read_bytes()

    journal.recover(str(repo_path), blob)

    assert layout.committed_path("alpha").read_bytes() == b"one"
    assert layout.committed_path("beta").read_bytes() == b"two"
    assert not layout.committed_path("gamma").exists()
    assert_no_transaction_artifacts(repo_path)


def test_filesystem_recover_commits_prepared_nested_tree_after_process_exit(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    child_repo_path = repo_path / "users" / "alice"
    script = f"""
import os
from alpenstock.storage.backends.fs import FilesystemBlobBackend, JsonlWalJournalBackend

repo_path = {str(repo_path)!r}
child_repo_path = {str(child_repo_path)!r}

blob = FilesystemBlobBackend()
journal = JsonlWalJournalBackend()
root_tx = journal.begin(repo_path, blob)
child_tx = journal.begin(child_repo_path, blob, parent_tx=root_tx)
with root_tx.open_handle("config", "wb") as handle:
    handle.write(b"root")
with child_tx.open_handle("profile", "wb") as handle:
    handle.write(b"alice")
child_tx.prepare()
root_tx.mark_child_prepared("users/alice")
root_tx.prepare()
os._exit(17)
"""

    _run_crash_script(script, cwd=tmp_path)

    blob = FilesystemBlobBackend()
    journal = JsonlWalJournalBackend()
    journal.recover(str(repo_path), blob)

    assert (repo_path / "config").read_bytes() == b"root"
    assert (child_repo_path / "profile").read_bytes() == b"alice"
    assert_no_transaction_artifacts(repo_path)


def test_filesystem_recover_completes_after_root_marked_committing_before_publication(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    child_repo_path = repo_path / "users" / "alice"
    script = f"""
import os
from alpenstock.storage.backends.fs import FilesystemBlobBackend, JsonlWalJournalBackend

blob = FilesystemBlobBackend()
journal = JsonlWalJournalBackend()
root_tx = journal.begin({str(repo_path)!r}, blob)
child_tx = journal.begin({str(child_repo_path)!r}, blob, parent_tx=root_tx)
with root_tx.open_handle("config", "wb") as handle:
    handle.write(b"root")
with child_tx.open_handle("profile", "wb") as handle:
    handle.write(b"alice")
child_tx.prepare()
root_tx.mark_child_prepared("users/alice")
root_tx.prepare()
root_tx.mark_committing()
os._exit(17)
"""

    _run_crash_script(script, cwd=tmp_path)

    blob = FilesystemBlobBackend()
    journal = JsonlWalJournalBackend()
    journal.recover(str(repo_path), blob)

    assert (repo_path / "config").read_bytes() == b"root"
    assert (child_repo_path / "profile").read_bytes() == b"alice"
    assert_no_transaction_artifacts(repo_path)
    assert_no_transaction_artifacts(child_repo_path)


def test_filesystem_read_refuses_after_root_publication_before_child_publication(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    child_repo_path = repo_path / "users" / "alice"
    script = f"""
import os
from alpenstock.storage.backends.fs import FilesystemBlobBackend, JsonlWalJournalBackend

blob = FilesystemBlobBackend()
journal = JsonlWalJournalBackend()
root_tx = journal.begin({str(repo_path)!r}, blob)
child_tx = journal.begin({str(child_repo_path)!r}, blob, parent_tx=root_tx)
with root_tx.open_handle("config", "wb") as handle:
    handle.write(b"root")
with child_tx.open_handle("profile", "wb") as handle:
    handle.write(b"alice")
child_tx.prepare()
root_tx.mark_child_prepared("users/alice")
root_tx.prepare()
root_tx.mark_committing()
root_tx.publish_prepared()
os._exit(17)
"""

    _run_crash_script(script, cwd=tmp_path)

    blob = FilesystemBlobBackend()
    journal = JsonlWalJournalBackend()
    repo = Repo.open(str(repo_path), blob_backend=blob, journal_backend=journal)

    assert (repo_path / "config").read_bytes() == b"root"
    assert not (child_repo_path / "profile").exists()
    with pytest.raises(TransactionStateError, match="pending transaction state"):
        repo.file("config").read_bytes()

    journal.recover(str(repo_path), blob)

    assert (repo_path / "config").read_bytes() == b"root"
    assert (child_repo_path / "profile").read_bytes() == b"alice"
    assert_no_transaction_artifacts(repo_path)
    assert_no_transaction_artifacts(child_repo_path)


def test_filesystem_recover_cleans_after_child_publication_before_cleanup(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    child_repo_path = repo_path / "users" / "alice"
    script = f"""
import os
from alpenstock.storage.backends.fs import FilesystemBlobBackend, JsonlWalJournalBackend

blob = FilesystemBlobBackend()
journal = JsonlWalJournalBackend()
root_tx = journal.begin({str(repo_path)!r}, blob)
child_tx = journal.begin({str(child_repo_path)!r}, blob, parent_tx=root_tx)
with root_tx.open_handle("config", "wb") as handle:
    handle.write(b"root")
with child_tx.open_handle("profile", "wb") as handle:
    handle.write(b"alice")
child_tx.prepare()
root_tx.mark_child_prepared("users/alice")
root_tx.prepare()
root_tx.mark_committing()
root_tx.publish_prepared()
child_tx.authorize_root_publication()
child_tx.mark_committing()
child_tx.publish_prepared()
os._exit(17)
"""

    _run_crash_script(script, cwd=tmp_path)

    blob = FilesystemBlobBackend()
    journal = JsonlWalJournalBackend()
    journal.recover(str(repo_path), blob)
    journal.recover(str(repo_path), blob)

    assert (repo_path / "config").read_bytes() == b"root"
    assert (child_repo_path / "profile").read_bytes() == b"alice"
    assert_no_transaction_artifacts(repo_path)
    assert_no_transaction_artifacts(child_repo_path)
