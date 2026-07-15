from __future__ import annotations

import asyncio
import json

from copilot.connections import WSRegistry


class FakeWebSocket:
    def __init__(self, *, delay: float = 0.0, fail: bool = False):
        self.delay = delay
        self.fail = fail
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("dead socket")
        self.sent.append(json.loads(text))


def test_forward_events_broadcast_to_mentors_and_only_target_float():
    async def scenario():
        registry = WSRegistry(send_timeout=0.05)
        mentor_a = FakeWebSocket()
        mentor_b = FakeWebSocket()
        float_a1 = FakeWebSocket()
        float_a2 = FakeWebSocket()
        float_b = FakeWebSocket()

        registry.register_mentor(mentor_a)
        registry.register_mentor(mentor_b)
        registry.register_float("student-a", float_a1)
        registry.register_float("student-a", float_a2)
        registry.register_float("student-b", float_b)

        payload = {
            "type": "analysis",
            "student_id": "student-a",
            "session_id": "sess-a",
            "result": {"topic": "loops"},
            "timestamp": 1.0,
        }
        await registry.handle_event(payload)

        assert mentor_a.sent == [payload]
        assert mentor_b.sent == [payload]
        assert float_a1.sent == [payload]
        assert float_a2.sent == [payload]
        assert float_b.sent == []

    asyncio.run(scenario())


def test_mentor_message_targets_one_student_without_claiming_delivery_before_receipt():
    async def scenario():
        registry = WSRegistry(send_timeout=0.05)
        mentor = FakeWebSocket()
        target_float = FakeWebSocket()
        other_float = FakeWebSocket()

        registry.register_mentor(mentor)
        registry.register_float("student-a", target_float)
        registry.register_float("student-b", other_float)

        await registry.handle_event({
            "type": "mentor_message",
            "student_id": "student-a",
            "message_id": "msg-1",
            "id": 7,
            "text": "Try a smaller example",
            "mentor_id": "mentor-1",
            "timestamp": 10.0,
        })

        assert target_float.sent == [{
            "type": "mentor_message",
            "student_id": "student-a",
            "message_id": "msg-1",
            "id": 7,
            "text": "Try a smaller example",
            "mentor_id": "mentor-1",
            "timestamp": 10.0,
        }]
        assert other_float.sent == []
        # A successful WebSocket write only means the server attempted delivery.
        # StudentAgent must post its REST receipt before the mentor can see a
        # durable "delivered" state.
        assert mentor.sent == []

    asyncio.run(scenario())


def test_mentor_command_targets_only_one_student_without_mentor_broadcast():
    async def scenario():
        registry = WSRegistry(send_timeout=0.05)
        mentor = FakeWebSocket()
        target_float = FakeWebSocket()
        other_float = FakeWebSocket()

        registry.register_mentor(mentor)
        registry.register_float("student-a", target_float)
        registry.register_float("student-b", other_float)

        payload = {
            "type": "mentor_command",
            "student_id": "student-a",
            "command": "upload_conversations",
            "request_id": "req-1",
            "timestamp": 10.0,
        }
        await registry.handle_event(payload)

        assert target_float.sent == [payload]
        assert other_float.sent == []
        assert mentor.sent == []

    asyncio.run(scenario())


def test_send_timeout_and_exception_remove_dead_sockets_without_blocking_others():
    async def scenario():
        registry = WSRegistry(send_timeout=0.01)
        ok_mentor = FakeWebSocket()
        stuck_mentor = FakeWebSocket(delay=1.0)
        failing_float = FakeWebSocket(fail=True)
        ok_float = FakeWebSocket()

        registry.register_mentor(ok_mentor)
        registry.register_mentor(stuck_mentor)
        registry.register_float("student-a", failing_float)
        registry.register_float("student-a", ok_float)

        payload = {
            "type": "prompt",
            "student_id": "student-a",
            "prompt": "hello",
            "timestamp": 2.0,
        }
        await registry.handle_event(payload)

        assert ok_mentor.sent == [payload]
        assert ok_float.sent == [payload]
        assert stuck_mentor not in registry.mentors
        assert failing_float not in registry.floats["student-a"]
        assert ok_mentor in registry.mentors
        assert ok_float in registry.floats["student-a"]

    asyncio.run(scenario())


def test_pilot_capacity_routes_50_students_to_25_mentors_without_cross_delivery():
    async def scenario():
        registry = WSRegistry(send_timeout=0.1)
        mentors = [FakeWebSocket() for _ in range(25)]
        floats = {f"student-{index}": FakeWebSocket() for index in range(50)}
        for mentor in mentors:
            registry.register_mentor(mentor)
        for student_id, socket in floats.items():
            registry.register_float(student_id, socket)

        payloads = [
            {
                "type": "analysis",
                "student_id": student_id,
                "report_id": index + 1,
            }
            for index, student_id in enumerate(floats)
        ]
        await asyncio.gather(*(registry.handle_event(payload) for payload in payloads))

        assert registry.connection_counts() == (50, 25)
        assert all(len(mentor.sent) == 50 for mentor in mentors)
        for student_id, socket in floats.items():
            assert [item["student_id"] for item in socket.sent] == [student_id]

    asyncio.run(scenario())
