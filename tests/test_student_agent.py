from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from copilot.student_core.agent import StudentAgent
from copilot.student_core.coordinator import StudentCoordinator
from copilot.student_core.models import HookEvent
from copilot.student_core.spool import EventSpool
from copilot.student_core.transport import Accepted


class FakeTransport:
    student_id = "student-1"

    def post_hook(self, event: HookEvent, *, event_id: str = "") -> Accepted:
        return Accepted(202)


class PersistentSocket:
    def __init__(self, first_event: dict[str, Any], received: asyncio.Event) -> None:
        self.first_event = first_event
        self.received = received
        self.closed = False
        self._sent_first = False
        self._block = asyncio.Event()

    async def __aenter__(self) -> "PersistentSocket":
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True

    async def recv(self) -> str:
        if not self._sent_first:
            self._sent_first = True
            return json.dumps(self.first_event)
        await self._block.wait()
        raise AssertionError("blocking socket unexpectedly resumed")


class PersistentTransport(FakeTransport):
    def __init__(self, socket: PersistentSocket) -> None:
        super().__init__()
        self.socket = socket
        self.acked: list[tuple[str, str]] = []
        self.opens = 0

    def open_ws(self) -> PersistentSocket:
        self.opens += 1
        return self.socket

    async def ack_message(self, message_id: str, *, student_id: str) -> Accepted:
        self.acked.append((student_id, message_id))
        return Accepted(200, {"ok": True})


class FailingSocket:
    async def __aenter__(self):
        raise OSError("offline")

    async def __aexit__(self, *_args: object) -> None:
        return None


class ReconnectingTransport(PersistentTransport):
    def open_ws(self):
        self.opens += 1
        return FailingSocket() if self.opens == 1 else self.socket


class CountingCoordinator:
    def __init__(self) -> None:
        self.cycles = 0

    async def flush_spool_once(self) -> int:
        self.cycles += 1
        return 1


class RecoveryCoordinator(CountingCoordinator):
    def __init__(self) -> None:
        super().__init__()
        self.pull_calls = 0

    async def pull_pending_messages(self) -> int:
        self.pull_calls += 1
        return 1


def test_agent_one_cycle_is_injectable_and_does_not_sleep() -> None:
    async def scenario() -> None:
        coordinator = CountingCoordinator()
        sleeper_calls: list[float] = []
        agent = StudentAgent(coordinator, sleeper=lambda delay: sleeper_calls.append(delay))

        assert await agent.one_cycle() == 1
        assert coordinator.cycles == 1
        assert sleeper_calls == []

    asyncio.run(scenario())


def test_agent_one_cycle_pulls_pending_receipts_without_waiting_for_new_ws_frame() -> None:
    async def scenario() -> None:
        coordinator = RecoveryCoordinator()
        agent = StudentAgent(coordinator, sleeper=lambda _: None)

        assert await agent.one_cycle() == 1
        assert coordinator.cycles == 1
        assert coordinator.pull_calls == 1

    asyncio.run(scenario())


def test_agent_one_cycle_keeps_running_after_spool_filesystem_failure() -> None:
    class FailingSpoolCoordinator:
        def __init__(self) -> None:
            self.pull_calls = 0

        async def flush_spool_once(self) -> int:
            raise OSError("spool directory unavailable")

        async def pull_pending_messages(self) -> int:
            self.pull_calls += 1
            return 0

    async def scenario() -> None:
        coordinator = FailingSpoolCoordinator()
        agent = StudentAgent(coordinator, sleeper=lambda _: None)

        assert await agent.one_cycle() == 0
        assert coordinator.pull_calls == 1

    asyncio.run(scenario())


def test_agent_start_stop_can_be_driven_without_ui_or_platform_modules() -> None:
    async def scenario() -> None:
        coordinator = CountingCoordinator()
        wake = asyncio.Event()

        async def sleeper(_: float) -> None:
            wake.set()
            await asyncio.sleep(0)

        agent = StudentAgent(coordinator, sleeper=sleeper, interval=0)
        task = agent.start()
        await asyncio.wait_for(wake.wait(), timeout=1)
        await agent.stop()
        await asyncio.wait_for(task, timeout=1)

        assert agent.stopped is True
        assert coordinator.cycles >= 1

    asyncio.run(scenario())


