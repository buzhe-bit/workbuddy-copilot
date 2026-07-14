from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from copilot.models import UploadOutcome
from copilot.student_core.models import HookEvent
from copilot.student_core.process_liveness import ProcessIdentity
from copilot.student_core.spool import EventSpool
from copilot.student_core.transcript_jobs import TranscriptUploadQueue
from copilot.student_core.transport import (
    Accepted,
    PermanentTransportError,
    TemporaryNetworkError,
)
from copilot.student_core.coordinator import StudentCoordinator


def event() -> HookEvent:
    return HookEvent(
        event="Stop",
        student_id="student-1",
        session_id="session-1",
        cwd="/workspace",
        transcript_tail="tail",
    )


class FakeTransport:
    student_id = "student-1"

    def __init__(self, result: object = Accepted(202), *, student_id: str = "student-1") -> None:
        self.result = result
        self.student_id = student_id
        self.posts: list[HookEvent] = []
        self.post_event_ids: list[str] = []
        self.sent: list[dict[str, Any]] = []
        self.acked: list[tuple[str, str]] = []

    def post_hook(self, payload: HookEvent, *, event_id: str = "") -> object:
        self.posts.append(payload)
        self.post_event_ids.append(event_id)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    async def send_ws(self, payload: dict[str, Any]) -> Accepted:
        self.sent.append(payload)
        return Accepted(200)

    async def ack_message(self, message_id: str, *, student_id: str) -> Accepted:
        self.acked.append((student_id, message_id))
        return Accepted(200, {"ok": True})


class FakeUploader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def upload(self, *, request_id: str, session_id: str | None) -> UploadOutcome:
        self.calls.append((request_id, session_id))
        return UploadOutcome(1, 1, 1, 0, 0)


class FakeProcessLiveness:
    def __init__(self, *, legacy_state: str = "alive", default: str = "alive") -> None:
        self.legacy_state = legacy_state
        self.default = default

    def probe(self, _identity: ProcessIdentity) -> str:
        return self.default

    def probe_pid(self, _pid: int) -> str:
        return self.legacy_state


def spool_with_identity(
    root: Path,
    owner: str,
    *,
    liveness: FakeProcessLiveness,
    pid: int,
) -> EventSpool:
    return EventSpool(
        root,
        process_identity=ProcessIdentity(pid=pid, started_at=pid * 10, owner_token=owner),
        process_liveness=liveness,
    )


def test_coordinator_acks_only_after_server_accepts(tmp_path: Path) -> None:
    async def scenario() -> None:
        spool = EventSpool(tmp_path)
        spool.enqueue(event(), event_id="event-1")
        transport = FakeTransport(Accepted(202))

        accepted = await StudentCoordinator(spool, transport).flush_spool_once()

        assert accepted == 1
        assert spool.pending() == []
        assert len(transport.posts) == 1
        assert transport.post_event_ids == ["event-1"]

    asyncio.run(scenario())


@pytest.mark.parametrize("ack_failure", [False, OSError("disk busy")])
def test_accepted_report_with_failed_local_ack_releases_claim_for_idempotent_retry(
    tmp_path: Path,
    ack_failure: bool | BaseException,
) -> None:
    async def scenario() -> None:
        spool = EventSpool(tmp_path)
        spool.enqueue(event(), event_id="event-1")
        original_ack = spool.ack

        def fail_ack(event_id: str) -> bool:
            if isinstance(ack_failure, BaseException):
                raise ack_failure
            assert ack_failure is False
            return False

        spool.ack = fail_ack  # type: ignore[method-assign]
        transport = FakeTransport(Accepted(202, {"report_id": 11}))

        accepted = await StudentCoordinator(spool, transport).flush_spool_once()

        assert accepted == 0
        assert [entry.event_id for entry in spool.pending()] == ["event-1"]
        assert transport.post_event_ids == ["event-1"]
        spool.ack = original_ack  # type: ignore[method-assign]

    asyncio.run(scenario())


