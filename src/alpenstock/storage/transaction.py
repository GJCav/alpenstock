from __future__ import annotations

from typing import TYPE_CHECKING, overload

import attrs

from ._backend import BackendTransaction
from ._errors import TransactionStateError
from ._handles import BinaryFileHandle, FileHandle, ReadableBinaryMode, ReadableTextMode, TextFileHandle
from ._types import OpenMode

if TYPE_CHECKING:
    from .repo import Repo


@attrs.define(slots=True)
class TransactionContext:
    repo: Repo = attrs.field(repr=False)
    _backend_tx: BackendTransaction | None = attrs.field(default=None, init=False, repr=False)
    _parent: TransactionContext | None = attrs.field(default=None, init=False, repr=False)
    _children: dict[str, TransactionContext] = attrs.field(factory=dict, init=False, repr=False)
    _scope_depth: int = attrs.field(default=0, init=False, repr=False)
    _started: bool = attrs.field(default=False, init=False, repr=False)
    _rollback_done: bool = attrs.field(default=False, init=False, repr=False)
    _finished: bool = attrs.field(default=False, init=False, repr=False)
    _finalization_failed: bool = attrs.field(default=False, init=False, repr=False)
    _recovery_required: bool = attrs.field(default=False, init=False, repr=False)

    @property
    def backend_tx(self) -> BackendTransaction:
        if self._finished:
            if self._recovery_required:
                raise TransactionStateError(
                    "Transaction context is finished and requires backend.recover() on the repo root"
                )
            raise TransactionStateError("Transaction context has already finished")
        if self._backend_tx is None:
            raise TransactionStateError("Transaction context has not been entered")
        return self._backend_tx

    @property
    def parent(self) -> TransactionContext | None:
        return self._parent

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
            self._scope_depth += 1
            return self

        if self._started:
            raise TransactionStateError("Transaction context cannot be entered twice")

        backend_tx = self.repo.backend.begin(self.repo.repo_locator, parent_tx=None)
        self._backend_tx = backend_tx
        self.repo._bind_transaction(self)
        self._started = True
        self._scope_depth = 1
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._parent is not None:
            if self._scope_depth > 0:
                self._scope_depth -= 1
            if exc_type is not None and not self.root._finished:
                try:
                    self.root.rollback()
                except BaseException as cleanup_error:
                    if exc is not None:
                        self._add_cleanup_note(exc, phase="rollback", cleanup_error=cleanup_error)
                    else:
                        raise
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
        return self.backend_tx.open_handle(key, mode, encoding=encoding)

    def delete(self, key: str) -> None:
        self.backend_tx.delete(key)

    def rollback(self) -> None:
        if self._finished:
            if self._recovery_required:
                raise TransactionStateError(
                    "rollback() is invalid after commit finalization failed; run backend.recover() on the repo root"
                )
            raise TransactionStateError("rollback() is invalid after the transaction has finished")
        if self._parent is not None:
            self.root.rollback()
            return
        if not self._started:
            raise TransactionStateError("Transaction context has not been entered")
        if self._rollback_done:
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
            self.backend_tx.rollback()
        finally:
            child_repo_path = self.repo._child_repo_path
            assert child_repo_path is not None
            self._parent.backend_tx.unregister_child_repo(child_repo_path)
            self._parent._children.pop(self.repo.repo_locator, None)
            self.repo._unbind_transaction(self)
            self._backend_tx = None
            self._finished = True

    def _ensure_child_transaction(self, repo: Repo) -> TransactionContext:
        existing = self._children.get(repo.repo_locator)
        if existing is not None:
            return existing
        if repo.active_transaction is not None:
            return repo.active_transaction

        child_backend_tx = repo.backend.begin(repo.repo_locator, parent_tx=self.backend_tx)
        child_tx = TransactionContext(repo=repo)
        child_tx._backend_tx = child_backend_tx
        child_tx._parent = self
        child_tx._started = True
        repo._bind_transaction(child_tx)
        self._children[repo.repo_locator] = child_tx
        return child_tx

    def _prepare_tree(self) -> None:
        self._assert_all_child_scopes_closed()
        for child in self._children.values():
            child._prepare_tree()
            child_repo_path = child.repo._child_repo_path
            assert child_repo_path is not None
            self.backend_tx.mark_child_prepared(child_repo_path)

        self.backend_tx.assert_all_writers_closed()
        self.backend_tx.prepare()

    def _commit_tree(self) -> None:
        for child in self._children.values():
            child._commit_tree()
            child_repo_path = child.repo._child_repo_path
            assert child_repo_path is not None
            self.backend_tx.mark_child_committed(child_repo_path)

        self.backend_tx.commit()

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
                self.backend_tx.discard_open_writers()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            try:
                self.backend_tx.rollback()
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

    def _detach_tree_for_recovery(self) -> None:
        first_error: BaseException | None = None
        for child in self._children.values():
            try:
                child._detach_tree_for_recovery()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        try:
            self.backend_tx.detach_for_recovery()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
            else:
                first_error.add_note(f"Additional detach-for-recovery error: {exc!r}")
        if first_error is not None:
            raise first_error

    def _mark_tree_finished(self) -> None:
        self._finished = True
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
        self._backend_tx = None
        self._scope_depth = 0


__all__ = ["TransactionContext"]
