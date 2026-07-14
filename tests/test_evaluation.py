from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from copilot.evaluation import (
    EXPECTED_CATEGORY_COUNTS,
    evaluate_cases,
    evaluate_prediction_records,
    load_jsonl_records,
    validate_case_catalog,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_CASES = PROJECT_ROOT / "tests/fixtures/evaluation/diagnosis_cases.jsonl"


def _ground_truth_review(expected_attention, reasons, actions, forbidden):
    def annotation(reviewer_id):
        return {
            "reviewer_id": reviewer_id,
            "expected_attention": expected_attention,
            "expected_reason_codes": list(reasons),
            "evidence": ["运行测试后显示断言失败"],
            "acceptable_actions": list(actions),
            "forbidden_claims": list(forbidden),
        }

    return {
        "reviewer_1": annotation("ground-reviewer-1"),
        "reviewer_2": annotation("ground-reviewer-2"),
        "adjudicated": annotation("ground-adjudicator"),
    }


def _human_review(actionability=4, *, evidence_supported=True):
    score = {
        "actionability": actionability,
        "evidence_supported": evidence_supported,
    }
    return {
        "reviewer_1": {"reviewer_id": "prediction-reviewer-1", **score},
        "reviewer_2": {"reviewer_id": "prediction-reviewer-2", **score},
        "adjudicated": {"reviewer_id": "prediction-adjudicator", **score},
    }


def _case(
    case_id,
    *,
    category="technical_stuck",
    expected_attention="high",
    expected_reason_codes=None,
    forbidden_claims=None,
):
    if isinstance(expected_attention, bool):
        expected_attention = "high" if expected_attention else "none"
    reasons = (
        list(expected_reason_codes)
        if expected_reason_codes is not None
        else ([category] if expected_attention != "none" else [])
    )
    actions = ["查看失败断言与实际值"]
    forbidden = forbidden_claims or ["数据库连接失败"]
    return {
        "id": case_id,
        "category": category,
        "transcript": "学员：运行测试后显示断言失败。",
        "latest_prompt": "下一步如何定位？",
        "expected_attention": expected_attention,
        "expected_reason_codes": reasons,
        "acceptable_actions": actions,
        "forbidden_claims": forbidden,
        **_ground_truth_review(expected_attention, reasons, actions, forbidden),
    }


def _prediction(
    *,
    attention=True,
    priority="high",
):
    return {
        "attention": attention,
        "priority": priority,
        "reason_codes": ["technical_stuck"] if attention else [],
        "evidence": ["运行测试后显示断言失败"],
        "suggested_action": "查看失败断言与实际值",
        "confidence": 0.9 if attention else 0.4,
    }


def _evaluate(
    cases,
    analyzer,
    *,
    actionability=4,
    evidence_supported=True,
    prediction_reviews=None,
):
    rows = list(cases)
    if prediction_reviews is None:
        prediction_reviews = {
            case["id"]: _human_review(
                actionability,
                evidence_supported=evidence_supported,
            )
            for case in rows
        }
    return evaluate_cases(rows, analyzer, prediction_reviews)


def test_gate_accepts_exact_metric_boundaries():
    cases = [_case(f"high-{index}") for index in range(10)]
    cases.extend(
        _case(
            f"normal-{index}",
            category="normal",
            expected_attention="none",
            expected_reason_codes=(
                ["technical_stuck"] if index == 0 else []
            ),
        )
        for index in range(20)
    )
    cases.append(
        _case(
            "context-fp",
            category="insufficient_context",
            expected_attention="none",
            expected_reason_codes=["insufficient_context"],
        )
    )

    def analyzer(case):
        case_id = case["id"]
        if case_id == "high-9":
            return _prediction(attention=True, priority="medium")
        if case_id == "normal-0":
            return _prediction()
        if case_id == "context-fp":
            prediction = _prediction()
            prediction["reason_codes"] = ["insufficient_context"]
            prediction["confidence"] = 0.5
            return prediction
        if case["expected_attention"] == "high":
            return _prediction()
        return _prediction(attention=False, priority="none")

    report = _evaluate(cases, analyzer)

    assert report.json_valid_rate == 1.0
    assert report.high_recall == pytest.approx(0.9)
    assert report.high_precision == pytest.approx(9 / 11)
    assert report.normal_high_false_positive_rate == pytest.approx(0.05)
    assert report.actionability_mean == 4.0
    assert report.evidence_supported_rate == 1.0
    assert report.gate_passed is True
    assert report.failures == []


@pytest.mark.parametrize(
    ("medium_prediction_priority", "expected_precision"),
    [("medium", 1.0), ("high", 0.5)],
)
def test_medium_ground_truth_is_not_a_high_positive(
    medium_prediction_priority,
    expected_precision,
):
    cases = [
        _case("expected-high", expected_attention="high"),
        _case("expected-medium", expected_attention="medium"),
    ]

    def analyzer(case):
        if case["id"] == "expected-medium":
            return _prediction(priority=medium_prediction_priority)
        return _prediction(priority="high")

    report = _evaluate(cases, analyzer)

    assert report.high_recall == 1.0
    assert report.high_precision == expected_precision


def test_zero_denominators_fail_closed_with_stable_metric_failures():
    report = _evaluate(
        [_case("normal", category="normal", expected_attention="none")],
        lambda _case: _prediction(attention=False, priority="none"),
    )

    assert report.high_precision == 0.0
    assert report.high_recall == 0.0
    assert report.normal_high_false_positive_rate == 0.0
    assert report.gate_passed is False
    assert report.evidence_supported_rate == 1.0
    assert "high_precision_below_threshold" in report.failures
    assert "high_recall_below_threshold" in report.failures


def test_invalid_json_is_counted_without_crashing_other_cases():
    cases = [_case("valid"), _case("invalid")]

    def analyzer(case):
        if case["id"] == "invalid":
            return "{not-json"
        return json.dumps(_prediction(), ensure_ascii=False)

    report = _evaluate(cases, analyzer)

    assert report.total == 2
    assert report.json_valid_rate == 0.5
    assert report.gate_passed is False
    assert "invalid_prediction:invalid" in report.failures
    assert "json_valid_rate_below_threshold" in report.failures


@pytest.mark.parametrize(
    "mutation",
    [
        {"attention": "yes"},
        {"priority": "urgent"},
        {"priority": []},
        {"priority": {}},
        {"reason_codes": ["UPPER_CASE"]},
        {"reason_codes": [f"reason_{index}" for index in range(6)]},
        {"evidence": ["evidence"] * 4},
        {"evidence": ["x" * 161]},
        {"suggested_action": "   "},
        {"suggested_action": "x" * 501},
        {"confidence": True},
        {"confidence": 1.1},
        {"confidence": float("nan")},
        {"confidence": float("inf")},
    ],
)
def test_prediction_protocol_is_bounded_and_fail_closed(mutation):
    prediction = _prediction()
    prediction.update(mutation)

    report = _evaluate([_case("bounded")], lambda _case: prediction)

    assert report.json_valid_rate == 0.0
    assert report.gate_passed is False
    assert "invalid_prediction:bounded" in report.failures


def test_attention_prediction_requires_at_least_one_evidence_item():
    prediction = _prediction()
    prediction["evidence"] = []

    report = _evaluate([_case("missing-evidence")], lambda _case: prediction)

    assert report.json_valid_rate == 0.0
    assert report.gate_passed is False
    assert "invalid_prediction:missing-evidence" in report.failures


@pytest.mark.parametrize(
    ("incomplete_layer", "expected_failure"),
    [
        ("ground_truth", "ground_truth_review_incomplete:review"),
        ("ground_truth_content", "ground_truth_review_incomplete:review"),
        ("prediction", "prediction_review_incomplete:review"),
    ],
)
def test_ground_truth_and_prediction_reviews_fail_independently(
    incomplete_layer,
    expected_failure,
):
    case = _case("review")
    prediction = _prediction()
    prediction_reviews = {case["id"]: _human_review()}
    if incomplete_layer == "ground_truth":
        case["reviewer_2"] = None
    elif incomplete_layer == "ground_truth_content":
        case["reviewer_1"].pop("acceptable_actions")
    else:
        prediction_reviews[case["id"]]["adjudicated"] = None

    report = _evaluate(
        [case],
        lambda _case: prediction,
        prediction_reviews=prediction_reviews,
    )

    assert report.gate_passed is False
    assert "human_review_incomplete" in report.failures
    assert expected_failure in report.failures


@pytest.mark.parametrize("layer", ["ground_truth", "prediction"])
def test_all_three_human_review_ids_must_be_distinct(layer):
    case = _case("reviewer-ids")
    prediction = _prediction()
    prediction_reviews = {case["id"]: _human_review()}
    if layer == "ground_truth":
        case["adjudicated"]["reviewer_id"] = case["reviewer_1"]["reviewer_id"]
        expected_failure = "ground_truth_review_incomplete:reviewer-ids"
    else:
        prediction_reviews[case["id"]]["adjudicated"]["reviewer_id"] = (
            prediction_reviews[case["id"]]["reviewer_1"]["reviewer_id"]
        )
        expected_failure = "prediction_review_incomplete:reviewer-ids"

    report = _evaluate(
        [case],
        lambda _case: prediction,
        prediction_reviews=prediction_reviews,
    )

    assert report.gate_passed is False
    assert expected_failure in report.failures


def test_ground_truth_reviews_require_nonempty_annotation_content():
    case = _case("empty-ground-review")
    case["reviewer_1"]["evidence"] = []

    report = _evaluate([case], lambda _case: _prediction())

    assert report.gate_passed is False
    assert "ground_truth_review_incomplete:empty-ground-review" in report.failures


def test_forbidden_claim_and_unsupported_evidence_are_hard_failures():
    prediction = _prediction()
    prediction["suggested_action"] = "先修复数据库连接失败。"

    report = _evaluate(
        [_case("claims")],
        lambda _case: prediction,
        evidence_supported=False,
    )

    assert report.gate_passed is False
    assert report.evidence_supported_rate == 0.0
    assert "forbidden_claim:claims:数据库连接失败" in report.failures
    assert "evidence_not_supported:claims" in report.failures


def test_insufficient_context_requires_low_confidence():
    case = _case(
        "insufficient",
        category="insufficient_context",
        expected_attention="none",
    )
    prediction = _prediction(attention=False, priority="none")
    prediction["confidence"] = 0.8

    report = _evaluate([case], lambda _case: prediction)

    assert report.gate_passed is False
    assert "insufficient_context_confidence_too_high:insufficient" in report.failures


def test_none_priority_can_report_insufficient_context_reason():
    case = _case(
        "insufficient-reason",
        category="insufficient_context",
        expected_attention="none",
        expected_reason_codes=["insufficient_context"],
    )
    prediction = _prediction(attention=False, priority="none")
    prediction["reason_codes"] = ["insufficient_context"]

    report = _evaluate([case], lambda _case: prediction)

    assert report.json_valid_rate == 1.0
    assert "invalid_prediction:insufficient-reason" not in report.failures
    assert "reason_code_mismatch:insufficient-reason" not in report.failures


def test_expected_reason_codes_require_at_least_one_prediction_overlap():
    prediction = _prediction()
    prediction["reason_codes"] = ["system_failure"]

    report = _evaluate([_case("reason-mismatch")], lambda _case: prediction)

    assert report.gate_passed is False
    assert "reason_code_mismatch:reason-mismatch" in report.failures


def test_empty_expected_reasons_reject_unexpected_prediction_reason():
    case = _case(
        "normal-reason",
        category="normal",
        expected_attention="none",
    )
    prediction = _prediction(attention=False, priority="none")
    prediction["reason_codes"] = ["system_failure"]

    report = _evaluate([case], lambda _case: prediction)

    assert report.gate_passed is False
    assert "unexpected_reason_codes:normal-reason" in report.failures


def test_missing_prediction_is_reported_without_analyzer_exception_leaking():
    def missing(_case):
        raise KeyError("not predicted")

    report = _evaluate([_case("missing")], missing)

    assert report.gate_passed is False
    assert "missing_prediction:missing" in report.failures


def _write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _sixty_case_catalog():
    cases = []
    for category, count in EXPECTED_CATEGORY_COUNTS.items():
        expected_attention = (
            "none"
            if category in {"normal", "insufficient_context"}
            else "high"
        )
        cases.extend(
            _case(
                f"{category}-{index + 1:02d}",
                category=category,
                expected_attention=expected_attention,
                expected_reason_codes=(
                    ["insufficient_context"]
                    if category == "insufficient_context"
                    else None
                ),
            )
            for index in range(count)
        )
    return cases


def _prediction_for_case(case):
    expected_attention = case["expected_attention"]
    if expected_attention in {"medium", "high"}:
        prediction = _prediction(priority=expected_attention)
        prediction["reason_codes"] = list(case["expected_reason_codes"])
        return prediction
    prediction = _prediction(attention=False, priority="none")
    prediction["reason_codes"] = list(case["expected_reason_codes"])
    return prediction


def _model_prediction_for_case(case):
    return _prediction_for_case(case)


def _review_record(case):
    return {"id": case["id"], **_human_review()}


def test_real_catalog_has_exact_distribution_and_unfinished_human_placeholders():
    cases, failures = load_jsonl_records(REAL_CASES, kind="case")

    assert failures == []
    assert len(cases) == 60
    assert validate_case_catalog(cases) == []
    assert {
        category: sum(case["category"] == category for case in cases)
        for category in EXPECTED_CATEGORY_COUNTS
    } == EXPECTED_CATEGORY_COUNTS
    assert {case["expected_attention"] for case in cases} <= {
        "none",
        "medium",
        "high",
    }
    assert {
        case["id"]
        for case in cases
        if case["category"] == "repeated_or_offtopic"
        and case["expected_attention"] == "medium"
    } == {
        "repeated-or-offtopic-02",
        "repeated-or-offtopic-04",
        "repeated-or-offtopic-06",
        "repeated-or-offtopic-09",
    }
    assert all(
        case[reviewer] is None
        for case in cases
        for reviewer in ("reviewer_1", "reviewer_2", "adjudicated")
    )


def test_catalog_validation_rejects_duplicate_ids_and_wrong_category_counts():
    cases = _sixty_case_catalog()
    cases[-1]["id"] = cases[0]["id"]
    cases[-1]["category"] = "normal"

    failures = validate_case_catalog(cases)

    assert f"duplicate_case_id:{cases[0]['id']}" in failures
    assert "category_count_mismatch:normal:15:16" in failures
    assert "category_count_mismatch:system_failure:5:4" in failures


@pytest.mark.parametrize(
    ("field", "value", "expected_failure"),
    [
        (
            "expected_attention",
            False,
            "case_expected_attention_invalid:normal-01",
        ),
        ("transcript", "", "case_transcript_invalid:normal-01"),
        ("transcript", "x" * 20_001, "case_transcript_invalid:normal-01"),
        ("latest_prompt", 7, "case_latest_prompt_invalid:normal-01"),
        ("latest_prompt", "x" * 2_001, "case_latest_prompt_invalid:normal-01"),
        (
            "expected_reason_codes",
            7,
            "case_expected_reason_codes_invalid:normal-01",
        ),
        (
            "expected_reason_codes",
            ["UPPER_CASE"],
            "case_expected_reason_codes_invalid:normal-01",
        ),
        (
            "expected_reason_codes",
            [f"reason_{index}" for index in range(6)],
            "case_expected_reason_codes_invalid:normal-01",
        ),
        (
            "acceptable_actions",
            [],
            "case_acceptable_actions_invalid:normal-01",
        ),
        (
            "acceptable_actions",
            ["action"] * 6,
            "case_acceptable_actions_invalid:normal-01",
        ),
        (
            "acceptable_actions",
            ["x" * 501],
            "case_acceptable_actions_invalid:normal-01",
        ),
        ("forbidden_claims", 7, "case_forbidden_claims_invalid:normal-01"),
        (
            "forbidden_claims",
            ["claim"] * 11,
            "case_forbidden_claims_invalid:normal-01",
        ),
        (
            "forbidden_claims",
            ["x" * 161],
            "case_forbidden_claims_invalid:normal-01",
        ),
    ],
)
def test_catalog_validation_rejects_invalid_case_content(
    field,
    value,
    expected_failure,
):
    cases = _sixty_case_catalog()
    cases[0][field] = value

    failures = validate_case_catalog(cases)

    assert expected_failure in failures


def test_catalog_validation_rejects_unexpected_fields():
    cases = _sixty_case_catalog()
    cases[0]["model_generated_review"] = True

    failures = validate_case_catalog(cases)

    assert (
        "case_fields_unexpected:normal-01:model_generated_review"
        in failures
    )


@pytest.mark.parametrize("invalid_category", [[], {}])
def test_catalog_validation_rejects_unhashable_category(invalid_category):
    cases = _sixty_case_catalog()
    cases[0]["category"] = invalid_category

    failures = validate_case_catalog(cases)

    assert "category_invalid:line_1" in failures


@pytest.mark.parametrize(
    ("field", "value", "expected_failure"),
    [
        (
            "expected_reason_codes",
            7,
            "case_expected_reason_codes_invalid:malformed-case",
        ),
        (
            "forbidden_claims",
            7,
            "case_forbidden_claims_invalid:malformed-case",
        ),
    ],
)
def test_malformed_case_content_fails_closed_without_crashing(
    field,
    value,
    expected_failure,
):
    case = _case("malformed-case")
    case[field] = value
    case["adjudicated"][field] = value

    report = _evaluate([case], lambda _case: _prediction())

    assert report.gate_passed is False
    assert expected_failure in report.failures


def test_prediction_records_reject_duplicates_missing_and_extra_ids():
    cases = [_case("case-1"), _case("case-2")]
    predictions = [
        {"id": "case-1", "prediction": _prediction()},
        {"id": "case-1", "prediction": _prediction()},
        {"id": "extra", "prediction": _prediction()},
    ]

    report = evaluate_prediction_records(cases, predictions)

    assert report.gate_passed is False
    assert "duplicate_prediction_id:case-1" in report.failures
    assert "missing_prediction:case-2" in report.failures
    assert "unexpected_prediction_id:extra" in report.failures


def test_prediction_records_reject_model_embedded_human_review():
    case = _case("self-reviewed")
    prediction = _prediction_for_case(case)
    prediction["human_review"] = _human_review()

    report = evaluate_prediction_records(
        [case],
        [{"id": case["id"], "prediction": prediction}],
        [_review_record(case)],
    )

    assert report.gate_passed is False
    assert "prediction_review_embedded:self-reviewed" in report.failures


def test_evaluate_cases_rejects_analyzer_embedded_human_review():
    case = _case("direct-self-reviewed")
    prediction = _prediction_for_case(case)
    prediction["human_review"] = _human_review(actionability=5)

    report = evaluate_cases(
        [case],
        lambda _case: prediction,
        {case["id"]: _human_review()},
    )

    assert report.gate_passed is False
    assert "prediction_review_embedded:direct-self-reviewed" in report.failures


def test_prediction_records_reject_top_level_model_self_review():
    case = _case("top-level-self-reviewed")
    record = {
        "id": case["id"],
        "prediction": _model_prediction_for_case(case),
        "human_review": _human_review(),
    }

    report = evaluate_prediction_records([case], [record])

    assert report.gate_passed is False
    assert "prediction_review_embedded:top-level-self-reviewed" in report.failures


def test_prediction_records_reject_duplicate_review_ids():
    case = _case("duplicate-review")
    prediction = {
        "id": case["id"],
        "prediction": _model_prediction_for_case(case),
    }
    review = _review_record(case)

    report = evaluate_prediction_records([case], [prediction], [review, review])

    assert report.gate_passed is False
    assert "duplicate_review_id:duplicate-review" in report.failures


def _run_cli(*args):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "scripts/evaluate_diagnosis.py", *map(str, args)],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_runs_from_repo_root_and_passes_only_with_complete_dual_review(tmp_path):
    cases = _sixty_case_catalog()
    predictions = [
        {"id": case["id"], "prediction": _model_prediction_for_case(case)}
        for case in cases
    ]
    reviews = [_review_record(case) for case in cases]
    cases_path = tmp_path / "cases.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    reviews_path = tmp_path / "reviews.jsonl"
    output_path = tmp_path / "report.json"
    _write_jsonl(cases_path, cases)
    _write_jsonl(predictions_path, predictions)
    _write_jsonl(reviews_path, reviews)

    completed = _run_cli(
        "--cases",
        cases_path,
        "--predictions",
        predictions_path,
        "--reviews",
        reviews_path,
        "--output",
        output_path,
    )

    assert completed.returncode == 0, completed.stderr
    stdout_report = json.loads(completed.stdout)
    assert stdout_report["gate_passed"] is True
    assert stdout_report["total"] == 60
    assert json.loads(output_path.read_text(encoding="utf-8")) == stdout_report


