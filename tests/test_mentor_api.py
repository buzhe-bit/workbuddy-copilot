"""测试 mentor API 路由（MVC 重构后，mock 打在 Service 层）。"""
import asyncio

import pytest

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from copilot.app_context import get_message_service, get_session_service, get_store
from copilot.eventbus import EventBus
from copilot.service import app
from copilot.models import Student, Conversation, TimelineEntry
from copilot.services import MessageService
from copilot.store import Store


@pytest.fixture
def client():
    return TestClient(app)


class TestMentorStudents:
    """GET /api/mentor/students。"""

    def test_returns_student_list_with_display_name(self, client):
        class FakeSessionService:
            def list_students(self):
                return [
                    Student(student_id="stu-1", display_name="王同学",
                            session_count=2, analysis_count=5, last_topic="Python"),
                ]

        app.dependency_overrides[get_session_service] = lambda: FakeSessionService()
        try:
            resp = client.get("/api/mentor/students")
            assert resp.status_code == 200
            data = resp.json()
            assert "items" in data
            assert len(data["items"]) == 1
            assert data["items"][0]["student_id"] == "stu-1"
            assert data["items"][0]["display_name"] == "王同学"
        finally:
            app.dependency_overrides.clear()

    def test_requires_token_when_configured(self, client, monkeypatch):
        class FakeSessionService:
            def list_students(self):
                return []

        monkeypatch.setenv("COPILOT_TOKEN", "secret")
        app.dependency_overrides[get_session_service] = lambda: FakeSessionService()
        try:
            denied = client.get("/api/mentor/students")
            allowed = client.get(
                "/api/mentor/students",
                headers={"Authorization": "Bearer secret"},
            )
            assert denied.status_code == 401
            assert allowed.status_code == 200
        finally:
            app.dependency_overrides.clear()


class TestMentorStudentSessions:
    """GET /api/mentor/students/{id}/sessions。"""

    def test_returns_sessions_for_student(self, client):
        class FakeSessionService:
            def list_sessions(self, student_id):
                assert student_id == "stu-1"
                return [
                    Conversation(session_id="s1", title="Python学习", analysis_count=3),
                ]

        app.dependency_overrides[get_session_service] = lambda: FakeSessionService()
        try:
            resp = client.get("/api/mentor/students/stu-1/sessions")
            assert resp.status_code == 200
            data = resp.json()
            assert "items" in data
            assert len(data["items"]) == 1
            assert data["items"][0]["session_id"] == "s1"
            assert data["items"][0]["session_title"] == "Python学习"
            assert "title" not in data["items"][0]
        finally:
            app.dependency_overrides.clear()


class TestMentorTimeline:
    """GET /api/mentor/sessions/{id}/timeline。"""

    def test_returns_timeline(self, client):
        class FakeSessionService:
            def get_timeline(self, session_id):
                assert session_id == "sess-1"
                return [
                    TimelineEntry(type="prompt", content="q1", created_at=1.0),
                    TimelineEntry(type="ai_summary", content="a1", created_at=2.0),
                    TimelineEntry(
                        type="analysis",
                        content="d1",
                        created_at=1.5,
                        understanding="low",
                    ),
                ]

        app.dependency_overrides[get_session_service] = lambda: FakeSessionService()
        try:
            resp = client.get("/api/mentor/sessions/sess-1/timeline")
            assert resp.status_code == 200
            data = resp.json()
            assert "items" in data
            assert len(data["items"]) == 3
            analysis = next(item for item in data["items"] if item["type"] == "analysis")
            assert analysis["understanding"] == "low"
        finally:
            app.dependency_overrides.clear()

    def test_empty_session_timeline(self, client):
        class FakeSessionService:
            def get_timeline(self, session_id):
                return []

        app.dependency_overrides[get_session_service] = lambda: FakeSessionService()
        try:
            resp = client.get("/api/mentor/sessions/nonexistent/timeline")
            assert resp.status_code == 200
            assert resp.json()["items"] == []
        finally:
            app.dependency_overrides.clear()


class TestMentorTranscript:
    def test_returns_raw_transcript_content(self, client):
        class FakeStore:
            def get_raw_transcript(self, session_id):
                assert session_id == "sess-raw"
                return {"content": "完整原文\nAI reply", "created_at": 123.5}

        app.dependency_overrides[get_store] = lambda: FakeStore()
        try:
            resp = client.get("/api/mentor/sessions/sess-raw/transcript")
            assert resp.status_code == 200
            assert resp.json() == {"content": "完整原文\nAI reply", "created_at": 123.5}
        finally:
            app.dependency_overrides.clear()


