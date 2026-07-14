from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copilot.models import AnalysisEnvelope, UploadOutcome
from copilot import wb_upload
from copilot.app_context import AppContext
from copilot.connections import WSRegistry
from copilot.eventbus import EventBus
from copilot.service import create_app
from copilot.services import AnalysisService, MessageService, SessionQueryService
from copilot.store import Store
from copilot.upload_service import UploadRequestService
from copilot.student_core.coordinator import StudentCoordinator
from copilot.student_core.models import HookEvent
from copilot.student_core.spool import EventSpool
from copilot.student_core.transcript_jobs import TranscriptUploadQueue
from copilot.student_core.transport import Accepted, StudentTransport
import copilot.student_platform.windows_runtime as windows_runtime_module
from copilot.student_platform.windows_runtime import (
    WindowsAnalysisStore,
    WindowsRuntimeBlocked,
    WindowsStudentRuntime,
)


pytestmark = [pytest.mark.windows, pytest.mark.integration, pytest.mark.critical]


class _Transport:
    student_id = "student-a"

    def __init__(self) -> None:
        self.acked: list[tuple[str, str]] = []

    def ack_message(self, message_id: str, *, student_id: str) -> Accepted:
        self.acked.append((student_id, message_id))
        return Accepted(200, {"ok": True})


def _command(request_id: str = "request-1") -> dict[str, str]:
    return {
        "type": "mentor_command",
        "student_id": "student-a",
        "command": "upload_conversations",
        "request_id": request_id,
        "session_id": "session-a",
    }


def test_upload_outcome_requires_a_real_complete_match() -> None:
    assert UploadOutcome(
        matched=1,
        attempted=1,
        accepted=1,
        skipped=0,
        failed=0,
    ).complete is True
    assert UploadOutcome(
        matched=0,
        attempted=0,
        accepted=0,
        skipped=0,
        failed=0,
    ).complete is False
    assert UploadOutcome(
        matched=1,
        attempted=0,
        accepted=1,
        skipped=0,
        failed=0,
    ).complete is False


class _Session:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    def to_dict(self) -> dict[str, str]:
        return {"session_id": self.session_id, "work_dir": "C:/work"}


class _DataAdapter:
    def __init__(self, session_ids: list[str]) -> None:
        self._sessions = [_Session(value) for value in session_ids]

    def list_sessions(self):
        return list(self._sessions)

    def read_transcript(self, session_id: str):
        class Read:
            failure = None
            content = json.dumps({
                "type": "message",
                "session_id": session_id,
                "role": "user",
                "content": "help",
            }) + "\n"

        return Read()


def test_specific_upload_with_zero_local_match_is_not_complete(monkeypatch) -> None:
    monkeypatch.setattr(wb_upload, "get_known_shas", lambda *_args, **_kwargs: {})

    result = wb_upload.upload_conversations(
        {},
        "student-a",
        session_id="missing-session",
        request_id="request-1",
        data_adapter=_DataAdapter(["other-session"]),
    )

    assert isinstance(result, UploadOutcome)
    assert result.complete is False
    assert result.error_code == "session_not_found"


def test_specific_upload_with_multiple_local_matches_is_ambiguous(monkeypatch) -> None:
    monkeypatch.setattr(wb_upload, "get_known_shas", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        wb_upload,
        "post_transcript",
        lambda *_args, **_kwargs: pytest.fail("ambiguous session must not upload"),
    )

    result = wb_upload.upload_conversations(
        {},
        "student-a",
        session_id="session-a",
        request_id="request-1",
        data_adapter=_DataAdapter(["session-a", "session-a"]),
    )

    assert result.matched == 2
    assert result.attempted == 0
    assert result.complete is False
    assert result.error_code == "session_ambiguous"


def test_upload_requires_server_confirmation_of_session_and_sha(monkeypatch) -> None:
    monkeypatch.setattr(wb_upload, "get_known_shas", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        wb_upload,
        "post_transcript",
        lambda *_args, **_kwargs: {"ok": True},
    )

    result = wb_upload.upload_conversations(
        {},
        "student-a",
        session_id="session-a",
        request_id="request-1",
        data_adapter=_DataAdapter(["session-a"]),
    )

    assert result.accepted == 0
    assert result.failed == 1
    assert result.complete is False
    assert result.error_code == "response_unconfirmed"


def test_stop_job_upload_uses_store_only_source_contract(monkeypatch) -> None:
    monkeypatch.setattr(wb_upload, "get_known_shas", lambda *_args, **_kwargs: {})
    captured: list[dict] = []

    def post(_server_url, session_id, payload, **_kwargs):
        captured.append(payload)
        return {
            "ok": True,
            "session_id": session_id,
            "sha": payload["sha"],
            "analysis_mode": "store_only",
        }

    monkeypatch.setattr(wb_upload, "post_transcript", post)
    result = wb_upload.upload_conversations(
        {},
        "student-a",
        mode="full",
        session_id="session-a",
        analysis_mode="store_only",
        source_event_id="event-a",
        source_report_id=7,
        data_adapter=_DataAdapter(["session-a"]),
    )

    assert result.complete is True
    assert captured[0]["analysis_mode"] == "store_only"
    assert captured[0]["source_event_id"] == "event-a"
    assert captured[0]["source_report_id"] == 7
    assert "request_id" not in captured[0]
    assert UploadOutcome(
        matched=2,
        attempted=2,
        accepted=1,
        skipped=0,
        failed=1,
        error_code="partial_failure",
    ).complete is False


