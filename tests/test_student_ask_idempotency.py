from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import urllib.error

import httpx
import pytest
from fastapi.testclient import TestClient

from copilot import service as service_module
from copilot.app_context import AppContext
from copilot.connections import WSRegistry
from copilot.eventbus import EventBus
from copilot.service import create_app
from copilot.services import AnalysisService, MessageService, SessionQueryService
from copilot.store import Store
from copilot.student_core.transport import (
    Accepted,
    PermanentTransportError,
    StudentAskNotFound,
    StudentTransport,
)


pytestmark = [pytest.mark.windows, pytest.mark.critical]


async def _unused_analysis(config, snap, event, latest_prompt):
    raise AssertionError("analysis LLM is not part of student ask recovery")


def _build_app(
    tmp_path,
    *,
    auth: dict | None = None,
    enable_llm: bool = True,
):
    store = Store(tmp_path / "copilot.db")
    bus = EventBus()
    registry = WSRegistry(send_timeout=0.05)
    bus.subscribe(registry.handle_event)
    events: list[dict] = []

    async def capture(payload: dict):
        events.append(payload)

    bus.subscribe(capture)
    config = {
        "student_id": "server-default",
        "service": {"host": "127.0.0.1", "port": 8765},
        "auth": auth or {"token": "secret"},
        "store": {"db_path": str(tmp_path / "copilot.db")},
        "llm": {"enable_llm": enable_llm, "timeout": 5},
    }
    context = AppContext(
        config=config,
        store=store,
        analysis_svc=AnalysisService(store, _unused_analysis, config, bus),
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=registry,
    )
    return create_app(context), store, events


