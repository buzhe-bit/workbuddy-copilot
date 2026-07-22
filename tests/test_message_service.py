from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from copilot.connections import WSRegistry
from copilot.eventbus import EventBus
from copilot.services import MessageService
from copilot.store import Store


class FakeWebSocket:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


def test_send_persists_before_live_push_and_waits_for_student_rest_receipt(tmp_path):
    async def scenario():
        store = Store(tmp_path / "messages.db")
        bus = EventBus()
        seen_during_publish = []
        target = FakeWebSocket()
        registry = WSRegistry(send_timeout=0.05)
        registry.register_float("student-a", target)

        async def assert_persisted_before_push(payload):
            rows = store.list_messages_since("student-a", 0)
            seen_during_publish.append((payload["message_id"], rows[0]["delivered_at"]))

        bus.subscribe(assert_persisted_before_push)
        bus.subscribe(registry.handle_event)
        service = MessageService(store, bus)

        result = await service.send("student-a", "mentor-1", "Try a smaller example")

        rows = store.list_messages_since("student-a", 0)
        assert len(rows) == 1
        assert rows[0]["message_id"] == result["message_id"]
        assert seen_during_publish == [(result["message_id"], None)]
        assert rows[0]["delivered_at"] is None
        assert result == {
            "message_id": rows[0]["message_id"],
            "id": rows[0]["id"],
            "student_id": "student-a",
            "session_id": "",
            "scope": "student",
            "client_request_id": None,
            "delivered": False,
            "duplicate": False,
        }

        assert await service.ack(result["message_id"], "student-a") is True
        assert store.list_messages_since("student-a", 0)[0]["delivered_at"] is not None

    asyncio.run(scenario())


def test_send_offline_remains_undelivered_and_catchup_filters_since(tmp_path):
    async def scenario():
        store = Store(tmp_path / "messages.db")
        bus = EventBus()
        registry = WSRegistry(send_timeout=0.05)
        bus.subscribe(registry.handle_event)
        service = MessageService(store, bus)

        first = await service.send("student-a", "mentor-1", "First")
        second = await service.send("student-a", "mentor-1", "Second")

        rows = store.list_messages_since("student-a", 0)
        assert [row["message_id"] for row in rows] == [first["message_id"], second["message_id"]]
        assert all(row["delivered_at"] is None for row in rows)
        assert first["delivered"] is False
        assert second["delivered"] is False

        catchup = service.get_catchup("student-a", first["id"])

        assert [row["message_id"] for row in catchup] == [second["message_id"]]
        assert catchup[0]["type"] == "mentor_message"

    asyncio.run(scenario())


def test_catchup_limit_bounds_backlog_in_store_order(tmp_path):
    async def scenario():
        store = Store(tmp_path / "messages.db")
        service = MessageService(store, EventBus())

        first = await service.send("student-a", "mentor-1", "First")
        second = await service.send("student-a", "mentor-1", "Second")
        third = await service.send("student-a", "mentor-1", "Third")

        pending = service.get_catchup("student-a", 0, limit=2)

        assert [row["message_id"] for row in pending] == [
            first["message_id"],
            second["message_id"],
        ]
        assert third["message_id"] not in {row["message_id"] for row in pending}

    asyncio.run(scenario())


def test_ack_is_idempotent_and_message_ids_are_unique(tmp_path):
    async def scenario():
        store = Store(tmp_path / "messages.db")
        service = MessageService(store, EventBus())

        first = await service.send("student-a", "mentor-1", "Same text")
        second = await service.send("student-a", "mentor-1", "Same text")

        assert first["message_id"] != second["message_id"]
        assert await service.ack(first["message_id"], "student-a") is True
        delivered_once = store.list_messages_since("student-a", 0)[0]["delivered_at"]
        assert await service.ack(first["message_id"], "student-a") is True
        delivered_twice = store.list_messages_since("student-a", 0)[0]["delivered_at"]
        assert delivered_twice == delivered_once
        assert await service.ack(first["message_id"], "student-b") is False

    asyncio.run(scenario())