def test_analysis_envelope_has_one_stable_wire_shape() -> None:
    envelope = AnalysisEnvelope(
        analysis_id=9,
        student_id="student-a",
        session_id="session-a",
        report_id=42,
        event="Stop",
        result={"severity": "warn", "diagnosis": "stuck"},
        timestamp=123.5,
    )

    assert envelope.to_dict() == {
        "type": "analysis",
        "analysis_id": 9,
        "student_id": "student-a",
        "session_id": "session-a",
        "report_id": 42,
        "event": "Stop",
        "result": {"severity": "warn", "diagnosis": "stuck"},
        "timestamp": 123.5,
    }


def _analysis_app(tmp_path: Path):
    store = Store(tmp_path / "copilot.db")
    bus = EventBus()
    registry = WSRegistry(send_timeout=0.05)
    bus.subscribe(registry.handle_event)

    async def fake_llm(*_args, **_kwargs):
        raise AssertionError("catch-up must not call the model")

    config = {
        "student_id": "server-default",
        "store": {"db_path": str(tmp_path / "copilot.db")},
        "auth": {
            "mode": "pilot",
            "student_tokens": {
                "student-a": "token-a",
                "student-b": "token-b",
            },
            "mentor_token": "mentor-token",
        },
        "llm": {"enable_llm": False},
    }
    context = AppContext(
        config=config,
        store=store,
        analysis_svc=AnalysisService(store, fake_llm, config, bus),
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=registry,
        upload_svc=UploadRequestService(store),
    )
    return create_app(context), store


def _seed_analysis(store: Store, student_id: str, number: int) -> int:
    report_id = store.add_report(
        student_id=student_id,
        session_id=f"session-{student_id}",
        event="Stop",
        prompt=f"prompt-{number}",
        transcript_path="",
        msg_count=1,
        tool_calls=0,
    )
    store.add_analysis(
        report_id=report_id,
        student_id=student_id,
        session_id=f"session-{student_id}",
        result={
            "severity": "warn",
            "diagnosis": f"diagnosis-{number}",
            "suggestion": "inspect evidence",
        },
    )
    return report_id


def test_analysis_catch_up_is_scoped_ascending_and_paginated(tmp_path: Path) -> None:
    app, store = _analysis_app(tmp_path)
    expected = [_seed_analysis(store, "student-a", index) for index in range(5)]
    _seed_analysis(store, "student-b", 99)

    with TestClient(app) as client:
        first = client.get(
            "/api/student/analyses",
            params={"student_id": "student-a", "after_report_id": 0, "limit": 2},
            headers={"Authorization": "Bearer token-a"},
        )
        second = client.get(
            "/api/student/analyses",
            params={
                "student_id": "student-a",
                "after_report_id": first.json()["next_cursor"],
                "limit": 3,
            },
            headers={"Authorization": "Bearer token-a"},
        )
        cross_student = client.get(
            "/api/student/analyses",
            params={"student_id": "student-b", "after_report_id": 0},
            headers={"Authorization": "Bearer token-a"},
        )

    assert first.status_code == 200
    assert [item["report_id"] for item in first.json()["items"]] == expected[:2]
    assert first.json()["next_cursor"] == expected[1]
    assert first.json()["has_more"] is True
    assert [item["report_id"] for item in second.json()["items"]] == expected[2:]
    assert second.json()["next_cursor"] == expected[-1]
    assert second.json()["has_more"] is False
    assert all(item["student_id"] == "student-a" for item in first.json()["items"])
    assert first.json()["items"][0]["type"] == "analysis"
    assert first.json()["items"][0]["event"] == "Stop"
    assert cross_student.status_code == 403


def test_analysis_commit_cursor_delivers_late_lower_report_without_loss(
    tmp_path: Path,
) -> None:
    app, store = _analysis_app(tmp_path)
    lower_report = store.add_report(
        student_id="student-a",
        session_id="session-a",
        event="Stop",
        prompt="lower",
        transcript_path="",
        msg_count=1,
        tool_calls=0,
    )
    higher_report = store.add_report(
        student_id="student-a",
        session_id="session-a",
        event="Stop",
        prompt="higher",
        transcript_path="",
        msg_count=1,
        tool_calls=0,
    )
    higher_analysis = store.add_analysis(
        report_id=higher_report,
        student_id="student-a",
        session_id="session-a",
        result={"severity": "warn", "diagnosis": "higher finished first"},
    )

    with TestClient(app) as client:
        first = client.get(
            "/api/student/analyses",
            params={"after_analysis_id": 0},
            headers={"Authorization": "Bearer token-a"},
        )
        lower_analysis = store.add_analysis(
            report_id=lower_report,
            student_id="student-a",
            session_id="session-a",
            result={"severity": "warn", "diagnosis": "lower finished later"},
        )
        second = client.get(
            "/api/student/analyses",
            params={"after_analysis_id": first.json()["next_cursor"]},
            headers={"Authorization": "Bearer token-a"},
        )

    assert [item["report_id"] for item in first.json()["items"]] == [higher_report]
    assert first.json()["items"][0]["analysis_id"] == higher_analysis
    assert first.json()["cursor_kind"] == "analysis_id"
    assert [item["report_id"] for item in second.json()["items"]] == [lower_report]
    assert second.json()["items"][0]["analysis_id"] == lower_analysis
    assert second.json()["next_cursor"] == lower_analysis


