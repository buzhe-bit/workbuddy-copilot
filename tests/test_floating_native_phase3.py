from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
from urllib.parse import parse_qs, urlparse

import copilot.floating_native as floating_native
from copilot.floating_native import (
    CopilotNativeApp,
    _build_float_ws_url,
    _panel_origin_for_icon,
    _session_display_title,
)
from copilot.student_platform.macos import StudentCoordinatorCommandCallback


def test_panel_origin_places_panel_left_of_right_side_icon_and_clamps_to_screen():
    screen_frame = (0.0, 0.0, 1440.0, 900.0)
    panel_size = (380.0, 520.0)
    icon_frame = (1360.0, 760.0, 48.0, 48.0)

    x, y = _panel_origin_for_icon(icon_frame, panel_size, screen_frame)

    assert x + panel_size[0] <= icon_frame[0] - 8.0
    assert x >= screen_frame[0]
    assert y >= screen_frame[1]
    assert y + panel_size[1] <= screen_frame[1] + screen_frame[3]
    assert (x, y) != (
        screen_frame[0] + (screen_frame[2] - panel_size[0]) / 2,
        screen_frame[1] + screen_frame[3] * 0.3,
    )


def test_panel_origin_follows_icon_position_changes():
    screen_frame = (0.0, 0.0, 1440.0, 900.0)
    panel_size = (380.0, 520.0)

    origin_a = _panel_origin_for_icon((900.0, 600.0, 48.0, 48.0), panel_size, screen_frame)
    origin_b = _panel_origin_for_icon((1000.0, 520.0, 48.0, 48.0), panel_size, screen_frame)

    assert origin_a != origin_b
    assert origin_b[0] > origin_a[0]
    assert origin_b[1] < origin_a[1]


def test_native_session_title_never_falls_back_to_session_id():
    assert _session_display_title({"session_id": "private-session-id"}) == "新建对话"
    assert _session_display_title({"display_title": "显示名"}) == "显示名"


def test_float_ws_url_includes_student_token_and_last_seen_without_leaking_when_redacted(monkeypatch):
    monkeypatch.delenv("COPILOT_TOKEN", raising=False)
    cfg = {
        "student_id": "student-a",
        "service": {"host": "127.0.0.1", "port": 8765, "token": "secret-token"},
    }

    raw_url = _build_float_ws_url(cfg, "student-a", 42)
    redacted_url = _build_float_ws_url(cfg, "student-a", 42, redact_token=True)

    raw_query = parse_qs(urlparse(raw_url).query)
    redacted_query = parse_qs(urlparse(redacted_url).query)

    assert raw_query["student_id"] == ["student-a"]
    assert raw_query["token"] == ["secret-token"]
    assert raw_query["last_seen_message_id"] == ["42"]
    assert "secret-token" not in redacted_url
    assert redacted_query["token"] == ["<redacted>"]


def test_float_ws_url_prefers_student_role_token(monkeypatch):
    monkeypatch.delenv("COPILOT_TOKEN", raising=False)
    cfg = {
        "student_id": "student-a",
        "auth": {
            "token": "legacy-token",
            "student_token": "student-token",
            "mentor_token": "mentor-token",
        },
        "service": {"host": "127.0.0.1", "port": 8765},
    }

    raw_url = _build_float_ws_url(cfg, "student-a", 0)
    raw_query = parse_qs(urlparse(raw_url).query)

    assert raw_query["token"] == ["student-token"]


def test_float_urls_use_public_base_url_for_https_and_wss(monkeypatch):
    monkeypatch.delenv("COPILOT_TOKEN", raising=False)
    cfg = {
        "student_id": "student-a",
        "auth": {"student_token": "student-token"},
        "service": {
            "host": "127.0.0.1",
            "port": 8765,
            "public_base_url": "https://copilot.example.com/copilot/",
        },
    }

    ws_url = _build_float_ws_url(cfg, "student-a", 0)

    assert ws_url.startswith("wss://copilot.example.com/copilot/ws?")
    assert parse_qs(urlparse(ws_url).query)["token"] == ["student-token"]


def test_float_ws_loopback_uses_proxy_safe_connector(monkeypatch):
    captured: dict[str, object] = {}

    def fake_connect(url: str, headers: dict[str, str]):
        captured["url"] = url
        captured["headers"] = headers
        return "connection"

    monkeypatch.setattr(floating_native, "_default_ws_connect", fake_connect)

    assert floating_native._connect_float_ws("ws://127.0.0.1:8765/ws") == "connection"
    assert captured == {
        "url": "ws://127.0.0.1:8765/ws",
        "headers": {},
    }


def test_macos_command_callback_hands_non_ui_command_to_student_coordinator():
    class FakeCoordinator:
        def __init__(self):
            self.commands = []

        async def handle_command(self, command):
            self.commands.append(command)
            return True

    coordinator = FakeCoordinator()
    results = []
    callback = StudentCoordinatorCommandCallback(
        coordinator,
        run=lambda awaitable: results.append(asyncio.run(awaitable)),
    )
    command = {"type": "mentor_command", "command": "upload_conversations"}

    assert callback.submit(command, lambda _command: None) is True
    assert coordinator.commands == [command]
    assert results == [None]


def test_macos_command_callback_does_not_claim_async_false_without_result_bridge():
    class FalseCoordinator:
        async def handle_command(self, command):
            return False

    scheduled = []
    callback = StudentCoordinatorCommandCallback(
        FalseCoordinator(), run=lambda awaitable: scheduled.append(awaitable)
    )

    assert callback({"type": "mentor_command", "command": "upload_conversations"}) is False
    assert scheduled == []


def test_macos_command_callback_bridge_runs_legacy_fallback_after_async_false():
    class FalseCoordinator:
        async def handle_command(self, command):
            return False

    fallback = []
    callback = StudentCoordinatorCommandCallback(
        FalseCoordinator(), run=lambda awaitable: asyncio.run(awaitable)
    )
    command = {"type": "mentor_command", "command": "upload_conversations"}

    assert callback.submit(command, fallback.append) is True
    assert fallback == [command]


def test_floating_uses_injected_student_coordinator_callback_before_legacy_worker():
    calls = []

    class FakeApp:
        _handle_mentor_command = CopilotNativeApp._handle_mentor_command

        def _student_coordinator_callback(self, command):
            calls.append(command)
            return True

        def _handle_mentor_command_upload(self, command):
            raise AssertionError("legacy upload worker should not run after coordinator handoff")

    command = {"type": "mentor_command", "command": "upload_conversations", "request_id": "req-1"}

    FakeApp()._handle_mentor_command(command)

    assert calls == [command]


def test_ws_raw_messages_are_dispatched_to_main_thread(monkeypatch):
    calls = []

    def fake_call_after(func, *args):
        calls.append((func, args))

    monkeypatch.setattr(floating_native.AppHelper, "callAfter", fake_call_after)

    class FakeApp:
        _handle_ws_message = CopilotNativeApp._handle_ws_message

    app = FakeApp()

    CopilotNativeApp._dispatch_ws_message(app, '{"type":"analysis"}')

    assert len(calls) == 1
    func, args = calls[0]
    assert func.__self__ is app
    assert func.__func__ is CopilotNativeApp._handle_ws_message
    assert args == ('{"type":"analysis"}',)


