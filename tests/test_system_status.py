from __future__ import annotations

import json

from fastapi.testclient import TestClient

from copilot.app_context import AppContext
from copilot.connections import WSRegistry
from copilot.eventbus import EventBus
from copilot.mentor import routes as mentor_routes
from copilot.service import create_app
from copilot.services import AnalysisService, MessageService, SessionQueryService
from copilot.store import Store
from copilot.student_platform.windows_evidence import WindowsEvidenceResult


MENTOR_TOKEN = "mentor-secret"


async def _fake_llm(config, snap, event, latest_prompt):
    return {
        "topic": "status",
        "understanding": "medium",
        "severity": "info",
        "diagnosis": "ok",
        "suggestion": "ok",
        "is_technical": False,
        "ai_reply_summary": "",
    }


def _build_app(tmp_path, *, windows_rollout=None):
    store = Store(tmp_path / "copilot.db")
    bus = EventBus()
    registry = WSRegistry(send_timeout=0.05)
    bus.subscribe(registry.handle_event)
    config = {
        "student_id": "local-student",
        "service": {"host": "127.0.0.1", "port": 8765},
        "store": {"db_path": str(tmp_path / "copilot.db")},
        "auth": {"mode": "local", "mentor_token": MENTOR_TOKEN},
        "llm": {"enable_llm": False},
    }
    if windows_rollout is not None:
        config["windows_rollout"] = windows_rollout
    context = AppContext(
        config=config,
        store=store,
        analysis_svc=AnalysisService(store, _fake_llm, config, bus),
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=registry,
    )
    return create_app(context), store, registry


def _add_stop_report(store: Store, *, event_id: str) -> int:
    report, duplicate = store.accept_report(
        student_id="student-a",
        session_id=f"session-{event_id}",
        event="Stop",
        event_id=event_id,
        prompt="bounded prompt",
        transcript_path="",
        msg_count=1,
        tool_calls=0,
        analysis_input="bounded analysis input",
    )
    assert duplicate is False
    return int(report["id"])


def _add_attention(store: Store, *, source_id: str) -> dict:
    return store.insert_attention_decisions(
        [
            {
                "source_type": "analysis",
                "source_id": source_id,
                "category": "learning",
                "student_id": "student-a",
                "session_id": "session-attention",
                "priority": "high",
                "reason_code": "analysis_error",
                "reason": "Learner needs bounded mentor attention.",
                "evidence_json": json.dumps(["bounded evidence"]),
                "suggested_action": "Review the next concrete step.",
                "confidence": 0.9,
            }
        ]
    )[0]


def test_system_status_requires_mentor_auth_and_returns_only_safe_counts(tmp_path):
    app, store, registry = _build_app(tmp_path)

    _add_stop_report(store, event_id="pending-report")
    failed_report_id = _add_stop_report(store, event_id="failed-report")
    claim = store.claim_report_analysis(failed_report_id, max_attempts=3)
    assert claim is not None
    store.mark_report_analysis_failed(
        failed_report_id,
        attempt=1,
        error_code="provider_timeout",
        next_retry_at=None,
    )

    store.add_raw_transcript(
        "session-raw-pending",
        "student-a",
        "pending transcript",
        content_sha256="pending-sha",
    )
    store.set_raw_transcript_analysis_status(
        "session-raw-pending",
        "student-a",
        status="pending",
        content_sha256="pending-sha",
    )
    store.add_raw_transcript(
        "session-raw-failed",
        "student-a",
        "failed transcript",
        content_sha256="failed-sha",
    )
    store.set_raw_transcript_analysis_status(
        "session-raw-failed",
        "student-a",
        status="failed",
        error_message="provider_timeout",
        content_sha256="failed-sha",
    )

    _add_attention(store, source_id="open-attention")
    in_progress = _add_attention(store, source_id="handled-attention")
    store.update_attention_status(
        int(in_progress["id"]),
        status="in_progress",
        mentor_id="mentor-a",
        note="working",
    )

    registry.register_float("student-a", object())
    registry.register_float("student-a", object())
    registry.register_float("student-b", object())
    registry.register_mentor(object())
    registry.register_mentor(object())

    client = TestClient(app)
    denied = client.get("/api/mentor/system-status")
    response = client.get(
        "/api/mentor/system-status",
        headers={"Authorization": f"Bearer {MENTOR_TOKEN}"},
    )

    assert denied.status_code == 401
    assert response.status_code == 200
    assert response.json() == {
        "version": "0.2.0",
        "pending_analyses": 2,
        "failed_analyses": 2,
        "open_attention": 1,
        "float_connections": 3,
        "mentor_connections": 2,
        "windows_rollout_status": "BLOCKED: real-machine evidence missing",
    }


def test_health_response_remains_backward_compatible(tmp_path):
    app, _store, _registry = _build_app(tmp_path)

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "UP", "student": "local-student"}


def test_system_status_uses_validated_windows_evidence_for_current_build(
    tmp_path,
    monkeypatch,
):
    evidence_path = tmp_path / "windows-w1-evidence.json"
    expected_commit = "a" * 40
    expected_build = "pilot-build-7"
    expected_runner = "windows-pilot-01"
    app, _store, _registry = _build_app(
        tmp_path,
        windows_rollout={
            "evidence_path": str(evidence_path),
            "expected_commit": expected_commit,
            "expected_build": expected_build,
            "expected_runner_id": expected_runner,
            # This ordinary config flag must never be authoritative.
            "rollout_ready": False,
        },
    )
    captured = {}

    def fake_validate(path, expected_commit_arg, expected_build_arg, **kwargs):
        captured.update(
            path=str(path),
            expected_commit=expected_commit_arg,
            expected_build=expected_build_arg,
            expected_runner_id=kwargs.get("expected_runner_id"),
        )
        return WindowsEvidenceResult(
            status="rollout_ready",
            verdict="rollout_ready",
            rollout_ready=True,
            evidence_sha256="b" * 64,
        )

    monkeypatch.setattr(mentor_routes, "validate_windows_evidence", fake_validate)

    response = TestClient(app).get(
        "/api/mentor/system-status",
        headers={"Authorization": f"Bearer {MENTOR_TOKEN}"},
    )

    assert response.status_code == 200
    assert response.json()["windows_rollout_status"] == "rollout_ready"
    assert captured == {
        "path": str(evidence_path),
        "expected_commit": expected_commit,
        "expected_build": expected_build,
        "expected_runner_id": expected_runner,
    }


def test_system_status_cannot_be_promoted_by_an_ordinary_config_boolean(tmp_path):
    app, _store, _registry = _build_app(
        tmp_path,
        windows_rollout={"rollout_ready": True},
    )

    response = TestClient(app).get(
        "/api/mentor/system-status",
        headers={"Authorization": f"Bearer {MENTOR_TOKEN}"},
    )

    assert response.status_code == 200
    assert (
        response.json()["windows_rollout_status"]
        == "BLOCKED: real-machine evidence missing"
    )