def test_partial_or_zero_match_upload_never_writes_command_completion(tmp_path: Path) -> None:
    class Uploader:
        def __init__(self) -> None:
            self.outcomes = [
                UploadOutcome(0, 0, 0, 0, 0, "session_not_found"),
                UploadOutcome(1, 1, 0, 0, 1, "partial_failure"),
                UploadOutcome(1, 1, 1, 0, 0, ""),
            ]

        async def upload(self, **_kwargs) -> UploadOutcome:
            return self.outcomes.pop(0)

    async def scenario() -> None:
        uploader = Uploader()
        coordinator = StudentCoordinator(EventSpool(tmp_path), _Transport(), uploader)

        assert await coordinator.handle_command(_command()) is False
        assert await coordinator.handle_command(_command()) is False
        assert await coordinator.handle_command(_command()) is True
        assert await coordinator.handle_command(_command()) is False

    asyncio.run(scenario())


def test_headless_coordinator_does_not_mark_or_ack_unrendered_message(tmp_path: Path) -> None:
    async def scenario() -> None:
        transport = _Transport()
        spool = EventSpool(tmp_path)
        coordinator = StudentCoordinator(spool, transport, message_handler=None)
        payload = {
            "type": "mentor_message",
            "student_id": "student-a",
            "message_id": "message-1",
            "text": "please inspect the failing test",
        }

        assert await coordinator.handle_message(payload) is False
        assert transport.acked == []
        assert spool.receipt_ledger.status("student-a", "message-1") is None

    asyncio.run(scenario())


def test_async_transport_offloads_blocking_http_from_event_loop() -> None:
    started = threading.Event()
    release = threading.Event()

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return json.dumps({"status": "accepted", "report_id": 1}).encode()

    def blocking_opener(_request, timeout):
        assert timeout == 5.0
        started.set()
        assert release.wait(2.0)
        return Response()

    async def scenario() -> None:
        transport = StudentTransport(
            "https://copilot.example",
            student_id="student-a",
            opener=blocking_opener,
        )
        from copilot.student_core.models import HookEvent

        task = asyncio.create_task(
            transport.post_hook_async(HookEvent(event="Stop", student_id="student-a"))
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        heartbeat_started = time.monotonic()
        await asyncio.sleep(0)
        assert time.monotonic() - heartbeat_started < 0.1
        release.set()
        assert (await task).status_code == 202

    asyncio.run(scenario())


def test_analysis_handler_failure_does_not_advance_cursor(tmp_path: Path) -> None:
    attempts = 0

    async def handler(_payload) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("message store unavailable")

    async def scenario() -> None:
        coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            _Transport(),
            analysis_handler=handler,
        )
        payload = AnalysisEnvelope(
            student_id="student-a",
            session_id="session-a",
            report_id=7,
            event="Stop",
            result={"diagnosis": "retry me"},
            timestamp=1.0,
        ).to_dict()

        assert await coordinator.handle_analysis(payload) is False
        assert coordinator.analysis_cursor == 0
        assert await coordinator.handle_analysis(payload) is True
        assert coordinator.analysis_cursor == 7
        assert await coordinator.handle_analysis(payload) is False
        assert attempts == 2

    asyncio.run(scenario())


def _stop_event() -> HookEvent:
    return HookEvent(
        event="Stop",
        student_id="student-a",
        session_id="session-a",
        transcript_tail="tail",
    )


def test_stop_spool_ack_happens_only_after_durable_transcript_job(tmp_path: Path) -> None:
    order: list[str] = []

    class Transport(_Transport):
        async def post_hook_async(self, _event, *, event_id: str) -> Accepted:
            order.append("report")
            return Accepted(202, {"status": "accepted", "report_id": 41})

    class Queue:
        def enqueue(self, **values):
            order.append("job")
            assert values == {
                "event_id": "event-stop",
                "report_id": 41,
                "student_id": "student-a",
                "session_id": "session-a",
            }

    async def scenario() -> None:
        spool = EventSpool(tmp_path / "spool")
        spool.enqueue(_stop_event(), event_id="event-stop")
        real_ack = spool.ack

        def observed_ack(event_id: str) -> bool:
            order.append("spool_ack")
            return real_ack(event_id)

        spool.ack = observed_ack  # type: ignore[method-assign]
        coordinator = StudentCoordinator(
            spool,
            Transport(),
            transcript_queue=Queue(),
        )

        assert await coordinator.flush_spool_once() == 1
        assert order == ["report", "job", "spool_ack"]
        assert spool.pending() == []

    asyncio.run(scenario())


def test_stop_job_commit_failure_keeps_spool_for_idempotent_retry(tmp_path: Path) -> None:
    class Transport(_Transport):
        async def post_hook_async(self, _event, *, event_id: str) -> Accepted:
            return Accepted(202, {"status": "accepted", "report_id": 41})

    class BrokenQueue:
        def enqueue(self, **_values):
            raise OSError("disk full before commit")

    async def scenario() -> None:
        spool = EventSpool(tmp_path / "spool")
        spool.enqueue(_stop_event(), event_id="event-stop")
        coordinator = StudentCoordinator(
            spool,
            Transport(),
            transcript_queue=BrokenQueue(),
        )

        assert await coordinator.flush_spool_once() == 0
        assert [entry.event_id for entry in spool.pending()] == ["event-stop"]

    asyncio.run(scenario())


def test_transcript_job_queue_survives_restart_and_deletes_only_on_confirmation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "transcript-jobs.sqlite3"
    first = TranscriptUploadQueue(path)
    first.enqueue(
        event_id="event-1",
        report_id=12,
        student_id="student-a",
        session_id="session-a",
    )
    first.enqueue(
        event_id="event-1",
        report_id=12,
        student_id="student-a",
        session_id="session-a",
    )

    restarted = TranscriptUploadQueue(path)
    assert [job.event_id for job in restarted.pending()] == ["event-1"]
    restarted.record_outcome(
        "event-1",
        UploadOutcome(1, 1, 0, 0, 1, "network_failure"),
    )
    assert [job.event_id for job in restarted.pending()] == ["event-1"]
    # A caller cannot delete an intent that never durably pinned the body it
    # claims the server confirmed.
    assert restarted.record_outcome(
        "event-1",
        UploadOutcome(1, 1, 1, 0, 0),
    ) is False
    assert [job.event_id for job in restarted.pending()] == ["event-1"]
    content = '{"type":"message","role":"user","content":"fixed"}\n'
    restarted.pin_payload(
        "event-1",
        filtered_content=content,
        content_sha256=wb_upload.content_sha256(content),
    )
    restarted.record_outcome(
        "event-1",
        UploadOutcome(1, 1, 1, 0, 0),
    )
    assert restarted.pending() == []


