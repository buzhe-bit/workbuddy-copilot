from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading

from fastapi.testclient import TestClient

from copilot.app_context import AppContext
from copilot.connections import WSRegistry
from copilot.eventbus import EventBus
from copilot.service import create_app
from copilot.services import AnalysisService, MessageService, SessionQueryService
from copilot.store import Store
from copilot.upload_service import UploadRequestService
from copilot.upload_service import InvalidStateTransition


STUDENT_TOKEN = "student-token"
MENTOR_TOKEN = "mentor-token"


async def _fake_llm(config, snap, event, latest_prompt):
    return {
        "topic": "",
        "understanding": "medium",
        "severity": "info",
        "diagnosis": "",
        "suggestion": "",
        "is_technical": False,
        "ai_reply_summary": "",
    }


def _build_app(tmp_path, *, llm=_fake_llm, enable_llm=False):
    store = Store(tmp_path / "copilot.db")
    bus = EventBus()
    registry = WSRegistry(send_timeout=0.05)
    bus.subscribe(registry.handle_event)
    config = {
        "student_id": "server",
        "service": {"host": "0.0.0.0", "port": 8765},
        "store": {"db_path": str(tmp_path / "copilot.db")},
        "auth": {
            "mode": "public",
            "student_tokens": {"student-a": STUDENT_TOKEN},
            "mentor_token": MENTOR_TOKEN,
        },
        "analysis": {"enable_llm": enable_llm},
        "llm": {
            "enable_llm": enable_llm,
            "api_key": "test-key" if enable_llm else "",
            "model": "test-model" if enable_llm else "",
            "api_base": "https://llm.invalid" if enable_llm else "",
        },
    }
    context = AppContext(
        config=config,
        store=store,
        analysis_svc=AnalysisService(store, llm, config, bus),
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=registry,
        upload_svc=UploadRequestService(store),
    )
    return create_app(context), store


def _student_headers() -> dict[str, str]:
    return {"X-Copilot-Token": STUDENT_TOKEN}


def _mentor_headers() -> dict[str, str]:
    return {"X-Copilot-Token": MENTOR_TOKEN}


