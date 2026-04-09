from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Literal, Protocol, TextIO, cast

from alpenstock.storage import Dir, FileNode, KeyNotFoundError, MappedDir, MappedRepo, Repo, define
from alpenstock.storage.backends.fs import FilesystemBackend
from alpenstock.storage.backends.sqlite import SqliteBackend

from .report import BenchmarkRecord

BackendName = Literal["fs", "sqlite", "rawfs_direct", "rawfs_staged"]
BackendChoice = Literal["fs", "sqlite", "rawfs", "rawfs_direct", "rawfs_staged"]


class _RawFsNode(Protocol):
    def __truediv__(self, key: str) -> _RawFsNode: ...
    def write_text(self, data: str, *, encoding: str = "utf-8") -> int: ...
    def open(self, mode: str, *, encoding: str | None = None) -> TextIO | BinaryIO: ...
    def unlink(self) -> None: ...


class _RawFsRoot(Protocol):
    def __truediv__(self, key: str) -> _RawFsNode: ...
ScenarioName = Literal["commit", "rollback"]

_CHUNK_SIZE = 4 * 1024 * 1024
_SMALL_READ_KEYS = 6
_SMALL_WRITE_KEYS = 3
_SMALL_APPEND_KEYS = 1
_SMALL_DELETE_KEYS = 1
_NESTED_SMALL_OPS = 4


@define
class ConfigDir(Dir):
    app: FileNode
    runtime: FileNode


@define
class DatasetDir(Dir):
    meta: FileNode
    blob: FileNode


@define
class SessionDir(Dir):
    state: FileNode
    events: FileNode


@define
class UserRepo(Repo):
    profile: FileNode
    prefs: FileNode
    sessions: MappedDir[SessionDir]


@define
class ArtifactDir(Dir):
    index: FileNode
    payload: FileNode


@define
class ProjectRepo(Repo):
    readme: FileNode
    artifacts: MappedDir[ArtifactDir]


@define
class WorkspaceRepo(Repo):
    manifest: FileNode
    settings: ConfigDir
    datasets: MappedDir[DatasetDir]
    users: MappedRepo[UserRepo]
    projects: MappedRepo[ProjectRepo]


@dataclass(slots=True)
class BenchmarkCaseResult:
    record: BenchmarkRecord
    artifact_root: Path
    repo_locator: str


def benchmark_workload(blob_size_bytes: int) -> dict[str, int]:
    return {
        "small_read_count": _SMALL_READ_KEYS,
        "small_write_count": _SMALL_WRITE_KEYS,
        "small_append_count": _SMALL_APPEND_KEYS,
        "small_delete_count": _SMALL_DELETE_KEYS,
        "nested_small_op_count": _NESTED_SMALL_OPS,
        "small_op_count_total": _SMALL_READ_KEYS + _SMALL_WRITE_KEYS + _SMALL_APPEND_KEYS + _SMALL_DELETE_KEYS + _NESTED_SMALL_OPS,
        "blob_write_bytes": blob_size_bytes,
        "blob_read_bytes": blob_size_bytes,
    }


def setup_seed_state(
    backend_name: BackendName,
    artifact_root: Path,
    *,
    blob_size_bytes: int,
    seed: int,
) -> str:
    artifact_root.mkdir(parents=True, exist_ok=True)
    if _is_rawfs_backend(backend_name):
        workspace_root = artifact_root / "workspace"
        _setup_seed_state_rawfs(workspace_root, blob_size_bytes=blob_size_bytes, seed=seed)
        return str(workspace_root)

    backend = _make_backend(backend_name)
    repo_locator = _repo_locator(backend_name, artifact_root)
    repo = WorkspaceRepo.open(repo_locator, backend=backend)
    _setup_seed_state_storage(repo, blob_size_bytes=blob_size_bytes, seed=seed)
    return repo_locator


