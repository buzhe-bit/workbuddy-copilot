#!/usr/bin/env python3
"""Convert pytest's terminal summary into a small machine-readable artifact."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


COUNT_KEYS = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed", "warnings")
COUNT_PATTERN = re.compile(
    r"(?P<count>\d+)\s+"
    r"(?P<label>passed|failed|errors?|skipped|xfailed|xpassed|warnings?)\b"
)
DURATION_PATTERN = re.compile(r"\bin\s+(?P<seconds>\d+(?:\.\d+)?)s\b")
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")


def _parse_summary(
    pytest_output: str,
    source: Path,
    pytest_exit_code: int | None,
) -> dict[str, object]:
    counts = {key: 0 for key in COUNT_KEYS}
    summary_line = ""
    for raw_line in reversed(pytest_output.splitlines()):
        line = ANSI_PATTERN.sub("", raw_line).strip()
        if DURATION_PATTERN.search(line) and COUNT_PATTERN.search(line):
            summary_line = line
            break

    for match in COUNT_PATTERN.finditer(summary_line):
        label = match.group("label")
        normalized = {"error": "errors", "warning": "warnings"}.get(label, label)
        counts[normalized] = int(match.group("count"))

    duration_match = DURATION_PATTERN.search(summary_line)
    duration = float(duration_match.group("seconds")) if duration_match else None
    if not summary_line or pytest_exit_code is None:
        status = "unknown"
    elif pytest_exit_code != 0 or counts["failed"] or counts["errors"]:
        status = "failed"
    else:
        status = "passed"

    return {
        "schema_version": 1,
        "status": status,
        "pytest_exit_code": pytest_exit_code,
        "counts": counts,
        "duration_seconds": duration,
        "pytest_output": str(source),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pytest-output", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pytest-exit-code", type=int)
    args = parser.parse_args()

    summary = _parse_summary(
        args.pytest_output.read_text(encoding="utf-8", errors="replace"),
        args.pytest_output,
        args.pytest_exit_code,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
