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


def _load_pyright_json(stdout: str) -> dict[str, object]:
    # pyright may print nodeenv bootstrap logs to stdout on first run.
    # Keep parsing from each "{" and accept the first valid JSON payload.
    for index, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            payload = json.loads(stdout[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise json.JSONDecodeError("No JSON object found in stdout", stdout, 0)



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
        payload = _load_pyright_json(proc.stdout)
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
        "positive/import_ctor_ok.py",
        "positive/field_helper_args_ok.py",
        "positive/kwonly_required_after_defaults_ok.py",
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
        ("negative/ctor_unknown_arg_err.py", "reportCallIssue", "No parameter named"),
        ("negative/field_helper_hash_type_err.py", "reportArgumentType", "cannot be assigned"),
        ("negative/stage_call_with_arg_err.py", "reportCallIssue", "Expected 0 positional arguments"),
        ("negative/spec_on_setattr_err.py", "reportCallIssue", "No parameter named"),
        ("negative/stage_missing_order_err.py", "reportCallIssue", "Argument missing for parameter"),
        (
            "negative/kwonly_false_required_after_defaults_err.py",
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