def test_cli_without_separate_prediction_reviews_fails_closed(tmp_path):
    cases = _sixty_case_catalog()
    predictions = [
        {"id": case["id"], "prediction": _model_prediction_for_case(case)}
        for case in cases
    ]
    cases_path = tmp_path / "cases.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    _write_jsonl(cases_path, cases)
    _write_jsonl(predictions_path, predictions)

    completed = _run_cli(
        "--cases",
        cases_path,
        "--predictions",
        predictions_path,
    )

    assert completed.returncode != 0
    report = json.loads(completed.stdout)
    assert "human_review_incomplete" in report["failures"]
    assert any(
        failure.startswith("prediction_review_incomplete:")
        for failure in report["failures"]
    )


def test_cli_real_catalog_fails_closed_as_human_review_incomplete(tmp_path):
    predictions_path = tmp_path / "predictions.jsonl"
    predictions_path.write_text("", encoding="utf-8")

    completed = _run_cli(
        "--cases",
        REAL_CASES,
        "--predictions",
        predictions_path,
    )

    assert completed.returncode != 0
    report = json.loads(completed.stdout)
    assert report["gate_passed"] is False
    assert "human_review_incomplete" in report["failures"]
    assert any(
        failure.startswith("ground_truth_review_incomplete:")
        for failure in report["failures"]
    )


def test_cli_invalid_prediction_json_is_nonzero_and_machine_readable(tmp_path):
    cases_path = tmp_path / "cases.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    _write_jsonl(cases_path, _sixty_case_catalog())
    predictions_path.write_text("{not-json\n", encoding="utf-8")

    completed = _run_cli(
        "--cases",
        cases_path,
        "--predictions",
        predictions_path,
    )

    assert completed.returncode != 0
    report = json.loads(completed.stdout)
    assert "prediction_json_invalid:line_1" in report["failures"]
