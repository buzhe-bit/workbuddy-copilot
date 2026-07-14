from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import threading

import pytest

from copilot import service as service_module
from copilot.connections import FORWARD_EVENT_TYPES, WSRegistry
from copilot.eventbus import EventBus
from copilot.app_context import AppContext
from copilot.models import Student
from copilot.service import (
    _analyze_uploaded_session_background,
    _refresh_upload_parent_projections,
    _recover_pending_reports,
    create_app,
)
from copilot.services import (
    AnalysisRetriesExhausted,
    AnalysisService,
    MessageService,
    SessionQueryService,
)
from copilot.store import Store
from copilot.upload_service import UploadRequestService


ATTENTION_COLUMNS = [
    "id",
    "source_type",
    "source_id",
    "category",
    "student_id",
    "session_id",
    "priority",
    "reason_code",
    "reason",
    "evidence_json",
    "suggested_action",
    "confidence",
    "status",
    "handled_by",
    "resolution_note",
    "handled_at",
    "created_at",
    "updated_at",
]


def _attention_module():
    """Load the wished-for Task 5 domain without hiding a missing module."""
    try:
        return importlib.import_module("copilot.attention")
    except ModuleNotFoundError as exc:
        if exc.name != "copilot.attention":
            raise
        pytest.fail("Task 5 RED: copilot.attention does not exist yet", pytrace=False)


def _policy():
    return _attention_module().AttentionPolicy()


def _analysis(
    analysis_id: int,
    *,
    created_at: float,
    understanding: str = "medium",
    severity: str = "info",
    student_id: str = "student-a",
    session_id: str = "session-a",
    confidence: float = 0.5,
    off_topic: bool = False,
    evidence_json: str = "[]",
) -> dict:
    return {
        "id": analysis_id,
        "student_id": student_id,
        "session_id": session_id,
        "created_at": created_at,
        "understanding": understanding,
        "severity": severity,
        "off_topic": 1 if off_topic else 0,
        "confidence": confidence,
        "evidence_json": evidence_json,
        "diagnosis": "bounded diagnosis",
        "suggestion": "bounded action",
    }


def _attention_record(
    *,
    source_id: str = "1",
    reason_code: str = "analysis_error",
    student_id: str = "student-a",
    priority: str = "high",
    created_at: float = 10.0,
) -> dict:
    """Persistence-shaped input; generated handling fields use Store defaults."""
    return {
        "source_type": "analysis",
        "source_id": source_id,
        "category": "learning",
        "student_id": student_id,
        "session_id": "session-a",
        "priority": priority,
        "reason_code": reason_code,
        "reason": "Learner needs bounded mentor attention.",
        "evidence_json": json.dumps(["bounded evidence"]),
        "suggested_action": "Review the next concrete step.",
        "confidence": 0.9,
        "created_at": created_at,
    }


def _add_analysis(
    store: Store,
    *,
    understanding: str = "medium",
    severity: str = "info",
    confidence: float = 0.5,
    created_at: float | None = None,
    student_id: str = "student-a",
    session_id: str = "session-a",
) -> int:
    report_id = store.add_report(
        student_id,
        session_id,
        "Stop",
        "bounded prompt",
        "",
        1,
        0,
    )
    analysis_id = store.add_analysis(
        report_id,
        student_id,
        {
            "understanding": understanding,
            "severity": severity,
            "confidence": confidence,
            "diagnosis": "bounded diagnosis",
            "suggestion": "bounded action",
            "evidence": ["bounded evidence"],
        },
        session_id,
        "Session A",
    )
    if created_at is not None:
        with store._conn() as conn:
            conn.execute(
                "UPDATE analyses SET created_at = ? WHERE id = ?",
                (created_at, analysis_id),
            )
    return analysis_id


@pytest.mark.parametrize(
    ("target", "expected_priority"),
    [
        (_analysis(1, created_at=1.0, severity="error"), "high"),
        (_analysis(1, created_at=1.0), None),
    ],
    ids=["error-is-high", "normal-has-no-item"],
)
def test_analysis_policy_error_and_normal(target, expected_priority):
    decision = _policy().decide_analysis(target, history=[target])

    assert (decision.priority if decision else None) == expected_priority


@pytest.mark.parametrize(
    ("confidence", "expected_priority"),
    [(0.64, None), (0.65, "high")],
)
def test_stuck_requires_confidence_at_least_point_65(confidence, expected_priority):
    target = _analysis(
        1,
        created_at=1.0,
        understanding="stuck",
        confidence=confidence,
    )

    decision = _policy().decide_analysis(target, history=[target])

    assert (decision.priority if decision else None) == expected_priority
    if decision:
        assert decision.reason_code == "analysis_confident_stuck"


def test_analysis_policy_uses_latest_three_as_of_target_for_repeated_low():
    target = _analysis(3, created_at=20.0, understanding="low")
    history = [
        _analysis(4, created_at=20.0, understanding="medium"),  # same ts, after target
        target,
        _analysis(2, created_at=20.0, understanding="stuck"),
        _analysis(1, created_at=10.0, understanding="medium"),
        _analysis(5, created_at=30.0, understanding="low"),  # future row
    ]

    decision = _policy().decide_analysis(target, history=history)

    assert decision is not None
    assert decision.priority == "high"
    assert decision.reason_code == "analysis_repeated_low"


def test_same_timestamp_row_after_target_is_excluded_from_cutoff():
    target = _analysis(3, created_at=20.0, understanding="medium")
    history = [
        _analysis(2, created_at=20.0, understanding="low"),
        target,
        _analysis(4, created_at=20.0, understanding="stuck"),
        _analysis(5, created_at=30.0, understanding="low"),
    ]

    assert _policy().decide_analysis(target, history=history) is None


@pytest.mark.parametrize(
    ("target", "history", "expected_priority"),
    [
        (
            _analysis(3, created_at=30.0, understanding="low", session_id="session-a"),
            [
                _analysis(1, created_at=10.0, understanding="low", session_id="session-b"),
                _analysis(2, created_at=20.0, understanding="stuck", session_id="session-b"),
            ],
            "medium",
        ),
        (
            _analysis(3, created_at=30.0, understanding="low", session_id=""),
            [
                _analysis(1, created_at=10.0, understanding="medium", session_id="session-a"),
                _analysis(2, created_at=20.0, understanding="stuck", session_id="session-b"),
            ],
            "high",
        ),
        (
            _analysis(3, created_at=30.0, understanding="low", student_id="student-a"),
            [
                _analysis(1, created_at=10.0, understanding="low", student_id="student-b"),
                _analysis(2, created_at=20.0, understanding="stuck", student_id="student-b"),
            ],
            "medium",
        ),
    ],
    ids=["nonempty-session-isolated", "empty-session-student-wide", "student-isolated"],
)
def test_repeated_low_window_scopes_by_student_and_session(
    target,
    history,
    expected_priority,
):
    decision = _policy().decide_analysis(target, history=[*history, target])

    assert decision is not None
    assert decision.priority == expected_priority


@pytest.mark.parametrize(
    ("target", "expected_reason_code"),
    [
        (_analysis(1, created_at=1.0, severity="warn"), "analysis_warning"),
        (_analysis(1, created_at=1.0, off_topic=True), "analysis_off_topic"),
        (_analysis(1, created_at=1.0, understanding="low"), "analysis_low"),
    ],
    ids=["warning", "off-topic", "low-understanding"],
)
def test_analysis_policy_medium_rules(target, expected_reason_code):
    decision = _policy().decide_analysis(target, history=[target])

    assert decision is not None
    assert decision.priority == "medium"
    assert decision.reason_code == expected_reason_code


def test_analysis_policy_emits_only_highest_precedence_reason():
    target = _analysis(
        1,
        created_at=1.0,
        severity="error",
        understanding="stuck",
        confidence=0.99,
        off_topic=True,
    )

    decision = _policy().decide_analysis(target, history=[target])

    assert decision is not None
    assert decision.priority == "high"
    assert decision.reason_code == "analysis_error"


