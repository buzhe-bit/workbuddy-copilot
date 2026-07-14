#!/usr/bin/env python3
"""Evaluate bounded diagnosis predictions entirely offline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from copilot.evaluation import (  # noqa: E402
    evaluate_prediction_records,
    load_jsonl_records,
    validate_case_catalog,
)


DEFAULT_CASES = PROJECT_ROOT / "tests/fixtures/evaluation/diagnosis_cases.jsonl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the deterministic WorkBuddy diagnosis quality gate.",
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cases, case_input_failures = load_jsonl_records(args.cases, kind="case")
    predictions, prediction_input_failures = load_jsonl_records(
        args.predictions,
        kind="prediction",
    )
    if args.reviews is None:
        reviews, review_input_failures = [], []
    else:
        reviews, review_input_failures = load_jsonl_records(
            args.reviews,
            kind="review",
        )
    report = evaluate_prediction_records(cases, predictions, reviews).with_failures(
        [
            *case_input_failures,
            *prediction_input_failures,
            *review_input_failures,
            *validate_case_catalog(cases),
        ]
    )
    rendered = json.dumps(
        report.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report.gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
