"""测试 service.py /report 按事件分流落库。"""
from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from copilot.app_context import AppContext, get_analysis_service, get_store
from copilot.connections import WSRegistry
from copilot.eventbus import EventBus
from copilot.service import (
    _handle_stop_background,
    _recover_pending_reports,
    app,
    create_app,
)
from copilot.services import AnalysisService, MessageService, SessionQueryService
from copilot.store import Store


class FakeAnalysisService:
    def __init__(self):
        self.accept_calls = []
        self.prompt_calls = []
        self.stop_calls = []

    def accept_report(self, **kwargs):
        self.accept_calls.append(kwargs)
        snap = SimpleNamespace(messages=[], tool_calls=0, session_id="sess-1", ai_title="title")
        return 1, kwargs.get("session_id") or "sess-1", snap

    async def handle_user_prompt_submit(
        self, student_id, session_id, prompt_text, *, report_id=None,
    ):
        self.prompt_calls.append((student_id, session_id, prompt_text))
        return 10

    async def handle_stop(self, student_id, session_id, prompt_text, transcript_content, report_id):
        self.stop_calls.append((student_id, session_id, prompt_text, transcript_content, report_id))
        return SimpleNamespace(topic="done")

    async def handle_stop_with_retry(
        self,
        student_id,
        session_id,
        prompt_text,
        transcript_content,
        report_id,
        **kwargs,
    ):
        return await self.handle_stop(
            student_id,
            session_id,
            prompt_text,
            transcript_content,
            report_id,
        )


class FakeStore:
    def __init__(self):
        self.recent_calls = []

    def recent_analyses(self, student_id, limit=20, session_id=None):
        self.recent_calls.append((student_id, limit, session_id))
        return []


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _build_real_report_app(tmp_path):
    store = Store(tmp_path / "copilot.db")
    bus = EventBus()
    registry = WSRegistry(send_timeout=0.05)
    bus.subscribe(registry.handle_event)
    llm_calls = []

    async def fake_llm(config, snap, event, latest_prompt):
        llm_calls.append({
            "event": event,
            "latest_prompt": latest_prompt,
            "messages": [(message.role, message.text) for message in snap.messages],
        })
        return {
            "topic": "tail analysis",
            "understanding": "medium",
            "off_topic": False,
            "stuck_at": "",
            "is_technical": True,
            "severity": "info",
            "diagnosis": "The uploaded tail was analyzed.",
            "suggestion": "Continue with a minimal reproduction.",
            "progress": "debugging",
            "guidance": "Inspect the boundary condition.",
            "alert": "",
            "ai_reply_summary": "The assistant suggested checking the boundary.",
        }

    config = {
        "student_id": "server",
        "service": {"host": "127.0.0.1", "port": 8765},
        "store": {"db_path": str(tmp_path / "copilot.db")},
        "llm": {},
    }
    analysis_svc = AnalysisService(store, fake_llm, config, bus)
    context = AppContext(
        config=config,
        store=store,
        analysis_svc=analysis_svc,
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=registry,
    )
    return create_app(context), store, llm_calls


def test_same_event_id_repeated_ten_times_has_one_report_prompt_and_analysis(tmp_path):
    report_app, store, llm_calls = _build_real_report_app(tmp_path)
    transcript_tail = _line({
        "type": "message",
        "role": "user",
        "content": "IDEMPOTENT-TAIL",
        "sessionId": "sess-idempotent",
    })

    with TestClient(report_app) as client:
        responses = [
            client.post("/report", json={
                "student_id": "stu-idempotent",
                "session_id": "sess-idempotent",
                "event": "Stop",
                "event_id": "event-idempotent-1",
                "prompt": "Analyze this once",
                "transcript_tail": transcript_tail,
            })
            for _ in range(10)
        ]

    with store._conn() as conn:
        report_count = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE student_id = ?",
            ("stu-idempotent",),
        ).fetchone()[0]
        prompt_count = conn.execute(
            "SELECT COUNT(*) FROM prompts WHERE student_id = ?",
            ("stu-idempotent",),
        ).fetchone()[0]
        analysis_count = conn.execute(
            "SELECT COUNT(*) FROM analyses WHERE student_id = ?",
            ("stu-idempotent",),
        ).fetchone()[0]

    assert [response.status_code for response in responses] == [202] * 10
    assert report_count == 1
    assert prompt_count == 1
    assert analysis_count == 1
    assert len(llm_calls) == 1
    assert [response.json()["duplicate"] for response in responses] == [
        False, *([True] * 9),
    ]
    assert len({response.json()["report_id"] for response in responses}) == 1


def test_legacy_client_without_event_id_remains_non_idempotent(tmp_path):
    report_app, store, _ = _build_real_report_app(tmp_path)
    payload = {
        "student_id": "stu-legacy-client",
        "session_id": "sess-legacy-client",
        "event": "UserPromptSubmit",
        "prompt": "legacy payload",
        "transcript_tail": "",
    }

    with TestClient(report_app) as client:
        first = client.post("/report", json=payload)
        second = client.post("/report", json=payload)

    assert first.status_code == second.status_code == 202
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is False
    assert first.json()["report_id"] != second.json()["report_id"]
    with store._conn() as conn:
        rows = conn.execute(
            """SELECT event_id FROM reports
               WHERE student_id = ? ORDER BY id""",
            ("stu-legacy-client",),
        ).fetchall()
    assert [row["event_id"] for row in rows] == [None, None]


def test_user_prompt_duplicate_returns_original_prompt_id_without_side_effects(tmp_path):
    report_app, store, _ = _build_real_report_app(tmp_path)

    with TestClient(report_app) as client:
        original = client.post("/report", json={
            "student_id": "stu-prompt-idempotent",
            "session_id": "sess-prompt-original",
            "event": "UserPromptSubmit",
            "event_id": "prompt-event-1",
            "prompt": "ORIGINAL-PROMPT",
        })
        duplicate = client.post("/report", json={
            "student_id": "stu-prompt-idempotent",
            "session_id": "sess-prompt-conflict",
            "event": "UserPromptSubmit",
            "event_id": "prompt-event-1",
            "prompt": "CONFLICTING-PROMPT",
        })

    assert original.status_code == duplicate.status_code == 202
    assert original.json()["duplicate"] is False
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["report_id"] == original.json()["report_id"]
    assert duplicate.json()["prompt_id"] == original.json()["prompt_id"]
    with store._conn() as conn:
        prompts = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM prompts WHERE student_id = ?",
                ("stu-prompt-idempotent",),
            ).fetchall()
        ]
    assert len(prompts) == 1
    assert prompts[0]["report_id"] == original.json()["report_id"]
    assert prompts[0]["session_id"] == "sess-prompt-original"
    assert prompts[0]["content"] == "ORIGINAL-PROMPT"


def test_user_prompt_duplicate_repairs_report_without_prompt_after_restart(tmp_path):
    report_app, store, _ = _build_real_report_app(tmp_path)
    report, duplicate = store.accept_report(
        student_id="stu-prompt-repair",
        session_id="sess-prompt-repair",
        event="UserPromptSubmit",
        event_id="prompt-event-repair",
        prompt="DURABLE-PROMPT",
        transcript_path="",
        msg_count=0,
        tool_calls=0,
        analysis_input=None,
    )
    assert duplicate is False
    with store._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM prompts WHERE student_id = ?",
            ("stu-prompt-repair",),
        ).fetchone()[0] == 0

    with TestClient(report_app) as client:
        repaired = client.post("/report", json={
            "student_id": "stu-prompt-repair",
            "session_id": "sess-prompt-repair",
            "event": "UserPromptSubmit",
            "event_id": "prompt-event-repair",
            "prompt": "conflicting retry payload",
        })

    assert repaired.status_code == 202
    assert repaired.json()["duplicate"] is True
    assert repaired.json()["report_id"] == report["id"]
    with store._conn() as conn:
        prompts = conn.execute(
            "SELECT report_id, content FROM prompts WHERE student_id = ?",
            ("stu-prompt-repair",),
        ).fetchall()
    assert [(row["report_id"], row["content"]) for row in prompts] == [
        (report["id"], "DURABLE-PROMPT"),
    ]


@pytest.mark.parametrize(
    "invalid_event_id",
    ["../escape", "contains space", "x" * 129],
)
def test_report_rejects_invalid_event_id_without_writing(tmp_path, invalid_event_id):
    report_app, store, _ = _build_real_report_app(tmp_path)

    with TestClient(report_app) as client:
        response = client.post("/report", json={
            "student_id": "stu-invalid-event",
            "session_id": "sess-invalid-event",
            "event": "UserPromptSubmit",
            "event_id": invalid_event_id,
            "prompt": "must not persist",
        })

    assert response.status_code == 422
    with store._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM reports WHERE student_id = ?",
            ("stu-invalid-event",),
        ).fetchone()[0] == 0


