"""Hosted Windows student-runtime component contract.

This lane uses the production hook subprocess, SQLite stores, urllib transport,
Uvicorn socket and FastAPI routes.  Only the external LLM is deterministic.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Iterator

import pytest
import uvicorn

from copilot.student_core.spool import EventSpool
from copilot.student_platform.windows_runtime import WindowsStudentRuntime
from copilot.student_platform.workbuddy import TranscriptReadResult, WorkBuddySession


pytestmark = [
    pytest.mark.windows,
    pytest.mark.contract,
    pytest.mark.component,
    pytest.mark.critical,
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HOOK = PROJECT_ROOT / "copilot" / "hook.py"
STUDENT_ID = "windows-component-student"
STUDENT_TOKEN = "windows-component-token"


async def _deterministic_llm(_config, snapshot, event, latest_prompt):
    assert event == "Stop"
    assert snapshot.session_id or latest_prompt
    return {
        "topic": "windows hosted component",
        "understanding": "low",
        "off_topic": False,
        "stuck_at": "durable recovery boundary",
        "is_technical": True,
        "severity": "warn",
        "diagnosis": f"diagnosed {latest_prompt}",
        "suggestion": "inspect the persisted evidence",
        "progress": "report committed",
        "guidance": "continue from the durable cursor",
        "alert": "",
        "ai_reply_summary": "hosted component analysis",
        "confidence": 0.9,
        "evidence": ["the Stop report reached the loopback server"],
    }


def _transcript(session_id: str) -> str:
    return "\n".join(
        json.dumps(item, ensure_ascii=False)
        for item in (
            {
                "type": "message",
                "sessionId": session_id,
                "role": "user",
                "content": f"question from {session_id}",
                "cwd": "C:/study/workspace",
            },
            {
                "type": "message",
                "sessionId": session_id,
                "role": "assistant",
                "content": "use the durable outbox",
            },
        )
    ) + "\n"


class _WorkBuddyAdapter:
    def __init__(self, session_ids: list[str]) -> None:
        self.session_ids = list(session_ids)

    def list_sessions(self) -> list[WorkBuddySession]:
        return [
            WorkBuddySession(
                session_id=session_id,
                title=f"Hosted {session_id}",
                work_dir="C:/study/workspace",
                created_at=1.0,
                last_activity_at=2.0,
                deleted=False,
                group_type="task",
                space_name="Windows hosted",
            )
            for session_id in self.session_ids
        ]

    def read_transcript(self, session_id: str) -> TranscriptReadResult:
        if session_id not in self.session_ids:
            raise AssertionError(f"unexpected session {session_id}")
        return TranscriptReadResult(content=_transcript(session_id))


class _LoopbackServer:
    def __init__(self, root: Path) -> None:
        # Keep the server imports inside the hosted test.  On Windows this also
        # verifies that the production server composition is importable there.
        from copilot.app_context import AppContext
        from copilot.connections import WSRegistry
        from copilot.eventbus import EventBus
        from copilot.service import create_app
        from copilot.services import AnalysisService, MessageService, SessionQueryService
        from copilot.store import Store
        from copilot.upload_service import UploadRequestService

        self.store = Store(root / "server.sqlite3")
        bus = EventBus()
        registry = WSRegistry(send_timeout=0.5)
        bus.subscribe(registry.handle_event)
        self.bus = bus
        self.registry = registry
        config = {
            "student_id": STUDENT_ID,
            "student_name": "Windows component student",
            "store": {"db_path": str(self.store.db_path)},
            "auth": {
                "mode": "pilot",
                "student_tokens": {STUDENT_ID: STUDENT_TOKEN},
                "mentor_token": "mentor-component-token",
            },
            "service": {"analysis_max_concurrency": 2},
            "llm": {"enable_llm": True},
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
        self.app = create_app(context)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        host, port = listener.getsockname()
        self.base_url = f"http://{host}:{port}"
        self._listener = listener
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host=host,
                port=port,
                log_level="warning",
                lifespan="on",
            )
        )
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._server.serve(sockets=[listener])),
            daemon=True,
        )
        self._thread.start()
        self.wait_for(self._healthy, description="Uvicorn health")

    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=0.2) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    @staticmethod
    def wait_for(condition, *, description: str, timeout: float = 6.0) -> None:
        deadline = time.monotonic() + timeout
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            try:
                if condition():
                    return
            except BaseException as exc:
                last_error = exc
            time.sleep(0.02)
        suffix = f" ({type(last_error).__name__}: {last_error})" if last_error else ""
        raise AssertionError(f"timed out waiting for {description}{suffix}")

    def close(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise AssertionError("Uvicorn component server did not stop")


class _RunningStudentAgent:
    """Run the production StudentAgent owned by a Windows runtime."""

    def __init__(self, runtime: WindowsStudentRuntime) -> None:
        self.runtime = runtime
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        assert self._ready.wait(2), "StudentAgent event loop did not start"
        self._call(self._start())

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def _call(self, coroutine: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout=4)

    async def _start(self) -> None:
        self.runtime.agent.start()

    def close(self) -> None:
        try:
            self._call(self._shutdown())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=3)
            self._loop.close()

    async def _shutdown(self) -> None:
        await self.runtime.agent.stop()
        current = asyncio.current_task()
        remaining = [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)


@contextmanager
def _tail_read_failure_path(root: Path) -> Iterator[Path]:
    """Hold a no-share handle on Windows; use a deterministic OSError elsewhere."""
    if os.name != "nt":
        directory = root / "transcript-held-by-workbuddy"
        directory.mkdir()
        yield directory
        return

    import ctypes
    from ctypes import wintypes

    transcript = root / "transcript-held-by-workbuddy.jsonl"
    transcript.write_text(_transcript("session-locked"), encoding="utf-8")
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = ctypes.windll.kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(transcript),
        0x80000000,  # GENERIC_READ
        0,  # no sharing: the hook child must observe ERROR_SHARING_VIOLATION
        None,
        3,  # OPEN_EXISTING
        0x80,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        raise ctypes.WinError()
    try:
        yield transcript
    finally:
        if not close_handle(handle):
            raise ctypes.WinError()


def _run_hook(
    root: Path,
    spool_dir: Path,
    *,
    session_id: str,
    transcript_path: Path,
) -> subprocess.CompletedProcess[str]:
    config_path = root / "hook-config.json"
    config_path.write_text(json.dumps({"student_id": STUDENT_ID}), encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(root / "home"),
            "USERPROFILE": str(root / "home"),
            "APPDATA": str(root / "home" / "AppData"),
            "COPILOT_CONFIG": str(config_path),
            "COPILOT_SPOOL_DIR": str(spool_dir),
            "COPILOT_STUDENT_ID": STUDENT_ID,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "prompt": f"prompt {session_id}",
        "cwd": "C:/study/workspace",
        "transcript_path": str(transcript_path),
    }
    return subprocess.run(
        [sys.executable, str(HOOK)],
        cwd=PROJECT_ROOT,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=2,
        check=False,
    )


def test_windows_hosted_stop_outbox_store_only_and_analysis_recovery(
    tmp_path: Path,
) -> None:
    root = tmp_path
    (root / "home" / "AppData").mkdir(parents=True)
    spool_dir = root / "spool"
    state_dir = root / "state"
    session_ids = ["session-locked", "session-two", "session-three"]

    with _tail_read_failure_path(root) as locked_path:
        locked = _run_hook(
            root,
            spool_dir,
            session_id="session-locked",
            transcript_path=locked_path,
        )
    assert locked.returncode == 0, locked.stderr
    for session_id in session_ids[1:]:
        transcript = root / f"{session_id}.jsonl"
        transcript.write_text(_transcript(session_id), encoding="utf-8")
        completed = _run_hook(
            root,
            spool_dir,
            session_id=session_id,
            transcript_path=transcript,
        )
        assert completed.returncode == 0, completed.stderr

    first_spool = EventSpool(spool_dir)
    first_pending = first_spool.pending()
    assert len(first_pending) == 3
    locked_payload = next(
        entry.payload
        for entry in first_pending
        if entry.payload.session_id == "session-locked"
    )
    assert locked_payload.transcript_tail == ""
    assert locked_payload.transcript_path == ""
    # A brand-new object reads the same durable subprocess output.
    assert {entry.event_id for entry in EventSpool(spool_dir).pending()} == {
        entry.event_id for entry in first_pending
    }

    server = _LoopbackServer(root)
    try:
        adapter = _WorkBuddyAdapter(session_ids)
        runtime = WindowsStudentRuntime.build(
            base_url=server.base_url,
            student_id=STUDENT_ID,
            token=STUDENT_TOKEN,
            spool_dir=spool_dir,
            state_dir=state_dir,
            data_adapter=adapter,
        )
        assert asyncio.run(runtime.coordinator.flush_spool_once()) == 3
        assert runtime.spool.pending() == []
        initial_jobs = runtime.transcript_queue.pending()
        assert len(initial_jobs) == 3
        assert {job.session_id for job in initial_jobs} == set(session_ids)

        # Simulate terminating the student process after report acceptance but
        # before any full transcript supplement has been uploaded.
        restarted = WindowsStudentRuntime.build(
            base_url=server.base_url,
            student_id=STUDENT_ID,
            token=STUDENT_TOKEN,
            spool_dir=spool_dir,
            state_dir=state_dir,
            data_adapter=adapter,
        )
        assert {job.event_id for job in restarted.transcript_queue.pending()} == {
            job.event_id for job in initial_jobs
        }
        assert asyncio.run(restarted.drain_transcript_jobs_once(limit=8)) == 3
        assert restarted.transcript_queue.pending() == []

        server.wait_for(
            lambda: len(server.store.recent_analyses(STUDENT_ID, limit=10)) == 3,
            description="three durable Stop analyses",
        )
        reports = []
        with server.store._conn() as connection:
            reports = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT id, event_id, session_id, event
                    FROM reports WHERE student_id = ? ORDER BY id ASC
                    """,
                    (STUDENT_ID,),
                ).fetchall()
            ]
            raw_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT session_id, analysis_status, content
                    FROM raw_transcripts
                    WHERE student_id = ? ORDER BY session_id ASC
                    """,
                    (STUDENT_ID,),
                ).fetchall()
            ]
        assert len(reports) == 3
        assert all(row["event"] == "Stop" and row["event_id"] for row in reports)
        assert {row["session_id"] for row in raw_rows} == set(session_ids)
        assert all(row["analysis_status"] == "skipped" for row in raw_rows)
        assert all("question from" in row["content"] for row in raw_rows)
        # store_only supplements context; they must not create a second LLM run.
        assert len(server.store.recent_analyses(STUDENT_ID, limit=10)) == 3

        assert asyncio.run(
            restarted.coordinator.pull_analysis_catchup(page_limit=1, max_pages=8)
        ) == 3
        assert [
            item["report_id"] for item in restarted.analysis_store.list_after()
        ] == [row["id"] for row in reports]
        assert restarted.analysis_store.cursor == reports[-1]["id"]

        final_restart = WindowsStudentRuntime.build(
            base_url=server.base_url,
            student_id=STUDENT_ID,
            token=STUDENT_TOKEN,
            spool_dir=spool_dir,
            state_dir=state_dir,
            data_adapter=adapter,
        )
        assert final_restart.coordinator.analysis_cursor == reports[-1]["id"]
        assert asyncio.run(
            final_restart.coordinator.pull_analysis_catchup(page_limit=1, max_pages=8)
        ) == 0
        assert len(final_restart.analysis_store.list_after()) == 3

        # Exercise the actual resident StudentAgent over a real authenticated
        # WebSocket, then let its independent spool loop deliver a new Stop.
        running_agent = _RunningStudentAgent(final_restart)
        try:
            server.wait_for(
                lambda: bool(server.registry.floats.get(STUDENT_ID)),
                description="authenticated StudentAgent WebSocket",
            )
            realtime_transcript = root / "session-realtime.jsonl"
            realtime_transcript.write_text(
                _transcript("session-locked"),
                encoding="utf-8",
            )
            realtime_hook = _run_hook(
                root,
                spool_dir,
                session_id="session-locked",
                transcript_path=realtime_transcript,
            )
            assert realtime_hook.returncode == 0, realtime_hook.stderr
            server.wait_for(
                lambda: not final_restart.spool.pending(),
                description="StudentAgent HTTP spool delivery",
            )
            server.wait_for(
                lambda: len(server.store.recent_analyses(STUDENT_ID, limit=10)) == 4,
                description="realtime Stop analysis",
            )
            with server.store._conn() as connection:
                realtime_report_id = int(connection.execute(
                    """
                    SELECT MAX(id) FROM reports
                    WHERE student_id = ? AND session_id = ? AND event = 'Stop'
                    """,
                    (STUDENT_ID, "session-locked"),
                ).fetchone()[0])
            server.wait_for(
                lambda: final_restart.analysis_store.cursor == realtime_report_id,
                description="analysis delivered through the real WebSocket",
            )
        finally:
            running_agent.close()

        # The resident agent durably queued the full-context supplement; a
        # restart-safe drain confirms it without triggering a fifth analysis.
        assert asyncio.run(final_restart.drain_transcript_jobs_once(limit=8)) == 1
        assert final_restart.transcript_queue.pending() == []
        assert len(server.store.recent_analyses(STUDENT_ID, limit=10)) == 4
    finally:
        server.close()