@pytest.mark.parametrize(
    ("evidence_json", "expected"),
    [
        ("{not-json", []),
        (
            json.dumps(["x" * 200, "  second  ", "", 7, "third", "fourth"]),
            ["x" * 160, "second", "third"],
        ),
    ],
    ids=["corrupted-legacy-json", "three-snippets-of-160-chars"],
)
def test_analysis_policy_normalizes_bounded_evidence(evidence_json, expected):
    target = _analysis(
        1,
        created_at=1.0,
        severity="error",
        evidence_json=evidence_json,
    )

    decision = _policy().decide_analysis(target, history=[target])

    assert decision is not None
    assert list(decision.evidence) == expected


def test_analysis_policy_keeps_bounded_diagnosis_and_suggestion_for_mentor_context():
    target = _analysis(1, created_at=1.0, severity="error")
    target["diagnosis"] = "  " + "d" * 600
    target["suggestion"] = "  " + "s" * 600

    decision = _policy().decide_analysis(target, history=[target])

    assert decision is not None
    assert decision.reason == "d" * 500
    assert decision.suggested_action == "s" * 500


def test_attention_schema_has_exact_fields_indexes_and_no_analysis_fk(tmp_path):
    store = Store(tmp_path / "attention.db")

    with store._conn() as conn:
        columns = conn.execute("PRAGMA table_info(attention_items)").fetchall()
        foreign_keys = conn.execute("PRAGMA foreign_key_list(attention_items)").fetchall()
        indexes = conn.execute("PRAGMA index_list(attention_items)").fetchall()
        index_columns = {
            tuple(
                row["name"]
                for row in conn.execute(f"PRAGMA index_info({index['name']})").fetchall()
            ): bool(index["unique"])
            for index in indexes
        }

    assert [row["name"] for row in columns] == ATTENTION_COLUMNS
    assert next(row["type"] for row in columns if row["name"] == "source_id") == "TEXT"
    assert all(row["table"] != "analyses" for row in foreign_keys)
    assert index_columns[("source_type", "source_id", "reason_code")] is True
    assert ("status", "priority", "created_at", "id") in index_columns
    assert ("student_id", "status") in index_columns


def test_attention_migration_is_repeatable(tmp_path):
    db_path = tmp_path / "repeatable.db"
    first = Store(db_path)
    first.upsert_student("student-a", "A")

    second = Store(db_path)

    with second._conn() as conn:
        columns = conn.execute("PRAGMA table_info(attention_items)").fetchall()
        student = conn.execute(
            "SELECT display_name FROM students WHERE student_id = ?",
            ("student-a",),
        ).fetchone()
    assert [row["name"] for row in columns] == ATTENTION_COLUMNS
    assert student["display_name"] == "A"


@pytest.mark.parametrize("terminal_status", ["resolved", "dismissed"])
def test_attention_insert_is_unique_by_source_reason_and_never_reopens_terminal(
    tmp_path,
    terminal_status,
):
    store = Store(tmp_path / "idempotent.db")
    store.upsert_student("student-a")
    record = _attention_record()

    created = store.insert_attention_decisions([record])
    replayed = store.insert_attention_decisions([record])
    second_reason = store.insert_attention_decisions([
        _attention_record(reason_code="analysis_confident_stuck"),
    ])
    terminal, changed = store.update_attention_status(
        created[0]["id"],
        status=terminal_status,
        mentor_id="mentor-a",
        note="handled",
    )
    after_terminal_replay = store.insert_attention_decisions([record])
    persisted = next(
        item
        for item in store.list_attention(status=terminal_status, limit=10)
        if item["id"] == created[0]["id"]
    )
    later_source = store.insert_attention_decisions([
        _attention_record(source_id="2", created_at=20.0),
    ])

    assert len(created) == 1
    assert replayed == []
    assert len(second_reason) == 1
    assert changed is True
    assert terminal["status"] == terminal_status
    assert after_terminal_replay == []
    assert persisted["status"] == terminal_status
    assert len(later_source) == 1
    assert later_source[0]["source_id"] == "2"


def test_attention_unique_source_rejects_conflicting_student_identity(tmp_path):
    store = Store(tmp_path / "attention-source-identity-conflict.db")
    store.insert_attention_decisions([_attention_record(student_id="student-a")])

    with pytest.raises(ValueError, match="attention source identity conflict"):
        store.insert_attention_decisions([
            _attention_record(student_id="student-b"),
        ])

    rows = store.list_attention(limit=20)
    assert [row["student_id"] for row in rows] == ["student-a"]


class _FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


def test_attention_updated_fans_out_to_mentors_only():
    async def scenario():
        registry = WSRegistry(send_timeout=0.05)
        mentor = _FakeWebSocket()
        target_float = _FakeWebSocket()
        other_float = _FakeWebSocket()
        registry.register_mentor(mentor)
        registry.register_float("student-a", target_float)
        registry.register_float("student-b", other_float)
        payload = {
            "type": "attention_updated",
            "action": "created",
            "item": {
                "id": 7,
                "student_id": "student-a",
                "priority": "high",
                "status": "open",
                "reason_code": "analysis_error",
            },
        }

        await registry.handle_event(payload)

        assert "attention_updated" not in FORWARD_EVENT_TYPES
        assert mentor.sent == [payload]
        assert target_float.sent == []
        assert other_float.sent == []

    asyncio.run(scenario())


def test_student_attention_defaults_are_backward_compatible():
    student = Student(student_id="legacy-student")

    assert student.open_attention_count == 0
    assert student.highest_attention_priority == ""
    assert student.last_attention_at == 0


def test_student_overview_uses_independent_active_attention_aggregate(tmp_path):
    store = Store(tmp_path / "student-overview.db")
    for index in range(2):
        report_id = store.add_report(
            "student-a",
            "session-a",
            "Stop",
            f"prompt-{index}",
            "",
            1,
            0,
        )
        store.add_analysis(
            report_id,
            "student-a",
            {"severity": "info", "understanding": "medium"},
            "session-a",
            "Session A",
        )

    inserted = store.insert_attention_decisions([
        _attention_record(source_id="101", priority="high", created_at=10.0),
        _attention_record(source_id="102", priority="medium", created_at=20.0),
        _attention_record(source_id="103", priority="high", created_at=99.0),
    ])
    store.update_attention_status(
        inserted[1]["id"],
        status="in_progress",
        mentor_id="mentor-a",
        note="triaging",
    )
    store.update_attention_status(
        inserted[2]["id"],
        status="resolved",
        mentor_id="mentor-a",
        note="handled",
    )

    row = next(item for item in store.students_overview() if item["student_id"] == "student-a")
    student = SessionQueryService(
        store,
        {"student_id": "student-a", "student_name": "Student A"},
    ).list_students()[0]

    assert row["analysis_count"] == 2
    assert row["open_attention_count"] == 2
    assert row["highest_attention_priority"] == "high"
    assert row["last_attention_at"] == 20.0
    assert student.open_attention_count == 2
    assert student.highest_attention_priority == "high"
    assert student.last_attention_at == 20.0


def test_attention_service_project_analysis_accepts_only_durable_id(tmp_path):
    attention = _attention_module()
    signature = inspect.signature(attention.AttentionService.project_analysis)

    assert list(signature.parameters) == ["self", "analysis_id"]

    store = Store(tmp_path / "durable-source.db")
    analysis_id = _add_analysis(store, severity="error")
    events: list[dict] = []
    bus = EventBus()

    async def capture(payload: dict) -> None:
        events.append(payload)

    bus.subscribe(capture)
    service = attention.AttentionService(store=store, event_bus=bus)

    first = asyncio.run(service.project_analysis(analysis_id))
    replay = asyncio.run(service.project_analysis(analysis_id))
    rows = store.list_attention(limit=20)

    assert len(first) == 1
    assert replay == []
    assert [(row["source_type"], row["source_id"]) for row in rows] == [
        ("analysis", str(analysis_id)),
    ]
    assert rows[0]["priority"] == "high"
    assert [event["type"] for event in events] == ["attention_updated"]