def test_duplicate_event_id_ignores_conflicting_session_payload(tmp_path):
    report_app, store, _ = _build_real_report_app(tmp_path)
    original_tail = (
        _line({"type": "ai-title", "aiTitle": "Original title"})
        + _line({
            "type": "message",
            "role": "user",
            "content": "ORIGINAL",
            "sessionId": "sess-original",
            "cwd": "/original",
        })
    )
    conflicting_tail = (
        _line({"type": "ai-title", "aiTitle": "Injected title"})
        + _line({
            "type": "message",
            "role": "user",
            "content": "CONFLICT",
            "sessionId": "sess-conflict",
            "cwd": "/conflict",
        })
    )

    with TestClient(report_app) as client:
        original = client.post("/report", json={
            "student_id": "stu-conflict",
            "session_id": "sess-original",
            "event": "Stop",
            "event_id": "same-event",
            "prompt": "original prompt",
            "transcript_tail": original_tail,
            "cwd": "/original",
        })
        duplicate = client.post("/report", json={
            "student_id": "stu-conflict",
            "session_id": "sess-conflict",
            "event": "Stop",
            "event_id": "same-event",
            "prompt": "conflicting prompt",
            "transcript_tail": conflicting_tail,
            "cwd": "/conflict",
        })

    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["report_id"] == original.json()["report_id"]
    with store._conn() as conn:
        sessions = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM sessions WHERE student_id = ? ORDER BY session_id",
                ("stu-conflict",),
            ).fetchall()
        ]
    assert [(row["session_id"], row["work_dir"], row["title"]) for row in sessions] == [
        ("sess-original", "/original", "Original title"),
    ]


def test_duplicate_event_id_rejects_conflicting_event_without_side_effects(tmp_path):
    report_app, store, llm_calls = _build_real_report_app(tmp_path)

    with TestClient(report_app) as client:
        original = client.post("/report", json={
            "student_id": "stu-event-conflict",
            "session_id": "sess-event-conflict",
            "event": "Stop",
            "event_id": "event-kind-conflict",
            "prompt": "original stop",
            "transcript_tail": _line({
                "type": "message",
                "role": "user",
                "content": "ORIGINAL-STOP",
                "sessionId": "sess-event-conflict",
            }),
        })
        conflict = client.post("/report", json={
            "student_id": "stu-event-conflict",
            "session_id": "sess-event-conflict",
            "event": "UserPromptSubmit",
            "event_id": "event-kind-conflict",
            "prompt": "must not create a prompt",
        })

    assert original.status_code == 202
    assert conflict.status_code == 409
    assert len(llm_calls) == 1
    with store._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM reports WHERE student_id = ?",
            ("stu-event-conflict",),
        ).fetchone()[0] == 1
        prompts = conn.execute(
            "SELECT content FROM prompts WHERE student_id = ?",
            ("stu-event-conflict",),
        ).fetchall()
        assert [row["content"] for row in prompts] == ["original stop"]


def test_duplicate_stop_reschedules_persisted_pending_report_without_restart(tmp_path):
    async def scenario():
        report_app, store, llm_calls = _build_real_report_app(tmp_path)
        analysis_svc = report_app.state.context.analysis_svc
        accepted = analysis_svc.accept_report(
            student_id="stu-stop-gap",
            session_id="sess-stop-gap",
            event="Stop",
            event_id="stop-gap-event",
            prompt_text="repair the background gap",
            transcript_content=_line({
                "type": "message",
                "role": "user",
                "content": "PERSISTED-BEFORE-BACKGROUND",
                "sessionId": "sess-stop-gap",
            }),
        )
        assert store.get_report(accepted.report_id)["analysis_status"] == "pending"

        transport = httpx.ASGITransport(app=report_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post("/report", json={
                "student_id": "stu-stop-gap",
                "session_id": "sess-stop-gap",
                "event": "Stop",
                "event_id": "stop-gap-event",
                "prompt": "conflicting retry payload",
                "transcript_tail": "must not replace persisted input",
            })

        assert response.status_code == 202
        assert response.json()["duplicate"] is True
        assert response.json()["report_id"] == accepted.report_id
        assert len(llm_calls) == 1
        assert llm_calls[0]["messages"] == [
            ("user", "PERSISTED-BEFORE-BACKGROUND"),
        ]
        assert store.get_report(accepted.report_id)["analysis_status"] == "done"

    asyncio.run(scenario())


def test_concurrent_duplicate_stop_reschedules_once(tmp_path, monkeypatch):
    async def scenario():
        store = Store(tmp_path / "copilot.db")
        bus = EventBus()
        registry = WSRegistry(send_timeout=0.05)
        provider_started = asyncio.Event()
        provider_release = asyncio.Event()
        first_wrapper_started = asyncio.Event()
        second_wrapper_started = asyncio.Event()
        wrappers_release = asyncio.Event()
        provider_calls = 0
        wrapper_calls = 0

        async def blocking_llm(config, snap, event, latest_prompt):
            nonlocal provider_calls
            provider_calls += 1
            provider_started.set()
            await provider_release.wait()
            return {"topic": "one live repair", "diagnosis": "claimed once"}

        config = {
            "student_id": "server",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": str(tmp_path / "copilot.db")},
            "llm": {},
        }
        analysis_svc = AnalysisService(store, blocking_llm, config, bus)
        context = AppContext(
            config=config,
            store=store,
            analysis_svc=analysis_svc,
            session_svc=SessionQueryService(store, config),
            message_svc=MessageService(store, bus),
            bus=bus,
            ws_registry=registry,
        )
        report_app = create_app(context)
        accepted = analysis_svc.accept_report(
            student_id="stu-concurrent-stop-gap",
            session_id="sess-concurrent-stop-gap",
            event="Stop",
            event_id="concurrent-stop-gap-event",
            prompt_text="authoritative prompt",
            transcript_content=_line({
                "type": "message", "role": "user",
                "content": "AUTHORITATIVE-TAIL",
                "sessionId": "sess-concurrent-stop-gap",
            }),
        )
        original_wrapper = analysis_svc.handle_stop_with_retry

        async def observed_wrapper(*args, **kwargs):
            nonlocal wrapper_calls
            wrapper_calls += 1
            if wrapper_calls == 1:
                first_wrapper_started.set()
            if wrapper_calls == 2:
                second_wrapper_started.set()
            await wrappers_release.wait()
            return await original_wrapper(*args, **kwargs)

        monkeypatch.setattr(
            analysis_svc,
            "handle_stop_with_retry",
            observed_wrapper,
        )
        payload = {
            "student_id": "stu-concurrent-stop-gap",
            "session_id": "sess-concurrent-stop-gap",
            "event": "Stop",
            "event_id": "concurrent-stop-gap-event",
            "prompt": "retry payload",
            "transcript_tail": "retry tail",
        }
        transport = httpx.ASGITransport(app=report_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            first = asyncio.create_task(client.post("/report", json=payload))
            await asyncio.wait_for(first_wrapper_started.wait(), timeout=1)
            second = asyncio.create_task(client.post("/report", json=payload))
            await asyncio.wait_for(second_wrapper_started.wait(), timeout=1)
            wrappers_release.set()
            await asyncio.wait_for(provider_started.wait(), timeout=1)
            provider_release.set()
            responses = await asyncio.gather(first, second)
            done_response = await client.post("/report", json=payload)

        assert [response.status_code for response in responses] == [202, 202]
        assert [response.json()["duplicate"] for response in responses] == [True, True]
        assert done_response.status_code == 202
        assert done_response.json()["duplicate"] is True
        assert wrapper_calls == 2
        assert provider_calls == 1
        with store._conn() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM analyses WHERE report_id = ?",
                (accepted.report_id,),
            ).fetchone()[0] == 1
        assert store.get_report(accepted.report_id)["analysis_status"] == "done"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    (
        "analysis_status",
        "analysis_attempts",
        "analysis_next_retry_at",
        "expected_wrapper_calls",
    ),
    [
        ("failed", 1, 0.0, 1),
        ("running", 1, None, 0),
        ("done", 1, None, 0),
        ("failed", 3, None, 0),
    ],
)
def test_duplicate_stop_schedules_only_recoverable_states(
    tmp_path,
    monkeypatch,
    analysis_status,
    analysis_attempts,
    analysis_next_retry_at,
    expected_wrapper_calls,
):
    async def scenario():
        report_app, store, _ = _build_real_report_app(tmp_path)
        analysis_svc = report_app.state.context.analysis_svc
        accepted = analysis_svc.accept_report(
            student_id="stu-stop-state",
            session_id="sess-stop-state",
            event="Stop",
            event_id="stop-state-event",
            prompt_text="state gate",
            transcript_content="durable state input",
        )
        with store._conn() as conn:
            conn.execute(
                """UPDATE reports
                   SET analysis_status = ?, analysis_attempts = ?,
                       analysis_next_retry_at = ?
                   WHERE id = ?""",
                (
                    analysis_status,
                    analysis_attempts,
                    analysis_next_retry_at,
                    accepted.report_id,
                ),
            )
        wrapper_calls = 0

        async def observed_wrapper(*args, **kwargs):
            nonlocal wrapper_calls
            wrapper_calls += 1
            return None

        monkeypatch.setattr(
            analysis_svc,
            "handle_stop_with_retry",
            observed_wrapper,
        )
        transport = httpx.ASGITransport(app=report_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post("/report", json={
                "student_id": "stu-stop-state",
                "session_id": "sess-stop-state",
                "event": "Stop",
                "event_id": "stop-state-event",
                "prompt": "retry state",
            })

        assert response.status_code == 202
        assert response.json()["duplicate"] is True
        assert wrapper_calls == expected_wrapper_calls

    asyncio.run(scenario())


def test_session_owner_conflict_rolls_back_report_acceptance(tmp_path):
    report_app, store, llm_calls = _build_real_report_app(tmp_path)
    store.upsert_student("student-owner")
    store.upsert_session(
        "sess-owned",
        "student-owner",
        "/owner",
        "Owner session",
    )

    with TestClient(report_app) as client:
        response = client.post("/report", json={
            "student_id": "student-intruder",
            "session_id": "sess-owned",
            "event": "Stop",
            "event_id": "intruding-event",
            "prompt": "must roll back",
            "transcript_tail": _line({
                "type": "message",
                "role": "user",
                "content": "intruding tail",
                "sessionId": "sess-owned",
            }),
        })

    assert response.status_code == 409
    with store._conn() as conn:
        report_count = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE student_id = ?",
            ("student-intruder",),
        ).fetchone()[0]
    assert report_count == 0
    assert llm_calls == []


