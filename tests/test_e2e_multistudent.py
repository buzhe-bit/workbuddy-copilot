from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from copilot.app_context import AppContext
from copilot.connections import WSRegistry
from copilot.eventbus import EventBus
from copilot.service import create_app
from copilot.services import AnalysisService, MessageService, SessionQueryService
from copilot.store import Store


TOKEN = "phase4-token"


def _line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


async def _fake_llm(config, snap, event, latest_prompt):
    assert event == "Stop"
    key = "a" if "student-a" in latest_prompt else "b"
    student_id = f"student-{key}"
    assert snap.session_id == f"session-{key}"
    assert snap.ai_title == f"Student {key.upper()} Session"
    assert snap.cwd == f"/work/student-{key}"
    assert [m.role for m in snap.messages] == ["user", "assistant"]
    assert snap.messages[0].text == f"{student_id} user question"
    assert snap.messages[1].text == f"{student_id} assistant answer"
    return {
        "topic": f"topic-{key}",
        "understanding": "low" if key == "a" else "high",
        "off_topic": False,
        "stuck_at": f"stuck-{key}",
        "is_technical": True,
        "severity": "warn" if key == "a" else "info",
        "diagnosis": f"diagnosis-{key}",
        "suggestion": f"suggestion-{key}",
        "progress": f"progress-{key}",
        "guidance": f"guidance-{key}",
        "alert": f"alert-{key}" if key == "a" else "",
        "ai_reply_summary": f"summary-{key}",
    }


def _build_test_app(tmp_path):
    store = Store(tmp_path / "copilot.db")
    bus = EventBus()
    registry = WSRegistry(send_timeout=0.05)
    bus.subscribe(registry.handle_event)
    config = {
        "student_id": "mentor-host",
        "student_name": "Mentor Host",
        "auth": {"token": TOKEN},
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


def _auth_headers() -> dict[str, str]:
    return {"X-Copilot-Token": TOKEN}


def _transcript(student_id: str, session_id: str, title: str, cwd: str) -> str:
    return (
        _line({"type": "ai-title", "aiTitle": title})
        + _line({
            "type": "message",
            "role": "user",
            "content": f"{student_id} user question",
            "sessionId": session_id,
            "cwd": cwd,
        })
        + _line({
            "type": "message",
            "role": "assistant",
            "content": f"{student_id} assistant answer",
        })
    )


def _post_student_flow(
    client: TestClient,
    *,
    student_id: str,
    session_id: str,
    title: str,
    cwd: str,
) -> None:
    transcript = _transcript(student_id, session_id, title, cwd)
    submit = client.post(
        "/report",
        headers=_auth_headers(),
        json={
            "student_id": student_id,
            "session_id": session_id,
            "event": "UserPromptSubmit",
            "prompt": f"{student_id} submit prompt",
            "transcript_tail": transcript,
            "transcript_full": transcript,
            "cwd": cwd,
        },
    )
    assert submit.status_code == 202
    assert submit.json()["prompt_id"] > 0

    stop = client.post(
        "/report",
        headers=_auth_headers(),
        json={
            "student_id": student_id,
            "session_id": session_id,
            "event": "Stop",
            "prompt": f"{student_id} stop prompt",
            "transcript_tail": transcript,
            "transcript_full": transcript,
            "cwd": cwd,
        },
    )
    assert stop.status_code == 202
    assert stop.json()["report_id"] > 0


def _items_by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["student_id"]: item for item in items}


def _count_rows(store: Store, table: str, student_id: str) -> int:
    with store._conn() as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE student_id = ?",
            (student_id,),
        ).fetchone()[0]


