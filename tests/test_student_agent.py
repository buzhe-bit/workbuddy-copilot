from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from copilot.models import AnalysisEnvelope
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


def test_agent_periodically_recovers_analysis_without_a_ws_wakeup(
    tmp_path: Path,
) -> None:
    class Transport(FakeTransport):
        def __init__(self) -> None:
            self.analysis_calls = 0

        async def get_recent_analyses_async(
            self,
            *,
            after_analysis_id: int,
            limit: int,
        ) -> dict[str, object]:
            self.analysis_calls += 1
            item = AnalysisEnvelope(
                analysis_id=7,
                student_id="student-1",
                session_id="session-1",
                report_id=3,
                event="Stop",
                result={"diagnosis": "durable recovery"},
                timestamp=7.0,
            ).to_dict()
            return {
                "items": [item] if after_analysis_id < 7 else [],
                "next_cursor": 7 if after_analysis_id < 7 else after_analysis_id,
                "has_more": False,
            }

    async def scenario() -> None:
        now = [0.0]
        transport = Transport()
        handled: list[int] = []
        coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            transport,
            analysis_handler=lambda payload: handled.append(
                int(payload["analysis_id"])
            ),
        )
        agent = StudentAgent(
            coordinator,
            analysis_poll_interval=30.0,
            monotonic=lambda: now[0],
        )

        await agent.one_cycle()
        await agent.one_cycle()
        now[0] = 29.9
        await agent.one_cycle()
        assert transport.analysis_calls == 0
        assert coordinator.analysis_cursor == 0

        # No WebSocket frame or reconnect occurred; the durable REST stream is
        # still polled at the bounded interval and advances the cursor.
        now[0] = 30.0
        await agent.one_cycle()
        assert transport.analysis_calls == 1
        assert coordinator.analysis_cursor == 7
        assert handled == [7]

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


def test_agent_buffers_live_analysis_before_catchup_then_deduplicates(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        live_read = asyncio.Event()
        rendered = asyncio.Event()
        block = asyncio.Event()

        def envelope(report_id: int) -> dict[str, Any]:
            return AnalysisEnvelope(
                student_id="student-1",
                session_id="session-1",
                report_id=report_id,
                event="Stop",
                result={"diagnosis": str(report_id)},
                timestamp=float(report_id),
            ).to_dict()

        class Socket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def recv(self):
                if not live_read.is_set():
                    live_read.set()
                    return json.dumps(envelope(2))
                await block.wait()
                raise AssertionError("socket unexpectedly resumed")

        class Transport(FakeTransport):
            student_id = "student-1"

            def open_ws(self):
                return Socket()

            async def get_recent_analyses_async(
                self,
                *,
                after_report_id: int,
                limit: int,
            ):
                assert limit == 64
                await asyncio.wait_for(live_read.wait(), timeout=0.2)
                values = [envelope(1), envelope(2)] if after_report_id == 0 else []
                return {
                    "items": values,
                    "next_cursor": 2 if values else after_report_id,
                    "has_more": False,
                }

        handled: list[int] = []

        def handle(payload: dict[str, Any]) -> None:
            handled.append(int(payload["report_id"]))
            if handled == [1, 2]:
                rendered.set()

        coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            Transport(),
            analysis_handler=handle,
        )
        agent = StudentAgent(coordinator, interval=60)
        task = agent.start()
        await asyncio.wait_for(rendered.wait(), timeout=0.4)
        await agent.stop()
        await task

        assert handled == [1, 2]
        assert coordinator.analysis_cursor == 2

    asyncio.run(scenario())


def test_agent_reconnects_instead_of_entering_realtime_with_incomplete_catchup(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        rendered = asyncio.Event()
        block = asyncio.Event()

        def envelope(analysis_id: int, report_id: int) -> dict[str, Any]:
            return AnalysisEnvelope(
                analysis_id=analysis_id,
                student_id="student-1",
                session_id="session-1",
                report_id=report_id,
                event="Stop",
                result={"diagnosis": str(report_id)},
                timestamp=float(analysis_id),
            ).to_dict()

        class Socket:
            def __init__(self, live_payload: dict[str, Any]) -> None:
                self.live_payload = live_payload
                self.sent = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def recv(self):
                if not self.sent:
                    self.sent = True
                    return json.dumps(self.live_payload)
                await block.wait()
                raise AssertionError("socket unexpectedly resumed")

        class Transport(FakeTransport):
            opens = 0
            second_page_sent = False

            def open_ws(self):
                self.opens += 1
                return Socket(envelope(99, 7))

            async def get_recent_analyses_async(
                self,
                *,
                after_analysis_id: int,
                limit: int,
            ):
                if self.opens == 1:
                    next_id = after_analysis_id + 1
                    return {
                        "items": [envelope(next_id, next_id)],
                        "next_cursor": next_id,
                        "has_more": True,
                    }
                if not self.second_page_sent:
                    self.second_page_sent = True
                    return {
                        "items": [envelope(99, 7)],
                        "next_cursor": 99,
                        "has_more": False,
                    }
                return {
                    "items": [],
                    "next_cursor": after_analysis_id,
                    "has_more": False,
                }

        transport = Transport()

        def handle(payload: dict[str, Any]) -> None:
            if int(payload["analysis_id"]) == 99:
                rendered.set()

        coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            transport,
            analysis_handler=handle,
            sleeper=lambda _delay: None,
        )
        agent = StudentAgent(coordinator, interval=60)
        task = agent.start()
        await asyncio.wait_for(rendered.wait(), timeout=0.5)
        await agent.stop()
        await task

        assert transport.opens == 2
        assert coordinator.analysis_catchup_exhausted is True

    asyncio.run(scenario())