class TestMentorPromptReply:
    def test_returns_prompt_reply_text(self, client):
        class FakeStore:
            def get_prompt(self, prompt_id):
                assert prompt_id == 7
                return {"id": 7, "session_id": "sess-1", "seq_in_session": 0}

            def get_prompt_reply(self, session_id, prompt_seq):
                assert session_id == "sess-1"
                assert prompt_seq == 0
                return "AI 第一段\n\nAI 第二段"

        app.dependency_overrides[get_store] = lambda: FakeStore()
        try:
            resp = client.get("/api/mentor/prompts/7/reply")
            assert resp.status_code == 200
            assert resp.json() == {"reply": "AI 第一段\n\nAI 第二段"}
        finally:
            app.dependency_overrides.clear()

    def test_prompt_reply_404_for_unknown_prompt(self, client):
        class FakeStore:
            def get_prompt(self, prompt_id):
                return None

        app.dependency_overrides[get_store] = lambda: FakeStore()
        try:
            resp = client.get("/api/mentor/prompts/404/reply")
            assert resp.status_code == 404
        finally:
            app.dependency_overrides.clear()

    def test_returns_reply_text_by_prompt_reply_ref(self, client):
        class FakeStore:
            def get_prompt_reply_by_id(self, prompt_id):
                assert prompt_id == 7
                return "<think>内部推理</think>AI 第一段\n\nAI 第二段"

        app.dependency_overrides[get_store] = lambda: FakeStore()
        try:
            resp = client.get("/api/mentor/replies/prompt%3A7/text")
            assert resp.status_code == 200
            assert resp.json() == {"reply": "AI 第一段\n\nAI 第二段"}
        finally:
            app.dependency_overrides.clear()

    def test_returns_reply_text_by_message_reply_ref(self, client):
        class FakeStore:
            def get_message_reply_by_id(self, message_id):
                assert message_id == 42
                return "AI 第一段\n<think>推理\n跨行</think>\nAI 第二段"

        app.dependency_overrides[get_store] = lambda: FakeStore()
        try:
            resp = client.get("/api/mentor/replies/msg%3A42/text")
            assert resp.status_code == 200
            assert resp.json() == {"reply": "AI 第一段\n\nAI 第二段"}
        finally:
            app.dependency_overrides.clear()

    def test_reply_ref_404_for_unknown_or_empty_reply(self, client):
        class FakeStore:
            def get_message_reply_by_id(self, message_id):
                assert message_id == 404
                return ""

        app.dependency_overrides[get_store] = lambda: FakeStore()
        try:
            resp = client.get("/api/mentor/replies/msg%3A404/text")
            assert resp.status_code == 404
        finally:
            app.dependency_overrides.clear()

    def test_rebuilds_transcript_from_prompts_and_ai_summaries_when_raw_missing(self, client):
        class FakeStore:
            def get_raw_transcript(self, session_id):
                assert session_id == "sess-history"
                return None

            def get_prompts_by_session(self, session_id):
                assert session_id == "sess-history"
                return [
                    {"content": "怎么调试循环边界？", "created_at": 10.0, "seq_in_session": 0},
                    {"content": "那 off-by-one 怎么验证？", "created_at": 30.0, "seq_in_session": 1},
                ]

            def get_ai_summaries_by_session(self, session_id):
                assert session_id == "sess-history"
                return [
                    {"content": "建议先打印循环变量和终止条件。", "created_at": 20.0, "prompt_id": 1},
                    {"content": "用最小输入覆盖首尾边界。", "created_at": 40.0, "prompt_id": 2},
                ]

        app.dependency_overrides[get_store] = lambda: FakeStore()
        try:
            resp = client.get("/api/mentor/sessions/sess-history/transcript")
            assert resp.status_code == 200
            data = resp.json()
            assert data["created_at"] == 10.0
            assert "学员：怎么调试循环边界？" in data["content"]
            assert "AI：建议先打印循环变量和终止条件。" in data["content"]
            assert "学员：那 off-by-one 怎么验证？" in data["content"]
            assert "AI：用最小输入覆盖首尾边界。" in data["content"]
        finally:
            app.dependency_overrides.clear()

    def test_transcript_404_when_raw_prompts_and_summaries_are_all_missing(self, client):
        class FakeStore:
            def get_raw_transcript(self, session_id):
                assert session_id == "sess-empty"
                return None

            def get_prompts_by_session(self, session_id):
                assert session_id == "sess-empty"
                return []

            def get_ai_summaries_by_session(self, session_id):
                assert session_id == "sess-empty"
                return []

        app.dependency_overrides[get_store] = lambda: FakeStore()
        try:
            resp = client.get("/api/mentor/sessions/sess-empty/transcript")
            assert resp.status_code == 404
        finally:
            app.dependency_overrides.clear()


