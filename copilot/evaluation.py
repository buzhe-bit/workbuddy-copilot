"""Deterministic, offline diagnosis-quality evaluation."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Mapping


Analyzer = Callable[[Mapping[str, Any]], Any]
PredictionReviews = (
    Mapping[str, Mapping[str, Any]]
    | Iterable[Mapping[str, Any]]
    | None
)

JSON_VALID_RATE_MIN = 0.99
HIGH_PRECISION_MIN = 0.80
HIGH_RECALL_MIN = 0.90
NORMAL_HIGH_FALSE_POSITIVE_MAX = 0.05
ACTIONABILITY_MEAN_MIN = 4.0
INSUFFICIENT_CONTEXT_CONFIDENCE_MAX = 0.5

EXPECTED_CATEGORY_COUNTS = {
    "normal": 15,
    "technical_stuck": 15,
    "repeated_or_offtopic": 10,
    "insufficient_context": 10,
    "ask_failure": 5,
    "system_failure": 5,
}

_PRIORITIES = frozenset({"none", "medium", "high"})
_REASON_CODE = re.compile(r"^[a-z0-9_]{1,80}$")
_MAX_REASON_CODES = 5
_MAX_EVIDENCE = 3
_MAX_EVIDENCE_CHARS = 160
_MAX_ACTION_CHARS = 500
_MAX_ANNOTATION_ACTIONS = 5
_MAX_FORBIDDEN_CLAIMS = 10
_MAX_TRANSCRIPT_CHARS = 20_000
_MAX_LATEST_PROMPT_CHARS = 2_000
_GROUND_TRUTH_FIELDS = (
    "reviewer_id",
    "expected_attention",
    "expected_reason_codes",
    "evidence",
    "acceptable_actions",
    "forbidden_claims",
)
_CASE_FIELDS = frozenset({
    "id",
    "category",
    "transcript",
    "latest_prompt",
    "expected_attention",
    "expected_reason_codes",
    "acceptable_actions",
    "forbidden_claims",
    "reviewer_1",
    "reviewer_2",
    "adjudicated",
})


@dataclass(frozen=True)
class EvaluationReport:
    total: int
    json_valid_rate: float
    high_precision: float
    high_recall: float
    normal_high_false_positive_rate: float
    actionability_mean: float
    evidence_supported_rate: float
    gate_passed: bool
    failures: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable report."""
        return asdict(self)

    def with_failures(self, extra_failures: Iterable[str]) -> EvaluationReport:
        """Return this report with stable, de-duplicated hard failures."""
        combined = list(self.failures)
        for failure in extra_failures:
            _append_once(combined, str(failure))
        return EvaluationReport(
            total=self.total,
            json_valid_rate=self.json_valid_rate,
            high_precision=self.high_precision,
            high_recall=self.high_recall,
            normal_high_false_positive_rate=self.normal_high_false_positive_rate,
            actionability_mean=self.actionability_mean,
            evidence_supported_rate=self.evidence_supported_rate,
            gate_passed=self.gate_passed and not combined,
            failures=combined,
        )


def _append_once(failures: list[str], value: str) -> None:
    if value not in failures:
        failures.append(value)


def _is_string_list(value: Any, *, maximum: int, item_maximum: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= maximum
        and all(
            isinstance(item, str)
            and bool(item.strip())
            and len(item) <= item_maximum
            for item in value
        )
    )


def _decode_prediction(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, Mapping):
        return None
    prediction = dict(value)
    attention = prediction.get("attention")
    priority = prediction.get("priority")
    reason_codes = prediction.get("reason_codes")
    evidence = prediction.get("evidence")
    suggested_action = prediction.get("suggested_action")
    confidence = prediction.get("confidence")
    if (
        not isinstance(attention, bool)
        or not isinstance(priority, str)
        or priority not in _PRIORITIES
    ):
        return None
    if attention and priority == "none":
        return None
    if not attention and priority != "none":
        return None
    if not _is_string_list(
        reason_codes,
        maximum=_MAX_REASON_CODES,
        item_maximum=80,
    ):
        return None
    if any(_REASON_CODE.fullmatch(item) is None for item in reason_codes):
        return None
    if len(reason_codes) != len(set(reason_codes)):
        return None
    if attention and not reason_codes:
        return None
    if not _is_string_list(
        evidence,
        maximum=_MAX_EVIDENCE,
        item_maximum=_MAX_EVIDENCE_CHARS,
    ):
        return None
    if attention and not evidence:
        return None
    if (
        not isinstance(suggested_action, str)
        or not suggested_action.strip()
        or len(suggested_action) > _MAX_ACTION_CHARS
    ):
        return None
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        return None
    return prediction