def test_report_flow_keeps_students_sessions_and_delete_cascade_isolated(tmp_path):
    app, store = _build_test_app(tmp_path)
    with TestClient(app) as client:
        _post_student_flow(
            client,
            student_id="student-a",
            session_id="session-a",
            title="Student A Session",
            cwd="/work/student-a",
        )
        _post_student_flow(
            client,
            student_id="student-b",
            session_id="session-b",
            title="Student B Session",
            cwd="/work/student-b",
        )

        students = client.get("/api/mentor/students", headers=_auth_headers())
        assert students.status_code == 200
        overview = _items_by_id(students.json()["items"])
        assert set(overview) == {"student-a", "student-b"}
        assert overview["student-a"]["analysis_count"] == 1
        assert overview["student-a"]["session_count"] == 1
        assert overview["student-a"]["last_topic"] == "topic-a"
        assert overview["student-b"]["analysis_count"] == 1
        assert overview["student-b"]["session_count"] == 1
        assert overview["student-b"]["last_topic"] == "topic-b"

        sessions_a = client.get(
            "/api/mentor/students/student-a/sessions",
            headers=_auth_headers(),
        ).json()["items"]
        sessions_b = client.get(
            "/api/mentor/students/student-b/sessions",
            headers=_auth_headers(),
        ).json()["items"]
        assert [item["session_id"] for item in sessions_a] == ["session-a"]
        assert [item["session_id"] for item in sessions_b] == ["session-b"]
        assert sessions_a[0]["session_title"] == "Student A Session"
        assert sessions_b[0]["session_title"] == "Student B Session"
        assert sessions_a[0]["last_topic"] == "topic-a"
        assert sessions_b[0]["last_topic"] == "topic-b"

        timeline_a = client.get(
            "/api/mentor/sessions/session-a/timeline",
            headers=_auth_headers(),
        ).json()["items"]
        timeline_b = client.get(
            "/api/mentor/sessions/session-b/timeline",
            headers=_auth_headers(),
        ).json()["items"]
        assert [item["type"] for item in timeline_a] == [
            "prompt",
            "prompt",
            "ai_summary",
            "analysis",
        ]
        assert [item["type"] for item in timeline_b] == [
            "prompt",
            "prompt",
            "ai_summary",
            "analysis",
        ]
        assert any(item["content"] == "diagnosis-a" for item in timeline_a)
        assert any(item["content"] == "summary-a" for item in timeline_a)
        assert all("student-b" not in item["content"] for item in timeline_a)
        assert all(item["content"] not in {"diagnosis-b", "summary-b"} for item in timeline_a)
        assert any(item["content"] == "diagnosis-b" for item in timeline_b)

        with store._conn() as conn:
            session_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT session_id, student_id FROM sessions ORDER BY session_id"
                ).fetchall()
            ]
        assert session_rows == [
            {"session_id": "session-a", "student_id": "student-a"},
            {"session_id": "session-b", "student_id": "student-b"},
        ]

        deleted = client.delete(
            "/api/admin/students/student-a",
            headers=_auth_headers(),
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] == {
            "attention_items": 1,
            "analyses": 1,
            "ai_summaries": 1,
            "prompts": 2,
            "raw_transcripts": 1,
            "mentor_messages": 0,
            "reports": 2,
            "sessions": 1,
            "students": 1,
        }

        for table in [
            "attention_items",
            "system_failure_occurrences",
            "analyses",
            "ai_summaries",
            "prompts",
            "raw_transcripts",
            "reports",
            "sessions",
            "students",
        ]:
            assert _count_rows(store, table, "student-a") == 0
        assert _count_rows(store, "analyses", "student-b") == 1
        assert _count_rows(store, "ai_summaries", "student-b") == 1
        assert _count_rows(store, "prompts", "student-b") == 2
        assert _count_rows(store, "raw_transcripts", "student-b") == 1
        assert _count_rows(store, "reports", "student-b") == 2
        assert _count_rows(store, "sessions", "student-b") == 1
        assert _count_rows(store, "students", "student-b") == 1

        remaining = client.get("/api/mentor/students", headers=_auth_headers()).json()["items"]
        assert [item["student_id"] for item in remaining] == ["student-b"]
        remaining_timeline = client.get(
            "/api/mentor/sessions/session-b/timeline",
            headers=_auth_headers(),
        ).json()["items"]
        assert any(item["content"] == "diagnosis-b" for item in remaining_timeline)