def run_benchmark_case(
    backend_name: BackendName,
    scenario: ScenarioName,
    *,
    iteration_root: Path,
    iteration: int,
    blob_size_bytes: int,
    seed: int,
) -> BenchmarkCaseResult:
    repo_locator = setup_seed_state(
        backend_name,
        iteration_root,
        blob_size_bytes=blob_size_bytes,
        seed=seed,
    )
    if _is_rawfs_backend(backend_name):
        record = _run_rawfs_case(
            root=Path(repo_locator),
            backend_name=backend_name,
            scenario=scenario,
            iteration=iteration,
            blob_size_bytes=blob_size_bytes,
            seed=seed,
        )
    else:
        backend = _make_backend(backend_name)
        repo = WorkspaceRepo.open(repo_locator, backend=backend)
        record = _run_storage_case(
            repo=repo,
            backend_name=backend_name,
            scenario=scenario,
            artifact_root=iteration_root,
            iteration=iteration,
            blob_size_bytes=blob_size_bytes,
            seed=seed,
        )
    return BenchmarkCaseResult(record=record, artifact_root=iteration_root, repo_locator=repo_locator)


def validate_commit_state(backend_name: BackendName, repo_locator: str) -> None:
    if _is_rawfs_backend(backend_name):
        root = Path(repo_locator)
        assert (root / "scratch" / "transient.log").exists() is False
        assert (root / "datasets" / "dataset-0" / "blob").stat().st_size > 0
        return

    backend = _make_backend(backend_name)
    repo = WorkspaceRepo.open(repo_locator, backend=backend)
    assert "bench-commit" in repo.manifest.read_text()
    assert repo.settings.runtime.read_text() == _small_text_payload("settings-runtime-commit", 4096, 17)
    try:
        repo.file("scratch/transient.log").read_text()
    except KeyNotFoundError:
        pass
    else:
        raise AssertionError("scratch/transient.log should have been deleted by the commit scenario")
    assert repo.projects["project-0"].artifacts["artifact-1"].payload.read_text() == _small_text_payload(
        "artifact-payload-commit", 2048, 23
    )
    assert repo.datasets["dataset-0"].blob.read_bytes()[:16] == _blob_chunk(seed=71, chunk_size=16)


def validate_rollback_state(backend_name: BackendName, repo_locator: str) -> None:
    if _is_rawfs_backend(backend_name):
        root = Path(repo_locator)
        assert "bench-commit" not in (root / "manifest").read_text(encoding="utf-8")
        assert (root / "settings" / "runtime").read_text(encoding="utf-8") == _small_text_payload(
            "settings-runtime-seed", 4096, 3
        )
        assert (root / "scratch" / "transient.log").read_text(encoding="utf-8") == _small_text_payload(
            "transient-seed", 1024, 4
        )
        return

    backend = _make_backend(backend_name)
    repo = WorkspaceRepo.open(repo_locator, backend=backend)
    assert "bench-commit" not in repo.manifest.read_text()
    assert repo.settings.runtime.read_text() == _small_text_payload("settings-runtime-seed", 4096, 3)
    assert repo.file("scratch/transient.log").read_text() == _small_text_payload("transient-seed", 1024, 4)
    assert repo.projects["project-0"].artifacts["artifact-1"].payload.read_text() == _small_text_payload(
        "artifact-payload-seed-0-1",
        2048,
        101,
    )


