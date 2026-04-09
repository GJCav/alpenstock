from __future__ import annotations

from typing import TYPE_CHECKING, Self

import attrs

from ._backend import Backend
from ._errors import TransactionStateError
from ._keys import validate_logical_key
from .dir import Dir

if TYPE_CHECKING:
    from .file import FileNode
    from .transaction import TransactionContext


@attrs.define(slots=True)
class Repo(Dir):
    backend: Backend = attrs.field(repr=False)
    repo_locator: str = attrs.field()
    _active_transaction: TransactionContext | None = attrs.field(default=None, init=False, repr=False)
    _parent_repo: Repo | None = attrs.field(default=None, init=False, repr=False)
    _child_repo_path: str | None = attrs.field(default=None, init=False, repr=False)

    @classmethod
    def open(cls, path: str, backend: Backend) -> Self:
        repo = cls(backend=backend, repo_locator=path)
        repo._bind_schema_runtime(parent_repo=None, child_repo_path=None)
        return repo

    def transaction(self) -> TransactionContext:
        from .transaction import TransactionContext

        if self._active_transaction is not None:
            if self._active_transaction.parent is not None:
                return self._active_transaction
            raise TransactionStateError(
                "Starting another transaction on the same repo while one is active is prohibited"
            )

        if self._parent_repo is not None:
            parent = self._parent_repo
            if parent.active_transaction is not None or parent._nearest_active_ancestor_transaction() is not None:
                parent_tx = parent._transaction_for_descendant()
                return parent_tx._ensure_child_transaction(self)

        return TransactionContext(repo=self)

    def file(self, key: str) -> FileNode:
        from .file import FileNode

        return FileNode(repo=self, key=validate_logical_key(key))

    @property
    def active_transaction(self) -> TransactionContext | None:
        return self._active_transaction

    def _bind_transaction(self, tx: TransactionContext) -> None:
        if self._active_transaction is not None and self._active_transaction is not tx:
            raise TransactionStateError(
                "Starting another transaction on the same repo while one is active is prohibited"
            )
        self._active_transaction = tx

    def _unbind_transaction(self, tx: TransactionContext) -> None:
        if self._active_transaction is tx:
            self._active_transaction = None

    def _bind_schema_runtime(self, *, parent_repo: Repo | None, child_repo_path: str | None) -> None:
        self._parent_repo = parent_repo
        self._child_repo_path = child_repo_path
        self._bind(repo=self, prefix="")

    def _nearest_active_ancestor_transaction(self) -> TransactionContext | None:
        current = self._parent_repo
        while current is not None:
            if current._active_transaction is not None:
                return current._active_transaction
            current = current._parent_repo
        return None

    def _transaction_for_descendant(self) -> TransactionContext:
        if self._active_transaction is not None:
            return self._active_transaction
        if self._parent_repo is None:
            raise TransactionStateError(
                "Cannot enroll a descendant repo without an active ancestor transaction"
            )
        parent_tx = self._parent_repo._transaction_for_descendant()
        return parent_tx._ensure_child_transaction(self)


__all__ = ["Repo"]