def _reviewer_id(value: Mapping[str, Any]) -> str:
    reviewer_id = value.get("reviewer_id")
    if not isinstance(reviewer_id, str):
        return ""
    reviewer_id = reviewer_id.strip()
    return reviewer_id if 0 < len(reviewer_id) <= 80 else ""


def _valid_reason_codes(value: Any) -> bool:
    return (
        _is_string_list(value, maximum=_MAX_REASON_CODES, item_maximum=80)
        and len(value) == len(set(value))
        and all(_REASON_CODE.fullmatch(item) is not None for item in value)
    )


def _ground_truth_annotation_complete(value: Mapping[str, Any]) -> bool:
    if any(field not in value for field in _GROUND_TRUTH_FIELDS):
        return False
    expected_attention = value.get("expected_attention")
    if not (
        isinstance(expected_attention, bool)
        or (
            isinstance(expected_attention, str)
            and expected_attention in _PRIORITIES
        )
    ):
        return False
    return (
        bool(_reviewer_id(value))
        and _valid_reason_codes(value.get("expected_reason_codes"))
        and bool(value.get("evidence"))
        and _is_string_list(
            value.get("evidence"),
            maximum=_MAX_EVIDENCE,
            item_maximum=_MAX_EVIDENCE_CHARS,
        )
        and bool(value.get("acceptable_actions"))
        and _is_string_list(
            value.get("acceptable_actions"),
            maximum=_MAX_ANNOTATION_ACTIONS,
            item_maximum=_MAX_ACTION_CHARS,
        )
        and _is_string_list(
            value.get("forbidden_claims"),
            maximum=_MAX_FORBIDDEN_CLAIMS,
            item_maximum=_MAX_EVIDENCE_CHARS,
        )
    )


def _ground_truth_complete(case: Mapping[str, Any]) -> bool:
    reviews: list[Mapping[str, Any]] = []
    for key in ("reviewer_1", "reviewer_2", "adjudicated"):
        value = case.get(key)
        if not isinstance(value, Mapping):
            return False
        if not _ground_truth_annotation_complete(value):
            return False
        reviews.append(value)
    if len({_reviewer_id(review) for review in reviews}) != len(reviews):
        return False
    adjudicated = reviews[2]
    for field in (
        "expected_attention",
        "expected_reason_codes",
        "acceptable_actions",
        "forbidden_claims",
    ):
        if adjudicated.get(field) != case.get(field):
            return False
    return True


def _prediction_review(human_review: Any) -> tuple[float, bool] | None:
    if not isinstance(human_review, Mapping):
        return None
    reviews: list[Mapping[str, Any]] = []
    for key in ("reviewer_1", "reviewer_2", "adjudicated"):
        value = human_review.get(key)
        if not isinstance(value, Mapping) or not _reviewer_id(value):
            return None
        score = value.get("actionability")
        supported = value.get("evidence_supported")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            or not 1.0 <= float(score) <= 5.0
            or not isinstance(supported, bool)
        ):
            return None
        reviews.append(value)
    if len({_reviewer_id(review) for review in reviews}) != len(reviews):
        return None
    adjudicated = reviews[2]
    return float(adjudicated["actionability"]), bool(
        adjudicated["evidence_supported"]
    )


def _index_prediction_reviews(
    source: PredictionReviews,
) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    reviews: dict[str, Mapping[str, Any]] = {}
    failures: list[str] = []
    if source is None:
        return reviews, failures
    if isinstance(source, Mapping):
        entries = list(source.items())
    else:
        entries = []
        for index, record in enumerate(source, start=1):
            if not isinstance(record, Mapping):
                failures.append(f"review_record_invalid:line_{index}")
                continue
            entries.append((record.get("id"), record))
    for index, (raw_id, raw_review) in enumerate(entries, start=1):
        if not isinstance(raw_id, str) or not raw_id.strip():
            failures.append(f"review_id_invalid:line_{index}")
            continue
        review_id = raw_id.strip()
        if review_id in reviews:
            _append_once(failures, f"duplicate_review_id:{review_id}")
            continue
        if not isinstance(raw_review, Mapping):
            failures.append(f"review_record_invalid:{review_id}")
            continue
        reviews[review_id] = {
            key: raw_review.get(key)
            for key in ("reviewer_1", "reviewer_2", "adjudicated")
        }
    return reviews, failures