def _run_storage_case(
    *,
    repo: WorkspaceRepo,
    backend_name: BackendName,
    scenario: ScenarioName,
    artifact_root: Path,
    iteration: int,
    blob_size_bytes: int,
    seed: int,
) -> BenchmarkRecord:
    workload = benchmark_workload(blob_size_bytes)
    phase_ns: dict[str, int] = {}
    total_start = time.perf_counter_ns()

    tx = repo.transaction()
    begin_start = time.perf_counter_ns()
    tx.__enter__()
    phase_ns["begin_ns"] = time.perf_counter_ns() - begin_start

    phase_ns["small_read_ns"] = _time_ns(lambda: _run_small_reads_storage(repo))
    phase_ns["small_write_ns"] = _time_ns(lambda: _run_small_writes_storage(repo))
    phase_ns["small_append_ns"] = _time_ns(lambda: _run_small_append_storage(repo))
    phase_ns["small_delete_ns"] = _time_ns(lambda: _run_small_delete_storage(repo))
    phase_ns["nested_repo_ns"] = _time_ns(lambda: _run_nested_ops_storage(repo))
    phase_ns["large_blob_write_ns"] = _time_ns(
        lambda: _stream_write_storage(repo.datasets["dataset-0"].blob, blob_size_bytes, _blob_chunk(seed=71))
    )
    phase_ns["large_blob_read_ns"] = _time_ns(lambda: _stream_read_storage(repo.datasets["dataset-1"].blob))

    try:
        if scenario == "commit":
            prepare_start = time.perf_counter_ns()
            tx._prepare_tree()
            phase_ns["prepare_ns"] = time.perf_counter_ns() - prepare_start

            commit_start = time.perf_counter_ns()
            tx._commit_tree()
            phase_ns["commit_ns"] = time.perf_counter_ns() - commit_start
            tx._mark_tree_finished()
        else:
            rollback_start = time.perf_counter_ns()
            tx.rollback()
            phase_ns["rollback_ns"] = time.perf_counter_ns() - rollback_start
    finally:
        tx._unbind_tree()

    phase_ns["total_ns"] = time.perf_counter_ns() - total_start
    return BenchmarkRecord(
        backend=backend_name,
        scenario=scenario,
        iteration=iteration,
        artifact_root=str(artifact_root),
        blob_size_bytes=blob_size_bytes,
        workload=workload,
        phase_ns=phase_ns,
        derived=_derive_metrics(phase_ns, workload=workload),
    )


def _run_rawfs_case(
    *,
    root: Path,
    backend_name: BackendName,
    scenario: ScenarioName,
    iteration: int,
    blob_size_bytes: int,
    seed: int,
) -> BenchmarkRecord:
    workload = benchmark_workload(blob_size_bytes)
    phase_ns: dict[str, int] = {"begin_ns": 0}
    total_start = time.perf_counter_ns()
    staged = _RawFsStaging(root) if backend_name == "rawfs_staged" else None
    writer: _RawFsRoot = staged if staged is not None else cast(_RawFsRoot, root)
    phase_ns["small_read_ns"] = _time_ns(lambda: _run_small_reads_rawfs(root))
    phase_ns["small_write_ns"] = _time_ns(lambda: _run_small_writes_rawfs(writer))
    phase_ns["small_append_ns"] = _time_ns(lambda: _run_small_append_rawfs(writer))
    phase_ns["small_delete_ns"] = _time_ns(lambda: _run_small_delete_rawfs(writer))
    phase_ns["nested_repo_ns"] = _time_ns(lambda: _run_nested_ops_rawfs(writer))
    phase_ns["large_blob_write_ns"] = _time_ns(
        lambda: _stream_write_rawfs_target(
            writer / "datasets" / "dataset-0" / "blob",
            blob_size_bytes,
            _blob_chunk(seed=71),
        )
    )
    phase_ns["large_blob_read_ns"] = _time_ns(lambda: _stream_read_path(root / "datasets" / "dataset-1" / "blob"))
    phase_ns["prepare_ns"] = 0
    if scenario == "commit":
        phase_ns["commit_ns"] = _time_ns(staged.commit) if staged is not None else 0
    else:
        phase_ns["rollback_ns"] = _time_ns(staged.rollback) if staged is not None else 0
    phase_ns["total_ns"] = time.perf_counter_ns() - total_start
    return BenchmarkRecord(
        backend=backend_name,
        scenario=scenario,
        iteration=iteration,
        artifact_root=str(root.parent),
        blob_size_bytes=blob_size_bytes,
        workload=workload,
        phase_ns=phase_ns,
        derived=_derive_metrics(phase_ns, workload=workload),
    )


def _run_small_reads_storage(repo: WorkspaceRepo) -> None:
    repo.manifest.read_text()
    repo.settings.app.read_text()
    repo.datasets["dataset-0"].meta.read_text()
    repo.users["user-0"].profile.read_text()
    repo.users["user-1"].sessions["session-0"].state.read_text()
    repo.projects["project-1"].artifacts["artifact-0"].index.read_text()


def _run_small_writes_storage(repo: WorkspaceRepo) -> None:
    repo.manifest.write_text(_small_text_payload("manifest-commit", 2048, 11))
    repo.settings.runtime.write_text(_small_text_payload("settings-runtime-commit", 4096, 17))
    repo.datasets["dataset-0"].meta.write_text(_small_text_payload("dataset-meta-commit", 1536, 19))