def test_stop_job_uses_authenticated_transport_identity_after_account_rotation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        spool = EventSpool(tmp_path / "spool")
        spool.enqueue(
            HookEvent(
                event="Stop",
                student_id="stale-student",
                session_id="session-1",
            ),
            event_id="event-rotated",
        )
        queue = TranscriptUploadQueue(tmp_path / "jobs.sqlite3")
        transport = FakeTransport(
            Accepted(202, {"report_id": 17}),
            student_id="current-student",
        )

        accepted = await StudentCoordinator(
            spool,
            transport,
            transcript_queue=queue,
        ).flush_spool_once()

        assert accepted == 1
        assert queue.pending()[0].student_id == "current-student"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    [TemporaryNetworkError("offline"), PermanentTransportError("rejected")],
)
def test_coordinator_keeps_spool_entry_when_post_is_not_accepted(
    tmp_path: Path, failure: BaseException
) -> None:
    async def scenario() -> None:
        spool = EventSpool(tmp_path)
        spool.enqueue(event(), event_id="event-1")
        transport = FakeTransport(failure)

        accepted = await StudentCoordinator(spool, transport).flush_spool_once()

        assert accepted == 0
        assert [entry.event_id for entry in spool.pending()] == ["event-1"]

    asyncio.run(scenario())


def test_spool_delivery_never_overtakes_the_oldest_unconfirmed_event(
    tmp_path: Path,
) -> None:
    class RetryOldestTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.fail_oldest_once = True

        def post_hook(self, payload: HookEvent, *, event_id: str = "") -> object:
            self.posts.append(payload)
            self.post_event_ids.append(event_id)
            if event_id == "z-oldest" and self.fail_oldest_once:
                self.fail_oldest_once = False
                raise TemporaryNetworkError("offline")
            return Accepted(202)

    async def scenario() -> None:
        spool = EventSpool(tmp_path)
        spool.enqueue(event(), event_id="z-oldest")
        spool.enqueue(event(), event_id="a-later")
        transport = RetryOldestTransport()
        coordinator = StudentCoordinator(spool, transport)

        assert await coordinator.flush_spool_once() == 0
        assert transport.post_event_ids == ["z-oldest"]
        assert [entry.event_id for entry in spool.pending()] == [
            "z-oldest",
            "a-later",
        ]

        assert await coordinator.flush_spool_once() == 2
        assert transport.post_event_ids == [
            "z-oldest",
            "z-oldest",
            "a-later",
        ]
        assert spool.pending() == []

    asyncio.run(scenario())


def test_active_claim_on_oldest_event_blocks_later_delivery(tmp_path: Path) -> None:
    async def scenario() -> None:
        first_worker = EventSpool(tmp_path)
        first_worker.enqueue(event(), event_id="z-oldest")
        first_worker.enqueue(event(), event_id="a-later")
        assert first_worker.claim("z-oldest") is True

        second_worker = EventSpool(tmp_path)
        transport = FakeTransport(Accepted(202))
        coordinator = StudentCoordinator(second_worker, transport)

        assert await coordinator.flush_spool_once() == 0
        assert transport.post_event_ids == []
        # The public pending view still hides work actively owned elsewhere.
        assert [entry.event_id for entry in second_worker.pending()] == ["a-later"]

        assert first_worker.release_claim("z-oldest") is True
        assert await coordinator.flush_spool_once() == 2
        assert transport.post_event_ids == ["z-oldest", "a-later"]

    asyncio.run(scenario())


def test_concurrent_enqueue_commit_cannot_publish_a_later_event_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older_spool = EventSpool(tmp_path)
    later_spool = EventSpool(tmp_path)
    delivery_spool = EventSpool(tmp_path)
    older_at_commit = threading.Event()
    release_older = threading.Event()
    later_finished = threading.Event()
    errors: list[BaseException] = []
    real_replace = os.replace

    def paused_replace(source, destination) -> None:
        if Path(destination).name == "z-oldest.json":
            older_at_commit.set()
            assert release_older.wait(1)
        real_replace(source, destination)

    monkeypatch.setattr(
        "copilot.student_core.spool.os.replace",
        paused_replace,
    )

    def enqueue(spool: EventSpool, event_id: str, finished=None) -> None:
        try:
            spool.enqueue(event(), event_id=event_id)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if finished is not None:
                finished.set()

    older = threading.Thread(target=enqueue, args=(older_spool, "z-oldest"))
    later = threading.Thread(
        target=enqueue,
        args=(later_spool, "a-later", later_finished),
    )
    older.start()
    assert older_at_commit.wait(1)
    later.start()
    transport = FakeTransport(Accepted(202))
    coordinator = StudentCoordinator(delivery_spool, transport)
    try:
        # The later writer must remain behind the older writer's atomic commit
        # boundary; no wire-visible event may appear in the gap.
        later_finished.wait(0.1)
        assert asyncio.run(coordinator.flush_spool_once()) == 0
        assert transport.post_event_ids == []
    finally:
        release_older.set()
        older.join(timeout=1)
        later.join(timeout=1)

    assert older.is_alive() is False
    assert later.is_alive() is False
    assert errors == []
    assert asyncio.run(coordinator.flush_spool_once()) == 2
    assert transport.post_event_ids == ["z-oldest", "a-later"]