def test_stop_tail_only_is_analyzed_without_persisting_raw_transcript(tmp_path):
    report_app, store, llm_calls = _build_real_report_app(tmp_path)
    transcript_tail = (
        _line({
            "type": "message",
            "role": "user",
            "content": "TAIL-ONLY user asks about an off-by-one error",
            "sessionId": "sess-tail-only",
        })
        + _line({
            "type": "message",
            "role": "assistant",
            "content": "TAIL-ONLY assistant suggests printing the final index",
        })
    )

    with TestClient(report_app) as client:
        response = client.post("/report", json={
            "student_id": "stu-tail-only",
            "session_id": "sess-tail-only",
            "event": "Stop",
            "prompt": "Check my loop boundary",
            "transcript_tail": transcript_tail,
        })

    assert response.status_code == 202
    assert llm_calls == [{
        "event": "Stop",
        "latest_prompt": "Check my loop boundary",
        "messages": [
            ("user", "TAIL-ONLY user asks about an off-by-one error"),
            ("assistant", "TAIL-ONLY assistant suggests printing the final index"),
        ],
    }]
    analyses = store.recent_analyses(
        "stu-tail-only", limit=10, session_id="sess-tail-only"
    )
    assert [row["topic"] for row in analyses] == ["tail analysis"]
    with store._conn() as conn:
        raw_count = conn.execute(
            """SELECT COUNT(*) FROM raw_transcripts
               WHERE student_id = ? AND session_id = ?""",
            ("stu-tail-only", "sess-tail-only"),
        ).fetchone()[0]
    assert raw_count == 0


def test_tail_only_stop_is_recovered_after_restart(tmp_path):
    db_path = tmp_path / "copilot.db"
    store = Store(db_path)
    bus = EventBus()
    config = {
        "student_id": "server",
        "service": {"host": "127.0.0.1", "port": 8765},
        "store": {"db_path": str(db_path)},
        "llm": {},
    }
    calls = []

    async def fixed_llm(config, snap, event, latest_prompt):
        calls.append([message.text for message in snap.messages])
        return {"topic": "live tail", "diagnosis": "live tail completed"}

    service = AnalysisService(store, fixed_llm, config, bus)
    report_id, _, _ = service.accept_report(
        student_id="student-tail",
        session_id="sess-abandoned-tail",
        event="Stop",
        prompt_text="abandoned",
        transcript_content=_line({
            "type": "message",
            "role": "user",
            "content": "abandoned tail",
            "sessionId": "sess-abandoned-tail",
        }),
    )

    assert [row["id"] for row in store.list_pending_reports()] == [report_id]

    restarted_store = Store(db_path)
    restarted_service = AnalysisService(restarted_store, fixed_llm, config, bus)
    context = AppContext(
        config=config,
        store=restarted_store,
        analysis_svc=restarted_service,
        session_svc=SessionQueryService(restarted_store, config),
        message_svc=MessageService(restarted_store, bus),
        bus=bus,
        ws_registry=WSRegistry(send_timeout=0.05),
    )
    with TestClient(create_app(context)) as client:
        assert client.get("/health").status_code == 200

    assert calls == [["abandoned tail"]]
    assert [row["topic"] for row in restarted_store.recent_analyses(
        "student-tail", limit=10, session_id="sess-abandoned-tail"
    )] == ["live tail"]
    with restarted_store._conn() as conn:
        row = dict(conn.execute(
            "SELECT * FROM reports WHERE id = ?", (report_id,)
        ).fetchone())
    assert row["analysis_pending"] == 0
    assert row["analysis_input"] is None


def test_stop_explicit_full_persists_only_exact_full_transcript(tmp_path):
    report_app, store, llm_calls = _build_real_report_app(tmp_path)
    transcript_tail = _line({
        "type": "message",
        "role": "user",
        "content": "TAIL-SOURCE must be analyzed but never stored as raw",
        "sessionId": "sess-explicit-full",
    })
    transcript_full = (
        "FULL-ONLY-PREFIX\n"
        + _line({
            "type": "message",
            "role": "user",
            "content": "完整全文内容，保留 Unicode 与换行。",
            "sessionId": "sess-explicit-full",
        })
        + "FULL-ONLY-SUFFIX::exact-end"
    )

    with TestClient(report_app) as client:
        response = client.post("/report", json={
            "student_id": "stu-explicit-full",
            "session_id": "sess-explicit-full",
            "event": "Stop",
            "prompt": "Analyze the tail source",
            "transcript_tail": transcript_tail,
            "transcript_full": transcript_full,
        })

    assert response.status_code == 202
    assert llm_calls == [{
        "event": "Stop",
        "latest_prompt": "Analyze the tail source",
        "messages": [
            ("user", "TAIL-SOURCE must be analyzed but never stored as raw"),
        ],
    }]
    with store._conn() as conn:
        raw_rows = [
            dict(row)
            for row in conn.execute(
                """SELECT content FROM raw_transcripts
                   WHERE student_id = ? AND session_id = ?
                   ORDER BY id""",
                ("stu-explicit-full", "sess-explicit-full"),
            ).fetchall()
        ]
    assert raw_rows == [{"content": transcript_full}]


def test_user_prompt_submit_returns_202_and_does_not_run_llm():
    fake = FakeAnalysisService()
    app.dependency_overrides[get_analysis_service] = lambda: fake
    try:
        with TestClient(app) as client:
            resp = client.post("/report", json={
                "student_id": "stu-1",
                "session_id": "sess-1",
                "event": "UserPromptSubmit",
                "prompt": "学员提问全文",
                "transcript_tail": '{"type":"message","role":"user","content":"hi"}\n',
            })
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"
        assert fake.accept_calls[0]["transcript_content"].startswith('{"type"')
        assert fake.prompt_calls == [("stu-1", "sess-1", "学员提问全文")]
        assert fake.stop_calls == []
    finally:
        app.dependency_overrides.clear()


def test_stop_event_returns_202_and_background_runs_handle_stop():
    fake = FakeAnalysisService()
    app.dependency_overrides[get_analysis_service] = lambda: fake
    transcript = '{"type":"message","role":"assistant","content":"done"}\n'
    try:
        with TestClient(app) as client:
            resp = client.post("/report", json={
                "student_id": "stu-1",
                "session_id": "sess-1",
                "event": "Stop",
                "prompt": "",
                "transcript_tail": transcript,
            })
        assert resp.status_code == 202
        assert fake.stop_calls == [("stu-1", "sess-1", "", transcript, 1)]
    finally:
        app.dependency_overrides.clear()


