from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import cast
from uuid import uuid4

import attrs

from ..._blob_backend import BlobBackend
from ..._errors import TransactionStateError
from ..._journal_backend import JournalTransaction, RecoveryIntent
from ..._tx_core import TransactionCore
from ..._types import OverlayEntry
from .blob import FilesystemBlobBackend, FilesystemCommittedState
from .transaction import JsonlWalTransaction
from .layout import RepoLayout
from .locking import HierarchicalLockSet
from .metadata import (
    coordination_root_locator as metadata_coordination_root_locator,
    ensure_child_metadata,
    ensure_root_or_existing_metadata,
    metadata_chain_root_to_target,
)
from .recovery import (
    WalReplay,
    WalState,
    append_wal_record,
    load_wal_replay,
    wal_child_record,
    wal_child_unenrolled_record,
    wal_coordinated_child_record,
    wal_overlay_record,
    wal_state_record,
)
from .refs import FsValueRef


@attrs.define(frozen=True, slots=True)
class JsonlWalJournalBackend:
    @property
    def identity(self) -> str:
        return "jsonl-wal"

    def prepare_repo_open(
        self,
        repo_locator: str,
        blob_backend: BlobBackend,
        *,
        parent_repo_locator: str | None = None,
        child_repo_path: str | None = None,
    ) -> str:
        if not isinstance(blob_backend, FilesystemBlobBackend):
            raise TransactionStateError("JSONL WAL currently requires a filesystem blob backend")
        layout = RepoLayout(repo_locator)
        if parent_repo_locator is None:
            if child_repo_path is not None:
                raise TransactionStateError("child_repo_path requires parent_repo_locator")
            return metadata_coordination_root_locator(layout, self.identity)
        if child_repo_path is None:
            raise TransactionStateError("parent_repo_locator requires child_repo_path")
        ensure_child_metadata(RepoLayout(parent_repo_locator), layout, child_repo_path, self.identity)
        return metadata_coordination_root_locator(layout, self.identity)

    def begin(
        self,
        repo_locator: str,
        blob_backend: BlobBackend,
        parent_tx: JournalTransaction | None = None,
        coordination_root_locator: str | None = None,
    ) -> JsonlWalTransaction:
        if not isinstance(blob_backend, FilesystemBlobBackend):
            raise TransactionStateError("JSONL WAL currently requires a filesystem blob backend")

        jsonl_parent_tx = cast(JsonlWalTransaction | None, parent_tx)
        return self._begin_locked(
            repo_locator,
            blob_backend,
            parent_tx=jsonl_parent_tx,
            coordination_root_locator=coordination_root_locator,
        )

    def _begin_locked(
        self,
        repo_locator: str,
        blob_backend: FilesystemBlobBackend,
        *,
        parent_tx: JsonlWalTransaction | None,
        coordination_root_locator: str | None,
    ) -> JsonlWalTransaction:
        layout = RepoLayout(repo_locator)
        lock = HierarchicalLockSet()
        lock_transferred = False
        child_repo_path: str | None = None
        try:
            if parent_tx is None:
                coordination_root = self.prepare_repo_open(repo_locator, blob_backend)
                if coordination_root_locator is not None and Path(coordination_root_locator).resolve() != Path(coordination_root).resolve():
                    raise TransactionStateError(
                        f"Configured coordination root {coordination_root_locator!r} does not match filesystem metadata root {coordination_root!r}"
                    )
                lock = self._acquire_independent_locks(layout)
                self._assert_independent_begin_is_safe(layout, blob_backend)
                root_tx_id = str(uuid4())
            else:
                if not isinstance(parent_tx, JsonlWalTransaction):
                    raise TransactionStateError("JSONL WAL nested repo transactions require a JSONL WAL parent transaction")
                try:
                    child_repo_path = Path(repo_locator).relative_to(parent_tx.layout.repo_root).as_posix()
                except ValueError as exc:
                    raise TransactionStateError(
                        f"Child repo {repo_locator!r} must be inside parent repo {str(parent_tx.layout.repo_root)!r}"
                    ) from exc
                coordination_root = self.prepare_repo_open(
                    repo_locator,
                    blob_backend,
                    parent_repo_locator=str(parent_tx.layout.repo_root),
                    child_repo_path=child_repo_path,
                )
                if Path(coordination_root).resolve() != Path(parent_tx.coordination_root_locator).resolve():
                    raise TransactionStateError(
                        f"Child repo {repo_locator!r} metadata root {coordination_root!r} does not match parent root "
                        f"{parent_tx.coordination_root_locator!r}"
                    )
                self._assert_coordinated_begin_is_safe(layout, blob_backend, parent_tx)
                root_tx_id = parent_tx.root_tx_id
            if self._has_pending_state(layout):
                raise TransactionStateError(
                    "Cannot begin a JSONL WAL transaction while pending recovery state exists; run recover() first"
                )
            tx = JsonlWalTransaction(
                layout=layout,
                lock=lock,
                core=TransactionCore(base_state=FilesystemCommittedState(layout)),
                blob=blob_backend,
                journal=self,
                root_tx_id=root_tx_id,
                coordination_root_locator=coordination_root,
            )
            lock_transferred = True
            if parent_tx is None:
                try:
                    tx._write_active_fence(role="root")
                except BaseException:
                    tx.rollback()
                    raise
            if parent_tx is not None:
                assert child_repo_path is not None
                try:
                    parent_tx.register_child_repo(child_repo_path)
                    tx._mark_coordinated_child(str(parent_tx.layout.repo_root))
                    tx._write_active_fence(role="subordinated", parent_repo_locator=str(parent_tx.layout.repo_root))
                except BaseException as original_error:
                    try:
                        parent_tx.unregister_child_repo(child_repo_path)
                    except BaseException as cleanup_error:
                        original_error.add_note(f"Additional child enrollment cleanup error: {cleanup_error!r}")
                    try:
                        tx.rollback()
                    except BaseException as cleanup_error:
                        original_error.add_note(f"Additional child transaction rollback error: {cleanup_error!r}")
                    raise
            return tx
        except BaseException:
            if not lock_transferred:
                lock.release()
            raise

    def recover(
        self,
        repo_locator: str,
        blob_backend: BlobBackend,
        *,
        intent: RecoveryIntent = "auto",
        coordination_root_locator: str | None = None,
    ) -> None:
        if not isinstance(blob_backend, FilesystemBlobBackend):
            raise TransactionStateError("JSONL WAL currently requires a filesystem blob backend")
        coordination_root = self.prepare_repo_open(repo_locator, blob_backend)
        if coordination_root_locator is not None and Path(coordination_root_locator).resolve() != Path(coordination_root).resolve():
            raise TransactionStateError(
                f"Configured coordination root {coordination_root_locator!r} does not match filesystem metadata root {coordination_root!r}"
            )
        locks = self._acquire_independent_locks(RepoLayout(repo_locator))
        try:
            self._assert_fence_matches_journal(RepoLayout(repo_locator), blob_backend)
            self._recover(repo_locator, blob_backend, intent=intent, allow_coordinated_child=False)
        finally:
            locks.release()

    def _recover(
        self,
        repo_locator: str,
        blob_backend: FilesystemBlobBackend,
        *,
        intent: RecoveryIntent,
        allow_coordinated_child: bool,
    ) -> None:
        layout = RepoLayout(repo_locator)
        fence = self._assert_fence_matches_journal(layout, blob_backend)
        if fence is not None and fence["role"] == "subordinated" and not allow_coordinated_child:
            parent_locator = fence["parent_repo_locator"]
            raise TransactionStateError(
                f"Cannot recover coordinated filesystem child repo {repo_locator!r} directly; "
                f"recover coordinator repo {parent_locator!r}"
            )
        if not self._has_pending_state(layout):
            return

        layout.ensure_repo_root()
        if not self._has_pending_state(layout):
            return
        if not layout.wal_path.exists():
            layout.cleanup_tx_root()
            return
        replay = load_wal_replay(layout)
        if replay.parent_repo_locator is not None and not allow_coordinated_child:
            raise TransactionStateError(
                f"Cannot recover coordinated filesystem child repo {repo_locator!r} directly; "
                f"recover coordinator repo {replay.parent_repo_locator!r}"
            )
        if intent == "abort" and replay.state != "open" and not allow_coordinated_child:
            raise TransactionStateError(
                f"Cannot abort recovery for {repo_locator!r}; WAL state {replay.state!r} "
                "must complete publication"
            )
        if intent == "abort" or replay.state == "open":
            for child_repo_path, child_state in replay.children.items():
                self._recover_child_participant(
                    repo_locator,
                    blob_backend,
                    child_repo_path,
                    child_state,
                    intent="abort",
                )
            layout.cleanup_tx_root()
            return
        blob_backend.publish_prepared(
            layout,
            replay.overlay,
            allow_missing_published_puts=True,
            child_repo_paths=set(replay.children),
        )
        for child_repo_path, child_state in replay.children.items():
            self._recover_child_participant(
                repo_locator,
                blob_backend,
                child_repo_path,
                child_state,
                intent="auto",
            )
        layout.cleanup_tx_root()

    def _recover_child_participant(
        self,
        parent_repo_locator: str,
        blob_backend: FilesystemBlobBackend,
        child_repo_path: str,
        child_state: Mapping[str, bool],
        *,
        intent: RecoveryIntent,
    ) -> None:
        child_locator = blob_backend.child_repo_locator(parent_repo_locator, child_repo_path)
        child_layout = RepoLayout(child_locator)
        if not self._has_pending_state(child_layout):
            return
        self._recover(child_locator, blob_backend, intent=intent, allow_coordinated_child=True)

    def _has_pending_state(self, layout: RepoLayout) -> bool:
        return (
            layout.wal_path.exists()
            or layout.fence_path.exists()
            or layout.fence_tmp_path.exists()
            or layout.staged_root.exists()
        )

    def _acquire_independent_locks(self, layout: RepoLayout) -> HierarchicalLockSet:
        chain = metadata_chain_root_to_target(layout, self.identity)
        lock_plan = [
            (chain_layout.lock_path, "shared" if index < len(chain) - 1 else "exclusive")
            for index, (chain_layout, _metadata) in enumerate(chain)
        ]
        return HierarchicalLockSet.acquire(lock_plan)

    def _assert_fence_matches_journal(self, layout: RepoLayout, blob_backend: FilesystemBlobBackend) -> Mapping[str, object] | None:
        fence = blob_backend.read_fence(layout)
        if fence is None:
            return None
        self._validate_fence_payload(layout, fence)
        journal_backend = fence.get("journal_backend")
        if journal_backend != self.identity:
            raise TransactionStateError(
                f"Blob-side fence for {str(layout.repo_root)!r} belongs to journal backend "
                f"{journal_backend!r}, not configured backend {self.identity!r}"
            )
        return fence

    def _validate_fence_payload(self, layout: RepoLayout, fence: Mapping[str, object]) -> None:
        if fence.get("version") != 1:
            raise TransactionStateError(f"Invalid blob-side fence version for {str(layout.repo_root)!r}")
        expected_strings = {
            "journal_backend",
            "repo_locator",
            "role",
            "root_tx_id",
            "state",
        }
        for key in expected_strings:
            value = fence.get(key)
            if not isinstance(value, str) or not value:
                raise TransactionStateError(
                    f"Malformed blob-side fence for {str(layout.repo_root)!r}: {key!r} must be a non-empty string"
                )
        if fence["repo_locator"] != str(layout.repo_root):
            raise TransactionStateError(
                f"Blob-side fence for {str(layout.repo_root)!r} belongs to repo {fence['repo_locator']!r}"
            )
        if fence["role"] not in {"root", "subordinated"}:
            raise TransactionStateError(
                f"Malformed blob-side fence for {str(layout.repo_root)!r}: invalid role {fence['role']!r}"
            )
        if fence["state"] not in {"active"}:
            raise TransactionStateError(
                f"Malformed blob-side fence for {str(layout.repo_root)!r}: invalid state {fence['state']!r}"
            )
        recovery_required = fence.get("recovery_required")
        if not isinstance(recovery_required, bool):
            raise TransactionStateError(
                f"Malformed blob-side fence for {str(layout.repo_root)!r}: 'recovery_required' must be boolean"
            )
        parent_repo_locator = fence.get("parent_repo_locator")
        if fence["role"] == "subordinated":
            if not isinstance(parent_repo_locator, str) or not parent_repo_locator:
                raise TransactionStateError(
                    f"Malformed blob-side fence for {str(layout.repo_root)!r}: subordinated fence needs parent locator"
                )
        elif parent_repo_locator is not None:
            raise TransactionStateError(
                f"Malformed blob-side fence for {str(layout.repo_root)!r}: root fence must not have parent locator"
            )

    def _assert_independent_begin_is_safe(self, layout: RepoLayout, blob_backend: FilesystemBlobBackend) -> None:
        self._assert_fence_matches_journal(layout, blob_backend)
        self._assert_no_pending_ancestor_coordination(layout, blob_backend)
        self._assert_no_pending_descendant_coordination(layout, blob_backend)

    def _assert_coordinated_begin_is_safe(
        self,
        layout: RepoLayout,
        blob_backend: FilesystemBlobBackend,
        parent_tx: JsonlWalTransaction,
    ) -> None:
        fence = self._assert_fence_matches_journal(layout, blob_backend)
        if fence is None:
            return
        if fence.get("root_tx_id") != parent_tx.root_tx_id or fence.get("parent_repo_locator") != str(parent_tx.layout.repo_root):
            raise TransactionStateError(
                f"Blob-side fence for {str(layout.repo_root)!r} is not subordinated to the active parent transaction"
            )

    def _assert_no_pending_ancestor_coordination(
        self,
        layout: RepoLayout,
        blob_backend: FilesystemBlobBackend,
    ) -> None:
        repo_root = layout.repo_root
        ancestor = repo_root.parent
        while ancestor != ancestor.parent:
            ancestor_layout = RepoLayout(ancestor)
            if ancestor_layout.wal_path.exists():
                replay = load_wal_replay(ancestor_layout)
                repo_path = PurePosixPath(repo_root.relative_to(ancestor_layout.repo_root).as_posix())
                for child_repo_path in replay.children:
                    child_path = PurePosixPath(child_repo_path)
                    if repo_path == child_path or child_path in repo_path.parents:
                        raise TransactionStateError(
                            f"Cannot begin filesystem transaction at {str(repo_root)!r}; "
                            f"ancestor coordinator {str(ancestor_layout.repo_root)!r} has pending child "
                            f"coordination for {child_repo_path!r}. Recover the coordinator repo first."
                        )
            if ancestor_layout.fence_path.exists():
                self._assert_fence_matches_journal(ancestor_layout, blob_backend)
                raise TransactionStateError(
                    f"Cannot begin filesystem transaction at {str(repo_root)!r}; "
                    f"ancestor repo {str(ancestor_layout.repo_root)!r} has a non-clear blob-side fence. "
                    f"Recover the ancestor repo first."
                )
            ancestor = ancestor.parent

    def _assert_no_pending_descendant_coordination(
        self,
        layout: RepoLayout,
        blob_backend: FilesystemBlobBackend,
    ) -> None:
        if not layout.repo_root.exists():
            return
        for tx_root in layout.repo_root.rglob(".tx"):
            if tx_root == layout.tx_root:
                continue
            descendant_layout = RepoLayout(tx_root.parent)
            if self._has_pending_state(descendant_layout):
                if descendant_layout.fence_path.exists():
                    self._assert_fence_matches_journal(descendant_layout, blob_backend)
                raise TransactionStateError(
                    f"Cannot begin filesystem transaction at {str(layout.repo_root)!r}; "
                    f"descendant repo {str(descendant_layout.repo_root)!r} has pending transaction state. "
                    f"Recover the descendant or coordinator repo first."
                )

    def require_clear_for_read(self, repo_locator: str, blob_backend: BlobBackend) -> None:
        if not isinstance(blob_backend, FilesystemBlobBackend):
            raise TransactionStateError("JSONL WAL currently requires a filesystem blob backend")
        layout = RepoLayout(repo_locator)
        self._assert_fence_matches_journal(layout, blob_backend)
        if self._has_pending_state(layout):
            raise TransactionStateError(
                f"Cannot read filesystem repo {repo_locator!r}; pending transaction state exists. "
                "Run recover() before reading committed state."
            )
        self._assert_no_pending_ancestor_read_state(layout, blob_backend)
        self._assert_no_pending_descendant_coordination(layout, blob_backend)

    def _assert_no_pending_ancestor_read_state(
        self,
        layout: RepoLayout,
        blob_backend: FilesystemBlobBackend,
    ) -> None:
        repo_root = layout.repo_root
        ancestor = repo_root.parent
        while ancestor != ancestor.parent:
            ancestor_layout = RepoLayout(ancestor)
            if self._has_pending_state(ancestor_layout):
                if ancestor_layout.fence_path.exists():
                    self._assert_fence_matches_journal(ancestor_layout, blob_backend)
                if ancestor_layout.wal_path.exists():
                    load_wal_replay(ancestor_layout)
                raise TransactionStateError(
                    f"Cannot read filesystem repo {str(repo_root)!r}; "
                    f"ancestor repo {str(ancestor_layout.repo_root)!r} has pending transaction state. "
                    "Run recover() before reading committed state."
                )
            ancestor = ancestor.parent

    def debug_status(self, repo_locator: str, blob_backend: BlobBackend) -> object:
        del blob_backend
        layout = RepoLayout(repo_locator)
        return {
            "repo_root": str(layout.repo_root),
            "tx_root_exists": layout.tx_root.exists(),
            "wal_exists": layout.wal_path.exists(),
            "lock_exists": layout.lock_path.exists(),
            "keys": sorted(layout.snapshot_keys()),
        }

    def append_overlay(
        self,
        layout: RepoLayout,
        key: str,
        entry: OverlayEntry[FsValueRef],
    ) -> None:
        append_wal_record(layout, wal_overlay_record(layout, key, entry))

    def mark_state(self, layout: RepoLayout, state: WalState) -> None:
        append_wal_record(layout, wal_state_record(state))

    def enroll_child(self, layout: RepoLayout, child_repo_path: str, child_state: Mapping[str, bool]) -> None:
        append_wal_record(
            layout,
            wal_child_record(
                layout,
                child_repo_path,
                prepared=child_state["prepared"],
            ),
        )

    def unenroll_child(self, layout: RepoLayout, child_repo_path: str) -> None:
        append_wal_record(layout, wal_child_unenrolled_record(layout, child_repo_path))

    def mark_coordinated_child(self, layout: RepoLayout, parent_repo_locator: str) -> None:
        append_wal_record(layout, wal_coordinated_child_record(parent_repo_locator))

    def replay(self, layout: RepoLayout) -> WalReplay:
        return load_wal_replay(layout)


__all__ = ["JsonlWalJournalBackend"]
