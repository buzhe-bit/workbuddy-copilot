"""导师观察台 HTTP 路由（Controller 层）。

只做 HTTP 编解码 + 调用 Service 层，不直接操作 Store。

  GET  /api/mentor/students                       学员列表 + 状态概览
  GET  /api/mentor/students/{student_id}/sessions 某学员的对话列表
  GET  /api/mentor/sessions/{session_id}/timeline 按时间排序的事件列表
"""
from __future__ import annotations

import logging
from pathlib import Path
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..app_context import (
    get_attention_service,
    get_session_service,
    get_store,
    get_ws_registry,
)
from ..attention import AttentionService
from ..connections import WSRegistry
from ..services import SessionQueryService
from ..store import Store
from ..student_platform.windows_evidence import validate_windows_evidence

router = APIRouter(prefix="/api/mentor", tags=["mentor"])
log = logging.getLogger("copilot.mentor.routes")


class AttentionUpdateIn(BaseModel):
    status: Literal["in_progress", "resolved", "dismissed"]
    mentor_id: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=500)


def _strip_think_blocks(text: str) -> str:
    """Remove model reasoning blocks from reply text before showing mentors."""
    cleaned = re.sub(r"<think\b[^>]*>.*?</think>", "", text or "", flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()


def _rebuild_transcript_from_history(store: Store, session_id: str) -> dict[str, Any] | None:
    """Rebuild readable transcript text from prompt and AI summary history."""
    prompts = store.get_prompts_by_session(session_id)
    summaries = store.get_ai_summaries_by_session(session_id)
    if not prompts and not summaries:
        return None

    events: list[tuple[float, int, int, str, str, float | None]] = []
    created_values: list[float] = []

    def add_event(row: dict[str, Any], priority: int, index: int, speaker: str) -> None:
        raw_created = row.get("created_at")
        created = float(raw_created) if raw_created is not None else None
        if created is not None:
            created_values.append(created)
        events.append((
            created if created is not None else 0.0,
            priority,
            index,
            speaker,
            str(row.get("content") or ""),
            created,
        ))

    for idx, prompt in enumerate(prompts):
        add_event(prompt, 0, idx, "学员")
    for idx, summary in enumerate(summaries):
        add_event(summary, 1, idx, "AI")

    events.sort(key=lambda event: (event[0], event[1], event[2]))
    content = "\n".join(f"{speaker}：{text}" for _, _, _, speaker, text, _ in events)
    return {
        "content": content,
        "created_at": min(created_values) if created_values else None,
    }


def _windows_rollout_status(config: dict[str, Any]) -> str:
    """Derive the Windows gate only from validated evidence for this build."""
    raw_rollout = config.get("windows_rollout")
    rollout = raw_rollout if isinstance(raw_rollout, dict) else {}
    configured_path = str(rollout.get("evidence_path") or "").strip()
    if configured_path:
        evidence_path = Path(configured_path).expanduser()
    else:
        db_path = Path(str(config.get("store", {}).get("db_path") or "."))
        evidence_path = db_path.parent / "windows-w1-evidence.json"
    result = validate_windows_evidence(
        evidence_path,
        str(rollout.get("expected_commit") or ""),
        str(rollout.get("expected_build") or ""),
        expected_runner_id=str(rollout.get("expected_runner_id") or "") or None,
    )
    return result.verdict


@router.get("/students")
async def list_students(session_svc: SessionQueryService = Depends(get_session_service)):
    """返回学员列表 + 状态概览。"""
    students = session_svc.list_students()
    return {"items": [s.__dict__ for s in students]}


@router.get("/system-status")
async def get_system_status(
    request: Request,
    store: Store = Depends(get_store),
    ws_registry: WSRegistry = Depends(get_ws_registry),
):
    """Return mentor-only operational counts with no learner or provider data."""
    counts = store.get_system_status_counts()
    float_connections, mentor_connections = ws_registry.connection_counts()
    config = request.app.state.context.config
    return {
        "version": request.app.version,
        **counts,
        "float_connections": float_connections,
        "mentor_connections": mentor_connections,
        "windows_rollout_status": _windows_rollout_status(config),
    }


@router.get("/attention")
async def list_attention(
    status: Literal["open", "in_progress", "resolved", "dismissed"] | None = None,
    priority: Literal["high", "medium"] | None = None,
    category: Literal["learning", "system"] | None = None,
    student_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    attention_svc: AttentionService = Depends(get_attention_service),
):
    """Return the authoritative mentor attention queue."""
    return {
        "items": attention_svc.list_attention(
            status=status,
            priority=priority,
            category=category,
            student_id=student_id,
            limit=limit,
        ),
    }


@router.patch("/attention/{item_id}")
async def update_attention(
    item_id: int,
    body: AttentionUpdateIn,
    attention_svc: AttentionService = Depends(get_attention_service),
):
    """Apply a transactional queue transition and publish true changes."""
    try:
        return await attention_svc.update_attention(
            item_id,
            status=body.status,
            mentor_id=body.mentor_id,
            note=body.note,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        if str(exc) == "attention status conflict":
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/students/{student_id}/sessions")
async def list_student_sessions(
    student_id: str,
    session_svc: SessionQueryService = Depends(get_session_service),
):
    """返回指定学员的最近活跃对话列表。"""
    conversations = session_svc.list_sessions(student_id)
    items = []
    for c in conversations:
        row = c.__dict__.copy()
        row["session_title"] = row.pop("title", "")
        items.append(row)
    return {"items": items}


@router.get("/sessions/{session_id}/timeline")
async def get_timeline(
    session_id: str,
    session_svc: SessionQueryService = Depends(get_session_service),
):
    """返回指定会话按时间排序的事件列表（prompt|ai_summary|analysis）。"""
    entries = session_svc.get_timeline(session_id)
    return {"items": [e.__dict__ for e in entries]}


@router.get("/sessions/{session_id}/transcript")
async def get_transcript(
    session_id: str,
    store: Store = Depends(get_store),
):
    """返回指定会话的完整原文 transcript。"""
    row = store.get_raw_transcript(session_id)
    if not row:
        rebuilt = _rebuild_transcript_from_history(store, session_id)
        if not rebuilt:
            raise HTTPException(status_code=404, detail="transcript not found")
        log.info("raw transcript missing; rebuilt from history session_id=%s", session_id)
        return rebuilt
    return {
        "content": row.get("content", ""),
        "created_at": row.get("created_at"),
    }


@router.get("/prompts/{prompt_id}/reply")
async def get_prompt_reply(
    prompt_id: int,
    store: Store = Depends(get_store),
):
    """Return the full assistant reply text for one student prompt."""
    prompt = store.get_prompt(prompt_id)
    if not prompt:
        raise HTTPException(status_code=404, detail="prompt not found")
    reply = store.get_prompt_reply(
        str(prompt.get("session_id") or ""),
        int(prompt.get("seq_in_session") or 0),
    )
    return {"reply": _strip_think_blocks(reply)}


@router.get("/replies/{reply_ref}/text")
async def get_reply_text(
    reply_ref: str,
    store: Store = Depends(get_store),
):
    """Return the full assistant reply text for a prompt/message reply ref."""
    if ":" not in reply_ref:
        raise HTTPException(status_code=404, detail="reply not found")
    kind, raw_id = reply_ref.split(":", 1)
    try:
        numeric_id = int(raw_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="reply not found") from exc

    if kind == "prompt":
        reply = store.get_prompt_reply_by_id(numeric_id)
    elif kind == "msg":
        reply = store.get_message_reply_by_id(numeric_id)
    else:
        raise HTTPException(status_code=404, detail="reply not found")

    reply = _strip_think_blocks(reply)
    if not reply:
        raise HTTPException(status_code=404, detail="reply not found")
    return {"reply": reply}
