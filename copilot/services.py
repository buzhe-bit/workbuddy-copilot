"""Service 层：业务编排。

封装核心业务流程，与 HTTP/WS 无关，可被路由、hook、定时任务复用。
- AnalysisService：transcript → LLM → 入库 → 发事件
- SessionQueryService：对话列表 / 当前会话 / 时间线
"""
from __future__ import annotations

import asyncio
import logging
import copy
import re
import time
import uuid
from time import monotonic
from typing import Any, Awaitable, Callable

from .config import _validate_analysis_max_concurrency
from .models import (
    AcceptedReport, AnalysisEnvelope, Student, Conversation, TimelineEntry, AnalysisResult,
)
from .eventbus import EventBus
from .llm import analysis_prompt_hash, coerce_analysis_outcome
from .transcript import TranscriptSnapshot, parse_text

log = logging.getLogger("copilot.services")

EXPLICIT_RAW_TRANSCRIPT_MARKER = "copilot:explicit-raw-transcript"
MAX_ANALYSIS_INPUT_BYTES = 256 * 1024


def bounded_analysis_input(content: str | bytes | None) -> str:
    raw = (
        content
        if isinstance(content, bytes)
        else str(content or "").encode("utf-8")
    )
    return raw[-MAX_ANALYSIS_INPUT_BYTES:].decode("utf-8", errors="ignore")


class AnalysisRetriesExhausted(RuntimeError):
    """Expected terminal state after a durable report uses all attempts."""


