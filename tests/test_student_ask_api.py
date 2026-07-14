from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from copilot import service as service_module
from copilot.app_context import AppContext
from copilot.connections import WSRegistry
from copilot.eventbus import EventBus
from copilot.service import create_app
from copilot.models import QuestionAnswerOutcome
from copilot.services import AnalysisService, MessageService, SessionQueryService
from copilot.store import Store


async def _unused_llm(config, snap, event, latest_prompt):
    raise AssertionError("analysis LLM is not part of student ask API")


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _build_app(tmp_path, *, llm_config: dict | None = None):
    store = Store(tmp_path / "copilot.db")
    bus = EventBus()
    registry = WSRegistry(send_timeout=0.05)
    bus.subscribe(registry.handle_event)
    events: list[dict] = []

    async def capture_event(payload: dict):
        events.append(payload)

    bus.subscribe(capture_event)
    config = {
        "student_id": "server",
        "service": {"host": "127.0.0.1", "port": 8765},
        "auth": {"token": "secret"},
        "store": {"db_path": str(tmp_path / "copilot.db")},
        "llm": llm_config or {"enable_llm": True, "timeout": 5},
    }
    context = AppContext(
        config=config,
        store=store,
        analysis_svc=AnalysisService(store, _unused_llm, config, bus),
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=registry,
    )
    return create_app(context), store, events