def test_transcript_job_failure_uses_durable_bounded_retry_schedule(
    tmp_path: Path,
) -> None:
    now = [100.0]
    queue = TranscriptUploadQueue(
        tmp_path / "transcript-jobs.sqlite3",
        clock=lambda: now[0],
    )
    queue.enqueue(
        event_id="event-backoff",
        report_id=12,
        student_id="student-a",
        session_id="session-a",
    )

    assert [job.event_id for job in queue.ready()] == ["event-backoff"]
    queue.record_outcome(
        "event-backoff",
        UploadOutcome(1, 1, 0, 0, 1, "network_failure"),
    )

    persisted = queue.pending()[0]
    assert persisted.attempts == 1
    assert persisted.next_attempt_at == 102.0
    assert queue.ready() == []
    now[0] = 102.0
    assert [job.event_id for job in queue.ready()] == ["event-backoff"]


def test_transcript_job_event_collision_cannot_change_source(tmp_path: Path) -> None:
    queue = TranscriptUploadQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(
        event_id="event-1",
        report_id=12,
        student_id="student-a",
        session_id="session-a",
    )

    with pytest.raises(ValueError, match="collision"):
        queue.enqueue(
            event_id="event-1",
            report_id=13,
            student_id="student-a",
            session_id="session-a",
        )


