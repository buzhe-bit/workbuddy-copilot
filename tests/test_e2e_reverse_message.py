from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from typing import Any

from fastapi.testclient import TestClient

from copilot.floating_native import CopilotNativeApp
from copilot.app_context import AppContext
from copilot.connections import WSRegistry
from copilot.eventbus import EventBus
from copilot.service import create_app
from copilot.services import AnalysisService, MessageService, SessionQueryService
from copilot.store import Store


TOKEN = "phase4-token"


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


async def _fake_llm(config, snap, event, latest_prompt):
    return {
        "topic": "",
        "understanding": "medium",
        "severity": "info",
        "diagnosis": "",
        "suggestion": "",
        "is_technical": False,
        "ai_reply_summary": "",
    }


def _auth_headers() -> dict[str, str]:
    return {"X-Copilot-Token": TOKEN}


def _build_message_app(tmp_path):
    store = Store(tmp_path / "copilot.db")
    bus = EventBus()
    registry = WSRegistry(send_timeout=0.05)
    bus.subscribe(registry.handle_event)
    config = {"student_id": "mentor-host", "auth": {"token": TOKEN}}
    context = AppContext(
        config=config,
        store=store,
        analysis_svc=AnalysisService(store, _fake_llm, config, bus),
        session_svc=SessionQueryService(store, config),
        message_svc=MessageService(store, bus),
        bus=bus,
        ws_registry=registry,
    )
    return create_app(context), store, registry


def test_online_send_targets_one_float_but_waits_for_student_receipt(tmp_path):
    async def scenario():
        store = Store(tmp_path / "messages.db")
        bus = EventBus()
        registry = WSRegistry(send_timeout=0.05)
        bus.subscribe(registry.handle_event)
        service = MessageService(store, bus)

        mentor_ws = FakeWebSocket()
        float_a = FakeWebSocket()
        float_b = FakeWebSocket()
        registry.register_mentor(mentor_ws)
        registry.register_float("student-a", float_a)
        registry.register_float("student-b", float_b)

        result = await service.send("student-a", "mentor-1", "Try a smaller example")

        assert result["delivered"] is False
        assert len(float_a.sent) == 1
        assert float_a.sent[0] == {
            "type": "mentor_message",
            "student_id": "student-a",
            "message_id": result["message_id"],
            "id": result["id"],
            "text": "Try a smaller example",
            "mentor_id": "mentor-1",
            "timestamp": float_a.sent[0]["timestamp"],
        }
        assert float_b.sent == []
        assert mentor_ws.sent == []

        rows = store.list_messages_since("student-a", 0)
        assert len(rows) == 1
        assert rows[0]["message_id"] == result["message_id"]
        assert rows[0]["delivered_at"] is None

        assert await service.ack(result["message_id"], "student-a") is True
        delivered_at = store.list_messages_since("student-a", 0)[0]["delivered_at"]
        assert delivered_at is not None
        assert mentor_ws.sent == [{
            "type": "message_delivered",
            "student_id": "student-a",
            "message_id": result["message_id"],
            "id": result["id"],
            "timestamp": delivered_at,
        }]

    asyncio.run(scenario())


def test_offline_send_is_caught_up_by_student_api_and_ack_marks_delivered(tmp_path):
    app, store, registry = _build_message_app(tmp_path)
    assert registry.floats == {}
    with TestClient(app) as client:
        sent_a = client.post(
            "/api/mentor/message",
            headers=_auth_headers(),
            json={
                "student_id": "student-a",
                "mentor_id": "mentor-1",
                "text": "Read the error from top to bottom",
            },
        )
        sent_b = client.post(
            "/api/mentor/message",
            headers=_auth_headers(),
            json={
                "student_id": "student-b",
                "mentor_id": "mentor-1",
                "text": "This belongs only to student-b",
            },
        )
        assert sent_a.status_code == 200
        assert sent_b.status_code == 200
        sent_a_body = sent_a.json()
        sent_b_body = sent_b.json()
        assert sent_a_body["delivered"] is False
        assert sent_b_body["delivered"] is False

        rows = store.list_messages_since("student-a", 0)
        assert len(rows) == 1
        assert rows[0]["message_id"] == sent_a_body["message_id"]
        assert rows[0]["delivered_at"] is None

        catchup = client.get(
            "/api/student/messages?student_id=student-a&since=0",
            headers=_auth_headers(),
        )
        assert catchup.status_code == 200
        assert catchup.json()["items"] == [{
            "type": "mentor_message",
            "student_id": "student-a",
            "message_id": sent_a_body["message_id"],
            "id": sent_a_body["id"],
            "text": "Read the error from top to bottom",
            "mentor_id": "mentor-1",
            "timestamp": catchup.json()["items"][0]["timestamp"],
        }]
        assert sent_b_body["message_id"] not in {
            item["message_id"] for item in catchup.json()["items"]
        }

        catchup_b = client.get(
            "/api/student/messages?student_id=student-b&since=0",
            headers=_auth_headers(),
        )
        assert catchup_b.status_code == 200
        assert [item["message_id"] for item in catchup_b.json()["items"]] == [
            sent_b_body["message_id"],
        ]

        no_duplicate_catchup = client.get(
            f"/api/student/messages?student_id=student-a&since={sent_a_body['id']}",
            headers=_auth_headers(),
        )
        assert no_duplicate_catchup.status_code == 200
        assert no_duplicate_catchup.json()["items"] == []

        ack = client.post(
            "/api/student/messages/ack",
            headers=_auth_headers(),
            json={"student_id": "student-a", "message_id": sent_a_body["message_id"]},
        )
        assert ack.status_code == 200
        assert ack.json() == {"ok": True}

        delivered_at = store.list_messages_since("student-a", 0)[0]["delivered_at"]
        assert delivered_at is not None