def test_duplicate_mentor_message_is_handled_and_receipted_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        handled: list[str] = []
        coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            transport,
            message_handler=lambda payload: handled.append(payload["message_id"]),
        )
        payload = {
            "type": "mentor_message",
            "student_id": "student-1",
            "message_id": "message-1",
            "text": "Try a smaller example",
        }

        assert await coordinator.handle_message(payload) is True
        assert await coordinator.handle_message(payload) is False

        assert handled == ["message-1"]
        assert transport.acked == [("student-1", "message-1")]
        assert transport.sent == []

    asyncio.run(scenario())


def test_same_message_id_for_two_students_is_not_cross_deduplicated(tmp_path: Path) -> None:
    async def scenario() -> None:
        seen: set[tuple[str, str]] = set()
        handled: list[tuple[str, str]] = []
        first_coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            FakeTransport(student_id="student-a"),
            message_handler=lambda payload: handled.append(
                (str(payload["student_id"]), str(payload["message_id"]))
            ),
            seen_message_ids=seen,
        )
        second_coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            FakeTransport(student_id="student-b"),
            message_handler=lambda payload: handled.append(
                (str(payload["student_id"]), str(payload["message_id"]))
            ),
            seen_message_ids=seen,
        )

        first = {"type": "mentor_message", "student_id": "student-a", "message_id": "same"}
        second = {"type": "mentor_message", "student_id": "student-b", "message_id": "same"}
        assert await first_coordinator.handle_message(first) is True
        assert await second_coordinator.handle_message(second) is True

        assert handled == [("student-a", "same"), ("student-b", "same")]
        assert seen == {("student-a", "same"), ("student-b", "same")}

    asyncio.run(scenario())


def test_concurrent_duplicate_mentor_message_runs_handler_and_receipt_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()
        calls: list[str] = []

        async def handler(payload: dict[str, Any]) -> None:
            calls.append(str(payload["message_id"]))
            handler_started.set()
            await release_handler.wait()

        coordinator = StudentCoordinator(EventSpool(tmp_path), transport, message_handler=handler)
        payload = {"type": "mentor_message", "student_id": "student-1", "message_id": "race"}
        first = asyncio.create_task(coordinator.handle_message(payload))
        await handler_started.wait()
        second = asyncio.create_task(coordinator.handle_message(payload))
        await asyncio.sleep(0)
        release_handler.set()

        assert await first is True
        assert await second is False
        assert calls == ["race"]
        assert transport.acked == [("student-1", "race")]

    asyncio.run(scenario())


def test_failed_mentor_message_handler_releases_inflight_key_for_retry(tmp_path: Path) -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        attempts = 0

        async def handler(_: dict[str, Any]) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary UI failure")

        coordinator = StudentCoordinator(EventSpool(tmp_path), transport, message_handler=handler)
        payload = {"type": "mentor_message", "student_id": "student-1", "message_id": "retry"}

        assert await coordinator.handle_message(payload) is False
        assert await coordinator.handle_message(payload) is True
        assert attempts == 2
        assert transport.acked == [("student-1", "retry")]

    asyncio.run(scenario())


