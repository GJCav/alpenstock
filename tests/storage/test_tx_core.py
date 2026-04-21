from __future__ import annotations

import pytest

from alpenstock.storage import HandleStateError, TransactionStateError, ValueContractError
from alpenstock.storage._tx_core import TransactionCore
from alpenstock.storage._types import DELETE, TransactionState


class OpaqueValueRef:
    def __init__(self, label: str) -> None:
        self.label = label

    def __eq__(self, other: object) -> bool:
        return isinstance(other, OpaqueValueRef) and self.label == other.label


def test_read_visible_ref_uses_base_state_when_overlay_has_no_entry() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})

    assert tx.read_visible_ref("alpha") == b"1"
    assert tx.read_visible_ref("missing") is None


def test_put_overrides_base_state_in_visible_reads() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})

    tx.put_ref("alpha", b"2")
    tx.put_ref("beta", b"3")

    assert tx.read_visible_ref("alpha") == b"2"
    assert tx.read_visible_ref("beta") == b"3"


def test_delete_hides_visible_value() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})

    tx.delete("alpha")

    assert tx.read_visible_ref("alpha") is None
    assert tx.overlay["alpha"] == DELETE


def test_latest_overlay_entry_is_authoritative() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})

    tx.delete("alpha")
    tx.put_ref("alpha", b"2")
    assert tx.read_visible_ref("alpha") == b"2"

    tx.put_ref("alpha", b"3")
    tx.delete("alpha")
    assert tx.read_visible_ref("alpha") is None


def test_prepare_freezes_overlay_and_blocks_mutation() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})
    tx.put_ref("alpha", b"2")

    tx.prepare()

    assert tx.state is TransactionState.PREPARED
    assert tx.prepared_overlay == {"alpha": tx.overlay["alpha"]}

    with pytest.raises(TransactionStateError, match="requires state 'open'"):
        tx.put_ref("beta", b"3")

    with pytest.raises(TransactionStateError, match="requires state 'open'"):
        tx.delete("alpha")


def test_overlay_snapshot_tracks_authoritative_frozen_overlay() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})
    tx.put_ref("alpha", b"2")
    tx.prepare()

    overlay = tx.overlay
    overlay["alpha"] = DELETE

    assert tx.overlay["alpha"] != DELETE
    assert tx.read_visible_ref("alpha") == b"2"


def test_read_visible_ref_uses_frozen_overlay_after_prepare() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})
    tx.put_ref("alpha", b"2")
    tx.prepare()

    assert tx.read_visible_ref("alpha") == b"2"


def test_reopen_prepared_restores_frozen_overlay_as_mutable_state() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})
    tx.put_ref("alpha", b"2")
    tx.prepare()

    tx.reopen_prepared()
    tx.put_ref("beta", b"3")

    assert tx.state is TransactionState.OPEN
    assert tx.prepared_overlay is None
    assert tx.read_visible_ref("alpha") == b"2"
    assert tx.read_visible_ref("beta") == b"3"


def test_commit_requires_prepared_state() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})

    with pytest.raises(TransactionStateError, match="requires state 'prepared'"):
        tx.commit()


def test_commit_applies_only_frozen_overlay_once() -> None:
    tx = TransactionCore(base_state={"alpha": b"1", "beta": b"2", "untouched": b"keep"})
    tx.put_ref("alpha", b"updated")
    tx.delete("beta")
    tx.put_ref("gamma", b"3")
    tx.prepare()

    tx.commit()

    assert tx.state is TransactionState.COMMITTED
    assert tx.committed_state() == {"alpha": b"updated", "gamma": b"3", "untouched": b"keep"}
    assert tx.overlay == {}
    assert tx.prepared_overlay is None

    with pytest.raises(TransactionStateError, match="requires state 'prepared'"):
        tx.commit()


def test_prepare_and_commit_with_empty_overlay_preserves_base_state() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})

    tx.prepare()
    tx.commit()
    assert tx.committed_state() == {"alpha": b"1"}


def test_rollback_from_open_discards_overlay_without_changing_base_state() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})
    tx.put_ref("alpha", b"2")

    tx.rollback()
    assert tx.state is TransactionState.ABORTED
    assert tx.committed_state() == {"alpha": b"1"}
    assert tx.overlay == {}


def test_rollback_from_prepared_discards_frozen_overlay_without_changing_base_state() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})
    tx.put_ref("alpha", b"2")
    tx.prepare()

    tx.rollback()
    assert tx.state is TransactionState.ABORTED
    assert tx.committed_state() == {"alpha": b"1"}
    assert tx.overlay == {}
    assert tx.prepared_overlay is None


def test_reads_are_rejected_after_transaction_ends() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})
    tx.rollback()

    with pytest.raises(TransactionStateError, match="requires an active transaction"):
        tx.read_visible_ref("alpha")


