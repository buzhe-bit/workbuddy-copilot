from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_summary(tmp_path, output_text, *, exit_code="omitted"):
    pytest_output = tmp_path / "pytest-output.txt"
    summary_output = tmp_path / "quality-summary.json"
    pytest_output.write_text(output_text, encoding="utf-8")
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "quality_summary.py"),
        "--pytest-output",
        str(pytest_output),
        "--output",
        str(summary_output),
    ]
    if exit_code != "omitted":
        command.extend(("--pytest-exit-code", str(exit_code)))
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    summary = (
        json.loads(summary_output.read_text(encoding="utf-8"))
        if summary_output.exists()
        else None
    )
    return completed, summary, pytest_output


def test_quality_summary_writes_machine_readable_pytest_counts(tmp_path):
    completed, summary, pytest_output = _run_summary(
        tmp_path,
        ".............. [100%]\n14 passed, 2 skipped, 3 warnings in 1.25s\n",
        exit_code=0,
    )

    assert completed.returncode == 0, completed.stderr
    assert summary == {
        "schema_version": 1,
        "status": "passed",
        "pytest_exit_code": 0,
        "counts": {
            "passed": 14,
            "failed": 0,
            "errors": 0,
            "skipped": 2,
            "xfailed": 0,
            "xpassed": 0,
            "warnings": 3,
        },
        "duration_seconds": 1.25,
        "pytest_output": str(pytest_output),
    }


@pytest.mark.parametrize(
    ("output_text", "exit_code", "expected_counts"),
    [
        (
            "1 failed, 2 passed in 0.20s\n",
            1,
            {"failed": 1, "passed": 2, "errors": 0},
        ),
        (
            "ERROR collecting tests/test_broken.py\n1 error in 0.10s\n",
            2,
            {"failed": 0, "passed": 0, "errors": 1},
        ),
        (
            "KeyboardInterrupt\n1 passed in 0.25s\n",
            2,
            {"failed": 0, "passed": 1, "errors": 0},
        ),
    ],
)
def test_quality_summary_never_marks_nonzero_pytest_exit_as_passed(
    tmp_path, output_text, exit_code, expected_counts,
):
    completed, summary, _pytest_output = _run_summary(
        tmp_path, output_text, exit_code=exit_code,
    )

    assert completed.returncode == 0, completed.stderr
    assert summary["status"] == "failed"
    assert summary["pytest_exit_code"] == exit_code
    assert {
        key: summary["counts"][key] for key in expected_counts
    } == expected_counts


def test_quality_summary_marks_unparseable_no_tests_output_unknown(tmp_path):
    completed, summary, _pytest_output = _run_summary(
        tmp_path, "no tests ran in 0.01s\n", exit_code=5,
    )

    assert completed.returncode == 0, completed.stderr
    assert summary["status"] == "unknown"
    assert summary["pytest_exit_code"] == 5


def test_quality_summary_keeps_original_two_arguments_compatible(tmp_path):
    completed, summary, _pytest_output = _run_summary(
        tmp_path, "1 passed in 0.02s\n",
    )

    assert completed.returncode == 0, completed.stderr
    assert summary["status"] == "unknown"
    assert summary["pytest_exit_code"] is None
