from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import median
from typing import Any


@dataclass(slots=True)
class BenchmarkRecord:
    backend: str
    scenario: str
    iteration: int
    artifact_root: str
    blob_size_bytes: int
    workload: dict[str, int]
    phase_ns: dict[str, int]
    derived: dict[str, float]


@dataclass(slots=True)
class BenchmarkSuiteResult:
    environment: dict[str, Any]
    config: dict[str, Any]
    records: list[BenchmarkRecord]
    summary: list[dict[str, Any]]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "config": self.config,
            "records": [asdict(record) for record in self.records],
            "summary": self.summary,
        }


def summarize_records(records: list[BenchmarkRecord]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[BenchmarkRecord]] = {}
    for record in records:
        grouped.setdefault((record.backend, record.scenario), []).append(record)

    summary: list[dict[str, Any]] = []
    for (backend, scenario), group in sorted(grouped.items()):
        total_values = [record.phase_ns["total_ns"] for record in group]
        begin_values = [record.phase_ns.get("begin_ns", 0) for record in group]
        prepare_values = [record.phase_ns.get("prepare_ns", 0) for record in group]
        commit_values = [record.phase_ns.get("commit_ns", record.phase_ns.get("rollback_ns", 0)) for record in group]
        write_rates_phase = [record.derived["blob_write_mib_per_s_phase"] for record in group]
        read_rates_phase = [record.derived["blob_read_mib_per_s_phase"] for record in group]
        small_ops_phase = [record.derived["small_ops_per_s_phase"] for record in group]
        write_rates_amortized = [record.derived["blob_write_mib_per_s_amortized_total"] for record in group]
        read_rates_amortized = [record.derived["blob_read_mib_per_s_amortized_total"] for record in group]
        small_ops_amortized = [record.derived["small_ops_per_s_amortized_total"] for record in group]
        summary.append(
            {
                "backend": backend,
                "backend_label": _backend_label(backend),
                "backend_note": _backend_note(backend),
                "scenario": scenario,
                "iterations": len(group),
                "median_total_ms": _ns_to_ms(int(median(total_values))),
                "min_total_ms": _ns_to_ms(min(total_values)),
                "max_total_ms": _ns_to_ms(max(total_values)),
                "p95_total_ms": _ns_to_ms(_p95(total_values)),
                "median_begin_ms": _ns_to_ms(int(median(begin_values))),
                "median_prepare_ms": _ns_to_ms(int(median(prepare_values))),
                "median_finalize_ms": _ns_to_ms(int(median(commit_values))),
                "median_blob_write_mib_per_s_phase": round(median(write_rates_phase), 3),
                "median_blob_read_mib_per_s_phase": round(median(read_rates_phase), 3),
                "median_small_ops_per_s_phase": round(median(small_ops_phase), 3),
                "median_blob_write_mib_per_s_amortized_total": round(median(write_rates_amortized), 3),
                "median_blob_read_mib_per_s_amortized_total": round(median(read_rates_amortized), 3),
                "median_small_ops_per_s_amortized_total": round(median(small_ops_amortized), 3),
            }
        )
    return summary


def format_summary_table(summary: list[dict[str, Any]]) -> str:
    if not summary:
        return "No benchmark records collected."

    phase_headers = [
        "backend",
        "scenario",
        "iters",
        "median_total_ms",
        "median_begin_ms",
        "median_prepare_ms",
        "median_finalize_ms",
        "median_blob_write_mib_per_s_phase",
        "median_blob_read_mib_per_s_phase",
        "median_small_ops_per_s_phase",
    ]
    amortized_headers = [
        "backend",
        "scenario",
        "iters",
        "median_blob_write_mib_per_s_amortized_total",
        "median_blob_read_mib_per_s_amortized_total",
        "median_small_ops_per_s_amortized_total",
    ]
    phase_rows = []
    amortized_rows = []
    for item in summary:
        phase_rows.append(
            [
                str(item.get("backend_label", item["backend"])),
                str(item["scenario"]),
                str(item["iterations"]),
                f"{item['median_total_ms']:.3f}",
                f"{item['median_begin_ms']:.3f}",
                f"{item['median_prepare_ms']:.3f}",
                f"{item['median_finalize_ms']:.3f}",
                f"{item['median_blob_write_mib_per_s_phase']:.3f}",
                f"{item['median_blob_read_mib_per_s_phase']:.3f}",
                f"{item['median_small_ops_per_s_phase']:.3f}",
            ]
        )
        amortized_rows.append(
            [
                str(item.get("backend_label", item["backend"])),
                str(item["scenario"]),
                str(item["iterations"]),
                f"{item['median_blob_write_mib_per_s_amortized_total']:.3f}",
                f"{item['median_blob_read_mib_per_s_amortized_total']:.3f}",
                f"{item['median_small_ops_per_s_amortized_total']:.3f}",
            ]
        )

    return "\n\n".join(
        [
            "Phase-local metrics\n" + _render_table(phase_headers, phase_rows),
            "Amortized full-lifecycle metrics\n" + _render_table(amortized_headers, amortized_rows),
            "Baseline notes\n" + "\n".join(_baseline_notes(summary)),
        ]
    )


def _ns_to_ms(value: int) -> float:
    return round(value / 1_000_000.0, 3)


def _p95(values: list[int]) -> int:
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95))))
    return ordered[rank]


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [
        max(len(header), *(len(row[index]) for row in rows))
        for index, header in enumerate(headers)
    ]
    rendered = [
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)),
        "  ".join("-" * widths[index] for index in range(len(headers))),
    ]
    for row in rows:
        rendered.append("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    return "\n".join(rendered)


def _backend_label(backend: str) -> str:
    return {
        "fs": "fs",
        "rawfs_direct": "rawfs_direct",
        "rawfs_staged": "rawfs_staged",
    }.get(backend, backend)


def _backend_note(backend: str) -> str:
    return {
        "rawfs_direct": "direct I/O lower bound; not transactional",
        "rawfs_staged": "staged I/O lower bound; not transactional or recoverable",
    }.get(backend, "")


def _baseline_notes(summary: list[dict[str, Any]]) -> list[str]:
    notes = []
    seen = set()
    for item in summary:
        note = item.get("backend_note", "")
        if note and note not in seen:
            notes.append(f"- {item['backend_label']}: {note}")
            seen.add(note)
    if not notes:
        notes.append("- No raw filesystem baselines were included in this run.")
    notes.append("- Compare filesystem staged blob-write phases primarily with rawfs_staged, not rawfs_direct.")
    notes.append("- Use rawfs_direct only as a direct-I/O lower bound, not as a transaction-equivalent baseline.")
    return notes


__all__ = [
    "BenchmarkRecord",
    "BenchmarkSuiteResult",
    "format_summary_table",
    "summarize_records",
]