def test_bootstrap_buffer_orders_analyses_by_commit_id_not_report_id(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        handled: list[int] = []
        items = [
            AnalysisEnvelope(
                analysis_id=1,
                student_id="student-1",
                session_id="session-1",
                report_id=20,
                event="Stop",
                result={"diagnosis": "first commit"},
                timestamp=1.0,
            ).to_dict(),
            AnalysisEnvelope(
                analysis_id=2,
                student_id="student-1",
                session_id="session-1",
                report_id=10,
                event="Stop",
                result={"diagnosis": "second commit"},
                timestamp=2.0,
            ).to_dict(),
        ]

        class Transport(FakeTransport):
            async def get_recent_analyses_async(
                self,
                *,
                after_analysis_id: int,
                limit: int,
            ):
                remaining = [
                    item for item in items if item["analysis_id"] > after_analysis_id
                ]
                return {"items": remaining, "has_more": False}

        coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            Transport(),
            analysis_handler=lambda payload: handled.append(int(payload["report_id"])),
        )
        agent = StudentAgent(coordinator)
        frames: asyncio.Queue[Any] = asyncio.Queue()
        await frames.put(
            AnalysisEnvelope(
                analysis_id=1,
                student_id="student-1",
                session_id="session-1",
                report_id=20,
                event="Stop",
                result={"diagnosis": "first commit"},
                timestamp=1.0,
            ).to_dict()
        )
        await frames.put(
            AnalysisEnvelope(
                analysis_id=2,
                student_id="student-1",
                session_id="session-1",
                report_id=10,
                event="Stop",
                result={"diagnosis": "second commit"},
                timestamp=2.0,
            ).to_dict()
        )

        await agent._drain_bootstrap_frames(frames)

        assert handled == [20, 10]
        assert coordinator.analysis_cursor == 2

    asyncio.run(scenario())


def test_realtime_ws_analysis_uses_authoritative_commit_order_catchup(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        bootstrapped = asyncio.Event()
        rendered = asyncio.Event()
        block = asyncio.Event()
        items = [
            AnalysisEnvelope(
                analysis_id=1,
                student_id="student-1",
                session_id="session-1",
                report_id=20,
                event="Stop",
                result={"diagnosis": "commit one"},
                timestamp=1.0,
            ).to_dict(),
            AnalysisEnvelope(
                analysis_id=2,
                student_id="student-1",
                session_id="session-1",
                report_id=10,
                event="Stop",
                result={"diagnosis": "commit two"},
                timestamp=2.0,
            ).to_dict(),
        ]

        class Socket:
            sent = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def recv(self):
                if not self.sent:
                    await bootstrapped.wait()
                    self.sent = True
                    # The later commit's WS projection arrives first.
                    return json.dumps(items[1])
                await block.wait()
                raise AssertionError("socket unexpectedly resumed")

        class Transport(FakeTransport):
            pulls = 0

            def open_ws(self):
                return Socket()

            async def get_recent_analyses_async(
                self,
                *,
                after_analysis_id: int,
                limit: int,
            ):
                self.pulls += 1
                if self.pulls == 1:
                    bootstrapped.set()
                    return {"items": [], "has_more": False}
                remaining = [
                    item for item in items if item["analysis_id"] > after_analysis_id
                ]
                return {"items": remaining, "has_more": False}

        handled: list[int] = []

        def handle(payload: dict[str, Any]) -> None:
            handled.append(int(payload["report_id"]))
            if len(handled) == 2:
                rendered.set()

        coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            Transport(),
            analysis_handler=handle,
        )
        agent = StudentAgent(coordinator, interval=60)
        task = agent.start()
        await asyncio.wait_for(rendered.wait(), timeout=0.5)
        await agent.stop()
        await task

        assert handled == [20, 10]
        assert coordinator.analysis_cursor == 2

    asyncio.run(scenario())