def test_mentor_catchup_messages_are_dispatched_to_main_thread(monkeypatch):
    calls = []

    def fake_call_after(func, *args):
        calls.append((func, args))

    class FakeApp:
        _student_id = "student-a"
        _last_seen_mentor_message_id = 6
        _handle_mentor_message = CopilotNativeApp._handle_mentor_message
        _dispatch_mentor_message = CopilotNativeApp._dispatch_mentor_message

        def _get_json(self, path, *, query, timeout):
            assert path == "/api/student/messages"
            assert query == {"student_id": "student-a", "since": "6"}
            assert timeout == 5
            return {
                "items": [{
                    "type": "mentor_message",
                    "student_id": "student-a",
                    "message_id": "msg-7",
                    "id": 7,
                    "text": "Try a smaller example",
                }]
            }

    monkeypatch.setattr(floating_native.AppHelper, "callAfter", fake_call_after)

    CopilotNativeApp._fetch_mentor_catchup(FakeApp())

    assert len(calls) == 1
    func, args = calls[0]
    assert func.__func__ is CopilotNativeApp._handle_mentor_message
    assert args[0]["message_id"] == "msg-7"


def test_poll_current_session_prefers_local_workbuddy_detection(monkeypatch):
    refreshed = []
    server_calls = []

    def fake_read_sessions(limit):
        assert limit == 8
        return [
            {
                "session_id": "sess-current",
                "title": "当前任务",
                "work_dir": "/work/current",
                "last_activity_at": 20.0,
            },
            {
                "session_id": "sess-old",
                "title": "旧任务",
                "work_dir": "/work/old",
                "last_activity_at": 10.0,
            },
        ]

    class FakeApp:
        _read_local_current_session = CopilotNativeApp._read_local_current_session
        pollCurrentSession_ = CopilotNativeApp.pollCurrentSession_

        def __init__(self):
            self._student_id = "student-a"
            self._current_session_id = None
            self._panel_visible = False
            self._wb_sessions = []
            self._sessions = {}

        def _get_json(self, path, *, timeout):
            server_calls.append(path)
            raise AssertionError("local detection should avoid server current_session")

        def _refresh_data(self):
            refreshed.append(self._current_session_id)

        def _rebuild_session_bar(self):
            raise AssertionError("panel is hidden")

        def _update_icon_state(self):
            raise AssertionError("panel is hidden")

    monkeypatch.setattr(floating_native.wb_sync, "read_sessions", fake_read_sessions)

    app = FakeApp()
    app.pollCurrentSession_(None)

    assert app._current_session_id == "sess-current"
    assert refreshed == ["sess-current"]
    assert server_calls == []
    assert app._wb_sessions[0]["session_id"] == "sess-current"
    assert app._wb_sessions[0]["is_active"] is True


def test_student_ask_worker_posts_context_and_dispatches_answer(monkeypatch):
    calls = []

    def fake_call_after(func, *args):
        calls.append((func, args))

    class FakeApp:
        _handle_ask_answer = CopilotNativeApp._handle_ask_answer
        _handle_ask_error = CopilotNativeApp._handle_ask_error

        def __init__(self):
            self._student_id = "student-a"

        def _post_json(self, path, payload, *, timeout):
            assert path == "/api/student/ask"
            assert payload == {
                "student_id": "student-a",
                "session_id": "sess-1",
                "question": "怎么定位循环边界？",
            }
            assert timeout >= 10
            return {
                "ask_id": 12,
                "answer": "先打印最后一次循环的 index。",
                "status": "degraded",
                "needs_attention": True,
            }

    monkeypatch.setattr(floating_native.AppHelper, "callAfter", fake_call_after)

    app = FakeApp()
    CopilotNativeApp._send_student_ask_worker(app, "怎么定位循环边界？", "sess-1")

    assert len(calls) == 1
    func, args = calls[0]
    assert func.__self__ is app
    assert func.__func__ is CopilotNativeApp._handle_ask_answer
    assert args == (
        "怎么定位循环边界？",
        "先打印最后一次循环的 index。",
        12,
        "degraded",
        True,
    )


def test_student_ask_answer_renders_status_and_enables_feedback_actions():
    rendered = []

    class FakeControl:
        def __init__(self):
            self.enabled = None
            self.hidden = None
            self.value = None

        def setEnabled_(self, value):
            self.enabled = value

        def setHidden_(self, value):
            self.hidden = value

        def setStringValue_(self, value):
            self.value = value

    class FakeApp:
        _handle_ask_answer = CopilotNativeApp._handle_ask_answer

        def __init__(self):
            self.ask_send_button = FakeControl()
            self.ask_input = FakeControl()
            self.ask_feedback_helpful_button = FakeControl()
            self.ask_feedback_unresolved_button = FakeControl()

        def _set_ask_answer_text(self, text):
            rendered.append(text)

        def _end_ask_focus(self):
            return None

    app = FakeApp()
    app._handle_ask_answer("为什么失败？", "这是安全降级答案", 31, "degraded", True)

    assert app._current_ask_id == 31
    assert app._current_ask_status == "degraded"
    assert "状态：降级回答（建议稍后重试或请导师协助）" in rendered[-1]
    assert "这是安全降级答案" in rendered[-1]
    assert app.ask_feedback_helpful_button.hidden is False
    assert app.ask_feedback_unresolved_button.hidden is False
    assert app.ask_feedback_helpful_button.enabled is True
    assert app.ask_feedback_unresolved_button.enabled is True


def test_student_ask_feedback_worker_posts_original_ask_and_dispatches_result(monkeypatch):
    calls = []
    posts = []

    def fake_call_after(func, *args):
        calls.append((func, args))

    class FakeApp:
        _handle_ask_feedback_result = CopilotNativeApp._handle_ask_feedback_result
        _handle_ask_feedback_error = CopilotNativeApp._handle_ask_feedback_error

        def __init__(self):
            self._student_id = "student-a"

        def _post_json(self, path, payload, *, timeout):
            posts.append((path, payload, timeout))
            return {
                "ask_id": 31,
                "feedback": "unresolved",
                "feedback_note": "",
                "feedback_at": 123.0,
                "updated": True,
            }

    monkeypatch.setattr(floating_native.AppHelper, "callAfter", fake_call_after)

    app = FakeApp()
    CopilotNativeApp._send_ask_feedback_worker(app, 31, "unresolved")

    assert posts == [(
        "/api/student/asks/31/feedback",
        {"student_id": "student-a", "feedback": "unresolved"},
        5,
    )]
    assert len(calls) == 1
    func, args = calls[0]
    assert func.__func__ is CopilotNativeApp._handle_ask_feedback_result
    assert args == (31, "unresolved", True)


def test_student_ask_feedback_conflict_is_rendered_as_already_recorded(monkeypatch):
    calls = []

    def fake_call_after(func, *args):
        calls.append((func, args))

    class FakeApp:
        _handle_ask_feedback_result = CopilotNativeApp._handle_ask_feedback_result
        _handle_ask_feedback_error = CopilotNativeApp._handle_ask_feedback_error
        _student_id = "student-a"

        def _post_json(self, path, payload, *, timeout):
            raise urllib.error.HTTPError(path, 409, "Conflict", {}, None)

    monkeypatch.setattr(floating_native.AppHelper, "callAfter", fake_call_after)

    app = FakeApp()
    CopilotNativeApp._send_ask_feedback_worker(app, 31, "helpful")

    assert len(calls) == 1
    func, args = calls[0]
    assert func.__func__ is CopilotNativeApp._handle_ask_feedback_result
    assert args == (31, "helpful", False)