def test_failed_receipt_retries_backlog_without_rendering_duplicate(tmp_path: Path) -> None:
    class FlakyReceiptTransport(FakeTransport):
        def __init__(self, backlog: list[dict[str, str]]) -> None:
            super().__init__()
            self.backlog = backlog
            self.receipt_attempts = 0

        def get_pending_messages(self) -> list[dict[str, str]]:
            return list(self.backlog)

        async def ack_message(self, message_id: str, *, student_id: str) -> Accepted:
            self.receipt_attempts += 1
            self.acked.append((student_id, message_id))
            if self.receipt_attempts == 1:
                raise TemporaryNetworkError("temporary receipt failure")
            return Accepted(200, {"ok": True})

    async def scenario() -> None:
        payload = {
            "type": "mentor_message",
            "student_id": "student-1",
            "message_id": "retry-receipt",
            "text": "Render exactly once",
        }
        rendered: list[str] = []
        transport = FlakyReceiptTransport([payload])
        coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            transport,
            message_handler=lambda item: rendered.append(str(item["message_id"])),
        )

        assert await coordinator.handle_message(payload) is False
        assert rendered == ["retry-receipt"]
        assert transport.acked == [("student-1", "retry-receipt")]

        assert await coordinator.pull_pending_messages() == 1
        assert rendered == ["retry-receipt"]
        assert transport.acked == [
            ("student-1", "retry-receipt"),
            ("student-1", "retry-receipt"),
        ]
        assert ("student-1", "retry-receipt") in coordinator.seen_message_ids

    asyncio.run(scenario())


def test_receipt_ledger_skips_handler_after_agent_restart_and_retries_only_ack(tmp_path: Path) -> None:
    class ReceiptTransport(FakeTransport):
        def __init__(self, *, fail_ack: bool) -> None:
            super().__init__()
            self.fail_ack = fail_ack

        def get_pending_messages(self) -> list[dict[str, str]]:
            return [{
                "type": "mentor_message",
                "student_id": "student-1",
                "message_id": "restart-receipt",
                "text": "Do not render twice",
            }]

        async def ack_message(self, message_id: str, *, student_id: str) -> Accepted:
            self.acked.append((student_id, message_id))
            if self.fail_ack:
                raise TemporaryNetworkError("receipt endpoint unavailable")
            return Accepted(200, {"ok": True})

    async def scenario() -> None:
        payload = ReceiptTransport(fail_ack=True).get_pending_messages()[0]
        first_rendered: list[str] = []
        first_transport = ReceiptTransport(fail_ack=True)
        first = StudentCoordinator(
            EventSpool(tmp_path),
            first_transport,
            message_handler=lambda item: first_rendered.append(str(item["message_id"])),
        )
        assert await first.handle_message(payload) is False
        assert first_rendered == ["restart-receipt"]

        restarted_rendered: list[str] = []
        restarted_transport = ReceiptTransport(fail_ack=False)
        restarted = StudentCoordinator(
            EventSpool(tmp_path),
            restarted_transport,
            message_handler=lambda item: restarted_rendered.append(str(item["message_id"])),
        )
        assert await restarted.pull_pending_messages() == 1
        assert restarted_rendered == []
        assert restarted_transport.acked == [("student-1", "restart-receipt")]
        assert restarted.spool.receipt_ledger.status("student-1", "restart-receipt") == "acked"

    asyncio.run(scenario())


def test_message_without_student_scope_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        transport = FakeTransport(student_id="")
        coordinator = StudentCoordinator(EventSpool(tmp_path), transport)

        assert await coordinator.handle_message({"type": "mentor_message", "message_id": "unscoped"}) is False
        assert transport.sent == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "mentor_message", "message_id": "missing-scope"},
        {"type": "mentor_message", "student_id": "student-2", "message_id": "wrong-scope"},
    ],
)
def test_message_requires_exact_transport_student_scope(tmp_path: Path, payload: dict[str, str]) -> None:
    async def scenario() -> None:
        transport = FakeTransport(student_id="student-1")
        coordinator = StudentCoordinator(EventSpool(tmp_path), transport)

        assert await coordinator.handle_message(payload) is False
        assert transport.acked == []

    asyncio.run(scenario())


