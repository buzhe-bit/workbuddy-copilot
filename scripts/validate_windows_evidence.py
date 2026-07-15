#!/usr/bin/env python3
"""CLI wrapper for the importable Windows W1 evidence validator."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from copilot.student_platform.windows_evidence import (  # noqa: E402
    seal_windows_evidence,
    validate_windows_evidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-build", required=True)
    parser.add_argument("--expected-runner-id")
    parser.add_argument("--hosted-ci", action="store_true", default=None)
    parser.add_argument("--seal", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.seal:
        seal_windows_evidence(args.evidence, schema_path=args.schema)
    result = validate_windows_evidence(
        args.evidence,
        expected_commit=args.expected_commit,
        expected_build=args.expected_build,
        expected_runner_id=args.expected_runner_id,
        schema_path=args.schema,
        hosted_ci=args.hosted_ci,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    if result.status == "rollout_ready":
        return 0
    return 2 if result.status == "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