def test_normal_stop_uses_configured_bounded_concurrency(tmp_path):
    async def scenario():
        store = Store(tmp_path / "copilot.db")
        bus = EventBus()
        active_calls = 0
        max_active_calls = 0
        first_call_started = asyncio.Event()
        release_calls = asyncio.Event()
        llm_prompts = []

        async def blocking_llm(config, snap, event, latest_prompt):
            nonlocal active_calls, max_active_calls
            llm_prompts.append(latest_prompt)
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
            first_call_started.set()
            try:
                await release_calls.wait()
            finally:
                active_calls -= 1
            return {"topic": latest_prompt, "diagnosis": "bounded"}

        config = {
            "service": {"analysis_max_concurrency": 1},
            "llm": {},
        }
        analysis_svc = AnalysisService(store, blocking_llm, config, bus)
        work = []
        for index in range(2):
            prompt = f"prompt-{index}"
            transcript = _line({
                "type": "message",
                "role": "user",
                "content": prompt,
                "sessionId": f"session-{index}",
            })
            report_id, session_id, _ = analysis_svc.accept_report(
                student_id="student-a",
                session_id=f"session-{index}",
                event="Stop",
                prompt_text=prompt,
                transcript_content=transcript,
            )
            work.append((session_id, prompt, transcript, report_id))

        tasks = [
            asyncio.create_task(_handle_stop_background(
                analysis_svc,
                "student-a",
                session_id,
                prompt,
                transcript,
                report_id,
            ))
            for session_id, prompt, transcript, report_id in work
        ]
        await asyncio.wait_for(first_call_started.wait(), timeout=1)
        await asyncio.sleep(0.05)
        release_calls.set()
        await asyncio.gather(*tasks)

        assert max_active_calls == 1
        assert sorted(llm_prompts) == ["prompt-0", "prompt-1"]
        for index in range(2):
            assert [row["topic"] for row in store.recent_analyses(
                "student-a", limit=10, session_id=f"session-{index}"
            )] == [f"prompt-{index}"]

    asyncio.run(scenario())


def test_runtime_eventbus_wires_services_and_ws_registry():
    context = app.state.context

    assert context.analysis_svc.bus is context.bus
    assert context.message_svc.bus is context.bus
    assert any(
        inspect.ismethod(sub)
        and sub.__self__ is context.ws_registry
        and sub.__func__ is context.ws_registry.handle_event.__func__
        for sub in context.bus._subscribers
    )


def test_health_still_works():
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "UP"


def test_recent_uses_injected_store():
    fake_store = FakeStore()
    app.dependency_overrides[get_store] = lambda: fake_store
    try:
        with TestClient(app) as client:
            resp = client.get("/recent?student_id=stu-1&limit=5")
        assert resp.status_code == 200
        assert resp.json() == {"items": []}
        assert fake_store.recent_calls == [("stu-1", 5, None)]
    finally:
        app.dependency_overrides.clear()


def test_lifespan_recovers_persisted_tail_without_borrowing_later_full_upload(tmp_path):
    seen_inputs = []

    async def fake_llm(config, snap, event, latest_prompt):
        seen_inputs.append({
            "messages": [message.text for message in snap.messages],
            "latest_prompt": latest_prompt,
        })
        return {
            "topic": "recovered persisted tail",
            "understanding": "unknown",
            "severity": "info",
            "diagnosis": "The persisted Stop tail was used after restart.",
            "ai_reply_summary": "",
        }

    store = Store(tmp_path / "copilot.db")
    bus = EventBus()
    registry = WSRegistry(send_timeout=0.05)
    config = {
        "student_id": "server",
        "service": {"host": "127.0.0.1", "port": 8765},
        "store": {"db_path": str(tmp_path / "copilot.db")},
        "llm": {},
    }
    analysis_svc = AnalysisService(store, fake_llm, config, bus)
    report_id, _, _ = analysis_svc.accept_report(
        student_id="stu-pending-tail",
        session_id="sess-pending-tail",
        event="Stop",
        prompt_text="recover this prompt without unrelated transcript content",
        transcript_content=_line({
            "type": "message",
            "role": "user",
            "content": "transient Stop tail",
            "sessionId": "sess-pending-tail",
        }),
    )
    store.add_raw_transcript(
        "sess-pending-tail",
        "stu-pending-tail",
        _line({
            "type": "message",
            "role": "user",
            "content": "later unrelated full upload",
            "sessionId": "sess-pending-tail",
        }),
    )

    context = AppContext(
        config=config,
        store=store,
        analysis_svc=analysis_svc,
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=registry,
    )

    with TestClient(create_app(context)):
        pass

    assert seen_inputs == [{
        "messages": ["transient Stop tail"],
        "latest_prompt": "recover this prompt without unrelated transcript content",
    }]
    assert store.list_pending_reports() == []
    assert [row["topic"] for row in store.recent_analyses(
        "stu-pending-tail", limit=10, session_id="sess-pending-tail"
    )] == ["recovered persisted tail"]
    with store._conn() as conn:
        row = dict(conn.execute(
            "SELECT * FROM reports WHERE id = ?", (report_id,)
        ).fetchone())
    assert row["analysis_pending"] == 0
    assert row["analysis_status"] == "done"
    assert row["analysis_input"] is None


def test_report_requires_token_when_configured(monkeypatch):
    fake = FakeAnalysisService()
    monkeypatch.setenv("COPILOT_TOKEN", "secret")
    app.dependency_overrides[get_analysis_service] = lambda: fake
    try:
        with TestClient(app) as client:
            denied = client.post("/report", json={
                "student_id": "stu-1",
                "session_id": "sess-1",
                "event": "UserPromptSubmit",
                "prompt": "hi",
                "transcript_tail": "",
            })
            allowed = client.post(
                "/report",
                headers={"Authorization": "Bearer secret"},
                json={
                    "student_id": "stu-1",
                    "session_id": "sess-1",
                    "event": "UserPromptSubmit",
                    "prompt": "hi",
                    "transcript_tail": "",
                },
            )
        assert denied.status_code == 401
        assert allowed.status_code == 202
    finally:
        app.dependency_overrides.clear()


def test_lifespan_recovers_pending_stop_reports_before_serving(tmp_path):
    submitted_prompt = "Why does my loop skip the last item?"
    transcript = (
        _line({"type": "ai-title", "aiTitle": "Recovered Session"})
        + _line({
            "type": "message",
            "role": "user",
            "content": "Why does my loop skip the last item?",
            "sessionId": "sess-pending",
            "cwd": "/work/recover",
        })
        + _line({
            "type": "message",
            "role": "assistant",
            "content": "Check the upper bound and print the final index.",
        })
    )

    async def fake_llm(config, snap, event, latest_prompt):
        assert event == "Stop"
        assert latest_prompt == submitted_prompt
        assert snap.ai_title == "Recovered Session"
        assert [m.text for m in snap.messages] == [
            "Why does my loop skip the last item?",
            "Check the upper bound and print the final index.",
        ]
        return {
            "topic": "loop bounds",
            "understanding": "low",
            "off_topic": False,
            "stuck_at": "range end",
            "is_technical": True,
            "severity": "warn",
            "diagnosis": "The student is debugging an exclusive upper bound.",
            "suggestion": "Print the final index and expected length.",
            "progress": "debugging",
            "guidance": "Use a tiny list to confirm the boundary.",
            "alert": "needs mentor follow-up",
            "ai_reply_summary": "Assistant suggested checking the upper bound.",
        }

    store = Store(tmp_path / "copilot.db")
    bus = EventBus()
    registry = WSRegistry(send_timeout=0.05)
    bus.subscribe(registry.handle_event)
    config = {
        "student_id": "server",
        "service": {"host": "127.0.0.1", "port": 8765},
        "store": {"db_path": str(tmp_path / "copilot.db")},
        "llm": {},
    }
    accepting_service = AnalysisService(store, fake_llm, config, bus)
    report_id, _, _ = accepting_service.accept_report(
        student_id="stu-pending",
        session_id="sess-pending",
        event="Stop",
        prompt_text=submitted_prompt,
        transcript_content=transcript,
        raw_transcript_content=transcript,
        cwd="/work/recover",
    )
    assert [row["id"] for row in store.list_pending_reports()] == [report_id]

    # Simulate a new process: recovery must rely only on the persisted database.
    store = Store(tmp_path / "copilot.db")
    analysis_svc = AnalysisService(store, fake_llm, config, bus)

    context = AppContext(
        config=config,
        store=store,
        analysis_svc=analysis_svc,
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=registry,
    )

    with TestClient(create_app(context)) as client:
        assert client.get("/health").json()["status"] == "UP"

    assert store.list_pending_reports() == []
    rows = store.recent_analyses("stu-pending", limit=10, session_id="sess-pending")
    assert len(rows) == 1
    assert rows[0]["report_id"] == report_id
    assert rows[0]["topic"] == "loop bounds"
    assert rows[0]["diagnosis"] == "The student is debugging an exclusive upper bound."
    with store._conn() as conn:
        prompts = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM prompts WHERE report_id = ?",
                (report_id,),
            ).fetchall()
        ]
        summaries = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM ai_summaries WHERE session_id = ?",
                ("sess-pending",),
            ).fetchall()
        ]
    assert len(prompts) == 1
    assert prompts[0]["content"] == submitted_prompt
    assert prompts[0]["seq_in_session"] == 0
    assert len(summaries) == 1
    assert summaries[0]["prompt_id"] == prompts[0]["id"]