def test_upload_request_status_flow_and_error_are_persisted(tmp_path):
    store = Store(tmp_path / "copilot.db")
    request_id = store.add_upload_request(
        mentor_id="mentor-1",
        student_id="student-a",
        session_id="sess-a",
    )

    running = store.update_upload_request_status(
        request_id,
        student_id="student-a",
        status="running",
    )
    failed = store.update_upload_request_status(
        request_id,
        student_id="student-a",
        status="failed",
        error_message="network timeout",
        result={"failed": 1},
    )

    assert running == 1
    assert failed == 1
    row = store.get_upload_request(request_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_message"] == "network timeout"
    assert row["result_json"] == '{"failed": 1}'
    assert row["updated_at"] >= row["created_at"]
    assert store.list_pending_upload_requests("student-a") == []


def test_offline_upload_request_is_visible_to_student_and_can_complete(tmp_path):
    app, store = _build_app(tmp_path)

    with TestClient(app) as client:
        requested = client.post(
            "/api/mentor/students/student-a/request-upload",
            headers=_mentor_headers(),
            json={"mentor_id": "mentor-1", "session_id": "sess-a"},
        )
        assert requested.status_code == 200
        request_id = requested.json()["request_id"]

        catchup = client.get(
            "/api/student/upload-requests?student_id=student-a",
            headers=_student_headers(),
        )
        assert catchup.status_code == 200
        assert catchup.json()["items"] == [{
            "request_id": request_id,
            "mentor_id": "mentor-1",
            "student_id": "student-a",
            "session_id": "sess-a",
            "status": "pending",
            "created_at": catchup.json()["items"][0]["created_at"],
            "updated_at": catchup.json()["items"][0]["updated_at"],
            "error_message": "",
            "result": None,
            "transfer_status": "pending",
            "analysis_status": "not_requested",
            "transfer_error": "",
            "analysis_error": "",
        }]

        running = client.post(
            f"/api/student/upload-requests/{request_id}/status",
            headers=_student_headers(),
            json={"student_id": "student-a", "status": "running"},
        )
        done = client.post(
            f"/api/student/upload-requests/{request_id}/status",
            headers=_student_headers(),
            json={
                "student_id": "student-a",
                "status": "done",
                "result": {"total": 2, "synced": 2, "failed": 0},
            },
        )
        later = client.get(
            "/api/student/upload-requests?student_id=student-a",
            headers=_student_headers(),
        )

    assert running.status_code == 200
    assert done.status_code == 200
    assert done.json()["status"] == "done"
    assert later.status_code == 200
    assert later.json()["items"] == []
    row = store.get_upload_request(request_id)
    assert row is not None
    assert row["status"] == "done"
    assert row["transfer_status"] == "stored"
    assert row["analysis_status"] == "not_requested"
    assert row["result_json"] == '{"total": 2, "synced": 2, "failed": 0}'


def test_upload_request_status_rejects_cross_student_update(tmp_path):
    app, store = _build_app(tmp_path)
    request_id = store.add_upload_request("mentor-1", "student-a")

    with TestClient(app) as client:
        resp = client.post(
            f"/api/student/upload-requests/{request_id}/status",
            headers=_student_headers(),
            json={"student_id": "student-b", "status": "failed", "error_message": "bad"},
        )

    assert resp.status_code == 403
    assert store.get_upload_request(request_id)["status"] == "pending"


def test_legacy_store_status_api_derives_transfer_axis(tmp_path):
    store = Store(tmp_path / "copilot.db")
    request_id = store.add_upload_request("mentor-1", "student-a")

    updated = store.update_upload_request_status(
        request_id,
        student_id="student-a",
        status="done",
        result={"synced": 1},
    )

    assert updated == 1
    row = store.get_upload_request(request_id)
    assert row is not None
    assert row["status"] == "done"
    assert row["transfer_status"] == "stored"
    assert row["analysis_status"] == "not_requested"


def test_response_prefers_transfer_error_then_analysis_error(tmp_path):
    app, store = _build_app(tmp_path)
    service = app.state.context.upload_svc
    request_id = service.create("mentor-1", "student-a")
    service.mark_transfer(request_id, "student-a", "failed", error="upload offline")
    service.mark_analysis(request_id, "student-a", "pending")
    service.mark_analysis(request_id, "student-a", "failed", error="llm timeout")

    with TestClient(app) as client:
        analysis_failed = client.get(
            "/api/student/upload-requests?student_id=student-a&status=all",
            headers=_student_headers(),
        )

    item = analysis_failed.json()["items"][0]
    assert item["status"] == "failed"
    assert item["transfer_status"] == "failed"
    assert item["analysis_status"] == "failed"
    assert item["error_message"] == "upload offline"
    assert item["transfer_error"] == "upload offline"
    assert item["analysis_error"] == "llm timeout"


def test_controller_routes_status_changes_through_injected_upload_service(tmp_path):
    app, store = _build_app(tmp_path)
    real_service = app.state.context.upload_svc
    request_id = real_service.create("mentor-1", "student-a")
    calls = []

    class RecordingService:
        def mark_transfer(self, *args, **kwargs):
            calls.append((args, kwargs))
            return real_service.mark_transfer(*args, **kwargs)

        def create(self, *args, **kwargs):
            return real_service.create(*args, **kwargs)

        def refresh_parent_analysis(self, *args, **kwargs):
            return real_service.refresh_parent_analysis(*args, **kwargs)

    app.state.context.upload_svc = RecordingService()

    with TestClient(app) as client:
        response = client.post(
            f"/api/student/upload-requests/{request_id}/status",
            headers=_student_headers(),
            json={"student_id": "student-a", "status": "running"},
        )

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0][0] == (request_id, "student-a", "running")
    assert "store.update_upload_request_status" not in __import__(
        "inspect"
    ).getsource(__import__("copilot.service", fromlist=["create_app"]).create_app)
    assert store.get_upload_request(request_id)["transfer_status"] == "running"


def test_controller_rejects_illegal_backward_transition(tmp_path):
    app, _store = _build_app(tmp_path)
    request_id = app.state.context.upload_svc.create("mentor-1", "student-a")
    app.state.context.upload_svc.mark_transfer(request_id, "student-a", "running")
    app.state.context.upload_svc.mark_transfer(request_id, "student-a", "stored")

    with TestClient(app) as client:
        response = client.post(
            f"/api/student/upload-requests/{request_id}/status",
            headers=_student_headers(),
            json={"student_id": "student-a", "status": "running"},
        )

    assert response.status_code == 409
    assert "stored" in response.json()["detail"]