def _remove_embedded_review(value: Any) -> tuple[Any, bool]:
    decoded = value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value, False
    if not isinstance(decoded, Mapping) or "human_review" not in decoded:
        return value, False
    return (
        {key: item for key, item in decoded.items() if key != "human_review"},
        True,
    )


def _expected_high(value: Any) -> bool:
    return value is True or value == "high"


def load_jsonl_records(
    path: str | Path,
    *,
    kind: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Load bounded JSON objects while returning machine-readable input failures."""
    if kind not in {"case", "prediction", "review"}:
        raise ValueError("kind must be case, prediction, or review")
    source = Path(path)
    if not source.is_file():
        return [], [f"{kind}_file_not_found"]
    records: list[dict[str, Any]] = []
    failures: list[str] = []
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return [], [f"{kind}_file_unreadable"]
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            failures.append(f"{kind}_json_invalid:line_{line_number}")
            continue
        if not isinstance(value, Mapping):
            failures.append(f"{kind}_json_invalid:line_{line_number}")
            continue
        records.append(dict(value))
    return records, failures


def validate_case_catalog(cases: Iterable[Mapping[str, Any]]) -> list[str]:
    """Validate the frozen 60-case category matrix without claiming review complete."""
    rows = list(cases)
    failures: list[str] = []
    if len(rows) != sum(EXPECTED_CATEGORY_COUNTS.values()):
        failures.append(
            f"dataset_total_mismatch:{sum(EXPECTED_CATEGORY_COUNTS.values())}:{len(rows)}"
        )
    seen_ids: set[str] = set()
    counts = {category: 0 for category in EXPECTED_CATEGORY_COUNTS}
    for index, case in enumerate(rows, start=1):
        missing = sorted(_CASE_FIELDS - set(case))
        if missing:
            failures.append(f"case_fields_missing:line_{index}:{','.join(missing)}")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip() or len(case_id) > 80:
            failures.append(f"case_id_invalid:line_{index}")
        else:
            normalized_id = case_id.strip()
            if normalized_id in seen_ids:
                _append_once(failures, f"duplicate_case_id:{normalized_id}")
            seen_ids.add(normalized_id)
        failure_id = (
            case_id.strip()
            if isinstance(case_id, str) and case_id.strip()
            else f"line_{index}"
        )
        unexpected = sorted(set(case) - _CASE_FIELDS)
        if unexpected:
            failures.append(
                f"case_fields_unexpected:{failure_id}:{','.join(unexpected)}"
            )
        category = case.get("category")
        if not isinstance(category, str) or category not in counts:
            failures.append(f"category_invalid:line_{index}")
        else:
            counts[str(category)] += 1
        expected_attention = case.get("expected_attention")
        if not (
            isinstance(expected_attention, str)
            and expected_attention in _PRIORITIES
        ):
            failures.append(
                f"case_expected_attention_invalid:{failure_id}"
            )
        if not (
            isinstance(case.get("transcript"), str)
            and bool(case["transcript"].strip())
            and len(case["transcript"]) <= _MAX_TRANSCRIPT_CHARS
        ):
            failures.append(f"case_transcript_invalid:{failure_id}")
        if not (
            isinstance(case.get("latest_prompt"), str)
            and bool(case["latest_prompt"].strip())
            and len(case["latest_prompt"]) <= _MAX_LATEST_PROMPT_CHARS
        ):
            failures.append(f"case_latest_prompt_invalid:{failure_id}")
        if not _valid_reason_codes(case.get("expected_reason_codes")):
            failures.append(
                f"case_expected_reason_codes_invalid:{failure_id}"
            )
        acceptable_actions = case.get("acceptable_actions")
        if not (
            bool(acceptable_actions)
            and _is_string_list(
                acceptable_actions,
                maximum=_MAX_ANNOTATION_ACTIONS,
                item_maximum=_MAX_ACTION_CHARS,
            )
        ):
            failures.append(f"case_acceptable_actions_invalid:{failure_id}")
        if not _is_string_list(
            case.get("forbidden_claims"),
            maximum=_MAX_FORBIDDEN_CLAIMS,
            item_maximum=_MAX_EVIDENCE_CHARS,
        ):
            failures.append(f"case_forbidden_claims_invalid:{failure_id}")
    for category, expected in EXPECTED_CATEGORY_COUNTS.items():
        actual = counts[category]
        if actual != expected:
            failures.append(
                f"category_count_mismatch:{category}:{expected}:{actual}"
            )
    return failures


def evaluate_prediction_records(
    cases: Iterable[Mapping[str, Any]],
    prediction_records: Iterable[Mapping[str, Any]],
    review_records: Iterable[Mapping[str, Any]] = (),
) -> EvaluationReport:
    """Join offline predictions by case id and reject duplicate/missing/extra rows."""
    case_rows = list(cases)
    prediction_rows = list(prediction_records)
    review_rows = list(review_records)
    predictions: dict[str, Any] = {}
    failures: list[str] = []
    for index, record in enumerate(prediction_rows, start=1):
        prediction_id = record.get("id")
        if not isinstance(prediction_id, str) or not prediction_id.strip():
            failures.append(f"prediction_id_invalid:line_{index}")
            continue
        prediction_id = prediction_id.strip()
        if prediction_id in predictions:
            _append_once(failures, f"duplicate_prediction_id:{prediction_id}")
            continue
        if "human_review" in record:
            _append_once(
                failures,
                f"prediction_review_embedded:{prediction_id}",
            )
        if "prediction" in record:
            prediction = record["prediction"]
        else:
            prediction = {
                key: value for key, value in record.items() if key != "id"
            }
        if isinstance(prediction, Mapping) and "human_review" in prediction:
            _append_once(
                failures,
                f"prediction_review_embedded:{prediction_id}",
            )
            prediction = {
                key: value
                for key, value in prediction.items()
                if key != "human_review"
            }
        elif isinstance(prediction, str):
            try:
                decoded = json.loads(prediction)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, Mapping) and "human_review" in decoded:
                _append_once(
                    failures,
                    f"prediction_review_embedded:{prediction_id}",
                )
                prediction = {
                    key: value for key, value in decoded.items() if key != "human_review"
                }
        predictions[prediction_id] = prediction

    case_ids = {
        str(case.get("id") or "").strip()
        for case in case_rows
        if str(case.get("id") or "").strip()
    }
    for prediction_id in sorted(set(predictions) - case_ids):
        failures.append(f"unexpected_prediction_id:{prediction_id}")
    def analyzer(case: Mapping[str, Any]) -> Any:
        case_id = str(case.get("id") or "").strip()
        if case_id not in predictions:
            raise KeyError(case_id)
        return predictions[case_id]

    return evaluate_cases(
        case_rows,
        analyzer,
        review_rows,
    ).with_failures(failures)


def evaluate_cases(
    cases: Iterable[Mapping[str, Any]],
    analyzer: Analyzer,
    prediction_reviews: PredictionReviews = None,
) -> EvaluationReport:
    """Evaluate bounded predictions without invoking any provider itself."""
    rows = list(cases)
    reviews, failures = _index_prediction_reviews(prediction_reviews)
    valid_predictions = 0
    true_positive = 0
    predicted_positive = 0
    expected_positive = 0
    normal_total = 0
    normal_false_positive = 0
    actionability_scores: list[float] = []
    evidence_supported: list[bool] = []
    human_review_incomplete = False
    case_ids = {
        str(case.get("id") or "").strip()
        for case in rows
        if str(case.get("id") or "").strip()
    }
    for review_id in sorted(set(reviews) - case_ids):
        failures.append(f"unexpected_review_id:{review_id}")

    for index, case in enumerate(rows):
        case_id = str(case.get("id") or f"case-{index}")[:80]
        expected_high = _expected_high(case.get("expected_attention"))
        expected_positive += int(expected_high)
        if case.get("category") == "normal":
            normal_total += 1
        if not _ground_truth_complete(case):
            failures.append(f"ground_truth_review_incomplete:{case_id}")
            human_review_incomplete = True
        review = _prediction_review(reviews.get(case_id))
        if review is None:
            failures.append(f"prediction_review_incomplete:{case_id}")
            human_review_incomplete = True

        try:
            raw_prediction = analyzer(case)
        except KeyError:
            failures.append(f"missing_prediction:{case_id}")
            continue
        except Exception as exc:  # analyzer boundary must not leak provider detail
            failures.append(f"analyzer_error:{case_id}:{type(exc).__name__}")
            continue
        raw_prediction, embedded_review = _remove_embedded_review(raw_prediction)
        if embedded_review:
            failures.append(f"prediction_review_embedded:{case_id}")
        prediction = _decode_prediction(raw_prediction)
        if prediction is None:
            failures.append(f"invalid_prediction:{case_id}")
            continue
        valid_predictions += 1
        predicted_high = (
            prediction["attention"] is True
            and prediction["priority"] == "high"
        )
        predicted_positive += int(predicted_high)
        true_positive += int(predicted_high and expected_high)
        if case.get("category") == "normal":
            normal_false_positive += int(predicted_high)

        raw_expected_reason_codes = case.get("expected_reason_codes")
        if _valid_reason_codes(raw_expected_reason_codes):
            expected_reason_codes = set(raw_expected_reason_codes)
        else:
            expected_reason_codes = set()
            failures.append(
                f"case_expected_reason_codes_invalid:{case_id}"
            )
        if expected_reason_codes and expected_reason_codes.isdisjoint(
            prediction["reason_codes"]
        ):
            failures.append(f"reason_code_mismatch:{case_id}")
        elif not expected_reason_codes and prediction["reason_codes"]:
            failures.append(f"unexpected_reason_codes:{case_id}")

        if review is not None:
            actionability, supported = review
            actionability_scores.append(actionability)
            evidence_supported.append(supported)
            if not supported:
                failures.append(f"evidence_not_supported:{case_id}")

        prediction_text = "\n".join(
            [
                *prediction["reason_codes"],
                *prediction["evidence"],
                prediction["suggested_action"],
            ]
        ).casefold()
        raw_forbidden_claims = case.get("forbidden_claims")
        if _is_string_list(
            raw_forbidden_claims,
            maximum=_MAX_FORBIDDEN_CLAIMS,
            item_maximum=_MAX_EVIDENCE_CHARS,
        ):
            forbidden_claims = raw_forbidden_claims
        else:
            forbidden_claims = []
            failures.append(f"case_forbidden_claims_invalid:{case_id}")
        for claim in forbidden_claims:
            if claim.strip().casefold() in prediction_text:
                failures.append(f"forbidden_claim:{case_id}:{claim[:160]}")

        if (
            case.get("category") == "insufficient_context"
            and float(prediction["confidence"])
            > INSUFFICIENT_CONTEXT_CONFIDENCE_MAX
        ):
            failures.append(
                f"insufficient_context_confidence_too_high:{case_id}"
            )

    total = len(rows)
    json_valid_rate = valid_predictions / total if total else 0.0
    high_precision = (
        true_positive / predicted_positive if predicted_positive else 0.0
    )
    high_recall = true_positive / expected_positive if expected_positive else 0.0
    normal_high_false_positive_rate = (
        normal_false_positive / normal_total if normal_total else 0.0
    )
    actionability_mean = (
        sum(actionability_scores) / len(actionability_scores)
        if actionability_scores
        else 0.0
    )
    evidence_supported_rate = (
        sum(evidence_supported) / len(evidence_supported)
        if evidence_supported
        else 0.0
    )
    if human_review_incomplete:
        _append_once(failures, "human_review_incomplete")
    if total == 0:
        failures.append("empty_dataset")
    if json_valid_rate < JSON_VALID_RATE_MIN:
        failures.append("json_valid_rate_below_threshold")
    if high_precision < HIGH_PRECISION_MIN:
        failures.append("high_precision_below_threshold")
    if high_recall < HIGH_RECALL_MIN:
        failures.append("high_recall_below_threshold")
    if normal_high_false_positive_rate > NORMAL_HIGH_FALSE_POSITIVE_MAX:
        failures.append("normal_high_false_positive_rate_above_threshold")
    if actionability_mean < ACTIONABILITY_MEAN_MIN:
        failures.append("actionability_mean_below_threshold")
    if evidence_supported_rate < 1.0:
        failures.append("evidence_supported_rate_below_threshold")

    return EvaluationReport(
        total=total,
        json_valid_rate=json_valid_rate,
        high_precision=high_precision,
        high_recall=high_recall,
        normal_high_false_positive_rate=normal_high_false_positive_rate,
        actionability_mean=actionability_mean,
        evidence_supported_rate=evidence_supported_rate,
        gate_passed=not failures,
        failures=failures,
    )