def test_direct_projection_racing_backfill_inserts_and_publishes_once(tmp_path):
    attention = _attention_module()
    db_path = tmp_path / "race.db"
    store_a = Store(db_path)
    analysis_id = _add_analysis(store_a, severity="error")
    store_b = Store(db_path)
    events_a: list[dict] = []
    events_b: list[dict] = []
    bus_a = EventBus()
    bus_b = EventBus()

    async def capture_a(payload: dict) -> None:
        events_a.append(payload)

    async def capture_b(payload: dict) -> None:
        events_b.append(payload)

    bus_a.subscribe(capture_a)
    bus_b.subscribe(capture_b)
    direct = attention.AttentionService(store=store_a, event_bus=bus_a)
    backfill = attention.AttentionService(store=store_b, event_bus=bus_b)

    async def race() -> None:
        await asyncio.gather(
            direct.project_analysis(analysis_id),
            backfill.backfill_missing(publish=True, page_size=1, max_sources=10),
        )

    asyncio.run(race())

    rows = store_a.list_attention(limit=20)
    assert len(rows) == 1
    assert len(events_a) + len(events_b) == 1
    assert (events_a + events_b)[0]["type"] == "attention_updated"


def test_two_sqlite_connections_racing_insert_have_exactly_one_creator(tmp_path):
    db_path = tmp_path / "sqlite-race.db"
    store_a = Store(db_path)
    store_a.upsert_student("student-a")
    store_b = Store(db_path)
    barrier = threading.Barrier(2)
    results: list[list[dict]] = []
    errors: list[BaseException] = []

    def insert(store: Store) -> None:
        try:
            barrier.wait(timeout=2)
            results.append(store.insert_attention_decisions([_attention_record()]))
        except BaseException as exc:  # captured and asserted in the parent thread
            errors.append(exc)

    threads = [
        threading.Thread(target=insert, args=(store_a,)),
        threading.Thread(target=insert, args=(store_b,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert sum(len(created) for created in results) == 1
    assert len(store_a.list_attention(limit=20)) == 1


def test_publish_failure_keeps_durable_attention_for_authoritative_get(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "publish-failure.db")
    analysis_id = _add_analysis(store, severity="error")

    class ExplodingBus:
        async def publish(self, payload: dict) -> None:
            raise RuntimeError("socket fanout failed")

    service = attention.AttentionService(store=store, event_bus=ExplodingBus())

    async def project() -> None:
        try:
            await service.project_analysis(analysis_id)
        except RuntimeError as exc:
            assert str(exc) == "socket fanout failed"

    asyncio.run(project())

    rows = store.list_attention(status="open", limit=20)
    assert len(rows) == 1
    assert rows[0]["source_id"] == str(analysis_id)


def test_backfill_is_paginated_and_uses_historical_as_of_window(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "historical-backfill.db")
    first_id = _add_analysis(
        store,
        understanding="low",
        created_at=10.0,
    )
    second_id = _add_analysis(
        store,
        understanding="stuck",
        confidence=0.1,
        created_at=20.0,
    )
    service = attention.AttentionService(store=store, event_bus=EventBus())

    asyncio.run(service.backfill_missing(publish=False, page_size=1, max_sources=10))

    by_source = {row["source_id"]: row for row in store.list_attention(limit=20)}
    assert by_source[str(first_id)]["priority"] == "medium"
    assert by_source[str(first_id)]["reason_code"] == "analysis_low"
    assert by_source[str(second_id)]["priority"] == "high"
    assert by_source[str(second_id)]["reason_code"] == "analysis_repeated_low"


def test_backfill_cap_does_not_starve_later_source_kinds_and_publish_false_is_silent(
    tmp_path,
):
    attention = _attention_module()
    store = Store(tmp_path / "fair-backfill.db")
    for created_at in (1.0, 2.0, 3.0):
        _add_analysis(store, created_at=created_at)

    ask_id = store.add_student_ask(
        "student-a",
        "session-a",
        "SECRET_FAIR_ASK_QUESTION",
        "SECRET_FAIR_ASK_ANSWER",
        answer_status="degraded",
        error_code="llm_disabled",
    )
    store.record_student_ask_feedback(
        ask_id,
        "student-a",
        "unresolved",
        "SECRET_FAIR_FEEDBACK_NOTE",
    )
    stop_id = store.add_report(
        "student-a", "session-a", "Stop", "prompt", "", 1, 0,
    )
    with store._conn() as conn:
        conn.execute(
            """UPDATE reports
               SET analysis_status = 'failed', analysis_attempts = 3,
                   analysis_error = 'SECRET_FAIR_STOP_ERROR'
               WHERE id = ?""",
            (stop_id,),
        )

    request_id = store.add_upload_request(
        "mentor-a",
        "student-a",
        "session-a",
        request_id="fair-upload",
    )
    store.update_upload_request_status(
        request_id,
        student_id="student-a",
        status="failed",
        error_message="SECRET_FAIR_UPLOAD_ERROR",
    )
    store.replace_session_messages(
        "session-b",
        "student-a",
        [{"seq": 0, "role": "user", "text": "SECRET_FAIR_TRANSCRIPT", "ts": 1.0}],
        "SECRET_FAIR_TRANSCRIPT",
        "fair-sha",
    )
    with store._conn() as conn:
        raw = conn.execute(
            "SELECT id FROM raw_transcripts WHERE session_id = 'session-b'",
        ).fetchone()
        raw_id = int(raw["id"])
        conn.execute(
            """UPDATE raw_transcripts
               SET analysis_status = 'failed', analysis_generation = 2,
                   analysis_error = 'SECRET_FAIR_BULK_ERROR'
               WHERE id = ?""",
            (raw_id,),
        )

    events: list[dict] = []
    bus = EventBus()

    async def capture(payload: dict) -> None:
        events.append(payload)

    bus.subscribe(capture)
    service = attention.AttentionService(store=store, event_bus=bus)

    created_count = asyncio.run(service.backfill_missing(
        publish=False,
        page_size=1,
        max_sources=2,
    ))

    actual = {
        (row["source_type"], row["source_id"], row["reason_code"])
        for row in store.list_attention(limit=20)
    }
    assert created_count == 5
    assert actual == {
        ("student_ask", str(ask_id), "student_ask_degraded"),
        ("student_ask", str(ask_id), "student_ask_unresolved"),
        ("system", f"stop:{stop_id}:3", "system_stop_retries_exhausted"),
        (
            "system",
            "upload-transfer:fair-upload:1",
            "system_upload_transfer_failed",
        ),
        ("system", f"bulk:{raw_id}:2", "system_bulk_analysis_failed"),
    }
    assert events == []


def test_backfill_persists_each_kind_cursor_across_service_restart(tmp_path):
    attention = _attention_module()
    db_path = tmp_path / "persistent-backfill-cursor.db"
    first_store = Store(db_path)
    _add_analysis(first_store, created_at=1.0)
    _add_analysis(first_store, created_at=2.0)
    error_id = _add_analysis(first_store, severity="error", created_at=3.0)

    first_service = attention.AttentionService(
        store=first_store,
        event_bus=EventBus(),
    )
    first_created = asyncio.run(first_service.backfill_missing(
        publish=False,
        page_size=1,
        max_sources=2,
    ))

    assert first_created == 0
    assert first_store.list_attention(limit=20) == []

    restarted_store = Store(db_path)
    restarted_service = attention.AttentionService(
        store=restarted_store,
        event_bus=EventBus(),
    )
    second_created = asyncio.run(restarted_service.backfill_missing(
        publish=False,
        page_size=1,
        max_sources=2,
    ))

    assert second_created == 1
    assert [row["source_id"] for row in restarted_store.list_attention(limit=20)] == [
        str(error_id),
    ]


@pytest.mark.parametrize(
    ("answer_status", "expected_reason"),
    [
        ("degraded", "student_ask_degraded"),
        ("failed", "student_ask_failed"),
    ],
)
def test_student_ask_policy_marks_degraded_and_failed_as_high(
    answer_status,
    expected_reason,
):
    decisions = _policy().decide_student_ask({
        "id": 7,
        "student_id": "student-a",
        "session_id": "session-a",
        "answer_status": answer_status,
        "feedback": "",
        "question": "must not be copied in full",
        "answer": "must not be copied in full",
        "feedback_note": "",
        "created_at": 1.0,
    })

    assert [decision.reason_code for decision in decisions] == [expected_reason]
    assert decisions[0].category == "learning"
    assert decisions[0].priority == "high"
    serialized = repr(decisions)
    assert "must not be copied in full" not in serialized


def test_later_unresolved_feedback_adds_reason_without_reopening_first(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "ask-reasons.db")
    ask_id = store.add_student_ask(
        "student-a",
        "session-a",
        "SECRET_ASK_QUESTION_7f31",
        "SECRET_ASK_ANSWER_9c42",
        answer_status="degraded",
        error_code="llm_disabled",
    )
    events: list[dict] = []
    bus = EventBus()

    async def capture(payload: dict) -> None:
        events.append(payload)

    bus.subscribe(capture)
    service = attention.AttentionService(store=store, event_bus=bus)

    asyncio.run(service.project_student_ask(ask_id))
    first = store.list_attention(limit=20)[0]
    store.update_attention_status(
        first["id"],
        status="resolved",
        mentor_id="mentor-a",
        note="handled",
    )
    store.record_student_ask_feedback(
        ask_id,
        "student-a",
        "unresolved",
        "SECRET_ASK_FEEDBACK_NOTE_2a84",
    )
    asyncio.run(service.project_student_ask(ask_id))

    rows = store.list_attention(limit=20)
    by_reason = {row["reason_code"]: row for row in rows}
    assert set(by_reason) == {"student_ask_degraded", "student_ask_unresolved"}
    assert by_reason["student_ask_degraded"]["status"] == "resolved"
    assert by_reason["student_ask_unresolved"]["status"] == "open"
    serialized = json.dumps({"rows": rows, "events": events}, ensure_ascii=False)
    assert "SECRET_ASK_QUESTION_7f31" not in serialized
    assert "SECRET_ASK_ANSWER_9c42" not in serialized
    assert "SECRET_ASK_FEEDBACK_NOTE_2a84" not in serialized


def test_system_projection_uses_durable_terminal_rules_and_source_axes(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "system-sources.db")
    events: list[dict] = []
    bus = EventBus()

    async def capture(payload: dict) -> None:
        events.append(payload)

    bus.subscribe(capture)
    service = attention.AttentionService(store=store, event_bus=bus)

    stop_rows: list[tuple[int, int, str]] = []
    for attempts, error_code in [
        (2, "llm_provider_error"),
        (3, "llm_provider_error"),
        (1, "analysis_input_unavailable"),
    ]:
        report_id = store.add_report(
            "student-a", "session-a", "Stop", "prompt", "", 1, 0,
        )
        stop_rows.append((report_id, attempts, error_code))
        with store._conn() as conn:
            conn.execute(
                """UPDATE reports
                   SET analysis_status = 'failed', analysis_attempts = ?, analysis_error = ?,
                       analysis_next_retry_at = ?
                   WHERE id = ?""",
                (
                    attempts,
                    error_code,
                    123.0 if attempts == 2 else None,
                    report_id,
                ),
            )

    request_id = store.add_upload_request(
        "mentor-a", "student-a", "session-a", request_id="request-7",
    )
    store.update_upload_request_status(
        request_id,
        student_id="student-a",
        status="failed",
        error_message="private provider failure",
    )

    store.replace_session_messages(
        "session-b",
        "student-a",
        [{"seq": 0, "role": "user", "text": "hello", "ts": 1.0}],
        "private transcript",
        "sha-1",
    )
    with store._conn() as conn:
        raw = conn.execute(
            "SELECT id FROM raw_transcripts WHERE session_id = ?",
            ("session-b",),
        ).fetchone()
        raw_id = int(raw["id"])
        conn.execute(
            """UPDATE raw_transcripts
               SET analysis_status = 'failed', analysis_generation = 1,
                   analysis_error = 'private provider failure'
               WHERE id = ?""",
            (raw_id,),
        )

    async def project_all() -> None:
        for report_id, _, _ in stop_rows:
            await service.project_system_failure("stop", str(report_id))
        await service.project_system_failure("upload_transfer", request_id)
        await service.project_system_failure("bulk_analysis", f"{raw_id}:1")
        with store._conn() as conn:
            conn.execute(
                """UPDATE raw_transcripts
                   SET analysis_status = 'failed', analysis_generation = 2
                   WHERE id = ?""",
                (raw_id,),
            )
        await service.project_system_failure("bulk_analysis", f"{raw_id}:2")

    asyncio.run(project_all())

    rows = store.list_attention(limit=20)
    source_ids = {row["source_id"] for row in rows}
    assert not any(source.startswith(f"stop:{stop_rows[0][0]}:") for source in source_ids)
    assert f"stop:{stop_rows[1][0]}:3" in source_ids
    assert f"stop:{stop_rows[2][0]}:1" in source_ids
    assert "upload-transfer:request-7:1" in source_ids
    assert f"bulk:{raw_id}:1" in source_ids
    assert f"bulk:{raw_id}:2" in source_ids
    assert all(row["source_type"] == "system" for row in rows)
    assert all(row["category"] == "system" for row in rows)
    assert all(row["priority"] == "high" for row in rows)
    serialized = json.dumps({"rows": rows, "events": events}, ensure_ascii=False)
    assert "private provider failure" not in serialized
    assert "private transcript" not in serialized


def test_stop_success_projects_only_after_durable_analysis_commit(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "stop-trigger.db")

    class ExplodingBus(EventBus):
        async def publish(self, payload: dict) -> None:
            raise RuntimeError("transient stop fanout failed")

    bus = ExplodingBus()
    attention_service = attention.AttentionService(store=store, event_bus=bus)

    async def fake_llm(config, snapshot, event, latest_prompt):
        return {
            "topic": "debugging",
            "understanding": "stuck",
            "severity": "error",
            "confidence": 0.9,
            "diagnosis": "bounded diagnosis",
            "suggestion": "bounded action",
            "evidence": ["bounded evidence"],
        }

    config = {"service": {"analysis_max_concurrency": 2}, "analysis": {}}
    analysis_service = AnalysisService(
        store,
        fake_llm,
        config,
        bus,
        attention_service=attention_service,
    )
    report_id = store.add_report(
        "student-a", "session-a", "Stop", "prompt", "", 1, 0,
    )

    result = asyncio.run(analysis_service.handle_stop(
        "student-a",
        "session-a",
        "prompt",
        "",
        report_id,
    ))

    analyses = store.recent_analyses("student-a", limit=10)
    rows = store.list_attention(limit=20)
    assert result.severity == "error"
    assert len(analyses) == 1
    assert len(rows) == 1
    assert rows[0]["source_id"] == str(analyses[0]["id"])


def test_stop_projection_failure_does_not_mask_or_rollback_committed_source(tmp_path):
    _attention_module()
    store = Store(tmp_path / "stop-projection-failure.db")
    bus = EventBus()

    class FailingAttentionService:
        async def project_analysis(self, analysis_id: int):
            raise RuntimeError("projection failed after source commit")

    async def fake_llm(config, snapshot, event, latest_prompt):
        return {
            "topic": "debugging",
            "understanding": "medium",
            "severity": "info",
            "diagnosis": "bounded diagnosis",
            "suggestion": "bounded action",
        }

    analysis_service = AnalysisService(
        store,
        fake_llm,
        {"service": {"analysis_max_concurrency": 2}},
        bus,
        attention_service=FailingAttentionService(),
    )
    report_id = store.add_report(
        "student-a", "session-a", "Stop", "prompt", "", 1, 0,
    )

    result = asyncio.run(analysis_service.handle_stop(
        "student-a", "session-a", "prompt", "", report_id,
    ))

    assert result.topic == "debugging"
    assert len(store.recent_analyses("student-a", limit=10)) == 1
    assert store.get_report(report_id)["analysis_status"] == "done"


def test_bulk_success_projects_committed_analysis_id(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "bulk-trigger.db")

    class ExplodingBus(EventBus):
        async def publish(self, payload: dict) -> None:
            raise RuntimeError("transient bulk fanout failed")

    bus = ExplodingBus()
    registry = WSRegistry(send_timeout=0.05)
    bus.subscribe(registry.handle_event)
    attention_service = attention.AttentionService(store=store, event_bus=bus)
    turns = [
        {"seq": 0, "role": "user", "text": "why broken", "ts": 1.0},
        {"seq": 0, "role": "assistant", "text": "inspect boundary", "ts": 2.0},
    ]

    async def fake_llm(config, snapshot, event, latest_prompt):
        return {
            "topic": "bulk debugging",
            "understanding": "medium",
            "severity": "error",
            "confidence": 0.9,
            "diagnosis": "bounded diagnosis",
            "suggestion": "bounded action",
            "evidence": ["bounded evidence"],
        }

    config = {
        "student_id": "server",
        "store": {"db_path": str(tmp_path / "bulk-trigger.db")},
        "service": {"analysis_max_concurrency": 2},
        "llm": {"enable_llm": True},
    }
    analysis_service = AnalysisService(
        store,
        fake_llm,
        config,
        bus,
        attention_service=attention_service,
    )
    context = AppContext(
        config=config,
        store=store,
        analysis_svc=analysis_service,
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=registry,
    )
    context.attention_svc = attention_service
    store.replace_session_messages(
        "session-a", "student-a", turns, "private transcript", "sha-a",
    )
    store.queue_raw_transcript_analysis(
        student_id="student-a",
        session_id="session-a",
        content_sha256="sha-a",
    )

    success, error = asyncio.run(_analyze_uploaded_session_background(
        context,
        "student-a",
        "session-a",
        turns,
        "sha-a",
    ))

    analyses = store.recent_analyses("student-a", limit=10)
    rows = store.list_attention(limit=20)
    assert success is True
    assert error == ""
    assert len(analyses) == 1
    assert len(rows) == 1
    assert rows[0]["source_id"] == str(analyses[0]["id"])


def test_bulk_missing_projection_metadata_does_not_reverse_durable_success(
    tmp_path,
    monkeypatch,
):
    attention = _attention_module()
    store = Store(tmp_path / "bulk-missing-projection-metadata.db")
    bus = EventBus()
    attention_service = attention.AttentionService(store=store, event_bus=bus)
    turns = [{"seq": 0, "role": "user", "text": "why", "ts": 1.0}]

    async def fake_llm(config, snapshot, event, latest_prompt):
        return {
            "topic": "bulk debugging",
            "understanding": "medium",
            "severity": "error",
            "diagnosis": "bounded diagnosis",
            "suggestion": "bounded action",
        }

    config = {
        "student_id": "server",
        "store": {"db_path": str(store.db_path)},
        "service": {"analysis_max_concurrency": 2},
        "llm": {"enable_llm": True},
    }
    analysis_service = AnalysisService(
        store,
        fake_llm,
        config,
        bus,
        attention_service=attention_service,
    )
    context = AppContext(
        config=config,
        store=store,
        analysis_svc=analysis_service,
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=WSRegistry(send_timeout=0.05),
        attention_svc=attention_service,
    )
    store.replace_session_messages(
        "session-a", "student-a", turns, "SECRET_BULK_TRANSCRIPT", "sha-a",
    )
    store.queue_raw_transcript_analysis(
        student_id="student-a",
        session_id="session-a",
        content_sha256="sha-a",
    )
    real_commit = store.commit_bulk_analysis_if_current

    def commit_without_projection_metadata(**kwargs):
        committed = real_commit(**kwargs)
        assert committed is not None
        result = dict(committed)
        result.pop("analysis_id")
        result.pop("report_id")
        return result

    monkeypatch.setattr(
        store,
        "commit_bulk_analysis_if_current",
        commit_without_projection_metadata,
    )
    real_refresh = service_module._refresh_upload_parent_projections
    refresh_calls = 0

    async def fail_only_after_bulk_commit(*args, **kwargs):
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 2:
            raise RuntimeError("transient post-commit parent projection failed")
        return await real_refresh(*args, **kwargs)

    monkeypatch.setattr(
        service_module,
        "_refresh_upload_parent_projections",
        fail_only_after_bulk_commit,
    )

    success, error = asyncio.run(_analyze_uploaded_session_background(
        context,
        "student-a",
        "session-a",
        turns,
        "sha-a",
    ))

    raw = store.get_raw_transcript_for_student_session_sha(
        "student-a", "session-a", "sha-a",
    )
    analyses = store.recent_analyses("student-a", limit=10)
    assert success is True
    assert error == ""
    assert refresh_calls == 2
    assert raw is not None and raw["analysis_status"] == "done"
    assert len(analyses) == 1
    assert store.list_attention(limit=20) == []

    asyncio.run(attention_service.backfill_missing(
        publish=False,
        page_size=1,
        max_sources=10,
    ))
    assert [row["source_id"] for row in store.list_attention(limit=20)] == [
        str(analyses[0]["id"]),
    ]


def test_stop_first_two_failures_have_no_system_item_and_third_projects(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "stop-terminal-trigger.db")
    bus = EventBus()
    attention_service = attention.AttentionService(store=store, event_bus=bus)
    calls = 0

    async def always_fail(config, snapshot, event, latest_prompt):
        nonlocal calls
        calls += 1
        raise TimeoutError("private provider details")

    analysis_service = AnalysisService(
        store,
        always_fail,
        {"service": {"analysis_max_concurrency": 2}},
        bus,
        attention_service=attention_service,
    )
    accepted = analysis_service.accept_report(
        student_id="student-a",
        session_id="session-a",
        event="Stop",
        prompt_text="prompt",
        transcript_content="durable bounded input",
    )
    observed_before_attempt: list[int] = []

    async def no_wait(delay: float) -> None:
        observed_before_attempt.append(len(store.list_attention(limit=20)))

    async def run() -> None:
        with pytest.raises(AnalysisRetriesExhausted):
            await analysis_service.handle_stop_with_retry(
                student_id="ignored",
                session_id="ignored",
                prompt_text="ignored",
                transcript_content="ignored",
                report_id=accepted.report_id,
                max_attempts=3,
                sleeper=no_wait,
            )

    asyncio.run(run())

    report = store.get_report(accepted.report_id)
    rows = store.list_attention(limit=20)
    assert calls == 3
    assert observed_before_attempt == [0, 0, 0]
    assert report["analysis_status"] == "failed"
    assert report["analysis_attempts"] == 3
    assert [row["source_id"] for row in rows] == [
        f"stop:{accepted.report_id}:3",
    ]
    assert "private provider details" not in json.dumps(rows, ensure_ascii=False)


def test_stop_max_attempts_one_projects_terminal_system_item(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "stop-max-one.db")
    service = attention.AttentionService(store=store, event_bus=EventBus())

    async def always_fail(config, snapshot, event, latest_prompt):
        raise TimeoutError("SECRET_MAX_ONE_PROVIDER_ERROR")

    analysis_service = AnalysisService(
        store,
        always_fail,
        {"service": {"analysis_max_concurrency": 2}},
        EventBus(),
        attention_service=service,
    )
    accepted = analysis_service.accept_report(
        student_id="student-a",
        session_id="session-a",
        event="Stop",
        prompt_text="prompt",
        transcript_content="bounded input",
    )

    async def no_wait(delay: float) -> None:
        return None

    with pytest.raises(AnalysisRetriesExhausted):
        asyncio.run(analysis_service.handle_stop_with_retry(
            student_id="ignored",
            session_id="ignored",
            prompt_text="ignored",
            transcript_content="ignored",
            report_id=accepted.report_id,
            max_attempts=1,
            sleeper=no_wait,
        ))

    report = store.get_report(accepted.report_id)
    rows = store.list_attention(limit=20)
    assert report is not None and report["analysis_next_retry_at"] is None
    assert [row["source_id"] for row in rows] == [
        f"stop:{accepted.report_id}:1",
    ]


def test_terminal_stop_is_not_auto_reclaimed_by_a_larger_startup_budget(tmp_path):
    store = Store(tmp_path / "terminal-stop-recovery.db")
    report_id = store.add_report(
        "student-a", "session-a", "Stop", "prompt", "", 1, 0,
    )
    with store._conn() as conn:
        conn.execute(
            "UPDATE reports SET analysis_status = 'pending' WHERE id = ?",
            (report_id,),
        )
    store.set_report_analysis_input_if_missing(report_id, "bounded input")
    claimed = store.claim_report_analysis(report_id, max_attempts=1)
    assert claimed is not None
    store.mark_report_analysis_failed(
        report_id,
        attempt=1,
        error_code="analysis_timeout_error",
        next_retry_at=None,
    )

    assert store.list_recoverable_reports(max_attempts=3) == []
    assert store.claim_report_analysis(report_id, max_attempts=3) is None


def test_stop_max_attempts_five_does_not_project_after_third_retryable_failure(
    tmp_path,
):
    attention = _attention_module()
    store = Store(tmp_path / "stop-max-five.db")
    service = attention.AttentionService(store=store, event_bus=EventBus())
    provider_calls = 0

    async def always_fail(config, snapshot, event, latest_prompt):
        nonlocal provider_calls
        provider_calls += 1
        raise TimeoutError("SECRET_MAX_FIVE_PROVIDER_ERROR")

    analysis_service = AnalysisService(
        store,
        always_fail,
        {"service": {"analysis_max_concurrency": 2}},
        EventBus(),
        attention_service=service,
    )
    accepted = analysis_service.accept_report(
        student_id="student-a",
        session_id="session-a",
        event="Stop",
        prompt_text="prompt",
        transcript_content="bounded input",
    )

    class StopAfterThirdFailure(RuntimeError):
        pass

    sleeper_calls = 0

    async def stop_before_fourth(delay: float) -> None:
        nonlocal sleeper_calls
        sleeper_calls += 1
        if sleeper_calls == 4:
            raise StopAfterThirdFailure

    with pytest.raises(StopAfterThirdFailure):
        asyncio.run(analysis_service.handle_stop_with_retry(
            student_id="ignored",
            session_id="ignored",
            prompt_text="ignored",
            transcript_content="ignored",
            report_id=accepted.report_id,
            max_attempts=5,
            sleeper=stop_before_fourth,
        ))

    report = store.get_report(accepted.report_id)
    assert provider_calls == 3
    assert report is not None and report["analysis_attempts"] == 3
    assert report["analysis_next_retry_at"] is not None
    assert store.list_attention(limit=20) == []


def test_bulk_failure_projects_each_raw_generation_immediately(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "bulk-failure-trigger.db")
    bus = EventBus()
    registry = WSRegistry(send_timeout=0.05)
    bus.subscribe(registry.handle_event)
    attention_service = attention.AttentionService(store=store, event_bus=bus)
    turns = [{"seq": 0, "role": "user", "text": "why", "ts": 1.0}]

    async def failing_llm(config, snapshot, event, latest_prompt):
        raise TimeoutError("private bulk provider details")

    config = {
        "student_id": "server",
        "store": {"db_path": str(store.db_path)},
        "service": {"analysis_max_concurrency": 2},
        "llm": {"enable_llm": True},
    }
    analysis_service = AnalysisService(
        store,
        failing_llm,
        config,
        bus,
        attention_service=attention_service,
    )
    context = AppContext(
        config=config,
        store=store,
        analysis_svc=analysis_service,
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=registry,
    )
    context.attention_svc = attention_service
    store.replace_session_messages(
        "session-a", "student-a", turns, "private transcript", "sha-a",
    )
    store.queue_raw_transcript_analysis(
        student_id="student-a",
        session_id="session-a",
        content_sha256="sha-a",
    )

    first_success, _ = asyncio.run(_analyze_uploaded_session_background(
        context, "student-a", "session-a", turns, "sha-a",
    ))
    raw = store.get_raw_transcript_for_student_session("student-a", "session-a")
    first_rows = store.list_attention(limit=20)
    with store._conn() as conn:
        conn.execute(
            "UPDATE raw_transcripts SET analysis_status = 'pending' WHERE id = ?",
            (raw["id"],),
        )
    second_success, _ = asyncio.run(_analyze_uploaded_session_background(
        context, "student-a", "session-a", turns, "sha-a",
    ))

    rows = store.list_attention(limit=20)
    assert first_success is False
    assert second_success is False
    assert [row["source_id"] for row in first_rows] == [f"bulk:{raw['id']}:1"]
    assert {row["source_id"] for row in rows} == {
        f"bulk:{raw['id']}:1",
        f"bulk:{raw['id']}:2",
    }
    serialized = json.dumps(rows, ensure_ascii=False)
    assert "private bulk provider details" not in serialized
    assert "private transcript" not in serialized


def _commit_normal_bulk_generation(
    store: Store,
    *,
    raw_id: int,
    generation: int,
    session_id: str,
    sha: str,
) -> None:
    committed = store.commit_bulk_analysis_if_current(
        student_id="student-a",
        session_id=session_id,
        content_sha256=sha,
        raw_id=raw_id,
        generation=generation,
        result={
            "understanding": "medium",
            "severity": "info",
            "diagnosis": "bounded diagnosis",
            "suggestion": "bounded action",
        },
        session_title="Session A",
        msg_count=1,
    )
    assert committed is not None


def test_bulk_failure_occurrence_survives_later_success_and_backfills(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "bulk-failure-occurrence.db")
    store.replace_session_messages(
        "session-a",
        "student-a",
        [{"seq": 0, "role": "user", "text": "SECRET_RAW", "ts": 1.0}],
        "SECRET_RAW",
        "sha-a",
    )
    store.queue_raw_transcript_analysis(
        student_id="student-a",
        session_id="session-a",
        content_sha256="sha-a",
    )
    first_claim = store.claim_raw_transcript_analysis(
        student_id="student-a",
        session_id="session-a",
        content_sha256="sha-a",
        prompt_hash="prompt-v1",
    )
    assert first_claim["state"] == "claimed"
    store.fail_raw_transcript_analysis(
        student_id="student-a",
        session_id="session-a",
        content_sha256="sha-a",
        raw_id=int(first_claim["raw_id"]),
        generation=int(first_claim["generation"]),
        error_message="SECRET_PROVIDER_FAILURE",
        analysis_model="model-a",
        prompt_hash="prompt-v1",
        latency_ms=5,
    )

    second_claim = store.claim_raw_transcript_analysis(
        student_id="student-a",
        session_id="session-a",
        content_sha256="sha-a",
        prompt_hash="prompt-v2",
    )
    assert second_claim["state"] == "claimed"
    _commit_normal_bulk_generation(
        store,
        raw_id=int(second_claim["raw_id"]),
        generation=int(second_claim["generation"]),
        session_id="session-a",
        sha="sha-a",
    )

    service = attention.AttentionService(store=store, event_bus=EventBus())
    asyncio.run(service.backfill_missing(publish=False, page_size=1, max_sources=20))

    rows = store.list_attention(limit=20)
    assert [row["source_id"] for row in rows] == [
        f"bulk:{first_claim['raw_id']}:{first_claim['generation']}",
    ]
    assert "SECRET_PROVIDER_FAILURE" not in json.dumps(rows, ensure_ascii=False)


def test_recovered_interrupted_raw_occurrence_survives_retry_success(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "recovered-raw-occurrence.db")
    store.replace_session_messages(
        "session-a",
        "student-a",
        [{"seq": 0, "role": "user", "text": "SECRET_RAW", "ts": 1.0}],
        "SECRET_RAW",
        "sha-a",
    )
    store.queue_raw_transcript_analysis(
        student_id="student-a",
        session_id="session-a",
        content_sha256="sha-a",
    )
    interrupted = store.claim_raw_transcript_analysis(
        student_id="student-a",
        session_id="session-a",
        content_sha256="sha-a",
        prompt_hash="prompt-v1",
    )
    assert interrupted["state"] == "claimed"
    assert store.recover_interrupted_raw_transcript_analyses() == 1

    retry = store.claim_raw_transcript_analysis(
        student_id="student-a",
        session_id="session-a",
        content_sha256="sha-a",
        prompt_hash="prompt-v2",
    )
    assert retry["state"] == "claimed"
    _commit_normal_bulk_generation(
        store,
        raw_id=int(retry["raw_id"]),
        generation=int(retry["generation"]),
        session_id="session-a",
        sha="sha-a",
    )

    service = attention.AttentionService(store=store, event_bus=EventBus())
    asyncio.run(service.backfill_missing(publish=False, page_size=1, max_sources=20))

    assert [row["source_id"] for row in store.list_attention(limit=20)] == [
        f"bulk:{interrupted['raw_id']}:{interrupted['generation']}",
    ]


def test_upload_parent_failure_occurrence_survives_later_done_state(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "upload-parent-occurrence.db")
    uploads = UploadRequestService(store)
    request_id = uploads.create(
        "mentor-a",
        "student-a",
        request_id="parent-only-request",
    )
    uploads.mark_transfer(request_id, "student-a", "running")
    uploads.mark_transfer(request_id, "student-a", "stored")
    uploads.mark_analysis(request_id, "student-a", "pending")
    uploads.mark_analysis(request_id, "student-a", "running")

    recovered = uploads.recover_interrupted_analysis()
    assert recovered[-1]["analysis_status"] == "failed"
    uploads.mark_analysis(request_id, "student-a", "pending")
    uploads.mark_analysis(request_id, "student-a", "running")
    uploads.mark_analysis(request_id, "student-a", "done")

    service = attention.AttentionService(store=store, event_bus=EventBus())
    asyncio.run(service.backfill_missing(publish=False, page_size=1, max_sources=20))

    rows = store.list_attention(limit=20)
    assert [row["source_id"] for row in rows] == [
        "upload-analysis:parent-only-request:1",
    ]
    assert rows[0]["reason_code"] == "system_upload_analysis_failed"


def test_upload_parent_failure_projects_immediately_after_parent_commit(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "upload-parent-realtime.db")
    bus = EventBus()
    attention_service = attention.AttentionService(store=store, event_bus=bus)
    uploads = UploadRequestService(store)
    request_id = uploads.create(
        "mentor-a",
        "student-a",
        request_id="parent-realtime-request",
    )
    uploads.mark_transfer(request_id, "student-a", "running")
    uploads.mark_transfer(request_id, "student-a", "stored")
    uploads.register_session(
        request_id,
        "student-a",
        "session-a",
        "sha-a",
        analysis_status="pending",
    )
    store.compare_and_set_upload_request_session(
        request_id,
        "student-a",
        "session-a",
        expected="pending",
        new_status="failed",
        error="SECRET_CHILD_FAILURE",
        sha="sha-a",
    )
    config = {
        "student_id": "server",
        "store": {"db_path": str(store.db_path)},
        "service": {"analysis_max_concurrency": 2},
    }

    async def unused_llm(config, snapshot, event, latest_prompt):
        raise AssertionError("provider is not used by parent aggregation")

    context = AppContext(
        config=config,
        store=store,
        analysis_svc=AnalysisService(
            store,
            unused_llm,
            config,
            bus,
            attention_service=attention_service,
        ),
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=WSRegistry(send_timeout=0.05),
        attention_svc=attention_service,
        upload_svc=uploads,
    )

    asyncio.run(_refresh_upload_parent_projections(
        context,
        "student-a",
        [request_id],
    ))

    rows = store.list_attention(limit=20)
    assert [row["source_id"] for row in rows] == [
        "upload-analysis:parent-realtime-request:1",
    ]


def test_upload_transfer_failure_occurrence_survives_later_stored_state(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "upload-transfer-occurrence.db")
    uploads = UploadRequestService(store)
    request_id = uploads.create(
        "mentor-a",
        "student-a",
        request_id="transfer-retry-request",
    )
    uploads.mark_transfer(
        request_id,
        "student-a",
        "failed",
        error="SECRET_TRANSFER_FAILURE",
    )
    uploads.mark_transfer(request_id, "student-a", "running")
    uploads.mark_transfer(request_id, "student-a", "stored")

    service = attention.AttentionService(store=store, event_bus=EventBus())
    asyncio.run(service.backfill_missing(publish=False, page_size=1, max_sources=20))

    rows = store.list_attention(limit=20)
    assert [row["source_id"] for row in rows] == [
        "upload-transfer:transfer-retry-request:1",
    ]
    assert "SECRET_TRANSFER_FAILURE" not in json.dumps(rows, ensure_ascii=False)


def test_stop_terminal_failure_occurrence_survives_later_done_state(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "stop-failure-occurrence.db")
    report_id = store.add_report(
        "student-a", "session-a", "Stop", "prompt", "", 1, 0,
    )
    with store._conn() as conn:
        conn.execute(
            """UPDATE reports
               SET analysis_input = 'bounded input', analysis_status = 'pending'
               WHERE id = ?""",
            (report_id,),
        )
    claimed = store.claim_report_analysis(report_id, max_attempts=1)
    assert claimed is not None and claimed["analysis_attempts"] == 1
    store.mark_report_analysis_failed(
        report_id,
        attempt=1,
        error_code="SECRET_STOP_FAILURE",
        next_retry_at=None,
    )
    store.mark_report_analysis_done(report_id)

    service = attention.AttentionService(store=store, event_bus=EventBus())
    asyncio.run(service.backfill_missing(publish=False, page_size=1, max_sources=20))

    rows = store.list_attention(limit=20)
    assert [row["source_id"] for row in rows] == [f"stop:{report_id}:1"]
    assert "SECRET_STOP_FAILURE" not in json.dumps(rows, ensure_ascii=False)


def test_repeated_store_initialization_seeds_each_legacy_failure_once(tmp_path):
    attention = _attention_module()
    db_path = tmp_path / "legacy-failure-seed.db"
    store = Store(db_path)
    report_id = store.add_report(
        "student-a", "session-a", "Stop", "prompt", "", 1, 0,
    )
    store.replace_session_messages(
        "session-b",
        "student-a",
        [{"seq": 0, "role": "user", "text": "SECRET_RAW", "ts": 1.0}],
        "SECRET_RAW",
        "legacy-sha",
    )
    request_id = store.add_upload_request(
        "mentor-a",
        "student-a",
        request_id="legacy-upload",
    )
    with store._conn() as conn:
        raw_id = int(conn.execute(
            "SELECT id FROM raw_transcripts WHERE session_id = 'session-b'",
        ).fetchone()["id"])
        conn.execute(
            """UPDATE reports
               SET analysis_status = 'failed', analysis_attempts = 0,
                   analysis_error = 'analysis_input_unavailable',
                   analysis_next_retry_at = NULL
               WHERE id = ?""",
            (report_id,),
        )
        conn.execute(
            """UPDATE raw_transcripts
               SET analysis_status = 'failed', analysis_generation = 0,
                   analysis_error = 'SECRET_LEGACY_RAW_ERROR'
               WHERE id = ?""",
            (raw_id,),
        )
        conn.execute(
            """UPDATE upload_requests
               SET transfer_status = 'failed', analysis_status = 'failed',
                   transfer_error = 'SECRET_LEGACY_TRANSFER_ERROR',
                   analysis_error = 'SECRET_LEGACY_ANALYSIS_ERROR'
               WHERE request_id = ?""",
            (request_id,),
        )

    Store(db_path)
    restarted = Store(db_path)
    service = attention.AttentionService(store=restarted, event_bus=EventBus())
    asyncio.run(service.backfill_missing(publish=False, page_size=1, max_sources=20))

    expected = {
        f"stop:{report_id}:1",
        f"bulk:{raw_id}:1",
        "upload-transfer:legacy-upload:1",
        "upload-analysis:legacy-upload:1",
    }
    assert {row["source_id"] for row in restarted.list_attention(limit=20)} == expected
    with restarted._conn() as conn:
        table_exists = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type = 'table' AND name = 'system_failure_occurrences'""",
        ).fetchone()
        assert table_exists is not None
        occurrence_count = conn.execute(
            "SELECT COUNT(*) FROM system_failure_occurrences",
        ).fetchone()[0]
    assert occurrence_count == 4


def test_legacy_seed_runs_after_reports_with_analyses_are_canonicalized(tmp_path):
    attention = _attention_module()
    db_path = tmp_path / "legacy-analysis-canonicalization.db"
    store = Store(db_path)
    analysis_id = _add_analysis(store, severity="info")
    report_id = int(store.get_analysis(analysis_id)["report_id"])
    with store._conn() as conn:
        conn.execute(
            """UPDATE reports
               SET analysis_status = 'failed', analysis_attempts = 1,
                   analysis_error = 'analysis_timeout_error',
                   analysis_next_retry_at = NULL
               WHERE id = ?""",
            (report_id,),
        )
        conn.execute("DELETE FROM system_failure_occurrences")

    reopened = Store(db_path)
    service = attention.AttentionService(store=reopened, event_bus=EventBus())
    asyncio.run(service.backfill_missing(publish=False, page_size=1, max_sources=20))

    assert reopened.get_report(report_id)["analysis_status"] == "done"
    assert reopened.list_system_failure_occurrences(kind="stop") == []
    assert reopened.list_attention(limit=20) == []


def test_occurrence_identity_is_exact_and_delete_cascades_long_student_id(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "long-occurrence-identity.db")
    student_id = "student-" + ("x" * 241)
    uploads = UploadRequestService(store)
    request_id = uploads.create(
        "mentor-a",
        student_id,
        request_id="long-student-upload",
    )
    uploads.mark_transfer(
        request_id,
        student_id,
        "failed",
        error="SECRET_TRANSFER_ERROR",
    )
    service = attention.AttentionService(store=store, event_bus=EventBus())
    asyncio.run(service.backfill_missing(publish=False, page_size=1, max_sources=20))

    rows = store.list_attention(limit=20)
    assert [row["student_id"] for row in rows] == [student_id]

    store.delete_student(student_id)

    assert store.list_attention(limit=20) == []
    assert store.list_system_failure_occurrences(kind="upload_transfer") == []


def test_occurrence_identity_preserves_whitespace_and_delete_cascades(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "whitespace-occurrence-identity.db")
    student_id = " student-a "
    uploads = UploadRequestService(store)
    request_id = uploads.create(
        "mentor-a",
        student_id,
        request_id="whitespace-student-upload",
    )
    uploads.mark_transfer(
        request_id,
        student_id,
        "failed",
        error="SECRET_TRANSFER_ERROR",
    )
    service = attention.AttentionService(store=store, event_bus=EventBus())
    asyncio.run(service.backfill_missing(publish=False, page_size=1, max_sources=20))

    assert [row["student_id"] for row in store.list_attention(limit=20)] == [
        student_id
    ]

    store.delete_student(student_id)

    assert store.list_attention(limit=20) == []
    assert store.list_system_failure_occurrences(kind="upload_transfer") == []


def test_blank_occurrence_identity_rolls_back_failure_transition(tmp_path):
    store = Store(tmp_path / "blank-occurrence-identity.db")
    uploads = UploadRequestService(store)
    request_id = uploads.create(
        "mentor-a",
        "",
        request_id="blank-student-upload",
    )

    with pytest.raises(ValueError, match="failure occurrence student id"):
        uploads.mark_transfer(
            request_id,
            "",
            "failed",
            error="SECRET_TRANSFER_ERROR",
        )

    row = store.get_upload_request(request_id)
    assert row["transfer_status"] == "pending"
    assert row["transfer_failure_generation"] == 0


def test_occurrence_unique_key_rejects_conflicting_student_and_rolls_back(tmp_path):
    store = Store(tmp_path / "occurrence-identity-conflict.db")
    uploads = UploadRequestService(store)
    request_id = uploads.create(
        "mentor-a",
        "student-a",
        request_id="conflicting-occurrence-upload",
    )
    store.upsert_student("student-b")
    with store._conn() as conn:
        conn.execute(
            """INSERT INTO system_failure_occurrences
               (kind, logical_key, generation, student_id, session_id,
                reason_code, created_at)
               VALUES ('upload_transfer', ?, 1, 'student-b', '',
                       'system_upload_transfer_failed', 1.0)""",
            (request_id,),
        )

    with pytest.raises(ValueError, match="failure occurrence identity conflict"):
        uploads.mark_transfer(
            request_id,
            "student-a",
            "failed",
            error="SECRET_TRANSFER_ERROR",
        )

    row = store.get_upload_request(request_id)
    assert row["transfer_status"] == "pending"
    assert row["transfer_failure_generation"] == 0


def test_backfill_cursor_compare_and_set_rejects_stale_writer(tmp_path):
    store_a = Store(tmp_path / "attention-cursor-cas.db")
    store_b = Store(store_a.db_path)
    cursor, version = store_a.get_attention_backfill_cursor_state("analysis")
    assert (cursor, version) == (0, 0)

    advanced = store_a.compare_and_set_attention_backfill_cursor(
        "analysis",
        expected_version=version,
        last_id=200,
    )
    stale = store_b.compare_and_set_attention_backfill_cursor(
        "analysis",
        expected_version=version,
        last_id=101,
    )

    assert advanced == 1
    assert stale is None
    assert store_a.get_attention_backfill_cursor_state("analysis") == (200, 1)


def test_recovery_input_unavailable_commits_failure_then_projects_system_item(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "input-unavailable.db")
    bus = EventBus()
    registry = WSRegistry(send_timeout=0.05)
    attention_service = attention.AttentionService(store=store, event_bus=bus)

    async def unused_llm(config, snapshot, event, latest_prompt):
        raise AssertionError("provider must not run without durable input")

    config = {
        "student_id": "server",
        "store": {"db_path": str(store.db_path)},
        "service": {"analysis_max_concurrency": 2},
    }
    analysis_service = AnalysisService(
        store,
        unused_llm,
        config,
        bus,
        attention_service=attention_service,
    )
    context = AppContext(
        config=config,
        store=store,
        analysis_svc=analysis_service,
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=registry,
    )
    context.attention_svc = attention_service
    report_id = store.add_report(
        "student-a", "session-a", "Stop", "prompt", "legacy-path", 1, 0,
    )
    with store._conn() as conn:
        conn.execute(
            """UPDATE reports
               SET analysis_status = 'pending', analysis_pending = 1,
                   analysis_input = NULL
               WHERE id = ?""",
            (report_id,),
        )

    asyncio.run(_recover_pending_reports(context, report_ids=(report_id,)))

    report = store.get_report(report_id)
    rows = store.list_attention(limit=20)
    assert report["analysis_status"] == "failed"
    assert report["analysis_error"] == "analysis_input_unavailable"
    assert [row["source_id"] for row in rows] == [f"stop:{report_id}:3"]


def test_delete_student_cascades_attention_and_legacy_missing_student_backfills(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "delete-and-legacy.db")
    store.upsert_student("delete-me")
    created = store.insert_attention_decisions([
        _attention_record(student_id="delete-me"),
    ])

    deleted = store.delete_student("delete-me")

    assert len(created) == 1
    assert deleted["attention_items"] == 1
    assert store.list_attention(student_id="delete-me", limit=20) == []

    analysis_id = _add_analysis(
        store,
        severity="error",
        student_id="legacy-student",
        session_id="legacy-session",
    )
    with store._conn() as conn:
        conn.execute(
            "DELETE FROM students WHERE student_id = ?",
            ("legacy-student",),
        )
    service = attention.AttentionService(store=store, event_bus=EventBus())

    asyncio.run(service.backfill_missing(publish=False, page_size=1, max_sources=10))

    legacy_rows = store.list_attention(student_id="legacy-student", limit=20)
    assert [row["source_id"] for row in legacy_rows] == [str(analysis_id)]