def test_upload_command_runs_injectable_handler_once_for_duplicate_request(tmp_path: Path) -> None:
    async def scenario() -> None:
        uploader = FakeUploader()
        coordinator = StudentCoordinator(EventSpool(tmp_path), FakeTransport(), uploader)
        command = {
            "type": "mentor_command",
            "student_id": "student-1",
            "command": "upload_conversations",
            "request_id": "request-1",
            "session_id": "session-1",
        }

        assert await coordinator.handle_command(command) is True
        assert await coordinator.handle_command(command) is False
        assert uploader.calls == [("request-1", "session-1")]

    asyncio.run(scenario())


def test_legacy_sync_uploader_runs_off_the_websocket_event_loop(tmp_path: Path) -> None:
    class BlockingUploader:
        def __init__(self) -> None:
            self.release = threading.Event()

        def upload(self, **_kwargs) -> UploadOutcome:
            self.release.wait(timeout=0.3)
            return UploadOutcome(1, 1, 1, 0, 0)

    async def scenario() -> None:
        uploader = BlockingUploader()
        coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            FakeTransport(),
            uploader,
        )
        started_at = time.monotonic()
        task = asyncio.create_task(coordinator.handle_command({
            "type": "mentor_command",
            "student_id": "student-1",
            "command": "upload_conversations",
            "request_id": "sync-uploader",
            "session_id": "session-1",
        }))

        await asyncio.sleep(0)
        loop_delay = time.monotonic() - started_at
        uploader.release.set()

        assert loop_delay < 0.1
        assert await task is True

    asyncio.run(scenario())


def test_unknown_command_is_ignored_without_invoking_uploader(tmp_path: Path) -> None:
    async def scenario() -> None:
        uploader = FakeUploader()
        coordinator = StudentCoordinator(EventSpool(tmp_path), FakeTransport(), uploader)

        assert await coordinator.handle_command({"type": "mentor_command", "command": "nope"}) is False
        assert uploader.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "mentor_command", "command": "upload_conversations", "request_id": "missing"},
        {
            "type": "mentor_command",
            "student_id": "student-2",
            "command": "upload_conversations",
            "request_id": "wrong",
        },
    ],
)
def test_upload_command_requires_exact_transport_student_scope(
    tmp_path: Path, payload: dict[str, str]
) -> None:
    async def scenario() -> None:
        uploader = FakeUploader()
        coordinator = StudentCoordinator(EventSpool(tmp_path), FakeTransport(), uploader)

        assert await coordinator.handle_command(payload) is False
        assert uploader.calls == []

    asyncio.run(scenario())


def test_completed_upload_command_is_deduplicated_after_coordinator_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        command = {
            "type": "mentor_command",
            "student_id": "student-1",
            "command": "upload_conversations",
            "request_id": "request-after-restart",
        }
        first_uploader = FakeUploader()
        first = StudentCoordinator(EventSpool(tmp_path), FakeTransport(), first_uploader)
        assert await first.handle_command(command) is True

        restarted_uploader = FakeUploader()
        restarted = StudentCoordinator(EventSpool(tmp_path), FakeTransport(), restarted_uploader)
        assert await restarted.handle_command(command) is False
        assert first_uploader.calls == [("request-after-restart", None)]
        assert restarted_uploader.calls == []

    asyncio.run(scenario())


def test_failed_upload_command_releases_durable_claim_for_retry(tmp_path: Path) -> None:
    class FlakyUploader:
        def __init__(self) -> None:
            self.calls = 0

        async def upload(self, *, request_id: str, session_id: str | None) -> UploadOutcome:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary platform failure")
            return UploadOutcome(1, 1, 1, 0, 0)

    async def scenario() -> None:
        command = {
            "type": "mentor_command",
            "student_id": "student-1",
            "command": "upload_conversations",
            "request_id": "retry-durable-claim",
        }
        uploader = FlakyUploader()
        coordinator = StudentCoordinator(EventSpool(tmp_path), FakeTransport(), uploader)

        assert await coordinator.handle_command(command) is False
        assert await coordinator.handle_command(command) is True
        assert uploader.calls == 2

    asyncio.run(scenario())


