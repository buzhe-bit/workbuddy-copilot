"""Deterministic rules for durable mentor-attention projections.

This module deliberately contains no I/O.  Source rows are reduced to bounded
decisions that can safely be persisted without copying transcripts, questions,
answers, or private feedback notes.
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable, Mapping
from typing import Any

from .models import AttentionDecision, normalize_confidence, normalize_evidence

log = logging.getLogger("copilot.attention")


def _text(value: object, *, limit: int = 240) -> str:
    return str(value or "").strip()[:limit]


def _identity(value: object) -> str:
    """Preserve durable identity exactly; truncation would create ghost owners."""
    identity = str(value or "")
    return identity if identity.strip() else ""


def _created_at(row: Mapping[str, Any]) -> float:
    value = row.get("created_at", 0.0)
    if isinstance(value, bool):
        return 0.0
    try:
        timestamp = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return timestamp if math.isfinite(timestamp) else 0.0


def _row_id(row: Mapping[str, Any]) -> int:
    value = row.get("id", 0)
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class AttentionPolicy:
    """Apply the Task 5 precedence and historical-window rules."""

    _ANALYSIS_REASONS = {
        "analysis_error": (
            "Analysis reported an error-level learning risk.",
            "Review the learner's next concrete step.",
        ),
        "analysis_confident_stuck": (
            "Analysis confidently indicates the learner is stuck.",
            "Offer a focused unblock or checkpoint.",
        ),
        "analysis_repeated_low": (
            "Recent analyses show repeated low or stuck understanding.",
            "Review the recent learning pattern and intervene.",
        ),
        "analysis_warning": (
            "Analysis reported a warning-level learning risk.",
            "Review the bounded diagnosis when convenient.",
        ),
        "analysis_off_topic": (
            "The learner appears to be off topic.",
            "Help reconnect the work to the current objective.",
        ),
        "analysis_low": (
            "Analysis indicates low understanding.",
            "Check whether a short clarification is needed.",
        ),
    }

    def decide_analysis(
        self,
        target: Mapping[str, Any],
        *,
        history: Iterable[Mapping[str, Any]],
    ) -> AttentionDecision | None:
        """Return the single highest-precedence decision for one analysis."""
        understanding = _text(target.get("understanding"), limit=40).lower()
        severity = _text(target.get("severity"), limit=40).lower()
        confidence = normalize_confidence(target.get("confidence"))

        reason_code: str | None = None
        priority = "medium"
        if severity == "error":
            reason_code = "analysis_error"
            priority = "high"
        elif understanding == "stuck" and confidence >= 0.65:
            reason_code = "analysis_confident_stuck"
            priority = "high"
        elif self._has_repeated_low(target, history):
            reason_code = "analysis_repeated_low"
            priority = "high"
        elif severity == "warn":
            reason_code = "analysis_warning"
        elif bool(target.get("off_topic")):
            reason_code = "analysis_off_topic"
        elif understanding == "low":
            reason_code = "analysis_low"

        if reason_code is None:
            return None

        evidence_value: object = target.get("evidence_json", "[]")
        if isinstance(evidence_value, str):
            try:
                evidence_value = json.loads(evidence_value)
            except (TypeError, ValueError, json.JSONDecodeError):
                evidence_value = []
        fallback_reason, fallback_action = self._ANALYSIS_REASONS[reason_code]
        reason = _text(target.get("diagnosis"), limit=500) or fallback_reason
        suggested_action = (
            _text(target.get("suggestion"), limit=500) or fallback_action
        )
        return AttentionDecision(
            source_type="analysis",
            source_id=str(_row_id(target)),
            category="learning",
            student_id=_identity(target.get("student_id")),
            session_id=_identity(target.get("session_id")),
            priority=priority,  # type: ignore[arg-type]
            reason_code=reason_code,
            reason=reason,
            evidence=tuple(normalize_evidence(evidence_value)),
            suggested_action=suggested_action,
            confidence=confidence,
            created_at=_created_at(target),
        )

    def decide_student_ask(
        self,
        ask: Mapping[str, Any],
    ) -> list[AttentionDecision]:
        """Project only status axes; never copy learner-authored ask content."""
        reason_codes: list[str] = []
        answer_status = _text(ask.get("answer_status"), limit=40).lower()
        if answer_status == "degraded":
            reason_codes.append("student_ask_degraded")
        elif answer_status == "failed":
            reason_codes.append("student_ask_failed")
        if _text(ask.get("feedback"), limit=40).lower() == "unresolved":
            reason_codes.append("student_ask_unresolved")

        descriptions = {
            "student_ask_degraded": (
                "A learner question received a degraded answer.",
                "Review whether a reliable follow-up is needed.",
            ),
            "student_ask_failed": (
                "A learner question could not be answered.",
                "Provide a reliable follow-up when possible.",
            ),
            "student_ask_unresolved": (
                "The learner marked a question as unresolved.",
                "Check the unresolved learning need.",
            ),
        }
        decisions: list[AttentionDecision] = []
        for reason_code in reason_codes:
            reason, suggested_action = descriptions[reason_code]
            decisions.append(AttentionDecision(
                source_type="student_ask",
                source_id=str(_row_id(ask)),
                category="learning",
                student_id=_identity(ask.get("student_id")),
                session_id=_identity(ask.get("session_id")),
                priority="high",
                reason_code=reason_code,
                reason=reason,
                suggested_action=suggested_action,
                confidence=1.0,
                created_at=_created_at(ask),
            ))
        return decisions

    def decide_system_failure(
        self,
        source_kind: str,
        source: Mapping[str, Any],
    ) -> AttentionDecision | None:
        """Project one immutable occurrence without copying provider details."""
        reason_code = _text(source.get("reason_code"), limit=120)
        logical_key = _identity(source.get("logical_key"))
        generation = _row_id({"id": source.get("generation", 0)})
        expected_reasons = {
            "stop": {
                "system_stop_input_unavailable",
                "system_stop_retries_exhausted",
            },
            "bulk_analysis": {"system_bulk_analysis_failed"},
            "upload_transfer": {"system_upload_transfer_failed"},
            "upload_analysis": {"system_upload_analysis_failed"},
        }
        if source_kind not in expected_reasons:
            raise ValueError("invalid attention system source kind")
        if (
            not logical_key
            or generation < 1
            or reason_code not in expected_reasons[source_kind]
        ):
            return None
        prefixes = {
            "stop": "stop",
            "bulk_analysis": "bulk",
            "upload_transfer": "upload-transfer",
            "upload_analysis": "upload-analysis",
        }
        source_id = f"{prefixes[source_kind]}:{logical_key}:{generation}"
        descriptions = {
            "system_stop_input_unavailable": (
                "A durable Stop analysis could not recover its bounded input.",
                "Review whether the learner session needs manual follow-up.",
            ),
            "system_stop_retries_exhausted": (
                "A durable Stop analysis exhausted its retry budget.",
                "Review the session and restore analysis coverage.",
            ),
            "system_upload_transfer_failed": (
                "A transcript upload transfer failed.",
                "Check the upload workflow and retry when safe.",
            ),
            "system_bulk_analysis_failed": (
                "A stored transcript analysis generation failed.",
                "Review the analysis workflow and retry when safe.",
            ),
            "system_upload_analysis_failed": (
                "An upload request analysis reached a failed state.",
                "Review the upload analysis workflow and retry when safe.",
            ),
        }
        reason, suggested_action = descriptions[reason_code]
        return AttentionDecision(
            source_type="system",
            source_id=source_id,
            category="system",
            student_id=_identity(source.get("student_id")),
            session_id=_identity(source.get("session_id")),
            priority="high",
            reason_code=reason_code,
            reason=reason,
            suggested_action=suggested_action,
            confidence=1.0,
            created_at=_created_at(source),
        )

    @staticmethod
    def _has_repeated_low(
        target: Mapping[str, Any],
        history: Iterable[Mapping[str, Any]],
    ) -> bool:
        target_student = _identity(target.get("student_id"))
        target_session = _identity(target.get("session_id"))
        cutoff = (_created_at(target), _row_id(target))

        scoped: dict[int, Mapping[str, Any]] = {}
        for row in history:
            row_id = _row_id(row)
            if _identity(row.get("student_id")) != target_student:
                continue
            if target_session and _identity(row.get("session_id")) != target_session:
                continue
            if (_created_at(row), row_id) > cutoff:
                continue
            scoped[row_id] = row
        scoped.setdefault(_row_id(target), target)

        latest = sorted(
            scoped.values(),
            key=lambda row: (_created_at(row), _row_id(row)),
            reverse=True,
        )[:3]
        return sum(
            _text(row.get("understanding"), limit=40).lower() in {"low", "stuck"}
            for row in latest
        ) >= 2


class AttentionService:
    """Project durable sources into attention items, then publish new changes."""

    _MENTOR_FIELDS = (
        "id",
        "source_type",
        "source_id",
        "category",
        "student_id",
        "session_id",
        "priority",
        "reason_code",
        "status",
        "handled_by",
        "handled_at",
        "created_at",
        "updated_at",
    )

    def __init__(self, *, store: Any, event_bus: Any, policy: AttentionPolicy | None = None):
        self.store = store
        self.event_bus = event_bus
        self.policy = policy or AttentionPolicy()

    @staticmethod
    def _evidence(row: Mapping[str, Any]) -> list[str]:
        value: object = row.get("evidence_json", "[]")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                value = []
        return normalize_evidence(value)

    @classmethod
    def _mentor_item(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        """Serialize the authenticated mentor API view with bounded detail."""
        item = {field: row.get(field) for field in cls._MENTOR_FIELDS}
        item.update({
            "reason": _text(row.get("reason"), limit=500),
            "evidence": cls._evidence(row),
            "suggested_action": _text(row.get("suggested_action"), limit=500),
            "confidence": normalize_confidence(row.get("confidence")),
            "resolution_note": _text(row.get("resolution_note"), limit=500),
        })
        return item

    @classmethod
    def _event_item(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        """Serialize a realtime card while excluding the private handling note."""
        item = cls._mentor_item(row)
        item.pop("resolution_note", None)
        return item

    @classmethod
    def _public_item(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        """Backward-compatible name for the authenticated mentor representation."""
        return cls._mentor_item(row)

    async def _insert_and_publish(
        self,
        decisions: Iterable[AttentionDecision],
        *,
        publish: bool = True,
    ) -> list[dict[str, Any]]:
        created = self.store.insert_attention_decisions(decisions)
        if publish:
            for row in created:
                await self.event_bus.publish({
                    "type": "attention_updated",
                    "action": "created",
                    "item": self._event_item(row),
                })
        return [self._mentor_item(row) for row in created]

    async def project_analysis(self, analysis_id: int) -> list[dict[str, Any]]:
        return await self._project_analysis(analysis_id, publish=True)

    async def _project_analysis(
        self,
        analysis_id: int,
        *,
        publish: bool,
    ) -> list[dict[str, Any]]:
        target = self.store.get_analysis(analysis_id)
        if target is None:
            return []
        history = self.store.recent_analyses_as_of(
            student_id=str(target.get("student_id") or ""),
            session_id=str(target.get("session_id") or ""),
            created_at=_created_at(target),
            analysis_id=int(target["id"]),
            limit=3,
        )
        decision = self.policy.decide_analysis(target, history=history)
        return await self._insert_and_publish(
            [] if decision is None else [decision],
            publish=publish,
        )

    async def project_student_ask(self, ask_id: int) -> list[dict[str, Any]]:
        return await self._project_student_ask(ask_id, publish=True)

    async def _project_student_ask(
        self,
        ask_id: int,
        *,
        publish: bool,
    ) -> list[dict[str, Any]]:
        ask = self.store.get_student_ask(ask_id)
        if ask is None:
            return []
        return await self._insert_and_publish(
            self.policy.decide_student_ask(ask),
            publish=publish,
        )

    def _read_system_occurrence(
        self,
        source_kind: str,
        durable_key: str,
    ) -> Mapping[str, Any] | None:
        if source_kind not in {
            "stop", "bulk_analysis", "upload_transfer", "upload_analysis",
        }:
            raise ValueError("invalid attention system source kind")
        logical_key = str(durable_key)
        generation: int | None = None
        if source_kind == "bulk_analysis":
            raw_id_text, separator, generation_text = str(durable_key).partition(":")
            if not separator:
                return None
            try:
                logical_key = str(int(raw_id_text))
                generation = int(generation_text)
            except (TypeError, ValueError):
                return None
        occurrence = self.store.get_latest_system_failure_occurrence(
            kind=source_kind,
            logical_key=logical_key,
            generation=generation,
        )
        if occurrence is None:
            self.store.seed_legacy_system_failure_occurrences()
            occurrence = self.store.get_latest_system_failure_occurrence(
                kind=source_kind,
                logical_key=logical_key,
                generation=generation,
            )
        return occurrence

    async def project_system_failure(
        self,
        source_kind: str,
        durable_key: str,
    ) -> list[dict[str, Any]]:
        return await self._project_system_failure(
            source_kind,
            durable_key,
            publish=True,
        )

    async def _project_system_failure(
        self,
        source_kind: str,
        durable_key: str,
        *,
        publish: bool,
    ) -> list[dict[str, Any]]:
        occurrence = self._read_system_occurrence(source_kind, durable_key)
        if occurrence is None:
            return []
        decision = self.policy.decide_system_failure(source_kind, occurrence)
        return await self._insert_and_publish(
            [] if decision is None else [decision],
            publish=publish,
        )

    async def _project_system_occurrence(
        self,
        occurrence_id: int,
        *,
        publish: bool,
    ) -> list[dict[str, Any]]:
        occurrence = self.store.get_system_failure_occurrence(occurrence_id)
        if occurrence is None:
            return []
        source_kind = str(occurrence.get("kind") or "")
        decision = self.policy.decide_system_failure(source_kind, occurrence)
        return await self._insert_and_publish(
            [] if decision is None else [decision],
            publish=publish,
        )

    async def backfill_missing(
        self,
        *,
        publish: bool = False,
        page_size: int = 100,
        max_sources: int = 1000,
    ) -> int:
        """Replay a bounded, cursor-paginated scan of every durable source."""
        if (
            not isinstance(page_size, int)
            or isinstance(page_size, bool)
            or not 1 <= page_size <= 200
        ):
            raise ValueError("invalid attention backfill page size")
        if (
            not isinstance(max_sources, int)
            or isinstance(max_sources, bool)
            or not 0 <= max_sources <= 10_000
        ):
            raise ValueError("invalid attention backfill source limit")
        if max_sources == 0:
            return 0

        self.store.seed_legacy_system_failure_occurrences()
        created_count = 0
        source_kinds = (
            "analysis",
            "student_ask",
            "stop",
            "upload_transfer",
            "bulk_analysis",
            "upload_analysis",
        )
        for source_kind in source_kinds:
            processed_for_kind = 0
            cursor, cursor_version = (
                self.store.get_attention_backfill_cursor_state(source_kind)
            )
            wrapped = cursor == 0
            cursor_conflict = False
            while processed_for_kind < max_sources:
                page_limit = min(page_size, max_sources - processed_for_kind)
                if source_kind == "analysis":
                    rows = self.store.list_attention_analysis_sources(
                        after_id=cursor,
                        limit=page_limit,
                    )
                elif source_kind == "student_ask":
                    rows = self.store.list_attention_ask_sources(
                        after_id=cursor,
                        limit=page_limit,
                    )
                else:
                    rows = self.store.list_system_failure_occurrences(
                        kind=source_kind,
                        after_id=cursor,
                        limit=page_limit,
                    )
                if not rows:
                    if cursor > 0 and not wrapped:
                        next_version = (
                            self.store.compare_and_set_attention_backfill_cursor(
                                source_kind,
                                expected_version=cursor_version,
                                last_id=0,
                            )
                        )
                        if next_version is None:
                            cursor_conflict = True
                            break
                        cursor = 0
                        cursor_version = next_version
                        wrapped = True
                        continue
                    break
                for row in rows:
                    if source_kind == "analysis":
                        created = await self._project_analysis(
                            int(row["id"]),
                            publish=publish,
                        )
                    elif source_kind == "student_ask":
                        created = await self._project_student_ask(
                            int(row["id"]),
                            publish=publish,
                        )
                    else:
                        created = await self._project_system_occurrence(
                            int(row["id"]),
                            publish=publish,
                        )
                    created_count += len(created)
                    next_cursor = int(row["id"])
                    next_version = (
                        self.store.compare_and_set_attention_backfill_cursor(
                            source_kind,
                            expected_version=cursor_version,
                            last_id=next_cursor,
                        )
                    )
                    if next_version is None:
                        cursor_conflict = True
                        break
                    cursor = next_cursor
                    cursor_version = next_version
                    processed_for_kind += 1
                    if processed_for_kind >= max_sources:
                        break
                if cursor_conflict:
                    break
        return created_count

    def list_attention(
        self,
        *,
        status: str | None = None,
        priority: str | None = None,
        category: str | None = None,
        student_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = self.store.list_attention(
            status=status,
            priority=priority,
            category=category,
            student_id=student_id,
            limit=limit,
        )
        return [self._mentor_item(row) for row in rows]

    async def update_attention(
        self,
        item_id: int,
        *,
        status: str,
        mentor_id: str,
        note: str,
    ) -> dict[str, Any]:
        row, changed = self.store.update_attention_status(
            item_id,
            status=status,
            mentor_id=mentor_id,
            note=note,
        )
        if changed:
            try:
                await self.event_bus.publish({
                    "type": "attention_updated",
                    "action": "updated",
                    "item": self._event_item(row),
                })
            except Exception:
                log.exception(
                    "attention update fanout failed after durable transition item=%s",
                    item_id,
                )
        return self._mentor_item(row)


__all__ = ["AttentionDecision", "AttentionPolicy", "AttentionService"]
