from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import copilot.app_context as app_context
from copilot.app_context import AppContext
from copilot.connections import WSRegistry
from copilot.eventbus import EventBus
from copilot.service import create_app
from copilot.services import AnalysisService, MessageService, SessionQueryService
from copilot.store import Store
from copilot.upload_service import UploadRequestService


STUDENT_A = "student-a"
STUDENT_B = "student-b"
TOKEN_A = "mapped-token-a"
TOKEN_B = "mapped-token-b"
SHARED_TOKEN = "shared-student-token"
MENTOR_TOKEN = "mentor-token"


@pytest.fixture(autouse=True)
def _auth_environment_is_config_driven(monkeypatch):
    for name in (
        "COPILOT_PUBLIC",
        "COPILOT_TOKEN",
        "COPILOT_STUDENT_TOKEN",
        "COPILOT_MENTOR_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


async def _fake_analysis(config, snap, event, latest_prompt):
    return {
        "topic": "identity",
        "understanding": "high",
        "severity": "info",
        "diagnosis": "ok",
        "suggestion": "continue",
        "is_technical": False,
        "ai_reply_summary": "",
    }


@dataclass(frozen=True)
class SeededIdentityApp:
    app: Any
    store: Store
    registry: WSRegistry
    upload_request_ids: dict[str, str]
    message_ids: dict[str, str]
    ask_ids: dict[str, int]


def _build_identity_app(
    tmp_path,
    *,
    mode: str = "public",
    allow_shared_student_token: bool | None = None,
    student_tokens: dict[str, str] | None = None,
) -> SeededIdentityApp:
    store = Store(tmp_path / "copilot.db")
    bus = EventBus()
    registry = WSRegistry(send_timeout=0.05)
    bus.subscribe(registry.handle_event)
    auth: dict[str, Any] = {
        "mode": mode,
        "student_token": SHARED_TOKEN,
        "student_tokens": student_tokens or {
            STUDENT_A: TOKEN_A,
            STUDENT_B: TOKEN_B,
        },
        "mentor_token": MENTOR_TOKEN,
    }
    if allow_shared_student_token is not None:
        auth["allow_shared_student_token"] = allow_shared_student_token
    config = {
        "student_id": "server-default-must-not-be-used-for-mapped-auth",
        "service": {"host": "127.0.0.1", "port": 8765},
        "store": {"db_path": str(tmp_path / "copilot.db")},
        "auth": auth,
        "llm": {"enable_llm": False},
    }
    upload_service = UploadRequestService(store)
    context = AppContext(
        config=config,
        store=store,
        analysis_svc=AnalysisService(store, _fake_analysis, config, bus),
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=registry,
        upload_svc=upload_service,
    )

    upload_request_ids: dict[str, str] = {}
    message_ids: dict[str, str] = {}
    ask_ids: dict[str, int] = {}
    for student_id in (STUDENT_A, STUDENT_B):
        session_id = f"session-{student_id}"
        store.replace_session_messages(
            session_id=session_id,
            student_id=student_id,
            turns=[{"seq": 1, "role": "user", "text": f"hello from {student_id}"}],
            raw=f"raw transcript for {student_id}",
            sha=f"seed-sha-{student_id}",
        )
        upload_request_ids[student_id] = upload_service.create(
            mentor_id="mentor",
            student_id=student_id,
            request_id=f"upload-{student_id}",
        )
        message_id = f"message-{student_id}"
        store.add_mentor_message(
            student_id=student_id,
            mentor_id="mentor",
            session_id=session_id,
            text=f"private message for {student_id}",
            message_id=message_id,
        )
        message_ids[student_id] = message_id
        ask_ids[student_id] = store.add_student_ask(
            student_id=student_id,
            session_id=session_id,
            question=f"seed question from {student_id}",
            answer="seed answer",
        )
        report_id = store.add_report(
            student_id=student_id,
            session_id=session_id,
            event="Stop",
            prompt=f"seed prompt from {student_id}",
            transcript_path="",
            msg_count=1,
            tool_calls=0,
        )
        store.add_analysis(
            report_id=report_id,
            student_id=student_id,
            session_id=session_id,
            result={
                "topic": f"topic-{student_id}",
                "understanding": "low",
                "severity": "warn",
                "diagnosis": f"diagnosis-{student_id}",
                "suggestion": "ask for help",
                "alert": f"alert-{student_id}",
            },
        )

    return SeededIdentityApp(
        app=create_app(context),
        store=store,
        registry=registry,
        upload_request_ids=upload_request_ids,
        message_ids=message_ids,
        ask_ids=ask_ids,
    )


def _mapped_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN_A}"}