def _run_small_append_storage(repo: WorkspaceRepo) -> None:
    with repo.manifest.open("a", encoding="utf-8") as handle:
        handle.write("\n" + _small_text_payload("bench-commit-append", 512, 13))


def _run_small_delete_storage(repo: WorkspaceRepo) -> None:
    repo.file("scratch/transient.log").delete()


def _run_nested_ops_storage(repo: WorkspaceRepo) -> None:
    with repo.users["user-1"].transaction():
        repo.users["user-1"].profile.write_text(_small_text_payload("user-profile-commit", 1024, 21))
        repo.users["user-1"].sessions["session-1"].events.write_text(_small_text_payload("session-events", 2048, 22))
    repo.projects["project-0"].artifacts["artifact-1"].payload.write_text(
        _small_text_payload("artifact-payload-commit", 2048, 23)
    )
    repo.users["user-2"].prefs.write_text(_small_text_payload("prefs-commit", 1024, 24))


def _run_small_reads_rawfs(root: Path) -> None:
    (root / "manifest").read_text(encoding="utf-8")
    (root / "settings" / "app").read_text(encoding="utf-8")
    (root / "datasets" / "dataset-0" / "meta").read_text(encoding="utf-8")
    (root / "users" / "user-0" / "profile").read_text(encoding="utf-8")
    (root / "users" / "user-1" / "sessions" / "session-0" / "state").read_text(encoding="utf-8")
    (root / "projects" / "project-1" / "artifacts" / "artifact-0" / "index").read_text(encoding="utf-8")


class _RawFsStaging:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.stage_root = root.parent / "rawfs-staged"
        self.stage_root.mkdir(parents=True, exist_ok=True)
        self._deletes: list[Path] = []

    def __truediv__(self, key: str) -> _RawFsNode:
        return _RawFsStagedPath(self, Path(key))

    def resolve(self, relative: Path) -> Path:
        return self.stage_root / relative

    def committed(self, relative: Path) -> Path:
        return self.root / relative

    def record_delete(self, relative: Path) -> None:
        self._deletes.append(relative)

    def commit(self) -> None:
        for relative in self._deletes:
            target = self.root / relative
            if target.exists():
                target.unlink()
        for staged_path in sorted(self.stage_root.rglob("*")):
            if not staged_path.is_file():
                continue
            relative = staged_path.relative_to(self.stage_root)
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            staged_path.replace(target)
        self.rollback()

    def rollback(self) -> None:
        shutil.rmtree(self.stage_root, ignore_errors=True)