def test_list_controller_reads_through_injected_upload_service(tmp_path, monkeypatch):
    app, store = _build_app(tmp_path)
    request_id = app.state.context.upload_svc.create("mentor-1", "student-a")
    row = store.get_upload_request(request_id)
    calls = []

    class ReadService:
        def list(self, student_id, status=None):
            calls.append((student_id, status))
            return [row]

    app.state.context.upload_svc = ReadService()
    monkeypatch.setattr(
        store,
        "list_upload_requests",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("controller read Store")),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/student/upload-requests?student_id=student-a&status=all",
            headers=_student_headers(),
        )

    assert response.status_code == 200
    assert response.json()["items"][0]["request_id"] == request_id
    assert calls == [("student-a", None)]


def test_update_controller_uses_row_returned_by_upload_service(tmp_path, monkeypatch):
    app, store = _build_app(tmp_path)
    request_id = app.state.context.upload_svc.create("mentor-1", "student-a")
    row = store.get_upload_request(request_id)
    row["status"] = "running"
    row["transfer_status"] = "running"
    calls = []

    class WriteService:
        def mark_transfer(self, *args, **kwargs):
            calls.append((args, kwargs))
            return row

        def refresh_parent_analysis(self, *args, **kwargs):
            return []

    app.state.context.upload_svc = WriteService()
    monkeypatch.setattr(
        store,
        "get_upload_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("controller read Store")),
    )

    with TestClient(app) as client:
        response = client.post(
            f"/api/student/upload-requests/{request_id}/status",
            headers=_student_headers(),
            json={"student_id": "student-a", "status": "running"},
        )

    assert response.status_code == 200
    assert response.json()["transfer_status"] == "running"
    assert calls[0][0] == (request_id, "student-a", "running")


def test_retry_without_result_clears_failed_result_from_api(tmp_path):
    app, _store = _build_app(tmp_path)
    request_id = app.state.context.upload_svc.create("mentor-1", "student-a")

    with TestClient(app) as client:
        failed = client.post(
            f"/api/student/upload-requests/{request_id}/status",
            headers=_student_headers(),
            json={
                "student_id": "student-a",
                "status": "failed",
                "error_message": "offline",
                "result": {"failed": 1},
            },
        )
        running = client.post(
            f"/api/student/upload-requests/{request_id}/status",
            headers=_student_headers(),
            json={"student_id": "student-a", "status": "running"},
        )
        stored = client.post(
            f"/api/student/upload-requests/{request_id}/status",
            headers=_student_headers(),
            json={"student_id": "student-a", "status": "done"},
        )

    assert failed.json()["result"] == {"failed": 1}
    assert running.status_code == 200
    assert stored.status_code == 200
    assert stored.json()["status"] == "done"
    assert stored.json()["result"] is None


def _seed_failed_specific_upload(app, store, *, request_id=None):
    service = app.state.context.upload_svc
    request_id = request_id or service.create(
        "mentor-1", "student-a", session_id="sess-a"
    )
    raw = "\n".join([
        json.dumps({"type": "user", "message": {"content": "debug this"}}),
        json.dumps({"type": "assistant", "message": {"content": "try logs"}}),
    ])
    store.replace_session_messages(
        session_id="sess-a",
        student_id="student-a",
        turns=[
            {"seq": 1, "role": "user", "text": "debug this"},
            {"seq": 2, "role": "assistant", "text": "try logs"},
        ],
        raw=raw,
        sha="sha-a",
    )
    service.mark_transfer(request_id, "student-a", "running")
    service.mark_transfer(
        request_id,
        "student-a",
        "stored",
        result={"total": 1, "synced": 1, "failed": 0},
    )
    service.mark_analysis(request_id, "student-a", "pending")
    service.mark_analysis(request_id, "student-a", "running")
    service.mark_analysis(
        request_id, "student-a", "failed", error="LLM provider unavailable"
    )
    store.set_raw_transcript_analysis_status(
        "sess-a",
        "student-a",
        status="failed",
        error_message="LLM provider unavailable",
        content_sha256="sha-a",
    )
    return request_id, raw


def _bulk_messages_snapshot(store, session_id):
    with store._conn() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                """SELECT id, seq, role, text, source, content_sha256
                   FROM messages WHERE session_id = ? ORDER BY id""",
                (session_id,),
            ).fetchall()
        ]