def test_generic_catchup_omits_301_delivered_rows_after_recovery_cursor_rolls_back(tmp_path):
    """A recovery cursor must never replay confirmed history into the float UI.

    The first row represents a card rendered before its local state publish
    failed, so it remains undelivered and must be recoverable.  The other 300
    rows have already received their durable REST receipt.  A restart can
    legitimately reconnect with ``since=0``; returning all 301 rows would
    exceed the float's bounded display de-duplication cache and re-render the
    confirmed history.
    """
    app, store, _registry = _build_message_app(tmp_path)
    store.upsert_student("student-a")
    recoverable_message_id = "recoverable-unpersisted"
    store.add_mentor_message(
        student_id="student-a",
        mentor_id="mentor-1",
        session_id="",
        text="must remain recoverable",
        message_id=recoverable_message_id,
    )
    for index in range(300):
        message_id = f"already-confirmed-{index:03d}"
        store.add_mentor_message(
            student_id="student-a",
            mentor_id="mentor-1",
            session_id="",
            text=f"confirmed {index}",
            message_id=message_id,
        )
        assert store.mark_message_delivered(message_id, student_id="student-a") == 1

    with TestClient(app) as client:
        catchup = client.get(
            "/api/student/messages?student_id=student-a&since=0",
            headers=_auth_headers(),
        )

    assert catchup.status_code == 200
    assert [item["message_id"] for item in catchup.json()["items"]] == [
        recoverable_message_id,
    ]


def test_pending_receipt_endpoint_omits_64_delivered_messages_and_returns_one_pending(tmp_path):
    app, store, _registry = _build_message_app(tmp_path)
    store.upsert_student("student-a")
    for index in range(64):
        message_id = f"delivered-{index}"
        store.add_mentor_message(
            student_id="student-a",
            mentor_id="mentor-1",
            session_id="",
            text=f"delivered {index}",
            message_id=message_id,
        )
        assert store.mark_message_delivered(message_id, student_id="student-a") == 1
    pending_id = "pending-only"
    store.add_mentor_message(
        student_id="student-a",
        mentor_id="mentor-1",
        session_id="",
        text="receipt must recover this one",
        message_id=pending_id,
    )

    with TestClient(app) as client:
        pending = client.get(
            "/api/student/messages/pending-receipts?student_id=student-a&limit=64",
            headers=_auth_headers(),
        )
        assert pending.status_code == 200
        assert [item["message_id"] for item in pending.json()["items"]] == [pending_id]

        ack = client.post(
            "/api/student/messages/ack",
            headers=_auth_headers(),
            json={"student_id": "student-a", "message_id": pending_id},
        )
        assert ack.status_code == 200
        assert client.get(
            "/api/student/messages/pending-receipts?student_id=student-a&limit=64",
            headers=_auth_headers(),
        ).json()["items"] == []


def test_pending_receipt_endpoint_uses_cursor_to_page_past_unknown_messages(tmp_path):
    app, store, _registry = _build_message_app(tmp_path)
    store.upsert_student("student-a")
    for index in range(65):
        store.add_mentor_message(
            student_id="student-a",
            mentor_id="mentor-1",
            session_id="",
            text=f"pending {index}",
            message_id=f"pending-{index}",
        )

    with TestClient(app) as client:
        first = client.get(
            "/api/student/messages/pending-receipts?student_id=student-a&limit=64&after_id=0",
            headers=_auth_headers(),
        )
        assert first.status_code == 200
        first_items = first.json()["items"]
        assert len(first_items) == 64
        cursor = first_items[-1]["id"]

        second = client.get(
            f"/api/student/messages/pending-receipts?student_id=student-a&limit=64&after_id={cursor}",
            headers=_auth_headers(),
        )
        assert second.status_code == 200
        assert [item["message_id"] for item in second.json()["items"]] == ["pending-64"]


