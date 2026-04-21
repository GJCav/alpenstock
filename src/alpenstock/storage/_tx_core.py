from __future__ import annotations

from collections.abc import Mapping
from typing import Generic, TypeVar

import attrs

from ._errors import HandleStateError, TransactionStateError, ValueContractError
from ._keys import validate_logical_key
from ._overlay import apply_overlay, put_entry, read_visible
from ._types import (
    CommittedState,
    DELETE,
    LogicalKey,
    OverlayEntry,
    OverlayMap,
    TransactionState,
)

TValueRef = TypeVar("TValueRef")


@attrs.define(slots=True)
class TransactionCore(Generic[TValueRef]):
    """
    Backend-neutral transaction core over a committed base state and an overlay.

    This class models the Phase 2 semantics only. It does not perform persistence,
    file staging, or recovery I/O.
    """

    _base_state: Mapping[LogicalKey, TValueRef] = attrs.field(factory=dict, alias="base_state", repr=False)
    _overlay: OverlayMap[TValueRef] = attrs.field(factory=dict, init=False, repr=False)
    _state: TransactionState = attrs.field(default=TransactionState.OPEN, init=False)
    _prepared_overlay: OverlayMap[TValueRef] | None = attrs.field(default=None, init=False, repr=False)
    _open_writable_keys: set[LogicalKey] = attrs.field(factory=set, init=False, repr=False)

    def __attrs_post_init__(self) -> None:
        if isinstance(self._base_state, dict):
            self._base_state = {
                self._validate_key(key): self._validate_value_ref(value)
                for key, value in self._base_state.items()
            }

    @property
    def state(self) -> TransactionState:
        return self._state

    @property
    def overlay(self) -> OverlayMap[TValueRef]:
        return dict(self._current_overlay())

    @property
    def base_state(self) -> CommittedState[TValueRef]:
        return dict(self._base_state)

    @property
    def prepared_overlay(self) -> OverlayMap[TValueRef] | None:
        if self._prepared_overlay is None:
            return None
        return dict(self._prepared_overlay)

    def read_visible_ref(self, key: LogicalKey) -> TValueRef | None:
        self._require_active("read_visible_ref")
        normalized_key = self._validate_key(key)
        overlay = self._current_overlay()
        return read_visible(self._base_state, overlay, normalized_key)

    def put_ref(self, key: LogicalKey, value_ref: TValueRef) -> None:
        self._require_open("put_ref")
        self._overlay[self._validate_key(key)] = put_entry(self._validate_value_ref(value_ref))

    def delete(self, key: LogicalKey) -> None:
        self._require_open("delete")
        validated_key = self._validate_key(key)
        if validated_key in self._open_writable_keys:
            raise HandleStateError(
                "delete() requires the writable handle for the key to be closed first; "
                f"still open: {validated_key}"
            )
        self._overlay[validated_key] = DELETE

    def prepare(self) -> None:
        self._require_open("prepare")
        if self._open_writable_keys:
            open_keys = ", ".join(sorted(self._open_writable_keys))
            raise HandleStateError(
                "prepare() requires all writable handles to be closed before prepare; "
                f"still open: {open_keys}"
            )
        self._prepared_overlay = dict(self._overlay)
        self._state = TransactionState.PREPARED

    def commit(self) -> None:
        if self._state is not TransactionState.PREPARED:
            raise TransactionStateError(
                f"commit() requires state 'prepared', got {self._state.value!r}"
            )
        assert self._prepared_overlay is not None
        committed = apply_overlay(self._base_state, self._prepared_overlay)
        self._base_state = committed
        self._overlay.clear()
        self._prepared_overlay = None
        self._state = TransactionState.COMMITTED

    def reopen_prepared(self) -> None:
        if self._state is not TransactionState.PREPARED:
            raise TransactionStateError(
                f"reopen_prepared() requires state 'prepared', got {self._state.value!r}"
            )
        assert self._prepared_overlay is not None
        self._overlay = dict(self._prepared_overlay)
        self._prepared_overlay = None
        self._state = TransactionState.OPEN

    def rollback(self) -> None:
        if self._state not in (TransactionState.OPEN, TransactionState.PREPARED):
            raise TransactionStateError(
                f"rollback() requires state 'open' or 'prepared', got {self._state.value!r}"
            )
        self._overlay.clear()
        self._prepared_overlay = None
        self._state = TransactionState.ABORTED

    def committed_state(self) -> CommittedState[TValueRef]:
        return dict(self._base_state)

    def register_writable_handle(self, key: LogicalKey) -> None:
        validated_key = self._validate_key(key)
        self._require_open("register_writable_handle")
        if validated_key in self._open_writable_keys:
            raise HandleStateError(f"Writable handle for key {validated_key!r} is already open")
        self._open_writable_keys.add(validated_key)

    def unregister_writable_handle(self, key: LogicalKey) -> None:
        validated_key = self._validate_key(key)
        self._open_writable_keys.discard(validated_key)

    def _current_overlay(self) -> Mapping[LogicalKey, OverlayEntry[TValueRef]]:
        if self._state is TransactionState.PREPARED:
            assert self._prepared_overlay is not None
            return self._prepared_overlay
        return self._overlay

    def _require_open(self, op_name: str) -> None:
        if self._state is not TransactionState.OPEN:
            raise TransactionStateError(
                f"{op_name}() requires state 'open', got {self._state.value!r}"
            )

    def _require_active(self, op_name: str) -> None:
        if self._state not in (TransactionState.OPEN, TransactionState.PREPARED):
            raise TransactionStateError(
                f"{op_name}() requires an active transaction, got {self._state.value!r}"
            )

    def _validate_key(self, key: LogicalKey) -> LogicalKey:
        return validate_logical_key(key)

    def _validate_value_ref(self, value_ref: TValueRef) -> TValueRef:
        if value_ref is None:
            raise ValueContractError("value refs must not be None")
        return value_ref
