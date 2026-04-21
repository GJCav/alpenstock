from __future__ import annotations

from dataclasses import dataclass
import json
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from ..._errors import TransactionStateError
from ..._types import DELETE, OverlayEntry, Put
from .layout import RepoLayout, STAGED_DIRNAME
from .refs import FsStagedValueRef, FsValueRef, resolve_value_ref_path

WalState = Literal["open", "prepared", "committing"]
_CRASH_AFTER_PUBLICATION_OPS_ENV = "ALPENSTOCK_FS_CRASH_AFTER_PUBLICATION_OPS"
ChildWalState = dict[str, dict[str, bool]]
WalRecord = dict[str, object]


@dataclass(frozen=True, slots=True)
class WalReplay:
    state: WalState
    overlay: dict[str, OverlayEntry[FsValueRef]]
    children: ChildWalState
    parent_repo_locator: str | None


def append_wal_record(layout: RepoLayout, record: WalRecord) -> None:
    path = layout.wal_path
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with path.open("ab") as handle:
        handle.write(line)


def wal_state_record(state: WalState) -> WalRecord:
    return {"kind": "state", "state": state}


def wal_overlay_record(layout: RepoLayout, key: str, entry: OverlayEntry[FsValueRef]) -> WalRecord:
    layout.committed_path(key)
    if entry is DELETE:
        return {"kind": "delete", "key": key}
    assert isinstance(entry, Put)
    if not isinstance(entry.value, FsStagedValueRef):
        raise TransactionStateError("Filesystem WAL can only persist staged candidate value references")
    validated_staged_relpath(layout, entry.value.relpath)
    return {
        "kind": "put",
        "key": key,
        "staged_relpath": entry.value.relpath,
    }


def wal_child_record(layout: RepoLayout, repo_path: str, *, prepared: bool) -> WalRecord:
    layout.committed_path(repo_path)
    return {
        "kind": "child",
        "repo_path": repo_path,
        "prepared": prepared,
    }


def wal_child_unenrolled_record(layout: RepoLayout, repo_path: str) -> WalRecord:
    layout.committed_path(repo_path)
    return {
        "kind": "child_unenrolled",
        "repo_path": repo_path,
    }


def wal_coordinated_child_record(parent_repo_locator: str) -> WalRecord:
    if not parent_repo_locator:
        raise TransactionStateError("Coordinated filesystem child WAL requires a parent repo locator")
    return {
        "kind": "coordinated_child",
        "parent_repo_locator": parent_repo_locator,
    }


def load_wal_replay(layout: RepoLayout) -> WalReplay:
    _truncate_torn_final_record(layout.wal_path)
    state_box: list[WalState] = ["open"]
    overlay: dict[str, OverlayEntry[FsValueRef]] = {}
    children: ChildWalState = {}
    parent_repo_locator_box: list[str | None] = [None]
    with layout.wal_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TransactionStateError(
                    f"Invalid filesystem WAL JSON on line {line_number}: {exc.msg}"
                ) from exc
            if not isinstance(payload, dict):
                raise TransactionStateError(f"Filesystem WAL line {line_number} must be a JSON object")
            _apply_wal_record(
                layout,
                payload,
                state_box=state_box,
                overlay=overlay,
                children=children,
                parent_repo_locator_box=parent_repo_locator_box,
                line_number=line_number,
            )
    return WalReplay(
        state=state_box[0],
        overlay=overlay,
        children=children,
        parent_repo_locator=parent_repo_locator_box[0],
    )


def _truncate_torn_final_record(path: Path) -> None:
    payload = path.read_bytes()
    if not payload or payload.endswith(b"\n"):
        return
    previous_newline = payload.rfind(b"\n")
    truncate_at = 0 if previous_newline < 0 else previous_newline + 1
    with path.open("r+b") as handle:
        handle.truncate(truncate_at)


def load_wal(layout: RepoLayout) -> tuple[WalState, dict[str, OverlayEntry[FsValueRef]], ChildWalState]:
    replay = load_wal_replay(layout)
    return replay.state, replay.overlay, replay.children