def test_student_ask_feedback_result_is_local_ui_only_and_disables_repeat_actions():
    rendered = []

    class FakeControl:
        def __init__(self):
            self.enabled = True
            self.hidden = False

        def setEnabled_(self, value):
            self.enabled = value

        def setHidden_(self, value):
            self.hidden = value

    class FakeApp:
        _handle_ask_feedback_result = CopilotNativeApp._handle_ask_feedback_result

        def __init__(self):
            self._current_ask_rendered_text = "你问：问题\n\nCopilot：回答"
            self.ask_feedback_helpful_button = FakeControl()
            self.ask_feedback_unresolved_button = FakeControl()

        def _set_ask_answer_text(self, text):
            rendered.append(text)

    app = FakeApp()
    app._current_ask_id = 31
    app._handle_ask_feedback_result(31, "helpful", True)

    assert rendered[-1].endswith("反馈：已记录“有帮助”。")
    assert app.ask_feedback_helpful_button.enabled is False
    assert app.ask_feedback_unresolved_button.enabled is False


def test_student_ask_duplicate_or_conflicting_feedback_shows_generic_recorded_state():
    rendered = []

    class FakeControl:
        def setEnabled_(self, value):
            return None

        def setHidden_(self, value):
            return None

    class FakeApp:
        _handle_ask_feedback_result = CopilotNativeApp._handle_ask_feedback_result
        _current_ask_rendered_text = "你问：问题\n\nCopilot：回答"
        ask_feedback_helpful_button = FakeControl()
        ask_feedback_unresolved_button = FakeControl()

        def _set_ask_answer_text(self, text):
            rendered.append(text)

    app = FakeApp()
    app._current_ask_id = 31
    app._handle_ask_feedback_result(31, "helpful", False)

    assert rendered[-1].endswith("反馈：该问题已记录过反馈。")


def test_late_feedback_result_cannot_overwrite_a_newer_ask():
    rendered = []

    class FakeApp:
        _handle_ask_feedback_result = CopilotNativeApp._handle_ask_feedback_result
        _current_ask_id = 32
        _current_ask_rendered_text = "新问题正在显示"

        def _set_ask_answer_text(self, text):
            rendered.append(text)

    FakeApp()._handle_ask_feedback_result(31, "helpful", True)

    assert rendered == []


def test_student_ask_worker_dispatches_friendly_error(monkeypatch):
    calls = []

    def fake_call_after(func, *args):
        calls.append((func, args))

    class FakeApp:
        _handle_ask_answer = CopilotNativeApp._handle_ask_answer
        _handle_ask_error = CopilotNativeApp._handle_ask_error

        def __init__(self):
            self._student_id = "student-a"

        def _post_json(self, path, payload, *, timeout):
            raise TimeoutError("too slow")

    monkeypatch.setattr(floating_native.AppHelper, "callAfter", fake_call_after)

    app = FakeApp()
    CopilotNativeApp._send_student_ask_worker(app, "为什么失败？", "sess-1")

    assert len(calls) == 1
    func, args = calls[0]
    assert func.__self__ is app
    assert func.__func__ is CopilotNativeApp._handle_ask_error
    assert "暂时没能连接" in args[0]


def test_mentor_message_acks_only_after_render_and_state_save():
    class FakeApp:
        def __init__(self):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = set()
            self._pending_receipt_message_ids = set()
            self._last_seen_mentor_message_id = 0
            self.rendered = []
            self.saved = []
            self.acked = []

        def _render_mentor_message(self, item):
            self.rendered.append(item["message_id"])
            return True

        def _save_mentor_message_state(self):
            self.saved.append(self._last_seen_mentor_message_id)

        def _ack_mentor_message(self, message_id):
            self.acked.append((message_id, list(self.rendered), list(self.saved)))

    app = FakeApp()

    CopilotNativeApp._handle_mentor_message(app, {
        "type": "mentor_message",
        "student_id": "student-a",
        "message_id": "msg-1",
        "id": 7,
        "text": "Try a smaller example",
        "mentor_id": "mentor-1",
        "timestamp": 12.0,
    })

    assert app.rendered == ["msg-1"]
    assert app.saved == [7]
    assert app.acked == [("msg-1", ["msg-1"], [7])]
    assert app._seen_mentor_message_ids == {"msg-1"}
    assert app._pending_receipt_message_ids == {"msg-1"}
    assert app._last_seen_mentor_message_id == 7


def test_session_mentor_message_for_another_session_is_persisted_unread_not_rendered_or_acked():
    class FakeApp:
        _handle_mentor_message = CopilotNativeApp._handle_mentor_message

        def __init__(self):
            self._student_id = "student-a"
            self._current_session_id = "session-a"
            self._sessions = {"session-b": {"title": "B", "unread": 0}}
            self._seen_mentor_message_ids = set()
            self._pending_receipt_message_ids = set()
            self._last_seen_mentor_message_id = 0
            self.rendered: list[str] = []
            self.acked: list[str] = []
            self.saved = 0

        def _render_mentor_message(self, item):
            self.rendered.append(item["message_id"])
            return True

        def _save_mentor_message_state(self):
            self.saved += 1
            return True

        def _ack_mentor_message(self, message_id):
            self.acked.append(message_id)
            return True

    app = FakeApp()
    app._handle_mentor_message({
        "type": "mentor_message",
        "student_id": "student-a",
        "message_id": "message-b",
        "session_id": "session-b",
        "scope": "session",
        "id": 7,
        "text": "Only show in B",
    })

    assert app.rendered == []
    assert app.acked == []
    assert app._sessions["session-b"]["unread"] == 1

    app._current_session_id = "session-b"
    CopilotNativeApp._settle_current_session_mentor_messages(app)

    assert app.acked == ["message-b"]
    assert app._mentor_items_by_session["session-b"][0]["rendered"] is True


def test_session_bucket_and_unread_survive_native_restart(tmp_path):
    state_path = tmp_path / "float-state.json"

    class FakeApp:
        _handle_mentor_message = CopilotNativeApp._handle_mentor_message
        _load_mentor_message_state = CopilotNativeApp._load_mentor_message_state
        _save_mentor_message_state = CopilotNativeApp._save_mentor_message_state

        def __init__(self):
            self._student_id = "student-a"
            self._current_session_id = "session-a"
            self._sessions = {"session-b": {"title": "B", "unread": 0}}
            self._mentor_items_by_session = {}
            self._mentor_notifications = []
            self._mentor_unread = 0
            self._seen_mentor_message_ids = set()
            self._seen_mentor_message_order = []
            self._pending_receipt_message_ids = set()
            self._unpersisted_rendered_message_ids = {}
            self._rendering_mentor_message_ids = set()
            self._receipt_ack_inflight_ids = set()
            self._mentor_message_state_lock = threading.RLock()
            self._last_seen_mentor_message_id = 0

        def _mentor_message_state_path(self):
            return str(state_path)

        def _ack_mentor_message(self, _message_id):
            raise AssertionError("off-session item must not ack before selection")

    first = FakeApp()
    first._handle_mentor_message({
        "type": "mentor_message",
        "student_id": "student-a",
        "message_id": "message-b",
        "session_id": "session-b",
        "scope": "session",
        "id": 7,
        "text": "Keep this in B",
    })

    restarted = FakeApp()
    restarted._load_mentor_message_state()

    assert restarted._sessions["session-b"]["unread"] == 1
    assert [item["message_id"] for item in restarted._mentor_items_by_session["session-b"]] == [
        "message-b"
    ]
    assert restarted._mentor_items_by_session["session-b"][0]["rendered"] is False