class TestReverseMessageApi:
    def test_post_mentor_message_returns_message_id_and_delivery_status(self, client):
        class FakeMessageService:
            async def send(self, student_id, mentor_id, text, client_request_id=None):
                assert student_id == "stu-1"
                assert mentor_id == "mentor-9"
                assert text == "Try a smaller example"
                assert client_request_id is None
                return {"message_id": "msg-1", "id": 3, "delivered": True}

        app.dependency_overrides[get_message_service] = lambda: FakeMessageService()
        try:
            resp = client.post("/api/mentor/message", json={
                "student_id": "stu-1",
                "mentor_id": "mentor-9",
                "text": "Try a smaller example",
            })
            assert resp.status_code == 200
            assert resp.json() == {"message_id": "msg-1", "id": 3, "delivered": True}
        finally:
            app.dependency_overrides.clear()

    def test_post_mentor_message_forwards_client_request_id(self, client):
        class FakeMessageService:
            async def send(self, student_id, mentor_id, text, client_request_id=None):
                assert student_id == "stu-1"
                assert mentor_id is None
                assert text == "Retry-safe hint"
                assert client_request_id == "request-123"
                return {
                    "message_id": "msg-1",
                    "id": 3,
                    "delivered": False,
                    "client_request_id": client_request_id,
                    "duplicate": False,
                }

        app.dependency_overrides[get_message_service] = lambda: FakeMessageService()
        try:
            resp = client.post("/api/mentor/message", json={
                "student_id": "stu-1",
                "text": "Retry-safe hint",
                "client_request_id": "request-123",
            })
            assert resp.status_code == 200
            assert resp.json()["client_request_id"] == "request-123"
        finally:
            app.dependency_overrides.clear()

    def test_mentor_message_status_is_bounded_and_mentor_protected(
        self,
        client,
        monkeypatch,
    ):
        class FakeMessageService:
            def get_mentor_message_statuses(self, client_request_ids):
                assert client_request_ids == ["request-123", "missing"]
                return [{
                    "client_request_id": "request-123",
                    "message_id": "msg-1",
                    "id": 3,
                    "student_id": "stu-1",
                    "delivered": True,
                }]

        monkeypatch.setenv("COPILOT_TOKEN", "secret")
        app.dependency_overrides[get_message_service] = lambda: FakeMessageService()
        try:
            denied = client.post("/api/mentor/messages/status", json={
                "client_request_ids": ["request-123", "missing"],
            })
            allowed = client.post(
                "/api/mentor/messages/status",
                json={"client_request_ids": ["request-123", "missing"]},
                headers={"Authorization": "Bearer secret"},
            )
            too_many = client.post(
                "/api/mentor/messages/status",
                json={"client_request_ids": [f"request-{i}" for i in range(301)]},
                headers={"Authorization": "Bearer secret"},
            )

            assert denied.status_code == 401
            assert allowed.status_code == 200
            assert allowed.json() == {"items": [{
                "client_request_id": "request-123",
                "message_id": "msg-1",
                "id": 3,
                "student_id": "stu-1",
                "delivered": True,
            }]}
            assert too_many.status_code == 422
        finally:
            app.dependency_overrides.clear()

    def test_conflicting_client_request_id_returns_409(self, client):
        class FakeMessageService:
            async def send(self, **kwargs):
                raise ValueError("client_request_id payload conflict")

        app.dependency_overrides[get_message_service] = lambda: FakeMessageService()
        try:
            response = client.post("/api/mentor/message", json={
                "student_id": "stu-1",
                "text": "Changed hint",
                "client_request_id": "request-conflict",
            })
            assert response.status_code == 409
            assert response.json()["detail"] == "client_request_id payload conflict"
        finally:
            app.dependency_overrides.clear()

    def test_real_status_projection_does_not_expose_message_text(
        self,
        client,
        tmp_path,
    ):
        store = Store(tmp_path / "message-status.db")
        service = MessageService(store, EventBus())
        sent = asyncio.run(service.send(
            "stu-1",
            "mentor-1",
            "private mentor guidance",
            client_request_id="request-private",
        ))
        asyncio.run(service.ack(sent["message_id"], "stu-1"))

        app.dependency_overrides[get_message_service] = lambda: service
        try:
            response = client.post("/api/mentor/messages/status", json={
                "client_request_ids": ["request-private"],
            })
            assert response.status_code == 200
            assert response.json() == {"items": [{
                "client_request_id": "request-private",
                "message_id": sent["message_id"],
                "id": sent["id"],
                "student_id": "stu-1",
                "delivered": True,
            }]}
            assert "private mentor guidance" not in response.text
        finally:
            app.dependency_overrides.clear()

    def test_get_student_messages_returns_catchup_items(self, client):
        class FakeMessageService:
            def get_catchup(self, student_id, since_id, *, limit=None):
                assert student_id == "stu-1"
                assert since_id == 10
                assert limit is None
                return [{
                    "type": "mentor_message",
                    "student_id": "stu-1",
                    "message_id": "msg-2",
                    "id": 11,
                    "text": "Next hint",
                    "mentor_id": "mentor-1",
                    "timestamp": 12.5,
                }]

        app.dependency_overrides[get_message_service] = lambda: FakeMessageService()
        try:
            resp = client.get("/api/student/messages?student_id=stu-1&since=10")
            assert resp.status_code == 200
            assert resp.json()["items"][0]["message_id"] == "msg-2"
        finally:
            app.dependency_overrides.clear()

    def test_ack_student_message_marks_delivered(self, client):
        class FakeMessageService:
            def ack(self, message_id, student_id):
                assert message_id == "msg-1"
                assert student_id == "stu-1"
                return True

        app.dependency_overrides[get_message_service] = lambda: FakeMessageService()
        try:
            resp = client.post("/api/student/messages/ack", json={
                "student_id": "stu-1",
                "message_id": "msg-1",
            })
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}
        finally:
            app.dependency_overrides.clear()

    def test_delete_student_routes_to_store_cascade(self, client):
        class FakeStore:
            def delete_student(self, student_id):
                assert student_id == "stu-1"
                return {"students": 1, "reports": 2}

        app.dependency_overrides[get_store] = lambda: FakeStore()
        try:
            resp = client.delete("/api/admin/students/stu-1")
            assert resp.status_code == 200
            assert resp.json() == {"deleted": {"students": 1, "reports": 2}}
        finally:
            app.dependency_overrides.clear()