def test_same_message_id_cannot_create_duplicate_persisted_message(tmp_path):
    store = Store(tmp_path / "messages.db")
    store.upsert_student("student-a")

    first_id = store.add_mentor_message(
        student_id="student-a",
        mentor_id="mentor-1",
        session_id="",
        text="Same id",
        message_id="fixed-message-id",
    )
    try:
        store.add_mentor_message(
            student_id="student-a",
            mentor_id="mentor-1",
            session_id="",
            text="Duplicate id",
            message_id="fixed-message-id",
        )
    except sqlite3.IntegrityError:
        pass

    rows = store.list_messages_since("student-a", 0)
    assert [row["id"] for row in rows] == [first_id]
    assert [row["message_id"] for row in rows] == ["fixed-message-id"]
    assert [row["text"] for row in rows] == ["Same id"]


def test_300_message_history_survives_lost_ack_restart_and_ws_rest_duplicate(tmp_path):
    """One combined recovery path must render once and acknowledge durably."""
    app, store, registry = _build_message_app(tmp_path)
    mentor_ws = FakeWebSocket()
    registry.register_mentor(mentor_ws)
    store.upsert_student("student-a")
    for index in range(299):
        message_id = f"history-{index:03d}"
        store.add_mentor_message(
            student_id="student-a",
            mentor_id="mentor-1",
            session_id="",
            text=f"confirmed history {index}",
            message_id=message_id,
        )
        assert store.mark_message_delivered(message_id, student_id="student-a") == 1
    target_message_id = "history-target-299"
    target_row_id = store.add_mentor_message(
        student_id="student-a",
        mentor_id="mentor-1",
        session_id="",
        text="render this exactly once",
        message_id=target_message_id,
    )
    state_path = tmp_path / "float-state.json"

    class FakeFloat:
        _handle_mentor_message = CopilotNativeApp._handle_mentor_message
        _ack_mentor_message = CopilotNativeApp._ack_mentor_message
        _load_mentor_message_state = CopilotNativeApp._load_mentor_message_state
        _save_mentor_message_state = CopilotNativeApp._save_mentor_message_state

        def __init__(self, client, *, lose_ack_response: bool):
            self.client = client
            self.lose_ack_response = lose_ack_response
            self._student_id = "student-a"
            self._seen_mentor_message_ids: set[str] = set()
            self._seen_mentor_message_order: list[str] = []
            self._pending_receipt_message_ids: set[str] = set()
            self._unpersisted_rendered_message_ids: dict[str, int] = {}
            self._rendering_mentor_message_ids: set[str] = set()
            self._receipt_ack_inflight_ids: set[str] = set()
            self._mentor_message_state_lock = threading.RLock()
            self._last_seen_mentor_message_id = 0
            self._ws_asyncio_loop = None
            self.rendered: list[str] = []
            self.ack_posts: list[str] = []

        def _mentor_message_state_path(self):
            return str(state_path)

        def _render_mentor_message(self, item):
            self.rendered.append(item["message_id"])
            return True

        def _post_json(self, path, payload, *, timeout):
            assert path == "/api/student/messages/ack"
            assert timeout == 3
            self.ack_posts.append(payload["message_id"])
            response = self.client.post(
                path,
                headers=_auth_headers(),
                json=payload,
            )
            assert response.status_code == 200
            if self.lose_ack_response:
                raise TimeoutError("response lost after durable server ack")
            return response.json()

    with TestClient(app) as client:
        # Hold one REST catch-up response while the same persisted row arrives by WS.
        held_rest_item = client.get(
            "/api/student/messages?student_id=student-a&since=0",
            headers=_auth_headers(),
        ).json()["items"]
        assert len(held_rest_item) == 1
        payload = held_rest_item[0]
        assert payload["id"] == target_row_id

        writer = FakeFloat(client, lose_ack_response=True)
        writer._handle_mentor_message(payload)  # WS arrival
        writer._handle_mentor_message(payload)  # held REST duplicate

        assert writer.rendered == [target_message_id]
        assert writer._pending_receipt_message_ids == {target_message_id}
        assert writer.ack_posts == [target_message_id, target_message_id]

        restarted = FakeFloat(client, lose_ack_response=False)
        restarted._load_mentor_message_state()
        assert restarted._pending_receipt_message_ids == {target_message_id}
        restarted._handle_mentor_message(payload)  # replayed WS duplicate after restart
        restarted._handle_mentor_message(payload)  # replayed REST duplicate after restart

        assert restarted.rendered == []
        assert restarted.ack_posts == [target_message_id]
        assert restarted._pending_receipt_message_ids == set()
        assert client.get(
            "/api/student/messages?student_id=student-a&since=0",
            headers=_auth_headers(),
        ).json()["items"] == []

    rows = store.list_messages_since("student-a", 0)
    assert len(rows) == 300
    assert len({row["message_id"] for row in rows}) == 300
    target = next(row for row in rows if row["message_id"] == target_message_id)
    assert target["delivered_at"] is not None
    assert [item["type"] for item in mentor_ws.sent] == ["message_delivered"]