def test_crash_stale_upload_claim_is_recovered_after_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        command = {
            "type": "mentor_command",
            "student_id": "student-1",
            "command": "upload_conversations",
            "request_id": "recover-crashed-upload",
        }
        liveness = FakeProcessLiveness(legacy_state="dead")
        crashed = StudentCoordinator(
            spool_with_identity(tmp_path, "crashed", liveness=liveness, pid=101),
            FakeTransport(),
            FakeUploader(),
        )
        claim_path, _done_path = crashed._request_paths(command["request_id"])
        claim_path.write_text("424242 1\n", encoding="ascii")
        restarted_uploader = FakeUploader()
        restarted = StudentCoordinator(
            spool_with_identity(tmp_path, "restarted", liveness=liveness, pid=102),
            FakeTransport(),
            restarted_uploader,
        )

        assert await restarted.handle_command(command) is True
        assert restarted_uploader.calls == [("recover-crashed-upload", None)]
        assert not claim_path.exists()

    asyncio.run(scenario())


def test_live_upload_claim_stays_exclusive_after_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        command = {
            "type": "mentor_command",
            "student_id": "student-1",
            "command": "upload_conversations",
            "request_id": "live-upload",
        }
        liveness = FakeProcessLiveness(legacy_state="alive")
        original = StudentCoordinator(
            spool_with_identity(tmp_path, "original", liveness=liveness, pid=103),
            FakeTransport(),
            FakeUploader(),
        )
        claim_path, _done_path = original._request_paths(command["request_id"])
        claim_path.write_text(f"{os.getpid()} 1\n", encoding="ascii")

        restarted_uploader = FakeUploader()
        restarted = StudentCoordinator(
            spool_with_identity(tmp_path, "restarted", liveness=liveness, pid=104),
            FakeTransport(),
            restarted_uploader,
        )

        assert await restarted.handle_command(command) is False
        assert restarted_uploader.calls == []

    asyncio.run(scenario())


def test_expired_upload_claim_fails_closed_when_process_liveness_is_unavailable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        command = {
            "type": "mentor_command",
            "student_id": "student-1",
            "command": "upload_conversations",
            "request_id": "recover-expired-upload",
        }
        liveness = FakeProcessLiveness(legacy_state="unknown")
        original = StudentCoordinator(
            spool_with_identity(tmp_path, "original", liveness=liveness, pid=105),
            FakeTransport(),
            FakeUploader(),
        )
        claim_path, _done_path = original._request_paths(command["request_id"])
        claim_path.write_text("424243 1\n", encoding="ascii")
        uploader = FakeUploader()
        restarted = StudentCoordinator(
            spool_with_identity(tmp_path, "restarted", liveness=liveness, pid=106),
            FakeTransport(),
            uploader,
            stale_claim_after=0.001,
        )

        assert await restarted.handle_command(command) is False
        assert uploader.calls == []
        assert restarted.command_claim_health() == {
            "status": "degraded",
            "unknown_claims": [
                {
                    "claim_id": restarted._request_marker_stem(command["request_id"]),
                    "reason": "liveness_unknown",
                }
            ],
        }

    asyncio.run(scenario())


def test_concurrent_stale_claim_recovery_allows_only_one_new_owner(tmp_path: Path) -> None:
    liveness = FakeProcessLiveness(legacy_state="dead", default="alive")
    first = StudentCoordinator(
        spool_with_identity(tmp_path, "first", liveness=liveness, pid=107),
        FakeTransport(),
        FakeUploader(),
    )
    second = StudentCoordinator(
        spool_with_identity(tmp_path, "second", liveness=liveness, pid=108),
        FakeTransport(),
        FakeUploader(),
    )
    claim_path, _done_path = first._request_paths("concurrent-stale")
    claim_path.write_text("424244 1\n", encoding="ascii")

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda coordinator: coordinator._claim_upload_request("concurrent-stale"), [first, second]))

    assert sum(claim is not None for claim in claims) == 1
    assert claim_path.exists()


def test_claim_mutex_does_not_leave_a_persistent_gate_after_owner_closes(tmp_path: Path) -> None:
    first = StudentCoordinator(EventSpool(tmp_path), FakeTransport(), FakeUploader())
    second = StudentCoordinator(EventSpool(tmp_path), FakeTransport(), FakeUploader())

    held = first._open_claim_lock()
    assert held is not None
    assert second._open_claim_lock() is None
    held.close()

    recovered = second._open_claim_lock()
    assert recovered is not None
    recovered.close()