def test_student_ask_uses_llm_context_persists_and_publishes_event(tmp_path, monkeypatch):
    captured: dict = {}

    async def fake_answer_question(config, question, context_messages):
        captured["question"] = question
        captured["context_messages"] = context_messages
        return "固定技术助教答案"

    monkeypatch.setattr(service_module, "llm_answer_question", fake_answer_question, raising=False)
    app, store, events = _build_app(tmp_path)
    store.upsert_student("stu-1", "Alice")
    store.upsert_session("sess-1", "stu-1", "/work/alice", "循环调试")
    store.add_raw_transcript(
        "sess-1",
        "stu-1",
        _line({
            "type": "message",
            "role": "user",
            "content": "我的 for 循环最后一个元素没处理到",
            "sessionId": "sess-1",
        })
        + _line({
            "type": "message",
            "role": "assistant",
            "content": "检查 range 的结束边界是否少了 1。",
            "sessionId": "sess-1",
        }),
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/student/ask",
            json={
                "student_id": "stu-1",
                "session_id": "sess-1",
                "question": "我应该怎么验证边界？",
            },
            headers={"Authorization": "Bearer secret"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ask_id"] > 0
    assert body["answer"] == "固定技术助教答案"
    assert body["status"] == "answered"
    assert body["needs_attention"] is False
    assert captured["question"] == "我应该怎么验证边界？"
    assert any("for 循环" in msg["content"] for msg in captured["context_messages"])

    asks = store.list_student_asks("stu-1", "sess-1")
    assert len(asks) == 1
    assert asks[0]["question"] == "我应该怎么验证边界？"
    assert asks[0]["answer"] == "固定技术助教答案"
    assert asks[0]["answer_status"] == "answered"
    assert asks[0]["error_code"] == ""
    assert any(event.get("type") == "student_ask" for event in events)


def test_student_ask_llm_disabled_falls_back_and_still_persists(tmp_path):
    app, store, _events = _build_app(tmp_path, llm_config={"enable_llm": False})

    with TestClient(app) as client:
        resp = client.post(
            "/api/student/ask",
            json={"student_id": "stu-1", "question": "没有 LLM 会怎样？"},
            headers={"X-Copilot-Token": "secret"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ask_id"] > 0
    assert "LLM" in body["answer"]
    assert body["status"] == "degraded"
    assert body["needs_attention"] is True
    stored = store.list_student_asks("stu-1")[0]
    assert stored["answer"] == body["answer"]
    assert stored["answer_status"] == "degraded"
    assert stored["error_code"] == "llm_disabled"


def test_student_ask_rejects_blank_question(tmp_path):
    app, _store, _events = _build_app(tmp_path)

    with TestClient(app) as client:
        resp = client.post(
            "/api/student/ask",
            json={"student_id": "stu-1", "question": "   "},
            headers={"Authorization": "Bearer secret"},
        )

    assert resp.status_code == 400


def test_configured_provider_failure_persists_failed_attention_status(tmp_path, monkeypatch):
    async def failed_answer(config, question, context_messages):
        return QuestionAnswerOutcome(
            status="failed",
            answer="安全降级回答",
            error_code="llm_timeout",
        )

    monkeypatch.setattr(service_module, "llm_answer_question", failed_answer)
    app, store, events = _build_app(tmp_path)

    with TestClient(app) as client:
        resp = client.post(
            "/api/student/ask",
            json={"student_id": "stu-1", "question": "为什么超时？"},
            headers={"Authorization": "Bearer secret"},
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "ask_id": resp.json()["ask_id"],
        "answer": "安全降级回答",
        "status": "failed",
        "needs_attention": True,
    }
    stored = store.list_student_asks("stu-1")[0]
    assert stored["answer_status"] == "failed"
    assert stored["error_code"] == "llm_timeout"
    assert [event["type"] for event in events] == ["student_ask"]


def test_outer_question_timeout_is_failed_without_dropping_safe_answer(tmp_path, monkeypatch):
    async def never_finishes(config, question, context_messages):
        await __import__("asyncio").sleep(60)

    monkeypatch.setattr(service_module, "llm_answer_question", never_finishes)
    monkeypatch.setattr(service_module, "_student_ask_timeout", lambda _config: 0.001)
    app, store, _events = _build_app(tmp_path)

    with TestClient(app) as client:
        resp = client.post(
            "/api/student/ask",
            json={"student_id": "stu-1", "question": "超时后还会记录吗？"},
            headers={"Authorization": "Bearer secret"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["needs_attention"] is True
    assert "LLM" in body["answer"]
    assert store.list_student_asks("stu-1")[0]["error_code"] == "llm_timeout"


def test_student_question_context_is_same_owner_current_session_and_bounded(tmp_path, monkeypatch):
    captured: list[dict] = []

    async def capture_answer(config, question, context_messages):
        captured.extend(context_messages)
        return "边界上下文已收到"

    monkeypatch.setattr(service_module, "llm_answer_question", capture_answer)
    app, store, _events = _build_app(tmp_path)
    store.upsert_student("stu-a")
    store.upsert_student("stu-b")
    store.upsert_session("sess-a", "stu-a", "", "A")
    store.upsert_session("sess-b", "stu-b", "", "B")
    store.add_raw_transcript(
        "sess-a",
        "stu-a",
        "".join(_line({
            "type": "message",
            "role": "user",
            "content": f"A-context-{index:02d}",
            "sessionId": "sess-a",
        }) for index in range(24)),
    )
    store.add_raw_transcript(
        "sess-b",
        "stu-b",
        _line({
            "type": "message",
            "role": "user",
            "content": "B-PRIVATE-CONTEXT",
            "sessionId": "sess-b",
        }),
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/student/ask",
            json={
                "student_id": "stu-a",
                "session_id": "sess-a",
                "question": "只看我当前会话",
            },
            headers={"Authorization": "Bearer secret"},
        )

    assert resp.status_code == 200
    assert len(captured) == 16
    rendered = "\n".join(item["content"] for item in captured)
    assert "A-context-08" in rendered
    assert "A-context-23" in rendered
    assert "A-context-07" not in rendered
    assert "B-PRIVATE-CONTEXT" not in rendered


def test_student_ask_rejects_session_owned_by_another_student_before_side_effects(
    tmp_path,
    monkeypatch,
):
    llm_calls = 0

    async def must_not_call_llm(config, question, context_messages):
        nonlocal llm_calls
        llm_calls += 1
        return "不应生成"

    monkeypatch.setattr(service_module, "llm_answer_question", must_not_call_llm)
    app, store, events = _build_app(tmp_path)
    store.upsert_student("stu-a")
    store.upsert_student("stu-b")
    store.upsert_session("sess-b", "stu-b", "", "B")

    with TestClient(app) as client:
        resp = client.post(
            "/api/student/ask",
            json={
                "student_id": "stu-a",
                "session_id": "sess-b",
                "question": "不能借用别人的会话",
            },
            headers={"Authorization": "Bearer secret"},
        )

    assert resp.status_code == 409
    assert llm_calls == 0
    assert store.list_student_asks("stu-a") == []
    assert store.list_student_asks("stu-b") == []
    assert events == []

    with pytest.raises(ValueError, match="belongs to"):
        store.add_student_ask("stu-a", "sess-b", "绕过 API", "不应落库")
    assert store.list_student_asks("stu-a") == []


def test_student_ask_claims_unknown_session_before_another_student_can_bind_it(tmp_path):
    store = Store(tmp_path / "copilot.db")

    ask_id = store.add_student_ask("stu-a", "sess-new", "先提问", "回答")

    assert ask_id > 0
    with pytest.raises(ValueError, match="belongs to"):
        store.upsert_session("sess-new", "stu-b", "", "B")
    with store._conn() as conn:
        owner = conn.execute(
            "SELECT student_id FROM sessions WHERE session_id = ?",
            ("sess-new",),
        ).fetchone()[0]
    assert owner == "stu-a"
    assert store.list_student_asks("stu-a", "sess-new")[0]["id"] == ask_id


def test_first_ask_binds_unknown_session_before_llm_and_rejects_late_owner(
    tmp_path,
    monkeypatch,
):
    app, store, events = _build_app(tmp_path)
    llm_calls = 0
    late_owner_claims: list[bool] = []

    async def late_owner_attempts_claim(config, question, context_messages):
        nonlocal llm_calls
        llm_calls += 1
        store.upsert_student("stu-b")
        try:
            store.upsert_session("sess-race", "stu-b", "", "B")
        except ValueError:
            late_owner_claims.append(False)
        else:
            late_owner_claims.append(True)
        return "先到的回答"

    monkeypatch.setattr(
        service_module,
        "llm_answer_question",
        late_owner_attempts_claim,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/student/ask",
            headers={"Authorization": "Bearer secret"},
            json={
                "student_id": "stu-a",
                "session_id": "sess-race",
                "question": "竞态期间提问",
            },
        )

    assert response.status_code == 200
    assert response.json()["answer"] == "先到的回答"
    assert response.json()["status"] == "answered"
    assert llm_calls == 1
    assert late_owner_claims == [False]
    with store._conn() as conn:
        owner = conn.execute(
            "SELECT student_id FROM sessions WHERE session_id = ?",
            ("sess-race",),
        ).fetchone()[0]
    assert owner == "stu-a"
    asks = store.list_student_asks("stu-a", "sess-race")
    assert len(asks) == 1
    assert asks[0]["answer"] == "先到的回答"
    assert [event.get("type") for event in events] == ["student_ask"]


def test_legacy_student_asks_migrate_status_and_feedback_defaults(tmp_path):
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
               VALUES ('legacy-student', 'legacy-session', '旧问题', '旧回答', 1.0);"""
        )

    store = Store(db_path)
    row = store.list_student_asks("legacy-student")[0]

    assert row["answer_status"] == "answered"
    assert row["error_code"] == ""
    assert row["feedback"] == ""
    assert row["feedback_note"] == ""
    assert row["feedback_at"] is None


def test_feedback_is_first_write_immutable_and_never_sends_mentor_message(tmp_path):
    app, store, events = _build_app(tmp_path)
    ask_id = store.add_student_ask("stu-1", "sess-1", "有用吗？", "固定回答")

    with TestClient(app) as client:
        first = client.post(
            f"/api/student/asks/{ask_id}/feedback",
            json={"student_id": "stu-1", "feedback": "helpful", "note": "  已解决  "},
            headers={"Authorization": "Bearer secret"},
        )
        duplicate = client.post(
            f"/api/student/asks/{ask_id}/feedback",
            json={"student_id": "stu-1", "feedback": "helpful", "note": "已解决"},
            headers={"Authorization": "Bearer secret"},
        )
        conflict = client.post(
            f"/api/student/asks/{ask_id}/feedback",
            json={"student_id": "stu-1", "feedback": "unresolved", "note": "仍然失败"},
            headers={"Authorization": "Bearer secret"},
        )

    assert first.status_code == 200
    assert first.json()["updated"] is True
    assert duplicate.status_code == 200
    assert duplicate.json()["updated"] is False
    assert conflict.status_code == 409
    row = store.list_student_asks("stu-1")[0]
    assert row["feedback"] == "helpful"
    assert row["feedback_note"] == "已解决"
    assert row["feedback_at"] == first.json()["feedback_at"]
    with store._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mentor_messages").fetchone()[0] == 0
    assert [event for event in events if event.get("type") == "mentor_message"] == []


def test_feedback_rejects_wrong_owner_missing_ask_invalid_value_and_long_note(tmp_path):
    app, store, _events = _build_app(tmp_path)
    ask_id = store.add_student_ask("owner", None, "问题", "回答")

    with TestClient(app) as client:
        wrong_owner = client.post(
            f"/api/student/asks/{ask_id}/feedback",
            json={"student_id": "intruder", "feedback": "helpful"},
            headers={"Authorization": "Bearer secret"},
        )
        missing = client.post(
            "/api/student/asks/999999/feedback",
            json={"student_id": "owner", "feedback": "helpful"},
            headers={"Authorization": "Bearer secret"},
        )
        invalid = client.post(
            f"/api/student/asks/{ask_id}/feedback",
            json={"student_id": "owner", "feedback": "maybe"},
            headers={"Authorization": "Bearer secret"},
        )
        too_long = client.post(
            f"/api/student/asks/{ask_id}/feedback",
            json={"student_id": "owner", "feedback": "unresolved", "note": "x" * 501},
            headers={"Authorization": "Bearer secret"},
        )

    assert wrong_owner.status_code == 403
    assert missing.status_code == 404
    assert invalid.status_code == 422
    assert too_long.status_code == 422
    assert store.list_student_asks("owner")[0]["feedback"] == ""


def test_feedback_store_rejects_long_note_when_called_without_http_boundary(tmp_path):
    store = Store(tmp_path / "copilot.db")
    ask_id = store.add_student_ask("owner", None, "问题", "回答")

    with pytest.raises(ValueError, match="note"):
        store.record_student_ask_feedback(
            ask_id,
            "owner",
            "helpful",
            "x" * 501,
        )

    assert store.list_student_asks("owner")[0]["feedback"] == ""
