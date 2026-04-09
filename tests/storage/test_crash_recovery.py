from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from alpenstock.storage.backends.fs import FilesystemBackend, RepoLayout
from alpenstock.storage.backends.sqlite import SqliteBackend, SqliteConfig


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
from alpenstock.storage.backends.fs import FilesystemBackend

backend = FilesystemBackend()
tx = backend.begin({str(repo_path)!r})
handle = tx.open_handle("alpha", "wb")
handle.write(b"one")
os._exit(17)
"""

    _run_crash_script(script, cwd=tmp_path)

    backend = FilesystemBackend()
    layout = RepoLayout(repo_path)
    backend.recover(str(repo_path))

    assert not layout.committed_path("alpha").exists()
    assert not layout.tx_root.exists()


def test_filesystem_recover_commits_prepared_state_after_process_exit(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    script = f"""
import os
from alpenstock.storage.backends.fs import FilesystemBackend

backend = FilesystemBackend()
tx = backend.begin({str(repo_path)!r})
with tx.open_handle("alpha", "wb") as handle:
    handle.write(b"one")
tx.prepare()
os._exit(17)
"""

    _run_crash_script(script, cwd=tmp_path)

    backend = FilesystemBackend()
    layout = RepoLayout(repo_path)
    backend.recover(str(repo_path))

    assert layout.committed_path("alpha").read_bytes() == b"one"
    assert not layout.tx_root.exists()


def test_filesystem_recover_completes_commit_after_process_exit_during_publication(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    script = f"""
import os
from alpenstock.storage.backends.fs import FilesystemBackend, RepoLayout

repo_path = {str(repo_path)!r}
layout = RepoLayout(repo_path)
gamma_path = layout.committed_path("gamma")
gamma_path.parent.mkdir(parents=True, exist_ok=True)
gamma_path.write_bytes(b"old")

backend = FilesystemBackend()
tx = backend.begin(repo_path)
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

    backend = FilesystemBackend()
    layout = RepoLayout(repo_path)

    assert layout.committed_path("alpha").read_bytes() == b"one"
    assert not layout.committed_path("beta").exists()
    assert not layout.committed_path("gamma").exists()

    backend.recover(str(repo_path))

    assert layout.committed_path("alpha").read_bytes() == b"one"
    assert layout.committed_path("beta").read_bytes() == b"two"
    assert not layout.committed_path("gamma").exists()
    assert not layout.tx_root.exists()


def test_sqlite_recover_discards_open_state_after_process_exit(tmp_path: Path) -> None:
    db_path = tmp_path / "repo.db"
    script = f"""
import os
from alpenstock.storage.backends.sqlite import SqliteBackend

backend = SqliteBackend()
tx = backend.begin({str(db_path)!r})
handle = tx.open_handle("alpha", "wb")
handle.write(b"one")
os._exit(17)
"""

    _run_crash_script(script, cwd=tmp_path)

    backend = SqliteBackend()
    backend.recover(str(db_path))

    names = SqliteConfig().schema_names
    connection = sqlite3.connect(str(db_path))
    try:
        rows = list(connection.execute(f'SELECT repo_path, key, value FROM "{names.objects}"'))
        pending = list(connection.execute(f'SELECT tx_id, repo_path, state FROM "{names.tx_meta}"'))
    finally:
        connection.close()

    assert rows == []
    assert pending == []


def test_sqlite_recover_commits_prepared_state_after_process_exit(tmp_path: Path) -> None:
    db_path = tmp_path / "repo.db"
    script = f"""
import os
from alpenstock.storage.backends.sqlite import SqliteBackend

backend = SqliteBackend()
tx = backend.begin({str(db_path)!r})
with tx.open_handle("alpha", "wb") as handle:
    handle.write(b"one")
tx.prepare()
os._exit(17)
"""

    _run_crash_script(script, cwd=tmp_path)

    backend = SqliteBackend()
    backend.recover(str(db_path))

    names = SqliteConfig().schema_names
    connection = sqlite3.connect(str(db_path))
    try:
        rows = list(connection.execute(f'SELECT repo_path, key, value FROM "{names.objects}" ORDER BY repo_path, key'))
        pending = list(connection.execute(f'SELECT tx_id, repo_path, state FROM "{names.tx_meta}"'))
    finally:
        connection.close()

    assert rows == [("", "alpha", b"one")]
    assert pending == []