def test_reads_are_rejected_after_commit() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})
    tx.prepare()
    tx.commit()

    with pytest.raises(TransactionStateError, match="requires an active transaction"):
        tx.read_visible_ref("alpha")


def test_prepare_cannot_be_called_twice() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})
    tx.prepare()

    with pytest.raises(TransactionStateError, match="requires state 'open'"):
        tx.prepare()


def test_rollback_after_commit_is_rejected() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})
    tx.prepare()
    tx.commit()

    with pytest.raises(TransactionStateError, match="requires state 'open' or 'prepared'"):
        tx.rollback()


def test_put_ref_is_rejected_after_rollback() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})
    tx.rollback()

    with pytest.raises(TransactionStateError, match="requires state 'open'"):
        tx.put_ref("alpha", b"2")


def test_mutating_input_state_after_construction_does_not_change_transaction_base() -> None:
    base_state = {"alpha": b"1"}
    tx = TransactionCore(base_state=base_state)

    base_state["alpha"] = b"mutated"

    assert tx.read_visible_ref("alpha") == b"1"


def test_mutating_returned_base_state_snapshot_does_not_change_transaction_base() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})

    snapshot = tx.base_state
    snapshot["alpha"] = b"mutated"

    assert tx.read_visible_ref("alpha") == b"1"


def test_put_ref_still_rejects_non_str_keys() -> None:
    tx = TransactionCore(base_state={})

    with pytest.raises(ValueContractError, match="logical keys must be str"):
        tx.put_ref(123, OpaqueValueRef("candidate"))  # type: ignore[arg-type]

    with pytest.raises(ValueContractError, match="logical keys must be str"):
        TransactionCore(base_state={123: OpaqueValueRef("bad")})  # type: ignore[arg-type]


def test_logical_keys_are_canonicalized_in_base_state_and_overlay() -> None:
    tx = TransactionCore(base_state={"alpha//beta": b"1"})

    assert tx.read_visible_ref("alpha/beta") == b"1"
    assert tx.base_state == {"alpha/beta": b"1"}

    tx.put_ref("./gamma", b"2")
    assert tx.overlay == {"gamma": tx.overlay["gamma"]}
    assert tx.read_visible_ref("gamma") == b"2"


def test_snapshot_properties_return_defensive_copies() -> None:
    tx = TransactionCore(base_state={"alpha": b"1"})
    tx.put_ref("alpha", b"2")
    tx.prepare()

    base_state = tx.base_state
    overlay = tx.overlay
    prepared_overlay = tx.prepared_overlay

    base_state["alpha"] = b"mutated"
    overlay["alpha"] = DELETE
    assert prepared_overlay is not None
    prepared_overlay["alpha"] = DELETE

    assert tx.base_state == {"alpha": b"1"}
    assert tx.overlay["alpha"] != DELETE
    assert tx.prepared_overlay == {"alpha": tx.overlay["alpha"]}


def test_core_supports_opaque_backend_owned_value_refs() -> None:
    old_ref = OpaqueValueRef("old")
    new_ref = OpaqueValueRef("new")
    tx = TransactionCore(base_state={"alpha": old_ref})

    tx.put_ref("alpha", new_ref)

    assert tx.read_visible_ref("alpha") == new_ref
    assert tx.committed_state() == {"alpha": old_ref}


def test_core_no_longer_rejects_non_bytes_value_refs() -> None:
    tx = TransactionCore[OpaqueValueRef](base_state={})
    ref = OpaqueValueRef("candidate")

    tx.put_ref("alpha", ref)

    assert tx.read_visible_ref("alpha") == ref


def test_put_ref_rejects_none_value_refs() -> None:
    tx = TransactionCore[OpaqueValueRef](base_state={})

    with pytest.raises(ValueContractError, match="must not be None"):
        tx.put_ref("alpha", None)  # type: ignore[arg-type]


def test_base_state_rejects_none_value_refs() -> None:
    with pytest.raises(ValueContractError, match="must not be None"):
        TransactionCore(base_state={"alpha": None})  # type: ignore[arg-type]


def test_opaque_value_refs_survive_prepare_and_commit() -> None:
    original = OpaqueValueRef("original")
    candidate = OpaqueValueRef("candidate")
    tx = TransactionCore(base_state={"alpha": original})

    tx.put_ref("alpha", candidate)
    tx.prepare()

    assert tx.read_visible_ref("alpha") == candidate
    assert tx.prepared_overlay == {"alpha": tx.overlay["alpha"]}

    tx.commit()

    assert tx.committed_state() == {"alpha": candidate}


def test_delete_is_rejected_while_same_key_has_open_writer_registered() -> None:
    tx = TransactionCore(base_state={"alpha": OpaqueValueRef("original")})
    tx.register_writable_handle("alpha")

    with pytest.raises(HandleStateError, match="requires the writable handle"):
        tx.delete("alpha")