def test_transcript_job_queue_migrates_legacy_row_and_pins_payload_idempotently(
    tmp_path: Path,
) -> None:
    path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE transcript_upload_jobs (
                   event_id TEXT PRIMARY KEY,
                   report_id INTEGER NOT NULL,
                   student_id TEXT NOT NULL,
                   session_id TEXT NOT NULL,
                   attempts INTEGER NOT NULL DEFAULT 0,
                   last_error TEXT NOT NULL DEFAULT '',
                   created_at REAL NOT NULL,
                   updated_at REAL NOT NULL
               )"""
        )
        connection.execute(
            """INSERT INTO transcript_upload_jobs
               VALUES('event-legacy', 12, 'student-a', 'session-a', 0, '', 1.0, 1.0)"""
        )

    content = '{"type":"message","role":"user","content":"fixed"}\n'
    sha = wb_upload.content_sha256(content)
    first = TranscriptUploadQueue(path)
    legacy = first.pending()[0]
    assert legacy.filtered_content == ""
    assert legacy.content_sha256 == ""
    pinned = first.pin_payload(
        "event-legacy",
        filtered_content=content,
        content_sha256=sha,
    )
    assert pinned.filtered_content == content
    assert pinned.content_sha256 == sha

    # Re-running the migration and pin operation must be harmless.
    restarted = TranscriptUploadQueue(path)
    same = restarted.pin_payload(
        "event-legacy",
        filtered_content=content,
        content_sha256=sha,
    )
    assert same == pinned
    with sqlite3.connect(path) as connection:
        columns = [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(transcript_upload_jobs)")
        ]
    assert columns.count("filtered_content") == 1
    assert columns.count("content_sha256") == 1


def test_transcript_job_payload_collision_fails_closed(tmp_path: Path) -> None:
    queue = TranscriptUploadQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(
        event_id="event-1",
        report_id=12,
        student_id="student-a",
        session_id="session-a",
    )
    original = '{"type":"message","role":"user","content":"original"}\n'
    changed = '{"type":"message","role":"user","content":"changed"}\n'
    queue.pin_payload(
        "event-1",
        filtered_content=original,
        content_sha256=wb_upload.content_sha256(original),
    )

    with pytest.raises(ValueError, match="payload collision"):
        queue.pin_payload(
            "event-1",
            filtered_content=changed,
            content_sha256=wb_upload.content_sha256(changed),
        )
    persisted = queue.pending()[0]
    assert persisted.filtered_content == original
    assert persisted.content_sha256 == wb_upload.content_sha256(original)


def test_windows_stop_retry_replays_pinned_payload_after_response_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "PRIVATE-TRANSCRIPT-CONTENT"
    first_content = "\n".join((
        json.dumps({"type": "ai-title", "aiTitle": "ignored"}),
        json.dumps({"type": "message", "role": "user", "content": secret}),
    )) + "\n"
    changed_content = json.dumps({
        "type": "message",
        "role": "user",
        "content": "mutated after first attempt",
    }) + "\n"

    class MutableAdapter(_DataAdapter):
        def __init__(self) -> None:
            super().__init__(["session-a"])
            self.read_count = 0

        def read_transcript(self, _session_id: str):
            self.read_count += 1
            content = first_content if self.read_count == 1 else changed_content

            class Read:
                failure = None

            result = Read()
            result.content = content
            return result

    captured: list[dict] = []

    def post(_server_url, session_id, payload, **_kwargs):
        captured.append(dict(payload))
        if len(captured) == 1:
            raise TimeoutError("response lost after server commit")
        if len(captured) == 2:
            return {"ok": True, "session_id": session_id, "sha": "0" * 64}
        return {
            "ok": True,
            "session_id": session_id,
            "sha": payload["sha"],
            "analysis_mode": "store_only",
        }

    monkeypatch.setattr(wb_upload, "post_transcript", post)
    monkeypatch.setattr(windows_runtime_module, "post_transcript", post, raising=False)
    adapter = MutableAdapter()
    runtime = WindowsStudentRuntime.build(
        base_url="https://copilot.example",
        student_id="student-a",
        token="token-a",
        spool_dir=tmp_path / "spool",
        state_dir=tmp_path / "state",
        data_adapter=adapter,
    )
    runtime.transcript_queue.enqueue(
        event_id="event-a",
        report_id=7,
        student_id="student-a",
        session_id="session-a",
    )
    expected_content = wb_upload.filter_message_jsonl_text(first_content)
    expected_sha = wb_upload.content_sha256(expected_content)

    assert asyncio.run(runtime.drain_transcript_jobs_once()) == 0
    pinned = runtime.transcript_queue.pending()[0]
    assert pinned.filtered_content == expected_content
    assert pinned.content_sha256 == expected_sha
    assert adapter.read_count == 1

    # A syntactically successful but mismatched confirmation remains pending.
    assert asyncio.run(runtime.drain_transcript_jobs_once()) == 0
    assert adapter.read_count == 1
    assert len(runtime.transcript_queue.pending()) == 1

    assert asyncio.run(runtime.drain_transcript_jobs_once()) == 1
    assert adapter.read_count == 1
    assert runtime.transcript_queue.pending() == []
    assert [payload["filtered_content"] for payload in captured] == [
        expected_content,
        expected_content,
        expected_content,
    ]
    assert all(payload["sha"] == expected_sha for payload in captured)
    assert all(payload["source_event_id"] == "event-a" for payload in captured)
    assert all(payload["source_report_id"] == 7 for payload in captured)
    assert secret not in caplog.text


def test_windows_stop_read_failure_retries_before_pinning(tmp_path: Path) -> None:
    content = '{"type":"message","role":"user","content":"later"}\n'

    class BusyThenReadableAdapter(_DataAdapter):
        def __init__(self) -> None:
            super().__init__(["session-a"])
            self.read_count = 0

        def read_transcript(self, _session_id: str):
            self.read_count += 1
            result = type("Read", (), {})()
            result.content = "" if self.read_count == 1 else content
            result.failure = (
                type("Failure", (), {"code": "busy"})()
                if self.read_count == 1
                else None
            )
            return result

    adapter = BusyThenReadableAdapter()
    runtime = WindowsStudentRuntime.build(
        base_url="https://copilot.example",
        student_id="student-a",
        token="token-a",
        spool_dir=tmp_path / "spool",
        state_dir=tmp_path / "state",
        data_adapter=adapter,
    )
    runtime.transcript_queue.enqueue(
        event_id="event-a",
        report_id=7,
        student_id="student-a",
        session_id="session-a",
    )

    assert asyncio.run(runtime.drain_transcript_jobs_once()) == 0
    unpinned = runtime.transcript_queue.pending()[0]
    assert unpinned.filtered_content == ""
    assert unpinned.content_sha256 == ""
    assert adapter.read_count == 1


def test_windows_stop_batch_reuses_one_snapshot_for_all_unpinned_jobs(
    tmp_path: Path,
) -> None:
    class BatchSnapshot:
        def __init__(self) -> None:
            self.read_sessions: list[str] = []

        def read_transcript(self, session_id: str):
            self.read_sessions.append(session_id)
            result = type("Read", (), {})()
            result.failure = None
            result.content = json.dumps({
                "type": "message",
                "role": "user",
                "content": f"help-{session_id}",
            }) + "\n"
            return result

    class SnapshotAdapter(_DataAdapter):
        def __init__(self) -> None:
            super().__init__(["session-a", "session-b"])
            self.snapshot_count = 0
            self.direct_read_count = 0
            self.snapshot = BatchSnapshot()

        def transcript_snapshot(self):
            self.snapshot_count += 1
            return self.snapshot

        def read_transcript(self, session_id: str):
            self.direct_read_count += 1
            return super().read_transcript(session_id)

    adapter = SnapshotAdapter()
    runtime = WindowsStudentRuntime.build(
        base_url="https://copilot.example",
        student_id="student-a",
        token="token-a",
        spool_dir=tmp_path / "spool",
        state_dir=tmp_path / "state",
        data_adapter=adapter,
    )
    for index, session_id in enumerate(("session-a", "session-b"), start=1):
        runtime.transcript_queue.enqueue(
            event_id=f"event-{index}",
            report_id=index,
            student_id="student-a",
            session_id=session_id,
        )
    uploaded: list[tuple[str, str]] = []

    async def upload_stop(job):
        uploaded.append((job.session_id, job.filtered_content))
        return UploadOutcome(1, 1, 1, 0, 0)

    runtime.uploader.upload_stop = upload_stop  # type: ignore[method-assign]

    assert asyncio.run(runtime.drain_transcript_jobs_once()) == 2
    assert adapter.snapshot_count == 1
    assert adapter.direct_read_count == 0
    assert adapter.snapshot.read_sessions == ["session-a", "session-b"]
    assert [session_id for session_id, _content in uploaded] == [
        "session-a",
        "session-b",
    ]
    assert "help-session-a" in uploaded[0][1]
    assert "help-session-b" in uploaded[1][1]


def test_analysis_catchup_drains_more_than_two_pages_without_duplicates(
    tmp_path: Path,
) -> None:
    items = [
        AnalysisEnvelope(
            student_id="student-a",
            session_id="session-a",
            report_id=report_id,
            event="Stop",
            result={"diagnosis": str(report_id)},
            timestamp=float(report_id),
        ).to_dict()
        for report_id in (2, 4, 6, 8, 10)
    ]

    class Transport(_Transport):
        async def get_recent_analyses_async(self, *, after_report_id: int, limit: int):
            remaining = [item for item in items if item["report_id"] > after_report_id]
            page = remaining[:limit]
            return {
                "items": page,
                "next_cursor": page[-1]["report_id"] if page else after_report_id,
                "has_more": len(remaining) > len(page),
            }

    async def scenario() -> None:
        rendered: list[int] = []
        coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            Transport(),
            analysis_handler=lambda payload: rendered.append(int(payload["report_id"])),
        )

        assert await coordinator.pull_analysis_catchup(page_limit=2, max_pages=8) == 5
        assert rendered == [2, 4, 6, 8, 10]
        assert coordinator.analysis_cursor == 10
        assert await coordinator.pull_analysis_catchup(page_limit=2, max_pages=8) == 0
        assert rendered == [2, 4, 6, 8, 10]

    asyncio.run(scenario())


def test_analysis_catchup_orders_by_commit_id_when_report_order_is_reversed(
    tmp_path: Path,
) -> None:
    items = [
        AnalysisEnvelope(
            analysis_id=1,
            student_id="student-a",
            session_id="session-a",
            report_id=20,
            event="Stop",
            result={"diagnosis": "newer report committed first"},
            timestamp=1.0,
        ).to_dict(),
        AnalysisEnvelope(
            analysis_id=2,
            student_id="student-a",
            session_id="session-a",
            report_id=10,
            event="Stop",
            result={"diagnosis": "older report committed later"},
            timestamp=2.0,
        ).to_dict(),
    ]

    class Transport(_Transport):
        async def get_recent_analyses_async(
            self,
            *,
            after_analysis_id: int,
            limit: int,
        ):
            remaining = [
                item for item in items if item["analysis_id"] > after_analysis_id
            ]
            return {
                "items": remaining[:limit],
                "next_cursor": (
                    remaining[-1]["analysis_id"] if remaining else after_analysis_id
                ),
                "has_more": False,
            }

    async def scenario() -> None:
        rendered: list[int] = []
        coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            Transport(),
            analysis_handler=lambda payload: rendered.append(int(payload["report_id"])),
        )

        assert await coordinator.pull_analysis_catchup() == 2
        assert rendered == [20, 10]
        assert coordinator.analysis_cursor == 2

    asyncio.run(scenario())


def test_analysis_catchup_handler_failure_keeps_server_cursor_retryable(
    tmp_path: Path,
) -> None:
    payload = AnalysisEnvelope(
        student_id="student-a",
        session_id="session-a",
        report_id=5,
        event="Stop",
        result={"diagnosis": "retry"},
        timestamp=1.0,
    ).to_dict()
    requested: list[int] = []

    class Transport(_Transport):
        async def get_recent_analyses_async(self, *, after_report_id: int, limit: int):
            requested.append(after_report_id)
            return {
                "items": [payload] if after_report_id < 5 else [],
                "next_cursor": 5 if after_report_id < 5 else after_report_id,
                "has_more": False,
            }

    attempts = 0

    async def handler(_payload) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("local analysis store busy")

    async def scenario() -> None:
        coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            Transport(),
            analysis_handler=handler,
        )

        assert await coordinator.pull_analysis_catchup(page_limit=2) == 0
        assert coordinator.analysis_cursor == 0
        assert await coordinator.pull_analysis_catchup(page_limit=2) == 1
        assert coordinator.analysis_cursor == 5
        assert requested == [0, 0]

    asyncio.run(scenario())


def test_windows_runtime_fails_closed_without_explicit_w0_profile(tmp_path: Path) -> None:
    with pytest.raises(WindowsRuntimeBlocked) as error:
        WindowsStudentRuntime.build(
            base_url="https://copilot.example",
            student_id="student-a",
            token="token-a",
            spool_dir=tmp_path / "spool",
            state_dir=tmp_path / "state",
            workbuddy_config_dir=tmp_path / "workbuddy",
            profile_path=tmp_path / "missing-w0.json",
        )

    assert error.value.code == "windows_profile_required"


def test_windows_runtime_wires_non_null_uploader_and_durable_job_drain(
    tmp_path: Path,
) -> None:
    runtime = WindowsStudentRuntime.build(
        base_url="https://copilot.example",
        student_id="student-a",
        token="token-a",
        spool_dir=tmp_path / "spool",
        state_dir=tmp_path / "state",
        data_adapter=_DataAdapter(["session-a"]),
    )
    runtime.transcript_queue.enqueue(
        event_id="event-a",
        report_id=7,
        student_id="student-a",
        session_id="session-a",
    )
    calls: list[tuple[str, int]] = []

    async def upload_stop(job):
        calls.append((job.event_id, job.report_id))
        return UploadOutcome(1, 1, 1, 0, 0)

    runtime.uploader.upload_stop = upload_stop  # type: ignore[method-assign]

    assert runtime.uploader is not None
    assert runtime.coordinator.uploader is runtime.uploader
    assert asyncio.run(runtime.drain_transcript_jobs_once()) == 1
    assert calls == [("event-a", 7)]
    assert runtime.transcript_queue.pending() == []


def test_windows_runtime_session_sync_uses_shared_transport_async_seam(
    tmp_path: Path,
) -> None:
    class Transport(_Transport):
        base_url = "https://copilot.example"

        def __init__(self):
            super().__init__()
            self.sessions: list[dict] = []

        async def post_sync_async(self, sessions):
            self.sessions = [dict(item) for item in sessions]
            return Accepted(200, {"ok": True, "synced": len(sessions)})

    transport = Transport()
    runtime = WindowsStudentRuntime.build(
        base_url=transport.base_url,
        student_id="student-a",
        token="token-a",
        spool_dir=tmp_path / "spool",
        state_dir=tmp_path / "state",
        data_adapter=_DataAdapter(["session-a"]),
        transport=transport,
    )

    result = asyncio.run(runtime.sync_sessions_once())

    assert result.status_code == 200
    assert transport.sessions[0]["session_id"] == "session-a"


def test_windows_runtime_session_discovery_does_not_block_ws_event_loop(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingAdapter(_DataAdapter):
        def list_sessions(self):
            started.set()
            assert release.wait(2)
            return super().list_sessions()

    class Transport(_Transport):
        async def post_sync_async(self, sessions):
            return Accepted(200, {"ok": True, "synced": len(sessions)})

    runtime = WindowsStudentRuntime.build(
        base_url="https://copilot.example",
        student_id="student-a",
        token="token-a",
        spool_dir=tmp_path / "spool",
        state_dir=tmp_path / "state",
        data_adapter=BlockingAdapter(["session-a"]),
        transport=Transport(),
    )

    async def scenario() -> None:
        task = asyncio.create_task(runtime.sync_sessions_once())
        assert await asyncio.to_thread(started.wait, 1)
        heartbeat = time.monotonic()
        await asyncio.sleep(0)
        assert time.monotonic() - heartbeat < 0.1
        release.set()
        assert (await task).status_code == 200

    asyncio.run(scenario())


def test_windows_maintenance_retries_failed_session_sync_without_hot_loop(
    tmp_path: Path,
) -> None:
    runtime = WindowsStudentRuntime.build(
        base_url="https://copilot.example",
        student_id="student-a",
        token="token-a",
        spool_dir=tmp_path / "spool",
        state_dir=tmp_path / "state",
        data_adapter=_DataAdapter(["session-a"]),
        transport=_Transport(),
    )
    sync_calls = 0
    drain_calls = 0

    async def sync_sessions_once():
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            raise OSError("offline")
        return Accepted(200, {"ok": True})

    async def drain_transcript_jobs_once(*, limit=16, due_only=False):
        nonlocal drain_calls
        assert limit == 16
        assert due_only is True
        drain_calls += 1
        if drain_calls == 2:
            runtime._stopping = True
        return 0

    runtime.sync_sessions_once = sync_sessions_once  # type: ignore[method-assign]
    runtime.drain_transcript_jobs_once = drain_transcript_jobs_once  # type: ignore[method-assign]

    asyncio.run(
        runtime._maintenance_loop(
            0.001,
            session_sync_interval=60.0,
            session_retry_interval=0.0,
        )
    )

    assert sync_calls == 2
    assert drain_calls == 2


def test_windows_runtime_stop_cancels_tasks_created_by_run(tmp_path: Path) -> None:
    runtime = WindowsStudentRuntime.build(
        base_url="https://copilot.example",
        student_id="student-a",
        token="token-a",
        spool_dir=tmp_path / "spool",
        state_dir=tmp_path / "state",
        data_adapter=_DataAdapter(["session-a"]),
        transport=_Transport(),
    )

    async def scenario() -> None:
        maintenance_started = asyncio.Event()
        never_finishes = asyncio.Event()

        async def blocked_maintenance(_interval: float) -> None:
            maintenance_started.set()
            await never_finishes.wait()

        runtime._maintenance_loop = blocked_maintenance  # type: ignore[method-assign]
        running = asyncio.create_task(runtime.run(maintenance_interval=60.0))
        await asyncio.wait_for(maintenance_started.wait(), timeout=0.2)

        await asyncio.wait_for(runtime.stop(), timeout=0.2)
        await asyncio.wait_for(running, timeout=0.2)

        assert running.done()

    asyncio.run(scenario())


def test_windows_runtime_stop_before_run_is_scheduled_cannot_revive_it(
    tmp_path: Path,
) -> None:
    runtime = WindowsStudentRuntime.build(
        base_url="https://copilot.example",
        student_id="student-a",
        token="token-a",
        spool_dir=tmp_path / "spool",
        state_dir=tmp_path / "state",
        data_adapter=_DataAdapter(["session-a"]),
        transport=_Transport(),
    )

    async def scenario() -> None:
        running = asyncio.create_task(runtime.run(maintenance_interval=60.0))
        # Deliberately do not yield after create_task: stop commits before run
        # receives its first scheduler turn.
        await runtime.stop()
        await asyncio.wait_for(running, timeout=0.2)

        assert runtime._stopping is True
        assert runtime._run_tasks is None

    asyncio.run(scenario())


def test_windows_analysis_store_persists_before_advancing_cursor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state" / "analyses.sqlite3"
    payload = AnalysisEnvelope(
        student_id="student-a",
        session_id="session-a",
        report_id=7,
        event="Stop",
        result={"diagnosis": "persist me"},
        timestamp=7.0,
    ).to_dict()
    store = WindowsAnalysisStore(path, student_id="student-a")

    assert store.persist(payload) is True
    assert store.cursor == 0
    assert [item["report_id"] for item in store.list_after()] == [7]
    assert store.advance_cursor(7) is True

    restarted = WindowsAnalysisStore(path, student_id="student-a")
    assert restarted.cursor == 7
    assert [item["report_id"] for item in restarted.list_after()] == [7]


def test_windows_analysis_store_rejects_cross_student_state_reuse(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state" / "analyses.sqlite3"
    WindowsAnalysisStore(path, student_id="student-a")

    with pytest.raises(ValueError, match="identity mismatch"):
        WindowsAnalysisStore(path, student_id="student-b")


def test_windows_runtime_restart_uses_durable_analysis_cursor_and_deduplicates(
    tmp_path: Path,
) -> None:
    items = [
        AnalysisEnvelope(
            student_id="student-a",
            session_id="session-a",
            report_id=report_id,
            event="Stop",
            result={"diagnosis": str(report_id)},
            timestamp=float(report_id),
        ).to_dict()
        for report_id in (2, 4, 6, 8, 10)
    ]
    requested: list[int] = []

    class Transport(_Transport):
        async def get_recent_analyses_async(self, *, after_report_id: int, limit: int):
            requested.append(after_report_id)
            remaining = [item for item in items if item["report_id"] > after_report_id]
            # Include a duplicate and reverse each page to prove local ordering
            # and idempotency do not depend on server frame order.
            page = list(reversed(remaining[:limit]))
            if page:
                page.append(dict(page[-1]))
            return {
                "items": page,
                "next_cursor": max(
                    (int(item["report_id"]) for item in page),
                    default=after_report_id,
                ),
                "has_more": len(remaining) > limit,
            }

    def build() -> WindowsStudentRuntime:
        return WindowsStudentRuntime.build(
            base_url="https://copilot.example",
            student_id="student-a",
            token="token-a",
            spool_dir=tmp_path / "spool",
            state_dir=tmp_path / "state",
            data_adapter=_DataAdapter(["session-a"]),
            transport=Transport(),
        )

    first = build()
    assert asyncio.run(
        first.coordinator.pull_analysis_catchup(page_limit=2, max_pages=8)
    ) == 5
    assert first.coordinator.analysis_cursor == 10
    assert first.analysis_store.cursor == 10
    assert [
        item["report_id"] for item in first.analysis_store.list_after()
    ] == [2, 4, 6, 8, 10]

    restarted = build()
    assert restarted.coordinator.analysis_cursor == 10
    assert asyncio.run(
        restarted.coordinator.pull_analysis_catchup(page_limit=2, max_pages=8)
    ) == 0
    assert [
        item["report_id"] for item in restarted.analysis_store.list_after()
    ] == [2, 4, 6, 8, 10]
    assert requested[-1] == 10


def test_windows_runtime_does_not_advance_cursor_when_analysis_handler_fails(
    tmp_path: Path,
) -> None:
    payload = AnalysisEnvelope(
        student_id="student-a",
        session_id="session-a",
        report_id=5,
        event="Stop",
        result={"diagnosis": "retry rendering"},
        timestamp=5.0,
    ).to_dict()

    class Transport(_Transport):
        async def get_recent_analyses_async(self, *, after_report_id: int, limit: int):
            return {
                "items": [payload] if after_report_id < 5 else [],
                "next_cursor": 5 if after_report_id < 5 else after_report_id,
                "has_more": False,
            }

    attempts = 0

    async def handler(_payload) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("native analysis view busy")

    runtime = WindowsStudentRuntime.build(
        base_url="https://copilot.example",
        student_id="student-a",
        token="token-a",
        spool_dir=tmp_path / "spool",
        state_dir=tmp_path / "state",
        data_adapter=_DataAdapter(["session-a"]),
        transport=Transport(),
        analysis_handler=handler,
    )

    assert asyncio.run(runtime.coordinator.pull_analysis_catchup(page_limit=2)) == 0
    assert runtime.coordinator.analysis_cursor == 0
    assert runtime.analysis_store.cursor == 0
    assert [item["report_id"] for item in runtime.analysis_store.list_after()] == [5]

    assert asyncio.run(runtime.coordinator.pull_analysis_catchup(page_limit=2)) == 1
    assert runtime.coordinator.analysis_cursor == 5
    assert runtime.analysis_store.cursor == 5
    assert attempts == 2


def test_failed_lower_analysis_blocks_higher_render_and_cursor(
    tmp_path: Path,
) -> None:
    attempts: list[int] = []
    fail_first = True

    async def handler(payload) -> None:
        nonlocal fail_first
        report_id = int(payload["report_id"])
        attempts.append(report_id)
        if report_id == 5 and fail_first:
            fail_first = False
            raise OSError("native analysis view busy")

    runtime = WindowsStudentRuntime.build(
        base_url="https://copilot.example",
        student_id="student-a",
        token="token-a",
        spool_dir=tmp_path / "spool",
        state_dir=tmp_path / "state",
        data_adapter=_DataAdapter(["session-a"]),
        transport=_Transport(),
        analysis_handler=handler,
    )
    lower = AnalysisEnvelope(
        student_id="student-a",
        session_id="session-a",
        report_id=5,
        event="Stop",
        result={"diagnosis": "first"},
        timestamp=5.0,
    ).to_dict()
    higher = AnalysisEnvelope(
        student_id="student-a",
        session_id="session-a",
        report_id=7,
        event="Stop",
        result={"diagnosis": "second"},
        timestamp=7.0,
    ).to_dict()

    assert asyncio.run(runtime.coordinator.handle_analysis(lower)) is False
    assert asyncio.run(runtime.coordinator.handle_analysis(higher)) is False
    assert runtime.coordinator.analysis_cursor == 0
    assert runtime.analysis_store.cursor == 0
    assert attempts == [5]

    assert asyncio.run(runtime.coordinator.handle_analysis(lower)) is True
    assert asyncio.run(runtime.coordinator.handle_analysis(higher)) is True
    assert runtime.coordinator.analysis_cursor == 7
    assert runtime.analysis_store.cursor == 7
    assert attempts == [5, 5, 7]