def validated_staged_relpath(layout: RepoLayout, staged_relpath: str) -> str:
    relative = PurePosixPath(staged_relpath)
    parts = relative.parts
    if relative.is_absolute() or not parts:
        raise TransactionStateError(f"Invalid staged payload path {staged_relpath!r}")
    if parts[0] != STAGED_DIRNAME or any(part in {"", ".", ".."} for part in parts):
        raise TransactionStateError(f"Invalid staged payload path {staged_relpath!r}")
    path = layout.tx_root.joinpath(*parts)
    if layout.tx_root not in path.parents and path != layout.tx_root:
        raise TransactionStateError(f"Invalid staged payload path {staged_relpath!r}")
    return staged_relpath


def apply_overlay_to_repo(
    layout: RepoLayout,
    overlay: dict[str, OverlayEntry[FsValueRef]],
    *,
    allow_missing_published_puts: bool = False,
    prevalidated: bool = False,
    child_repo_paths: set[str] | None = None,
) -> None:
    path_map = {key: layout.committed_path(key) for key in overlay}
    delete_paths = sorted(
        (path_map[key] for key, entry in overlay.items() if entry is DELETE),
        key=lambda path: (len(path.parts), path.as_posix()),
        reverse=True,
    )
    put_items = sorted(
        ((key, path_map[key], entry) for key, entry in overlay.items() if entry is not DELETE),
        key=lambda item: (len(item[1].parts), item[1].as_posix()),
    )

    if not prevalidated:
        validate_overlay_publication(
            layout,
            overlay,
            allow_missing_published_puts=allow_missing_published_puts,
            child_repo_paths=child_repo_paths,
        )

    for committed_path in delete_paths:
        _remove_path(committed_path, repo_root=layout.repo_root)
        _maybe_crash_after_publication_op()

    for key, committed_path, entry in put_items:
        assert isinstance(entry, Put)
        source_path = resolve_value_ref_path(layout, entry.value)
        if not source_path.exists():
            if committed_path.exists():
                continue
            raise TransactionStateError(f"Missing staged payload for key {key!r}: {source_path}")
        if committed_path.exists() and committed_path.is_dir():
            _remove_path(committed_path, repo_root=layout.repo_root)
        committed_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source_path, committed_path)
        _maybe_crash_after_publication_op()


def _maybe_crash_after_publication_op() -> None:
    if "PYTEST_CURRENT_TEST" not in os.environ:
        return

    remaining_raw = os.environ.get(_CRASH_AFTER_PUBLICATION_OPS_ENV)
    if remaining_raw is None:
        return

    try:
        remaining = int(remaining_raw)
    except ValueError as exc:
        raise TransactionStateError(
            f"Invalid test crash counter {_CRASH_AFTER_PUBLICATION_OPS_ENV}={remaining_raw!r}"
        ) from exc
    if remaining <= 1:
        os._exit(17)
    os.environ[_CRASH_AFTER_PUBLICATION_OPS_ENV] = str(remaining - 1)