def test_legacy_student_asks_migrate_idempotency_key_without_breaking_blank_rows(
    tmp_path,
):
    db_path = tmp_path / "legacy-asks.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """CREATE TABLE student_asks (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   student_id TEXT NOT NULL,
                   session_id TEXT,
                   question TEXT,
                   answer TEXT,
                   created_at REAL NOT NULL
               );
               INSERT INTO student_asks
                   (student_id, session_id, question, answer, created_at)
               VALUES ('legacy-student', NULL, 'old question', 'old answer', 1.0);"""
        )

    store = Store(db_path)
    legacy = store.list_student_asks("legacy-student")[0]
    assert legacy["client_request_id"] == ""

    # Legacy callers still have no key and may create more than one row.
    store.add_student_ask("legacy-student", None, "new one", "answer one")
    store.add_student_ask("legacy-student", None, "new two", "answer two")

    first, created = store.reserve_student_ask(
        student_id="legacy-student",
        session_id=None,
        question="retry-safe question",
        client_request_id="ask-request-1",
    )
    duplicate, duplicate_created = store.reserve_student_ask(
        student_id="legacy-student",
        session_id=None,
        question="retry-safe question",
        client_request_id="ask-request-1",
    )

    assert created is True
    assert duplicate_created is False
    assert duplicate["id"] == first["id"]
    assert duplicate["answer_status"] == "pending"
    with pytest.raises(ValueError, match="client_request_id payload conflict"):
        store.reserve_student_ask(
            student_id="legacy-student",
            session_id=None,
            question="changed payload",
            client_request_id="ask-request-1",
        )


@pytest.mark.asyncio
async def test_concurrent_retries_call_model_once_and_return_same_durable_ask(
    tmp_path,
    monkeypatch,
):
    model_started = asyncio.Event()
    release_model = asyncio.Event()
    model_calls = 0

    async def delayed_answer(config, question, context_messages):
        nonlocal model_calls
        model_calls += 1
        model_started.set()
        await release_model.wait()
        return "one durable answer"

    monkeypatch.setattr(service_module, "llm_answer_question", delayed_answer)
    app, store, events = _build_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer secret"}
    payload = {
        "student_id": "student-a",
        "question": "How do I recover this answer?",
        "client_request_id": "windows-ask-1",
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first_task = asyncio.create_task(
            client.post("/api/student/ask", headers=headers, json=payload)
        )
        await asyncio.wait_for(model_started.wait(), timeout=1)
        retry_responses = await asyncio.gather(*[
            client.post("/api/student/ask", headers=headers, json=payload)
            for _ in range(9)
        ])
        release_model.set()
        first_response = await asyncio.wait_for(first_task, timeout=1)
        recovered = await client.get(
            "/api/student/asks/by-client-request/windows-ask-1",
            headers=headers,
            params={"student_id": "student-a"},
        )

    assert first_response.status_code == 200
    assert all(response.status_code == 200 for response in retry_responses)
    assert {response.json()["ask_id"] for response in retry_responses} == {
        first_response.json()["ask_id"]
    }
    assert {response.json()["status"] for response in retry_responses} == {"pending"}
    assert model_calls == 1
    rows = store.list_student_asks("student-a")
    assert len(rows) == 1
    assert rows[0]["answer_status"] == "answered"
    assert recovered.status_code == 200
    assert recovered.json() == first_response.json()
    assert [event["type"] for event in events].count("student_ask") == 1


def test_same_key_changed_payload_conflicts_before_second_model_call(
    tmp_path,
    monkeypatch,
):
    model_calls = 0

    async def answer(config, question, context_messages):
        nonlocal model_calls
        model_calls += 1
        return "stable answer"

    monkeypatch.setattr(service_module, "llm_answer_question", answer)
    app, store, _events = _build_app(tmp_path)
    headers = {"Authorization": "Bearer secret"}

    with TestClient(app) as client:
        first = client.post(
            "/api/student/ask",
            headers=headers,
            json={
                "student_id": "student-a",
                "question": "original",
                "client_request_id": "windows-ask-conflict",
            },
        )
        conflict = client.post(
            "/api/student/ask",
            headers=headers,
            json={
                "student_id": "student-a",
                "question": "changed",
                "client_request_id": "windows-ask-conflict",
            },
        )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "client_request_id payload conflict"
    assert model_calls == 1
    assert len(store.list_student_asks("student-a")) == 1


def test_pending_retry_and_query_do_not_call_model_or_publish_terminal_event(
    tmp_path,
    monkeypatch,
):
    app, store, events = _build_app(tmp_path)
    row, created = store.reserve_student_ask(
        student_id="student-a",
        session_id=None,
        question="still running",
        client_request_id="windows-ask-pending",
    )
    assert created is True

    async def must_not_call_model(config, question, context_messages):
        raise AssertionError("a replay of a pending ask must not start another model call")

    monkeypatch.setattr(service_module, "llm_answer_question", must_not_call_model)
    headers = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        replay = client.post(
            "/api/student/ask",
            headers=headers,
            json={
                "student_id": "student-a",
                "question": "still running",
                "client_request_id": "windows-ask-pending",
            },
        )
        query = client.get(
            "/api/student/asks/by-client-request/windows-ask-pending",
            headers=headers,
            params={"student_id": "student-a"},
        )

    expected = {
        "ask_id": row["id"],
        "answer": "",
        "status": "pending",
        "error_code": "",
        "needs_attention": False,
    }
    assert replay.status_code == 200
    assert replay.json() == expected
    assert query.status_code == 200
    assert query.json() == expected
    assert events == []


def test_response_loss_then_process_restart_recovers_terminal_without_second_model(
    tmp_path,
    monkeypatch,
):
    model_calls = 0

    async def answer(config, question, context_messages):
        nonlocal model_calls
        model_calls += 1
        return "answer committed before the socket response was lost"

    monkeypatch.setattr(service_module, "llm_answer_question", answer)
    first_app, first_store, _first_events = _build_app(tmp_path)
    payload = {
        "student_id": "student-a",
        "question": "recover after restart",
        "client_request_id": "windows-ask-restart",
    }
    headers = {"Authorization": "Bearer secret"}
    with TestClient(first_app) as client:
        lost_response = client.post(
            "/api/student/ask",
            headers=headers,
            json=payload,
        )
    assert lost_response.status_code == 200
    expected = lost_response.json()

    async def must_not_call_again(config, question, context_messages):
        raise AssertionError("terminal recovery must not invoke the model again")

    monkeypatch.setattr(service_module, "llm_answer_question", must_not_call_again)
    restarted_app, restarted_store, restarted_events = _build_app(tmp_path)
    with TestClient(restarted_app) as client:
        recovered = client.get(
            "/api/student/asks/by-client-request/windows-ask-restart",
            headers=headers,
            params={"student_id": "student-a"},
        )
        replay = client.post(
            "/api/student/ask",
            headers=headers,
            json=payload,
        )

    assert model_calls == 1
    assert recovered.status_code == 200
    assert recovered.json() == expected
    assert replay.status_code == 200
    assert replay.json() == expected
    assert len(first_store.list_student_asks("student-a")) == 1
    assert len(restarted_store.list_student_asks("student-a")) == 1
    assert restarted_events == []


def test_query_is_scoped_to_mapped_student_token(tmp_path):
    app, store, _events = _build_app(
        tmp_path,
        auth={
            "mode": "public",
            "student_tokens": {
                "student-a": "token-a",
                "student-b": "token-b",
            },
            "mentor_token": "mentor-token",
        },
        enable_llm=False,
    )
    row, _created = store.reserve_student_ask(
        student_id="student-b",
        session_id=None,
        question="private pending question",
        client_request_id="private-request",
    )

    with TestClient(app) as client:
        own = client.get(
            "/api/student/asks/by-client-request/private-request",
            headers={"Authorization": "Bearer token-b"},
        )
        spoof = client.get(
            "/api/student/asks/by-client-request/private-request",
            headers={"Authorization": "Bearer token-a"},
            params={"student_id": "student-b"},
        )
        hidden = client.get(
            "/api/student/asks/by-client-request/private-request",
            headers={"Authorization": "Bearer token-a"},
        )

    assert own.status_code == 200
    assert own.json()["ask_id"] == row["id"]
    assert spoof.status_code in {401, 403}
    assert hidden.status_code == 404


class _Response:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self._raw = io.BytesIO(json.dumps(body).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return self._raw.read()


def test_student_transport_ask_query_feedback_and_async_wrappers_use_own_identity():
    captured: list[dict] = []

    def opener(request, timeout):
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        captured.append({
            "url": request.full_url,
            "method": request.get_method(),
            "body": body,
            "headers": dict(request.header_items()),
        })
        if request.full_url.endswith("/api/student/ask"):
            return _Response(200, {
                "ask_id": 7,
                "answer": "answer",
                "status": "answered",
                "needs_attention": False,
            })
        if "by-client-request" in request.full_url:
            return _Response(200, {
                "ask_id": 7,
                "answer": "answer",
                "status": "answered",
                "needs_attention": False,
            })
        return _Response(200, {"ask_id": 7, "feedback": "helpful"})

    transport = StudentTransport(
        "https://copilot.example",
        student_id="student-a",
        token="secret-token",
        opener=opener,
    )

    asked = transport.ask(
        "question",
        session_id="session-a",
        client_request_id="windows-request-7",
    )
    queried = transport.get_ask_by_client_request("windows-request-7")
    feedback = asyncio.run(
        transport.submit_ask_feedback_async(7, "helpful", note="solved")
    )

    assert asked == Accepted(200, {
        "ask_id": 7,
        "answer": "answer",
        "status": "answered",
        "needs_attention": False,
    })
    assert queried["ask_id"] == 7
    assert feedback.body["feedback"] == "helpful"
    assert captured[0]["body"] == {
        "student_id": "student-a",
        "question": "question",
        "session_id": "session-a",
        "client_request_id": "windows-request-7",
    }
    assert captured[1]["method"] == "GET"
    assert captured[1]["url"] == (
        "https://copilot.example/api/student/asks/by-client-request/"
        "windows-request-7?student_id=student-a"
    )
    assert captured[2]["body"] == {
        "student_id": "student-a",
        "feedback": "helpful",
        "note": "solved",
    }
    for request in captured:
        headers = {str(key).lower(): str(value) for key, value in request["headers"].items()}
        assert headers["authorization"] == "Bearer secret-token"


def test_student_transport_distinguishes_missing_recovery_row_from_auth_rejection():
    def missing(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            404,
            "missing",
            {},
            io.BytesIO(b'{"detail":"student ask not found"}'),
        )

    transport = StudentTransport(
        "https://copilot.example",
        student_id="student-a",
        token="secret-token",
        opener=missing,
    )
    with pytest.raises(StudentAskNotFound):
        transport.get_ask_by_client_request("request-missing")

    def unauthorized(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "denied",
            {},
            io.BytesIO(b'{"detail":"invalid token"}'),
        )

    denied = StudentTransport(
        "https://copilot.example",
        student_id="student-a",
        token="wrong-token",
        opener=unauthorized,
    )
    with pytest.raises(PermanentTransportError) as exc_info:
        denied.get_ask_by_client_request("request-private")
    assert not isinstance(exc_info.value, StudentAskNotFound)
