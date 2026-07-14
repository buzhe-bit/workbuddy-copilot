"""Real component coverage for the hook -> agent -> server -> mentor path.

This suite intentionally avoids FastAPI's TestClient and browser route mocks.
Each case owns a temporary HOME, database, spool, loopback socket and browser
context, so the protocol is exercised as it runs in the student deployment.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from copilot.app_context import AppContext
from copilot.connections import WSRegistry
from copilot.eventbus import EventBus
from copilot.service import create_app
from copilot.services import AnalysisService, MessageService, SessionQueryService
from copilot.store import Store
from copilot.student_core.agent import StudentAgent
from copilot.student_core.coordinator import StudentCoordinator
from copilot.student_core.spool import EventSpool
from copilot.student_core.transport import StudentTransport
from copilot.upload_service import UploadRequestService


STUDENT_ID = "student-e2e"
STUDENT_TOKEN = "student-e2e-token"
MENTOR_TOKEN = "mentor-e2e-token"


async def _deterministic_llm(_config, snapshot, event, latest_prompt):
    """The sole permitted replacement: deterministic external LLM output."""
    assert event == "Stop"
    assert snapshot.session_id
    assert latest_prompt
    return {
        "topic": "system e2e",
        "understanding": "low",
        "off_topic": False,
        "stuck_at": "real component boundary",
        "is_technical": True,
        "severity": "warn",
        "diagnosis": "真实 Agent 已将诊断写入服务端。",
        "suggestion": "保持事件落盘后再交给常驻 Agent。",
        "progress": "round trip complete",
        "guidance": "inspect the persisted timeline",
        "alert": "",
        "ai_reply_summary": "学生端事件已经经由真实链路抵达导师台。",
    }


def _transcript(session_id: str, *, title: str) -> str:
    return "\n".join(
        json.dumps(item, ensure_ascii=False)
        for item in (
            {"type": "ai-title", "aiTitle": title},
            {
                "type": "message",
                "role": "user",
                "content": "真实链路里的学员提问",
                "sessionId": session_id,
                "cwd": "/e2e/workspace",
            },
            {
                "type": "message",
                "role": "assistant",
                "content": "先验证端到端的状态闭环。",
            },
        )
    ) + "\n"


class _AgentRuntime:
    """Keep the real StudentAgent on its own event-loop thread."""

    def __init__(
        self,
        spool_dir: Path,
        base_url: str,
        *,
        message_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.agent = StudentAgent(
            StudentCoordinator(
                EventSpool(spool_dir),
                StudentTransport(
                    base_url,
                    student_id=STUDENT_ID,
                    token=STUDENT_TOKEN,
                    timeout=0.3,
                ),
                message_handler=message_handler,
            ),
            interval=0.02,
            stop_timeout=1.0,
        )
        self._call(self._start())

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coroutine: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout=3)

    async def _start(self) -> None:
        self.agent.start()

    def close(self) -> None:
        try:
            self._call(self._shutdown())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=3)
            self._loop.close()

    async def _shutdown(self) -> None:
        """Let websocket cleanup run before the isolated loop is closed."""
        await self.agent.stop()
        current = asyncio.current_task()
        remaining = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)


class _RejectingAckServer:
    """A real loopback failure endpoint; StudentTransport is not replaced."""

    def __init__(self) -> None:
        self.requests = 0
        self._request_seen = threading.Event()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - HTTP handler contract
                length = int(self.headers.get("Content-Length", "0") or 0)
                self.rfile.read(length)
                owner.requests += 1
                owner._request_seen.set()
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args: object) -> None:
                return

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        host, port = self._httpd.server_address
        self.base_url = f"http://{host}:{port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def request_seen(self) -> bool:
        return self._request_seen.is_set()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3)


class StudentAgentSystem:
    """A fully-real loopback system with controlled lifecycle operations."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.spool_dir = tmp_path / "spool"
        self.home.mkdir()
        # Browser binaries are an installed test-runtime dependency, not
        # student state. Preserve their explicit cache root while isolating
        # every HOME-derived application path below.
        browser_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(
            Path(os.environ.get("HOME", str(Path.home())))
            / "Library"
            / "Caches"
            / "ms-playwright"
        )
        for name, value in {
            "HOME": self.home,
            "USERPROFILE": self.home,
            "APPDATA": self.home / "AppData",
            "PLAYWRIGHT_BROWSERS_PATH": browser_cache,
        }.items():
            monkeypatch.setenv(name, str(value))
        self.store = Store(tmp_path / "copilot.db")
        self.app = self._build_app()
        self.base_url = ""
        self._listener: socket.socket | None = None
        self._server: uvicorn.Server | None = None
        self._server_thread: threading.Thread | None = None
        self._agent: _AgentRuntime | None = None
        self.rendered_message_ids: list[str] = []
        self._rejecting_ack_server: _RejectingAckServer | None = None
        self._playwright = None
        self._browser = None
        self._browser_context = None
        self.page = None
        self.start_server()
        self.start_agent()

    def _build_app(self):
        bus = EventBus()
        registry = WSRegistry(send_timeout=0.5)
        bus.subscribe(registry.handle_event)
        config = {
            "student_id": STUDENT_ID,
            "student_name": "系统 E2E 学员",
            "store": {"db_path": str(self.store.db_path)},
            "auth": {
                "mode": "public",
                "student_token": STUDENT_TOKEN,
                "mentor_token": MENTOR_TOKEN,
            },
        }
        context = AppContext(
            config=config,
            store=self.store,
            analysis_svc=AnalysisService(self.store, _deterministic_llm, config, bus),
            session_svc=SessionQueryService(self.store, config),
            message_svc=MessageService(self.store, bus),
            bus=bus,
            ws_registry=registry,
            upload_svc=UploadRequestService(self.store),
        )
        return create_app(context)

    def start_server(self) -> None:
        assert self._server is None
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        host, port = listener.getsockname()
        self.base_url = f"http://{host}:{port}"
        server = uvicorn.Server(
            uvicorn.Config(self.app, host=host, port=port, log_level="warning", lifespan="on")
        )
        thread = threading.Thread(
            target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True
        )
        self._listener = listener
        self._server = server
        self._server_thread = thread
        thread.start()
        self.wait_for(self._health_is_up, description="uvicorn health")

    def stop_server(self) -> None:
        server, thread = self._server, self._server_thread
        if server is None:
            return
        server.should_exit = True
        assert thread is not None
        thread.join(timeout=3)
        if thread.is_alive():
            raise AssertionError("uvicorn did not stop")
        self._server = None
        self._server_thread = None
        self._listener = None

    def start_agent(self) -> None:
        assert self._agent is None
        self._agent = _AgentRuntime(
            self.spool_dir,
            self.base_url,
            message_handler=lambda payload: self.rendered_message_ids.append(
                str(payload.get("message_id") or "")
            ),
        )

    def stop_agent(self) -> None:
        if self._agent is None:
            return
        self._agent.close()
        self._agent = None

    def restart_agent(self) -> None:
        self.stop_agent()
        self.start_agent()

    def force_agent_ack_failure(self) -> None:
        """Route only the real Agent's REST receipt at a real 503 endpoint.

        Its already-connected WebSocket remains attached to the production
        loopback server, so this isolates the receipt boundary without
        replacing StudentTransport, StudentAgent, WSRegistry, or the service.
        """
        self.wait_for_student_ws()
        assert self._agent is not None
        self._rejecting_ack_server = _RejectingAckServer()
        self._agent.agent.coordinator.transport.base_url = self._rejecting_ack_server.base_url

    def restore_agent_ack_route(self) -> None:
        """Restore the real server endpoint after proving a receipt failure."""
        assert self._agent is not None
        self._agent.agent.coordinator.transport.base_url = self.base_url
        if self._rejecting_ack_server is not None:
            self._rejecting_ack_server.close()
            self._rejecting_ack_server = None

    def restart_agent_after_receipt_failure(self) -> None:
        """Model a process restart after a persisted-but-unconfirmed render."""
        self.stop_agent()
        if self._rejecting_ack_server is not None:
            self._rejecting_ack_server.close()
            self._rejecting_ack_server = None
        self.start_agent()

    @property
    def rejected_ack_request_seen(self) -> bool:
        return bool(self._rejecting_ack_server and self._rejecting_ack_server.request_seen)

    def run_hook(
        self,
        *,
        event: str,
        session_id: str,
        prompt: str,
        title: str | None = None,
    ) -> None:
        transcript = self.root / f"{session_id}.jsonl"
        transcript.write_text(_transcript(session_id, title=title or session_id), encoding="utf-8")
        hook_input = {
            "hook_event_name": event,
            "session_id": session_id,
            "prompt": prompt,
            "cwd": "/e2e/workspace",
            "transcript_path": str(transcript),
        }
        config_path = self.root / "hook-config.json"
        config_path.write_text(json.dumps({"student_id": STUDENT_ID}), encoding="utf-8")
        env = {
            **os.environ,
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "APPDATA": str(self.home / "AppData"),
            "COPILOT_CONFIG": str(config_path),
            "COPILOT_SPOOL_DIR": str(self.spool_dir),
            "COPILOT_STUDENT_ID": STUDENT_ID,
        }
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[2] / "copilot" / "hook.py")],
            input=json.dumps(hook_input),
            text=True,
            env=env,
            capture_output=True,
            timeout=2,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def spool_count(self) -> int:
        return len(list(self.spool_dir.glob("*.json"))) if self.spool_dir.exists() else 0

    def wait_for_spool_empty(self) -> None:
        self.wait_for(lambda: self.spool_count() == 0, description="spool acknowledgement")

    def wait_for_report(self, session_id: str, *, event: str) -> None:
        def report_exists() -> bool:
            with self.store._conn() as conn:
                row = conn.execute(
                    "SELECT 1 FROM reports WHERE student_id = ? AND session_id = ? AND event = ?",
                    (STUDENT_ID, session_id, event),
                ).fetchone()
            return row is not None

        self.wait_for(report_exists, description=f"persisted {event} report")

    def wait_for_student_ws(self) -> None:
        registry = self.app.state.context.ws_registry
        self.wait_for(
            lambda: bool(registry.floats.get(STUDENT_ID)),
            description="persistent StudentAgent WebSocket",
        )

    def open_mentor_page(self):
        if self.page is not None:
            return self.page
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch()
        self._browser_context = self._browser.new_context()
        self._browser_context.add_init_script(
            "sessionStorage.setItem('workbuddy_copilot_mentor_token', %s);"
            % json.dumps(MENTOR_TOKEN)
        )
        self.page = self._browser_context.new_page()
        self.page.goto(f"{self.base_url}/mentor/", wait_until="domcontentloaded")
        expect(self.page.locator("#ws-status")).to_have_text("已连接", timeout=5_000)
        return self.page

    def reload_mentor_page(self):
        assert self.page is not None
        self.page.reload(wait_until="domcontentloaded")
        expect(self.page.locator("#ws-status")).to_have_text("已连接", timeout=5_000)

    def wait_for(self, condition: Callable[[], bool], *, description: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            try:
                if condition():
                    return
            except BaseException as exc:  # retry startup-only connection errors
                last_error = exc
            time.sleep(0.02)
        suffix = f" ({type(last_error).__name__}: {last_error})" if last_error else ""
        raise AssertionError(f"timed out waiting for {description}{suffix}")

    def _health_is_up(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=0.2) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def close(self) -> None:
        if self._browser_context is not None:
            self._browser_context.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        if self._rejecting_ack_server is not None:
            self._rejecting_ack_server.close()
        self.stop_agent()
        self.stop_server()


@pytest.fixture
def system(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime = StudentAgentSystem(tmp_path, monkeypatch)
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.mark.critical
@pytest.mark.component
@pytest.mark.server
@pytest.mark.core
def test_hook_agent_server_browser_round_trip(system: StudentAgentSystem) -> None:
    """A real Hook event becomes a persisted diagnosis visible in Mentor UI."""
    page = system.open_mentor_page()
    system.run_hook(
        event="Stop",
        session_id="session-round-trip",
        prompt="请确认真实组件是否已经闭环",
        title="真实闭环会话",
    )

    system.wait_for_spool_empty()
    system.wait_for_report("session-round-trip", event="Stop")
    system.wait_for(
        lambda: system.store.recent_analyses(STUDENT_ID, session_id="session-round-trip"),
        description="background analysis",
    )

    # The page talks to the same uvicorn process; no route interception is used.
    system.reload_mentor_page()
    student = page.locator(f'[data-student-id="{STUDENT_ID}"]')
    expect(student).to_be_visible()
    student.click()
    session = page.locator('[data-session-id="session-round-trip"]')
    expect(session).to_be_visible()
    session.click()
    expect(page.locator(".timeline")).to_contain_text("真实 Agent 已将诊断写入服务端。")
    expect(page.locator(".timeline")).to_contain_text("学生端事件已经经由真实链路抵达导师台。")


@pytest.mark.critical
@pytest.mark.component
@pytest.mark.server
@pytest.mark.core
def test_offline_spool_recovers_after_server_and_agent_restart(system: StudentAgentSystem) -> None:
    """A Hook event is retained while offline and delivered after both restarts."""
    system.stop_server()
    system.run_hook(
        event="UserPromptSubmit",
        session_id="session-offline-recovery",
        prompt="离线事件不能丢失",
        title="离线恢复会话",
    )
    system.wait_for(lambda: system.spool_count() == 1, description="offline local spool")

    system.start_server()
    system.restart_agent()
    system.wait_for_spool_empty()
    system.wait_for_report("session-offline-recovery", event="UserPromptSubmit")

    page = system.open_mentor_page()
    student = page.locator(f'[data-student-id="{STUDENT_ID}"]')
    expect(student).to_be_visible()
    student.click()
    session = page.locator('[data-session-id="session-offline-recovery"]')
    expect(session).to_be_visible()
    session.click()
    expect(page.locator(".timeline")).to_contain_text("离线事件不能丢失")


@pytest.mark.critical
@pytest.mark.component
@pytest.mark.server
@pytest.mark.core
def test_mentor_message_reaches_real_agent_and_browser_receives_delivery_receipt(
    system: StudentAgentSystem,
) -> None:
    """Mentor DOM -> real WS -> StudentAgent receipt -> real mentor WS round-trip."""
    system.run_hook(
        event="UserPromptSubmit",
        session_id="session-receipt",
        prompt="先让导师台知道这个会话",
        title="送达回执会话",
    )
    system.wait_for_spool_empty()
    system.wait_for_report("session-receipt", event="UserPromptSubmit")

    page = system.open_mentor_page()
    student = page.locator(f'[data-student-id="{STUDENT_ID}"]')
    expect(student).to_be_visible()
    student.click()
    session = page.locator('[data-session-id="session-receipt"]')
    expect(session).to_be_visible()
    session.click()

    page.locator("#compose-input").fill("请先缩小问题范围")
    page.locator("#compose").press("Enter")
    expect(page.locator(".timeline")).to_contain_text("请先缩小问题范围")
    expect(page.locator(".timeline")).to_contain_text("✓ 已展示", timeout=5_000)

    def message_was_receipted() -> bool:
        with system.store._conn() as conn:
            row = conn.execute(
                "SELECT delivered_at FROM mentor_messages WHERE student_id = ? ORDER BY id DESC LIMIT 1",
                (STUDENT_ID,),
            ).fetchone()
        return row is not None and row[0] is not None

    system.wait_for(message_was_receipted, description="real StudentAgent message receipt")


@pytest.mark.critical
@pytest.mark.component
@pytest.mark.server
@pytest.mark.core
def test_failed_student_transport_receipt_recovers_from_real_backlog_without_false_delivery(
    system: StudentAgentSystem,
) -> None:
    """A real failing StudentTransport.ack_message keeps DB and DOM pending."""
    system.run_hook(
        event="UserPromptSubmit",
        session_id="session-receipt-failure",
        prompt="先建立真实 WS 学员连接",
        title="回执失败会话",
    )
    system.wait_for_spool_empty()
    system.wait_for_report("session-receipt-failure", event="UserPromptSubmit")

    page = system.open_mentor_page()
    student = page.locator(f'[data-student-id="{STUDENT_ID}"]')
    expect(student).to_be_visible()
    student.click()
    session = page.locator('[data-session-id="session-receipt-failure"]')
    expect(session).to_be_visible()
    session.click()
    system.force_agent_ack_failure()

    page.locator("#compose-input").fill("这条消息不能伪报送达")
    page.locator("#compose").press("Enter")
    system.wait_for(lambda: system.rejected_ack_request_seen, description="real rejected receipt POST")

    expect(page.locator(".timeline")).to_contain_text("这条消息不能伪报送达")
    expect(page.locator(".timeline")).to_contain_text("发送中…")
    assert "✓ 已展示" not in page.locator(".timeline").inner_text()
    with system.store._conn() as conn:
        row = conn.execute(
            "SELECT delivered_at FROM mentor_messages WHERE student_id = ? ORDER BY id DESC LIMIT 1",
            (STUDENT_ID,),
        ).fetchone()
    assert row is not None and row[0] is None
    assert system.rendered_message_ids == [next(
        item["message_id"]
        for item in system.store.list_messages_since(STUDENT_ID, 0)
        if item["text"] == "这条消息不能伪报送达"
    )]

    system.restart_agent_after_receipt_failure()
    def recovered_message_was_receipted() -> bool:
        with system.store._conn() as conn:
            recovered = conn.execute(
                "SELECT delivered_at FROM mentor_messages WHERE student_id = ? ORDER BY id DESC LIMIT 1",
                (STUDENT_ID,),
            ).fetchone()
        return recovered is not None and recovered[0] is not None

    system.wait_for(recovered_message_was_receipted, description="recovered StudentAgent receipt backlog")
    expect(page.locator(".timeline")).to_contain_text("✓ 已展示", timeout=5_000)
    assert len(system.rendered_message_ids) == 1
