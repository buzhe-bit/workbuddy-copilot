"""FastAPI controller layer for WorkBuddy Copilot."""
from __future__ import annotations

import inspect
import asyncio
import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .app_context import (
    _extract_supplied_token,
    AppContext,
    StudentPrincipal,
    acquire_worker_lock,
    build_context,
    ensure_attention_service,
    get_analysis_service,
    get_context,
    get_message_service,
    get_session_service,
    get_store,
    get_upload_service,
    require_mentor_token,
    require_student_principal,
    resolve_student_id,
    release_worker_lock,
    student_principal_for_token,
    token_is_valid,
    validate_auth_config,
)
from .llm import (
    analysis_prompt_hash,
    answer_question as llm_answer_question,
    coerce_analysis_outcome,
    coerce_question_answer_outcome,
    question_fallback_answer,
)
from .models import AnalysisEnvelope, AnalysisResult, QuestionAnswerOutcome, normalize_event_id
from .services import (
    EXPLICIT_RAW_TRANSCRIPT_MARKER,
    AnalysisRetriesExhausted,
    AnalysisService,
    MessageService,
    SessionQueryService,
    bounded_analysis_input,
)
from .store import ActiveTranscriptAnalysisConflict, Store
from .upload_service import (
    InvalidStateTransition,
    UploadRequestNotFound,
    UploadRequestService,
    UploadTranscriptNotFound,
)
from .transcript import Message, TranscriptSnapshot, parse_text, parse_turns

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("copilot.service")


class _EventId(str):
    """Pydantic 1/2 compatible hook id type backed by the Store invariant."""

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        from pydantic_core import core_schema

        return core_schema.no_info_after_validator_function(
            cls.validate,
            core_schema.str_schema(),
        )

    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, value, *args):
        normalized = normalize_event_id(value)
        if normalized is None:
            raise ValueError("invalid event_id")
        return cls(normalized)


class _ClientRequestId(str):
    """Pydantic 1/2 compatible opaque idempotency key."""

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        from pydantic_core import core_schema

        return core_schema.no_info_after_validator_function(
            cls.validate,
            core_schema.str_schema(),
        )

    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, value, *args):
        candidate = str(value or "")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
        if not candidate or len(candidate) > 128:
            raise ValueError("invalid client_request_id")
        if any(char not in allowed for char in candidate):
            raise ValueError("invalid client_request_id")
        return cls(candidate)


class ReportIn(BaseModel):
    student_id: str | None = None
    session_id: str | None = None
    event: str
    event_id: _EventId | None = None
    prompt: str = ""
    transcript_tail: str | None = None
    transcript_full: str | None = None
    cwd: str | None = None


class MentorMessageIn(BaseModel):
    student_id: str
    text: str
    mentor_id: str | None = None
    client_request_id: _ClientRequestId | None = None


class MentorMessageStatusIn(BaseModel):
    client_request_ids: list[_ClientRequestId]


class StudentMessageAckIn(BaseModel):
    student_id: str | None = None
    message_id: str


class StudentAskIn(BaseModel):
    student_id: str | None = None
    question: str
    session_id: str | None = None


class StudentAskFeedbackIn(BaseModel):
    student_id: str | None = None
    feedback: Literal["helpful", "unresolved"]
    note: str | None = None


class SyncSessionIn(BaseModel):
    session_id: str
    title: str = ""
    work_dir: str = ""
    group_type: Literal["space", "task"] | None = None
    space_name: str = ""
    created_at: float | None = None
    last_activity_at: float | None = None


class SessionsSyncIn(BaseModel):
    student_id: str | None = None
    sessions: list[SyncSessionIn]


class TranscriptUploadIn(BaseModel):
    student_id: str | None = None
    filtered_content: Any
    sha: str
    request_id: str | None = None
    analysis_mode: Literal["analyze", "store_only"] = "analyze"
    source_event_id: _EventId | None = None
    source_report_id: int | None = None


class MentorUploadRequestIn(BaseModel):
    mentor_id: str | None = None
    session_id: str | None = None


class UploadRequestStatusIn(BaseModel):
    student_id: str | None = None
    status: Literal["pending", "running", "done", "failed"]
    error_message: str | None = None
    result: dict[str, Any] | None = None


def _question_context_from_raw(content: str | bytes | None) -> list[dict[str, str]]:
    if not content:
        return []
    snap = parse_text(content)
    messages = [
        {"role": msg.role, "content": msg.text}
        for msg in snap.messages[-16:]
        if msg.text
    ]
    if messages:
        return messages
    raw = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
    raw = raw.strip()
    return [{"role": "transcript", "content": raw[-6000:]}] if raw else []


def _question_context_from_recent(rows: list[dict]) -> list[dict[str, str]]:
    context: list[dict[str, str]] = []
    for row in reversed(rows):
        parts: list[str] = []
        topic = row.get("topic") or ""
        diagnosis = row.get("diagnosis") or ""
        suggestion = row.get("suggestion") or ""
        progress = row.get("progress") or ""
        if topic:
            parts.append(f"主题：{topic}")
        if diagnosis:
            parts.append(f"诊断：{diagnosis}")
        if suggestion:
            parts.append(f"建议：{suggestion}")
        if progress:
            parts.append(f"进展：{progress}")
        if parts:
            context.append({"role": "analysis", "content": "；".join(parts)})
    return context


def _build_student_question_context(
    store: Store,
    student_id: str,
    session_id: str | None,
) -> list[dict[str, str]]:
    if session_id:
        raw = store.get_raw_transcript_for_student_session(student_id, session_id)
        raw_context = _question_context_from_raw((raw or {}).get("content"))
        if raw_context:
            return raw_context
    recent = store.recent_analyses(student_id, limit=5, session_id=session_id)
    return _question_context_from_recent(recent)


def _student_ask_timeout(config: dict[str, Any]) -> float:
    try:
        base = float(config.get("llm", {}).get("timeout", 30))
    except (TypeError, ValueError):
        base = 30.0
    return min(max(base + 5.0, 5.0), 45.0)


async def _handle_stop_background(
    analysis_svc: AnalysisService,
    student_id: str,
    session_id: str,
    prompt: str,
    transcript_content: str,
    report_id: int,
    *,
    sleeper=None,
) -> None:
    try:
        kwargs = {"sleeper": sleeper} if sleeper is not None else {}
        await analysis_svc.handle_stop_with_retry(
            student_id=student_id,
            session_id=session_id,
            prompt_text=prompt,
            transcript_content=transcript_content,
            report_id=report_id,
            **kwargs,
        )
    except Exception as exc:
        log.exception("background Stop analysis failed report_id=%s: %s", report_id, exc)