class _RawFsStagedPath:
    def __init__(self, staging: _RawFsStaging, relative: Path) -> None:
        self._staging = staging
        self._relative = relative

    def __truediv__(self, key: str) -> _RawFsNode:
        return _RawFsStagedPath(self._staging, self._relative / key)

    def write_text(self, data: str, *, encoding: str = "utf-8") -> int:
        path = self._staging.resolve(self._relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.write_text(data, encoding=encoding)

    def open(self, mode: str, *, encoding: str | None = None) -> TextIO | BinaryIO:
        path = self._staging.resolve(self._relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if "a" in mode and not path.exists():
            committed = self._staging.committed(self._relative)
            if committed.exists():
                shutil.copyfile(committed, path)
        return cast(TextIO | BinaryIO, path.open(mode, encoding=encoding))

    def unlink(self) -> None:
        self._staging.record_delete(self._relative)


def _run_small_writes_rawfs(root: _RawFsRoot) -> None:
    (root / "manifest").write_text(_small_text_payload("manifest-commit", 2048, 11), encoding="utf-8")
    (root / "settings" / "runtime").write_text(
        _small_text_payload("settings-runtime-commit", 4096, 17),
        encoding="utf-8",
    )
    (root / "datasets" / "dataset-0" / "meta").write_text(
        _small_text_payload("dataset-meta-commit", 1536, 19),
        encoding="utf-8",
    )


def _run_small_append_rawfs(root: _RawFsRoot) -> None:
    handle = cast(TextIO, (root / "manifest").open("a", encoding="utf-8"))
    with handle:
        handle.write("\n" + _small_text_payload("bench-commit-append", 512, 13))


def _run_small_delete_rawfs(root: _RawFsRoot) -> None:
    (root / "scratch" / "transient.log").unlink()


def _run_nested_ops_rawfs(root: _RawFsRoot) -> None:
    (root / "users" / "user-1" / "profile").write_text(
        _small_text_payload("user-profile-commit", 1024, 21),
        encoding="utf-8",
    )
    (root / "users" / "user-1" / "sessions" / "session-1" / "events").write_text(
        _small_text_payload("session-events", 2048, 22),
        encoding="utf-8",
    )
    (root / "projects" / "project-0" / "artifacts" / "artifact-1" / "payload").write_text(
        _small_text_payload("artifact-payload-commit", 2048, 23),
        encoding="utf-8",
    )
    (root / "users" / "user-2" / "prefs").write_text(
        _small_text_payload("prefs-commit", 1024, 24),
        encoding="utf-8",
    )


def _setup_seed_state_storage(repo: WorkspaceRepo, *, blob_size_bytes: int, seed: int) -> None:
    repo.manifest.write_text(_small_text_payload("manifest-seed", 1024, 1))
    repo.settings.app.write_text(_small_text_payload("settings-app-seed", 2048, 2))
    repo.settings.runtime.write_text(_small_text_payload("settings-runtime-seed", 4096, 3))
    repo.file("scratch/transient.log").write_text(_small_text_payload("transient-seed", 1024, 4))

    for dataset_index in range(2):
        dataset = repo.datasets[f"dataset-{dataset_index}"]
        dataset.meta.write_text(_small_text_payload(f"dataset-meta-seed-{dataset_index}", 1024, 10 + dataset_index))
        _stream_write_storage(dataset.blob, blob_size_bytes, _blob_chunk(seed=seed + dataset_index))

    for user_index in range(3):
        user = repo.users[f"user-{user_index}"]
        user.profile.write_text(_small_text_payload(f"user-profile-seed-{user_index}", 1024, 30 + user_index))
        user.prefs.write_text(_small_text_payload(f"user-prefs-seed-{user_index}", 1024, 40 + user_index))
        for session_index in range(2):
            session = user.sessions[f"session-{session_index}"]
            session.state.write_text(_small_text_payload(f"session-state-{user_index}-{session_index}", 768, 50 + session_index))
            session.events.write_text(
                _small_text_payload(f"session-events-{user_index}-{session_index}", 1536, 60 + session_index)
            )

    for project_index in range(2):
        project = repo.projects[f"project-{project_index}"]
        project.readme.write_text(_small_text_payload(f"project-readme-{project_index}", 2048, 70 + project_index))
        for artifact_index in range(2):
            artifact = project.artifacts[f"artifact-{artifact_index}"]
            artifact.index.write_text(
                _small_text_payload(f"artifact-index-{project_index}-{artifact_index}", 1024, 80 + artifact_index)
            )
            artifact.payload.write_text(
                _small_text_payload(
                    f"artifact-payload-seed-{project_index}-{artifact_index}",
                    2048,
                    100 + artifact_index,
                )
            )


def _setup_seed_state_rawfs(root: Path, *, blob_size_bytes: int, seed: int) -> None:
    (root / "settings").mkdir(parents=True, exist_ok=True)
    (root / "scratch").mkdir(parents=True, exist_ok=True)
    (root / "datasets").mkdir(parents=True, exist_ok=True)
    (root / "users").mkdir(parents=True, exist_ok=True)
    (root / "projects").mkdir(parents=True, exist_ok=True)

    (root / "manifest").write_text(_small_text_payload("manifest-seed", 1024, 1), encoding="utf-8")
    (root / "settings" / "app").write_text(_small_text_payload("settings-app-seed", 2048, 2), encoding="utf-8")
    (root / "settings" / "runtime").write_text(
        _small_text_payload("settings-runtime-seed", 4096, 3),
        encoding="utf-8",
    )
    (root / "scratch" / "transient.log").write_text(_small_text_payload("transient-seed", 1024, 4), encoding="utf-8")

    for dataset_index in range(2):
        dataset_dir = root / "datasets" / f"dataset-{dataset_index}"
        dataset_dir.mkdir(parents=True, exist_ok=True)
        (dataset_dir / "meta").write_text(
            _small_text_payload(f"dataset-meta-seed-{dataset_index}", 1024, 10 + dataset_index),
            encoding="utf-8",
        )
        _stream_write_path(dataset_dir / "blob", blob_size_bytes, _blob_chunk(seed=seed + dataset_index))

    for user_index in range(3):
        user_dir = root / "users" / f"user-{user_index}"
        (user_dir / "sessions").mkdir(parents=True, exist_ok=True)
        (user_dir / "profile").write_text(
            _small_text_payload(f"user-profile-seed-{user_index}", 1024, 30 + user_index),
            encoding="utf-8",
        )
        (user_dir / "prefs").write_text(
            _small_text_payload(f"user-prefs-seed-{user_index}", 1024, 40 + user_index),
            encoding="utf-8",
        )
        for session_index in range(2):
            session_dir = user_dir / "sessions" / f"session-{session_index}"
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "state").write_text(
                _small_text_payload(f"session-state-{user_index}-{session_index}", 768, 50 + session_index),
                encoding="utf-8",
            )
            (session_dir / "events").write_text(
                _small_text_payload(f"session-events-{user_index}-{session_index}", 1536, 60 + session_index),
                encoding="utf-8",
            )

    for project_index in range(2):
        project_dir = root / "projects" / f"project-{project_index}" / "artifacts"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir.parent / "readme").write_text(
            _small_text_payload(f"project-readme-{project_index}", 2048, 70 + project_index),
            encoding="utf-8",
        )
        for artifact_index in range(2):
            artifact_dir = project_dir / f"artifact-{artifact_index}"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "index").write_text(
                _small_text_payload(f"artifact-index-{project_index}-{artifact_index}", 1024, 80 + artifact_index),
                encoding="utf-8",
            )
            (artifact_dir / "payload").write_text(
                _small_text_payload(f"artifact-payload-seed-{project_index}-{artifact_index}", 2048, 100 + artifact_index),
                encoding="utf-8",
            )


