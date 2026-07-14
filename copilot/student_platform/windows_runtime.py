"""First-class Windows composition root built from the shared Student Core."""
from __future__ import annotations

import inspect
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..models import UploadOutcome
from ..student_core.agent import StudentAgent
from ..student_core.coordinator import StudentCoordinator
from ..student_core.spool import EventSpool
from ..student_core.transcript_jobs import TranscriptUploadJob, TranscriptUploadQueue
from ..student_core.transport import Accepted, StudentTransport
from ..wb_upload import (
    content_sha256,
    filter_message_jsonl_text,
    post_transcript,
    upload_conversations,
)
from .windows import WindowsWorkBuddyData
from .workbuddy import WorkBuddyDataAdapter


log = logging.getLogger("copilot.student_platform.windows_runtime")


class WindowsRuntimeBlocked(RuntimeError):
    """Typed production gate for missing or unverified Windows evidence."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = str(code)


class WindowsAnalysisStore:
    """Durable, identity-scoped analysis inbox and recovery cursor.

    Persisting an envelope and advancing the catch-up cursor are deliberately
    separate operations. A renderer can fail after the envelope is safely on
    disk; in that case the unchanged cursor makes the server retry it rather
    than silently skipping a result.
    """

    def __init__(self, path: str | Path, *, student_id: str) -> None:
        self.path = Path(path).expanduser()
        self.student_id = str(student_id or "").strip()
        if not self.student_id:
            raise ValueError("student identity is required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("Windows analysis store must not be a symlink")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS windows_analysis_items (
                    report_id INTEGER PRIMARY KEY,
                    analysis_id INTEGER NOT NULL DEFAULT 0,
                    student_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    persisted_at REAL NOT NULL
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(windows_analysis_items)"
                ).fetchall()
            }
            reset_legacy_cursor = "analysis_id" not in columns
            if "analysis_id" not in columns:
                connection.execute(
                    """ALTER TABLE windows_analysis_items
                       ADD COLUMN analysis_id INTEGER NOT NULL DEFAULT 0"""
                )
                # Task 9 pre-release databases used report IDs as cursors.
                # Reset the local cache so the authoritative server can replay
                # it once using commit-order IDs without silently skipping.
                connection.execute("DELETE FROM windows_analysis_items")
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS
                   idx_windows_analysis_commit_id
                   ON windows_analysis_items(analysis_id)
                   WHERE analysis_id > 0"""
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS windows_analysis_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT value FROM windows_analysis_state WHERE key = 'student_id'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO windows_analysis_state(key, value) VALUES('student_id', ?)",
                    (self.student_id,),
                )
            elif str(row["value"]) != self.student_id:
                raise ValueError("Windows analysis store identity mismatch")
            connection.execute(
                """
                INSERT OR IGNORE INTO windows_analysis_state(key, value)
                VALUES('analysis_cursor', '0')
                """
            )
            if reset_legacy_cursor:
                connection.execute(
                    """UPDATE windows_analysis_state SET value = '0'
                       WHERE key = 'analysis_cursor'"""
                )

    @property
    def cursor(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM windows_analysis_state WHERE key = 'analysis_cursor'"
            ).fetchone()
        try:
            return max(0, int(row["value"])) if row is not None else 0
        except (TypeError, ValueError):
            raise ValueError("invalid Windows analysis cursor") from None

    @property
    def next_pending_analysis_id(self) -> int | None:
        """Return the oldest durable commit-order envelope after the cursor."""

        current = self.cursor
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MIN(analysis_id) AS analysis_id
                FROM windows_analysis_items
                WHERE student_id = ? AND analysis_id > ?
                """,
                (self.student_id, current),
            ).fetchone()
        if row is None or row["analysis_id"] is None:
            return None
        return int(row["analysis_id"])

    @property
    def next_pending_report_id(self) -> int | None:
        """Compatibility alias for pre-commit-cursor callers."""
        return self.next_pending_analysis_id

    def persist(self, payload: Mapping[str, Any]) -> bool:
        """Write one stable envelope idempotently without moving the cursor."""
        if not isinstance(payload, Mapping) or payload.get("type") not in {
            "analysis",
            "analysis_result",
        }:
            raise ValueError("invalid analysis envelope")
        student_id = str(payload.get("student_id") or "").strip()
        if student_id != self.student_id:
            raise ValueError("analysis student identity mismatch")
        try:
            report_id = int(payload.get("report_id") or 0)
        except (TypeError, ValueError):
            report_id = 0
        if report_id <= 0:
            raise ValueError("analysis report_id is required")
        try:
            analysis_id = int(payload.get("analysis_id") or report_id)
        except (TypeError, ValueError):
            analysis_id = 0
        if analysis_id <= 0:
            raise ValueError("analysis commit id is required")
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT analysis_id, student_id, payload_json
                   FROM windows_analysis_items WHERE report_id = ?""",
                (report_id,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["student_id"]) != self.student_id
                    or int(row["analysis_id"] or 0) != analysis_id
                    or str(row["payload_json"]) != encoded
                ):
                    raise ValueError("analysis report collision")
                return False
            connection.execute(
                """
                INSERT INTO windows_analysis_items(
                    report_id, analysis_id, student_id, payload_json, persisted_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (report_id, analysis_id, self.student_id, encoded, time.time()),
            )
        return True

    def advance_cursor(self, analysis_id: int) -> bool:
        """Advance only to an envelope that is already durably stored."""
        resolved_analysis_id = int(analysis_id)
        if resolved_analysis_id <= 0:
            raise ValueError("analysis commit id is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                """
                SELECT 1 FROM windows_analysis_items
                WHERE analysis_id = ? AND student_id = ?
                """,
                (resolved_analysis_id, self.student_id),
            ).fetchone()
            if exists is None:
                raise ValueError("analysis must be persisted before cursor advance")
            row = connection.execute(
                "SELECT value FROM windows_analysis_state WHERE key = 'analysis_cursor'"
            ).fetchone()
            current = max(0, int(row["value"])) if row is not None else 0
            if resolved_analysis_id <= current:
                return False
            oldest = connection.execute(
                """
                SELECT MIN(analysis_id) AS analysis_id
                FROM windows_analysis_items
                WHERE student_id = ? AND analysis_id > ?
                """,
                (self.student_id, current),
            ).fetchone()
            if oldest is None or int(oldest["analysis_id"] or 0) != resolved_analysis_id:
                raise ValueError("analysis cursor cannot skip a pending envelope")
            connection.execute(
                """
                UPDATE windows_analysis_state SET value = ?
                WHERE key = 'analysis_cursor'
                """,
                (str(resolved_analysis_id),),
            )
        return True

    def list_after(
        self,
        after_report_id: int = 0,
        *,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return stored envelopes in report order for the native UI."""
        bounded_limit = max(1, min(int(limit), 10_000))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM windows_analysis_items
                WHERE student_id = ? AND report_id > ?
                ORDER BY report_id ASC
                LIMIT ?
                """,
                (self.student_id, max(0, int(after_report_id)), bounded_limit),
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]


class WindowsTranscriptUploader:
    """Async adapter around the existing platform-neutral upload function."""

    def __init__(
        self,
        *,
        data_adapter: WorkBuddyDataAdapter,
        student_id: str,
        base_url: str,
        token: str,
        timeout: float = 60.0,
    ) -> None:
        self.data_adapter = data_adapter
        self.student_id = student_id
        self.base_url = base_url
        self.token = token
        self.timeout = timeout
        self.config = {"service": {"public_base_url": base_url}}

    async def upload(
        self,
        *,
        request_id: str,
        session_id: str | None,
    ) -> UploadOutcome:
        import asyncio

        return await asyncio.to_thread(
            upload_conversations,
            self.config,
            self.student_id,
            "missing",
            server_url=self.base_url,
            token=self.token,
            timeout=self.timeout,
            request_id=request_id,
            session_id=session_id,
            data_adapter=self.data_adapter,
        )

    async def upload_stop(self, job: TranscriptUploadJob) -> UploadOutcome:
        if job.student_id != self.student_id:
            return UploadOutcome(0, 0, 0, 0, 1, "student_mismatch")
        if not job.payload_pinned:
            return UploadOutcome(1, 0, 0, 0, 1, "payload_not_pinned")
        import asyncio

        payload = {
            "student_id": self.student_id,
            "filtered_content": job.filtered_content,
            "sha": job.content_sha256,
            "analysis_mode": "store_only",
            "source_event_id": job.event_id,
            "source_report_id": job.report_id,
        }
        response = await asyncio.to_thread(
            post_transcript,
            self.base_url,
            job.session_id,
            payload,
            token=self.token,
            timeout=self.timeout,
        )
        confirmed = (
            isinstance(response, Mapping)
            and response.get("ok") is True
            and str(response.get("session_id") or "") == job.session_id
            and str(response.get("sha") or "") == job.content_sha256
        )
        if not confirmed:
            return UploadOutcome(1, 1, 0, 0, 1, "response_unconfirmed")
        skipped = 1 if bool(response.get("skipped")) else 0
        return UploadOutcome(1, 1, 1 - skipped, skipped, 0)

    async def read_stop_payload(
        self,
        job: TranscriptUploadJob,
        *,
        transcript_snapshot: Any | None = None,
    ) -> tuple[str, str]:
        """Read and filter once; the queue owns persistence before networking."""
        if job.student_id != self.student_id:
            raise WindowsRuntimeBlocked("student_mismatch")
        import asyncio

        return await asyncio.to_thread(
            self._read_stop_payload,
            job.session_id,
            transcript_snapshot,
        )

    def _read_stop_payload(
        self,
        session_id: str,
        transcript_snapshot: Any | None = None,
    ) -> tuple[str, str]:
        transcript_source = (
            transcript_snapshot
            if transcript_snapshot is not None
            else self.data_adapter
        )
        transcript = transcript_source.read_transcript(session_id)
        failure = getattr(transcript, "failure", None)
        if failure is not None:
            code = str(getattr(failure, "code", "transcript_unavailable") or "")
            raise WindowsRuntimeBlocked(code[:80] or "transcript_unavailable")
        filtered_content = filter_message_jsonl_text(
            str(getattr(transcript, "content", "") or "")
        )
        return filtered_content, content_sha256(filtered_content)


@dataclass
class WindowsStudentRuntime:
    """Own all Windows student-side collaborators without a second core."""

    data: WorkBuddyDataAdapter
    uploader: WindowsTranscriptUploader
    transport: Any
    spool: EventSpool
    transcript_queue: TranscriptUploadQueue
    analysis_store: WindowsAnalysisStore
    coordinator: StudentCoordinator
    agent: StudentAgent
    state_dir: Path
    _stopping: bool = False
    _stop_requested: bool = False
    _run_tasks: tuple[Any, Any] | None = None

    @classmethod
    def build(
        cls,
        *,
        base_url: str,
        student_id: str,
        token: str,
        spool_dir: str | Path,
        state_dir: str | Path,
        workbuddy_config_dir: str | Path | None = None,
        profile_path: str | Path | None = None,
        data_adapter: WorkBuddyDataAdapter | None = None,
        transport: Any | None = None,
        message_handler: Callable[[Mapping[str, Any]], Any] | None = None,
        analysis_handler: Callable[[Mapping[str, Any]], Any] | None = None,
        interval: float = 1.0,
    ) -> "WindowsStudentRuntime":
        resolved_student_id = str(student_id or "").strip()
        resolved_base_url = str(base_url or "").strip().rstrip("/")
        if not resolved_student_id or not resolved_base_url:
            raise ValueError("base_url and student_id are required")
        if data_adapter is None:
            if workbuddy_config_dir is None:
                raise WindowsRuntimeBlocked(
                    "windows_profile_required",
                    "an explicit WorkBuddy config and W0 profile are required",
                )
            windows_data = WindowsWorkBuddyData(
                workbuddy_config_dir,
                profile_path=profile_path,
            )
            failure = windows_data.transcript_profile_failure
            if failure is not None:
                raise WindowsRuntimeBlocked(failure.code, failure.message)
            data_adapter = windows_data

        resolved_state_dir = Path(state_dir).expanduser()
        if resolved_state_dir.is_symlink():
            raise ValueError("Windows runtime state directory must not be a symlink")
        resolved_state_dir.mkdir(parents=True, exist_ok=True)
        spool = EventSpool(spool_dir)
        resolved_transport = transport or StudentTransport(
            resolved_base_url,
            student_id=resolved_student_id,
            token=token,
        )
        if str(getattr(resolved_transport, "student_id", "") or "") != resolved_student_id:
            raise ValueError("transport student identity mismatch")
        uploader = WindowsTranscriptUploader(
            data_adapter=data_adapter,
            student_id=resolved_student_id,
            base_url=resolved_base_url,
            token=token,
        )
        transcript_queue = TranscriptUploadQueue(
            resolved_state_dir / "transcript-jobs.sqlite3"
        )
        analysis_store = WindowsAnalysisStore(
            resolved_state_dir / "analyses.sqlite3",
            student_id=resolved_student_id,
        )

        async def persist_analysis(payload: Mapping[str, Any]) -> None:
            import asyncio

            await asyncio.to_thread(analysis_store.persist, payload)
            report_id = int(payload.get("report_id") or 0)
            delivery_id = int(payload.get("analysis_id") or report_id)
            next_pending = await asyncio.to_thread(
                lambda: analysis_store.next_pending_analysis_id
            )
            if next_pending != delivery_id:
                raise RuntimeError("an earlier analysis is still pending")
            if analysis_handler is not None:
                result = analysis_handler(payload)
                if inspect.isawaitable(result):
                    await result
            await asyncio.to_thread(analysis_store.advance_cursor, delivery_id)

        coordinator = StudentCoordinator(
            spool,
            resolved_transport,
            uploader,
            message_handler=message_handler,
            analysis_handler=persist_analysis,
            analysis_cursor=analysis_store.cursor,
            transcript_queue=transcript_queue,
        )
        agent = StudentAgent(coordinator, interval=interval)
        return cls(
            data=data_adapter,
            uploader=uploader,
            transport=resolved_transport,
            spool=spool,
            transcript_queue=transcript_queue,
            analysis_store=analysis_store,
            coordinator=coordinator,
            agent=agent,
            state_dir=resolved_state_dir,
        )

    async def sync_sessions_once(self) -> Accepted:
        import asyncio

        sessions = await asyncio.to_thread(self._session_sync_payloads)
        async_method = getattr(self.transport, "post_sync_async", None)
        if callable(async_method):
            result = async_method(sessions)
            result = await result if inspect.isawaitable(result) else result
        else:
            sync_method = getattr(self.transport, "post_sync", None)
            if not callable(sync_method):
                raise WindowsRuntimeBlocked("session_sync_unavailable")
            result = await asyncio.to_thread(sync_method, sessions)
        if not isinstance(result, Accepted):
            raise WindowsRuntimeBlocked("session_sync_unconfirmed")
        return result

    def _session_sync_payloads(self) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        for session in self.data.list_sessions():
            row = session.to_dict()
            sessions.append({
                "session_id": str(row.get("session_id") or ""),
                "title": str(row.get("title") or ""),
                "work_dir": str(row.get("work_dir") or ""),
                "group_type": row.get("group_type") or None,
                "space_name": str(row.get("space_name") or ""),
                "created_at": float(row.get("created_at") or 0.0),
                "last_activity_at": float(row.get("last_activity_at") or 0.0),
            })
        return sessions

    async def drain_transcript_jobs_once(
        self,
        *,
        limit: int = 16,
        due_only: bool = False,
    ) -> int:
        import asyncio

        completed = 0
        try:
            queue_reader = (
                self.transcript_queue.ready
                if due_only
                else self.transcript_queue.pending
            )
            jobs = await asyncio.to_thread(queue_reader, limit=limit)
        except Exception as exc:
            log.warning("Windows transcript queue read failed type=%s", type(exc).__name__)
            return 0
        batch_snapshot: Any | None = None
        batch_snapshot_failed = False
        if any(not job.payload_pinned for job in jobs):
            snapshot_factory = getattr(self.data, "transcript_snapshot", None)
            if callable(snapshot_factory):
                try:
                    batch_snapshot = await asyncio.to_thread(snapshot_factory)
                except Exception as exc:
                    batch_snapshot_failed = True
                    log.warning(
                        "Windows transcript snapshot failed type=%s",
                        type(exc).__name__,
                    )
        for job in jobs:
            try:
                pinned_job = job
                if not pinned_job.payload_pinned:
                    if batch_snapshot_failed:
                        raise WindowsRuntimeBlocked("transcript_snapshot_failed")
                    filtered_content, sha = await self.uploader.read_stop_payload(
                        job,
                        transcript_snapshot=batch_snapshot,
                    )
                    pinned_job = await asyncio.to_thread(
                        self.transcript_queue.pin_payload,
                        job.event_id,
                        filtered_content=filtered_content,
                        content_sha256=sha,
                    )
                outcome = await self.uploader.upload_stop(pinned_job)
            except Exception as exc:
                log.warning("Windows transcript job failed type=%s", type(exc).__name__)
                outcome = UploadOutcome(1, 1, 0, 0, 1, "upload_failed")
            recorded = await asyncio.to_thread(
                self.transcript_queue.record_outcome,
                job.event_id,
                outcome,
            )
            if recorded:
                if outcome.complete:
                    completed += 1
        return completed

    async def run(self, *, maintenance_interval: float = 2.0) -> None:
        import asyncio

        # A stop committed before this coroutine's first scheduler turn must
        # remain authoritative; run() must never clear and revive it.
        if self._stop_requested:
            self._stopping = True
            return
        if self._run_tasks is not None and any(
            not task.done() for task in self._run_tasks
        ):
            raise RuntimeError("Windows student runtime is already running")
        self._stopping = False
        # Use StudentAgent.start() so its own bounded stop path owns the task
        # even when the socket reader is blocked in recv().
        agent_task = self.agent.start()
        maintenance_task = asyncio.create_task(
            self._maintenance_loop(max(0.1, float(maintenance_interval)))
        )
        run_tasks = (agent_task, maintenance_task)
        self._run_tasks = run_tasks
        try:
            await asyncio.gather(*run_tasks)
        except asyncio.CancelledError:
            if not self._stopping:
                raise
        finally:
            self._stopping = True
            await self.agent.stop()
            for task in run_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*run_tasks, return_exceptions=True)
            if self._run_tasks is run_tasks:
                self._run_tasks = None

    async def _maintenance_loop(
        self,
        interval: float,
        *,
        session_sync_interval: float = 30.0,
        session_retry_interval: float = 5.0,
    ) -> None:
        import asyncio

        next_session_sync_at = 0.0
        while not self._stopping:
            now = time.monotonic()
            if now >= next_session_sync_at:
                try:
                    await self.sync_sessions_once()
                except Exception as exc:
                    log.warning(
                        "Windows session sync deferred type=%s",
                        type(exc).__name__,
                    )
                    delay = max(0.0, float(session_retry_interval))
                else:
                    delay = max(0.0, float(session_sync_interval))
                next_session_sync_at = now + delay
            try:
                await self.drain_transcript_jobs_once(due_only=True)
            except Exception as exc:
                log.warning("Windows transcript maintenance failed type=%s", type(exc).__name__)
            if self._stopping:
                return
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        import asyncio

        self._stop_requested = True
        self._stopping = True
        await self.agent.stop()
        run_tasks = self._run_tasks
        if run_tasks is None:
            return
        current = asyncio.current_task()
        wait_for: list[Any] = []
        for task in run_tasks:
            if task is current:
                continue
            if not task.done():
                task.cancel()
            wait_for.append(task)
        if wait_for:
            await asyncio.gather(*wait_for, return_exceptions=True)
