from __future__ import annotations

import ast
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from copilot.floating_windows import (
    Rect,
    TkWindowsStudentAdapter,
    WindowsMessageStore,
    WindowsStudentView,
    create_windows_ui_host,
    dpi_scale,
    panel_rect_for_anchor,
)
from copilot.student_core.spool import ReceiptLedger
from copilot.student_core.transport import (
    PermanentTransportError,
    TemporaryNetworkError,
)


class _Clock:
    def __init__(self, value: int = 1_000) -> None:
        self.value = value

    def __call__(self) -> int:
        self.value += 1
        return self.value


class _Renderer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.states: list[Any] = []

    def render(self, state: Any) -> None:
        if self.fail:
            raise RuntimeError("tk render failed")
        self.states.append(state)


def _mentor(message_id: str, *, text: str | None = None) -> dict[str, Any]:
    return {
        "type": "mentor_message",
        "student_id": "student-a",
        "message_id": message_id,
        "content": text or f"mentor {message_id}",
        "timestamp": 1_720_000_000.0,
    }


def _analysis(
    report_id: int,
    *,
    analysis_id: int | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "analysis_result",
        "student_id": "student-a",
        "report_id": report_id,
        "session_id": "session-a",
        "result": {"diagnosis": summary or f"analysis {report_id}"},
        "timestamp": 1_720_000_000.0,
    }
    if analysis_id is not None:
        payload["analysis_id"] = analysis_id
    return payload


