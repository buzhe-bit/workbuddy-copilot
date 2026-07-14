from __future__ import annotations

import asyncio
import copy
import importlib
import json
import threading

import pytest
from fastapi.testclient import TestClient

from copilot import service as service_module
from copilot.connections import WSRegistry
from copilot.eventbus import EventBus
from copilot.models import QuestionAnswerOutcome
from copilot.service import app as global_app
from copilot.service import create_app
from copilot.services import AnalysisService, MessageService, SessionQueryService
from copilot.store import Store
from copilot.upload_service import UploadRequestService


def _attention_module():
    try:
        return importlib.import_module("copilot.attention")
    except ModuleNotFoundError as exc:
        if exc.name != "copilot.attention":
            raise
        pytest.fail("Task 5 RED: copilot.attention does not exist yet", pytrace=False)


class _FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


@pytest.fixture
def attention_api(tmp_path):
    """Real Store -> AttentionService -> controller composition, isolated per test."""
    attention = _attention_module()
    store = Store(tmp_path / "attention-api.db")
    bus = EventBus()
    attention_service = attention.AttentionService(store=store, event_bus=bus)
    context = _isolated_context(store, attention_service, bus=bus)
    isolated_app = create_app(context)
    return TestClient(isolated_app), store


def _isolated_context(store: Store, attention_service, *, bus: EventBus | None = None):
    bus = bus or EventBus()
    registry = WSRegistry(send_timeout=0.05)
    bus.subscribe(registry.handle_event)
    context = copy.copy(global_app.state.context)
    context.config = copy.deepcopy(context.config)
    context.config.setdefault("store", {})["db_path"] = str(store.db_path)
    context.store = store
    context.session_svc = SessionQueryService(store, context.config)
    context.message_svc = MessageService(store, bus)
    context.bus = bus
    context.ws_registry = registry
    context.attention_svc = attention_service
    context.upload_svc = None
    context.worker_lock_file = None
    context.report_recovery_prepared = False
    context.report_recovery_task = None
    return context