async def _project_attention_safely(
    context: AppContext,
    method_name: str,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Run one post-commit projection without changing source success/failure."""
    try:
        attention_svc = ensure_attention_service(context)
        method = getattr(attention_svc, method_name)
        await method(*args, **kwargs)
    except Exception:
        log.exception(
            "attention projection failed after durable source commit method=%s",
            method_name,
        )


async def _publish_event_safely(
    context: AppContext,
    payload: dict[str, Any],
) -> None:
    """Best-effort fanout after the authoritative state is already durable."""
    try:
        await context.bus.publish(payload)
    except Exception:
        log.exception(
            "event fanout failed after durable commit type=%s",
            str(payload.get("type") or "unknown"),
        )


async def _project_committed_analysis_safely(
    context: AppContext,
    committed: dict[str, Any],
) -> None:
    """Keep missing/corrupt projection metadata outside bulk source semantics."""
    try:
        analysis_id = int(committed["analysis_id"])
    except (KeyError, TypeError, ValueError, OverflowError):
        log.error(
            "attention projection metadata missing after durable bulk commit"
        )
        return
    await _project_attention_safely(
        context,
        "project_analysis",
        analysis_id,
    )


def _committed_report_id_safely(committed: dict[str, Any]) -> int | None:
    """Read optional event metadata without changing a committed source result."""
    try:
        return int(committed["report_id"])
    except (KeyError, TypeError, ValueError, OverflowError):
        log.error("report event metadata missing after durable bulk commit")
        return None


def _filtered_content_to_raw(filtered_content: Any) -> str:
    """Normalize already-filtered uploaded message content into JSONL text."""
    if filtered_content is None:
        return ""
    if isinstance(filtered_content, str):
        return filtered_content
    if isinstance(filtered_content, (bytes, bytearray)):
        return bytes(filtered_content).decode("utf-8", errors="replace")
    if isinstance(filtered_content, dict):
        for key in ("messages", "items", "lines"):
            value = filtered_content.get(key)
            if isinstance(value, list):
                return _filtered_content_to_raw(value)
        return json.dumps(filtered_content, ensure_ascii=False)
    if isinstance(filtered_content, list):
        lines: list[str] = []
        for item in filtered_content:
            if isinstance(item, (dict, list)):
                lines.append(json.dumps(item, ensure_ascii=False))
            else:
                lines.append(str(item))
        return "\n".join(lines)
    return str(filtered_content)


def _bulk_upload_llm_enabled(config: dict[str, Any]) -> bool:
    analysis_cfg = config.get("analysis", {}) or {}
    if not analysis_cfg.get("enable_llm", True):
        return False
    llm_cfg = config.get("llm", {}) or {}
    if not llm_cfg.get("enable_llm", True):
        return False
    return bool(
        llm_cfg.get("api_key")
        and llm_cfg.get("model")
        and llm_cfg.get("api_base")
    )


def _upload_request_to_response(row: dict[str, Any]) -> dict[str, Any]:
    result = None
    result_json = row.get("result_json")
    if result_json:
        try:
            result = json.loads(str(result_json))
        except json.JSONDecodeError:
            result = None
    transfer_status = str(row.get("transfer_status") or {
        "done": "stored",
    }.get(str(row.get("status") or "pending"), row.get("status") or "pending"))
    legacy_status = {
        "pending": "pending",
        "running": "running",
        "stored": "done",
        "failed": "failed",
    }.get(transfer_status, str(row.get("status") or "pending"))
    analysis_status = str(row.get("analysis_status") or "not_requested")
    transfer_error = str(row.get("transfer_error") or "")
    analysis_error = str(row.get("analysis_error") or "")
    if transfer_status == "failed":
        compatibility_error = transfer_error
    elif analysis_status == "failed":
        compatibility_error = analysis_error
    else:
        compatibility_error = str(row.get("error_message") or "")
    return {
        "request_id": row.get("request_id"),
        "mentor_id": row.get("mentor_id"),
        "student_id": row.get("student_id"),
        "session_id": row.get("session_id") or "",
        "status": legacy_status,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at") or row.get("created_at"),
        "error_message": compatibility_error,
        "result": result,
        "transfer_status": transfer_status,
        "analysis_status": analysis_status,
        "transfer_error": transfer_error,
        "analysis_error": analysis_error,
    }


def _snapshot_from_turns(turns: list[dict[str, Any]]) -> TranscriptSnapshot:
    snap = TranscriptSnapshot()
    for turn in turns:
        role = str(turn.get("role") or "")
        text = str(turn.get("text") or "")
        if role not in {"user", "assistant"} or not text:
            continue
        snap.messages.append(
            Message(role=role, text=text, timestamp=turn.get("ts"))
        )
    return snap


def _latest_user_prompt(turns: list[dict[str, Any]]) -> str:
    for turn in reversed(turns):
        if turn.get("role") == "user" and turn.get("text"):
            return str(turn["text"])
    return ""


async def _analyze_uploaded_session_background(
    context: AppContext,
    student_id: str,
    session_id: str,
    turns: list[dict[str, Any]],
    sha: str,
    request_id: str | None = None,
) -> tuple[bool, str]:
    """Run bounded LLM analysis for an uploaded historical session."""
    started_at = monotonic()
    prompt_hash = ""
    analysis_model = ""
    claim: dict[str, Any] | None = None
    latency_ms = 0
    latency_measured = False
    try:
        snap = _snapshot_from_turns(turns)
        latest_prompt = _latest_user_prompt(turns)
        llm_config = (
            context.analysis_svc._config_with_prompt_overrides()
            if hasattr(context.analysis_svc, "_config_with_prompt_overrides")
            else context.config
        )
        prompt_hash = analysis_prompt_hash(llm_config)
        claim = context.store.claim_raw_transcript_analysis(
            student_id=student_id,
            session_id=session_id,
            content_sha256=sha,
            prompt_hash=prompt_hash,
        )
        await _refresh_upload_parent_projections(
            context,
            student_id,
            claim.get("request_ids", []),
        )
        claim_state = str(claim.get("state") or "")
        if claim_state == "stale":
            return False, "analysis stale transcript"
        if claim_state == "running":
            return False, "analysis already running"
        if claim_state == "done":
            return True, ""
        if claim_state != "claimed":
            return False, "analysis not pending"

        async with context.analysis_svc.analysis_semaphore:
            raw_outcome = await context.analysis_svc.llm(
                llm_config,
                snap,
                "Stop",
                latest_prompt,
            )
        latency_ms = max(0, int(round((monotonic() - started_at) * 1000)))
        latency_measured = True
        outcome = coerce_analysis_outcome(raw_outcome)
        analysis_model = str(outcome.model or "").strip()[:200]
        if not outcome.ok:
            raise RuntimeError(outcome.error or "LLM provider analysis failed")
        traced_value = dict(outcome.value)
        traced_value.update({
            "model": analysis_model,
            "prompt_hash": prompt_hash,
            "latency_ms": latency_ms,
        })
        result = AnalysisResult.from_dict(traced_value)
        session_title = context.store.get_session_title(session_id)
        committed = context.store.commit_bulk_analysis_if_current(
            student_id=student_id,
            session_id=session_id,
            content_sha256=sha,
            raw_id=int(claim["raw_id"]),
            generation=int(claim["generation"]),
            result=result.to_dict(),
            session_title=session_title,
            msg_count=len(snap.messages),
        )
        if committed is None:
            request_ids = context.store.fail_stale_upload_request_sessions(
                student_id=student_id,
                session_id=session_id,
                content_sha256=sha,
            )
            await _refresh_upload_parent_projections(context, student_id, request_ids)
            log.info(
                "bulk analysis discarded stale_sha student=%s session=%s sha=%s",
                student_id,
                session_id[:8],
                sha[:12],
            )
            return False, "analysis stale transcript"
        committed_metadata = committed if isinstance(committed, dict) else {}
        report_id: int | None = None
        try:
            report_id = _committed_report_id_safely(committed_metadata)
            await _project_committed_analysis_safely(context, committed_metadata)
            request_ids = committed_metadata.get("request_ids", [])
            if not isinstance(request_ids, (list, tuple)):
                log.error("upload parent metadata invalid after durable bulk commit")
                request_ids = []
            await _refresh_upload_parent_projections(
                context,
                student_id,
                list(request_ids),
            )
            if report_id is not None:
                analysis_id = int(committed_metadata.get("analysis_id") or 0)
                analysis_row = context.store.get_analysis(analysis_id) or {}
                await _publish_event_safely(context, AnalysisEnvelope(
                    analysis_id=analysis_id,
                    student_id=student_id,
                    session_id=session_id,
                    report_id=report_id,
                    event="BulkUpload",
                    result=result.to_dict(),
                    timestamp=float(analysis_row.get("created_at") or 0.0),
                ).to_dict())
        except Exception:
            log.exception(
                "bulk post-commit projection/fanout failed student=%s session=%s",
                student_id,
                session_id[:8],
            )
        log.info(
            "bulk upload analysis complete student=%s session=%s report_id=%s",
            student_id,
            session_id[:8],
            report_id if report_id is not None else "unknown",
        )
        return True, ""
    except Exception as exc:
        error_code = _stable_background_analysis_error(exc)
        if not latency_measured:
            latency_ms = max(0, int(round((monotonic() - started_at) * 1000)))
        failure = None
        if claim is not None and claim.get("state") == "claimed":
            failure = context.store.fail_raw_transcript_analysis(
                student_id=student_id,
                session_id=session_id,
                content_sha256=sha,
                raw_id=int(claim["raw_id"]),
                generation=int(claim["generation"]),
                error_message=error_code,
                analysis_model=analysis_model,
                prompt_hash=prompt_hash,
                latency_ms=latency_ms,
            )
        if failure is None and claim is not None and claim.get("state") == "claimed":
            request_ids = context.store.fail_stale_upload_request_sessions(
                student_id=student_id,
                session_id=session_id,
                content_sha256=sha,
            )
            await _refresh_upload_parent_projections(context, student_id, request_ids)
            return False, "analysis stale transcript"
        if failure is not None:
            await _project_attention_safely(
                context,
                "project_system_failure",
                "bulk_analysis",
                f"{int(claim['raw_id'])}:{int(claim['generation'])}",
            )
            await _refresh_upload_parent_projections(
                context,
                student_id,
                failure.get("request_ids", []),
            )
        log.error(
            "bulk upload analysis failed student=%s session=%s error=%s type=%s",
            student_id,
            session_id[:8],
            error_code,
            type(exc).__name__,
        )
        return False, error_code


def _stable_background_analysis_error(exc: Exception) -> str:
    """Return a bounded error code without provider response or exception details."""
    message = str(exc)
    if message.startswith("LLM provider HTTP "):
        status = message.removeprefix("LLM provider HTTP ").split(maxsplit=1)[0]
        return f"LLM provider HTTP {status}" if status.isdigit() else "LLM provider error"
    if message == "LLM provider TimeoutError":
        return message
    if message.startswith("LLM provider "):
        return "LLM provider error"
    if message.startswith("LLM response JSON invalid"):
        return "LLM response JSON invalid"
    return f"analysis {type(exc).__name__}"


async def _publish_upload_request_status(
    context: AppContext,
    row: dict[str, Any],
) -> None:
    """Publish a persisted request snapshot to mentor sockets only."""
    snapshot = _upload_request_to_response(row)
    snapshot["result"] = _sanitize_upload_event_result(snapshot.get("result"))
    await _publish_event_safely(context, {
        "type": "upload_request_status",
        **snapshot,
        "timestamp": time.time(),
    })


async def _publish_upload_parent_rows(
    context: AppContext,
    rows: list[dict[str, Any]],
) -> None:
    for row in rows:
        await _publish_upload_request_status(context, row)


async def _refresh_upload_parent_projections(
    context: AppContext,
    student_id: str,
    request_ids: list[Any],
) -> None:
    """Refresh parents after a raw transaction fans out child analysis state."""
    upload_svc = context.upload_svc or UploadRequestService(context.store)
    for request_id in dict.fromkeys(str(item) for item in request_ids if item):
        try:
            rows = upload_svc.refresh_parent_analysis(request_id, student_id)
        except (InvalidStateTransition, UploadRequestNotFound) as exc:
            log.warning(
                "upload parent projection changed request_id=%s error=%s",
                request_id,
                exc,
            )
            continue
        if any(str(row.get("analysis_status") or "") == "failed" for row in rows):
            await _project_attention_safely(
                context,
                "project_system_failure",
                "upload_analysis",
                request_id,
            )
        await _publish_upload_parent_rows(context, rows)


def _sanitize_upload_event_result(value: Any) -> Any:
    """Return only bounded aggregate counters from a client-controlled result."""
    if not isinstance(value, dict):
        return None
    sanitized: dict[str, int] = {}
    for key in ("total", "synced", "skipped", "failed"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 1_000_000:
            sanitized[key] = item
    return sanitized


async def _retry_upload_request_analysis_background(
    context: AppContext,
    request_id: str,
    student_id: str,
    session_id: str,
    raw: str,
    sha: str,
) -> None:
    """Analyze the already-persisted raw transcript and mirror request status."""
    try:
        turns = parse_turns(parse_text(raw).messages)
        await _analyze_uploaded_session_background(
            context,
            student_id,
            session_id,
            turns,
            sha,
            request_id=request_id,
        )
    except (InvalidStateTransition, UploadRequestNotFound) as exc:
        log.warning(
            "upload request retry state changed request_id=%s error=%s",
            request_id,
            exc,
        )


def _prepare_report_recovery(ctx: AppContext) -> tuple[int, ...]:
    """Repair crash state and enumerate work without invoking a provider."""
    if not ctx.report_recovery_prepared:
        interrupted = ctx.store.recover_interrupted_report_analyses(max_attempts=3)
        if interrupted:
            log.warning("recovered %d interrupted Stop analysis claims", interrupted)
        pending_reports = ctx.store.list_recoverable_reports(max_attempts=3)
        ctx.report_recovery_prepared = True
    else:
        pending_reports = ctx.store.list_recoverable_reports(max_attempts=3)
    return tuple(
        int(row["id"])
        for row in pending_reports
        if row.get("event") == "Stop"
    )


async def _recover_pending_reports(
    ctx: AppContext,
    *,
    sleeper=None,
    report_ids: tuple[int, ...] | None = None,
) -> None:
    if report_ids is None:
        report_ids = _prepare_report_recovery(ctx)
    if not report_ids:
        return

    log.warning(
        "recovering %d pending Stop reports from previous process",
        len(report_ids),
    )
    for report_id in report_ids:
        row = ctx.store.get_report(report_id)
        if not row or row.get("event") != "Stop":
            continue
        student_id = str(row.get("student_id") or "")
        session_id = str(row.get("session_id") or "")
        if ctx.store.analysis_exists_for_report(report_id):
            log.warning(
                "pending Stop report_id=%s already has analysis; clearing pending flag",
                report_id,
            )
            ctx.store.mark_report_analysis_done(report_id)
            continue

        persisted_analysis_input = row.get("analysis_input")
        if (
            persisted_analysis_input is None
            and session_id
            and row.get("transcript_path") == EXPLICIT_RAW_TRANSCRIPT_MARKER
        ):
            raw = ctx.store.get_raw_transcript_for_report(report_id)
            if raw is not None:
                ctx.store.set_report_analysis_input_if_missing(
                    report_id,
                    bounded_analysis_input(raw.get("content") or ""),
                )
                row = ctx.store.get_report(report_id) or row
                persisted_analysis_input = row.get("analysis_input")
        if persisted_analysis_input is None:
            ctx.store.mark_report_analysis_input_unavailable(
                report_id,
                max_attempts=3,
            )
            await _project_attention_safely(
                ctx,
                "project_system_failure",
                "stop",
                str(report_id),
            )
            log.error(
                "legacy Stop analysis input unavailable report_id=%s",
                report_id,
            )
            continue
        transcript_content = (
            str(persisted_analysis_input)
            if persisted_analysis_input is not None
            else ""
        )
        log.info(
            "requeue pending Stop report_id=%s student=%s session=%s transcript_bytes=%d",
            report_id,
            student_id,
            (session_id or "?")[:8],
            len(transcript_content.encode("utf-8")),
        )
        kwargs = {"sleeper": sleeper} if sleeper is not None else {}
        try:
            await ctx.analysis_svc.handle_stop_with_retry(
                student_id=student_id,
                session_id=session_id,
                prompt_text=str(row.get("prompt") or ""),
                transcript_content=transcript_content,
                report_id=report_id,
                **kwargs,
            )
        except AnalysisRetriesExhausted as exc:
            log.error(
                "recovered Stop analysis exhausted report_id=%s error=%s",
                report_id,
                exc,
            )


def _log_report_recovery_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(
            "report recovery task failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def _start_report_recovery(
    ctx: AppContext,
    report_ids: tuple[int, ...],
) -> asyncio.Task | None:
    existing = ctx.report_recovery_task
    if existing is not None:
        return existing
    if not report_ids:
        return None
    task = asyncio.create_task(
        _recover_pending_reports(ctx, report_ids=report_ids),
        name="report-recovery",
    )
    task.add_done_callback(_log_report_recovery_failure)
    ctx.report_recovery_task = task
    return task


def create_app(context: AppContext | None = None) -> FastAPI:
    ctx = context or build_context()
    ensure_attention_service(ctx)
    startup_upload_svc = ctx.upload_svc or UploadRequestService(ctx.store)
    if ctx.upload_svc is None:
        ctx.upload_svc = startup_upload_svc
    validate_auth_config(ctx.config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("Copilot service starting, student=%s", ctx.config.get("student_id", ""))
        try:
            acquire_worker_lock(ctx)
            recovered_uploads = startup_upload_svc.recover_interrupted_analysis()
            if recovered_uploads:
                log.warning(
                    "recovered %d interrupted upload analyses",
                    len(recovered_uploads),
                )
            report_ids = _prepare_report_recovery(ctx)
            await _project_attention_safely(
                ctx,
                "backfill_missing",
                publish=False,
                page_size=100,
                max_sources=1000,
            )
            _start_report_recovery(ctx, report_ids)
            yield
        finally:
            task = ctx.report_recovery_task
            requested_cancel = False
            try:
                if task is not None and not task.done():
                    requested_cancel = True
                    task.cancel()
                if task is not None:
                    try:
                        await task
                    except asyncio.CancelledError:
                        if not requested_cancel:
                            raise
            finally:
                ctx.report_recovery_task = None
                ctx.report_recovery_prepared = False
                release_worker_lock(ctx)
                log.info("Copilot service stopped")

    app = FastAPI(title="WorkBuddy Copilot", version="0.2.0", lifespan=lifespan)
    app.state.context = ctx

    from .mentor.routes import router as mentor_router

    app.include_router(mentor_router, dependencies=[Depends(require_mentor_token)])

    static_dir = Path(__file__).parent / "static" / "mentor"
    if static_dir.exists():
        from fastapi.staticfiles import StaticFiles

        app.mount(
            "/mentor",
            StaticFiles(directory=str(static_dir), html=True),
            name="mentor-static",
        )

    @app.get("/health")
    async def health(context: AppContext = Depends(get_context)):
        return {"status": "UP", "student": context.config.get("student_id", "")}

    @app.post("/report", status_code=202)
    async def report(
        data: ReportIn,
        background_tasks: BackgroundTasks,
        principal: StudentPrincipal = Depends(require_student_principal),
        analysis_svc: AnalysisService = Depends(get_analysis_service),
    ):
        student_id = resolve_student_id(principal, data.student_id)
        transcript_content = data.transcript_tail or ""
        try:
            accepted = analysis_svc.accept_report(
                student_id=student_id,
                session_id=data.session_id,
                event=data.event,
                prompt_text=data.prompt,
                transcript_content=transcript_content,
                raw_transcript_content=data.transcript_full,
                cwd=data.cwd,
                event_id=data.event_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        report_id, session_id, snap = accepted
        duplicate = bool(getattr(accepted, "duplicate", False))
        analysis_status = str(
            getattr(accepted, "analysis_status", "not_requested")
        )
        log.info(
            "report accepted student=%s session=%s event=%s msgs=%d tools=%d",
            student_id,
            (session_id or "?")[:8],
            data.event,
            len(snap.messages),
            snap.tool_calls,
        )

        body: dict[str, Any] = {
            "status": "accepted",
            "report_id": report_id,
            "duplicate": duplicate,
            "analysis_status": analysis_status,
        }
        if data.event == "UserPromptSubmit":
            body["prompt_id"] = await analysis_svc.handle_user_prompt_submit(
                student_id,
                session_id,
                data.prompt,
                report_id=report_id,
            )
        elif data.event == "Stop" and (
            not duplicate
            or analysis_svc.is_report_analysis_recoverable(report_id)
        ):
            background_tasks.add_task(
                _handle_stop_background,
                analysis_svc,
                student_id,
                session_id,
                data.prompt,
                transcript_content,
                report_id,
            )
        return body

    @app.get("/recent")
    async def recent(
        limit: int = 20,
        student_id: str | None = None,
        session_id: str | None = None,
        principal: StudentPrincipal = Depends(require_student_principal),
        store: Store = Depends(get_store),
    ):
        resolved_student_id = resolve_student_id(principal, student_id)
        return {
            "items": store.recent_analyses(
                resolved_student_id,
                limit=limit,
                session_id=session_id,
            )
        }

    @app.get("/api/student/analyses")
    async def student_analysis_catch_up(
        after_report_id: int = 0,
        after_analysis_id: int | None = None,
        limit: int = 64,
        student_id: str | None = None,
        principal: StudentPrincipal = Depends(require_student_principal),
        store: Store = Depends(get_store),
    ):
        """Return a scoped, durable ASC page matching the WS envelope."""
        resolved_student_id = resolve_student_id(principal, student_id)
        bounded_limit = max(1, min(int(limit), 100))
        use_commit_cursor = after_analysis_id is not None
        cursor = max(
            0,
            int(after_analysis_id if use_commit_cursor else after_report_id),
        )
        if use_commit_cursor:
            rows = store.analysis_envelopes_after_commit(
                resolved_student_id,
                after_analysis_id=cursor,
                limit=bounded_limit + 1,
            )
            cursor_field = "analysis_id"
        else:
            # Backward compatibility for old clients. New resident clients
            # always use the durable analysis commit cursor above.
            rows = store.analysis_envelopes_after(
                resolved_student_id,
                after_report_id=cursor,
                limit=bounded_limit + 1,
            )
            cursor_field = "report_id"
        has_more = len(rows) > bounded_limit
        items = rows[:bounded_limit]
        next_cursor = (
            int(items[-1][cursor_field])
            if items
            else cursor
        )
        return {
            "items": items,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "cursor_kind": cursor_field,
        }

    @app.post("/api/sessions/sync")
    async def sync_sessions(
        data: SessionsSyncIn,
        principal: StudentPrincipal = Depends(require_student_principal),
        store: Store = Depends(get_store),
    ):
        """Accept student-machine session inventory and upsert it into copilot.db."""
        student_id = resolve_student_id(principal, data.student_id)
        store.upsert_student(student_id)
        synced = 0
        for session in data.sessions:
            if not session.session_id:
                log.warning("skip sync session with empty session_id student=%s", student_id)
                continue
            try:
                store.upsert_session(
                    session_id=session.session_id,
                    student_id=student_id,
                    work_dir=session.work_dir,
                    title=session.title,
                    created_at=session.created_at,
                    last_activity_at=session.last_activity_at,
                    group_type=session.group_type,
                    space_name=session.space_name,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            synced += 1
        log.info("sessions sync accepted student=%s synced=%d", student_id, synced)
        return {"ok": True, "synced": synced}

    @app.post("/api/student/sessions/{session_id}/transcript")
    async def upload_session_transcript(
        session_id: str,
        data: TranscriptUploadIn,
        background_tasks: BackgroundTasks,
        principal: StudentPrincipal = Depends(require_student_principal),
        context: AppContext = Depends(get_context),
        store: Store = Depends(get_store),
        upload_svc: UploadRequestService = Depends(get_upload_service),
    ):
        """Accept one already-filtered session transcript from a student client."""
        student_id = resolve_student_id(principal, data.student_id)
        sha = data.sha.strip()
        if not sha:
            raise HTTPException(status_code=400, detail="sha is required")

        request_id = (data.request_id or "").strip() or None
        if data.analysis_mode == "store_only":
            if request_id is not None:
                raise HTTPException(
                    status_code=400,
                    detail="store_only cannot be attached to a mentor upload request",
                )
            if data.source_event_id is None or not data.source_report_id:
                raise HTTPException(
                    status_code=400,
                    detail="store_only requires a source Stop report",
                )
            raw = _filtered_content_to_raw(data.filtered_content)
            verified_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            if sha != verified_sha:
                raise HTTPException(status_code=409, detail="transcript sha mismatch")
            existing = store.get_raw_transcript_for_student_session_sha(
                student_id,
                session_id,
                sha,
            )
            turns = parse_turns(parse_text(raw).messages)
            try:
                stored_result = store.replace_session_messages_from_stop(
                    session_id=session_id,
                    student_id=student_id,
                    turns=turns,
                    raw=raw,
                    sha=sha,
                    source_report_id=int(data.source_report_id),
                    source_event_id=str(data.source_event_id),
                )
            except ActiveTranscriptAnalysisConflict as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "transcript_analysis_active",
                        "message": str(exc),
                        "retryable": True,
                    },
                    headers={"Retry-After": "2"},
                ) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            obsolete = bool(stored_result.get("obsolete"))
            if not obsolete:
                store.mark_raw_transcript_store_only(
                    session_id=session_id,
                    student_id=student_id,
                    content_sha256=sha,
                )
            return {
                "ok": True,
                "skipped": bool(stored_result.get("skipped")) or existing is not None,
                "session_id": session_id,
                "sha": sha,
                "stored": (
                    0
                    if existing is not None or bool(stored_result.get("skipped"))
                    else int(stored_result.get("stored") or 0)
                ),
                "analysis_scheduled": False,
                "retry_analysis": False,
                "analysis_mode": "store_only",
                "obsolete": obsolete,
            }
        analysis_scheduled = _bulk_upload_llm_enabled(context.config)

        known = store.get_known_session_shas(student_id)
        known_entry = known.get(session_id) or {}
        raw_row = store.get_raw_transcript_for_student_session_sha(
            student_id, session_id, sha
        )
        registered_child_status: str | None = None
        if request_id:
            raw_status = str((raw_row or {}).get("analysis_status") or "")
            if not analysis_scheduled:
                child_status = "not_requested"
            else:
                child_status = "done" if raw_status == "done" else "pending"
            try:
                child, parent_rows = upload_svc.register_session(
                    request_id,
                    student_id,
                    session_id,
                    sha,
                    analysis_status=child_status,
                )
            except UploadRequestNotFound as exc:
                raise HTTPException(status_code=404, detail="upload request not found") from exc
            except InvalidStateTransition as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            registered_child_status = str(child.get("analysis_status") or "pending")
            await _publish_upload_parent_rows(context, parent_rows)
            known = store.get_known_session_shas(student_id)
            known_entry = known.get(session_id) or {}
            raw_row = store.get_raw_transcript_for_student_session_sha(
                student_id, session_id, sha
            )
        if known_entry.get("sha") == sha:
            raw_status = str((raw_row or {}).get("analysis_status") or "")
            retry_analysis = (
                bool(raw_row)
                and analysis_scheduled
                and (
                    registered_child_status == "pending"
                    if registered_child_status is not None
                    else raw_status not in {"done", "running"}
                )
            )
            if retry_analysis:
                raw = str((raw_row or {}).get("content") or "")
                turns = parse_turns(parse_text(raw).messages)
                if raw_status in {"", "skipped"}:
                    store.queue_raw_transcript_analysis(
                        student_id=student_id,
                        session_id=session_id,
                        content_sha256=sha,
                    )
                background_tasks.add_task(
                    _analyze_uploaded_session_background,
                    context,
                    student_id,
                    session_id,
                    turns,
                    sha,
                    request_id,
                )
                log.info(
                    "bulk transcript unchanged; scheduling retryable analysis student=%s session=%s sha=%s",
                    student_id,
                    session_id[:8],
                    sha[:12],
                )
                return {
                    "ok": True,
                    "skipped": True,
                    "session_id": session_id,
                    "sha": sha,
                    "stored": 0,
                    "analysis_scheduled": True,
                    "retry_analysis": True,
                }
            log.info(
                "bulk transcript skipped unchanged student=%s session=%s sha=%s",
                student_id,
                session_id[:8],
                sha[:12],
            )
            return {
                "ok": True,
                "skipped": True,
                "session_id": session_id,
                "sha": sha,
                "stored": 0,
                "analysis_scheduled": False,
                "retry_analysis": False,
            }

        raw = _filtered_content_to_raw(data.filtered_content)
        snap = parse_text(raw)
        turns = parse_turns(snap.messages)
        try:
            stored = store.replace_session_messages(
                session_id=session_id,
                student_id=student_id,
                turns=turns,
                raw=raw,
                sha=sha,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if analysis_scheduled:
            store.queue_raw_transcript_analysis(
                student_id=student_id,
                session_id=session_id,
                content_sha256=sha,
            )
            background_tasks.add_task(
                _analyze_uploaded_session_background,
                context,
                student_id,
                session_id,
                turns,
                sha,
                request_id,
            )
        else:
            store.set_raw_transcript_analysis_status(
                session_id,
                student_id,
                status="skipped",
                error_message="",
                content_sha256=sha,
            )
        log.info(
            "bulk transcript accepted student=%s session=%s messages=%d sha=%s llm=%s",
            student_id,
            session_id[:8],
            stored,
            sha[:12],
            analysis_scheduled,
        )
        return {
            "ok": True,
            "skipped": False,
            "session_id": session_id,
            "sha": sha,
            "stored": stored,
            "analysis_scheduled": analysis_scheduled,
            "retry_analysis": False,
        }

    @app.get("/api/transcripts/known")
    async def known_transcript_shas(
        student_id: str | None = None,
        manifest_version: int = 1,
        principal: StudentPrincipal = Depends(require_student_principal),
        store: Store = Depends(get_store),
    ):
        resolved_student_id = resolve_student_id(principal, student_id)
        manifest = store.get_known_session_shas(resolved_student_id)
        if manifest_version >= 2:
            return manifest
        return {session_id: entry["sha"] for session_id, entry in manifest.items()}

    @app.post("/api/mentor/students/{student_id}/request-upload")
    async def request_student_upload(
        student_id: str,
        data: MentorUploadRequestIn | None = None,
        _: None = Depends(require_mentor_token),
        context: AppContext = Depends(get_context),
        upload_svc: UploadRequestService = Depends(get_upload_service),
    ):
        body = data or MentorUploadRequestIn()
        mentor_id = (body.mentor_id or "mentor").strip() or "mentor"
        session_id = (body.session_id or "").strip() or None
        request_id = upload_svc.create(
            mentor_id=mentor_id,
            student_id=student_id,
            session_id=session_id,
        )
        payload = {
            "type": "mentor_command",
            "student_id": student_id,
            "command": "upload_conversations",
            "request_id": request_id,
            "session_id": session_id or "",
            "mentor_id": mentor_id,
            "timestamp": time.time(),
        }
        await _publish_event_safely(context, payload)
        log.info(
            "upload requested mentor=%s student=%s session=%s request_id=%s",
            mentor_id,
            student_id,
            session_id or "*",
            request_id,
        )
        return {
            "request_id": request_id,
            "status": "pending",
            "student_id": student_id,
            "session_id": session_id or "",
            "transfer_status": "pending",
            "analysis_status": "not_requested",
        }

    @app.get("/api/mentor/upload-requests/{request_id}")
    async def get_mentor_upload_request(
        request_id: str,
        _: None = Depends(require_mentor_token),
        upload_svc: UploadRequestService = Depends(get_upload_service),
    ):
        try:
            row = upload_svc.get(request_id)
        except UploadRequestNotFound as exc:
            raise HTTPException(status_code=404, detail="upload request not found") from exc
        return _upload_request_to_response(row)

    @app.post(
        "/api/mentor/upload-requests/{request_id}/retry-analysis",
        status_code=202,
    )
    async def retry_mentor_upload_analysis(
        request_id: str,
        background_tasks: BackgroundTasks,
        _: None = Depends(require_mentor_token),
        context: AppContext = Depends(get_context),
        upload_svc: UploadRequestService = Depends(get_upload_service),
    ):
        try:
            pending, work_items = upload_svc.prepare_analysis_retry(request_id)
        except UploadRequestNotFound as exc:
            raise HTTPException(status_code=404, detail="upload request not found") from exc
        except UploadTranscriptNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidStateTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        response = _upload_request_to_response(pending)
        await _publish_upload_request_status(context, pending)
        for item in work_items:
            raw_row = item["raw"]
            background_tasks.add_task(
                _retry_upload_request_analysis_background,
                context,
                request_id,
                str(pending.get("student_id") or ""),
                str(item.get("session_id") or ""),
                str(raw_row.get("content") or ""),
                str(item.get("sha") or ""),
            )
        return response

    @app.get("/api/student/upload-requests")
    async def list_student_upload_requests(
        student_id: str | None = None,
        status: Literal["pending", "running", "done", "failed", "all"] = "pending",
        principal: StudentPrincipal = Depends(require_student_principal),
        upload_svc: UploadRequestService = Depends(get_upload_service),
    ):
        resolved_student_id = resolve_student_id(principal, student_id)
        status_value = None if status == "all" else status
        return {"items": [
            _upload_request_to_response(row)
            for row in upload_svc.list(
                student_id=resolved_student_id,
                status=status_value,
            )
        ]}

    @app.post("/api/student/upload-requests/{request_id}/status")
    async def update_student_upload_request_status(
        request_id: str,
        data: UploadRequestStatusIn,
        principal: StudentPrincipal = Depends(require_student_principal),
        context: AppContext = Depends(get_context),
        upload_svc: UploadRequestService = Depends(get_upload_service),
    ):
        student_id = resolve_student_id(principal, data.student_id)
        transfer_status = "stored" if data.status == "done" else data.status
        try:
            row = upload_svc.mark_transfer(
                request_id,
                student_id,
                transfer_status,
                error=data.error_message,
                result=data.result,
            )
        except UploadRequestNotFound as exc:
            raise HTTPException(status_code=404, detail="upload request not found")
        except InvalidStateTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if transfer_status == "failed":
            await _project_attention_safely(
                context,
                "project_system_failure",
                "upload_transfer",
                request_id,
            )
        await _publish_upload_request_status(context, row)
        parent_rows = upload_svc.refresh_parent_analysis(request_id, student_id)
        if any(
            str(parent.get("analysis_status") or "") == "failed"
            for parent in parent_rows
        ):
            await _project_attention_safely(
                context,
                "project_system_failure",
                "upload_analysis",
                request_id,
            )
        await _publish_upload_parent_rows(context, parent_rows)
        latest = parent_rows[-1] if parent_rows else row
        return _upload_request_to_response(latest)

    @app.get("/sessions")
    async def list_sessions(
        student_id: str | None = None,
        limit: int = 10,
        principal: StudentPrincipal = Depends(require_student_principal),
        session_svc: SessionQueryService = Depends(get_session_service),
    ):
        sid = resolve_student_id(principal, student_id)
        conversations = session_svc.list_sessions(sid, limit=limit)
        return {"items": [c.__dict__ for c in conversations]}

    @app.get("/current_session")
    async def current_session(
        work_dir: str | None = None,
        student_id: str | None = None,
        principal: StudentPrincipal = Depends(require_student_principal),
        session_svc: SessionQueryService = Depends(get_session_service),
    ):
        sid = resolve_student_id(principal, student_id)
        active = session_svc.get_active_session(work_dir, student_id=sid)
        if not active:
            return {"session_id": None, "items": []}
        all_sessions = session_svc.list_all_sessions_with_title(work_dir, student_id=sid, limit=8)
        items = [{
            "session_id": s["session_id"],
            "work_dir": s["work_dir"],
            "resumed_at": s["resumed_at"],
            "session_title": s.get("title", ""),
            "is_active": s["session_id"] == active["session_id"],
        } for s in all_sessions]
        return {
            "session_id": active["session_id"],
            "work_dir": active["work_dir"],
            "resumed_at": active["resumed_at"],
            "items": items,
        }

    @app.get("/alerts/unread")
    async def unread_alerts(
        since: float = 0.0,
        student_id: str | None = None,
        principal: StudentPrincipal = Depends(require_student_principal),
        store: Store = Depends(get_store),
    ):
        resolved_student_id = resolve_student_id(principal, student_id)
        return {"items": store.unread_alerts(since, resolved_student_id)}

    @app.post("/api/mentor/message")
    async def send_mentor_message(
        data: MentorMessageIn,
        _: None = Depends(require_mentor_token),
        message_svc: MessageService = Depends(get_message_service),
    ):
        try:
            return await message_svc.send(
                student_id=data.student_id,
                mentor_id=data.mentor_id,
                text=data.text,
                client_request_id=data.client_request_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/mentor/messages/status")
    async def get_mentor_message_statuses(
        data: MentorMessageStatusIn,
        _: None = Depends(require_mentor_token),
        message_svc: MessageService = Depends(get_message_service),
    ):
        if not 1 <= len(data.client_request_ids) <= 300:
            raise HTTPException(
                status_code=422,
                detail="client_request_ids must contain 1 to 300 items",
            )
        return {
            "items": message_svc.get_mentor_message_statuses(
                [str(value) for value in data.client_request_ids],
            )
        }

    @app.get("/api/student/messages")
    async def get_student_messages(
        student_id: str | None = None,
        since: int = 0,
        limit: int | None = None,
        principal: StudentPrincipal = Depends(require_student_principal),
        message_svc: MessageService = Depends(get_message_service),
    ):
        resolved_student_id = resolve_student_id(principal, student_id)
        return {
            "items": message_svc.get_catchup(
                resolved_student_id,
                since,
                limit=limit,
            )
        }

    @app.get("/api/student/messages/pending-receipts")
    async def get_pending_student_message_receipts(
        student_id: str | None = None,
        limit: int = 64,
        after_id: int = 0,
        principal: StudentPrincipal = Depends(require_student_principal),
        message_svc: MessageService = Depends(get_message_service),
    ):
        resolved_student_id = resolve_student_id(principal, student_id)
        return {
            "items": message_svc.get_pending_receipts(
                resolved_student_id,
                limit=limit,
                after_id=after_id,
            )
        }

    @app.post("/api/student/messages/ack")
    async def ack_student_message(
        data: StudentMessageAckIn,
        principal: StudentPrincipal = Depends(require_student_principal),
        message_svc: MessageService = Depends(get_message_service),
    ):
        student_id = resolve_student_id(principal, data.student_id)
        result = message_svc.ack(data.message_id, student_id)
        ok = await result if inspect.isawaitable(result) else result
        if not ok:
            raise HTTPException(status_code=404, detail="message not found")
        return {"ok": True}

    @app.post("/api/student/ask")
    async def ask_copilot(
        data: StudentAskIn,
        principal: StudentPrincipal = Depends(require_student_principal),
        context: AppContext = Depends(get_context),
        store: Store = Depends(get_store),
    ):
        student_id = resolve_student_id(principal, data.student_id)
        question = data.question.strip()
        session_id = (data.session_id or "").strip() or None
        if not question:
            raise HTTPException(status_code=400, detail="question is required")
        if session_id:
            try:
                store.ensure_session_owner(session_id, student_id)
            except ValueError:
                raise HTTPException(
                    status_code=409,
                    detail="session belongs to another student",
                )

        context_messages = _build_student_question_context(store, student_id, session_id)
        try:
            raw_outcome = await asyncio.wait_for(
                llm_answer_question(context.config, question, context_messages),
                timeout=_student_ask_timeout(context.config),
            )
            outcome = coerce_question_answer_outcome(raw_outcome)
        except asyncio.TimeoutError:
            log.warning("student ask LLM call exceeded outer timeout")
            outcome = QuestionAnswerOutcome(
                status="failed",
                answer=question_fallback_answer(),
                error_code="llm_timeout",
            )
        except Exception as exc:
            log.warning(
                "student ask LLM call failed type=%s",
                type(exc).__name__,
            )
            outcome = QuestionAnswerOutcome(
                status="failed",
                answer=question_fallback_answer(),
                error_code="llm_provider_error",
            )

        try:
            ask_id = store.add_student_ask(
                student_id=student_id,
                session_id=session_id,
                question=question,
                answer=outcome.answer,
                answer_status=outcome.status,
                error_code=outcome.error_code,
            )
        except ValueError:
            raise HTTPException(
                status_code=409,
                detail="session belongs to another student",
            )
        await _project_attention_safely(
            context,
            "project_student_ask",
            ask_id,
        )
        await _publish_event_safely(context, {
            "type": "student_ask",
            "student_id": student_id,
            "session_id": session_id or "",
            "ask_id": ask_id,
            "question": question[:300],
            "answer_status": outcome.status,
            "error_code": outcome.error_code,
            "needs_attention": outcome.status != "answered",
            "timestamp": time.time(),
        })
        return {
            "ask_id": ask_id,
            "answer": outcome.answer,
            "status": outcome.status,
            "needs_attention": outcome.status != "answered",
        }

    @app.post("/api/student/asks/{ask_id}/feedback")
    async def record_student_ask_feedback(
        ask_id: int,
        data: StudentAskFeedbackIn,
        principal: StudentPrincipal = Depends(require_student_principal),
        context: AppContext = Depends(get_context),
        store: Store = Depends(get_store),
    ):
        student_id = resolve_student_id(principal, data.student_id)
        note = (data.note or "").strip()
        if len(note) > 500:
            raise HTTPException(status_code=422, detail="feedback note is too long")
        try:
            row, updated = store.record_student_ask_feedback(
                ask_id=ask_id,
                student_id=student_id,
                feedback=data.feedback,
                note=note,
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="student ask not found")
        except PermissionError:
            raise HTTPException(status_code=403, detail="student ask owner mismatch")
        except ValueError:
            raise HTTPException(status_code=409, detail="student ask feedback conflict")
        await _project_attention_safely(
            context,
            "project_student_ask",
            ask_id,
        )
        return {
            "ask_id": ask_id,
            "feedback": row["feedback"],
            "feedback_note": row["feedback_note"],
            "feedback_at": row["feedback_at"],
            "updated": updated,
        }

    @app.delete("/api/admin/students/{student_id}")
    async def delete_student(
        student_id: str,
        _: None = Depends(require_mentor_token),
        store: Store = Depends(get_store),
    ):
        return {"deleted": store.delete_student(student_id)}

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        context: AppContext = ws.app.state.context
        registry = context.ws_registry
        supplied_student_id = ws.query_params.get("student_id")
        token = _extract_supplied_token(
            ws.headers.get("authorization"),
            ws.headers.get("x-copilot-token"),
        ) or ws.query_params.get("token")
        principal = student_principal_for_token(context.config, token)
        if principal is None:
            await ws.close(code=1008)
            return
        try:
            student_id = resolve_student_id(principal, supplied_student_id)
        except HTTPException:
            await ws.close(code=1008)
            return
        await ws.accept()
        registry.register_float(student_id, ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.warning("float WS error: %s", exc)
        finally:
            registry.unregister_float(student_id, ws)

    @app.websocket("/ws/mentor")
    async def mentor_ws(ws: WebSocket):
        context: AppContext = ws.app.state.context
        registry = context.ws_registry
        token = ws.query_params.get("token")
        if not token_is_valid(context.config, token, role="mentor"):
            await ws.close(code=1008)
            return
        await ws.accept()
        registry.register_mentor(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.warning("mentor WS error: %s", exc)
        finally:
            registry.unregister_mentor(ws)

    return app


app = create_app()
ws_clients = app.state.context.ws_registry.floats
mentor_ws_clients = app.state.context.ws_registry.mentors