def test_windows_module_has_no_macos_ui_imports() -> None:
    source = (Path(__file__).parents[1] / "copilot" / "floating_windows.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        str(node.module or "").split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    assert not ({"AppKit", "Foundation", "objc", "Cocoa"} & imported)


def test_message_store_upserts_mentor_and_analysis_idempotently(tmp_path: Path) -> None:
    store = WindowsMessageStore(
        tmp_path / "messages.sqlite3",
        student_id="student-a",
        clock_ns=_Clock(),
    )

    first = store.upsert_mentor(_mentor("message-1"))
    duplicate = store.upsert_mentor(_mentor("message-1"))
    analysis_first = store.upsert_analysis(_analysis(7, analysis_id=17))
    analysis_duplicate = store.upsert_analysis(_analysis(7, analysis_id=17))

    assert first.created is True
    assert duplicate.created is False
    assert analysis_first.created is True
    assert analysis_duplicate.created is False
    assert [item.key for item in store.list_mentor()] == ["message-1"]
    assert [item.key for item in store.list_analysis()] == ["analysis:17"]


def test_analysis_without_commit_id_deduplicates_by_report_id(tmp_path: Path) -> None:
    store = WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")

    assert store.upsert_analysis(_analysis(8)).created is True
    assert store.upsert_analysis(_analysis(8)).created is False

    assert [item.key for item in store.list_analysis()] == ["report:8"]


def test_stable_ids_reject_payload_collisions_without_overwriting_history(
    tmp_path: Path,
) -> None:
    store = WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")
    store.upsert_mentor(_mentor("message-1", text="first"))
    store.upsert_analysis(_analysis(7, analysis_id=17, summary="first"))

    with pytest.raises(ValueError, match="collision"):
        store.upsert_mentor(_mentor("message-1", text="tampered"))
    with pytest.raises(ValueError, match="collision"):
        store.upsert_analysis(
            _analysis(7, analysis_id=17, summary="tampered")
        )

    assert store.list_mentor()[0].payload["content"] == "first"
    assert store.list_analysis()[0].payload["result"]["diagnosis"] == "first"


@pytest.mark.parametrize(
    "payload",
    [
        {**_mentor("message-1"), "student_id": "student-b"},
        {**_analysis(1), "student_id": "student-b"},
    ],
)
def test_message_store_rejects_cross_student_payloads(
    tmp_path: Path,
    payload: dict[str, Any],
) -> None:
    store = WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")

    with pytest.raises(ValueError, match="identity"):
        if payload["type"] == "mentor_message":
            store.upsert_mentor(payload)
        else:
            store.upsert_analysis(payload)


def test_message_store_rejects_database_reuse_by_another_student(tmp_path: Path) -> None:
    path = tmp_path / "messages.sqlite3"
    WindowsMessageStore(path, student_id="student-a")

    with pytest.raises(ValueError, match="identity"):
        WindowsMessageStore(path, student_id="student-b")


def test_terminal_history_is_bounded_but_nonterminal_rows_are_never_pruned(
    tmp_path: Path,
) -> None:
    store = WindowsMessageStore(
        tmp_path / "messages.sqlite3",
        student_id="student-a",
        terminal_limit=3,
        clock_ns=_Clock(),
    )
    assert store.DEFAULT_TERMINAL_LIMIT == 300

    for index in range(5):
        message_id = f"terminal-{index}"
        store.upsert_mentor(_mentor(message_id))
        store.mark_mentor_state(message_id, "acked")
        store.upsert_analysis(_analysis(index + 1))
        store.mark_analysis_state(report_id=index + 1, state="rendered")
    for index in range(5):
        store.upsert_mentor(_mentor(f"unrendered-{index}"), state="unrendered")
        store.upsert_mentor(_mentor(f"ack-pending-{index}"), state="ack_pending")
        store.upsert_analysis(_analysis(100 + index), state="unrendered")

    mentor = store.list_mentor()
    analyses = store.list_analysis()
    assert sum(item.state == "acked" for item in mentor) == 3
    assert sum(item.state == "ack_pending" for item in mentor) == 5
    assert sum(item.state == "unrendered" for item in mentor) == 5
    assert sum(item.state == "rendered" for item in analyses) == 3
    assert sum(item.state == "unrendered" for item in analyses) == 5


def test_default_300_boundary_keeps_305_nonterminal_rows_per_stream(
    tmp_path: Path,
) -> None:
    store = WindowsMessageStore(
        tmp_path / "messages.sqlite3",
        student_id="student-a",
        clock_ns=_Clock(),
    )
    for index in range(305):
        store.upsert_mentor(_mentor(f"terminal-{index:03d}"), state="acked")
        store.upsert_analysis(_analysis(index + 1), state="rendered")
        store.upsert_mentor(
            _mentor(f"nonterminal-{index:03d}"),
            state=("ack_pending" if index % 2 else "unrendered"),
        )
        store.upsert_analysis(_analysis(10_000 + index), state="unrendered")

    mentor = store.list_mentor()
    analyses = store.list_analysis()
    assert sum(item.state == "acked" for item in mentor) == 300
    assert sum(item.state != "acked" for item in mentor) == 305
    assert sum(item.state == "rendered" for item in analyses) == 300
    assert sum(item.state != "rendered" for item in analyses) == 305


def test_terminal_pruning_uses_stable_id_when_timestamps_tie(tmp_path: Path) -> None:
    store = WindowsMessageStore(
        tmp_path / "messages.sqlite3",
        student_id="student-a",
        terminal_limit=2,
        clock_ns=lambda: 9,
    )
    for message_id in ("message-a", "message-c", "message-b"):
        store.upsert_mentor(_mentor(message_id))
        store.mark_mentor_state(message_id, "acked")

    assert [item.key for item in store.list_mentor()] == ["message-b", "message-c"]


def test_terminal_state_cannot_regress(tmp_path: Path) -> None:
    store = WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")
    store.upsert_mentor(_mentor("message-1"))
    store.mark_mentor_state("message-1", "acked")
    store.upsert_mentor(_mentor("message-1"), state="unrendered")
    store.upsert_analysis(_analysis(1), state="rendered")
    store.upsert_analysis(_analysis(1), state="unrendered")

    assert store.list_mentor()[0].state == "acked"
    assert store.list_analysis()[0].state == "rendered"


def test_store_survives_restart_with_unread_and_delivery_state(tmp_path: Path) -> None:
    path = tmp_path / "messages.sqlite3"
    store = WindowsMessageStore(path, student_id="student-a")
    store.upsert_mentor(_mentor("message-1"), state="ack_pending")
    store.upsert_analysis(_analysis(1), state="rendered")

    restarted = WindowsMessageStore(path, student_id="student-a")

    assert restarted.unread_count == 2
    assert restarted.list_mentor()[0].state == "ack_pending"
    assert restarted.list_analysis()[0].state == "rendered"
    restarted.mark_all_read()
    assert WindowsMessageStore(path, student_id="student-a").unread_count == 0


def test_ack_pending_messages_reconcile_idempotently_from_receipt_ledger(
    tmp_path: Path,
) -> None:
    store = WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")
    store.upsert_mentor(_mentor("message-1"), state="ack_pending")
    store.upsert_mentor(_mentor("message-2"), state="ack_pending")
    receipt_dir = tmp_path / "spool"
    receipt_dir.mkdir()
    ledger = ReceiptLedger(receipt_dir)
    ledger.mark_rendered("student-a", "message-1")

    assert store.reconcile_mentor_receipts(ledger) == 0
    assert {item.key: item.state for item in store.list_mentor()} == {
        "message-1": "ack_pending",
        "message-2": "ack_pending",
    }

    ledger.mark_acked("student-a", "message-1")
    assert store.reconcile_mentor_receipts(ledger) == 1
    assert store.reconcile_mentor_receipts(ledger) == 0
    assert {item.key: item.state for item in store.list_mentor()} == {
        "message-1": "acked",
        "message-2": "ack_pending",
    }


def test_presenter_persists_then_renders_and_only_then_marks_ack_pending(
    tmp_path: Path,
) -> None:
    path = tmp_path / "messages.sqlite3"
    failing = _Renderer(fail=True)
    failed_view = WindowsStudentView(
        WindowsMessageStore(path, student_id="student-a"),
        renderer=failing,
    )

    with pytest.raises(RuntimeError, match="render failed"):
        failed_view.present_mentor_message(_mentor("message-1"))

    after_failure = WindowsMessageStore(path, student_id="student-a")
    assert after_failure.list_mentor()[0].state == "unrendered"

    renderer = _Renderer()
    restarted_view = WindowsStudentView(after_failure, renderer=renderer)
    assert restarted_view.present_mentor_message(_mentor("message-1")) is False
    assert after_failure.list_mentor()[0].state == "ack_pending"
    assert len(renderer.states[-1].mentor_messages) == 1


def test_presenter_without_renderer_fails_closed_and_keeps_items_unrendered(
    tmp_path: Path,
) -> None:
    store = WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")
    view = WindowsStudentView(store)

    with pytest.raises(RuntimeError, match="renderer"):
        view.present_mentor_message(_mentor("message-1"))
    with pytest.raises(RuntimeError, match="renderer"):
        view.present_analysis(_analysis(1))

    assert store.list_mentor()[0].state == "unrendered"
    assert store.list_analysis()[0].state == "unrendered"


def test_duplicate_live_and_catchup_messages_remain_one_visible_card(tmp_path: Path) -> None:
    renderer = _Renderer()
    store = WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")
    view = WindowsStudentView(store, renderer=renderer)

    assert view.present_mentor_message(_mentor("message-1")) is True
    assert view.present_mentor_message(_mentor("message-1")) is False
    assert view.present_analysis(_analysis(1, analysis_id=10)) is True
    assert view.present_analysis(_analysis(1, analysis_id=10)) is False

    assert len(view.state.mentor_messages) == 1
    assert len(view.state.analyses) == 1
    assert store.list_mentor()[0].state == "ack_pending"
    assert store.list_analysis()[0].state == "rendered"


def test_opening_panel_clears_persisted_unread_badge(tmp_path: Path) -> None:
    renderer = _Renderer()
    store = WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")
    view = WindowsStudentView(store, renderer=renderer)
    view.present_mentor_message(_mentor("message-1"))
    view.present_analysis(_analysis(1))

    assert view.state.unread_count == 2
    view.open_panel()

    assert view.state.expanded is True
    assert view.state.unread_count == 0
    assert store.unread_count == 0


def test_open_panel_render_failure_does_not_clear_unread(tmp_path: Path) -> None:
    renderer = _Renderer()
    store = WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")
    view = WindowsStudentView(store, renderer=renderer)
    view.present_mentor_message(_mentor("message-1"))
    renderer.fail = True

    with pytest.raises(RuntimeError, match="render failed"):
        view.open_panel()

    assert store.unread_count == 1
    assert view.state.expanded is False


def test_close_panel_render_failure_restores_expanded_state(tmp_path: Path) -> None:
    renderer = _Renderer()
    view = WindowsStudentView(
        WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a"),
        renderer=renderer,
    )
    view.open_panel()
    renderer.fail = True

    with pytest.raises(RuntimeError, match="render failed"):
        view.close_panel()

    assert view.state.expanded is True


def test_expanded_panel_render_failure_keeps_new_delivery_unread(tmp_path: Path) -> None:
    renderer = _Renderer()
    store = WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")
    view = WindowsStudentView(store, renderer=renderer)
    view.open_panel()
    renderer.fail = True

    with pytest.raises(RuntimeError, match="render failed"):
        view.present_analysis(_analysis(1))

    assert store.unread_count == 1
    assert store.list_analysis()[0].state == "unrendered"


def test_restart_restore_renders_durable_history_without_duplicate_cards(tmp_path: Path) -> None:
    path = tmp_path / "messages.sqlite3"
    first = WindowsStudentView(
        WindowsMessageStore(path, student_id="student-a"),
        renderer=_Renderer(),
    )
    first.present_analysis(_analysis(1, analysis_id=11))
    first.present_analysis(_analysis(2, analysis_id=12))

    renderer = _Renderer()
    restarted = WindowsStudentView(
        WindowsMessageStore(path, student_id="student-a"),
        renderer=renderer,
    )
    state = restarted.restore()

    assert [item.key for item in state.analyses] == ["analysis:11", "analysis:12"]
    assert len(renderer.states[-1].analyses) == 2


def test_unreliable_active_session_never_impersonates_most_recent_session(
    tmp_path: Path,
) -> None:
    view = WindowsStudentView(
        WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")
    )
    sessions = [
        {"session_id": "older", "title": "Older", "last_activity_at": 10},
        {"session_id": "newest", "title": "Newest", "last_activity_at": 99},
    ]

    view.update_sessions(sessions, active_session_id="newest", active_reliable=False)

    assert view.state.selected_session_id is None
    assert view.state.session_selection_required is True
    view.select_session("older")
    assert view.state.selected_session_id == "older"
    assert view.state.session_selection_source == "manual"


def test_reliable_active_session_can_be_selected_automatically(tmp_path: Path) -> None:
    view = WindowsStudentView(
        WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")
    )
    sessions = [{"session_id": "active", "title": "Active"}]

    view.update_sessions(sessions, active_session_id="active", active_reliable=True)

    assert view.state.selected_session_id == "active"
    assert view.state.session_selection_source == "reliable_active"
    assert view.state.session_selection_required is False


@pytest.mark.parametrize("status", ["answered", "degraded", "failed"])
def test_ask_view_model_exposes_all_terminal_statuses(
    tmp_path: Path,
    status: str,
) -> None:
    renderer = _Renderer()
    view = WindowsStudentView(
        WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a"),
        renderer=renderer,
    )
    view.update_sessions(
        [{"session_id": "session-a", "title": "Session A"}],
        active_session_id="session-a",
        active_reliable=True,
    )
    request = view.begin_ask("为什么构建失败？", client_request_id="request-1")

    view.resolve_ask(
        status,
        ask_id=9 if status != "failed" else None,
        answer="请先看第一条错误" if status != "failed" else "",
        error_code="offline" if status == "failed" else "",
    )

    assert request == {
        "session_id": "session-a",
        "question": "为什么构建失败？",
        "client_request_id": "request-1",
    }
    assert view.state.ask.status == status


def test_ask_requires_explicit_session_when_active_session_is_unknown(tmp_path: Path) -> None:
    view = WindowsStudentView(
        WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")
    )
    view.update_sessions([{"session_id": "session-a", "title": "Session A"}])

    with pytest.raises(ValueError, match="session"):
        view.begin_ask("help", client_request_id="request-1")


def test_unresolved_feedback_failure_remains_retryable(tmp_path: Path) -> None:
    view = WindowsStudentView(
        WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")
    )
    view.update_sessions(
        [{"session_id": "session-a", "title": "Session A"}],
        active_session_id="session-a",
        active_reliable=True,
    )
    view.begin_ask("help", client_request_id="request-1")
    view.resolve_ask("degraded", ask_id=7, answer="临时建议")

    assert view.begin_feedback("unresolved") == {"ask_id": 7, "feedback": "unresolved"}
    view.finish_feedback(success=False, error_code="offline")
    assert view.state.ask.feedback_status == "failed"
    assert view.begin_feedback("unresolved") == {"ask_id": 7, "feedback": "unresolved"}
    view.finish_feedback(success=True)
    assert view.state.ask.feedback_status == "sent"


def test_pending_ask_survives_response_loss_and_restart_for_query_recovery(
    tmp_path: Path,
) -> None:
    path = tmp_path / "messages.sqlite3"
    renderer = _Renderer()
    first = WindowsStudentView(
        WindowsMessageStore(path, student_id="student-a"),
        renderer=renderer,
    )
    first.update_sessions(
        [{"session_id": "session-a", "title": "Session A"}],
        active_session_id="session-a",
        active_reliable=True,
    )
    first.begin_ask("为什么失败？", client_request_id="request-stable")
    # Simulate a lost POST response: no resolve_ask call is made.

    restarted = WindowsStudentView(
        WindowsMessageStore(path, student_id="student-a"),
        renderer=_Renderer(),
    )

    assert restarted.state.ask.status == "pending"
    assert restarted.state.ask.client_request_id == "request-stable"
    assert restarted.state.ask.question == "为什么失败？"
    assert restarted.state.ask.session_id == "session-a"
    assert restarted.pending_ask_query() == {
        "action": "query",
        "client_request_id": "request-stable",
        "session_id": "session-a",
    }
    with pytest.raises(RuntimeError, match="pending"):
        restarted.begin_ask("不要创建第二条", client_request_id="request-new")


def test_ask_resolution_and_feedback_state_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "messages.sqlite3"
    first = WindowsStudentView(
        WindowsMessageStore(path, student_id="student-a"),
        renderer=_Renderer(),
    )
    first.update_sessions(
        [{"session_id": "session-a", "title": "Session A"}],
        active_session_id="session-a",
        active_reliable=True,
    )
    first.begin_ask("help", client_request_id="request-1")
    first.resolve_ask("degraded", ask_id=7, answer="临时建议")
    first.begin_feedback("unresolved")
    first.finish_feedback(success=False, error_code="offline")

    restarted = WindowsStudentView(
        WindowsMessageStore(path, student_id="student-a"),
        renderer=_Renderer(),
    )
    assert restarted.state.ask.status == "degraded"
    assert restarted.state.ask.ask_id == 7
    assert restarted.state.ask.answer == "临时建议"
    assert restarted.state.ask.feedback == "unresolved"
    assert restarted.state.ask.feedback_status == "failed"
    assert restarted.state.ask.feedback_error_code == "offline"


def test_completed_client_request_id_cannot_regress_to_a_new_pending_ask(
    tmp_path: Path,
) -> None:
    store = WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")
    store.begin_ask(
        client_request_id="request-1",
        session_id="session-a",
        question="first",
    )
    store.resolve_ask(
        client_request_id="request-1",
        status="answered",
        ask_id=7,
        answer="done",
    )

    with pytest.raises(RuntimeError, match="client_request_id"):
        store.begin_ask(
            client_request_id="request-1",
            session_id="session-a",
            question="second",
        )
    assert store.load_ask().status == "answered"


@pytest.mark.parametrize(
    "client_request_id",
    ["a" * 129, "contains space", "contains/slash", "中文请求"],
)
def test_local_ask_rejects_client_request_ids_the_server_would_422(
    tmp_path: Path,
    client_request_id: str,
) -> None:
    store = WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")

    with pytest.raises(ValueError, match="client_request_id"):
        store.begin_ask(
            client_request_id=client_request_id,
            session_id="session-a",
            question="help",
        )

    assert store.load_ask() is None


def test_local_ask_accepts_server_client_request_id_boundaries(tmp_path: Path) -> None:
    store = WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a")
    request_id = "A" * 120 + "._:-1234"

    stored = store.begin_ask(
        client_request_id=request_id,
        session_id="session-a",
        question="help",
    )

    assert len(request_id) == 128
    assert stored.client_request_id == request_id


def test_begin_ask_is_durable_before_renderer_success(tmp_path: Path) -> None:
    path = tmp_path / "messages.sqlite3"
    renderer = _Renderer()
    first = WindowsStudentView(
        WindowsMessageStore(path, student_id="student-a"),
        renderer=renderer,
    )
    first.update_sessions(
        [{"session_id": "session-a", "title": "Session A"}],
        active_session_id="session-a",
        active_reliable=True,
    )
    renderer.fail = True

    with pytest.raises(RuntimeError, match="render failed"):
        first.begin_ask("help", client_request_id="request-1")

    restarted = WindowsStudentView(
        WindowsMessageStore(path, student_id="student-a"),
        renderer=_Renderer(),
    )
    assert restarted.pending_ask_query()["client_request_id"] == "request-1"


def test_focus_request_is_exposed_to_headless_renderer(tmp_path: Path) -> None:
    renderer = _Renderer()
    view = WindowsStudentView(
        WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a"),
        renderer=renderer,
    )

    view.focus_ask_input()

    assert renderer.states[-1].focus_target == "ask_input"


def test_dpi_and_multi_monitor_panel_geometry_are_deterministic() -> None:
    monitors = (
        Rect(-1920, 0, 1920, 1080),
        Rect(0, 0, 2560, 1440),
    )
    anchor = Rect(-60, 900, 48, 48)

    at_125 = panel_rect_for_anchor(anchor, (360, 520), monitors, dpi=120)
    at_150 = panel_rect_for_anchor(anchor, (360, 520), monitors, dpi=144)

    assert dpi_scale(120) == 1.25
    assert dpi_scale(144) == 1.5
    assert at_125.x >= -1920
    assert at_125.right <= 0
    assert at_125.y >= 0
    assert at_125.bottom <= 1080
    assert at_150.width == 540
    assert at_150.height == 780


class _FakeWindow:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.bindings: dict[str, Any] = {}
        self.x = 10
        self.y = 20

    def attributes(self, *args: Any) -> None:
        self.calls.append(("attributes", *args))

    def overrideredirect(self, value: bool) -> None:
        self.calls.append(("overrideredirect", value))

    def bind(self, event: str, callback: Any) -> None:
        self.bindings[event] = callback

    def winfo_x(self) -> int:
        return self.x

    def winfo_y(self) -> int:
        return self.y

    def geometry(self, value: str) -> None:
        self.calls.append(("geometry", value))

    def withdraw(self) -> None:
        self.calls.append(("withdraw",))

    def deiconify(self) -> None:
        self.calls.append(("deiconify",))

    def lift(self) -> None:
        self.calls.append(("lift",))


class _FakeLabel:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.options: dict[str, Any] = {}

    def configure(self, **kwargs: Any) -> None:
        self.options.update(kwargs)
        self.calls.append(("configure", kwargs))

    def place(self, **kwargs: Any) -> None:
        self.calls.append(("place", kwargs))

    def place_forget(self) -> None:
        self.calls.append(("place_forget",))


class _FakeText:
    def __init__(self) -> None:
        self.content = ""
        self.calls: list[tuple[Any, ...]] = []

    def configure(self, **kwargs: Any) -> None:
        self.calls.append(("configure", kwargs))

    def delete(self, start: str, end: str) -> None:
        self.content = ""

    def insert(self, index: str, content: str) -> None:
        self.content += content


class _FakeEntry:
    def __init__(self) -> None:
        self.focused = False
        self.focus_calls = 0
        self.value = ""

    def focus_set(self) -> None:
        self.focused = True
        self.focus_calls += 1

    def get(self) -> str:
        return self.value


class _FakeButton(_FakeLabel):
    def invoke(self) -> None:
        self.options["command"]()


class _FakeSelector(_FakeLabel):
    def __init__(self) -> None:
        super().__init__()
        self.value = ""
        self.bindings: dict[str, Any] = {}

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def bind(self, event: str, callback: Any) -> None:
        self.bindings[event] = callback

    def choose(self, value: str) -> None:
        self.value = value
        self.bindings["<<ComboboxSelected>>"](None)


def test_tk_adapter_is_topmost_draggable_and_renders_badge_headlessly(
    tmp_path: Path,
) -> None:
    icon = _FakeWindow()
    panel = _FakeWindow()
    badge = _FakeLabel()
    content = _FakeText()
    entry = _FakeEntry()
    adapter = TkWindowsStudentAdapter(
        icon_window=icon,
        panel_window=panel,
        unread_badge=badge,
        content_widget=content,
        ask_entry=entry,
    )
    view = WindowsStudentView(
        WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a"),
        renderer=adapter,
    )
    view.present_mentor_message(_mentor("message-1"))
    view.open_panel()
    view.focus_ask_input()

    press = type("Event", (), {"x_root": 20, "y_root": 35})()
    drag = type("Event", (), {"x_root": 100, "y_root": 120})()
    icon.bindings["<ButtonPress-1>"](press)
    icon.bindings["<B1-Motion>"](drag)

    assert ("attributes", "-topmost", True) in icon.calls
    assert ("overrideredirect", True) in icon.calls
    assert ("attributes", "-topmost", True) in panel.calls
    assert ("geometry", "+90+105") in icon.calls
    assert ("deiconify",) in panel.calls
    assert entry.focused is True
    assert "mentor message-1" in content.content


def test_tk_drag_formats_negative_multi_monitor_coordinates() -> None:
    icon = _FakeWindow()
    icon.x = -200
    icon.y = -100
    TkWindowsStudentAdapter(
        icon_window=icon,
        panel_window=_FakeWindow(),
        unread_badge=_FakeLabel(),
        content_widget=_FakeText(),
    )
    press = type("Event", (), {"x_root": -190, "y_root": -90})()
    drag = type("Event", (), {"x_root": -290, "y_root": -190})()

    icon.bindings["<ButtonPress-1>"](press)
    icon.bindings["<B1-Motion>"](drag)

    assert ("geometry", "-300-200") in icon.calls


def test_tk_adapter_projects_sessions_ask_feedback_and_callback_seams(
    tmp_path: Path,
) -> None:
    icon = _FakeWindow()
    panel = _FakeWindow()
    badge = _FakeLabel()
    content = _FakeText()
    entry = _FakeEntry()
    selector = _FakeSelector()
    ask_status = _FakeLabel()
    send = _FakeButton()
    helpful = _FakeButton()
    unresolved = _FakeButton()
    toggles: list[bool] = []
    selected: list[str] = []
    questions: list[str] = []
    feedback: list[str] = []
    adapter = TkWindowsStudentAdapter(
        icon_window=icon,
        panel_window=panel,
        unread_badge=badge,
        content_widget=content,
        ask_entry=entry,
        session_selector=selector,
        ask_status_widget=ask_status,
        ask_send_button=send,
        helpful_button=helpful,
        unresolved_button=unresolved,
        on_toggle=lambda: toggles.append(True),
        on_session_selected=selected.append,
        on_ask_submit=questions.append,
        on_feedback=feedback.append,
    )
    view = WindowsStudentView(
        WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a"),
        renderer=adapter,
    )
    view.update_sessions(
        [
            {"session_id": "session-a", "title": "Alpha"},
            {"session_id": "session-b", "title": "Beta"},
        ],
        active_session_id="session-a",
        active_reliable=True,
    )

    labels = selector.options["values"]
    assert len(labels) == 2
    assert "Alpha" in labels[0]
    selector.choose(labels[1])
    assert selected == ["session-b"]

    entry.value = "为什么失败？"
    send.invoke()
    icon.bindings["<ButtonRelease-1>"](
        type("Event", (), {"x_root": 10, "y_root": 20})()
    )
    assert questions == ["为什么失败？"]
    assert toggles == [True]

    pending = replace(
        view.state,
        ask=replace(view.state.ask, status="pending", question="为什么失败？"),
    )
    adapter.render(pending)
    assert send.options["state"] == "disabled"
    assert "处理中" in ask_status.options["text"]
    assert helpful.options["state"] == "disabled"
    degraded = replace(
        view.state,
        ask=replace(
            view.state.ask,
            status="degraded",
            ask_id=7,
            answer="临时建议",
        ),
    )
    adapter.render(degraded)
    assert "降级回答" in ask_status.options["text"]
    assert "临时建议" in ask_status.options["text"]
    assert helpful.options["state"] == "normal"
    assert unresolved.options["state"] == "normal"
    helpful.invoke()
    unresolved.invoke()
    assert feedback == ["helpful", "unresolved"]

    answered = replace(
        view.state,
        ask=replace(
            view.state.ask,
            status="answered",
            ask_id=8,
            answer="完整答案",
        ),
    )
    adapter.render(answered)
    assert "已回答" in ask_status.options["text"]
    assert "完整答案" in ask_status.options["text"]
    assert helpful.options["state"] == "normal"

    failed = replace(
        view.state,
        ask=replace(view.state.ask, status="failed", error_code="offline"),
    )
    adapter.render(failed)
    assert "失败" in ask_status.options["text"]
    assert "offline" in ask_status.options["text"]
    assert helpful.options["state"] == "disabled"


def test_focus_request_is_consumed_after_one_successful_tk_render(tmp_path: Path) -> None:
    entry = _FakeEntry()
    adapter = TkWindowsStudentAdapter(
        icon_window=_FakeWindow(),
        panel_window=_FakeWindow(),
        unread_badge=_FakeLabel(),
        content_widget=_FakeText(),
        ask_entry=entry,
    )
    view = WindowsStudentView(
        WindowsMessageStore(tmp_path / "messages.sqlite3", student_id="student-a"),
        renderer=adapter,
    )

    view.focus_ask_input()
    view.present_mentor_message(_mentor("message-1"))

    assert entry.focus_calls == 1


def test_tk_callback_errors_are_reported_without_tearing_down_ui_dispatch() -> None:
    errors: list[BaseException] = []

    def fail_toggle() -> None:
        raise RuntimeError("toggle failed")

    icon = _FakeWindow()
    TkWindowsStudentAdapter(
        icon_window=icon,
        panel_window=_FakeWindow(),
        unread_badge=_FakeLabel(),
        content_widget=_FakeText(),
        on_toggle=fail_toggle,
        on_callback_error=errors.append,
    )

    icon.bindings["<ButtonRelease-1>"](
        type("Event", (), {"x_root": 10, "y_root": 20})()
    )

    assert len(errors) == 1
    assert str(errors[0]) == "toggle failed"


class _HeadlessTkWidget(_FakeWindow):
    def __init__(self, parent: Any | None = None, **kwargs: Any) -> None:
        super().__init__()
        self.parent = parent
        self.options: dict[str, Any] = dict(kwargs)
        self.value = ""
        self.after_queue: list[Any] = []
        self.protocols: dict[str, Any] = {}
        self.mainloop_calls = 0

    def title(self, value: str) -> None:
        self.calls.append(("title", value))

    def configure(self, **kwargs: Any) -> None:
        self.options.update(kwargs)

    config = configure

    def pack(self, **kwargs: Any) -> None:
        self.calls.append(("pack", kwargs))

    def grid(self, **kwargs: Any) -> None:
        self.calls.append(("grid", kwargs))

    def columnconfigure(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("columnconfigure", *args, kwargs))

    def rowconfigure(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("rowconfigure", *args, kwargs))

    def place(self, **kwargs: Any) -> None:
        self.calls.append(("place", kwargs))

    def place_forget(self) -> None:
        self.calls.append(("place_forget",))

    def delete(self, start: str, end: str) -> None:
        self.value = ""

    def insert(self, index: str, value: str) -> None:
        self.value += value

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def focus_set(self) -> None:
        self.calls.append(("focus_set",))

    def protocol(self, name: str, callback: Any) -> None:
        self.protocols[name] = callback

    def after(self, milliseconds: int, callback: Any) -> None:
        self.after_queue.append(callback)

    def mainloop(self) -> None:
        self.mainloop_calls += 1
        for _ in range(8):
            if not self.after_queue:
                break
            callback = self.after_queue.pop(0)
            callback()

    def quit(self) -> None:
        self.after_queue.clear()

    def destroy(self) -> None:
        self.after_queue.clear()

    def winfo_screenwidth(self) -> int:
        return 1440

    def winfo_screenheight(self) -> int:
        return 900


class _HeadlessTkModule:
    Tk = _HeadlessTkWidget
    Toplevel = _HeadlessTkWidget
    Label = _HeadlessTkWidget
    Text = _HeadlessTkWidget
    Entry = _HeadlessTkWidget
    Button = _HeadlessTkWidget
    ttk = SimpleNamespace(Combobox=_HeadlessTkWidget)


def test_real_windows_ui_host_composes_widgets_and_runtime_recovery(tmp_path: Path) -> None:
    config = SimpleNamespace(
        state_dir=tmp_path,
        student_id="student-a",
        heartbeat_interval=0.1,
    )
    host = create_windows_ui_host(config, tk_module=_HeadlessTkModule)
    host.store.begin_ask(
        client_request_id="request-restart",
        session_id="session-a",
        question="lost response",
    )
    host.view.present_mentor_message(_mentor("message-1"))
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    ledger = ReceiptLedger(receipt_dir)
    ledger.mark_acked("student-a", "message-1")

    class Data:
        def list_sessions(self) -> list[Any]:
            return [
                SimpleNamespace(
                    to_dict=lambda: {
                        "session_id": "session-a",
                        "title": "Newest but not proven active",
                    }
                )
            ]

        def detect_active_session(self) -> Any:
            return SimpleNamespace(
                session_id=None,
                failure=SimpleNamespace(code="unknown_active_session"),
            )

    class Supervisor:
        def __init__(self) -> None:
            self.runtime = SimpleNamespace(
                data=Data(),
                coordinator=SimpleNamespace(receipt_ledger=ledger),
            )
            self.ask_calls: list[tuple[str, str, str]] = []
            self.pumps = 0

        def pump_ui(self, *, limit: int = 32) -> int:
            self.pumps += 1
            return 0

        def ask(
            self,
            question: str,
            *,
            session_id: str,
            client_request_id: str,
        ) -> Future[Any]:
            self.ask_calls.append((question, session_id, client_request_id))
            future: Future[Any] = Future()
            future.set_result(
                {"ask_id": 9, "status": "answered", "answer": "recovered"}
            )
            return future

        def feedback(self, ask_id: int, feedback: str) -> Future[Any]:
            future: Future[Any] = Future()
            future.set_result({"ask_id": ask_id, "feedback": feedback})
            return future

    supervisor = Supervisor()

    host.run(supervisor)

    assert host.root.mainloop_calls == 1
    assert supervisor.pumps > 0
    assert supervisor.ask_calls == [
        ("lost response", "session-a", "request-restart")
    ]
    assert host.view.state.ask.status == "answered"
    assert host.view.state.ask.answer == "recovered"
    assert host.view.state.selected_session_id is None
    assert host.view.state.session_selection_required is True
    assert host.store.list_mentor()[0].state == "acked"


def test_windows_ui_host_places_panel_on_the_icons_monitor_before_opening(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(state_dir=tmp_path, student_id="student-a")
    monitors = (
        Rect(-1920, 0, 1920, 1080),
        Rect(0, 0, 2560, 1440),
    )
    provider_calls: list[Any] = []

    def display_provider(window: Any) -> tuple[tuple[Rect, ...], float]:
        provider_calls.append(window)
        return monitors, 144.0

    host = create_windows_ui_host(
        config,
        tk_module=_HeadlessTkModule,
        display_provider=display_provider,
    )
    host.root.x = -80
    host.root.y = 900

    host._toggle_panel()

    expected = panel_rect_for_anchor(
        Rect(-80, 900, 56, 56),
        (420, 640),
        monitors,
        dpi=144,
    )
    expected_geometry = (
        f"{round(expected.width)}x{round(expected.height)}"
        f"{round(expected.x):+d}{round(expected.y):+d}"
    )
    assert provider_calls == [host.root]
    assert ("geometry", expected_geometry) in host.panel.calls
    assert ("deiconify",) in host.panel.calls
    assert ("focus_set",) in host.adapter.ask_entry.calls


def test_windows_ui_host_clamps_dragged_icon_to_nearest_monitor_on_release(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(state_dir=tmp_path, student_id="student-a")
    monitors = (
        Rect(-1920, 0, 1920, 1080),
        Rect(0, 0, 2560, 1440),
    )
    host = create_windows_ui_host(
        config,
        tk_module=_HeadlessTkModule,
        display_provider=lambda _window: (monitors, 120.0),
    )
    press = type("Event", (), {"x_root": -70, "y_root": 910})()
    drag = type("Event", (), {"x_root": -2190, "y_root": 1290})()
    host.root.x = -80
    host.root.y = 900
    host.adapter.drag_surface.bindings["<ButtonPress-1>"](press)
    host.adapter.drag_surface.bindings["<B1-Motion>"](drag)
    # Tk updates winfo_x/y after applying the motion geometry.  The headless
    # widget has no event loop, so expose the resulting off-screen position.
    host.root.x = -2200
    host.root.y = 1280

    host.adapter.drag_surface.bindings["<ButtonRelease-1>"](drag)

    assert host.root.calls[-1] == ("geometry", "-1920+1024")
    assert host.view.state.expanded is False


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_error_code"),
    [
        (
            PermanentTransportError("student ask rejected"),
            "failed",
            "permanent_transport_error",
        ),
        (
            RuntimeError("unexpected local agent failure"),
            "failed",
            "runtime_error",
        ),
        (TemporaryNetworkError("offline"), "pending", ""),
    ],
)
def test_windows_ui_host_retries_only_temporary_ask_failures(
    tmp_path: Path,
    error: Exception,
    expected_status: str,
    expected_error_code: str,
) -> None:
    config = SimpleNamespace(state_dir=tmp_path, student_id="student-a")
    host = create_windows_ui_host(config, tk_module=_HeadlessTkModule)
    host.store.begin_ask(
        client_request_id="request-error",
        session_id="session-a",
        question="will this recover?",
    )
    host.view.restore()
    future: Future[Any] = Future()
    future.set_exception(error)
    host._ask_future = future

    host._poll_ask(10.0)

    assert host.view.state.ask.status == expected_status
    assert host.view.state.ask.error_code == expected_error_code
    if expected_status == "pending":
        assert host._ask_retry_at == 12.0


def test_windows_ui_host_keeps_pending_ask_when_agent_is_temporarily_offline(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        state_dir=tmp_path,
        student_id="student-a",
        heartbeat_interval=0.1,
    )
    host = create_windows_ui_host(config, tk_module=_HeadlessTkModule)
    host.store.begin_ask(
        client_request_id="request-offline",
        session_id="session-a",
        question="uncertain post",
    )

    class Data:
        def list_sessions(self) -> list[Any]:
            return []

        def detect_active_session(self) -> Any:
            return SimpleNamespace(session_id=None, failure=RuntimeError("unknown"))

    class OfflineSupervisor:
        runtime = SimpleNamespace(data=Data(), coordinator=SimpleNamespace())

        def pump_ui(self, *, limit: int = 32) -> int:
            return 0

        def ask(self, *args: Any, **kwargs: Any) -> Future[Any]:
            raise OSError("agent loop unavailable")

    host.run(OfflineSupervisor())

    assert host.root.mainloop_calls == 1
    assert host.view.state.ask.status == "pending"
    assert host.view.pending_ask_query()["client_request_id"] == "request-offline"


def test_windows_ui_host_retries_persisted_pending_feedback_after_restart(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        state_dir=tmp_path,
        student_id="student-a",
        heartbeat_interval=0.1,
    )
    first = create_windows_ui_host(config, tk_module=_HeadlessTkModule)
    first.store.begin_ask(
        client_request_id="request-feedback",
        session_id="session-a",
        question="help",
    )
    first.store.resolve_ask(
        client_request_id="request-feedback",
        status="answered",
        ask_id=12,
        answer="done",
    )
    first.store.begin_feedback(
        client_request_id="request-feedback",
        feedback="unresolved",
    )
    restarted = create_windows_ui_host(config, tk_module=_HeadlessTkModule)

    class Data:
        def list_sessions(self) -> list[Any]:
            return []

        def detect_active_session(self) -> Any:
            return SimpleNamespace(session_id=None, failure=RuntimeError("unknown"))

    class Supervisor:
        def __init__(self) -> None:
            self.runtime = SimpleNamespace(data=Data(), coordinator=SimpleNamespace())
            self.feedback_calls: list[tuple[int, str]] = []

        def pump_ui(self, *, limit: int = 32) -> int:
            return 0

        def feedback(self, ask_id: int, feedback: str) -> Future[Any]:
            self.feedback_calls.append((ask_id, feedback))
            future: Future[Any] = Future()
            future.set_result({"ok": True})
            return future

    supervisor = Supervisor()
    restarted.run(supervisor)

    assert supervisor.feedback_calls == [(12, "unresolved")]
    assert restarted.view.state.ask.feedback_status == "sent"