def test_claim_mutex_rejects_symlinked_lock_database(tmp_path: Path) -> None:
    coordinator = StudentCoordinator(EventSpool(tmp_path), FakeTransport(), FakeUploader())
    lock_db = coordinator._command_state_dir / ".claim-locks.sqlite3"
    target = tmp_path / "outside-lock.sqlite3"
    target.write_text("not a database", encoding="ascii")
    lock_db.symlink_to(target)

    with pytest.raises(ValueError, match="lock"):
        coordinator._open_claim_lock()


def test_failed_claim_marker_fsync_cleans_only_its_claim_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        command = {
            "type": "mentor_command",
            "student_id": "student-1",
            "command": "upload_conversations",
            "request_id": "retry-after-marker-fsync-failure",
        }
        uploader = FakeUploader()
        coordinator = StudentCoordinator(EventSpool(tmp_path), FakeTransport(), uploader)
        claim_path, _done_path = coordinator._request_paths(command["request_id"])
        real_fsync = os.fsync
        calls = 0

        def fail_once(fd: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("simulated fsync failure")
            real_fsync(fd)

        monkeypatch.setattr("copilot.student_core.coordinator.os.fsync", fail_once)

        assert await coordinator.handle_command(command) is False
        assert not claim_path.exists()
        assert await coordinator.handle_command(command) is True
        assert uploader.calls == [("retry-after-marker-fsync-failure", None)]

    asyncio.run(scenario())


def test_failed_marker_cleanup_never_unlinks_an_inode_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "marker"

    def fail_fsync(_fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr("copilot.student_core.coordinator.os.fsync", fail_fsync)
    monkeypatch.setattr("copilot.student_core.coordinator.os.path.samestat", lambda *_args: False)

    with pytest.raises(OSError, match="fsync"):
        StudentCoordinator._write_marker(marker)

    assert marker.exists()


def test_completion_commit_failure_leaves_no_durable_completion_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        command = {
            "type": "mentor_command",
            "student_id": "student-1",
            "command": "upload_conversations",
            "request_id": "retry-after-completion-commit-failure",
        }
        uploader = FakeUploader()
        coordinator = StudentCoordinator(EventSpool(tmp_path), FakeTransport(), uploader)
        commits = 0

        def fail_first_completion_commit(_self: StudentCoordinator, connection: sqlite3.Connection) -> None:
            nonlocal commits
            commits += 1
            if commits == 1:
                raise sqlite3.OperationalError("simulated power loss before commit")
            connection.commit()

        monkeypatch.setattr(
            StudentCoordinator,
            "_commit_completion",
            fail_first_completion_commit,
            raising=False,
        )

        assert await coordinator.handle_command(command) is False
        assert await coordinator.handle_command(command) is True
        assert uploader.calls == [
            ("retry-after-completion-commit-failure", None),
            ("retry-after-completion-commit-failure", None),
        ]

    asyncio.run(scenario())


def test_legacy_done_file_is_not_completion_authority_after_sqlite_migration(tmp_path: Path) -> None:
    async def scenario() -> None:
        command = {
            "type": "mentor_command",
            "student_id": "student-1",
            "command": "upload_conversations",
            "request_id": "legacy-done-is-not-authority",
        }
        first = StudentCoordinator(EventSpool(tmp_path), FakeTransport(), FakeUploader())
        _claim_path, legacy_done = first._request_paths(command["request_id"])
        legacy_done.write_text("done\n", encoding="ascii")

        uploader = FakeUploader()
        restarted = StudentCoordinator(EventSpool(tmp_path), FakeTransport(), uploader)

        assert await restarted.handle_command(command) is True
        assert uploader.calls == [("legacy-done-is-not-authority", None)]

    asyncio.run(scenario())


def test_concurrent_upload_commands_complete_once_in_shared_sqlite_ledger(tmp_path: Path) -> None:
    class BlockingUploader:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def upload(self, *, request_id: str, session_id: str | None) -> UploadOutcome:
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return UploadOutcome(1, 1, 1, 0, 0)

    async def scenario() -> None:
        command = {
            "type": "mentor_command",
            "student_id": "student-1",
            "command": "upload_conversations",
            "request_id": "concurrent-completion-ledger",
        }
        first_uploader = BlockingUploader()
        first = StudentCoordinator(EventSpool(tmp_path), FakeTransport(), first_uploader)
        second_uploader = FakeUploader()
        second = StudentCoordinator(EventSpool(tmp_path), FakeTransport(), second_uploader)

        first_task = asyncio.create_task(first.handle_command(command))
        await first_uploader.started.wait()
        assert await second.handle_command(command) is False
        first_uploader.release.set()
        assert await first_task is True

        restarted = StudentCoordinator(EventSpool(tmp_path), FakeTransport(), FakeUploader())
        assert await restarted.handle_command(command) is False
        assert first_uploader.calls == 1
        assert second_uploader.calls == []

    asyncio.run(scenario())


def test_reconnect_backoff_uses_injected_sleeper_and_caps() -> None:
    async def scenario() -> None:
        waits: list[float] = []
        coordinator = StudentCoordinator(
            EventSpool(Path("/tmp") / "student-coordinator-test-spool"),
            FakeTransport(),
            sleeper=lambda delay: waits.append(delay),
            reconnect_initial=1.0,
            reconnect_max=4.0,
        )

        assert await coordinator.reconnect_once(lambda: False) is False
        assert await coordinator.reconnect_once(lambda: False) is False
        assert await coordinator.reconnect_once(lambda: False) is False
        assert waits == [1.0, 2.0, 4.0]

    asyncio.run(scenario())


def test_pending_receipt_cursor_reaches_later_rendered_message_after_failed_first_page(tmp_path: Path) -> None:
    class CursorTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.cursors: list[int] = []

        def get_pending_messages(self, *, after_id: int) -> list[dict[str, str | int]]:
            self.cursors.append(after_id)
            if after_id == 0:
                return [
                    {
                        "type": "mentor_message",
                        "student_id": "student-1",
                        "message_id": f"unknown-{index}",
                        "id": index,
                    }
                    for index in range(1, 65)
                ]
            if after_id == 64:
                return [{
                    "type": "mentor_message",
                    "student_id": "student-1",
                    "message_id": "rendered-65",
                    "id": 65,
                }]
            return []

    async def scenario() -> None:
        transport = CursorTransport()
        handled: list[str] = []

        async def handler(payload: dict[str, str | int]) -> None:
            message_id = str(payload["message_id"])
            if message_id.startswith("unknown-"):
                raise RuntimeError("unrendered legacy card")
            handled.append(message_id)

        coordinator = StudentCoordinator(
            EventSpool(tmp_path),
            transport,
            message_handler=handler,
        )

        assert await coordinator.pull_pending_messages() == 1
        assert transport.cursors == [0, 64]
        assert handled == ["rendered-65"]
        assert transport.acked == [("student-1", "rendered-65")]

    asyncio.run(scenario())


def test_headless_empty_pending_page_does_not_ack_without_a_renderer(tmp_path: Path) -> None:
    class EmptyBacklogTransport(FakeTransport):
        def get_pending_messages(self, *, after_id: int) -> list[dict[str, str]]:
            assert after_id == 0
            return []

    async def scenario() -> None:
        transport = EmptyBacklogTransport()
        spool = EventSpool(tmp_path)
        spool.receipt_ledger.mark_rendered("student-1", "already-delivered-server-side")
        coordinator = StudentCoordinator(spool, transport)

        assert await coordinator.pull_pending_messages() == 0
        assert transport.acked == []
        assert spool.receipt_ledger.status("student-1", "already-delivered-server-side") == "rendered"

    asyncio.run(scenario())


def test_pending_transport_internal_type_error_is_not_retried_as_legacy_signature(tmp_path: Path) -> None:
    class BrokenCursorTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def get_pending_messages(self, *, after_id: int | None = None) -> list[dict[str, str]]:
            self.calls += 1
            raise TypeError("adapter parsing bug")

    async def scenario() -> None:
        transport = BrokenCursorTransport()
        coordinator = StudentCoordinator(EventSpool(tmp_path), transport)

        assert await coordinator.pull_pending_messages() == 0
        assert transport.calls == 1

    asyncio.run(scenario())