def test_filesystem_recover_commits_prepared_nested_tree_after_process_exit(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    child_repo_path = repo_path / "users" / "alice"
    script = f"""
import os
from alpenstock.storage.backends.fs import FilesystemBackend

repo_path = {str(repo_path)!r}
child_repo_path = {str(child_repo_path)!r}

backend = FilesystemBackend()
root_tx = backend.begin(repo_path)
child_tx = backend.begin(child_repo_path, parent_tx=root_tx)
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

    backend = FilesystemBackend()
    backend.recover(str(repo_path))

    assert (repo_path / "config").read_bytes() == b"root"
    assert (child_repo_path / "profile").read_bytes() == b"alice"
    assert not RepoLayout(repo_path).tx_root.exists()


def test_sqlite_recover_commits_prepared_nested_tree_after_process_exit(tmp_path: Path) -> None:
    db_path = tmp_path / "repo.db"
    child_locator = f"{db_path}::repo::users/alice"
    script = f"""
import os
from alpenstock.storage.backends.sqlite import SqliteBackend

db_path = {str(db_path)!r}
child_locator = {child_locator!r}

backend = SqliteBackend()
root_tx = backend.begin(db_path)
child_tx = backend.begin(child_locator, parent_tx=root_tx)
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

    backend = SqliteBackend()
    backend.recover(str(db_path))

    names = SqliteConfig().schema_names
    connection = sqlite3.connect(str(db_path))
    try:
        rows = list(connection.execute(f'SELECT repo_path, key, value FROM "{names.objects}" ORDER BY repo_path, key'))
        pending = list(connection.execute(f'SELECT tx_id, repo_path, state FROM "{names.tx_meta}"'))
    finally:
        connection.close()

    assert rows == [
        ("", "config", b"root"),
        ("users/alice", "profile", b"alice"),
    ]
    assert pending == []


def test_sqlite_recover_prefixed_schema_after_process_exit(tmp_path: Path) -> None:
    db_path = tmp_path / "repo.db"
    config = SqliteConfig(schema_prefix="_storage_")
    names = config.schema_names
    script = f"""
import os
from alpenstock.storage.backends.sqlite import SqliteBackend, SqliteConfig

backend = SqliteBackend(config=SqliteConfig(schema_prefix="_storage_"))
tx = backend.begin({str(db_path)!r})
with tx.open_handle("alpha", "wb") as handle:
    handle.write(b"one")
tx.prepare()
os._exit(17)
"""

    _run_crash_script(script, cwd=tmp_path)

    backend = SqliteBackend(config=config)
    backend.recover(str(db_path))

    connection = sqlite3.connect(str(db_path))
    try:
        rows = list(connection.execute(f'SELECT repo_path, key, value FROM "{names.objects}" ORDER BY repo_path, key'))
        pending = list(connection.execute(f'SELECT tx_id, repo_path, state FROM "{names.tx_meta}"'))
    finally:
        connection.close()

    assert rows == [("", "alpha", b"one")]
    assert pending == []


def test_sqlite_recover_prefixed_prepared_nested_tree_after_process_exit(tmp_path: Path) -> None:
    db_path = tmp_path / "repo.db"
    child_locator = f"{db_path}::repo::users/alice"
    config = SqliteConfig(schema_prefix="_storage_")
    names = config.schema_names
    script = f"""
import os
from alpenstock.storage.backends.sqlite import SqliteBackend, SqliteConfig

db_path = {str(db_path)!r}
child_locator = {child_locator!r}

backend = SqliteBackend(config=SqliteConfig(schema_prefix="_storage_"))
root_tx = backend.begin(db_path)
child_tx = backend.begin(child_locator, parent_tx=root_tx)
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

    backend = SqliteBackend(config=config)
    backend.recover(str(db_path))

    connection = sqlite3.connect(str(db_path))
    try:
        rows = list(connection.execute(f'SELECT repo_path, key, value FROM "{names.objects}" ORDER BY repo_path, key'))
        pending = list(connection.execute(f'SELECT tx_id, repo_path, state FROM "{names.tx_meta}"'))
    finally:
        connection.close()

    assert rows == [
        ("", "config", b"root"),
        ("users/alice", "profile", b"alice"),
    ]
    assert pending == []