class AnalysisAttemptFailed(RuntimeError):
    """Safe failed-attempt trace propagated to the durable retry wrapper."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        model: str,
        prompt_hash: str,
        latency_ms: int,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.model = model
        self.prompt_hash = prompt_hash
        self.latency_ms = max(0, int(latency_ms))


def stable_analysis_error_code(exc: Exception) -> str:
    """Return a bounded provider-independent code safe for persistence."""
    explicit_code = getattr(exc, "error_code", "")
    if isinstance(explicit_code, str) and explicit_code:
        return explicit_code[:80]
    message = str(exc)
    if message.startswith("LLM provider HTTP "):
        parts = message.split()
        status = parts[3] if len(parts) > 3 and parts[3].isdigit() else "unknown"
        return f"llm_provider_http_{status}"
    if message.startswith("LLM provider "):
        parts = message.split()
        kind = parts[2] if len(parts) > 2 else "error"
        kind = re.sub(r"(?<!^)(?=[A-Z])", "_", kind)
        kind = re.sub(r"[^a-zA-Z0-9]+", "_", kind).strip("_").lower()
        return f"llm_provider_{kind or 'error'}"[:80]
    if message.startswith("LLM response JSON invalid"):
        return "llm_response_json_invalid"
    kind = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    return f"analysis_{kind}"[:80]


class AnalysisService:
    """学习分析编排服务。

    封装 Stop 事件的完整业务流程：
    transcript 解析 → LLM 分析 → 存储 ai_summary + analysis → 发布事件。

    依赖 Store + EventBus，不再读取学员机本地文件或 WorkBuddy DB。
    """

    def __init__(
        self,
        copilot_repo,
        llm_analyzer,
        config: dict,
        event_bus: EventBus,
        notifier: Any | None = None,
        attention_service: Any | None = None,
    ):
        """
        Args:
            copilot_repo: CopilotRepo 实例（读写 copilot.db）
            llm_analyzer: LLM 分析器（copilot.llm.analyze 函数）
            config: 全局配置 dict
            event_bus: 进程内事件总线
            notifier: Notifier 端口实现（系统通知，可选）
        """
        self.copilot = copilot_repo
        self.llm = llm_analyzer
        self.config = config
        self.bus = event_bus
        self.notifier = notifier
        self.attention_service = attention_service
        configured_concurrency = (
            config.get("service", {}).get("analysis_max_concurrency", 2)
        )
        max_concurrency = _validate_analysis_max_concurrency(configured_concurrency)
        self.analysis_semaphore = asyncio.Semaphore(max_concurrency)

    async def _project_attention_safely(
        self,
        method_name: str,
        *args: Any,
    ) -> None:
        """Keep a post-commit projection failure outside source semantics."""
        if self.attention_service is None:
            return
        try:
            method = getattr(self.attention_service, method_name)
            await method(*args)
        except Exception:
            log.exception(
                "attention projection failed after durable source commit method=%s",
                method_name,
            )

    async def _publish_after_commit_safely(
        self,
        payload: dict[str, Any],
    ) -> None:
        """Do not let transient fanout rewrite an already committed result."""
        try:
            await self.bus.publish(payload)
        except Exception:
            log.exception(
                "event fanout failed after durable commit type=%s",
                str(payload.get("type") or "unknown"),
            )

    def _config_with_prompt_overrides(self) -> dict:
        """Return analysis config with server-stored prompt overrides applied."""
        cfg = copy.deepcopy(self.config)
        try:
            row = self.copilot.get_prompt_config("process_reminder")
        except AttributeError:
            row = None
        if row and str(row.get("prompt") or "").strip():
            cfg.setdefault("analysis", {})["process_reminder_prompt"] = str(row["prompt"])
        return cfg

    def parse_transcript_content(self, transcript_content: str | bytes | None) -> TranscriptSnapshot:
        """Parse uploaded transcript content, degrading to an empty snapshot."""
        try:
            return parse_text(transcript_content or "")
        except Exception as exc:
            log.warning("transcript parse failed, using empty snapshot: %s", exc)
            return TranscriptSnapshot()

    def accept_report(
        self,
        *,
        student_id: str,
        session_id: str | None,
        event: str,
        prompt_text: str,
        transcript_content: str | bytes | None,
        raw_transcript_content: str | bytes | None = None,
        cwd: str | None = None,
        event_id: str | None = None,
    ) -> AcceptedReport:
        """Upsert ownership rows and persist the incoming report metadata."""
        raw_content = raw_transcript_content if event == "Stop" else None
        analysis_source = transcript_content
        if event == "Stop" and not analysis_source and raw_content:
            analysis_source = raw_content
        durable_analysis_input = bounded_analysis_input(analysis_source)
        snap = self.parse_transcript_content(durable_analysis_input)
        resolved_session_id = session_id or snap.session_id or ""
        has_explicit_raw = bool(raw_content and resolved_session_id)
        title = snap.ai_title or ""
        stored_raw_content: str | None = None
        if has_explicit_raw:
            stored_raw_content = (
                raw_content.decode("utf-8", errors="replace")
                if isinstance(raw_content, bytes)
                else str(raw_content)
            )
        report, duplicate = self.copilot.accept_report(
            student_id=student_id,
            session_id=resolved_session_id or None,
            event=event,
            event_id=event_id,
            prompt=prompt_text,
            transcript_path=(
                EXPLICIT_RAW_TRANSCRIPT_MARKER if has_explicit_raw else ""
            ),
            msg_count=len(snap.messages),
            tool_calls=snap.tool_calls,
            analysis_input=durable_analysis_input,
            work_dir=cwd or snap.cwd or "",
            title=title,
            raw_transcript_content=stored_raw_content,
        )
        report_id = int(report["id"])
        if duplicate:
            original_input = report.get("analysis_input")
            original_snapshot = (
                self.parse_transcript_content(original_input)
                if original_input is not None
                else TranscriptSnapshot(
                    session_id=str(report.get("session_id") or "") or None,
                    tool_calls=int(report.get("tool_calls") or 0),
                )
            )
            return AcceptedReport(
                report_id=report_id,
                session_id=str(report.get("session_id") or ""),
                snapshot=original_snapshot,
                duplicate=True,
                analysis_status=str(report.get("analysis_status") or "not_requested"),
            )
        return AcceptedReport(
            report_id=report_id,
            session_id=resolved_session_id,
            snapshot=snap,
            duplicate=False,
            analysis_status=str(report.get("analysis_status") or "not_requested"),
        )

    async def handle_user_prompt_submit(
        self,
        student_id: str,
        session_id: str,
        prompt_text: str,
        *,
        report_id: int | None = None,
    ) -> int:
        """处理 UserPromptSubmit 事件：存 prompt → 发事件。"""
        created = True
        if report_id is not None:
            report = self.copilot.get_report(report_id)
            if report:
                student_id = str(report.get("student_id") or "")
                session_id = str(report.get("session_id") or "")
                prompt_text = str(report.get("prompt") or "")
            prompt_row, created = self.copilot.get_or_create_prompt_for_report(
                report_id=report_id,
                session_id=session_id,
                student_id=student_id,
                content=prompt_text,
            )
            prompt_id = int(prompt_row["id"])
            seq = int(prompt_row["seq_in_session"])
        else:
            seq = len(self.copilot.get_prompts_by_session(session_id))
            prompt_id = self.copilot.add_prompt(
                session_id, seq, student_id, prompt_text,
            )

        if created:
            await self._publish_after_commit_safely({
                "type": "prompt",
                "student_id": student_id,
                "session_id": session_id,
                "prompt_id": prompt_id,
                "seq": seq,
                "prompt": prompt_text[:120],
                "timestamp": time.time(),
            })
        return prompt_id

    async def handle_stop(
        self, student_id: str, session_id: str, prompt_text: str,
        transcript_content: str | bytes | None, report_id: int,
    ) -> AnalysisResult:
        """处理 Stop 事件：存 prompt → LLM 分析 → 存结果 → 发事件。"""
        # Persist one prompt per report so process recovery is idempotent.
        prompt_id: int | None = None
        effective_prompt = prompt_text
        prompt_row = self.copilot.get_prompt_for_report(report_id)
        prompt_created = False
        if prompt_row:
            prompt_id = int(prompt_row["id"])
            effective_prompt = str(prompt_row.get("content") or prompt_text)
        elif prompt_text:
            prompt_row, prompt_created = self.copilot.get_or_create_prompt_for_report(
                report_id=report_id,
                session_id=session_id,
                student_id=student_id,
                content=prompt_text,
            )
            prompt_id = int(prompt_row["id"])
            effective_prompt = str(prompt_row.get("content") or prompt_text)

        if prompt_row and prompt_created:
            # The committed prompt row is authoritative. This EventBus signal is
            # transient; clients that miss it catch up through persisted queries.
            await self._publish_after_commit_safely({
                "type": "prompt",
                "student_id": student_id,
                "session_id": session_id,
                "prompt_id": prompt_id,
                "seq": int(prompt_row["seq_in_session"]),
                "prompt": effective_prompt[:120],
                "timestamp": time.time(),
            })

        snap = self.parse_transcript_content(transcript_content)
        # LLM 分析
        llm_config = self._config_with_prompt_overrides()
        prompt_hash = analysis_prompt_hash(llm_config)
        started_at = monotonic()
        try:
            async with self.analysis_semaphore:
                raw_outcome = await self.llm(
                    llm_config,
                    snap,
                    "Stop",
                    effective_prompt,
                )
        except asyncio.CancelledError as exc:
            exc.model = ""
            exc.prompt_hash = prompt_hash
            exc.latency_ms = max(0, int(round((monotonic() - started_at) * 1000)))
            raise
        except Exception as exc:
            latency_ms = max(0, int(round((monotonic() - started_at) * 1000)))
            safe_error = f"LLM provider {type(exc).__name__}"
            error_code = stable_analysis_error_code(RuntimeError(safe_error))
            raise AnalysisAttemptFailed(
                error_code,
                error_code=error_code,
                model="",
                prompt_hash=prompt_hash,
                latency_ms=latency_ms,
            ) from None
        latency_ms = max(0, int(round((monotonic() - started_at) * 1000)))
        outcome = coerce_analysis_outcome(raw_outcome)
        if not outcome.ok:
            error = outcome.error or "LLM provider analysis failed"
            error_code = stable_analysis_error_code(RuntimeError(error))
            log.error(
                "analysis provider failed rid=%d sid=%s session=%s error=%s status=pending",
                report_id,
                student_id,
                (session_id or "?")[:8],
                error_code,
            )
            raise AnalysisAttemptFailed(
                error_code,
                error_code=error_code,
                model=str(outcome.model or "")[:200],
                prompt_hash=prompt_hash,
                latency_ms=latency_ms,
            ) from None
        traced_value = dict(outcome.value)
        traced_value.update({
            "model": str(outcome.model or "")[:200],
            "prompt_hash": prompt_hash,
            "latency_ms": latency_ms,
        })
        result = AnalysisResult.from_dict(traced_value)

        # 标题从 copilot.db sessions 表读取；解析出的 ai_title 仅作兜底。
        session_title = self.copilot.get_session_title(session_id) or snap.ai_title or ""

        # 存储
        analysis_id, analysis_created = self.copilot.complete_report_analysis(
            report_id=report_id,
            prompt_id=prompt_id,
            session_id=session_id,
            student_id=student_id,
            result=result.to_dict(),
            session_title=session_title,
        )

        log.info(
            "分析完成 rid=%d sid=%s session=%s topic=%s",
            report_id, student_id, (session_id or "?")[:8], result.topic,
        )

        if not analysis_created:
            return result

        await self._project_attention_safely("project_analysis", analysis_id)

        # 发布 AI 摘要事件
        if result.ai_reply_summary:
            await self._publish_after_commit_safely({
                "type": "ai_summary",
                "student_id": student_id,
                "session_id": session_id,
                "summary": result.ai_reply_summary,
                "timestamp": time.time(),
            })

        # 发布分析事件
        analysis_row = self.copilot.get_analysis(analysis_id) or {}
        await self._publish_after_commit_safely(AnalysisEnvelope(
            analysis_id=analysis_id,
            student_id=student_id,
            session_id=session_id,
            report_id=report_id,
            event="Stop",
            result=result.to_dict(),
            timestamp=float(analysis_row.get("created_at") or 0.0),
        ).to_dict())

        return result

    async def handle_stop_with_retry(
        self,
        student_id: str,
        session_id: str,
        prompt_text: str,
        transcript_content: str | bytes | None,
        report_id: int,
        *,
        max_attempts: int = 3,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> AnalysisResult | None:
        """Analyze one durable Stop report with a bounded retry schedule.

        The persisted report row is authoritative; request-local transcript data
        is deliberately ignored so recovery cannot borrow unrelated content.
        """
        del student_id, session_id, prompt_text, transcript_content
        if max_attempts < 1:
            return None
        delays = (0, 1, 5)
        while True:
            current = self.copilot.get_report(report_id)
            if not current:
                return None
            status = str(current.get("analysis_status") or "")
            attempts = int(current.get("analysis_attempts") or 0)
            if status not in {"pending", "failed"} or attempts >= max_attempts:
                return None

            delay = delays[min(attempts, len(delays) - 1)]
            await sleeper(delay)
            claimed = self.copilot.claim_report_analysis(
                report_id,
                max_attempts=max_attempts,
            )
            if not claimed:
                return None

            attempt = int(claimed.get("analysis_attempts") or 0)
            try:
                return await self.handle_stop(
                    student_id=str(claimed.get("student_id") or ""),
                    session_id=str(claimed.get("session_id") or ""),
                    prompt_text=str(claimed.get("prompt") or ""),
                    transcript_content=str(claimed.get("analysis_input") or ""),
                    report_id=report_id,
                )
            except asyncio.CancelledError as exc:
                self.copilot.mark_report_analysis_failed(
                    report_id,
                    attempt=attempt,
                    error_code="analysis_cancelled",
                    next_retry_at=(0 if attempt < max_attempts else None),
                    model=str(getattr(exc, "model", "") or "")[:200],
                    prompt_hash=str(getattr(exc, "prompt_hash", "") or "")[:128],
                    latency_ms=max(0, int(getattr(exc, "latency_ms", 0) or 0)),
                )
                await self._project_attention_safely(
                    "project_system_failure",
                    "stop",
                    str(report_id),
                )
                raise
            except Exception as exc:
                error_code = stable_analysis_error_code(exc)
                next_delay = (
                    delays[min(attempt, len(delays) - 1)]
                    if attempt < max_attempts
                    else None
                )
                self.copilot.mark_report_analysis_failed(
                    report_id,
                    attempt=attempt,
                    error_code=error_code,
                    next_retry_at=(
                        time.time() + next_delay
                        if next_delay is not None
                        else None
                    ),
                    model=str(getattr(exc, "model", "") or "")[:200],
                    prompt_hash=str(getattr(exc, "prompt_hash", "") or "")[:128],
                    latency_ms=max(0, int(getattr(exc, "latency_ms", 0) or 0)),
                )
                await self._project_attention_safely(
                    "project_system_failure",
                    "stop",
                    str(report_id),
                )
                if attempt >= max_attempts:
                    raise AnalysisRetriesExhausted(error_code) from None

    def is_report_analysis_recoverable(
        self,
        report_id: int,
        *,
        max_attempts: int = 3,
    ) -> bool:
        """Return whether a durable Stop report can still be claimed."""
        report = self.copilot.get_report(report_id)
        if not report or report.get("event") != "Stop":
            return False
        analysis_status = str(report.get("analysis_status") or "")
        return (
            analysis_status in {"pending", "failed"}
            and not (
                analysis_status == "failed"
                and report.get("analysis_next_retry_at") is None
            )
            and int(report.get("analysis_attempts") or 0) < max_attempts
        )


class SessionQueryService:
    """会话查询服务。

    统一查询入口，浮标和导师台共用。
    所有会话查询均读取 copilot.db sessions 表。
    """

    def __init__(self, copilot_repo, config: dict):
        """
        Args:
            copilot_repo: CopilotRepo 实例
            config: 全局配置
        """
        self.copilot = copilot_repo
        self.config = config

    def list_students(self) -> list[Student]:
        """学员列表 + 状态概览。"""
        student_id = self.config.get("student_id", "student-1")
        student_name = self.config.get("student_name") or student_id

        rows = self.copilot.students_overview()
        students = []
        for r in rows:
            sid = r.get("student_id", student_id)
            display_name = r.get("display_name") or (student_name if sid == student_id else "") or sid
            # 实时计算有效会话数
            sessions = self.list_sessions(sid)
            students.append(Student(
                student_id=sid,
                display_name=display_name,
                analysis_count=r.get("analysis_count", 0),
                session_count=len(sessions),
                last_ts=r.get("last_ts", 0),
                last_topic=r.get("last_topic", ""),
                last_severity=r.get("last_severity", "info"),
                alert_count=r.get("alert_count", 0),
                last_diagnosis=r.get("last_diagnosis", ""),
                open_attention_count=r.get("open_attention_count", 0),
                highest_attention_priority=r.get("highest_attention_priority", ""),
                last_attention_at=r.get("last_attention_at", 0),
            ))
        return students

    def list_sessions(self, student_id: str, limit: int = 1000) -> list[Conversation]:
        """某学员的对话列表（以 copilot.db sessions 表为权威源）。"""
        rows = self.copilot.get_sessions_by_student(student_id, limit=limit)
        return [Conversation(
            session_id=r["session_id"],
            work_dir=r.get("work_dir", ""),
            title=r.get("session_title", ""),
            group_type=r.get("group_type", "") or "",
            space_name=r.get("space_name", "") or "",
            created_at=r.get("created_at", 0) or 0,
            analysis_count=r.get("analysis_count", 0),
            message_count=r.get("message_count", 0),
            alert_count=r.get("alert_count", 0),
            last_diagnosis=r.get("last_diagnosis", ""),
            last_topic=r.get("last_topic", ""),
            last_severity=r.get("last_severity", "info"),
            last_is_technical=r.get("last_is_technical", 0),
            last_activity_at=r.get("last_ts", 0),
        ) for r in rows]

    def get_timeline(self, session_id: str) -> list[TimelineEntry]:
        """某对话的时间线（三表 UNION）。"""
        rows = self.copilot.get_timeline_by_session(session_id)
        return [TimelineEntry(
            type=r.get("type", ""),
            content=r.get("content", ""),
            created_at=r.get("created_at", 0),
            session_id=r.get("session_id", session_id),
            seq_in_session=r.get("seq_in_session"),
            prompt_id=r.get("prompt_id"),
            reply_ref=r.get("reply_ref"),
            has_summary=bool(r.get("has_summary", False)),
            has_full_reply=bool(r.get("has_full_reply", False)),
            suggestion=r.get("suggestion", ""),
            severity=r.get("severity", ""),
            understanding=r.get("understanding", "") or "",
            topic=r.get("topic", "") or "",
            is_technical=bool(r.get("is_technical", 0)),
        ) for r in rows]

    def get_active_session(
        self,
        work_dir: str | None = None,
        student_id: str | None = None,
    ) -> dict | None:
        """当前激活的对话（浮标跟随用），读取 sessions 表。"""
        return self.copilot.get_active_session_from_table(work_dir=work_dir, student_id=student_id)

    def list_all_sessions_with_title(
        self,
        work_dir: str | None = None,
        student_id: str | None = None,
        limit: int = 8,
    ) -> list[dict]:
        """列出最近会话（带标题），浮标切换栏用，读取 sessions 表。"""
        return self.copilot.list_sessions_from_table(
            work_dir=work_dir,
            student_id=student_id,
            limit=limit,
        )


class MessageService:
    """Mentor-to-student message workflow."""

    def __init__(self, copilot_repo, event_bus: EventBus):
        self.copilot = copilot_repo
        self.bus = event_bus

    async def send(
        self,
        student_id: str,
        mentor_id: str | None,
        text: str,
        client_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist a mentor message, publish it, and report delivery status."""
        resolved_mentor_id = mentor_id or "mentor"
        message_id = uuid.uuid4().hex
        created = True
        if client_request_id is None:
            self.copilot.upsert_student(student_id)
            row_id = self.copilot.add_mentor_message(
                student_id=student_id,
                mentor_id=resolved_mentor_id,
                session_id="",
                text=text,
                message_id=message_id,
            )
            row = self._find_message(student_id, row_id)
        else:
            row, created = self.copilot.get_or_create_mentor_message(
                student_id=student_id,
                mentor_id=resolved_mentor_id,
                session_id="",
                text=text,
                message_id=message_id,
                client_request_id=client_request_id,
            )
            row_id = int(row["id"])
            message_id = str(row["message_id"])

        if created:
            await self.bus.publish(self._to_wire_message(row))

        delivered_row = self._find_message(student_id, row_id)
        result = {
            "message_id": message_id,
            "id": row_id,
            "delivered": bool(delivered_row.get("delivered_at")),
        }
        if client_request_id is not None:
            result.update({
                "client_request_id": client_request_id,
                "duplicate": not created,
            })
        return result

    def get_mentor_message_statuses(
        self,
        client_request_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Return the bounded recovery projection; message text stays private."""
        return [{
            "client_request_id": row["client_request_id"],
            "message_id": row["message_id"],
            "id": row["id"],
            "student_id": row["student_id"],
            "delivered": bool(row.get("delivered_at")),
        } for row in self.copilot.list_mentor_messages_by_client_request_ids(
            client_request_ids,
        )]

    def get_catchup(
        self,
        student_id: str,
        since_id: int | str | None,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return only still-unacknowledged messages after the client cursor.

        A client's display cursor can intentionally roll back while recovering
        an earlier state-publish failure.  Confirmed history is audit data,
        not a delivery backlog: returning it here would reintroduce it to a
        bounded client de-duplication cache and cause duplicate rendering.
        """
        return [
            self._to_wire_message(row)
            for row in self.copilot.list_undelivered_messages(
                student_id,
                since_id,
                limit=limit,
            )
        ]

    def get_pending_receipts(
        self,
        student_id: str,
        *,
        limit: int = 64,
        after_id: int = 0,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 64))
        return [
            self._to_wire_message(row)
            for row in self.copilot.list_pending_message_receipts(
                student_id,
                limit=bounded_limit,
                after_id=max(0, int(after_id)),
            )
        ]

    async def ack(self, message_id: str, student_id: str) -> bool:
        try:
            existing = self._find_message_by_message_id(student_id, message_id)
        except LookupError:
            return False
        if existing.get("delivered_at") is not None:
            return True

        updated = self.copilot.mark_message_delivered(message_id, student_id=student_id)
        if updated <= 0:
            return False

        row = self._find_message_by_message_id(student_id, message_id)
        receipt = {
            "type": "message_delivered",
            "student_id": student_id,
            "message_id": message_id,
            "id": row["id"],
            "timestamp": row.get("delivered_at") or time.time(),
        }
        if row.get("client_request_id"):
            receipt["client_request_id"] = row["client_request_id"]
        await self.bus.publish(receipt)
        return True

    def _find_message(self, student_id: str, row_id: int) -> dict[str, Any]:
        for row in self.copilot.list_messages_since(student_id, 0):
            if int(row["id"]) == int(row_id):
                return row
        raise LookupError(f"mentor message not found: student={student_id} id={row_id}")

    def _find_message_by_message_id(self, student_id: str, message_id: str) -> dict[str, Any]:
        for row in self.copilot.list_messages_since(student_id, 0):
            if row["message_id"] == message_id:
                return row
        raise LookupError(f"mentor message not found: student={student_id} message={message_id}")

    @staticmethod
    def _to_wire_message(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "mentor_message",
            "student_id": row["student_id"],
            "message_id": row["message_id"],
            "id": row["id"],
            "text": row["text"],
            "mentor_id": row["mentor_id"],
            "timestamp": row["created_at"],
        }