def test_ack_publishes_delivery_receipt_to_mentors_after_offline_catchup(tmp_path):
    async def scenario():
        store = Store(tmp_path / "messages.db")
        bus = EventBus()
        registry = WSRegistry(send_timeout=0.05)
        mentor = FakeWebSocket()
        registry.register_mentor(mentor)
        bus.subscribe(registry.handle_event)
        service = MessageService(store, bus)

        sent = await service.send("student-a", "mentor-1", "Catch up when you reconnect")
        assert sent["delivered"] is False
        assert mentor.sent == []
        assert store.list_messages_since("student-a", 0)[0]["delivered_at"] is None

        assert await service.ack(sent["message_id"], "student-a") is True

        delivered_row = store.list_messages_since("student-a", 0)[0]
        assert delivered_row["delivered_at"] is not None
        assert [json.loads(text) for text in mentor.sent] == [{
            "type": "message_delivered",
            "student_id": "student-a",
            "message_id": sent["message_id"],
            "id": sent["id"],
            "timestamp": delivered_row["delivered_at"],
        }]

    asyncio.run(scenario())


def test_ack_does_not_republish_receipt_when_message_is_already_delivered(tmp_path):
    async def scenario():
        store = Store(tmp_path / "messages.db")
        bus = EventBus()
        registry = WSRegistry(send_timeout=0.05)
        mentor = FakeWebSocket()
        registry.register_mentor(mentor)
        bus.subscribe(registry.handle_event)
        service = MessageService(store, bus)

        sent = await service.send("student-a", "mentor-1", "Already pushed live")
        store.mark_message_delivered(sent["message_id"], student_id="student-a")

        assert await service.ack(sent["message_id"], "student-a") is True
        assert mentor.sent == []

    asyncio.run(scenario())


def test_ack_receipt_includes_client_request_id_for_retry_safe_message(tmp_path):
    async def scenario():
        store = Store(tmp_path / "messages.db")
        bus = EventBus()
        published = []

        async def capture(payload):
            published.append(payload)

        bus.subscribe(capture)
        service = MessageService(store, bus)
        sent = await service.send(
            "student-a",
            "mentor-1",
            "Retry-safe receipt",
            client_request_id="request-receipt",
        )
        published.clear()

        assert await service.ack(sent["message_id"], "student-a") is True
        row = store.list_messages_since("student-a", 0)[0]
        assert published == [{
            "type": "message_delivered",
            "student_id": "student-a",
            "message_id": sent["message_id"],
            "id": sent["id"],
            "client_request_id": "request-receipt",
            "timestamp": row["delivered_at"],
        }]

    asyncio.run(scenario())


def test_client_request_id_is_idempotent_and_publishes_only_once(tmp_path):
    async def scenario():
        store = Store(tmp_path / "messages.db")
        bus = EventBus()
        published = []

        async def capture(payload):
            published.append(payload)

        bus.subscribe(capture)
        service = MessageService(store, bus)

        results = await asyncio.gather(*[
            service.send(
                "student-a",
                "mentor-1",
                "Try a smaller example",
                client_request_id="request-123",
            )
            for _ in range(10)
        ])
        restarted = MessageService(Store(store.db_path), bus)
        replay_after_restart = await restarted.send(
            "student-a",
            "mentor-1",
            "Try a smaller example",
            client_request_id="request-123",
        )

        rows = store.list_messages_since("student-a", 0)
        assert len(rows) == 1
        assert rows[0]["client_request_id"] == "request-123"
        assert len(published) == 1
        assert {result["message_id"] for result in results} == {rows[0]["message_id"]}
        assert sum(result["duplicate"] is False for result in results) == 1
        assert sum(result["duplicate"] is True for result in results) == 9
        assert replay_after_restart["duplicate"] is True
        assert replay_after_restart["message_id"] == rows[0]["message_id"]

    asyncio.run(scenario())


def test_client_request_id_reuse_with_different_payload_is_rejected(tmp_path):
    async def scenario():
        store = Store(tmp_path / "messages.db")
        service = MessageService(store, EventBus())

        await service.send(
            "student-a",
            "mentor-1",
            "Original",
            client_request_id="request-conflict",
        )

        try:
            await service.send(
                "student-b",
                "mentor-1",
                "Changed text",
                client_request_id="request-conflict",
            )
        except ValueError as exc:
            assert "client_request_id" in str(exc)
        else:
            raise AssertionError("conflicting idempotency key must be rejected")

        rows = store.list_messages_since("student-a", 0)
        assert len(rows) == 1
        assert rows[0]["text"] == "Original"
        with sqlite3.connect(store.db_path) as conn:
            students = {
                row[0] for row in conn.execute("SELECT student_id FROM students")
            }
        assert students == {"student-a"}

    asyncio.run(scenario())


