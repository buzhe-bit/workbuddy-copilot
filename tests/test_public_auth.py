from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from copilot.app_context import AppContext
from copilot.connections import WSRegistry
from copilot.eventbus import EventBus
from copilot.service import create_app
from copilot.services import AnalysisService, MessageService, SessionQueryService
from copilot.store import Store


STUDENT_TOKEN = "student-secret"
MENTOR_TOKEN = "mentor-secret"


async def _fake_llm(config, snap, event, latest_prompt):
    return {
        "topic": "auth",
        "understanding": "medium",
        "severity": "info",
        "diagnosis": "ok",
        "suggestion": "ok",
        "is_technical": False,
        "ai_reply_summary": "",
    }


def _build_public_app(tmp_path, *, auth: dict | None = None):
    store = Store(tmp_path / "copilot.db")
    bus = EventBus()
    registry = WSRegistry(send_timeout=0.05)
    bus.subscribe(registry.handle_event)
    config = {
        "student_id": "server",
        "service": {"host": "0.0.0.0", "port": 8765},
        "store": {"db_path": str(tmp_path / "copilot.db")},
        "auth": auth or {
            "mode": "public",
            "student_tokens": {"student-a": STUDENT_TOKEN},
            "mentor_token": MENTOR_TOKEN,
        },
        "llm": {"enable_llm": False},
    }
    context = AppContext(
        config=config,
        store=store,
        analysis_svc=AnalysisService(store, _fake_llm, config, bus),
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=registry,
    )
    return create_app(context), store


def _student_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {STUDENT_TOKEN}"}


def _mentor_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {MENTOR_TOKEN}"}


def test_public_mode_requires_explicit_student_and_mentor_tokens(tmp_path):
    with pytest.raises(RuntimeError, match="student_token.*mentor_token|mentor_token.*student_token"):
        _build_public_app(tmp_path, auth={"mode": "public"})


def test_public_role_tokens_gate_student_and_mentor_http_routes(tmp_path):
    app, _store = _build_public_app(tmp_path)

    with TestClient(app) as client:
        no_token = client.post(
            "/api/sessions/sync",
            json={"student_id": "student-a", "sessions": []},
        )
        mentor_on_student = client.post(
            "/api/sessions/sync",
            headers=_mentor_headers(),
            json={"student_id": "student-a", "sessions": []},
        )
        student_ok = client.post(
            "/api/sessions/sync",
            headers=_student_headers(),
            json={"student_id": "student-a", "sessions": []},
        )
        student_on_mentor = client.get("/api/mentor/students", headers=_student_headers())
        mentor_ok = client.get("/api/mentor/students", headers=_mentor_headers())

    assert no_token.status_code == 401
    assert mentor_on_student.status_code == 401
    assert student_ok.status_code == 200
    assert student_on_mentor.status_code == 401
    assert mentor_ok.status_code == 200


def test_public_role_tokens_gate_websockets(tmp_path):
    app, _store = _build_public_app(tmp_path)

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as student_denied:
            with client.websocket_connect(f"/ws?student_id=student-a&token={MENTOR_TOKEN}"):
                pass
        assert student_denied.value.code == 1008

        with client.websocket_connect(f"/ws?student_id=student-a&token={STUDENT_TOKEN}") as ws:
            ws.send_text("ping")

        with pytest.raises(WebSocketDisconnect) as mentor_denied:
            with client.websocket_connect(f"/ws/mentor?token={STUDENT_TOKEN}"):
                pass
        assert mentor_denied.value.code == 1008

        with client.websocket_connect(f"/ws/mentor?token={MENTOR_TOKEN}") as ws:
            ws.send_text("ping")


def test_student_websocket_accepts_rest_auth_headers_and_keeps_query_compatibility(tmp_path):
    app, _store = _build_public_app(tmp_path)

    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws?student_id=student-a",
            headers={"Authorization": f"Bearer {STUDENT_TOKEN}"},
        ) as ws:
            ws.send_text("header-authenticated")

        with client.websocket_connect(
            "/ws?student_id=student-a",
            headers={"X-Copilot-Token": STUDENT_TOKEN},
        ) as ws:
            ws.send_text("x-header-authenticated")

        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(
                "/ws?student_id=student-c",
                headers={"Authorization": "Bearer wrong"},
            ):
                pass
        assert denied.value.code == 1008

        with client.websocket_connect(f"/ws?student_id=student-a&token={STUDENT_TOKEN}") as ws:
            ws.send_text("query-token-compatible")
