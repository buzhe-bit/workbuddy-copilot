"""Headless resident runtime for the platform-neutral Student Core."""
from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import Mapping
from typing import Any, Callable

from .coordinator import StudentCoordinator

log = logging.getLogger("copilot.student_core.agent")


async def _default_sleeper(delay: float) -> None:
    # Keep asyncio lazy so importing Student Core remains platform-neutral.
    import asyncio

    await asyncio.sleep(delay)


class StudentAgent:
    """Run durable spool delivery and one long-lived student WebSocket.

    The agent contains no WorkBuddy/UI imports.  A platform adapter can supply
    an uploader to the coordinator, while this runtime keeps HTTP delivery and
    WebSocket reception alive across macOS and Windows.
    """

    def __init__(
        self,
        coordinator: StudentCoordinator,
        *,
        sleeper: Callable[[float], Any] | None = None,
        interval: float = 1.0,
        stop_timeout: float = 1.0,
        analysis_poll_interval: float = 30.0,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if interval < 0:
            raise ValueError("interval must be non-negative")
        if stop_timeout <= 0:
            raise ValueError("stop_timeout must be positive")
        if analysis_poll_interval <= 0:
            raise ValueError("analysis_poll_interval must be positive")
        self.coordinator = coordinator
        self._sleeper = sleeper or _default_sleeper
        self.interval = float(interval)
        self.stop_timeout = float(stop_timeout)
        self.analysis_poll_interval = float(analysis_poll_interval)
        self._monotonic = monotonic or time.monotonic
        # The WS bootstrap already performs an immediate catch-up. Start the
        # safety poll one interval later to avoid racing that authoritative
        # bootstrap on process start.
        self._next_analysis_poll_at = (
            self._monotonic() + self.analysis_poll_interval
        )
        self._analysis_recovery_active = False
        self._stopping = False
        self._task: Any | None = None

    @property
    def stopped(self) -> bool:
        return self._stopping

    async def one_cycle(self) -> int:
        """Flush spool and retry any persisted mentor-message receipts."""
        try:
            accepted = await self.coordinator.flush_spool_once()
        except Exception as exc:
            # Filesystem/spool failures must not tear down the independent WS
            # loop; the next interval can retry once the local disk recovers.
            log.warning("student spool cycle failed type=%s", type(exc).__name__)
            accepted = 0
        await self._pull_pending_messages()
        await self._poll_analysis_if_due()
        return accepted

    async def _pull_pending_messages(self) -> int:
        pull_method = getattr(self.coordinator, "pull_pending_messages", None)
        if not callable(pull_method):
            return 0
        try:
            result = pull_method()
            resolved = await result if inspect.isawaitable(result) else result
            return int(resolved) if isinstance(resolved, int) else 0
        except Exception as exc:
            log.warning("student message receipt recovery failed type=%s", type(exc).__name__)
            return 0

    async def _pull_analysis_catchup(self) -> int:
        if self._analysis_recovery_active:
            return 0
        pull_method = getattr(self.coordinator, "pull_analysis_catchup", None)
        if not callable(pull_method):
            return 0
        self._analysis_recovery_active = True
        try:
            result = pull_method()
            resolved = await result if inspect.isawaitable(result) else result
            return int(resolved) if isinstance(resolved, int) else 0
        except Exception as exc:
            log.warning("student analysis recovery failed type=%s", type(exc).__name__)
            return 0
        finally:
            self._analysis_recovery_active = False

    async def _poll_analysis_if_due(self) -> int:
        """Bounded REST safety net when a nominally-open WS loses wakeups."""

        now = self._monotonic()
        if now < self._next_analysis_poll_at:
            return 0
        # Advance before network I/O so a slow/failing request cannot create a
        # hot loop across the 50-student pilot cohort.
        self._next_analysis_poll_at = now + self.analysis_poll_interval
        return await self._pull_analysis_catchup()

    async def run(self) -> None:
        """Run spool and persistent WebSocket loops until stopped or cancelled."""
        import asyncio

        if self._stopping:
            return
        spool_task = asyncio.create_task(self._spool_loop())
        ws_task = asyncio.create_task(self._ws_loop())
        try:
            await asyncio.gather(spool_task, ws_task)
        finally:
            self._stopping = True
            for task in (spool_task, ws_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(spool_task, ws_task, return_exceptions=True)

    async def _spool_loop(self) -> None:
        while not self._stopping:
            await self.one_cycle()
            if self._stopping:
                return
            result = self._sleeper(self.interval)
            if inspect.isawaitable(result):
                await result

    async def _ws_loop(self) -> None:
        """Receive server frames on one connection; reconnect with core backoff."""
        import asyncio

        while not self._stopping:
            try:
                transport = getattr(self.coordinator, "transport", None)
                connector = getattr(transport, "open_ws", None)
                if connector is None:
                    # Unit-only coordinators can exercise the spool loop
                    # without inventing a platform/network implementation.
                    return
                async with connector() as socket:
                    self.coordinator.reset_reconnect_backoff()
                    frames: asyncio.Queue[Any] = asyncio.Queue(maxsize=512)
                    reader = asyncio.create_task(self._read_socket(socket, frames))
                    try:
                        await self._pull_pending_messages()
                        # The reader is already active, so analyses committed
                        # during pagination are buffered instead of lost.
                        await self._pull_analysis_catchup()
                        if not bool(
                            getattr(
                                self.coordinator,
                                "analysis_catchup_exhausted",
                                True,
                            )
                        ):
                            # Never enter realtime behind an incomplete durable
                            # backlog. Closing the socket makes the normal
                            # reconnect path retry from the advanced commit
                            # cursor while server-side REST remains authoritative.
                            raise RuntimeError("analysis catch-up incomplete")
                        await self._drain_bootstrap_frames(frames)
                        while not self._stopping:
                            frame = await frames.get()
                            if isinstance(frame, BaseException):
                                raise frame
                            if (
                                isinstance(frame, Mapping)
                                and frame.get("type") in {"analysis", "analysis_result"}
                            ):
                                # WS is a low-latency wake-up only. The durable
                                # REST stream owns commit ordering, so a later
                                # report cannot advance past an earlier analysis
                                # whose post-commit projection/publish is delayed.
                                await self._pull_analysis_catchup()
                                if not bool(
                                    getattr(
                                        self.coordinator,
                                        "analysis_catchup_exhausted",
                                        True,
                                    )
                                ):
                                    raise RuntimeError("analysis catch-up incomplete")
                                continue
                            await self.coordinator.handle_event(frame)
                    finally:
                        if not reader.done():
                            reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("student WS disconnected type=%s", type(exc).__name__)
                if not self._stopping:
                    await self.coordinator.reconnect_once(lambda: False)

    async def _read_socket(self, socket: Any, frames: Any) -> None:
        import asyncio

        try:
            while not self._stopping:
                raw = await socket.recv()
                payload = self._decode_event(raw)
                if payload is not None:
                    await frames.put(payload)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await frames.put(exc)

    async def _drain_bootstrap_frames(self, frames: Any) -> None:
        """Apply buffered analyses in durable commit order after catch-up."""
        import asyncio

        buffered: list[Any] = []
        while True:
            try:
                buffered.append(frames.get_nowait())
            except asyncio.QueueEmpty:
                break
        failures = [item for item in buffered if isinstance(item, BaseException)]
        if failures:
            raise failures[0]
        analyses: list[Mapping[str, Any]] = []
        other: list[Mapping[str, Any]] = []
        for item in buffered:
            if not isinstance(item, Mapping):
                continue
            if item.get("type") in {"analysis", "analysis_result"}:
                analyses.append(item)
            else:
                other.append(item)

        for payload in other:
            await self.coordinator.handle_event(payload)
        if analyses:
            await self._pull_analysis_catchup()
            if not bool(
                getattr(self.coordinator, "analysis_catchup_exhausted", True)
            ):
                raise RuntimeError("analysis catch-up incomplete")

    @staticmethod
    def _decode_event(raw: Any) -> Mapping[str, Any] | None:
        if not isinstance(raw, (str, bytes, bytearray)):
            return None
        try:
            text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
            payload = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, Mapping) else None

    async def _background_run(self) -> None:
        import asyncio

        try:
            await self.run()
        except asyncio.CancelledError:
            # ``stop`` deliberately cancels blocked socket/sleeper work. The
            # public task resolves normally once cleanup is complete.
            return

    def start(self) -> Any:
        """Schedule the loops and return a task; calling twice is idempotent."""
        if self._task is None or self._task.done():
            self._stopping = False
            import asyncio

            self._task = asyncio.create_task(self._background_run())
        return self._task

    async def stop(self) -> None:
        """Cancel blocked I/O and wait only for the bounded cleanup interval."""
        import asyncio

        self._stopping = True
        task = self._task
        if task is None or task is asyncio.current_task() or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=self.stop_timeout)
        except asyncio.TimeoutError:
            log.warning("student agent stop timed out")
        except asyncio.CancelledError:
            return

    async def __aenter__(self) -> "StudentAgent":
        self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()