def _shared_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SHARED_TOKEN}"}


def _body_with_student(student_id: str | None, **values: Any) -> dict[str, Any]:
    body = dict(values)
    if student_id is not None:
        body["student_id"] = student_id
    return body


def _query_with_student(student_id: str | None, **values: Any) -> dict[str, Any]:
    query = dict(values)
    if student_id is not None:
        query["student_id"] = student_id
    return query


def _resolved_student(student_id: str | None) -> str:
    return student_id or STUDENT_A


def _database_snapshot(store: Store) -> tuple[tuple[str, tuple[tuple[Any, ...], ...]], ...]:
    """Capture every logical table so a rejected request cannot mutate hidden state."""
    with store._conn() as conn:
        table_names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        snapshot = []
        for table_name in table_names:
            rows = [tuple(row) for row in conn.execute(f'SELECT * FROM "{table_name}"')]
            snapshot.append((table_name, tuple(sorted(rows, key=repr))))
    return tuple(snapshot)


StudentRouteCall = Callable[[TestClient, SeededIdentityApp, str | None], Any]


@dataclass(frozen=True)
class StudentRouteCase:
    name: str
    call: StudentRouteCall
    success_codes: frozenset[int] = frozenset({200})


def _report(client, seeded, student_id):
    resolved = _resolved_student(student_id)
    return client.post(
        "/report",
        headers=_mapped_headers(),
        json=_body_with_student(
            student_id,
            session_id=f"session-{resolved}",
            event="SessionStart",
            event_id=f"identity-report-{resolved}",
            prompt=f"report from {resolved}",
        ),
    )


def _recent(client, seeded, student_id):
    return client.get(
        "/recent",
        headers=_mapped_headers(),
        params=_query_with_student(student_id, limit=20),
    )


def _sync_sessions(client, seeded, student_id):
    resolved = _resolved_student(student_id)
    return client.post(
        "/api/sessions/sync",
        headers=_mapped_headers(),
        json=_body_with_student(
            student_id,
            sessions=[{
                "session_id": f"synced-{resolved}",
                "title": f"synced by {resolved}",
                "work_dir": f"/work/{resolved}",
            }],
        ),
    )


def _upload_transcript(client, seeded, student_id):
    resolved = _resolved_student(student_id)
    return client.post(
        f"/api/student/sessions/session-{resolved}/transcript",
        headers=_mapped_headers(),
        json=_body_with_student(
            student_id,
            filtered_content=[
                {"role": "user", "content": f"new transcript from {resolved}"},
            ],
            sha=f"new-sha-{resolved}",
        ),
    )


def _known_transcripts(client, seeded, student_id):
    return client.get(
        "/api/transcripts/known",
        headers=_mapped_headers(),
        params=_query_with_student(student_id),
    )


def _list_upload_requests(client, seeded, student_id):
    return client.get(
        "/api/student/upload-requests",
        headers=_mapped_headers(),
        params=_query_with_student(student_id, status="all"),
    )


def _update_upload_status(client, seeded, student_id):
    resolved = _resolved_student(student_id)
    return client.post(
        f"/api/student/upload-requests/{seeded.upload_request_ids[resolved]}/status",
        headers=_mapped_headers(),
        json=_body_with_student(student_id, status="running"),
    )


def _sessions(client, seeded, student_id):
    return client.get(
        "/sessions",
        headers=_mapped_headers(),
        params=_query_with_student(student_id, limit=20),
    )


def _current_session(client, seeded, student_id):
    return client.get(
        "/current_session",
        headers=_mapped_headers(),
        params=_query_with_student(student_id),
    )


def _alerts(client, seeded, student_id):
    return client.get(
        "/alerts/unread",
        headers=_mapped_headers(),
        params=_query_with_student(student_id, since=0),
    )


def _messages(client, seeded, student_id):
    return client.get(
        "/api/student/messages",
        headers=_mapped_headers(),
        params=_query_with_student(student_id, since=0),
    )


def _pending_receipts(client, seeded, student_id):
    return client.get(
        "/api/student/messages/pending-receipts",
        headers=_mapped_headers(),
        params=_query_with_student(student_id, after_id=0),
    )


def _ack_message(client, seeded, student_id):
    resolved = _resolved_student(student_id)
    return client.post(
        "/api/student/messages/ack",
        headers=_mapped_headers(),
        json=_body_with_student(
            student_id,
            message_id=seeded.message_ids[resolved],
        ),
    )