def test_lifespan_serves_health_while_slow_report_recovery_runs(tmp_path):
    async def scenario():
        store = Store(tmp_path / "copilot.db")
        provider_started = asyncio.Event()
        provider_release = asyncio.Event()

        async def slow_llm(config, snap, event, latest_prompt):
            provider_started.set()
            await provider_release.wait()
            return {"topic": "slow recovery", "diagnosis": "completed once"}

        bus = EventBus()
        config = {
            "student_id": "server",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": str(tmp_path / "copilot.db")},
            "llm": {},
        }
        analysis_svc = AnalysisService(store, slow_llm, config, bus)
        report_id, _, _ = analysis_svc.accept_report(
            student_id="stu-slow-recovery",
            session_id="sess-slow-recovery",
            event="Stop",
            prompt_text="recover without blocking health",
            transcript_content=_line({
                "type": "message",
                "role": "user",
                "content": "SLOW-RECOVERY-TAIL",
                "sessionId": "sess-slow-recovery",
            }),
        )
        context = AppContext(
            config=config,
            store=store,
            analysis_svc=analysis_svc,
            session_svc=SessionQueryService(store, config),
            message_svc=MessageService(store, bus),
            bus=bus,
            ws_registry=WSRegistry(send_timeout=0.05),
        )
        application = create_app(context)
        lifespan = application.router.lifespan_context(application)
        startup = asyncio.create_task(lifespan.__aenter__())
        entered_before_release = False
        try:
            await asyncio.wait_for(provider_started.wait(), timeout=1)
            await asyncio.sleep(0)
            entered_before_release = startup.done() and startup.exception() is None
            if entered_before_release:
                await startup
                transport = httpx.ASGITransport(app=application)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                ) as client:
                    response = await client.get("/health")
                assert response.status_code == 200
                assert response.json()["status"] == "UP"
        finally:
            provider_release.set()
            await startup

            async def report_is_done():
                while store.get_report(report_id)["analysis_status"] != "done":
                    await asyncio.sleep(0)

            await asyncio.wait_for(report_is_done(), timeout=1)
            await lifespan.__aexit__(None, None, None)

        assert entered_before_release is True
        with store._conn() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM analyses WHERE report_id = ?",
                (report_id,),
            ).fetchone()[0] == 1

    asyncio.run(scenario())


def test_lifespan_shutdown_cancels_recovery_then_restart_analyzes_once(tmp_path):
    async def scenario():
        store = Store(tmp_path / "copilot.db")
        provider_started = asyncio.Event()
        provider_cancelled = asyncio.Event()
        first_calls = 0

        async def blocked_llm(config, snap, event, latest_prompt):
            nonlocal first_calls
            first_calls += 1
            provider_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                provider_cancelled.set()

        bus = EventBus()
        config = {
            "student_id": "server",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": str(tmp_path / "copilot.db")},
            "llm": {},
        }
        analysis_svc = AnalysisService(store, blocked_llm, config, bus)
        report_id, _, _ = analysis_svc.accept_report(
            student_id="stu-cancel-recovery",
            session_id="sess-cancel-recovery",
            event="Stop",
            prompt_text="cancel safely",
            transcript_content=_line({
                "type": "message",
                "role": "user",
                "content": "CANCELLED-RECOVERY-TAIL",
                "sessionId": "sess-cancel-recovery",
            }),
        )
        context = AppContext(
            config=config,
            store=store,
            analysis_svc=analysis_svc,
            session_svc=SessionQueryService(store, config),
            message_svc=MessageService(store, bus),
            bus=bus,
            ws_registry=WSRegistry(send_timeout=0.05),
        )
        application = create_app(context)
        lifespan = application.router.lifespan_context(application)
        startup = asyncio.create_task(lifespan.__aenter__())
        await asyncio.wait_for(provider_started.wait(), timeout=1)
        await asyncio.sleep(0)
        entered = startup.done() and startup.exception() is None
        if not entered:
            startup.cancel()
            with pytest.raises(asyncio.CancelledError):
                await startup
        assert entered is True
        await startup

        await asyncio.wait_for(
            lifespan.__aexit__(None, None, None),
            timeout=1,
        )
        assert provider_cancelled.is_set()
        assert context.report_recovery_task is None
        assert context.worker_lock_file is None
        assert first_calls == 1
        cancelled_report = store.get_report(report_id)
        assert cancelled_report["analysis_status"] == "failed"
        assert cancelled_report["analysis_attempts"] == 1
        assert cancelled_report["analysis_error"] == "analysis_cancelled"
        assert cancelled_report["analysis_input"] is not None

        restarted_calls = 0

        async def successful_llm(config, snap, event, latest_prompt):
            nonlocal restarted_calls
            restarted_calls += 1
            return {"topic": "restarted", "diagnosis": "one durable result"}

        restarted_store = Store(tmp_path / "copilot.db")
        restarted = AppContext(
            config=config,
            store=restarted_store,
            analysis_svc=AnalysisService(
                restarted_store, successful_llm, config, bus,
            ),
            session_svc=SessionQueryService(restarted_store, config),
            message_svc=MessageService(restarted_store, bus),
            bus=bus,
            ws_registry=WSRegistry(send_timeout=0.05),
        )

        async def no_sleep(delay: float) -> None:
            return None

        await _recover_pending_reports(restarted, sleeper=no_sleep)
        assert restarted_calls == 1
        with store._conn() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM analyses WHERE report_id = ?",
                (report_id,),
            ).fetchone()[0] == 1

    asyncio.run(scenario())


def test_lifespan_recovery_failure_propagates_on_shutdown_and_releases_lock(
    tmp_path,
    monkeypatch,
):
    async def scenario():
        store = Store(tmp_path / "copilot.db")
        bus = EventBus()
        config = {
            "student_id": "server",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": str(tmp_path / "copilot.db")},
            "llm": {},
        }
        service = AnalysisService(store, lambda *args: None, config, bus)
        service.accept_report(
            student_id="stu-recovery-error",
            session_id="sess-recovery-error",
            event="Stop",
            prompt_text="surface infrastructure failure",
            transcript_content="durable input",
        )
        failure_raised = asyncio.Event()

        async def fail_recovery(**kwargs):
            failure_raised.set()
            raise ValueError("unexpected recovery infrastructure failure")

        monkeypatch.setattr(service, "handle_stop_with_retry", fail_recovery)
        context = AppContext(
            config=config,
            store=store,
            analysis_svc=service,
            session_svc=SessionQueryService(store, config),
            message_svc=MessageService(store, bus),
            bus=bus,
            ws_registry=WSRegistry(send_timeout=0.05),
        )
        application = create_app(context)
        lifespan = application.router.lifespan_context(application)
        await lifespan.__aenter__()
        await asyncio.wait_for(failure_raised.wait(), timeout=1)
        await asyncio.sleep(0)

        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            assert (await client.get("/health")).status_code == 200

        with pytest.raises(
            ValueError,
            match="unexpected recovery infrastructure failure",
        ):
            await lifespan.__aexit__(None, None, None)
        assert context.report_recovery_task is None
        assert context.worker_lock_file is None

    asyncio.run(scenario())


