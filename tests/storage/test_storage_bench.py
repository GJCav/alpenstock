from __future__ import annotations

import json
from pathlib import Path

from benchmarks.storage_bench.report import BenchmarkSuiteResult, format_summary_table, summarize_records
from benchmarks.storage_bench.runner import build_parser
from benchmarks.storage_bench.scenario import (
    run_benchmark_case,
    setup_seed_state,
    validate_commit_state,
    validate_rollback_state,
)


def test_setup_seed_state_builds_workspace_for_fs(tmp_path: Path) -> None:
    fs_root = tmp_path / "fs"

    fs_locator = setup_seed_state("fs", fs_root, blob_size_bytes=1024 * 1024, seed=7)

    assert Path(fs_locator).exists()


def test_rawfs_direct_commit_case_produces_expected_final_tree(tmp_path: Path) -> None:
    result = run_benchmark_case(
        "rawfs_direct",
        "commit",
        iteration_root=tmp_path / "rawfs-commit",
        iteration=0,
        blob_size_bytes=1024 * 1024,
        seed=11,
    )

    workspace_root = Path(result.repo_locator)
    assert (workspace_root / "manifest").exists()
    assert not (workspace_root / "scratch" / "transient.log").exists()
    assert (workspace_root / "datasets" / "dataset-0" / "blob").stat().st_size == 1024 * 1024


def test_rawfs_staged_commit_publishes_and_cleans_staged_artifacts(tmp_path: Path) -> None:
    result = run_benchmark_case(
        "rawfs_staged",
        "commit",
        iteration_root=tmp_path / "rawfs-staged-commit",
        iteration=0,
        blob_size_bytes=1024 * 1024,
        seed=12,
    )

    validate_commit_state("rawfs_staged", result.repo_locator)
    assert not (Path(result.repo_locator).parent / "rawfs-staged").exists()


def test_rawfs_staged_rollback_leaves_seed_tree_and_cleans_staged_artifacts(tmp_path: Path) -> None:
    result = run_benchmark_case(
        "rawfs_staged",
        "rollback",
        iteration_root=tmp_path / "rawfs-staged-rollback",
        iteration=0,
        blob_size_bytes=1024 * 1024,
        seed=12,
    )

    validate_rollback_state("rawfs_staged", result.repo_locator)
    assert not (Path(result.repo_locator).parent / "rawfs-staged").exists()


def test_transactional_commit_and_rollback_cases_leave_expected_state(tmp_path: Path) -> None:
    commit_result = run_benchmark_case(
        "fs",
        "commit",
        iteration_root=tmp_path / "fs-commit",
        iteration=0,
        blob_size_bytes=1024 * 1024,
        seed=13,
    )
    rollback_result = run_benchmark_case(
        "fs",
        "rollback",
        iteration_root=tmp_path / "fs-rollback",
        iteration=0,
        blob_size_bytes=1024 * 1024,
        seed=14,
    )

    assert commit_result.record.phase_ns["commit_ns"] >= 0
    assert commit_result.record.phase_ns["prepare_ns"] >= 0
    validate_rollback_state("fs", rollback_result.repo_locator)


def test_report_json_and_summary_are_parseable(tmp_path: Path) -> None:
    records = [
        run_benchmark_case(
            "fs",
            "commit",
            iteration_root=tmp_path / "iter-0",
            iteration=0,
            blob_size_bytes=1024 * 1024,
            seed=21,
        ).record
    ]
    suite = BenchmarkSuiteResult(
        environment={"python_version": "test"},
        config={"iterations": 1},
        records=records,
        summary=summarize_records(records),
    )

    payload = suite.to_json_dict()
    parsed = json.loads(json.dumps(payload))

    assert "records" in parsed
    assert "summary" in parsed
    table = format_summary_table(suite.summary)
    assert "Phase-local metrics" in table
    assert "Amortized full-lifecycle metrics" in table
    assert "median_total_ms" in table
    assert "median_blob_write_mib_per_s_amortized_total" in table


def test_cli_parser_and_timing_keys() -> None:
    parser = build_parser()
    args = parser.parse_args(["--backend", "rawfs_staged", "--scenario", "rollback", "--iterations", "2", "--blob-size-mib", "8"])

    assert args.backend == "rawfs_staged"
    assert args.scenario == "rollback"
    assert args.iterations == 2
    assert args.blob_size_mib == 8


def test_benchmark_record_contains_required_phase_keys(tmp_path: Path) -> None:
    record = run_benchmark_case(
        "fs",
        "commit",
        iteration_root=tmp_path / "iter-1",
        iteration=1,
        blob_size_bytes=1024 * 1024,
        seed=31,
    ).record

    required = {
        "begin_ns",
        "small_read_ns",
        "small_write_ns",
        "small_append_ns",
        "small_delete_ns",
        "nested_repo_ns",
        "large_blob_write_ns",
        "large_blob_read_ns",
        "prepare_ns",
        "commit_ns",
        "total_ns",
    }
    assert required.issubset(record.phase_ns)
    assert record.workload["small_op_count_total"] > 0
    assert record.workload["blob_write_bytes"] == 1024 * 1024
    assert "blob_write_mib_per_s_phase" in record.derived
    assert "blob_write_mib_per_s_amortized_total" in record.derived
    assert "small_ops_per_s_phase" in record.derived
    assert "small_ops_per_s_amortized_total" in record.derived


def test_rawfs_staged_record_contains_required_phase_keys(tmp_path: Path) -> None:
    record = run_benchmark_case(
        "rawfs_staged",
        "commit",
        iteration_root=tmp_path / "iter-staged",
        iteration=1,
        blob_size_bytes=1024 * 1024,
        seed=33,
    ).record

    assert record.backend == "rawfs_staged"
    required = {
        "begin_ns",
        "small_read_ns",
        "small_write_ns",
        "small_append_ns",
        "small_delete_ns",
        "nested_repo_ns",
        "large_blob_write_ns",
        "large_blob_read_ns",
        "prepare_ns",
        "commit_ns",
        "total_ns",
    }
    assert required.issubset(record.phase_ns)
    assert record.workload["small_op_count_total"] > 0


def test_amortized_metrics_use_total_time_denominator(tmp_path: Path) -> None:
    record = run_benchmark_case(
        "fs",
        "commit",
        iteration_root=tmp_path / "iter-2",
        iteration=2,
        blob_size_bytes=1024 * 1024,
        seed=37,
    ).record

    assert record.derived["blob_write_mib_per_s_amortized_total"] <= record.derived["blob_write_mib_per_s_phase"]
    assert record.derived["blob_read_mib_per_s_amortized_total"] <= record.derived["blob_read_mib_per_s_phase"]
    assert record.derived["small_ops_per_s_amortized_total"] <= record.derived["small_ops_per_s_phase"]


def test_summary_includes_phase_and_amortized_medians(tmp_path: Path) -> None:
    records = [
        run_benchmark_case(
            "rawfs_direct",
            "rollback",
            iteration_root=tmp_path / "iter-3",
            iteration=0,
            blob_size_bytes=1024 * 1024,
            seed=41,
        ).record
    ]
    summary = summarize_records(records)

    assert len(summary) == 1
    item = summary[0]
    assert "median_blob_write_mib_per_s_phase" in item
    assert "median_blob_write_mib_per_s_amortized_total" in item
    assert "median_small_ops_per_s_phase" in item
    assert "median_small_ops_per_s_amortized_total" in item