def test_mentor_message_status_recovers_delivery_without_exposing_text(tmp_path):
    async def scenario():
        store = Store(tmp_path / "messages.db")
        service = MessageService(store, EventBus())
        sent = await service.send(
            "student-a",
            "mentor-1",
            "Private mentor guidance",
            client_request_id="request-status",
        )

        assert await service.ack(sent["message_id"], "student-a") is True
        statuses = service.get_mentor_message_statuses([
            "missing-request",
            "request-status",
            "request-status",
        ])

        assert statuses == [{
            "client_request_id": "request-status",
            "message_id": sent["message_id"],
            "id": sent["id"],
            "student_id": "student-a",
            "session_id": "",
            "scope": "student",
            "delivered": True,
        }]
        assert "text" not in statuses[0]

    asyncio.run(scenario())


def test_legacy_mentor_message_schema_migrates_idempotency_column_reentrantly(tmp_path):
    db_path = tmp_path / "legacy-messages.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TABLE mentor_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT,
            mentor_id TEXT,
            session_id TEXT,
            text TEXT,
            message_id TEXT UNIQUE,
            created_at REAL,
            delivered_at REAL,
            read_at REAL
        )""")

    Store(db_path)
    Store(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(mentor_messages)",
            ).fetchall()
        }
        index_names = {
            row[1] for row in conn.execute(
                "PRAGMA index_list(mentor_messages)",
            ).fetchall()
        }
    assert "client_request_id" in columns
    assert "idx_mentor_messages_client_request_unique" in index_names


def test_session_message_is_scoped_and_isolated_from_other_sessions(tmp_path):
    async def scenario():
        store = Store(tmp_path / "messages.db")
        store.upsert_session("session-a", "student-a", "", "", created_at=1, last_activity_at=1)
        store.upsert_session("session-b", "student-a", "", "", created_at=1, last_activity_at=1)
        published = []

        async def capture(payload):
            published.append(payload)

        service = MessageService(store, EventBus())
        service.bus.subscribe(capture)
        sent = await service.send(
            "student-a", "mentor-1", "Only session A", session_id="session-a",
            client_request_id="session-request",
        )

        assert sent["student_id"] == "student-a"
        assert sent["session_id"] == "session-a"
        assert sent["scope"] == "session"
        assert sent["client_request_id"] == "session-request"
        assert sent["duplicate"] is False
        assert published[0]["session_id"] == "session-a"
        assert published[0]["scope"] == "session"
        assert published[0]["client_request_id"] == "session-request"
        assert service.get_mentor_message_statuses(["session-request"])[0] == {
            "client_request_id": "session-request",
            "message_id": sent["message_id"],
            "id": sent["id"],
            "student_id": "student-a",
            "session_id": "session-a",
            "scope": "session",
            "delivered": False,
        }
        assert not [row for row in store.get_timeline_by_session("session-b") if row["type"] == "mentor_message"]
        timeline = store.get_timeline_by_session("session-a")
        assert [row["content"] for row in timeline if row["type"] == "mentor_message"] == ["Only session A"]

        await service.send("student-a", "mentor-1", "Student notice")
        assert [row["content"] for row in store.get_timeline_by_session("session-a") if row["type"] == "mentor_message"] == ["Only session A"]

    asyncio.run(scenario())


def test_session_message_rejects_missing_or_foreign_session_without_creating_a_row(tmp_path):
    async def scenario():
        store = Store(tmp_path / "messages.db")
        store.upsert_session("other-session", "student-b", "", "", created_at=1, last_activity_at=1)
        service = MessageService(store, EventBus())

        for session_id in ("missing-session", "other-session"):
            try:
                await service.send("student-a", "mentor-1", "Private", session_id=session_id)
            except LookupError:
                pass
            else:
                raise AssertionError("unknown or foreign session must be fail-closed")

        assert store.list_messages_since("student-a", 0) == []

    asyncio.run(scenario())


def test_client_request_id_conflicts_when_session_changes(tmp_path):
    async def scenario():
        store = Store(tmp_path / "messages.db")
        for session_id in ("session-a", "session-b"):
            store.upsert_session(session_id, "student-a", "", "", created_at=1, last_activity_at=1)
        service = MessageService(store, EventBus())
        await service.send(
            "student-a", "mentor-1", "Same text", session_id="session-a",
            client_request_id="same-key",
        )

        with pytest.raises(ValueError, match="client_request_id payload conflict"):
            await service.send(
                "student-a", "mentor-1", "Same text", session_id="session-b",
                client_request_id="same-key",
            )

    asyncio.run(scenario())