def _make_backend(backend_name: BackendName) -> FilesystemBackend | SqliteBackend:
    if backend_name == "fs":
        return FilesystemBackend()
    if backend_name == "sqlite":
        return SqliteBackend()
    raise ValueError(f"Unsupported transactional backend {backend_name!r}")


def _repo_locator(backend_name: BackendName, artifact_root: Path) -> str:
    if backend_name == "fs":
        return str(artifact_root / "workspace")
    if backend_name == "sqlite":
        return str(artifact_root / "workspace.db")
    raise ValueError(f"Raw filesystem baseline uses direct paths, not repo locators: {backend_name!r}")


def normalize_backend_name(backend_name: BackendChoice) -> BackendName:
    if backend_name == "rawfs":
        return "rawfs_direct"
    return backend_name


def _is_rawfs_backend(backend_name: BackendName) -> bool:
    return backend_name in {"rawfs_direct", "rawfs_staged"}


def _blob_chunk(*, seed: int, chunk_size: int = _CHUNK_SIZE) -> bytes:
    digest = hashlib.sha256(f"alpenstock-storage-bench:{seed}".encode("utf-8")).digest()
    repeats = (chunk_size // len(digest)) + 1
    return (digest * repeats)[:chunk_size]


def _stream_write_storage(node: FileNode, size_bytes: int, chunk: bytes) -> None:
    with node.open("wb") as handle:
        _stream_write_handle(handle.write, size_bytes, chunk)


def _stream_write_path(path: Path, size_bytes: int, chunk: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        _stream_write_handle(handle.write, size_bytes, chunk)


def _stream_write_rawfs_target(path: _RawFsNode, size_bytes: int, chunk: bytes) -> None:
    handle = cast(BinaryIO, path.open("wb"))
    with handle:
        _stream_write_handle(handle.write, size_bytes, chunk)


def _stream_write_handle(writer: Callable[[bytes], int], size_bytes: int, chunk: bytes) -> None:
    remaining = size_bytes
    while remaining > 0:
        payload = chunk if remaining >= len(chunk) else chunk[:remaining]
        writer(payload)
        remaining -= len(payload)


def _stream_read_storage(node: FileNode) -> int:
    with node.open("rb") as handle:
        return _stream_read_handle(handle.read)


def _stream_read_path(path: Path) -> int:
    with path.open("rb") as handle:
        return _stream_read_handle(handle.read)


def _stream_read_handle(reader: Callable[[int], bytes]) -> int:
    total = 0
    while True:
        payload = reader(_CHUNK_SIZE)
        if not payload:
            return total
        total += len(payload)


def _small_text_payload(label: str, size_bytes: int, seed: int) -> str:
    template = json.dumps(
        {
            "label": label,
            "seed": seed,
            "kind": "alpenstock-storage-bench",
            "body": f"{label}-payload-{seed}",
        },
        sort_keys=True,
    )
    repeats = (size_bytes // len(template)) + 1
    return ((template + "\n") * repeats)[:size_bytes]


def _time_ns(fn: Callable[[], object]) -> int:
    start = time.perf_counter_ns()
    fn()
    return time.perf_counter_ns() - start


def _derive_metrics(phase_ns: dict[str, int], *, workload: dict[str, int]) -> dict[str, float]:
    blob_write_mib = workload["blob_write_bytes"] / (1024 * 1024)
    blob_read_mib = workload["blob_read_bytes"] / (1024 * 1024)
    small_ops = workload["small_op_count_total"]
    small_phase_ns = (
        phase_ns.get("small_read_ns", 0)
        + phase_ns.get("small_write_ns", 0)
        + phase_ns.get("small_append_ns", 0)
        + phase_ns.get("small_delete_ns", 0)
        + phase_ns.get("nested_repo_ns", 0)
    )
    total_ns = max(phase_ns.get("total_ns", 0), 1)
    finalize_key = "commit_ns" if "commit_ns" in phase_ns else "rollback_ns"
    finalize_ns = phase_ns.get(finalize_key, 0)
    prepare_ns = phase_ns.get("prepare_ns", 0)
    return {
        "total_ms": round(total_ns / 1_000_000.0, 3),
        "blob_write_mib_per_s_phase": _rate(blob_write_mib, phase_ns.get("large_blob_write_ns", 0)),
        "blob_read_mib_per_s_phase": _rate(blob_read_mib, phase_ns.get("large_blob_read_ns", 0)),
        "small_ops_per_s_phase": _rate(float(small_ops), small_phase_ns),
        "blob_write_mib_per_s_amortized_total": _rate(blob_write_mib, total_ns),
        "blob_read_mib_per_s_amortized_total": _rate(blob_read_mib, total_ns),
        "small_ops_per_s_amortized_total": _rate(float(small_ops), total_ns),
        "small_reads_per_s_amortized_total": _rate(float(workload["small_read_count"]), total_ns),
        "small_writes_per_s_amortized_total": _rate(float(workload["small_write_count"]), total_ns),
        "small_appends_per_s_amortized_total": _rate(float(workload["small_append_count"]), total_ns),
        "small_deletes_per_s_amortized_total": _rate(float(workload["small_delete_count"]), total_ns),
        "prepare_pct_total": round((prepare_ns / total_ns) * 100.0, 3),
        "finalize_pct_total": round((finalize_ns / total_ns) * 100.0, 3),
    }


def _rate(value: float, duration_ns: int) -> float:
    if duration_ns <= 0:
        return 0.0
    return round(value / (duration_ns / 1_000_000_000.0), 3)


__all__ = [
    "ArtifactDir",
    "BackendName",
    "BackendChoice",
    "BenchmarkCaseResult",
    "ConfigDir",
    "DatasetDir",
    "ProjectRepo",
    "ScenarioName",
    "SessionDir",
    "UserRepo",
    "WorkspaceRepo",
    "benchmark_workload",
    "normalize_backend_name",
    "run_benchmark_case",
    "setup_seed_state",
    "validate_commit_state",
    "validate_rollback_state",
]
