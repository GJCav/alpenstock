from __future__ import annotations


class StorageError(Exception):
    """Base exception for storage-layer failures."""


class TransactionStateError(StorageError):
    """Raised when a transaction operation is not valid in the current state."""


class HandleStateError(StorageError):
    """Raised when a handle operation is not valid in the current state."""


class KeyNotFoundError(StorageError):
    """Raised when a required logical key is absent."""


class OverlayInvariantError(StorageError):
    """Raised when an overlay contains an entry outside the contract."""


class ValueContractError(StorageError):
    """Raised when a key or value violates the storage value contract."""


class WriteConflictError(StorageError):
    """Raised when a backend cannot grant exclusive writer ownership."""