def _seed_attention(
    store: Store,
    *,
    item_id: int,
    priority: str = "high",
    status: str = "open",
    category: str = "learning",
    student_id: str = "student-a",
    created_at: float = 10.0,
    source_type: str = "analysis",
    source_id: str | None = None,
    reason_code: str | None = None,
) -> None:
    store.upsert_student(student_id)
    source_id = source_id or str(item_id)
    reason_code = reason_code or f"reason_{item_id}"
    with store._conn() as conn:
        conn.execute(
            """INSERT INTO attention_items
               (id, source_type, source_id, category, student_id, session_id,
                priority, reason_code, reason, evidence_json, suggested_action,
                confidence, status, handled_by, resolution_note, handled_at,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item_id,
                source_type,
                source_id,
                category,
                student_id,
                f"session-{student_id}",
                priority,
                reason_code,
                "Bounded reason",
                json.dumps(["bounded evidence"]),
                "Take one concrete next step",
                0.9,
                status,
                "",
                "",
                None,
                created_at,
                created_at,
            ),
        )


def test_create_app_wires_one_canonical_attention_service_when_context_is_missing(
    tmp_path,
):
    store = Store(tmp_path / "missing-attention-wiring.db")
    bus = EventBus()

    async def unused_llm(config, snapshot, event, latest_prompt):
        raise AssertionError("provider is not used by this composition test")

    context = _isolated_context(store, None, bus=bus)
    context.analysis_svc = AnalysisService(
        store,
        unused_llm,
        context.config,
        bus,
        attention_service=None,
    )

    create_app(context)

    assert context.attention_svc is not None
    assert context.analysis_svc.attention_service is context.attention_svc


def test_create_app_repairs_split_attention_service_wiring(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "split-attention-wiring.db")
    bus = EventBus()
    canonical = attention.AttentionService(store=store, event_bus=bus)
    stale = attention.AttentionService(store=store, event_bus=bus)

    async def unused_llm(config, snapshot, event, latest_prompt):
        raise AssertionError("provider is not used by this composition test")

    context = _isolated_context(store, canonical, bus=bus)
    context.analysis_svc = AnalysisService(
        store,
        unused_llm,
        context.config,
        bus,
        attention_service=stale,
    )

    create_app(context)

    assert context.attention_svc is canonical
    assert context.analysis_svc.attention_service is canonical


def test_create_app_replaces_attention_service_bound_to_another_store_or_bus(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "canonical-attention-wiring.db")
    stale_store = Store(tmp_path / "stale-attention-wiring.db")
    bus = EventBus()
    stale = attention.AttentionService(store=stale_store, event_bus=EventBus())

    async def unused_llm(config, snapshot, event, latest_prompt):
        raise AssertionError("provider is not used by this composition test")

    context = _isolated_context(store, stale, bus=bus)
    context.analysis_svc = AnalysisService(
        store,
        unused_llm,
        context.config,
        bus,
        attention_service=stale,
    )

    create_app(context)

    assert context.attention_svc is not stale
    assert context.attention_svc.store is store
    assert context.attention_svc.event_bus is bus
    assert context.analysis_svc.attention_service is context.attention_svc


def test_attention_get_route_exists_before_detailed_contract():
    response = TestClient(global_app).get("/api/mentor/attention")

    assert response.status_code == 200
    assert "items" in response.json()


def test_get_attention_validates_filters_and_sorts_priority_then_age(attention_api):
    client, store = attention_api
    _seed_attention(store, item_id=1, priority="medium", created_at=1.0)
    _seed_attention(store, item_id=2, priority="high", created_at=20.0)
    _seed_attention(store, item_id=3, priority="high", created_at=10.0)
    _seed_attention(store, item_id=4, priority="high", created_at=10.0)
    _seed_attention(
        store,
        item_id=5,
        priority="high",
        status="in_progress",
        category="system",
        student_id="student-b",
        created_at=5.0,
        source_type="system",
    )
    _seed_attention(store, item_id=6, status="resolved", created_at=0.5)

    queue = client.get(
        "/api/mentor/attention",
        params={"status": "open", "category": "learning", "limit": 10},
    )
    filtered = client.get(
        "/api/mentor/attention",
        params={
            "status": "in_progress",
            "priority": "high",
            "category": "system",
            "student_id": "student-b",
            "limit": 1,
        },
    )

    assert queue.status_code == 200
    assert [item["id"] for item in queue.json()["items"]] == [3, 4, 2, 1]
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.json()["items"]] == [5]


@pytest.mark.parametrize(
    "params",
    [
        {"status": "closed"},
        {"priority": "low"},
        {"category": "operations"},
        {"limit": 0},
        {"limit": 201},
    ],
    ids=["status", "priority", "category", "limit-low", "limit-high"],
)
def test_get_attention_rejects_invalid_filters_and_unbounded_limits(attention_api, params):
    client, _ = attention_api

    response = client.get("/api/mentor/attention", params=params)

    assert response.status_code == 422


def test_patch_attention_state_machine_exact_replay_and_cas(attention_api):
    client, store = attention_api
    _seed_attention(store, item_id=10)
    claim = {
        "status": "in_progress",
        "mentor_id": "mentor-a",
        "note": "triaging",
    }

    first = client.patch("/api/mentor/attention/10", json=claim)
    exact_replay = client.patch("/api/mentor/attention/10", json=claim)
    conflicting_claim = client.patch(
        "/api/mentor/attention/10",
        json={**claim, "mentor_id": "mentor-b"},
    )
    resolve = client.patch(
        "/api/mentor/attention/10",
        json={"status": "resolved", "mentor_id": "mentor-a", "note": "handled"},
    )
    terminal_replay = client.patch(
        "/api/mentor/attention/10",
        json={"status": "resolved", "mentor_id": "mentor-a", "note": "handled"},
    )
    reopen = client.patch("/api/mentor/attention/10", json=claim)

    assert first.status_code == 200
    assert first.json()["status"] == "in_progress"
    assert first.json()["handled_at"] is None
    assert exact_replay.status_code == 200
    assert exact_replay.json() == first.json()
    assert conflicting_claim.status_code == 409
    assert resolve.status_code == 200
    assert resolve.json()["status"] == "resolved"
    assert isinstance(resolve.json()["handled_at"], (int, float))
    assert terminal_replay.status_code == 200
    assert terminal_replay.json() == resolve.json()
    assert reopen.status_code == 409


def test_patch_attention_returns_404_and_validates_body(attention_api):
    client, store = attention_api
    _seed_attention(store, item_id=11)

    missing = client.patch(
        "/api/mentor/attention/999",
        json={"status": "resolved", "mentor_id": "mentor-a", "note": "handled"},
    )
    invalid_status = client.patch(
        "/api/mentor/attention/11",
        json={"status": "closed", "mentor_id": "mentor-a", "note": "handled"},
    )
    missing_mentor = client.patch(
        "/api/mentor/attention/11",
        json={"status": "resolved", "mentor_id": "", "note": "handled"},
    )
    oversized_note = client.patch(
        "/api/mentor/attention/11",
        json={"status": "resolved", "mentor_id": "mentor-a", "note": "x" * 501},
    )

    assert missing.status_code == 404
    assert invalid_status.status_code == 422
    assert missing_mentor.status_code == 422
    assert oversized_note.status_code == 422


@pytest.mark.parametrize(
    ("answer_status", "error_code", "expected_reason"),
    [
        ("degraded", "llm_disabled", "student_ask_degraded"),
        ("failed", "llm_timeout", "student_ask_failed"),
    ],
)
def test_student_ask_route_projects_degraded_and_failed_after_commit(
    attention_api,
    monkeypatch,
    answer_status,
    error_code,
    expected_reason,
):
    client, store = attention_api

    async def fake_answer(config, question, context_messages):
        return QuestionAnswerOutcome(
            status=answer_status,
            answer="private full answer",
            error_code=error_code,
        )

    monkeypatch.setattr(service_module, "llm_answer_question", fake_answer)

    response = client.post(
        "/api/student/ask",
        json={"student_id": "student-a", "question": "private full question"},
    )
    queue = client.get(
        "/api/mentor/attention",
        params={"status": "open", "student_id": "student-a"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == answer_status
    assert len(store.list_student_asks("student-a")) == 1
    assert queue.status_code == 200
    assert [item["reason_code"] for item in queue.json()["items"]] == [expected_reason]
    serialized = json.dumps(queue.json(), ensure_ascii=False)
    assert "private full question" not in serialized
    assert "private full answer" not in serialized


def test_student_ask_durable_success_survives_transient_event_fanout_failure(
    tmp_path,
    monkeypatch,
):
    attention = _attention_module()
    store = Store(tmp_path / "ask-fanout-failure.db")

    class ExplodingBus(EventBus):
        async def publish(self, payload: dict) -> None:
            raise RuntimeError("transient ask fanout failed")

    bus = ExplodingBus()
    attention_service = attention.AttentionService(store=store, event_bus=bus)
    context = _isolated_context(store, attention_service, bus=bus)

    async def degraded(config, question, context_messages):
        return QuestionAnswerOutcome(
            status="degraded",
            answer="bounded fallback",
            error_code="llm_disabled",
        )

    monkeypatch.setattr(service_module, "llm_answer_question", degraded)
    client = TestClient(create_app(context))

    response = client.post(
        "/api/student/ask",
        json={"student_id": "student-a", "question": "private question"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert len(store.list_student_asks("student-a")) == 1
    assert [row["reason_code"] for row in store.list_attention(limit=20)] == [
        "student_ask_degraded"
    ]


def test_upload_parent_failure_projects_in_status_route_despite_fanout_failure(
    tmp_path,
):
    attention = _attention_module()
    store = Store(tmp_path / "upload-status-fanout-failure.db")

    class ExplodingBus(EventBus):
        async def publish(self, payload: dict) -> None:
            raise RuntimeError("transient upload fanout failed")

    bus = ExplodingBus()
    attention_service = attention.AttentionService(store=store, event_bus=bus)
    context = _isolated_context(store, attention_service, bus=bus)
    uploads = UploadRequestService(store)
    context.upload_svc = uploads
    request_id = uploads.create(
        "mentor-a",
        "student-a",
        request_id="route-parent-failure",
    )
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
        error="PRIVATE_CHILD_FAILURE",
        sha="sha-a",
    )
    client = TestClient(create_app(context))

    running = client.post(
        f"/api/student/upload-requests/{request_id}/status",
        json={"student_id": "student-a", "status": "running"},
    )
    completed = client.post(
        f"/api/student/upload-requests/{request_id}/status",
        json={"student_id": "student-a", "status": "done"},
    )

    assert running.status_code == 200
    assert completed.status_code == 200
    assert completed.json()["analysis_status"] == "failed"
    rows = store.list_attention(limit=20)
    assert [row["source_id"] for row in rows] == [
        "upload-analysis:route-parent-failure:1"
    ]


def test_unresolved_feedback_route_projects_a_second_reason_after_commit(
    attention_api,
    monkeypatch,
):
    client, store = attention_api

    async def answered(config, question, context_messages):
        return QuestionAnswerOutcome(status="answered", answer="private answer")

    monkeypatch.setattr(service_module, "llm_answer_question", answered)
    ask = client.post(
        "/api/student/ask",
        json={"student_id": "student-a", "question": "private question"},
    )
    ask_id = ask.json()["ask_id"]

    feedback = client.post(
        f"/api/student/asks/{ask_id}/feedback",
        json={
            "student_id": "student-a",
            "feedback": "unresolved",
            "note": "private full feedback note",
        },
    )
    queue = client.get(
        "/api/mentor/attention",
        params={"status": "open", "student_id": "student-a"},
    )

    assert feedback.status_code == 200
    assert store.list_student_asks("student-a")[0]["feedback"] == "unresolved"
    assert [item["reason_code"] for item in queue.json()["items"]] == [
        "student_ask_unresolved",
    ]
    serialized = json.dumps(queue.json(), ensure_ascii=False)
    assert "private question" not in serialized
    assert "private answer" not in serialized
    assert "private full feedback note" not in serialized


def test_startup_backfill_repairs_all_durable_source_projection_gaps(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "startup-backfill.db")
    report_id = store.add_report(
        "student-a", "session-a", "Stop", "prompt", "", 1, 0,
    )
    analysis_id = store.add_analysis(
        report_id,
        "student-a",
        {
            "severity": "error",
            "understanding": "medium",
            "diagnosis": "bounded diagnosis",
            "suggestion": "bounded action",
        },
        "session-a",
        "Session A",
    )
    ask_id = store.add_student_ask(
        "student-a",
        "session-a",
        "private question",
        "private degraded answer",
        answer_status="degraded",
        error_code="llm_disabled",
    )
    store.record_student_ask_feedback(
        ask_id,
        "student-a",
        "unresolved",
        "private feedback note",
    )
    terminal_report_id = store.add_report(
        "student-a", "session-a", "Stop", "prompt", "", 1, 0,
    )
    with store._conn() as conn:
        conn.execute(
            """UPDATE reports
               SET analysis_status = 'failed', analysis_attempts = 3,
                   analysis_error = 'private provider error'
               WHERE id = ?""",
            (terminal_report_id,),
        )
    upload_id = store.add_upload_request(
        "mentor-a",
        "student-a",
        "session-a",
        request_id="startup-upload",
    )
    store.update_upload_request_status(
        upload_id,
        student_id="student-a",
        status="failed",
        error_message="private transfer error",
    )
    store.replace_session_messages(
        "session-b",
        "student-a",
        [{"seq": 0, "role": "user", "text": "private transcript", "ts": 1.0}],
        "private transcript",
        "sha-startup",
    )
    with store._conn() as conn:
        raw = conn.execute(
            "SELECT id FROM raw_transcripts WHERE session_id = 'session-b'",
        ).fetchone()
        raw_id = int(raw["id"])
        conn.execute(
            """UPDATE raw_transcripts
               SET analysis_status = 'failed', analysis_generation = 4,
                   analysis_error = 'private bulk error'
               WHERE id = ?""",
            (raw_id,),
        )
    bus = EventBus()
    attention_service = attention.AttentionService(store=store, event_bus=bus)
    context = _isolated_context(store, attention_service, bus=bus)
    isolated_app = create_app(context)

    assert store.list_attention(limit=20) == []
    with TestClient(isolated_app) as client:
        response = client.get(
            "/api/mentor/attention",
            params={"status": "open", "student_id": "student-a"},
        )

    assert response.status_code == 200
    actual = {
        (item["source_type"], item["source_id"], item["reason_code"])
        for item in response.json()["items"]
    }
    assert actual == {
        ("analysis", str(analysis_id), "analysis_error"),
        ("student_ask", str(ask_id), "student_ask_degraded"),
        ("student_ask", str(ask_id), "student_ask_unresolved"),
        (
            "system",
            f"stop:{terminal_report_id}:3",
            "system_stop_retries_exhausted",
        ),
        (
            "system",
            "upload-transfer:startup-upload:1",
            "system_upload_transfer_failed",
        ),
        ("system", f"bulk:{raw_id}:4", "system_bulk_analysis_failed"),
    }
    serialized = json.dumps(response.json(), ensure_ascii=False)
    for private_text in [
        "private question",
        "private degraded answer",
        "private feedback note",
        "private provider error",
        "private transfer error",
        "private transcript",
        "private bulk error",
    ]:
        assert private_text not in serialized


def test_publish_failure_still_catches_up_through_get(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "publish-catch-up.db")
    report_id = store.add_report(
        "student-a", "session-a", "Stop", "prompt", "", 1, 0,
    )
    analysis_id = store.add_analysis(
        report_id,
        "student-a",
        {
            "severity": "error",
            "understanding": "medium",
            "diagnosis": "bounded diagnosis",
            "suggestion": "bounded action",
        },
        "session-a",
        "Session A",
    )

    class ExplodingBus:
        async def publish(self, payload: dict) -> None:
            raise RuntimeError("fanout failed")

    exploding_service = attention.AttentionService(
        store=store,
        event_bus=ExplodingBus(),
    )

    async def project() -> None:
        try:
            await exploding_service.project_analysis(analysis_id)
        except RuntimeError:
            pass

    asyncio.run(project())
    context = _isolated_context(store, exploding_service)
    client = TestClient(create_app(context))

    response = client.get(
        "/api/mentor/attention",
        params={"status": "open", "student_id": "student-a"},
    )

    assert response.status_code == 200
    assert [item["source_id"] for item in response.json()["items"]] == [
        str(analysis_id),
    ]


def test_patch_returns_durable_transition_when_event_fanout_fails(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "patch-fanout-failure.db")
    _seed_attention(store, item_id=11)

    class ExplodingBus(EventBus):
        async def publish(self, payload: dict) -> None:
            raise RuntimeError("transient patch fanout failed")

    bus = ExplodingBus()
    attention_service = attention.AttentionService(store=store, event_bus=bus)
    context = _isolated_context(store, attention_service, bus=bus)
    client = TestClient(create_app(context))

    response = client.patch(
        "/api/mentor/attention/11",
        json={
            "status": "resolved",
            "mentor_id": "mentor-a",
            "note": "handled",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    assert store.get_attention(11)["status"] == "resolved"


def test_upload_transfer_failed_route_projects_request_axis_immediately(attention_api):
    client, _ = attention_api
    requested = client.post(
        "/api/mentor/students/student-a/request-upload",
        json={"mentor_id": "mentor-a", "session_id": "session-a"},
    )
    request_id = requested.json()["request_id"]

    failed = client.post(
        f"/api/student/upload-requests/{request_id}/status",
        json={
            "student_id": "student-a",
            "status": "failed",
            "error_message": "private transfer stack and path",
        },
    )
    queue = client.get(
        "/api/mentor/attention",
        params={"status": "open", "category": "system", "student_id": "student-a"},
    )

    assert requested.status_code == 200
    assert failed.status_code == 200
    assert failed.json()["transfer_status"] == "failed"
    assert [item["source_id"] for item in queue.json()["items"]] == [
        f"upload-transfer:{request_id}:1",
    ]
    assert "private transfer stack" not in json.dumps(queue.json(), ensure_ascii=False)


def test_patch_publishes_once_to_mentors_and_never_student_floats(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "patch-event.db")
    bus = EventBus()
    attention_service = attention.AttentionService(store=store, event_bus=bus)
    context = _isolated_context(store, attention_service, bus=bus)
    mentor = _FakeWebSocket()
    target_float = _FakeWebSocket()
    other_float = _FakeWebSocket()
    context.ws_registry.register_mentor(mentor)
    context.ws_registry.register_float("student-a", target_float)
    context.ws_registry.register_float("student-b", other_float)
    _seed_attention(store, item_id=41, student_id="student-a")
    client = TestClient(create_app(context))
    payload = {
        "status": "resolved",
        "mentor_id": "mentor-a",
        "note": "private mentor resolution note",
    }

    updated = client.patch("/api/mentor/attention/41", json=payload)
    replay = client.patch("/api/mentor/attention/41", json=payload)

    assert updated.status_code == 200
    assert replay.status_code == 200
    assert len(mentor.sent) == 1
    assert mentor.sent[0]["type"] == "attention_updated"
    assert target_float.sent == []
    assert other_float.sent == []
    assert "private mentor resolution note" not in json.dumps(mentor.sent, ensure_ascii=False)


def test_two_mentors_racing_patch_have_one_winner_and_one_stable_conflict(tmp_path):
    _attention_module()
    db_path = tmp_path / "patch-cas.db"
    store_a = Store(db_path)
    store_a.upsert_student("student-a")
    created = store_a.insert_attention_decisions([{
        "source_type": "analysis",
        "source_id": "1",
        "category": "learning",
        "student_id": "student-a",
        "session_id": "session-a",
        "priority": "high",
        "reason_code": "analysis_error",
        "reason": "Bounded reason",
        "evidence_json": "[]",
        "suggested_action": "Bounded action",
        "confidence": 0.9,
        "created_at": 1.0,
    }])
    item_id = created[0]["id"]
    store_b = Store(db_path)
    barrier = threading.Barrier(2)
    winners: list[dict] = []
    conflicts: list[str] = []

    def patch(store: Store, mentor_id: str) -> None:
        barrier.wait(timeout=2)
        try:
            row, changed = store.update_attention_status(
                item_id,
                status="resolved",
                mentor_id=mentor_id,
                note=f"handled by {mentor_id}",
            )
            if changed:
                winners.append(row)
        except ValueError as exc:
            conflicts.append(str(exc))

    threads = [
        threading.Thread(target=patch, args=(store_a, "mentor-a")),
        threading.Thread(target=patch, args=(store_b, "mentor-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert not any(thread.is_alive() for thread in threads)
    assert len(winners) == 1
    assert len(conflicts) == 1
    assert conflicts[0] == "attention status conflict"
    persisted = store_a.list_attention(status="resolved", limit=20)[0]
    assert persisted["handled_by"] == winners[0]["handled_by"]


def test_mentor_students_http_includes_independent_attention_summary_fields(tmp_path):
    attention = _attention_module()
    store = Store(tmp_path / "mentor-students.db")
    bus = EventBus()
    attention_service = attention.AttentionService(store=store, event_bus=bus)
    context = _isolated_context(store, attention_service, bus=bus)
    store.upsert_student("student-a", "Student A")
    created = store.insert_attention_decisions([
        {
            "source_type": "analysis",
            "source_id": "1",
            "category": "learning",
            "student_id": "student-a",
            "session_id": "session-a",
            "priority": "high",
            "reason_code": "analysis_error",
            "reason": "Bounded reason",
            "evidence_json": "[]",
            "suggested_action": "Bounded action",
            "confidence": 0.9,
            "created_at": 10.0,
        },
        {
            "source_type": "analysis",
            "source_id": "2",
            "category": "learning",
            "student_id": "student-a",
            "session_id": "session-a",
            "priority": "medium",
            "reason_code": "analysis_low",
            "reason": "Bounded reason",
            "evidence_json": "[]",
            "suggested_action": "Bounded action",
            "confidence": 0.5,
            "created_at": 20.0,
        },
    ])
    store.update_attention_status(
        created[1]["id"],
        status="in_progress",
        mentor_id="mentor-a",
        note="triaging",
    )

    response = TestClient(create_app(context)).get("/api/mentor/students")

    assert response.status_code == 200
    student = response.json()["items"][0]
    assert student["open_attention_count"] == 2
    assert student["highest_attention_priority"] == "high"
    assert student["last_attention_at"] == 20.0