def test_mentor_get_upload_request_returns_both_axes_errors_and_result(tmp_path):
    app, store = _build_app(tmp_path)
    request_id, _raw = _seed_failed_specific_upload(app, store)

    with TestClient(app) as client:
        response = client.get(
            f"/api/mentor/upload-requests/{request_id}",
            headers=_mentor_headers(),
        )
        missing = client.get(
            "/api/mentor/upload-requests/missing",
            headers=_mentor_headers(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "request_id": request_id,
        "mentor_id": "mentor-1",
        "student_id": "student-a",
        "session_id": "sess-a",
        "status": "done",
        "created_at": response.json()["created_at"],
        "updated_at": response.json()["updated_at"],
        "error_message": "LLM provider unavailable",
        "result": {"total": 1, "synced": 1, "failed": 0},
        "transfer_status": "stored",
        "analysis_status": "failed",
        "transfer_error": "",
        "analysis_error": "LLM provider unavailable",
    }
    assert missing.status_code == 404


def test_retry_analysis_reuses_saved_raw_without_rewriting_transfer(tmp_path):
    app, store = _build_app(tmp_path, enable_llm=True)
    request_id, raw_before = _seed_failed_specific_upload(app, store)
    raw_row_before = store.get_raw_transcript_for_student_session("student-a", "sess-a")
    messages_before = _bulk_messages_snapshot(store, "sess-a")
    status_events = []

    async def capture(payload):
        if payload.get("type") == "upload_request_status":
            status_events.append(payload)

    app.state.context.bus.subscribe(capture)

    with TestClient(app) as client:
        retried = client.post(
            f"/api/mentor/upload-requests/{request_id}/retry-analysis",
            headers=_mentor_headers(),
        )
        latest = client.get(
            f"/api/mentor/upload-requests/{request_id}",
            headers=_mentor_headers(),
        )

    assert retried.status_code == 202
    assert retried.json()["request_id"] == request_id
    assert retried.json()["transfer_status"] == "stored"
    assert retried.json()["analysis_status"] == "pending"
    assert latest.status_code == 200
    assert latest.json()["analysis_status"] == "done"
    assert latest.json()["analysis_error"] == ""
    assert latest.json()["result"] == {"total": 1, "synced": 1, "failed": 0}
    assert [event["analysis_status"] for event in status_events] == [
        "pending", "running", "done"
    ]
    raw_row_after = store.get_raw_transcript_for_student_session("student-a", "sess-a")
    assert raw_row_after["id"] == raw_row_before["id"]
    assert raw_row_after["content"] == raw_before
    assert raw_row_after["content_sha256"] == raw_row_before["content_sha256"]
    assert _bulk_messages_snapshot(store, "sess-a") == messages_before


def test_retry_success_fans_out_to_failed_requests_sharing_the_same_raw(tmp_path):
    llm_calls = []

    async def counting_llm(config, snap, event, latest_prompt):
        llm_calls.append(latest_prompt)
        return await _fake_llm(config, snap, event, latest_prompt)

    app, store = _build_app(tmp_path, llm=counting_llm, enable_llm=True)
    service = app.state.context.upload_svc
    request_ids = [
        service.create("mentor-1", "student-a", session_id="sess-shared"),
        service.create("mentor-1", "student-a", session_id="sess-shared"),
    ]
    for request_id in request_ids:
        service.mark_transfer(request_id, "student-a", "running")
        _add_request_child(
            service,
            store,
            request_id,
            "sess-shared",
            "sha-shared",
            "failed",
        )
        service.mark_transfer(request_id, "student-a", "stored")
        service.refresh_parent_analysis(request_id, "student-a")
    store.set_raw_transcript_analysis_status(
        "sess-shared",
        "student-a",
        status="failed",
        error_message="provider failed",
        content_sha256="sha-shared",
    )

    with TestClient(app) as client:
        first_retry = client.post(
            f"/api/mentor/upload-requests/{request_ids[0]}/retry-analysis",
            headers=_mentor_headers(),
        )
        second_retry = client.post(
            f"/api/mentor/upload-requests/{request_ids[1]}/retry-analysis",
            headers=_mentor_headers(),
        )

    assert first_retry.status_code == 202
    assert second_retry.status_code == 409
    assert len(llm_calls) == 1
    assert store.get_raw_transcript_for_student_session_sha(
        "student-a", "sess-shared", "sha-shared"
    )["analysis_status"] == "done"
    for request_id in request_ids:
        assert store.get_upload_request(request_id)["analysis_status"] == "done"
        assert store.list_upload_request_sessions(request_id)[0]["analysis_status"] == "done"


def test_retry_analysis_failure_remains_failed_with_bounded_error(tmp_path):
    async def failed_llm(config, snap, event, latest_prompt):
        raise TimeoutError("provider leaked detail must not reach browser")

    app, store = _build_app(tmp_path, llm=failed_llm, enable_llm=True)
    request_id, _raw = _seed_failed_specific_upload(app, store)

    with TestClient(app) as client:
        retried = client.post(
            f"/api/mentor/upload-requests/{request_id}/retry-analysis",
            headers=_mentor_headers(),
        )
        latest = client.get(
            f"/api/mentor/upload-requests/{request_id}",
            headers=_mentor_headers(),
        )

    assert retried.status_code == 202
    assert latest.json()["analysis_status"] == "failed"
    assert latest.json()["analysis_error"] == "analysis TimeoutError"
    assert "leaked detail" not in latest.text


def test_retry_analysis_rejects_illegal_state_bulk_request_and_missing_raw(tmp_path):
    app, store = _build_app(tmp_path, enable_llm=True)
    service = app.state.context.upload_svc
    pending_id = service.create("mentor-1", "student-a", session_id="sess-pending")
    bulk_id = service.create("mentor-1", "student-a")
    service.mark_transfer(bulk_id, "student-a", "running")
    service.mark_transfer(bulk_id, "student-a", "stored")
    service.mark_analysis(bulk_id, "student-a", "pending")
    service.mark_analysis(bulk_id, "student-a", "running")
    service.mark_analysis(bulk_id, "student-a", "failed", error="failed")
    no_raw_id = service.create("mentor-1", "student-a", session_id="sess-no-raw")
    service.mark_transfer(no_raw_id, "student-a", "running")
    service.mark_transfer(no_raw_id, "student-a", "stored")
    service.mark_analysis(no_raw_id, "student-a", "pending")
    service.mark_analysis(no_raw_id, "student-a", "running")
    service.mark_analysis(no_raw_id, "student-a", "failed", error="failed")

    with TestClient(app) as client:
        pending = client.post(
            f"/api/mentor/upload-requests/{pending_id}/retry-analysis",
            headers=_mentor_headers(),
        )
        bulk = client.post(
            f"/api/mentor/upload-requests/{bulk_id}/retry-analysis",
            headers=_mentor_headers(),
        )
        no_raw = client.post(
            f"/api/mentor/upload-requests/{no_raw_id}/retry-analysis",
            headers=_mentor_headers(),
        )
        missing = client.post(
            "/api/mentor/upload-requests/missing/retry-analysis",
            headers=_mentor_headers(),
        )

    assert pending.status_code == 409
    assert bulk.status_code == 409
    assert "children" in bulk.json()["detail"].lower()
    assert no_raw.status_code == 409
    assert missing.status_code == 404
    assert store.get_upload_request(pending_id)["analysis_status"] == "not_requested"
    assert store.get_upload_request(bulk_id)["analysis_status"] == "failed"
    assert store.get_upload_request(no_raw_id)["analysis_status"] == "failed"


def test_upload_status_events_are_snapshots_sent_only_to_mentors(tmp_path):
    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send_text(self, text):
            self.sent.append(json.loads(text))

    app, _store = _build_app(tmp_path)
    registry = app.state.context.ws_registry
    mentor = FakeWebSocket()
    student_a = FakeWebSocket()
    student_b = FakeWebSocket()
    registry.register_mentor(mentor)
    registry.register_float("student-a", student_a)
    registry.register_float("student-b", student_b)

    with TestClient(app) as client:
        request_id = client.post(
            "/api/mentor/students/student-a/request-upload",
            headers=_mentor_headers(),
        ).json()["request_id"]
        response = client.post(
            f"/api/student/upload-requests/{request_id}/status",
            headers=_student_headers(),
            json={
                "student_id": "student-a",
                "status": "failed",
                "error_message": "offline",
                "result": {
                    "total": 4,
                    "synced": 2,
                    "skipped": 1,
                    "failed": 1,
                    "transcript": [112, 114, 105, 118, 97, 116, 101],
                    "nested": {"total": 999, "token": "private-token"},
                    "success": 2,
                    "negative": -1,
                    "truthy": True,
                    "fraction": 1.5,
                    "huge": 10**30,
                },
            },
        )

    assert response.status_code == 200
    status_events = [x for x in mentor.sent if x.get("type") == "upload_request_status"]
    assert len(status_events) == 1
    event = status_events[0]
    assert event["request_id"] == request_id
    assert event["student_id"] == "student-a"
    assert event["transfer_status"] == "failed"
    assert event["analysis_status"] == "not_requested"
    assert event["result"] == {"total": 4, "synced": 2, "skipped": 1, "failed": 1}
    assert "token" not in json.dumps(event).lower()
    assert "transcript" not in json.dumps(event).lower()
    assert student_a.sent == [{
        "type": "mentor_command",
        "student_id": "student-a",
        "command": "upload_conversations",
        "request_id": request_id,
        "session_id": "",
        "mentor_id": "mentor",
        "timestamp": student_a.sent[0]["timestamp"],
    }]
    assert student_b.sent == []


def test_lifespan_marks_interrupted_parent_and_child_failed_for_retry(tmp_path):
    app, store = _build_app(tmp_path, enable_llm=True)
    service = app.state.context.upload_svc
    request_id = service.create("mentor-1", "student-a", session_id="sess-crash")
    store.replace_session_messages(
        "sess-crash", "student-a",
        [{"seq": 1, "role": "user", "text": "recover me"}],
        "raw recover", "sha-recover",
    )
    store.set_raw_transcript_analysis_status(
        "sess-crash",
        "student-a",
        status="running",
        content_sha256="sha-recover",
        increment_attempt=True,
    )
    service.mark_transfer(request_id, "student-a", "running")
    service.register_session(request_id, "student-a", "sess-crash", "sha-recover")
    service.mark_session_analysis(
        request_id, "student-a", "sess-crash", "running"
    )
    service.mark_transfer(request_id, "student-a", "stored")
    assert store.get_upload_request(request_id)["analysis_status"] == "running"

    rebuilt_app, rebuilt_store = _build_app(tmp_path, enable_llm=True)
    with TestClient(rebuilt_app):
        pass

    parent = rebuilt_store.get_upload_request(request_id)
    child = rebuilt_store.list_upload_request_sessions(request_id)[0]
    assert parent["transfer_status"] == "stored"
    assert parent["analysis_status"] == "failed"
    assert parent["analysis_error"] == "analysis interrupted; retry"
    assert child["analysis_status"] == "failed"
    assert child["analysis_error"] == "analysis interrupted; retry"
    interrupted_raw = rebuilt_store.get_raw_transcript_for_student_session_sha(
        "student-a", "sess-crash", "sha-recover"
    )
    assert interrupted_raw is not None
    assert interrupted_raw["analysis_status"] == "failed"
    assert interrupted_raw["analysis_error"] == "analysis interrupted; retry"

    with TestClient(rebuilt_app) as client:
        retried = client.post(
            f"/api/mentor/upload-requests/{request_id}/retry-analysis",
            headers=_mentor_headers(),
        )
    assert retried.status_code == 202
    assert rebuilt_store.get_upload_request(request_id)["analysis_status"] == "done"
    assert rebuilt_store.list_upload_request_sessions(request_id)[0]["analysis_status"] == "done"
    retried_raw = rebuilt_store.get_raw_transcript_for_student_session_sha(
        "student-a", "sess-crash", "sha-recover"
    )
    assert retried_raw is not None
    assert retried_raw["analysis_status"] == "done"
    assert retried_raw["analysis_attempts"] == 2


def test_lifespan_projects_committed_child_done_instead_of_failing_parent(tmp_path):
    app, store = _build_app(tmp_path, enable_llm=True)
    service = app.state.context.upload_svc
    request_id = service.create("mentor-1", "student-a", session_id="sess-committed")
    store.replace_session_messages(
        "sess-committed",
        "student-a",
        [{"seq": 1, "role": "user", "text": "committed before crash"}],
        "raw committed",
        "sha-committed",
    )
    store.queue_raw_transcript_analysis(
        student_id="student-a",
        session_id="sess-committed",
        content_sha256="sha-committed",
    )
    service.register_session(
        request_id, "student-a", "sess-committed", "sha-committed"
    )
    service.mark_transfer(request_id, "student-a", "running")
    service.mark_transfer(request_id, "student-a", "stored")
    claim = store.claim_raw_transcript_analysis(
        student_id="student-a",
        session_id="sess-committed",
        content_sha256="sha-committed",
        prompt_hash="prompt-hash",
    )
    assert claim["state"] == "claimed"
    service.refresh_parent_analysis(request_id, "student-a")
    committed = store.commit_bulk_analysis_if_current(
        student_id="student-a",
        session_id="sess-committed",
        content_sha256="sha-committed",
        raw_id=claim["raw_id"],
        generation=claim["generation"],
        result={
            "topic": "committed",
            "understanding": "medium",
            "severity": "info",
            "diagnosis": "committed before parent refresh",
            "model": "provider-model",
            "prompt_hash": "prompt-hash",
            "latency_ms": 1,
        },
        session_title="",
        msg_count=1,
    )
    assert committed is not None
    assert store.list_upload_request_sessions(request_id)[0]["analysis_status"] == "done"
    assert store.get_upload_request(request_id)["analysis_status"] == "running"

    rebuilt_app, rebuilt_store = _build_app(tmp_path, enable_llm=True)
    with TestClient(rebuilt_app):
        pass

    parent = rebuilt_store.get_upload_request(request_id)
    child = rebuilt_store.list_upload_request_sessions(request_id)[0]
    assert child["analysis_status"] == "done"
    assert parent["analysis_status"] == "done"
    assert parent["analysis_error"] == ""


def _add_request_child(service, store, request_id, session_id, sha, final_status):
    raw = json.dumps({"type": "message", "role": "user", "content": session_id}) + "\n"
    store.replace_session_messages(
        session_id, "student-a",
        [{"seq": 1, "role": "user", "text": session_id}],
        raw, sha,
    )
    service.register_session(request_id, "student-a", session_id, sha)
    service.mark_session_analysis(
        request_id, "student-a", session_id, "running", sha=sha
    )
    service.mark_session_analysis(
        request_id, "student-a", session_id, final_status, sha=sha,
        error="old failure" if final_status == "failed" else "",
    )


def test_batch_retry_reanalyzes_only_failed_children_and_aggregates_done(tmp_path):
    app, store = _build_app(tmp_path, enable_llm=True)
    service = app.state.context.upload_svc
    request_id = service.create("mentor-1", "student-a")
    service.mark_transfer(request_id, "student-a", "running")
    _add_request_child(service, store, request_id, "sess-failed-1", "sha-f1", "failed")
    _add_request_child(service, store, request_id, "sess-done", "sha-done", "done")
    _add_request_child(service, store, request_id, "sess-failed-2", "sha-f2", "failed")
    service.mark_transfer(request_id, "student-a", "stored")
    service.refresh_parent_analysis(request_id, "student-a")
    done_before = next(
        row for row in store.list_upload_request_sessions(request_id)
        if row["session_id"] == "sess-done"
    )

    with TestClient(app) as client:
        retried = client.post(
            f"/api/mentor/upload-requests/{request_id}/retry-analysis",
            headers=_mentor_headers(),
        )

    assert retried.status_code == 202
    children = store.list_upload_request_sessions(request_id)
    assert {row["session_id"]: row["analysis_status"] for row in children} == {
        "sess-failed-1": "done", "sess-done": "done", "sess-failed-2": "done",
    }
    done_after = next(row for row in children if row["session_id"] == "sess-done")
    assert done_after["updated_at"] == done_before["updated_at"]
    assert store.get_upload_request(request_id)["analysis_status"] == "done"


def test_batch_retry_validates_every_failed_child_before_mutating(tmp_path):
    app, store = _build_app(tmp_path, enable_llm=True)
    service = app.state.context.upload_svc
    request_id = service.create("mentor-1", "student-a")
    service.mark_transfer(request_id, "student-a", "running")
    _add_request_child(service, store, request_id, "sess-present", "sha-present", "failed")
    _add_request_child(service, store, request_id, "sess-missing", "sha-missing", "failed")
    service.mark_transfer(request_id, "student-a", "stored")
    service.refresh_parent_analysis(request_id, "student-a")
    with store._conn() as conn:
        conn.execute(
            "DELETE FROM raw_transcripts WHERE session_id = ? AND content_sha256 = ?",
            ("sess-missing", "sha-missing"),
        )

    with TestClient(app) as client:
        rejected = client.post(
            f"/api/mentor/upload-requests/{request_id}/retry-analysis",
            headers=_mentor_headers(),
        )

    assert rejected.status_code == 409
    assert store.get_upload_request(request_id)["analysis_status"] == "failed"
    assert {row["analysis_status"] for row in store.list_upload_request_sessions(request_id)} == {"failed"}


def test_child_new_sha_resets_state_and_stale_sha_cannot_complete_it(tmp_path):
    app, store = _build_app(tmp_path, enable_llm=True)
    service = app.state.context.upload_svc
    request_id = service.create("mentor-1", "student-a", session_id="sess-sha")
    service.mark_transfer(request_id, "student-a", "running")
    service.register_session(request_id, "student-a", "sess-sha", "sha-old")
    service.mark_session_analysis(
        request_id, "student-a", "sess-sha", "running", sha="sha-old"
    )
    service.mark_session_analysis(
        request_id, "student-a", "sess-sha", "failed", sha="sha-old", error="old failure"
    )

    child, _rows = service.register_session(
        request_id, "student-a", "sess-sha", "sha-new", analysis_status="pending"
    )
    assert child["sha"] == "sha-new"
    assert child["analysis_status"] == "pending"
    assert child["analysis_error"] == ""
    stale_child, stale_rows = service.mark_session_analysis(
        request_id, "student-a", "sess-sha", "done", sha="sha-old"
    )
    assert stale_rows == []
    assert stale_child["sha"] == "sha-new"
    assert stale_child["analysis_status"] == "pending"

    same_child, _rows = service.register_session(
        request_id, "student-a", "sess-sha", "sha-new", analysis_status="done"
    )
    assert same_child["analysis_status"] == "pending"
    assert same_child["updated_at"] == stale_child["updated_at"]


def test_specific_retry_uses_child_exact_sha_not_newer_unrelated_raw(tmp_path):
    app, store = _build_app(tmp_path, enable_llm=True)
    service = app.state.context.upload_svc
    request_id = service.create("mentor-1", "student-a", session_id="sess-exact")
    store.replace_session_messages(
        "sess-exact", "student-a",
        [{"seq": 1, "role": "user", "text": "old"}],
        "old raw", "sha-old",
    )
    service.mark_transfer(request_id, "student-a", "running")
    service.register_session(request_id, "student-a", "sess-exact", "sha-old")
    service.mark_session_analysis(
        request_id, "student-a", "sess-exact", "running", sha="sha-old"
    )
    service.mark_session_analysis(
        request_id, "student-a", "sess-exact", "failed", sha="sha-old", error="failed"
    )
    service.mark_transfer(request_id, "student-a", "stored")
    service.refresh_parent_analysis(request_id, "student-a")
    store.add_raw_transcript("sess-exact", "student-a", "newer raw", "sha-new")

    pending, work_items = service.prepare_analysis_retry(request_id)

    assert pending["analysis_status"] == "pending"
    assert len(work_items) == 1
    assert work_items[0]["sha"] == "sha-old"
    assert work_items[0]["raw"]["content"] == "old raw"


def test_concurrent_retry_claim_allows_exactly_one_store_winner(tmp_path):
    app, store = _build_app(tmp_path, enable_llm=True)
    service = app.state.context.upload_svc
    request_id = service.create("mentor-1", "student-a")
    service.mark_transfer(request_id, "student-a", "running")
    _add_request_child(service, store, request_id, "sess-race", "sha-race", "failed")
    service.mark_transfer(request_id, "student-a", "stored")
    service.refresh_parent_analysis(request_id, "student-a")

    services = [
        UploadRequestService(Store(tmp_path / "copilot.db")),
        UploadRequestService(Store(tmp_path / "copilot.db")),
    ]
    start = threading.Barrier(2)

    def claim(candidate):
        start.wait(timeout=2)
        try:
            _parent, targets = candidate.prepare_analysis_retry(request_id)
            return ("ok", [target["sha"] for target in targets])
        except InvalidStateTransition as exc:
            return ("conflict", str(exc))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, services))

    assert sorted(result[0] for result in results) == ["conflict", "ok"]
    assert store.get_upload_request(request_id)["analysis_status"] == "pending"
    children = store.list_upload_request_sessions(request_id)
    assert [(row["analysis_status"], row["analysis_error"]) for row in children] == [
        ("pending", "")
    ]


def test_retry_api_duplicate_schedules_background_once(tmp_path):
    llm_calls = []

    async def counting_llm(config, snap, event, latest_prompt):
        llm_calls.append(latest_prompt)
        return await _fake_llm(config, snap, event, latest_prompt)

    app, store = _build_app(tmp_path, llm=counting_llm, enable_llm=True)
    service = app.state.context.upload_svc
    request_id = service.create("mentor-1", "student-a")
    service.mark_transfer(request_id, "student-a", "running")
    _add_request_child(service, store, request_id, "sess-api-once", "sha-api", "failed")
    service.mark_transfer(request_id, "student-a", "stored")
    service.refresh_parent_analysis(request_id, "student-a")

    start = threading.Barrier(2)
    with TestClient(app) as client:
        def retry_once():
            start.wait(timeout=2)
            return client.post(
                f"/api/mentor/upload-requests/{request_id}/retry-analysis",
                headers=_mentor_headers(),
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _index: retry_once(), range(2)))

    assert sorted(response.status_code for response in responses) == [202, 409]
    assert len(llm_calls) == 1
