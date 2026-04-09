from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .report import BenchmarkSuiteResult, format_summary_table, summarize_records
from .scenario import BackendName, ScenarioName, normalize_backend_name, run_benchmark_case


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Microbenchmark the alpenstock storage backends.")
    parser.add_argument(
        "--backend",
        choices=["fs", "sqlite", "rawfs", "rawfs_direct", "rawfs_staged", "rawfs_all", "all"],
        default="all",
    )
    parser.add_argument("--scenario", choices=["commit", "rollback", "all"], default="all")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--blob-size-mib", type=int, default=256)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument("--seed", type=int, default=20260411)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.backend == "all":
        backend_names: list[BackendName] = ["fs", "sqlite", "rawfs_direct", "rawfs_staged"]
    elif args.backend == "rawfs_all":
        backend_names = ["rawfs_direct", "rawfs_staged"]
    else:
        backend_names = [normalize_backend_name(args.backend)]
    scenario_names: list[ScenarioName] = ["commit", "rollback"] if args.scenario == "all" else [args.scenario]
    blob_size_bytes = args.blob_size_mib * 1024 * 1024

    run_root = Path(tempfile.mkdtemp(prefix="alpenstock-storage-bench-"))
    records = []
    try:
        for backend_name in backend_names:
            for scenario_name in scenario_names:
                for warmup_index in range(args.warmups):
                    case_root = run_root / "warmups" / backend_name / scenario_name / f"warmup-{warmup_index}"
                    result = run_benchmark_case(
                        backend_name,
                        scenario_name,
                        iteration_root=case_root,
                        iteration=warmup_index,
                        blob_size_bytes=blob_size_bytes,
                        seed=args.seed + warmup_index,
                    )
                    if not args.keep_artifacts:
                        shutil.rmtree(result.artifact_root, ignore_errors=True)
                for iteration in range(args.iterations):
                    case_root = run_root / "runs" / backend_name / scenario_name / f"iter-{iteration}"
                    result = run_benchmark_case(
                        backend_name,
                        scenario_name,
                        iteration_root=case_root,
                        iteration=iteration,
                        blob_size_bytes=blob_size_bytes,
                        seed=args.seed + args.warmups + iteration,
                    )
                    records.append(result.record)
                    if not args.keep_artifacts:
                        shutil.rmtree(result.artifact_root, ignore_errors=True)

        suite = BenchmarkSuiteResult(
            environment=_collect_environment_metadata(run_root),
            config={
                "backends": backend_names,
                "scenarios": scenario_names,
                "iterations": args.iterations,
                "warmups": args.warmups,
                "blob_size_mib": args.blob_size_mib,
                "seed": args.seed,
            },
            records=records,
            summary=summarize_records(records),
        )
        print(format_summary_table(suite.summary))
        if args.keep_artifacts:
            print(f"\nArtifacts kept at: {run_root}")
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(suite.to_json_dict(), indent=2), encoding="utf-8")
            print(f"\nJSON report written to: {args.output}")
        return 0
    finally:
        if not args.keep_artifacts:
            shutil.rmtree(run_root, ignore_errors=True)


def _collect_environment_metadata(run_root: Path) -> dict[str, Any]:
    return {
        "python_version": sys.version,
        "platform": platform.platform(),
        "cpu": _cpu_name(),
        "run_root": str(run_root),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit_hash(),
        "pid": os.getpid(),
    }


def _cpu_name() -> str:
    cpu = platform.processor().strip()
    if cpu:
        return cpu
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.lower().startswith("model name"):
                _, _, value = line.partition(":")
                return value.strip()
    return "unknown"


def _git_commit_hash() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    return completed.stdout.strip() or None


if __name__ == "__main__":
    raise SystemExit(main())