def test_agent_can_run_one_real_spool_cycle(tmp_path: Path) -> None:
    async def scenario() -> None:
        spool = EventSpool(tmp_path)
        event_id = spool.enqueue(
            HookEvent(event="Stop", student_id="student-1"), event_id="event-1"
        )
        coordinator = StudentCoordinator(spool, FakeTransport())
        agent = StudentAgent(coordinator, sleeper=lambda _: None)

        assert await agent.one_cycle() == 1
        assert spool.pending() == []
        assert event_id == "event-1"

    asyncio.run(scenario())


def test_agent_keeps_one_persistent_ws_and_dispatches_received_event(tmp_path: Path) -> None:
    async def scenario() -> None:
        delivered = asyncio.Event()
        socket = PersistentSocket(
            {"type": "mentor_message", "student_id": "student-1", "message_id": "ws-message"},
            delivered,
        )
        transport = PersistentTransport(socket)
        coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            transport,
            message_handler=lambda _payload: delivered.set(),
        )
        agent = StudentAgent(coordinator, interval=60)
        task = agent.start()
        await asyncio.wait_for(delivered.wait(), timeout=0.2)
        await asyncio.wait_for(agent.stop(), timeout=0.2)
        await asyncio.wait_for(task, timeout=0.2)

        assert transport.opens == 1
        assert transport.acked == [("student-1", "ws-message")]
        assert socket.closed is True

    asyncio.run(scenario())


def test_agent_pulls_pending_receipts_immediately_after_websocket_connect(tmp_path: Path) -> None:
    async def scenario() -> None:
        received = asyncio.Event()
        pulled_after_connect = asyncio.Event()
        socket = PersistentSocket(
            {"type": "mentor_message", "student_id": "student-1", "message_id": "connect-pull"},
            received,
        )
        transport = PersistentTransport(socket)
        coordinator = StudentCoordinator(EventSpool(tmp_path), transport)
        original_pull = coordinator.pull_pending_messages

        async def track_pull() -> int:
            if transport.opens:
                pulled_after_connect.set()
            return await original_pull()

        coordinator.pull_pending_messages = track_pull  # type: ignore[method-assign]
        agent = StudentAgent(coordinator, interval=60)
        task = agent.start()
        await asyncio.wait_for(pulled_after_connect.wait(), timeout=0.2)
        await agent.stop()
        await task

        assert transport.opens == 1

    asyncio.run(scenario())


def test_agent_stop_cancels_a_blocking_sleeper_with_bounded_wait() -> None:
    async def scenario() -> None:
        coordinator = CountingCoordinator()
        sleeper_entered = asyncio.Event()
        block = asyncio.Event()

        async def blocking_sleeper(_: float) -> None:
            sleeper_entered.set()
            await block.wait()

        agent = StudentAgent(coordinator, sleeper=blocking_sleeper, stop_timeout=0.1)
        task = agent.start()
        await asyncio.wait_for(sleeper_entered.wait(), timeout=0.2)
        await asyncio.wait_for(agent.stop(), timeout=0.2)
        await asyncio.wait_for(task, timeout=0.2)

        assert agent.stopped is True

    asyncio.run(scenario())


def test_agent_reconnects_after_socket_error_without_crashing(tmp_path: Path) -> None:
    async def scenario() -> None:
        delivered = asyncio.Event()
        socket = PersistentSocket(
            {"type": "mentor_message", "student_id": "student-1", "message_id": "after-reconnect"},
            delivered,
        )
        transport = ReconnectingTransport(socket)
        reconnect_delays: list[float] = []
        coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            transport,
            message_handler=lambda _payload: delivered.set(),
            sleeper=lambda delay: reconnect_delays.append(delay),
        )
        agent = StudentAgent(coordinator, interval=60)
        task = agent.start()
        await asyncio.wait_for(delivered.wait(), timeout=0.2)
        await agent.stop()
        await task

        assert transport.opens == 2
        assert reconnect_delays == [1.0]
        assert transport.acked == [("student-1", "after-reconnect")]

    asyncio.run(scenario())