def _ask(client, seeded, student_id):
    resolved = _resolved_student(student_id)
    return client.post(
        "/api/student/ask",
        headers=_mapped_headers(),
        json=_body_with_student(
            student_id,
            session_id=f"session-{resolved}",
            question=f"identity question from {resolved}",
        ),
    )


def _feedback(client, seeded, student_id):
    resolved = _resolved_student(student_id)
    return client.post(
        f"/api/student/asks/{seeded.ask_ids[resolved]}/feedback",
        headers=_mapped_headers(),
        json=_body_with_student(
            student_id,
            feedback="helpful",
            note=f"feedback from {resolved}",
        ),
    )


STUDENT_ROUTE_INVENTORY = (
    StudentRouteCase("report", _report, frozenset({202})),
    StudentRouteCase("recent-analysis", _recent),
    StudentRouteCase("session-sync", _sync_sessions),
    StudentRouteCase("transcript-upload", _upload_transcript),
    StudentRouteCase("known-transcript-shas", _known_transcripts),
    StudentRouteCase("upload-request-list", _list_upload_requests),
    StudentRouteCase("upload-request-status", _update_upload_status),
    StudentRouteCase("sessions", _sessions),
    StudentRouteCase("current-session", _current_session),
    StudentRouteCase("alerts", _alerts),
    StudentRouteCase("message-catchup", _messages),
    StudentRouteCase("pending-message-receipts", _pending_receipts),
    StudentRouteCase("message-ack", _ack_message),
    StudentRouteCase("student-ask", _ask),
    StudentRouteCase("student-ask-feedback", _feedback),
)


def _assert_response_is_scoped_to_student_a(
    case: StudentRouteCase,
    response,
    seeded: SeededIdentityApp,
) -> None:
    body = response.json()
    if case.name == "report":
        with seeded.store._conn() as conn:
            row = conn.execute(
                "SELECT student_id FROM reports WHERE event_id = ?",
                (f"identity-report-{STUDENT_A}",),
            ).fetchone()
        assert row is not None and row["student_id"] == STUDENT_A
    elif case.name == "recent-analysis":
        assert {item["student_id"] for item in body["items"]} == {STUDENT_A}
    elif case.name == "session-sync":
        with seeded.store._conn() as conn:
            row = conn.execute(
                "SELECT student_id FROM sessions WHERE session_id = ?",
                (f"synced-{STUDENT_A}",),
            ).fetchone()
        assert row is not None and row["student_id"] == STUDENT_A
    elif case.name == "transcript-upload":
        assert body["session_id"] == f"session-{STUDENT_A}"
        assert seeded.store.get_known_session_shas(STUDENT_A)[f"session-{STUDENT_A}"][
            "sha"
        ] == f"new-sha-{STUDENT_A}"
    elif case.name == "known-transcript-shas":
        assert set(body) == {f"session-{STUDENT_A}"}
    elif case.name == "upload-request-list":
        assert {item["student_id"] for item in body["items"]} == {STUDENT_A}
    elif case.name == "upload-request-status":
        assert seeded.store.get_upload_request(
            seeded.upload_request_ids[STUDENT_A]
        )["transfer_status"] == "running"
    elif case.name == "sessions":
        assert {item["session_id"] for item in body["items"]} == {
            f"session-{STUDENT_A}"
        }
    elif case.name == "current-session":
        assert body["session_id"] == f"session-{STUDENT_A}"
    elif case.name == "alerts":
        assert {item["student_id"] for item in body["items"]} == {STUDENT_A}
    elif case.name in {"message-catchup", "pending-message-receipts"}:
        assert {item["student_id"] for item in body["items"]} == {STUDENT_A}
    elif case.name == "message-ack":
        rows = seeded.store.list_messages_since(STUDENT_A, 0)
        own = next(row for row in rows if row["message_id"] == seeded.message_ids[STUDENT_A])
        assert own["delivered_at"] is not None
    elif case.name == "student-ask":
        assert any(
            row["student_id"] == STUDENT_A
            and row["question"] == f"identity question from {STUDENT_A}"
            for row in seeded.store.list_student_asks(STUDENT_A)
        )
    elif case.name == "student-ask-feedback":
        own = seeded.store.get_student_ask(seeded.ask_ids[STUDENT_A])
        assert own is not None and own["feedback"] == "helpful"
    else:  # pragma: no cover - inventory additions must declare their ownership proof
        raise AssertionError(f"missing ownership assertion for {case.name}")