def test_recovery_prepare_failure_can_retry_same_context(tmp_path, monkeypatch):
    async def scenario():
        store = Store(tmp_path / "copilot.db")
        calls = 0

        async def fixed_llm(config, snap, event, latest_prompt):
            nonlocal calls
            calls += 1
            return {"topic": "recovered", "diagnosis": "prepare retried"}

        bus = EventBus()
        config = {
            "student_id": "server",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": str(tmp_path / "copilot.db")},
            "llm": {},
        }
        service = AnalysisService(store, fixed_llm, config, bus)
        report_id, _, _ = service.accept_report(
            student_id="stu-prepare-retry",
            session_id="sess-prepare-retry",
            event="Stop",
            prompt_text="retry prepare",
            transcript_content=_line({
                "type": "message", "role": "user",
                "content": "PREPARE-RETRY-TAIL",
                "sessionId": "sess-prepare-retry",
            }),
        )
        store.claim_report_analysis(report_id, max_attempts=3)
        context = AppContext(
            config=config,
            store=store,
            analysis_svc=service,
            session_svc=SessionQueryService(store, config),
            message_svc=MessageService(store, bus),
            bus=bus,
            ws_registry=WSRegistry(send_timeout=0.05),
        )
        original = store.recover_interrupted_report_analyses
        prepare_calls = 0

        def flaky_prepare(*, max_attempts=3):
            nonlocal prepare_calls
            prepare_calls += 1
            if prepare_calls == 1:
                raise RuntimeError("temporary sqlite prepare failure")
            return original(max_attempts=max_attempts)

        monkeypatch.setattr(
            store,
            "recover_interrupted_report_analyses",
            flaky_prepare,
        )
        with pytest.raises(RuntimeError, match="temporary sqlite prepare failure"):
            await _recover_pending_reports(context)
        assert context.report_recovery_prepared is False

        async def no_sleep(delay: float) -> None:
            return None

        await _recover_pending_reports(context, sleeper=no_sleep)
        assert prepare_calls == 2
        assert calls == 1
        assert store.get_report(report_id)["analysis_status"] == "done"

    asyncio.run(scenario())


def test_recovery_persists_legacy_raw_input_before_claim(tmp_path):
    async def scenario():
        store = Store(tmp_path / "copilot.db")
        seen_messages = []

        async def fixed_llm(config, snap, event, latest_prompt):
            seen_messages.extend(message.text for message in snap.messages)
            assert store.get_report(report_id)["analysis_input"] is not None
            return {"topic": "legacy", "diagnosis": "durable fallback"}

        bus = EventBus()
        config = {
            "student_id": "server",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": str(tmp_path / "copilot.db")},
            "llm": {},
        }
        report_id = store.add_report(
            "stu-legacy-recovery",
            "sess-legacy-recovery",
            "Stop",
            "legacy prompt",
            "copilot:explicit-raw-transcript",
            1,
            0,
        )
        with store._conn() as conn:
            conn.execute(
                """UPDATE reports
                   SET analysis_pending = 1, analysis_status = 'pending'
                   WHERE id = ?""",
                (report_id,),
            )
        store.add_raw_transcript(
            "sess-legacy-recovery",
            "stu-legacy-recovery",
            _line({
                "type": "message", "role": "user",
                "content": "LEGACY-RAW-INPUT",
                "sessionId": "sess-legacy-recovery",
            }),
        )
        service = AnalysisService(store, fixed_llm, config, bus)
        context = AppContext(
            config=config,
            store=store,
            analysis_svc=service,
            session_svc=SessionQueryService(store, config),
            message_svc=MessageService(store, bus),
            bus=bus,
            ws_registry=WSRegistry(send_timeout=0.05),
        )

        async def no_sleep(delay: float) -> None:
            return None

        await _recover_pending_reports(context, sleeper=no_sleep)
        assert seen_messages == ["LEGACY-RAW-INPUT"]
        assert store.get_report(report_id)["analysis_status"] == "done"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "legacy_case",
    [
        "far_future_raw",
        "two_raws_in_window",
        "two_reports_one_raw",
        "cross_student_raw",
        "bulk_sha_raw",
        "no_raw",
    ],
)
def test_recovery_fails_closed_without_unique_immediate_legacy_raw(
    tmp_path,
    legacy_case,
):
    async def scenario():
        store = Store(tmp_path / "copilot.db")
        llm_calls = 0

        async def must_not_run(config, snap, event, latest_prompt):
            nonlocal llm_calls
            llm_calls += 1
            return {"topic": "wrong input", "diagnosis": "must fail closed"}

        bus = EventBus()
        config = {
            "student_id": "server",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": str(tmp_path / "copilot.db")},
            "llm": {},
        }

        def add_legacy_report(created_at: float) -> int:
            report_id = store.add_report(
                "stu-legacy-ambiguous",
                "sess-legacy-ambiguous",
                "Stop",
                "legacy prompt",
                "copilot:explicit-raw-transcript",
                1,
                0,
            )
            with store._conn() as conn:
                conn.execute(
                    """UPDATE reports
                       SET analysis_pending = 1,
                           analysis_status = 'pending',
                           created_at = ?
                       WHERE id = ?""",
                    (created_at, report_id),
                )
            return report_id

        def add_raw(
            student_id: str,
            created_at: float,
            content: str,
            content_sha256: str | None = None,
        ) -> int:
            raw_id = store.add_raw_transcript(
                "sess-legacy-ambiguous",
                student_id,
                content,
                content_sha256,
            )
            with store._conn() as conn:
                conn.execute(
                    "UPDATE raw_transcripts SET created_at = ? WHERE id = ?",
                    (created_at, raw_id),
                )
            return raw_id

        report_ids = [add_legacy_report(10.0)]
        if legacy_case == "far_future_raw":
            add_raw("stu-legacy-ambiguous", 100.0, "UNRELATED-FUTURE-RAW")
        elif legacy_case == "two_raws_in_window":
            add_raw("stu-legacy-ambiguous", 11.0, "AMBIGUOUS-RAW-ONE")
            add_raw("stu-legacy-ambiguous", 12.0, "AMBIGUOUS-RAW-TWO")
        elif legacy_case == "two_reports_one_raw":
            report_ids.append(add_legacy_report(9.0))
            add_raw("stu-legacy-ambiguous", 12.0, "CONTESTED-RAW")
        elif legacy_case == "cross_student_raw":
            add_raw("different-student", 11.0, "CROSS-STUDENT-RAW")
        elif legacy_case == "bulk_sha_raw":
            add_raw(
                "stu-legacy-ambiguous",
                11.0,
                "BULK-UPLOAD-RAW",
                "bulk-content-sha",
            )

        service = AnalysisService(store, must_not_run, config, bus)
        context = AppContext(
            config=config,
            store=store,
            analysis_svc=service,
            session_svc=SessionQueryService(store, config),
            message_svc=MessageService(store, bus),
            bus=bus,
            ws_registry=WSRegistry(send_timeout=0.05),
        )

        async def no_sleep(delay: float) -> None:
            return None

        await _recover_pending_reports(context, sleeper=no_sleep)

        assert llm_calls == 0
        for report_id in report_ids:
            report = store.get_report(report_id)
            assert report["analysis_status"] == "failed"
            assert report["analysis_attempts"] == 3
            assert report["analysis_error"] == "analysis_input_unavailable"
            assert report["analysis_input"] is None

    asyncio.run(scenario())


def test_reopen_does_not_bind_ancient_prompt_to_modern_stop_report(tmp_path):
    async def scenario():
        db_path = tmp_path / "copilot.db"
        store = Store(db_path)
        store.upsert_session(
            "sess-modern-prompt",
            "stu-modern-prompt",
            "",
            "",
        )
        ancient_prompt_id = store.add_prompt(
            "sess-modern-prompt",
            0,
            "stu-modern-prompt",
            "继续",
        )
        with store._conn() as conn:
            conn.execute(
                "UPDATE prompts SET created_at = 1.0 WHERE id = ?",
                (ancient_prompt_id,),
            )

        accepted, duplicate = store.accept_report(
            student_id="stu-modern-prompt",
            session_id="sess-modern-prompt",
            event="Stop",
            event_id="modern-stop-event",
            prompt="继续",
            transcript_path="copilot:explicit-raw-transcript",
            msg_count=1,
            tool_calls=0,
            analysis_input=_line({
                "type": "message",
                "role": "user",
                "content": "modern durable input",
                "sessionId": "sess-modern-prompt",
            }),
        )
        report_id = int(accepted["id"])
        assert duplicate is False
        with store._conn() as conn:
            conn.execute(
                "UPDATE reports SET created_at = 100000.0 WHERE id = ?",
                (report_id,),
            )

        # Repeated initialization must not let the legacy migration claim a
        # modern event-addressed report for an unrelated ancient prompt.
        Store(db_path)
        store = Store(db_path)
        assert store.get_prompt(ancient_prompt_id)["report_id"] is None

        async def fixed_llm(config, snap, event, latest_prompt):
            return {"topic": "modern", "diagnosis": "uses its own prompt"}

        bus = EventBus()
        config = {
            "student_id": "server",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": str(db_path)},
            "llm": {},
        }
        service = AnalysisService(store, fixed_llm, config, bus)
        context = AppContext(
            config=config,
            store=store,
            analysis_svc=service,
            session_svc=SessionQueryService(store, config),
            message_svc=MessageService(store, bus),
            bus=bus,
            ws_registry=WSRegistry(send_timeout=0.05),
        )

        async def no_sleep(delay: float) -> None:
            return None

        await _recover_pending_reports(context, sleeper=no_sleep)

        assert store.get_prompt(ancient_prompt_id)["report_id"] is None
        report_prompt = store.get_prompt_for_report(report_id)
        assert report_prompt is not None
        assert report_prompt["id"] != ancient_prompt_id
        assert report_prompt["content"] == "继续"
        assert store.get_report(report_id)["analysis_status"] == "done"

    asyncio.run(scenario())