def test_duplicate_acknowledged_mentor_message_does_not_retry_receipt_or_render():
    class FakeApp:
        def __init__(self):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = {"msg-1"}
            self._pending_receipt_message_ids = set()
            self._last_seen_mentor_message_id = 7
            self.rendered: list[str] = []
            self.acked: list[str] = []

        def _render_mentor_message(self, item):
            self.rendered.append(item["message_id"])
            return True

        def _ack_mentor_message(self, message_id):
            self.acked.append(message_id)

    app = FakeApp()
    CopilotNativeApp._handle_mentor_message(app, {
        "type": "mentor_message",
        "student_id": "student-a",
        "message_id": "msg-1",
        "id": 7,
        "text": "Retry only the receipt",
    })

    assert app.rendered == []
    assert app.acked == []


def test_duplicate_pending_mentor_message_retries_only_receipt_without_rendering_again():
    class FakeApp:
        def __init__(self):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = {"msg-1"}
            self._pending_receipt_message_ids = {"msg-1"}
            self._last_seen_mentor_message_id = 7
            self.rendered: list[str] = []
            self.acked: list[str] = []

        def _render_mentor_message(self, item):
            self.rendered.append(item["message_id"])
            return True

        def _ack_mentor_message(self, message_id):
            self.acked.append(message_id)

    app = FakeApp()
    CopilotNativeApp._handle_mentor_message(app, {
        "type": "mentor_message",
        "student_id": "student-a",
        "message_id": "msg-1",
        "id": 7,
        "text": "Retry only the pending receipt",
    })

    assert app.rendered == []
    assert app.acked == ["msg-1"]


def test_ack_does_not_post_unknown_unrendered_message():
    class FakeApp:
        _ack_mentor_message = CopilotNativeApp._ack_mentor_message

        def __init__(self):
            self._student_id = "student-a"
            self._pending_receipt_message_ids = {"known-message"}
            self.posted: list[str] = []
            self.saves = 0

        def _post_json(self, path, payload, *, timeout):
            self.posted.append(payload["message_id"])
            return {"ok": True}

        def _save_mentor_message_state(self):
            self.saves += 1

    app = FakeApp()

    assert app._ack_mentor_message("unknown-message") is False
    assert app.posted == []
    assert app._pending_receipt_message_ids == {"known-message"}
    assert app.saves == 0


def test_pending_receipt_catchup_retries_503_then_recovers_without_duplicate_render():
    class FakeApp:
        _fetch_pending_mentor_receipts = CopilotNativeApp._fetch_pending_mentor_receipts
        _ack_mentor_message = CopilotNativeApp._ack_mentor_message

        def __init__(self):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = {"msg-7"}
            self._pending_receipt_message_ids = {"msg-7"}
            self._last_seen_mentor_message_id = 7
            self.post_attempts = 0
            self.rendered: list[str] = []
            self.saved = 0

        def _get_json(self, path, *, query, timeout):
            assert path == "/api/student/messages/pending-receipts"
            assert query == {
                "student_id": "student-a",
                "limit": "64",
                "after_id": "0",
            }
            assert timeout == 5
            return {"items": [{
                "type": "mentor_message",
                "student_id": "student-a",
                "message_id": "msg-7",
                "id": 7,
                "text": "Retry the persisted receipt",
            }]}

        def _post_json(self, path, payload, *, timeout):
            assert path == "/api/student/messages/ack"
            assert payload == {"student_id": "student-a", "message_id": "msg-7"}
            assert timeout == 3
            self.post_attempts += 1
            if self.post_attempts == 1:
                raise TimeoutError("503 while offline")
            return {"ok": True}

        def _render_mentor_message(self, item):
            self.rendered.append(item["message_id"])
            return True

        def _save_mentor_message_state(self):
            self.saved += 1

    app = FakeApp()
    CopilotNativeApp._fetch_pending_mentor_receipts(app)
    CopilotNativeApp._fetch_pending_mentor_receipts(app)

    assert app.post_attempts == 2
    assert app.rendered == []
    assert app._seen_mentor_message_ids == {"msg-7"}
    assert app._pending_receipt_message_ids == set()
    assert app.saved == 1


def test_mentor_message_no_ack_when_render_fails():
    class FakeApp:
        _student_id = "student-a"
        _seen_mentor_message_ids = set()
        _pending_receipt_message_ids = set()
        _last_seen_mentor_message_id = 0

        def __init__(self):
            self.acked = []

        def _render_mentor_message(self, item):
            return False

        def _save_mentor_message_state(self):
            raise AssertionError("state should not save before render")

        def _ack_mentor_message(self, message_id):
            self.acked.append(message_id)

    app = FakeApp()

    CopilotNativeApp._handle_mentor_message(app, {
        "type": "mentor_message",
        "student_id": "student-a",
        "message_id": "msg-1",
        "id": 7,
        "text": "Try a smaller example",
        "mentor_id": "mentor-1",
        "timestamp": 12.0,
    })

    assert app.acked == []
    assert app._seen_mentor_message_ids == set()
    assert app._last_seen_mentor_message_id == 0


def test_native_mentor_message_retries_after_real_render_method_fails_once():
    class Panel:
        def orderFrontRegardless(self):
            return None

        def setLevel_(self, _level):
            return None

    class FakeApp:
        _handle_mentor_message = CopilotNativeApp._handle_mentor_message
        _render_mentor_message = CopilotNativeApp._render_mentor_message

        def __init__(self):
            self._student_id = "student-a"
            self._current_session_id = "session-a"
            self._mentor_items_by_session = {}
            self._mentor_notifications = []
            self._mentor_unread = 0
            self._seen_mentor_message_ids = set()
            self._seen_mentor_message_order = []
            self._pending_receipt_message_ids = set()
            self._unpersisted_rendered_message_ids = {}
            self._rendering_mentor_message_ids = set()
            self._receipt_ack_inflight_ids = set()
            self._last_seen_mentor_message_id = 0
            self._mentor_message_state_lock = threading.RLock()
            self.analysis_panel = Panel()
            self._panel_visible = False
            self.rebuilds = 0
            self.acked = []

        def _rebuild_cards(self):
            self.rebuilds += 1
            if self.rebuilds == 1:
                raise RuntimeError("AppKit unavailable")

        def _position_analysis_panel_near_icon(self):
            return None

        def _update_icon_state(self):
            return None

        def _save_mentor_message_state(self):
            return True

        def _ack_mentor_message(self, message_id):
            self.acked.append(message_id)
            return True

    payload = {
        "type": "mentor_message",
        "student_id": "student-a",
        "message_id": "message-1",
        "session_id": "session-a",
        "scope": "session",
        "id": 1,
        "text": "retry once",
    }
    app = FakeApp()

    app._handle_mentor_message(payload)
    app._handle_mentor_message(payload)

    assert app.rebuilds == 2
    assert app.acked == ["message-1"]
    assert [item["message_id"] for item in app._mentor_items_by_session["session-a"]] == [
        "message-1"
    ]
    assert app._mentor_items_by_session["session-a"][0]["rendered"] is True