class TestMentorWS:
    """WS /ws/mentor 独立于 /ws。"""

    def test_mentor_ws_endpoint_exists(self, client):
        """验证 /ws/mentor 路由可连接。"""
        with client.websocket_connect("/ws/mentor"):
            pass

    def test_websockets_require_configured_token(self, client, monkeypatch):
        monkeypatch.setenv("COPILOT_TOKEN", "secret")
        try:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/ws/mentor"):
                    pass
            with client.websocket_connect("/ws/mentor?token=secret"):
                pass
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/ws?student_id=stu-1"):
                    pass
            with client.websocket_connect("/ws?student_id=stu-1&token=secret"):
                pass
        finally:
            monkeypatch.delenv("COPILOT_TOKEN", raising=False)

    def test_local_open_float_ws_derives_configured_student_when_id_is_omitted(self, client):
        registry = app.state.context.ws_registry
        student_id = str(app.state.context.config.get("student_id") or "")
        before = len(registry.floats.get(student_id, set()))

        with client.websocket_connect("/ws") as ws:
            assert len(registry.floats.get(student_id, set())) == before + 1
            ws.send_text("derived local identity")

        assert len(registry.floats.get(student_id, set())) == before

    def test_mentor_ws_pool_separate_from_floating(self):
        """验证导师 WS 客户端池与浮标独立。"""
        registry = app.state.context.ws_registry
        assert registry.mentors is not registry.floats
        assert isinstance(registry.mentors, set)
        assert isinstance(registry.floats, dict)