def test_legacy_recovery_cannot_borrow_modern_stop_raw(tmp_path):
    async def scenario():
        store = Store(tmp_path / "copilot.db")
        legacy_report_id = store.add_report(
            "stu-shared-raw",
            "sess-shared-raw",
            "Stop",
            "legacy",
            "copilot:explicit-raw-transcript",
            1,
            0,
        )
        with store._conn() as conn:
            conn.execute(
                """UPDATE reports
                   SET analysis_pending = 1,
                       analysis_status = 'pending',
                       created_at = 10.0
                   WHERE id = ?""",
                (legacy_report_id,),
            )

        modern, duplicate = store.accept_report(
            student_id="stu-shared-raw",
            session_id="sess-shared-raw",
            event="Stop",
            event_id="modern-raw-owner",
            prompt="modern",
            transcript_path="copilot:explicit-raw-transcript",
            msg_count=1,
            tool_calls=0,
            analysis_input=_line({
                "type": "message",
                "role": "user",
                "content": "MODERN-DURABLE-INPUT",
                "sessionId": "sess-shared-raw",
            }),
            raw_transcript_content=_line({
                "type": "message",
                "role": "user",
                "content": "MODERN-RAW",
                "sessionId": "sess-shared-raw",
            }),
        )
        modern_report_id = int(modern["id"])
        assert duplicate is False
        with store._conn() as conn:
            conn.execute(
                "UPDATE reports SET created_at = 11.0 WHERE id = ?",
                (modern_report_id,),
            )
            modern_raw_id = conn.execute(
                """SELECT id FROM raw_transcripts
                   WHERE student_id = 'stu-shared-raw'
                     AND session_id = 'sess-shared-raw'""",
            ).fetchone()["id"]
            conn.execute(
                "UPDATE raw_transcripts SET created_at = 12.0 WHERE id = ?",
                (modern_raw_id,),
            )

        calls = []

        async def fixed_llm(config, snap, event, latest_prompt):
            calls.append([message.text for message in snap.messages])
            return {"topic": "modern", "diagnosis": "uses durable input"}

        bus = EventBus()
        config = {
            "student_id": "server",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": str(tmp_path / "copilot.db")},
            "llm": {},
        }
        service = AnalysisService(store, fixed_llm, config, bus)
        context = AppContext(
            config=config,
            store=store,
            analysis_svc=service,
            session_svc=SessionQueryService(store, config),
            message_svc=MessageService(store, bus),
            bus=bus,
            ws_registry=WSRegistry(send_timeout=0.05),
        )

        async def no_sleep(delay: float) -> None:
            return None

        await _recover_pending_reports(context, sleeper=no_sleep)

        legacy = store.get_report(legacy_report_id)
        modern = store.get_report(modern_report_id)
        assert legacy["analysis_status"] == "failed"
        assert legacy["analysis_error"] == "analysis_input_unavailable"
        assert legacy["analysis_input"] is None
        assert modern["analysis_status"] == "done"
        assert calls == [["MODERN-DURABLE-INPUT"]]

    asyncio.run(scenario())


def test_legacy_empty_stop_prompt_does_not_claim_unrelated_user_prompt(tmp_path):
    async def scenario():
        db_path = tmp_path / "copilot.db"
        store = Store(db_path)
        report_id = store.add_report(
            "stu-empty-stop",
            "sess-empty-stop",
            "Stop",
            "",
            "copilot:explicit-raw-transcript",
            1,
            0,
        )
        unrelated_prompt_id = store.add_prompt(
            "sess-empty-stop",
            0,
            "stu-empty-stop",
            "UNRELATED-USER-PROMPT",
        )
        with store._conn() as conn:
            conn.execute(
                """UPDATE reports
                   SET analysis_pending = 1,
                       analysis_status = 'pending',
                       analysis_input = ?,
                       created_at = 10.0
                   WHERE id = ?""",
                (
                    _line({
                        "type": "message",
                        "role": "user",
                        "content": "EMPTY-STOP-DURABLE-INPUT",
                        "sessionId": "sess-empty-stop",
                    }),
                    report_id,
                ),
            )
            conn.execute(
                "UPDATE prompts SET created_at = 11.0 WHERE id = ?",
                (unrelated_prompt_id,),
            )

        Store(db_path)
        store = Store(db_path)
        latest_prompts = []

        async def fixed_llm(config, snap, event, latest_prompt):
            latest_prompts.append(latest_prompt)
            return {
                "topic": "empty stop",
                "diagnosis": "keeps prompt unbound",
                "ai_reply_summary": "summary without prompt ownership",
            }

        bus = EventBus()
        config = {
            "student_id": "server",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": str(db_path)},
            "llm": {},
        }
        service = AnalysisService(store, fixed_llm, config, bus)
        context = AppContext(
            config=config,
            store=store,
            analysis_svc=service,
            session_svc=SessionQueryService(store, config),
            message_svc=MessageService(store, bus),
            bus=bus,
            ws_registry=WSRegistry(send_timeout=0.05),
        )

        async def no_sleep(delay: float) -> None:
            return None

        await _recover_pending_reports(context, sleeper=no_sleep)

        assert latest_prompts == [""]
        assert store.get_prompt_for_report(report_id) is None
        assert store.get_prompt(unrelated_prompt_id)["report_id"] is None
        with store._conn() as conn:
            summary = conn.execute(
                "SELECT prompt_id FROM ai_summaries WHERE session_id = ?",
                ("sess-empty-stop",),
            ).fetchone()
        assert summary is not None
        assert summary["prompt_id"] is None

    asyncio.run(scenario())


def test_recovery_requeues_interrupted_running_report(tmp_path):
    async def scenario():
        store = Store(tmp_path / "copilot.db")
        calls: list[list[str]] = []

        async def fixed_llm(config, snap, event, latest_prompt):
            calls.append([message.text for message in snap.messages])
            return {"topic": "recovered running", "diagnosis": "resumed safely"}

        bus = EventBus()
        config = {
            "student_id": "server",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": str(tmp_path / "copilot.db")},
            "llm": {},
        }
        analysis_svc = AnalysisService(store, fixed_llm, config, bus)
        report_id, _, _ = analysis_svc.accept_report(
            student_id="stu-running",
            session_id="sess-running",
            event="Stop",
            prompt_text="resume",
            transcript_content=_line({
                "type": "message",
                "role": "user",
                "content": "persisted running tail",
                "sessionId": "sess-running",
            }),
        )
        claimed = store.claim_report_analysis(report_id, max_attempts=3)
        assert claimed["analysis_status"] == "running"
        assert claimed["analysis_attempts"] == 1

        restarted_store = Store(tmp_path / "copilot.db")
        restarted_service = AnalysisService(
            restarted_store, fixed_llm, config, bus,
        )
        context = AppContext(
            config=config,
            store=restarted_store,
            analysis_svc=restarted_service,
            session_svc=SessionQueryService(restarted_store, config),
            message_svc=MessageService(restarted_store, bus),
            bus=bus,
            ws_registry=WSRegistry(send_timeout=0.05),
        )

        async def no_sleep(delay: float) -> None:
            return None

        await _recover_pending_reports(context, sleeper=no_sleep)

        assert calls == [["persisted running tail"]]
        row = restarted_store.get_report(report_id)
        assert row["analysis_status"] == "done"
        assert row["analysis_attempts"] == 2
        assert row["analysis_input"] is None

    asyncio.run(scenario())


