"""Windows native-view delivery contracts through the real Student Core.

The renderer is deliberately fake, but it can run only on the test's owner
thread.  ``BoundedUiBridge`` therefore exercises the same agent-thread -> Tk
main-thread boundary as the Windows composition root while the production
``WindowsStudentRuntime`` owns receipt/cursor durability.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import threading
import time
from typing import Any, Mapping

import pytest

from copilot.floating_windows import WindowsMessageStore, WindowsStudentView
from copilot.student_core.transport import Accepted, StudentAskNotFound
from copilot.student_platform.windows_runtime import WindowsStudentRuntime
from start_windows_client import BoundedUiBridge, WindowsClientSupervisor


pytestmark = [
    pytest.mark.windows,
    pytest.mark.component,
    pytest.mark.critical,
]

STUDENT_ID = "student-a"


class _DataAdapter:
    def list_sessions(self) -> list[Any]:
        return []


class _Transport:
    student_id = STUDENT_ID

    def __init__(self) -> None:
        self.ack_calls: list[tuple[str, str]] = []
        self.ask_queries: list[str] = []
        self.ask_posts: list[tuple[str, str, str]] = []
        self.recovered_asks: dict[str, dict[str, Any]] = {}

    async def ack_message_async(
        self,
        message_id: str,
        *,
        student_id: str,
    ) -> Accepted:
        self.ack_calls.append((student_id, message_id))
        return Accepted(200, {"ok": True})

    async def get_ask_by_client_request_async(
        self,
        client_request_id: str,
    ) -> dict[str, Any]:
        self.ask_queries.append(client_request_id)
        try:
            return dict(self.recovered_asks[client_request_id])
        except KeyError:
            raise StudentAskNotFound("missing ask reservation") from None

    async def ask_async(
        self,
        question: str,
        *,
        session_id: str,
        client_request_id: str,
    ) -> Accepted:
        self.ask_posts.append((question, session_id, client_request_id))
        return Accepted(
            200,
            {
                "ask_id": 99,
                "status": "answered",
                "answer": "posted",
                "client_request_id": client_request_id,
            },
        )


class _MainThreadRenderer:
    def __init__(self) -> None:
        self.owner_thread = threading.get_ident()
        self.fail_next = 0
        self.states: list[Any] = []

    def render(self, state: Any) -> None:
        if threading.get_ident() != self.owner_thread:
            raise AssertionError("native view rendered outside the UI owner thread")
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("native renderer unavailable")
        self.states.append(state)


class _RuntimeHarness:
    def __init__(self, root: Path, *, transport: _Transport | None = None) -> None:
        self.transport = transport or _Transport()
        self.bridge = BoundedUiBridge(max_pending=8)
        self.renderer = _MainThreadRenderer()
        self.store = WindowsMessageStore(
            root / "windows-ui.sqlite3",
            student_id=STUDENT_ID,
        )
        self.view = WindowsStudentView(self.store, renderer=self.renderer)
        self.runtime = WindowsStudentRuntime.build(
            base_url="https://copilot.example",
            student_id=STUDENT_ID,
            token="token-a",
            spool_dir=root / "spool",
            state_dir=root / "state",
            data_adapter=_DataAdapter(),
            transport=self.transport,
            message_handler=self._present_message,
            analysis_handler=self._present_analysis,
        )

    def _present_message(self, payload: Mapping[str, Any]) -> Any:
        return self.bridge.call(
            self.view.present_mentor_message,
            dict(payload),
            timeout=1.0,
        )

    def _present_analysis(self, payload: Mapping[str, Any]) -> Any:
        return self.bridge.call(
            self.view.present_analysis,
            dict(payload),
            timeout=1.0,
        )

    def run(self, coroutine: Any) -> Any:
        result: list[Any] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                result.append(asyncio.run(coroutine))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker, name="windows-agent-test")
        thread.start()
        deadline = time.monotonic() + 3.0
        while thread.is_alive() and time.monotonic() < deadline:
            self.bridge.pump(limit=16)
            time.sleep(0.001)
        thread.join(timeout=0.2)
        if thread.is_alive():
            raise AssertionError("Windows agent delivery did not finish")
        self.bridge.pump(limit=16)
        if errors:
            raise errors[0]
        assert len(result) == 1
        return result[0]


def _mentor(message_id: str = "message-1") -> dict[str, Any]:
    return {
        "type": "mentor_message",
        "student_id": STUDENT_ID,
        "message_id": message_id,
        "content": "inspect the failed assertion",
        "timestamp": 1_720_000_000.0,
    }


def _analysis() -> dict[str, Any]:
    return {
        "type": "analysis_result",
        "student_id": STUDENT_ID,
        "analysis_id": 31,
        "report_id": 7,
        "session_id": "session-a",
        "result": {"diagnosis": "retry the durable render"},
        "timestamp": 1_720_000_001.0,
    }


def test_message_store_to_receipt_ledger_crash_replays_to_one_visible_card(
    tmp_path: Path,
) -> None:
    harness = _RuntimeHarness(tmp_path)
    payload = _mentor()
    ledger = harness.runtime.coordinator.receipt_ledger
    mark_rendered = ledger.mark_rendered

    def crash_between_ui_store_and_receipt_ledger(
        _student_id: str,
        _message_id: str,
    ) -> None:
        raise OSError("simulated process loss after UI commit")

    ledger.mark_rendered = crash_between_ui_store_and_receipt_ledger
    assert harness.run(harness.runtime.coordinator.handle_message(payload)) is False
    assert harness.transport.ack_calls == []
    assert [item.state for item in harness.store.list_mentor()] == ["ack_pending"]
    assert len(harness.renderer.states[-1].mentor_messages) == 1

    ledger.mark_rendered = mark_rendered
    assert harness.run(harness.runtime.coordinator.handle_message(payload)) is True
    assert harness.store.reconcile_mentor_receipts(ledger) == 1

    # A duplicate arriving later through the REST catch-up path is absorbed by
    # the same Student Core and keyed UI-store identities used for live WS.
    assert harness.run(harness.runtime.coordinator.handle_message(payload)) is False
    assert harness.transport.ack_calls == [(STUDENT_ID, "message-1")]
    items = harness.store.list_mentor()
    assert len(items) == 1
    assert items[0].state == "acked"
    assert all(len(state.mentor_messages) == 1 for state in harness.renderer.states)


def test_renderer_failure_never_creates_a_false_mentor_ack(tmp_path: Path) -> None:
    harness = _RuntimeHarness(tmp_path)
    harness.renderer.fail_next = 1

    assert harness.run(
        harness.runtime.coordinator.handle_message(_mentor("message-render-fails"))
    ) is False

    assert harness.transport.ack_calls == []
    assert (
        harness.runtime.coordinator.receipt_ledger.status(
            STUDENT_ID,
            "message-render-fails",
        )
        is None
    )
    items = harness.store.list_mentor()
    assert len(items) == 1
    assert items[0].state == "unrendered"


def test_reused_mentor_id_with_different_payload_is_not_rendered_or_acked(
    tmp_path: Path,
) -> None:
    harness = _RuntimeHarness(tmp_path)
    original = _mentor("message-collision")
    harness.store.upsert_mentor(original, state="unrendered")
    tampered = {**original, "content": "different content under the same id"}

    assert harness.run(
        harness.runtime.coordinator.handle_message(tampered)
    ) is False

    assert harness.transport.ack_calls == []
    assert (
        harness.runtime.coordinator.receipt_ledger.status(
            STUDENT_ID,
            "message-collision",
        )
        is None
    )
    items = harness.store.list_mentor()
    assert len(items) == 1
    assert items[0].payload["content"] == original["content"]


def test_analysis_render_failure_preserves_cursor_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    harness = _RuntimeHarness(tmp_path)
    payload = _analysis()
    harness.renderer.fail_next = 1

    assert harness.run(harness.runtime.coordinator.handle_analysis(payload)) is False
    assert harness.runtime.analysis_store.cursor == 0
    assert harness.runtime.coordinator.analysis_cursor == 0
    assert [item.state for item in harness.store.list_analysis()] == ["unrendered"]

    assert harness.run(harness.runtime.coordinator.handle_analysis(payload)) is True
    assert harness.runtime.analysis_store.cursor == 31
    assert harness.runtime.coordinator.analysis_cursor == 31
    assert harness.run(harness.runtime.coordinator.handle_analysis(payload)) is False
    analyses = harness.store.list_analysis()
    assert len(analyses) == 1
    assert analyses[0].state == "rendered"
    assert len(harness.renderer.states[-1].analyses) == 1


def test_pending_ask_restart_recovers_by_same_client_request_without_repost(
    tmp_path: Path,
) -> None:
    ui_path = tmp_path / "windows-ui.sqlite3"
    first_store = WindowsMessageStore(ui_path, student_id=STUDENT_ID)
    first_view = WindowsStudentView(first_store, renderer=_MainThreadRenderer())
    first_view.update_sessions(
        [{"session_id": "session-a", "title": "Session A"}],
        active_session_id="session-a",
        active_reliable=True,
    )
    request = first_view.begin_ask(
        "why did the build fail?",
        client_request_id="ask-restart-1",
    )

    transport = _Transport()
    transport.recovered_asks["ask-restart-1"] = {
        "ask_id": 17,
        "status": "answered",
        "answer": "read the first compiler error",
        "client_request_id": "ask-restart-1",
    }
    runtime = WindowsStudentRuntime.build(
        base_url="https://copilot.example",
        student_id=STUDENT_ID,
        token="token-a",
        spool_dir=tmp_path / "spool",
        state_dir=tmp_path / "state",
        data_adapter=_DataAdapter(),
        transport=transport,
    )
    restarted_view = WindowsStudentView(
        WindowsMessageStore(ui_path, student_id=STUDENT_ID),
        renderer=_MainThreadRenderer(),
    )
    recovery = restarted_view.pending_ask_query()
    supervisor = WindowsClientSupervisor(
        SimpleNamespace(bridge_timeout=1.0),
        view=restarted_view,
        bridge=BoundedUiBridge(max_pending=2),
    )
    supervisor.runtime = runtime

    response = asyncio.run(
        supervisor._recover_or_ask(
            request["question"],
            session_id=recovery["session_id"],
            client_request_id=recovery["client_request_id"],
        )
    )
    restarted_view.resolve_ask(
        str(response["status"]),
        ask_id=int(response["ask_id"]),
        answer=str(response["answer"]),
    )

    assert recovery["action"] == "query"
    assert transport.ask_queries == ["ask-restart-1"]
    assert transport.ask_posts == []
    assert restarted_view.state.ask.client_request_id == "ask-restart-1"
    assert restarted_view.state.ask.status == "answered"