def test_student_principal_dependency_contract_is_exposed():
    principal_type = getattr(app_context, "StudentPrincipal", None)
    dependency = getattr(app_context, "require_student_principal", None)

    assert principal_type is not None, "StudentPrincipal must be part of the auth boundary"
    assert callable(dependency), "require_student_principal must be a FastAPI dependency"
    principal = principal_type(student_id=STUDENT_A, auth_mode="mapped")
    assert principal.student_id == STUDENT_A
    assert principal.auth_mode == "mapped"


@pytest.mark.parametrize("case", STUDENT_ROUTE_INVENTORY, ids=lambda case: case.name)
def test_mapped_token_routes_reject_spoof_then_accept_and_derive_own_identity(
    tmp_path,
    case: StudentRouteCase,
):
    seeded = _build_identity_app(tmp_path)

    with TestClient(seeded.app) as client:
        before_spoof = _database_snapshot(seeded.store)
        spoofed = case.call(client, seeded, STUDENT_B)
        after_spoof = _database_snapshot(seeded.store)

        assert spoofed.status_code in {401, 403}
        assert after_spoof == before_spoof, (
            f"rejected {case.name} request changed persistent state"
        )

        own = case.call(client, seeded, STUDENT_A)
        assert own.status_code in case.success_codes, own.text

        derived = case.call(client, seeded, None)
        assert derived.status_code in case.success_codes, derived.text
        _assert_response_is_scoped_to_student_a(case, derived, seeded)


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [
        pytest.param("local", 200, id="local-compatible"),
        pytest.param("demo", 200, id="demo-compatible"),
        pytest.param("public", 401, id="public-default-deny"),
        pytest.param("production", 401, id="production-default-deny"),
        pytest.param("prod", 401, id="prod-default-deny"),
        pytest.param("pilot", 401, id="pilot-default-deny"),
        pytest.param("staging", 401, id="staging-default-deny"),
        pytest.param("publci", 401, id="unknown-mode-default-deny"),
    ],
)
def test_shared_student_token_defaults_are_environment_safe(
    tmp_path,
    mode: str,
    expected_status: int,
):
    seeded = _build_identity_app(tmp_path, mode=mode)

    with TestClient(seeded.app) as client:
        response = client.post(
            "/api/sessions/sync",
            headers=_shared_headers(),
            json={"student_id": STUDENT_A, "sessions": []},
        )

    assert response.status_code == expected_status


@pytest.mark.parametrize(
    "mode",
    ["public", "production", "prod", "pilot", "staging", "publci"],
)
def test_non_local_modes_reject_shared_token_even_with_explicit_opt_in(
    tmp_path,
    mode: str,
):
    seeded = _build_identity_app(
        tmp_path,
        mode=mode,
        allow_shared_student_token=True,
    )

    with TestClient(seeded.app) as client:
        response = client.post(
            "/api/sessions/sync",
            headers=_shared_headers(),
            json={"student_id": STUDENT_A, "sessions": []},
        )

    assert response.status_code == 401


def test_duplicate_mapped_tokens_are_rejected_at_startup(tmp_path):
    with pytest.raises(RuntimeError, match="duplicate"):
        _build_identity_app(
            tmp_path,
            student_tokens={
                STUDENT_A: "duplicate-token",
                STUDENT_B: "duplicate-token",
            },
        )


def test_local_shared_token_can_be_explicitly_disabled(tmp_path):
    seeded = _build_identity_app(
        tmp_path,
        mode="local",
        allow_shared_student_token=False,
    )

    with TestClient(seeded.app) as client:
        response = client.post(
            "/api/sessions/sync",
            headers=_shared_headers(),
            json={"student_id": STUDENT_A, "sessions": []},
        )

    assert response.status_code == 401


def test_mapped_student_websocket_rejects_spoof_and_derives_identity_when_omitted(
    tmp_path,
):
    seeded = _build_identity_app(tmp_path)

    with TestClient(seeded.app) as client:
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(
                f"/ws?student_id={STUDENT_B}&token={TOKEN_A}"
            ):
                pass
        assert denied.value.code == 1008
        assert STUDENT_B not in seeded.registry.floats

        with client.websocket_connect(
            "/ws",
            headers={"Authorization": f"Bearer {TOKEN_A}"},
        ) as ws:
            assert STUDENT_A in seeded.registry.floats
            assert STUDENT_B not in seeded.registry.floats
            ws.send_text("mapped principal owns this socket")

    assert not seeded.registry.floats
