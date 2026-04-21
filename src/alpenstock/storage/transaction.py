from __future__ import annotations

from typing import TYPE_CHECKING, overload

import attrs

from ._errors import TransactionStateError
from ._handles import BinaryFileHandle, FileHandle, ReadableBinaryMode, ReadableTextMode, TextFileHandle
from ._journal_backend import JournalTransaction
from ._types import OpenMode

if TYPE_CHECKING:
    from .repo import Repo


@attrs.define(slots=True)
class TransactionContext:
    repo: Repo = attrs.field(repr=False)
    _journal_tx: JournalTransaction | None = attrs.field(default=None, init=False, repr=False)
    _parent: TransactionContext | None = attrs.field(default=None, init=False, repr=False)
    _children: dict[str, TransactionContext] = attrs.field(factory=dict, init=False, repr=False)
    _scope_depth: int = attrs.field(default=0, init=False, repr=False)
    _started: bool = attrs.field(default=False, init=False, repr=False)
    _rollback_done: bool = attrs.field(default=False, init=False, repr=False)
    _finished: bool = attrs.field(default=False, init=False, repr=False)
    _prepared: bool = attrs.field(default=False, init=False, repr=False)
    _finalization_failed: bool = attrs.field(default=False, init=False, repr=False)
    _recovery_required: bool = attrs.field(default=False, init=False, repr=False)

    @property
    def journal_tx(self) -> JournalTransaction:
        if self._finished:
            if self._recovery_required:
                raise TransactionStateError(
                    "Transaction context is finished and requires repo.recover() on the repo root"
                )
            raise TransactionStateError("Transaction context has already finished")
        if self._journal_tx is None:
            raise TransactionStateError("Transaction context has not been entered")
        return self._journal_tx

    @property
    def parent(self) -> TransactionContext | None:
        return self._parent

    @property
    def is_prepared(self) -> bool:
        return self._prepared

    @property
    def root(self) -> TransactionContext:
        current = self
        while current._parent is not None:
            current = current._parent
        return current

    def __enter__(self) -> TransactionContext:
        if self._parent is not None:
            if self._finished:
                raise TransactionStateError("Coordinated child transaction is no longer active")
            if self._prepared:
                self._reopen_prepared_child()
            self._scope_depth += 1
            return self

        if self._started:
            raise TransactionStateError("Transaction context cannot be entered twice")

        journal_tx = self.repo.journal_backend.begin(
            self.repo.repo_locator,
            self.repo.blob_backend,
            parent_tx=None,
            coordination_root_locator=self.repo.coordination_root_locator,
        )
        self._journal_tx = journal_tx
        self.repo._bind_transaction(self)
        self._started = True
        self._scope_depth = 1
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._parent is not None:
            if self._scope_depth > 0:
                self._scope_depth -= 1
            if exc_type is not None and not self._finished:
                try:
                    self._abort_child_participant()
                except BaseException as cleanup_error:
                    if exc is not None:
                        self._add_cleanup_note(exc, phase="rollback", cleanup_error=cleanup_error)
                    else:
                        raise
            elif self._scope_depth == 0 and not self._prepared and not self._finished:
                self._prepare_child_participant()
            return None

        try:
            if not self._started or self._finished:
                return None
            if exc_type is None and not self._rollback_done:
                try:
                    self._prepare_tree()
                except Exception as original_error:
                    try:
                        self._abort_tree()
                    except BaseException as cleanup_error:
                        self._add_cleanup_note(original_error, phase="rollback", cleanup_error=cleanup_error)
                    raise
                try:
                    self._commit_tree()
                except Exception as original_error:
                    self._finalization_failed = True
                    self._recovery_required = True
                    try:
                        self._detach_tree_for_recovery()
                    except BaseException as cleanup_error:
                        self._add_cleanup_note(
                            original_error,
                            phase="detach-for-recovery",
                            cleanup_error=cleanup_error,
                        )
                    finally:
                        self._mark_tree_finished()
                    raise
                self._mark_tree_finished()
            elif not self._rollback_done:
                if exc is not None:
                    try:
                        self._abort_tree()
                    except BaseException as cleanup_error:
                        self._add_cleanup_note(exc, phase="rollback", cleanup_error=cleanup_error)
                else:
                    self._abort_tree()
            else:
                self._mark_tree_finished()
        finally:
            self._unbind_tree()
        return None

    @overload
    def open(
        self,
        key: str,
        mode: ReadableTextMode = "r",
        *,
        encoding: str | None = None,
    ) -> TextFileHandle: ...

    @overload
    def open(
        self,
        key: str,
        mode: ReadableBinaryMode,
        *,
        encoding: None = None,
    ) -> BinaryFileHandle: ...

    def open(self, key: str, mode: OpenMode = "r", *, encoding: str | None = None) -> FileHandle:
        self.repo._assert_raw_key_allowed(key)
        if self._parent is not None and self._prepared and mode not in ("r", "rb"):
            self._reopen_prepared_child()
        return self.journal_tx.open_handle(key, mode, encoding=encoding)

    def delete(self, key: str) -> None:
        self.repo._assert_raw_key_allowed(key)
        if self._parent is not None and self._prepared:
            self._reopen_prepared_child()
        self.journal_tx.delete(key)

    def rollback(self) -> None:
        if self._finished:
            if self._recovery_required:
                raise TransactionStateError(
                    "rollback() is invalid after commit finalization failed; run repo.recover() on the repo root"
                )
            raise TransactionStateError("rollback() is invalid after the transaction has finished")
        if not self._started:
            raise TransactionStateError("Transaction context has not been entered")
        if self._rollback_done:
            return
        if self._parent is not None:
            self._abort_child_participant()
            return
        self._abort_tree()

    def _cancel_child_setup_failure(self, *, remove_participant: bool) -> None:
        if self._parent is None:
            raise TransactionStateError("Only coordinated child transactions can cancel setup failure")
        if self._scope_depth > 0:
            self._scope_depth -= 1
        if not remove_participant:
            return
        try:
            self.journal_tx.rollback()
        finally:
            child_repo_path = self.repo._child_repo_path
            assert child_repo_path is not None
            self._parent.journal_tx.unregister_child_repo(child_repo_path)
            self._parent._children.pop(self.repo.repo_locator, None)
            self.repo._unbind_transaction(self)
            self._journal_tx = None
            self._finished = True

    def _ensure_child_transaction(self, repo: Repo) -> TransactionContext:
        existing = self._children.get(repo.repo_locator)
        if existing is not None:
            return existing
        if repo.active_transaction is not None:
            return repo.active_transaction

        child_journal_tx = repo.journal_backend.begin(
            repo.repo_locator,
            repo.blob_backend,
            parent_tx=self.journal_tx,
            coordination_root_locator=repo.coordination_root_locator,
        )
        child_tx = TransactionContext(repo=repo)
        child_tx._journal_tx = child_journal_tx
        child_tx._parent = self
        child_tx._started = True
        repo._bind_transaction(child_tx)
        self._children[repo.repo_locator] = child_tx
        return child_tx

    def _prepare_tree(self) -> None:
        self._assert_all_child_scopes_closed()
        for child in self._children.values():
            if not child._prepared:
                child._prepare_tree()
                child_repo_path = child.repo._child_repo_path
                assert child_repo_path is not None
                self.journal_tx.mark_child_prepared(child_repo_path)

        self.journal_tx.assert_all_writers_closed()
        if not self._prepared:
            self.journal_tx.prepare()
            self._prepared = True

    def _commit_tree(self) -> None:
        self.journal_tx.mark_committing()
        try:
            self.journal_tx.publish_prepared()
        except BaseException:
            self.journal_tx.detach_for_recovery()
            raise
        for child in self._children.values():
            child.journal_tx.authorize_root_publication()
            child._commit_tree()

        self.journal_tx.clear_committed()

    def _abort_tree(self) -> None:
        first_error: BaseException | None = None
        try:
            for child in self._children.values():
                try:
                    child._abort_tree()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            try:
                self.journal_tx.discard_open_writers()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            try:
                self.journal_tx.rollback()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
                else:
                    first_error.add_note(f"Additional rollback error: {exc!r}")
            else:
                if first_error is None:
                    self._rollback_done = True
                    self._finalization_failed = False
        finally:
            self._mark_tree_finished()
        if first_error is not None:
            raise first_error

    def _prepare_child_participant(self) -> None:
        if self._parent is None:
            raise TransactionStateError("Only coordinated child transactions can prepare as participants")
        self._prepare_tree()
        child_repo_path = self.repo._child_repo_path
        assert child_repo_path is not None
        self._parent.journal_tx.mark_child_prepared(child_repo_path)

    def _reopen_prepared_child(self) -> None:
        if self._parent is None:
            raise TransactionStateError("Only coordinated child transactions can reopen prepared state")
        child_repo_path = self.repo._child_repo_path
        assert child_repo_path is not None
        self.journal_tx.reopen_prepared()
        self._parent.journal_tx.mark_child_open(child_repo_path)
        self._prepared = False

    def _abort_child_participant(self) -> None:
        if self._parent is None:
            raise TransactionStateError("Only coordinated child transactions can abort as participants")
        child_repo_path = self.repo._child_repo_path
        assert child_repo_path is not None
        first_error: BaseException | None = None
        try:
            self._abort_tree()
        except BaseException as exc:
            first_error = exc
        try:
            self._parent.journal_tx.unregister_child_repo(child_repo_path)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
            else:
                first_error.add_note(f"Additional child unenroll error: {exc!r}")
        self._parent._children.pop(self.repo.repo_locator, None)
        self._unbind_tree()
        if first_error is not None:
            raise first_error

    def _detach_tree_for_recovery(self) -> None:
        first_error: BaseException | None = None
        for child in self._children.values():
            try:
                child._detach_tree_for_recovery()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        try:
            self.journal_tx.detach_for_recovery()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
            else:
                first_error.add_note(f"Additional detach-for-recovery error: {exc!r}")
        if first_error is not None:
            raise first_error

    def _mark_tree_finished(self) -> None:
        self._finished = True
        self._prepared = False
        for child in self._children.values():
            child._mark_tree_finished()

    def _assert_all_child_scopes_closed(self) -> None:
        for child in self._children.values():
            if child._scope_depth > 0:
                raise TransactionStateError(
                    "All coordinated child transaction scopes must exit before the root transaction can finish"
                )
            child._assert_all_child_scopes_closed()

    def _add_cleanup_note(
        self,
        primary_error: BaseException,
        *,
        phase: str,
        cleanup_error: BaseException,
    ) -> None:
        primary_error.add_note(
            f"Additional transaction {phase} error during lifecycle cleanup: {cleanup_error!r}"
        )

    def _unbind_tree(self) -> None:
        for child in self._children.values():
            child._unbind_tree()
        self.repo._unbind_transaction(self)
        self._journal_tx = None
        self._scope_depth = 0


__all__ = ["TransactionContext"]
