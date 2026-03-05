from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
CONFIG = Path(__file__).with_name("pyrightconfig.json")
CASES = Path(__file__).with_name("cases")


@dataclass
class PyrightResult:
    returncode: int
    error_count: int
    rules: set[str]
    messages: list[str]



def _run_pyright(case_file: str) -> PyrightResult:
    pyright_bin = shutil.which("pyright")
    if pyright_bin is None:
        raise RuntimeError("pyright executable not found in PATH")

    command = [
        pyright_bin,
        "--outputjson",
        "-p",
        str(CONFIG),
        str(CASES / case_file),
    ]
    proc = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    payload: dict[str, object]
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            "Failed to parse pyright JSON output. "
            f"stdout={proc.stdout!r}, stderr={proc.stderr!r}"
        ) from exc

    diagnostics = payload.get("generalDiagnostics", [])
    if not isinstance(diagnostics, list):
        diagnostics = []

    errors = [d for d in diagnostics if isinstance(d, dict) and d.get("severity") == "error"]
    rules = {
        d.get("rule")
        for d in errors
        if isinstance(d.get("rule"), str)
    }
    messages = [
        d.get("message", "")
        for d in errors
        if isinstance(d, dict)
    ]

    summary = payload.get("summary", {})
    if isinstance(summary, dict) and isinstance(summary.get("errorCount"), int):
        error_count = int(summary["errorCount"])
    else:
        error_count = len(errors)

    return PyrightResult(
        returncode=proc.returncode,
        error_count=error_count,
        rules=rules, # type: ignore
        messages=messages,
    )


@pytest.mark.parametrize(
    "case_file",
    [
        "pass_import_and_ctor.py",
        "pass_field_helper_args.py",
        "pass_kw_only_required_after_defaults.py",
    ],
)
def test_pyright_positive_cases(case_file: str) -> None:
    result = _run_pyright(case_file)
    assert result.error_count == 0, (
        f"Expected no pyright errors for {case_file}, got {result.error_count}. "
        f"rules={sorted(result.rules)} messages={result.messages}"
    )


@pytest.mark.parametrize(
    ("case_file", "expected_rule", "fallback_substring"),
    [
        ("fail_unknown_ctor_arg.py", "reportCallIssue", "No parameter named"),
        ("fail_helper_hash_type.py", "reportArgumentType", "cannot be assigned"),
        ("fail_call_stage_with_arg.py", "reportCallIssue", "Expected 0 positional arguments"),
        ("fail_spec_on_setattr.py", "reportCallIssue", "No parameter named"),
        ("fail_stage_missing_order.py", "reportCallIssue", "Argument missing for parameter"),
        (
            "fail_kw_only_false_required_after_defaults.py",
            "reportGeneralTypeIssues",
            "Fields without default values cannot appear after fields with default values",
        ),
    ],
)
def test_pyright_negative_cases(
    case_file: str,
    expected_rule: str,
    fallback_substring: str,
) -> None:
    result = _run_pyright(case_file)
    assert result.error_count > 0, f"Expected pyright errors for {case_file}"

    has_rule = expected_rule in result.rules
    has_message = any(fallback_substring in msg for msg in result.messages)
    assert has_rule or has_message, (
        f"Expected rule={expected_rule!r} or message containing {fallback_substring!r} "
        f"for {case_file}. Got rules={sorted(result.rules)} messages={result.messages}"
    )