def _remove_path(path: Path, *, repo_root: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
        _prune_empty_parents(path.parent, stop=repo_root)
        return

    if path.exists():
        path.unlink()
    _prune_empty_parents(path.parent, stop=repo_root)


def _validate_put_sources(
    put_items: list[tuple[str, Path, OverlayEntry[FsValueRef]]],
    *,
    layout: RepoLayout,
    allow_missing_published_puts: bool,
) -> None:
    for key, committed_path, entry in put_items:
        assert isinstance(entry, Put)
        source_path = resolve_value_ref_path(layout, entry.value)
        if source_path.exists():
            continue
        if allow_missing_published_puts and committed_path.is_file():
            continue
        raise TransactionStateError(f"Missing staged payload for key {key!r}: {source_path}")


def validate_overlay_publication(
    layout: RepoLayout,
    overlay: dict[str, OverlayEntry[FsValueRef]],
    *,
    allow_missing_published_puts: bool = False,
    child_repo_paths: set[str] | None = None,
) -> None:
    path_map = {key: layout.committed_path(key) for key in overlay}
    put_items = [
        (key, path_map[key], entry)
        for key, entry in overlay.items()
        if entry is not DELETE
    ]
    _validate_put_sources(put_items, layout=layout, allow_missing_published_puts=allow_missing_published_puts)
    _validate_put_path_conflicts(put_items)
    _validate_child_repo_boundaries(path_map, layout=layout, child_repo_paths=child_repo_paths or set())


def _validate_put_path_conflicts(
    put_items: list[tuple[str, Path, OverlayEntry[FsValueRef]]],
) -> None:
    sorted_paths = sorted((key, committed_path) for key, committed_path, _entry in put_items)
    for index, (key, committed_path) in enumerate(sorted_paths):
        for other_key, other_path in sorted_paths[index + 1 :]:
            if committed_path == other_path:
                continue
            if committed_path in other_path.parents or other_path in committed_path.parents:
                raise TransactionStateError(
                    f"Prepared filesystem overlay contains conflicting put keys {key!r} and {other_key!r}"
                )


def _validate_child_repo_boundaries(
    path_map: dict[str, Path],
    *,
    layout: RepoLayout,
    child_repo_paths: set[str],
) -> None:
    child_roots = {layout.committed_path(repo_path) for repo_path in child_repo_paths}
    for key, committed_path in path_map.items():
        for child_root in child_roots:
            if (
                committed_path == child_root
                or child_root in committed_path.parents
                or committed_path in child_root.parents
            ):
                raise TransactionStateError(
                    f"Prepared filesystem overlay key {key!r} overlaps enrolled child repo {child_root.relative_to(layout.repo_root).as_posix()!r}"
                )


def _prune_empty_parents(path: Path, *, stop: Path) -> None:
    current = path
    while current != stop and current.exists():
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _apply_wal_record(
    layout: RepoLayout,
    payload: dict[str, Any],
    *,
    state_box: list[WalState],
    overlay: dict[str, OverlayEntry[FsValueRef]],
    children: ChildWalState,
    parent_repo_locator_box: list[str | None],
    line_number: int,
) -> None:
    kind = payload.get("kind")
    if kind == "state":
        raw_state = payload.get("state")
        if raw_state not in {"open", "prepared", "committing"}:
            raise TransactionStateError(f"Unknown filesystem WAL state {raw_state!r} on line {line_number}")
        state_box[0] = cast(WalState, raw_state)
        return

    if kind == "put":
        key = payload.get("key")
        staged_relpath = payload.get("staged_relpath")
        if not isinstance(key, str) or not isinstance(staged_relpath, str):
            raise TransactionStateError(f"Filesystem WAL put record on line {line_number} is malformed")
        layout.committed_path(key)
        validated_staged_relpath(layout, staged_relpath)
        overlay[key] = Put(FsStagedValueRef(staged_relpath))
        return

    if kind == "delete":
        key = payload.get("key")
        if not isinstance(key, str):
            raise TransactionStateError(f"Filesystem WAL delete record on line {line_number} is malformed")
        layout.committed_path(key)
        overlay[key] = DELETE
        return

    if kind == "child":
        repo_path = payload.get("repo_path")
        if not isinstance(repo_path, str):
            raise TransactionStateError(f"Filesystem WAL child record on line {line_number} is malformed")
        prepared = payload.get("prepared", False)
        if not isinstance(prepared, bool):
            raise TransactionStateError(
                f"Filesystem WAL child record on line {line_number} must use a boolean prepared field"
            )
        layout.committed_path(repo_path)
        children[repo_path] = {
            "prepared": prepared,
        }
        return

    if kind == "child_unenrolled":
        repo_path = payload.get("repo_path")
        if not isinstance(repo_path, str):
            raise TransactionStateError(f"Filesystem WAL child_unenrolled record on line {line_number} is malformed")
        layout.committed_path(repo_path)
        children.pop(repo_path, None)
        return

    if kind == "coordinated_child":
        parent_repo_locator = payload.get("parent_repo_locator")
        if not isinstance(parent_repo_locator, str) or not parent_repo_locator:
            raise TransactionStateError(
                f"Filesystem WAL coordinated_child record on line {line_number} is malformed"
            )
        previous = parent_repo_locator_box[0]
        if previous is not None and previous != parent_repo_locator:
            raise TransactionStateError(
                f"Filesystem WAL coordinated_child record on line {line_number} conflicts with earlier parent"
            )
        parent_repo_locator_box[0] = parent_repo_locator
        return

    raise TransactionStateError(f"Unknown filesystem WAL record kind {kind!r} on line {line_number}")


__all__ = [
    "ChildWalState",
    "WalRecord",
    "WalReplay",
    "WalState",
    "append_wal_record",
    "apply_overlay_to_repo",
    "load_wal",
    "load_wal_replay",
    "validate_overlay_publication",
    "validated_staged_relpath",
    "wal_child_record",
    "wal_child_unenrolled_record",
    "wal_coordinated_child_record",
    "wal_overlay_record",
    "wal_state_record",
]