def test_recovery_replays_failed_under_limit_but_skips_exhausted(tmp_path):
    async def scenario():
        store = Store(tmp_path / "copilot.db")
        calls: list[str] = []
        sleeps: list[float] = []

        async def fixed_llm(config, snap, event, latest_prompt):
            calls.extend(message.text for message in snap.messages)
            return {"topic": "recovered failed", "diagnosis": "under limit"}

        bus = EventBus()
        config = {
            "student_id": "server",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": str(tmp_path / "copilot.db")},
            "llm": {},
        }
        analysis_svc = AnalysisService(store, fixed_llm, config, bus)
        retryable, _, _ = analysis_svc.accept_report(
            student_id="stu-recovery-state",
            session_id="sess-retryable",
            event="Stop",
            prompt_text="retryable",
            transcript_content=_line({
                "type": "message", "role": "user",
                "content": "RETRYABLE-TAIL", "sessionId": "sess-retryable",
            }),
        )
        exhausted, _, _ = analysis_svc.accept_report(
            student_id="stu-recovery-state",
            session_id="sess-exhausted",
            event="Stop",
            prompt_text="exhausted",
            transcript_content=_line({
                "type": "message", "role": "user",
                "content": "EXHAUSTED-TAIL", "sessionId": "sess-exhausted",
            }),
        )
        with store._conn() as conn:
            conn.execute(
                """UPDATE reports SET analysis_status = 'failed',
                   analysis_attempts = 1, analysis_error = 'llm_provider_timeout_error',
                   analysis_next_retry_at = 123 WHERE id = ?""",
                (retryable,),
            )
            conn.execute(
                """UPDATE reports SET analysis_status = 'failed',
                   analysis_attempts = 3, analysis_error = 'llm_provider_timeout_error',
                   analysis_next_retry_at = NULL WHERE id = ?""",
                (exhausted,),
            )

        context = AppContext(
            config=config,
            store=store,
            analysis_svc=analysis_svc,
            session_svc=SessionQueryService(store, config),
            message_svc=MessageService(store, bus),
            bus=bus,
            ws_registry=WSRegistry(send_timeout=0.05),
        )

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        await _recover_pending_reports(context, sleeper=fake_sleep)

        assert calls == ["RETRYABLE-TAIL"]
        assert sleeps == [1]
        assert store.get_report(retryable)["analysis_status"] == "done"
        exhausted_row = store.get_report(exhausted)
        assert exhausted_row["analysis_status"] == "failed"
        assert exhausted_row["analysis_attempts"] == 3
        assert exhausted_row["analysis_input"] is not None

    asyncio.run(scenario())


def test_concurrent_recovery_drains_one_report_once(tmp_path):
    async def scenario():
        store = Store(tmp_path / "copilot.db")
        provider_started = asyncio.Event()
        provider_release = asyncio.Event()
        llm_calls = 0

        async def blocking_llm(config, snap, event, latest_prompt):
            nonlocal llm_calls
            llm_calls += 1
            provider_started.set()
            await provider_release.wait()
            return {"topic": "one recovery", "diagnosis": "claimed once"}

        bus = EventBus()
        config = {
            "student_id": "server",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": str(tmp_path / "copilot.db")},
            "llm": {},
        }
        analysis_svc = AnalysisService(store, blocking_llm, config, bus)
        report_id, _, _ = analysis_svc.accept_report(
            student_id="stu-recovery-race",
            session_id="sess-recovery-race",
            event="Stop",
            prompt_text="recover once",
            transcript_content=_line({
                "type": "message", "role": "user",
                "content": "RECOVERY-RACE-TAIL",
                "sessionId": "sess-recovery-race",
            }),
        )
        context = AppContext(
            config=config,
            store=store,
            analysis_svc=analysis_svc,
            session_svc=SessionQueryService(store, config),
            message_svc=MessageService(store, bus),
            bus=bus,
            ws_registry=WSRegistry(send_timeout=0.05),
        )

        async def no_sleep(delay: float) -> None:
            return None

        first = asyncio.create_task(
            _recover_pending_reports(context, sleeper=no_sleep)
        )
        await provider_started.wait()
        second = asyncio.create_task(
            _recover_pending_reports(context, sleeper=no_sleep)
        )
        await asyncio.sleep(0)
        provider_release.set()
        await asyncio.gather(first, second)

        with store._conn() as conn:
            analysis_count = conn.execute(
                "SELECT COUNT(*) FROM analyses WHERE report_id = ?",
                (report_id,),
            ).fetchone()[0]
        assert llm_calls == 1
        assert analysis_count == 1
        assert store.get_report(report_id)["analysis_attempts"] == 1

    asyncio.run(scenario())


def test_lifespan_clears_pending_without_duplicate_when_analysis_already_exists(tmp_path):
    async def fail_if_called(config, snap, event, latest_prompt):
        raise AssertionError("completed pending report should not run LLM again")

    store = Store(tmp_path / "copilot.db")
    bus = EventBus()
    registry = WSRegistry(send_timeout=0.05)
    config = {
        "student_id": "server",
        "service": {"host": "127.0.0.1", "port": 8765},
        "store": {"db_path": str(tmp_path / "copilot.db")},
        "llm": {},
    }
    analysis_svc = AnalysisService(store, fail_if_called, config, bus)
    report_id, _, _ = analysis_svc.accept_report(
        student_id="stu-done",
        session_id="sess-done",
        event="Stop",
        prompt_text="",
        transcript_content=_line({
            "type": "message",
            "role": "user",
            "content": "already analyzed",
            "sessionId": "sess-done",
        }),
        raw_transcript_content="already analyzed",
        cwd="/work/done",
    )
    store.add_analysis(report_id, "stu-done", {
        "topic": "existing analysis",
        "understanding": "high",
        "severity": "info",
        "diagnosis": "This report already has an analysis row.",
    }, session_id="sess-done")
    assert [row["id"] for row in store.list_pending_reports()] == [report_id]

    context = AppContext(
        config=config,
        store=store,
        analysis_svc=analysis_svc,
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=registry,
    )

    with TestClient(create_app(context)):
        pass

    assert store.list_pending_reports() == []
    rows = store.recent_analyses("stu-done", limit=10, session_id="sess-done")
    assert len(rows) == 1
    assert rows[0]["topic"] == "existing analysis"


def test_lifespan_matches_pending_reports_to_nearest_raw_transcript(tmp_path):
    seen_prompts = []

    async def fake_llm(config, snap, event, latest_prompt):
        prompt = snap.messages[0].text
        seen_prompts.append(prompt)
        return {
            "topic": prompt,
            "understanding": "medium",
            "off_topic": False,
            "stuck_at": "",
            "is_technical": True,
            "severity": "info",
            "diagnosis": f"diagnosis for {prompt}",
            "suggestion": "continue",
            "progress": "debugging",
            "guidance": "keep going",
            "alert": "",
            "ai_reply_summary": f"summary for {prompt}",
        }

    store = Store(tmp_path / "copilot.db")
    bus = EventBus()
    registry = WSRegistry(send_timeout=0.05)
    config = {
        "student_id": "server",
        "service": {"host": "127.0.0.1", "port": 8765},
        "store": {"db_path": str(tmp_path / "copilot.db")},
        "llm": {},
    }
    analysis_svc = AnalysisService(store, fake_llm, config, bus)
    first_transcript = _line({
        "type": "message",
        "role": "user",
        "content": "first pending transcript",
        "sessionId": "sess-shared",
    })
    second_transcript = _line({
        "type": "message",
        "role": "user",
        "content": "second pending transcript",
        "sessionId": "sess-shared",
    })
    first_report, _, _ = analysis_svc.accept_report(
        student_id="stu-shared",
        session_id="sess-shared",
        event="Stop",
        prompt_text="",
        transcript_content=first_transcript,
        raw_transcript_content=first_transcript,
        cwd="/work/shared",
    )
    second_report, _, _ = analysis_svc.accept_report(
        student_id="stu-shared",
        session_id="sess-shared",
        event="Stop",
        prompt_text="",
        transcript_content=second_transcript,
        raw_transcript_content=second_transcript,
        cwd="/work/shared",
    )
    with store._conn() as conn:
        raw_rows = conn.execute(
            "SELECT id FROM raw_transcripts WHERE session_id = ? ORDER BY id",
            ("sess-shared",),
        ).fetchall()
        conn.execute("UPDATE reports SET created_at = ? WHERE id = ?", (10.0, first_report))
        conn.execute("UPDATE raw_transcripts SET created_at = ? WHERE id = ?", (11.0, raw_rows[0]["id"]))
        conn.execute("UPDATE reports SET created_at = ? WHERE id = ?", (20.0, second_report))
        conn.execute("UPDATE raw_transcripts SET created_at = ? WHERE id = ?", (21.0, raw_rows[1]["id"]))

    context = AppContext(
        config=config,
        store=store,
        analysis_svc=analysis_svc,
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=registry,
    )

    with TestClient(create_app(context)):
        pass

    assert seen_prompts == ["first pending transcript", "second pending transcript"]
    rows = store.recent_analyses("stu-shared", limit=10, session_id="sess-shared")
    by_report = {row["report_id"]: row["topic"] for row in rows}
    assert by_report == {
        first_report: "first pending transcript",
        second_report: "second pending transcript",
    }
    assert store.list_pending_reports() == []
