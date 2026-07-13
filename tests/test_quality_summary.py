from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_quality_summary_writes_machine_readable_pytest_counts(tmp_path):
    pytest_output = tmp_path / "pytest-output.txt"
    summary_output = tmp_path / "quality-summary.json"
    pytest_output.write_text(
        ".............. [100%]\n14 passed, 2 skipped, 3 warnings in 1.25s\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "quality_summary.py"),
            "--pytest-output",
            str(pytest_output),
            "--output",
            str(summary_output),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(summary_output.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "status": "passed",
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