def test_upload_mentor_command_starts_background_upload_without_rendering(monkeypatch):
    started = []
    rendered = []
    notices = []

    class FakeThread:
        def __init__(self, *, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            started.append(self.daemon)
            self.target()

    def fake_upload(cfg, student_id, *, mode, request_id=None):
        started.append((cfg, student_id, mode, request_id))
        return {"total": 1, "synced": 1, "skipped": 0, "failed": 0}

    def fake_call_after(func, *args):
        notices.append((func.__name__, args))
        func(*args)

    class FakeApp:
        _handle_ws_message = CopilotNativeApp._handle_ws_message
        _handle_mentor_command = CopilotNativeApp._handle_mentor_command
        _handle_mentor_command_upload = CopilotNativeApp._handle_mentor_command_upload
        _show_upload_sync_notice = CopilotNativeApp._show_upload_sync_notice
        _upload_conversations_worker = CopilotNativeApp._upload_conversations_worker

        def __init__(self):
            self.cfg = {"student_id": "student-a"}
            self._student_id = "student-a"

        def _render_mentor_message(self, item):
            rendered.append(item)
            return True

        def _set_ask_answer_text(self, text):
            notices.append(("text", text))

        def _post_upload_request_status(self, request_id, status, **kwargs):
            started.append(("status", request_id, status))

    monkeypatch.setattr(floating_native.threading, "Thread", FakeThread)
    monkeypatch.setattr(floating_native.wb_upload, "upload_conversations", fake_upload)
    monkeypatch.setattr(floating_native.AppHelper, "callAfter", fake_call_after)

    app = FakeApp()
    app._handle_ws_message(
        '{"type":"mentor_command","student_id":"student-a","command":"upload_conversations",'
        '"request_id":"req-real"}'
    )

    assert rendered == []
    assert True in started
    assert ({"student_id": "student-a"}, "student-a", "missing", "req-real") in started
    assert ("text", "导师请求同步对话中...") in notices


def test_upload_mentor_command_reports_running_and_failed_status(monkeypatch):
    posts = []
    notices = []

    class FakeThread:
        def __init__(self, *, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

    def fail_upload(cfg, student_id, *, mode, request_id=None):
        assert request_id == "req-1"
        raise RuntimeError("network unavailable")

    def fake_call_after(func, *args):
        notices.append((func.__name__, args))
        func(*args)

    class FakeApp:
        _handle_mentor_command_upload = CopilotNativeApp._handle_mentor_command_upload
        _show_upload_sync_notice = CopilotNativeApp._show_upload_sync_notice
        _upload_conversations_worker = CopilotNativeApp._upload_conversations_worker
        _post_upload_request_status = CopilotNativeApp._post_upload_request_status

        def __init__(self):
            self.cfg = {"student_id": "student-a"}
            self._student_id = "student-a"

        def _post_json(self, path, payload, *, timeout):
            posts.append((path, payload, timeout))
            return {"ok": True}

        def _set_ask_answer_text(self, text):
            notices.append(("text", text))

    monkeypatch.setattr(floating_native.threading, "Thread", FakeThread)
    monkeypatch.setattr(floating_native.wb_upload, "upload_conversations", fail_upload)
    monkeypatch.setattr(floating_native.AppHelper, "callAfter", fake_call_after)

    app = FakeApp()
    app._handle_mentor_command_upload({
        "type": "mentor_command",
        "student_id": "student-a",
        "command": "upload_conversations",
        "request_id": "req-1",
    })

    assert posts[0] == (
        "/api/student/upload-requests/req-1/status",
        {"student_id": "student-a", "status": "running"},
        5,
    )
    assert posts[1][0] == "/api/student/upload-requests/req-1/status"
    assert posts[1][1]["student_id"] == "student-a"
    assert posts[1][1]["status"] == "failed"
    assert "network unavailable" in posts[1][1]["error_message"]
    assert ("text", "导师请求同步对话中...") in notices


def test_upload_worker_does_not_upload_when_running_status_post_fails(monkeypatch):
    status_calls = []
    upload_calls = []

    class FakeApp:
        _upload_conversations_worker = CopilotNativeApp._upload_conversations_worker

        def __init__(self):
            self.cfg = {"student_id": "student-a"}
            self._student_id = "student-a"
            self._upload_requests_inflight = {"req-weak-network"}

        def _post_upload_request_status(self, request_id, status, **kwargs):
            status_calls.append((request_id, status, kwargs))
            raise ConnectionError(f"cannot post {status}")

    def forbidden_upload(*args, **kwargs):
        upload_calls.append((args, kwargs))
        raise AssertionError("uploader must not run before server accepts running")

    monkeypatch.setattr(floating_native.wb_upload, "upload_conversations", forbidden_upload)

    app = FakeApp()
    app._upload_conversations_worker("req-weak-network")

    assert upload_calls == []
    assert [status for _request_id, status, _kwargs in status_calls] == ["running", "failed"]
    assert app._upload_requests_inflight == set()


def test_mentor_message_state_persists_seen_ids_and_last_seen(tmp_path):
    state_path = tmp_path / "float_state.json"

    class FakeApp:
        def __init__(self):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = {"msg-1", "msg-2"}
            self._pending_receipt_message_ids = {"msg-1"}
            self._last_seen_mentor_message_id = 9

        def _mentor_message_state_path(self):
            return str(state_path)

    writer = FakeApp()
    CopilotNativeApp._save_mentor_message_state(writer)

    reader = FakeApp()
    reader._seen_mentor_message_ids = set()
    reader._pending_receipt_message_ids = set()
    reader._last_seen_mentor_message_id = 0
    CopilotNativeApp._load_mentor_message_state(reader)

    assert reader._last_seen_mentor_message_id == 9
    assert reader._seen_mentor_message_ids == {"msg-1", "msg-2"}
    assert reader._pending_receipt_message_ids == {"msg-1"}


def test_persisted_pending_receipts_survive_seen_limit_restart_and_ack_oldest_without_rendering(tmp_path):
    state_path = tmp_path / "float_state.json"
    message_ids = [f"msg-{index:03d}" for index in range(201)]

    class FakeApp:
        _handle_mentor_message = CopilotNativeApp._handle_mentor_message
        _fetch_pending_mentor_receipts = CopilotNativeApp._fetch_pending_mentor_receipts
        _ack_mentor_message = CopilotNativeApp._ack_mentor_message
        _load_mentor_message_state = CopilotNativeApp._load_mentor_message_state
        _save_mentor_message_state = CopilotNativeApp._save_mentor_message_state

        def __init__(self, *, fail_ack: bool):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = set()
            self._seen_mentor_message_order: list[str] = []
            self._pending_receipt_message_ids: set[str] = set()
            self._last_seen_mentor_message_id = 0
            self.fail_ack = fail_ack
            self.rendered: list[str] = []
            self.posted: list[str] = []

        def _mentor_message_state_path(self):
            return str(state_path)

        def _render_mentor_message(self, item):
            self.rendered.append(item["message_id"])
            return True

        def _post_json(self, path, payload, *, timeout):
            assert path == "/api/student/messages/ack"
            assert timeout == 3
            self.posted.append(payload["message_id"])
            if self.fail_ack:
                raise TimeoutError("503 while offline")
            return {"ok": True}

        def _get_json(self, path, *, query, timeout):
            assert path == "/api/student/messages/pending-receipts"
            assert query["student_id"] == "student-a"
            assert query["limit"] == "64"
            assert timeout == 5
            if query.get("after_id", "0") == "0":
                return {"items": [{"message_id": message_ids[0], "id": 1}]}
            return {"items": []}

    writer = FakeApp(fail_ack=True)
    for numeric_id, message_id in enumerate(message_ids, start=1):
        writer._handle_mentor_message({
            "type": "mentor_message",
            "student_id": "student-a",
            "message_id": message_id,
            "id": numeric_id,
        })

    restarted = FakeApp(fail_ack=False)
    restarted._load_mentor_message_state()

    assert restarted._pending_receipt_message_ids == set(message_ids)
    assert message_ids[0] not in restarted._seen_mentor_message_ids

    restarted._fetch_pending_mentor_receipts()

    assert restarted.rendered == []
    assert restarted.posted == message_ids[:65]
    assert restarted._pending_receipt_message_ids == set(message_ids[65:])


def test_pending_receipt_recovery_pages_past_64_messages_without_rendering():
    message_ids = [f"msg-{index:03d}" for index in range(201)]

    class FakeApp:
        _fetch_pending_mentor_receipts = CopilotNativeApp._fetch_pending_mentor_receipts
        _ack_mentor_message = CopilotNativeApp._ack_mentor_message

        def __init__(self):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = set()
            self._pending_receipt_message_ids = set(message_ids)
            self._last_seen_mentor_message_id = 201
            self.server_pending = [
                {"message_id": message_id, "id": index}
                for index, message_id in enumerate(message_ids, start=1)
            ]
            self.fetches = 0
            self.posted: list[str] = []
            self.rendered: list[str] = []
            self.saves = 0

        def _get_json(self, path, *, query, timeout):
            assert path == "/api/student/messages/pending-receipts"
            assert query["student_id"] == "student-a"
            assert query["limit"] == "64"
            assert timeout == 5
            self.fetches += 1
            after_id = int(query.get("after_id", "0"))
            return {
                "items": [
                    dict(item)
                    for item in self.server_pending
                    if item["id"] > after_id
                ][:64]
            }

        def _post_json(self, path, payload, *, timeout):
            assert path == "/api/student/messages/ack"
            assert timeout == 3
            message_id = payload["message_id"]
            self.posted.append(message_id)
            self.server_pending = [
                item for item in self.server_pending if item["message_id"] != message_id
            ]
            return {"ok": True}

        def _save_mentor_message_state(self):
            self.saves += 1

        def _render_mentor_message(self, item):
            self.rendered.append(item["message_id"])
            return True

    app = FakeApp()
    app._fetch_pending_mentor_receipts()

    assert app.posted == message_ids
    assert app.server_pending == []
    assert app._pending_receipt_message_ids == set()
    assert app.fetches == 4
    assert app.rendered == []
    assert app.saves == 201


def test_pending_receipt_cursor_scans_past_unknown_first_page_without_rendering():
    unknown_items = [
        {"message_id": f"unknown-{index}", "id": index}
        for index in range(1, 65)
    ]
    known_item = {"message_id": "rendered-65", "id": 65}

    class FakeApp:
        _fetch_pending_mentor_receipts = CopilotNativeApp._fetch_pending_mentor_receipts
        _ack_mentor_message = CopilotNativeApp._ack_mentor_message

        def __init__(self):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = set()
            self._pending_receipt_message_ids = {"rendered-65"}
            self._pending_receipt_after_id = 0
            self._receipt_ack_inflight_ids = set()
            self._mentor_message_state_lock = threading.RLock()
            self.fetch_after_ids: list[int] = []
            self.posted: list[str] = []
            self.rendered: list[str] = []

        def _get_json(self, path, *, query, timeout):
            assert path == "/api/student/messages/pending-receipts"
            assert timeout == 5
            after_id = int(query["after_id"])
            self.fetch_after_ids.append(after_id)
            all_items = unknown_items + [known_item]
            return {"items": [item for item in all_items if item["id"] > after_id][:64]}

        def _post_json(self, path, payload, *, timeout):
            assert path == "/api/student/messages/ack"
            assert payload["message_id"] == "rendered-65"
            self.posted.append(payload["message_id"])
            return {"ok": True}

        def _save_mentor_message_state(self):
            return None

        def _render_mentor_message(self, item):
            self.rendered.append(item["message_id"])
            return True

    app = FakeApp()
    app._fetch_pending_mentor_receipts()

    assert app.fetch_after_ids[:2] == [0, 64]
    assert app.posted == ["rendered-65"]
    assert app._pending_receipt_message_ids == set()
    assert app.rendered == []


def test_empty_cursor_scan_retries_persisted_receipt_after_lost_ack_response():
    class FakeApp:
        _fetch_pending_mentor_receipts = CopilotNativeApp._fetch_pending_mentor_receipts
        _ack_mentor_message = CopilotNativeApp._ack_mentor_message

        def __init__(self):
            self._student_id = "student-a"
            self._pending_receipt_message_ids = {"rendered-message"}
            self._pending_receipt_after_id = 0
            self._receipt_ack_inflight_ids = set()
            self._mentor_message_state_lock = threading.RLock()
            self.posted: list[str] = []
            self.saves = 0

        def _get_json(self, path, *, query, timeout):
            assert path == "/api/student/messages/pending-receipts"
            assert query["after_id"] == "0"
            return {"items": []}

        def _post_json(self, path, payload, *, timeout):
            assert path == "/api/student/messages/ack"
            self.posted.append(payload["message_id"])
            return {"ok": True}

        def _save_mentor_message_state(self):
            self.saves += 1

    app = FakeApp()

    assert app._fetch_pending_mentor_receipts() is False
    assert app.posted == ["rendered-message"]
    assert app._pending_receipt_message_ids == set()
    assert app.saves == 1


def test_failed_stable_ws_ack_wakes_the_single_throttled_retry_task():
    class RunningLoop:
        def __init__(self):
            self.callbacks = []

        def is_running(self):
            return True

        def call_soon_threadsafe(self, callback):
            self.callbacks.append(callback)

    class FakeApp:
        _handle_mentor_message = CopilotNativeApp._handle_mentor_message

        def __init__(self):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = set()
            self._seen_mentor_message_order = []
            self._pending_receipt_message_ids = set()
            self._mentor_message_state_lock = threading.RLock()
            self._last_seen_mentor_message_id = 0
            self._ws_asyncio_loop = RunningLoop()
            self._pending_receipt_retry_task = None
            self.rendered: list[str] = []

        def _render_mentor_message(self, item):
            self.rendered.append(item["message_id"])
            return True

        def _save_mentor_message_state(self):
            return None

        def _ack_mentor_message(self, message_id):
            assert message_id == "message-1"
            return False

    app = FakeApp()
    app._handle_mentor_message({
        "type": "mentor_message",
        "student_id": "student-a",
        "message_id": "message-1",
        "id": 1,
    })

    assert app.rendered == ["message-1"]
    assert app._pending_receipt_message_ids == {"message-1"}
    assert len(app._ws_asyncio_loop.callbacks) == 1


def test_pending_receipt_retry_continues_past_single_sync_budget_without_busy_loop(monkeypatch):
    message_ids = [f"message-{index}" for index in range(513)]
    monkeypatch.setattr(floating_native, "PENDING_RECEIPT_RETRY_DELAY_SECONDS", 0, raising=False)

    class FakeApp:
        _fetch_pending_mentor_receipts = CopilotNativeApp._fetch_pending_mentor_receipts
        _ack_mentor_message = CopilotNativeApp._ack_mentor_message

        def __init__(self):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = set()
            self._pending_receipt_message_ids = set(message_ids)
            self._pending_receipt_after_id = 0
            self._receipt_ack_inflight_ids = set()
            self._mentor_message_state_lock = threading.RLock()
            self.server_pending = [
                {"message_id": message_id, "id": index}
                for index, message_id in enumerate(message_ids, start=1)
            ]
            self.posted: list[str] = []
            self.saves = 0

        def _get_json(self, path, *, query, timeout):
            after_id = int(query["after_id"])
            return {
                "items": [
                    dict(item)
                    for item in self.server_pending
                    if item["id"] > after_id
                ][:64]
            }

        def _post_json(self, path, payload, *, timeout):
            message_id = payload["message_id"]
            self.posted.append(message_id)
            self.server_pending = [
                item for item in self.server_pending if item["message_id"] != message_id
            ]
            return {"ok": True}

        def _save_mentor_message_state(self):
            self.saves += 1

    assert hasattr(CopilotNativeApp, "_retry_pending_receipts_until_settled")
    FakeApp._retry_pending_receipts_until_settled = CopilotNativeApp._retry_pending_receipts_until_settled
    app = FakeApp()
    asyncio.run(app._retry_pending_receipts_until_settled())

    assert app.posted == message_ids
    assert app._pending_receipt_message_ids == set()
    assert app.server_pending == []


def test_pending_receipt_continuation_waits_once_per_bounded_retry_round(monkeypatch):
    waits: list[float] = []

    async def fake_sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(floating_native.asyncio, "sleep", fake_sleep)

    class FakeApp:
        _retry_pending_receipts_until_settled = CopilotNativeApp._retry_pending_receipts_until_settled

        def __init__(self):
            self._pending_receipt_message_ids = {"message-1"}
            self._mentor_message_state_lock = threading.RLock()
            self.calls = 0

        def _fetch_pending_mentor_receipts(self):
            self.calls += 1
            if self.calls == 2:
                self._pending_receipt_message_ids.clear()
                return False
            return True

    app = FakeApp()
    asyncio.run(app._retry_pending_receipts_until_settled())

    assert app.calls == 2
    assert waits == [floating_native.PENDING_RECEIPT_RETRY_DELAY_SECONDS] * 2


def test_state_replace_failure_keeps_last_complete_pending_receipt_ledger(tmp_path, monkeypatch):
    state_path = tmp_path / "float_state.json"

    class FakeApp:
        _save_mentor_message_state = CopilotNativeApp._save_mentor_message_state

        def __init__(self):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = {"seen"}
            self._seen_mentor_message_order = ["seen"]
            self._pending_receipt_message_ids = {"old-pending"}
            self._last_seen_mentor_message_id = 1
            self._mentor_message_state_lock = threading.RLock()

        def _mentor_message_state_path(self):
            return str(state_path)

    app = FakeApp()
    app._save_mentor_message_state()
    before = json.loads(state_path.read_text(encoding="utf-8"))
    app._pending_receipt_message_ids = {"new-pending"}

    def fail_replace(source, destination):
        raise OSError("simulated interrupted state publish")

    monkeypatch.setattr(floating_native.os, "replace", fail_replace)
    app._save_mentor_message_state()

    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_parallel_duplicate_receipt_retry_posts_once_and_persists_empty_pending(tmp_path):
    state_path = tmp_path / "float_state.json"
    first_post_started = threading.Event()
    allow_first_post = threading.Event()
    post_lock = threading.Lock()

    class FakeApp:
        _handle_mentor_message = CopilotNativeApp._handle_mentor_message
        _fetch_pending_mentor_receipts = CopilotNativeApp._fetch_pending_mentor_receipts
        _ack_mentor_message = CopilotNativeApp._ack_mentor_message
        _load_mentor_message_state = CopilotNativeApp._load_mentor_message_state
        _save_mentor_message_state = CopilotNativeApp._save_mentor_message_state

        def __init__(self):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = set()
            self._seen_mentor_message_order: list[str] = []
            self._pending_receipt_message_ids: set[str] = set()
            self._pending_receipt_after_id = 0
            self._receipt_ack_inflight_ids: set[str] = set()
            self._mentor_message_state_lock = threading.RLock()
            self._last_seen_mentor_message_id = 0
            self.posted: list[str] = []

        def _mentor_message_state_path(self):
            return str(state_path)

        def _render_mentor_message(self, item):
            return True

        def _get_json(self, path, *, query, timeout):
            return {"items": [{"message_id": "message-1", "id": 1}]}

        def _post_json(self, path, payload, *, timeout):
            with post_lock:
                self.posted.append(payload["message_id"])
                first = len(self.posted) == 1
            if first:
                first_post_started.set()
                assert allow_first_post.wait(timeout=2)
            return {"ok": True}

    app = FakeApp()
    render_thread = threading.Thread(target=app._handle_mentor_message, args=({
        "type": "mentor_message",
        "student_id": "student-a",
        "message_id": "message-1",
        "id": 1,
    },))
    render_thread.start()
    assert first_post_started.wait(timeout=2)
    retry_thread = threading.Thread(target=app._fetch_pending_mentor_receipts)
    retry_thread.start()
    allow_first_post.set()
    render_thread.join(timeout=2)
    retry_thread.join(timeout=2)

    assert app.posted == ["message-1"]
    assert app._pending_receipt_message_ids == set()

    restarted = FakeApp()
    restarted._load_mentor_message_state()
    assert restarted._pending_receipt_message_ids == set()


def test_parallel_live_message_render_is_claimed_once_before_pending_is_persisted():
    first_render_started = threading.Event()
    allow_first_render = threading.Event()
    render_lock = threading.Lock()

    class FakeApp:
        _handle_mentor_message = CopilotNativeApp._handle_mentor_message

        def __init__(self):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = set()
            self._seen_mentor_message_order = []
            self._pending_receipt_message_ids = set()
            self._rendering_mentor_message_ids = set()
            self._mentor_message_state_lock = threading.RLock()
            self._last_seen_mentor_message_id = 0
            self.rendered: list[str] = []
            self.acked: list[str] = []

        def _render_mentor_message(self, item):
            with render_lock:
                self.rendered.append(item["message_id"])
                is_first = len(self.rendered) == 1
            if is_first:
                first_render_started.set()
                assert allow_first_render.wait(timeout=2)
            return True

        def _save_mentor_message_state(self):
            return None

        def _ack_mentor_message(self, message_id):
            self.acked.append(message_id)
            return True

    app = FakeApp()
    payload = {
        "type": "mentor_message",
        "student_id": "student-a",
        "message_id": "message-1",
        "id": 1,
    }
    first = threading.Thread(target=app._handle_mentor_message, args=(payload,))
    second = threading.Thread(target=app._handle_mentor_message, args=(payload,))
    first.start()
    assert first_render_started.wait(timeout=2)
    second.start()
    allow_first_render.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert app.rendered == ["message-1"]
    assert app.acked == ["message-1"]


def test_old_acknowledged_ws_replay_after_seen_window_trim_does_not_render_or_ack():
    class FakeApp:
        _handle_mentor_message = CopilotNativeApp._handle_mentor_message

        def __init__(self):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = {f"message-{index}" for index in range(2, 202)}
            self._seen_mentor_message_order = [f"message-{index}" for index in range(2, 202)]
            self._pending_receipt_message_ids = set()
            self._rendering_mentor_message_ids = set()
            self._mentor_message_state_lock = threading.RLock()
            self._last_seen_mentor_message_id = 201
            self.rendered: list[str] = []
            self.acked: list[str] = []

        def _render_mentor_message(self, item):
            self.rendered.append(item["message_id"])
            return True

        def _save_mentor_message_state(self):
            return True

        def _ack_mentor_message(self, message_id):
            self.acked.append(message_id)
            return True

    app = FakeApp()
    app._handle_mentor_message({
        "type": "mentor_message",
        "student_id": "student-a",
        "message_id": "message-1",
        "id": 1,
    })

    assert app.rendered == []
    assert app.acked == []


def test_first_state_publish_failure_retries_before_failed_ack_preserves_restart_dedup(tmp_path, monkeypatch):
    state_path = tmp_path / "float_state.json"
    real_replace = floating_native.os.replace
    replacements = 0

    def fail_once_replace(source, destination):
        nonlocal replacements
        replacements += 1
        if replacements == 1:
            raise OSError("first state publish interrupted")
        return real_replace(source, destination)

    monkeypatch.setattr(floating_native.os, "replace", fail_once_replace)

    class FakeApp:
        _handle_mentor_message = CopilotNativeApp._handle_mentor_message
        _ack_mentor_message = CopilotNativeApp._ack_mentor_message
        _load_mentor_message_state = CopilotNativeApp._load_mentor_message_state
        _save_mentor_message_state = CopilotNativeApp._save_mentor_message_state

        def __init__(self, *, fail_ack: bool):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = set()
            self._seen_mentor_message_order = []
            self._pending_receipt_message_ids = set()
            self._rendering_mentor_message_ids = set()
            self._receipt_ack_inflight_ids = set()
            self._mentor_message_state_lock = threading.RLock()
            self._last_seen_mentor_message_id = 0
            self.fail_ack = fail_ack
            self.rendered: list[str] = []
            self.posted: list[str] = []

        def _mentor_message_state_path(self):
            return str(state_path)

        def _render_mentor_message(self, item):
            self.rendered.append(item["message_id"])
            return True

        def _post_json(self, path, payload, *, timeout):
            self.posted.append(payload["message_id"])
            if self.fail_ack:
                raise TimeoutError("ack response unavailable")
            return {"ok": True}

    writer = FakeApp(fail_ack=True)
    payload = {
        "type": "mentor_message",
        "student_id": "student-a",
        "message_id": "message-1",
        "id": 1,
    }
    writer._handle_mentor_message(payload)

    restarted = FakeApp(fail_ack=False)
    restarted._load_mentor_message_state()
    restarted._handle_mentor_message(payload)

    assert replacements >= 2
    assert writer.rendered == ["message-1"]
    assert restarted.rendered == []
    assert restarted.posted == ["message-1"]


def test_permanent_state_publish_failure_keeps_in_memory_render_recoverable_without_rerender(tmp_path, monkeypatch):
    state_path = tmp_path / "float_state.json"
    real_replace = floating_native.os.replace
    replacements = 0

    def fail_first_two_replaces(source, destination):
        nonlocal replacements
        replacements += 1
        if replacements <= 2:
            raise OSError("state storage temporarily unavailable")
        return real_replace(source, destination)

    monkeypatch.setattr(floating_native.os, "replace", fail_first_two_replaces)

    class FakeApp:
        _handle_mentor_message = CopilotNativeApp._handle_mentor_message
        _fetch_pending_mentor_receipts = CopilotNativeApp._fetch_pending_mentor_receipts
        _ack_mentor_message = CopilotNativeApp._ack_mentor_message
        _save_mentor_message_state = CopilotNativeApp._save_mentor_message_state

        def __init__(self):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = set()
            self._seen_mentor_message_order = []
            self._pending_receipt_message_ids = set()
            self._unpersisted_rendered_message_ids: dict[str, int] = {}
            self._rendering_mentor_message_ids = set()
            self._receipt_ack_inflight_ids = set()
            self._mentor_message_state_lock = threading.RLock()
            self._last_seen_mentor_message_id = 0
            self._pending_receipt_after_id = 0
            self.rendered: list[str] = []
            self.posted: list[str] = []

        def _mentor_message_state_path(self):
            return str(state_path)

        def _render_mentor_message(self, item):
            self.rendered.append(item["message_id"])
            return True

        def _get_json(self, path, *, query, timeout):
            assert path == "/api/student/messages/pending-receipts"
            return {"items": []}

        def _post_json(self, path, payload, *, timeout):
            self.posted.append(payload["message_id"])
            return {"ok": True}

    app = FakeApp()
    payload = {
        "type": "mentor_message",
        "student_id": "student-a",
        "message_id": "message-1",
        "id": 1,
    }
    app._handle_mentor_message(payload)

    assert app.rendered == ["message-1"]
    assert app.posted == []
    assert app._last_seen_mentor_message_id == 0
    assert app._pending_receipt_message_ids == set()
    assert app._unpersisted_rendered_message_ids == {"message-1": 1}

    assert app._fetch_pending_mentor_receipts() is False
    assert app.rendered == ["message-1"]
    assert app.posted == ["message-1"]
    assert app._unpersisted_rendered_message_ids == {}
    assert app._last_seen_mentor_message_id == 1


def test_persisted_cursor_does_not_skip_earlier_unpersisted_render_after_restart(tmp_path, monkeypatch):
    state_path = tmp_path / "float_state.json"
    real_replace = floating_native.os.replace
    replacements = 0

    def fail_first_two_replaces(source, destination):
        nonlocal replacements
        replacements += 1
        if replacements <= 2:
            raise OSError("m1 state unavailable")
        return real_replace(source, destination)

    monkeypatch.setattr(floating_native.os, "replace", fail_first_two_replaces)

    class FakeApp:
        _handle_mentor_message = CopilotNativeApp._handle_mentor_message
        _ack_mentor_message = CopilotNativeApp._ack_mentor_message
        _load_mentor_message_state = CopilotNativeApp._load_mentor_message_state
        _save_mentor_message_state = CopilotNativeApp._save_mentor_message_state

        def __init__(self):
            self._student_id = "student-a"
            self._seen_mentor_message_ids = set()
            self._seen_mentor_message_order = []
            self._pending_receipt_message_ids = set()
            self._unpersisted_rendered_message_ids: dict[str, int] = {}
            self._rendering_mentor_message_ids = set()
            self._receipt_ack_inflight_ids = set()
            self._mentor_message_state_lock = threading.RLock()
            self._last_seen_mentor_message_id = 0
            self.rendered: list[str] = []
            self.posted: list[str] = []

        def _mentor_message_state_path(self):
            return str(state_path)

        def _render_mentor_message(self, item):
            self.rendered.append(item["message_id"])
            return True

        def _post_json(self, path, payload, *, timeout):
            self.posted.append(payload["message_id"])
            return {"ok": True}

    first = FakeApp()
    m1 = {
        "type": "mentor_message",
        "student_id": "student-a",
        "message_id": "message-1",
        "id": 1,
    }
    m2 = {
        "type": "mentor_message",
        "student_id": "student-a",
        "message_id": "message-2",
        "id": 2,
    }
    first._handle_mentor_message(m1)
    first._handle_mentor_message(m2)

    persisted = json.loads(state_path.read_text(encoding="utf-8"))["student-a"]
    assert persisted["last_seen_message_id"] == 0

    restarted = FakeApp()
    restarted._load_mentor_message_state()
    restarted._handle_mentor_message(m1)

    assert restarted._last_seen_mentor_message_id == 1
    assert restarted.rendered == ["message-1"]
    assert restarted.posted == ["message-1"]
