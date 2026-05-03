from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Self

import attrs

from ._blob_backend import BlobBackend
from ._errors import TransactionStateError
from ._journal_backend import JournalBackend
from ._keys import validate_logical_key
from ._schema import repo_boundary_prefixes
from .dir import Dir

if TYPE_CHECKING:
    from .file import FileNode
    from .transaction import TransactionContext


@attrs.define(slots=True)
class Repo(Dir):
    blob_backend: BlobBackend = attrs.field(repr=False)
    journal_backend: JournalBackend = attrs.field(repr=False)
    repo_locator: str = attrs.field()
    coordination_root_locator: str = attrs.field()
    _active_transaction: TransactionContext | None = attrs.field(default=None, init=False, repr=False)
    _parent_repo: Repo | None = attrs.field(default=None, init=False, repr=False)
    _child_repo_path: str | None = attrs.field(default=None, init=False, repr=False)

    @classmethod
    def open(
        cls,
        path: str,
        *,
        blob_backend: BlobBackend | None = None,
        journal_backend: JournalBackend | None = None,
        coordination_root_locator: str | None = None,
    ) -> Self:
        """Open a repository at ``path``.

        For the filesystem JSONL backend, opening an empty path without existing
        tree metadata initializes that path as a new repo-tree root. If the path
        is intended to be a nested repo, materialize it through the parent schema
        before direct opening so it can discover the correct ancestor lock chain.
        """
        if blob_backend is None:
            from .backends.fs import FilesystemBlobBackend

            blob_backend = FilesystemBlobBackend()
        if journal_backend is None:
            from .backends.fs import JsonlWalJournalBackend

            journal_backend = JsonlWalJournalBackend()
        coordination_root = journal_backend.prepare_repo_open(path, blob_backend)
        if coordination_root_locator is not None:
            from pathlib import Path

            if Path(coordination_root_locator).resolve() != Path(coordination_root).resolve():
                raise TransactionStateError(
                    f"Configured coordination root {coordination_root_locator!r} does not match repo metadata root {coordination_root!r}"
                )
        repo = cls(
            blob_backend=blob_backend,
            journal_backend=journal_backend,
            repo_locator=path,
            coordination_root_locator=coordination_root,
        )
        repo._bind_schema_runtime(parent_repo=None, child_repo_path=None)
        return repo

    def recover(self) -> None:
        self.journal_backend.recover(
            self.repo_locator,
            self.blob_backend,
            coordination_root_locator=self.coordination_root_locator,
        )

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
        """Return a raw file node for internal/debug/testing use.

        Prefer schema-declared ``FileNode`` attributes in application code.
        This raw escape hatch only enforces repo-boundary ownership checks; it
        does not make arbitrary filesystem shape conflicts part of the public
        schema contract.
        """
        from .file import FileNode

        return FileNode(repo=self, key=self._assert_raw_key_allowed(key))

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

    def _assert_raw_key_allowed(self, key: str) -> str:
        normalized_key = validate_logical_key(key)
        key_path = PurePosixPath(normalized_key)
        for boundary in repo_boundary_prefixes(type(self)):
            boundary_path = PurePosixPath(boundary)
            if key_path == boundary_path or boundary_path in key_path.parents:
                raise TransactionStateError(
                    f"Raw file key {normalized_key!r} crosses nested repo boundary {boundary!r}"
                )
        return normalized_key

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
