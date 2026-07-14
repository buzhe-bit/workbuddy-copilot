from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from copilot.student_core.models import HookEvent
from copilot.student_core.spool import (
    EventSpool,
    _try_lock_order_reservation,
    _unlock_order_reservation,
    consume_one,
)
from copilot.student_core.transport import Accepted, TemporaryNetworkError


def make_event(**overrides) -> HookEvent:
    values = {
        "event": "Stop",
        "student_id": "student-1",
        "session_id": "session-1",
        "cwd": "/workspace",
        "transcript_tail": "hello",
        "transcript_path": "/tmp/transcript.jsonl",
    }
    values.update(overrides)
    return HookEvent(**values)


def test_hook_event_serializes_as_typed_contract() -> None:
    event = make_event()
    restored = HookEvent.from_dict(event.to_dict())

    assert restored == event
    assert restored.to_dict()["session_id"] == "session-1"


def test_spool_entry_survives_until_ack(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path)
    event_id = spool.enqueue(make_event())

    pending = spool.pending()
    assert [entry.event_id for entry in pending] == [event_id]
    assert pending[0].payload == make_event()

    spool.ack(event_id)
    assert spool.pending() == []


def test_receipt_ledger_persists_rendered_and_acked_state_across_spool_restart(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path)

    assert spool.receipt_ledger.status("student-1", "message-1") is None
    spool.receipt_ledger.mark_rendered("student-1", "message-1")

    restarted = EventSpool(tmp_path)
    assert restarted.receipt_ledger.status("student-1", "message-1") == "rendered"
    restarted.receipt_ledger.mark_acked("student-1", "message-1")
    assert EventSpool(tmp_path).receipt_ledger.status("student-1", "message-1") == "acked"


def test_receipt_ledger_bounds_acked_history_without_pruning_unacknowledged_rendered(tmp_path: Path) -> None:
    ledger = EventSpool(tmp_path).receipt_ledger
    ledger.mark_rendered("student-1", "must-not-prune")
    for index in range(300):
        ledger.mark_acked("student-1", f"acked-{index}")

    with sqlite3.connect(ledger.path) as connection:
        acked_count = connection.execute(
            "SELECT COUNT(*) FROM mentor_message_receipts WHERE student_id = ? AND state = 'acked'",
            ("student-1",),
        ).fetchone()[0]

    assert acked_count <= 256
    assert ledger.status("student-1", "must-not-prune") == "rendered"


def test_pending_order_follows_enqueue_sequence_not_random_event_id(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path)
    first = spool.enqueue(make_event(session_id="first"), event_id="0002")
    second = spool.enqueue(make_event(session_id="second"), event_id="0001")

    assert [entry.event_id for entry in spool.pending()] == [first, second]


def test_enqueue_uses_atomic_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spool = EventSpool(tmp_path)
    replacements: list[tuple[str, str]] = []
    real_replace = __import__("os").replace

    def record_replace(source: str, destination: str) -> None:
        replacements.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr("copilot.student_core.spool.os.replace", record_replace)
    event_id = spool.enqueue(make_event(), event_id="atomic")

    assert len(replacements) == 1
    source, destination = replacements[0]
    assert source != destination
    assert str(destination).endswith(f"{event_id}.json")
    assert Path(destination).exists()


def test_enqueue_does_not_expose_an_empty_final_path_before_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = EventSpool(tmp_path)
    replace_entered = threading.Event()
    allow_replace = threading.Event()
    errors: list[BaseException] = []
    real_replace = __import__("os").replace
    destination = tmp_path / "race.json"

    def paused_replace(source: str, target: str) -> None:
        if Path(target) == destination:
            replace_entered.set()
            assert allow_replace.wait(1)
        real_replace(source, target)

    monkeypatch.setattr("copilot.student_core.spool.os.replace", paused_replace)

    def enqueue() -> None:
        try:
            spool.enqueue(make_event(), event_id="race")
        except BaseException as exc:
            errors.append(exc)

    writer = threading.Thread(target=enqueue)
    writer.start()
    assert replace_entered.wait(1)
    try:
        assert destination.exists() is False
    finally:
        allow_replace.set()
        writer.join(timeout=1)
    assert writer.is_alive() is False
    assert errors == []


def test_failed_temp_cleanup_never_leaves_the_order_lock_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = EventSpool(tmp_path)
    real_unlink = Path.unlink

    def fail_event_temp_cleanup(self: Path, *args, **kwargs):
        if self.name.startswith(".cleanup-failure.") and self.suffix == ".tmp":
            raise PermissionError("antivirus sharing violation")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_event_temp_cleanup)
    monkeypatch.setattr(
        "copilot.student_core.spool.os.replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        spool.enqueue(make_event(), event_id="cleanup-failure")

    # The crash-recovery pass can acquire and remove the marker immediately;
    # a cleanup exception must not strand a live lock in the resident agent.
    spool.pending()
    assert list(tmp_path.glob(".copilot-enqueue-order-*.lock")) == []


def test_pending_never_quarantines_a_valid_replacement_of_the_opened_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = EventSpool(tmp_path)
    path = tmp_path / "race.json"
    path.write_text("", encoding="utf-8")
    opened_old_inode = threading.Event()
    replacement_committed = threading.Event()
    real_path_open = Path.open

    def paused_open(self: Path, *args, **kwargs):
        handle = real_path_open(self, *args, **kwargs)
        if self == path:
            opened_old_inode.set()
            assert replacement_committed.wait(1)
        return handle

    monkeypatch.setattr(Path, "open", paused_open)
    observed: list = []
    reader = threading.Thread(target=lambda: observed.extend(spool.pending()))
    reader.start()
    assert opened_old_inode.wait(1)

    replacement = tmp_path / ".race.valid.tmp"
    replacement.write_text(
        json.dumps({
            "event_id": "race",
            "enqueued_at_ns": 1,
            "payload": make_event().to_dict(),
        }),
        encoding="utf-8",
    )
    __import__("os").replace(replacement, path)
    replacement_committed.set()
    reader.join(timeout=1)

    assert reader.is_alive() is False
    assert observed == []
    assert path.exists() is True
    assert list((tmp_path / "quarantine").glob("race-*.json")) == []
    assert [entry.event_id for entry in spool.pending()] == ["race"]


def test_enqueue_fsyncs_spool_directory_after_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synced: list[Path] = []
    monkeypatch.setattr(
        "copilot.student_core.spool._fsync_directory",
        lambda path: synced.append(Path(path)),
        raising=False,
    )
    spool = EventSpool(tmp_path)

    spool.enqueue(make_event(), event_id="durable-enqueue")

    # Persist the used-order directory, locked reservation, exact tombstone,
    # complete event, and high watermark. POSIX also fsyncs reservation unlink;
    # Windows may defer that cleanup until the recovery pass closes the handle.
    used_orders = tmp_path / ".copilot-enqueue-used"
    assert synced[:5] == [tmp_path, tmp_path, used_orders, tmp_path, tmp_path]
    assert synced[5:] in ([], [tmp_path])


def test_ack_fsyncs_spool_directory_after_successful_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = EventSpool(tmp_path)
    spool.enqueue(make_event(), event_id="durable-ack")
    synced: list[Path] = []
    monkeypatch.setattr(
        "copilot.student_core.spool._fsync_directory",
        lambda path: synced.append(Path(path)),
        raising=False,
    )

    assert spool.ack("durable-ack") is True
    assert synced == [tmp_path]


def test_bad_json_is_quarantined_and_not_dropped(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path)
    bad = tmp_path / "broken.json"
    bad.write_text("{not-json", encoding="utf-8")

    assert spool.pending() == []
    quarantine = tmp_path / "quarantine"
    quarantined = list(quarantine.glob("broken*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{not-json"


@pytest.mark.parametrize("event_id", ["../escape", "a/b", "", ".", "..", "a\\b"])
def test_invalid_event_id_is_rejected(tmp_path: Path, event_id: str) -> None:
    spool = EventSpool(tmp_path)

    with pytest.raises(ValueError):
        spool.enqueue(make_event(), event_id=event_id)
    with pytest.raises(ValueError):
        spool.ack(event_id)


def test_malformed_entry_is_quarantined(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path)
    (tmp_path / "bad-entry.json").write_text(
        json.dumps({"event_id": "bad-entry", "payload": {"event": ""}}),
        encoding="utf-8",
    )

    assert spool.pending() == []
    assert list((tmp_path / "quarantine").glob("bad-entry*.json"))


@pytest.mark.parametrize("filename", [".json", "a.b.json", "bad space.json"])
def test_malformed_spool_filename_is_quarantined(tmp_path: Path, filename: str) -> None:
    spool = EventSpool(tmp_path)
    (tmp_path / filename).write_text(
        json.dumps({"event_id": "safe", "payload": {"event": "Stop"}}),
        encoding="utf-8",
    )

    assert spool.pending() == []
    assert list((tmp_path / "quarantine").glob("*.json"))


def test_failed_post_does_not_ack(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path)
    spool.enqueue(make_event(), event_id="keep")

    class OfflineTransport:
        def post_hook(self, event: HookEvent, *, event_id: str = "") -> Accepted:
            raise TemporaryNetworkError("offline")

    assert consume_one(spool, OfflineTransport()) is False
    assert [entry.event_id for entry in spool.pending()] == ["keep"]


def test_failed_post_releases_claim_for_later_retry(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path)
    spool.enqueue(make_event(), event_id="retry")

    class RetryTransport:
        def __init__(self) -> None:
            self.attempts = 0

        def post_hook(self, event: HookEvent, *, event_id: str = "") -> Accepted:
            self.attempts += 1
            if self.attempts == 1:
                raise TemporaryNetworkError("offline")
            return Accepted(status_code=202)

    transport = RetryTransport()
    assert consume_one(spool, transport) is False
    assert consume_one(spool, transport) is True
    assert spool.pending() == []


def test_consume_one_does_not_overtake_an_actively_claimed_oldest_event(
    tmp_path: Path,
) -> None:
    first_worker = EventSpool(tmp_path)
    first_worker.enqueue(make_event(), event_id="z-oldest")
    first_worker.enqueue(make_event(), event_id="a-later")
    assert first_worker.claim("z-oldest") is True
    second_worker = EventSpool(tmp_path)
    posted: list[str] = []

    class Transport:
        def post_hook(self, event: HookEvent, *, event_id: str = "") -> Accepted:
            posted.append(event_id)
            return Accepted(status_code=202)

    assert consume_one(second_worker, Transport()) is False
    assert posted == []


def test_post_is_acked_only_after_accepted(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path)
    spool.enqueue(make_event(), event_id="accepted")

    class AcceptedTransport:
        def post_hook(self, event: HookEvent, *, event_id: str = "") -> Accepted:
            return Accepted(status_code=202, body={"accepted": True})

    assert consume_one(spool, AcceptedTransport()) is True
    assert spool.pending() == []


def test_pending_preserves_durable_enqueue_order_across_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    timestamps = iter((100, 200))
    monkeypatch.setattr(
        "copilot.student_core.spool.time.time_ns",
        lambda: next(timestamps),
    )
    spool = EventSpool(tmp_path)
    spool.enqueue(make_event(), event_id="z-first")
    spool.enqueue(make_event(), event_id="a-second")

    restarted = EventSpool(tmp_path)

    assert [entry.event_id for entry in restarted.pending()] == [
        "z-first",
        "a-second",
    ]


def test_pending_fifo_survives_equal_and_rolling_back_clock_across_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter((100, 100, 50))
    monkeypatch.setattr(
        "copilot.student_core.spool.time.time_ns",
        lambda: next(timestamps),
    )
    spool = EventSpool(tmp_path)
    spool.enqueue(make_event(session_id="first"), event_id="z-first")
    spool.enqueue(make_event(session_id="second"), event_id="y-second")
    spool.enqueue(make_event(session_id="third"), event_id="a-third")

    restarted = EventSpool(tmp_path)
    pending = restarted.pending()

    assert [entry.event_id for entry in pending] == [
        "z-first",
        "y-second",
        "a-third",
    ]
    assert [entry.enqueued_at_ns for entry in pending] == [100, 101, 102]


def test_stale_candidate_cannot_reuse_an_already_committed_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = EventSpool(tmp_path)
    monkeypatch.setattr("copilot.student_core.spool.time.time_ns", lambda: 100)
    stale_before_link = threading.Event()
    release_stale = threading.Event()
    errors: list[BaseException] = []
    real_link = __import__("os").link

    def pause_stale_link(source, destination) -> None:
        if (
            threading.current_thread().name == "stale-order-writer"
            and Path(destination).name.startswith(".copilot-enqueue-order-")
            and not stale_before_link.is_set()
        ):
            stale_before_link.set()
            assert release_stale.wait(1)
        real_link(source, destination)

    monkeypatch.setattr("copilot.student_core.spool.os.link", pause_stale_link)

    def write_stale_candidate() -> None:
        try:
            spool.enqueue(make_event(session_id="second"), event_id="a-second")
        except BaseException as exc:
            errors.append(exc)

    stale = threading.Thread(target=write_stale_candidate, name="stale-order-writer")
    stale.start()
    assert stale_before_link.wait(1)
    spool.enqueue(make_event(session_id="first"), event_id="z-first")
    release_stale.set()
    stale.join(timeout=1)

    assert stale.is_alive() is False
    assert errors == []
    pending = spool.pending()
    assert [entry.event_id for entry in pending] == ["z-first", "a-second"]
    assert [entry.enqueued_at_ns for entry in pending] == [100, 101]


def test_order_marker_is_published_from_an_already_locked_owner_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = EventSpool(tmp_path)
    observed_lock_available: list[bool] = []
    real_link = __import__("os").link

    def inspect_then_link(source, destination) -> None:
        probe = __import__("os").open(source, __import__("os").O_RDWR)
        acquired = _try_lock_order_reservation(probe)
        observed_lock_available.append(acquired)
        if acquired:
            _unlock_order_reservation(probe)
        __import__("os").close(probe)
        real_link(source, destination)

    monkeypatch.setattr(
        "copilot.student_core.spool.os.link",
        inspect_then_link,
    )
    spool.enqueue(make_event(), event_id="later")

    # Both the active reservation and its exact used-order tombstone are hard
    # links published from the already locked owner inode.
    assert observed_lock_available == [False, False]
    assert [entry.event_id for entry in spool.pending()] == ["later"]


def test_persisted_order_high_watermark_avoids_rescanning_event_backlog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = EventSpool(tmp_path)
    spool.enqueue(make_event(), event_id="first")
    high_markers = list(tmp_path.glob(".copilot-enqueue-high-*.mark"))
    assert len(high_markers) == 1

    real_open = Path.open

    def reject_backlog_read(self: Path, *args, **kwargs):
        if self.suffix == ".json":
            raise AssertionError("enqueue order lookup rescanned event backlog")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_backlog_read)
    spool.enqueue(make_event(), event_id="second")


def test_crash_released_order_marker_is_promoted_before_reaping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = EventSpool(tmp_path)
    real_record = spool._record_enqueue_high_watermark
    monkeypatch.setattr(
        spool,
        "_record_enqueue_high_watermark",
        lambda _order: (_ for _ in ()).throw(OSError("high marker unavailable")),
    )

    with pytest.raises(OSError, match="high marker unavailable"):
        spool.enqueue(make_event(), event_id="durable-before-high")

    event_path = tmp_path / "durable-before-high.json"
    reservations = list(tmp_path.glob(".copilot-enqueue-order-*.lock"))
    assert event_path.is_file()
    assert len(reservations) == 1

    monkeypatch.setattr(spool, "_record_enqueue_high_watermark", real_record)
    assert [entry.event_id for entry in spool.pending()] == ["durable-before-high"]
    assert list(tmp_path.glob(".copilot-enqueue-order-*.lock")) == []
    assert len(list(tmp_path.glob(".copilot-enqueue-high-*.mark"))) == 1


def test_crash_recovery_removes_the_matching_orphan_owner_inode(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path)
    _order, _reservation, fd, _owner = spool._reserve_enqueue_order()
    _unlock_order_reservation(fd)
    __import__("os").close(fd)

    assert len(list(tmp_path.glob(".copilot-enqueue-order-owner-*.tmp"))) == 1
    spool.pending()

    assert list(tmp_path.glob(".copilot-enqueue-order-*.lock")) == []
    assert list(tmp_path.glob(".copilot-enqueue-order-owner-*.tmp")) == []


def test_recovery_cleans_owner_before_post_close_reservation_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = EventSpool(tmp_path)
    _order, reservations, fd, _owner = spool._reserve_enqueue_order()
    reservation = reservations[-1]
    _unlock_order_reservation(fd)
    __import__("os").close(fd)
    cleanup_observations: list[bool] = []
    monkeypatch.setattr(spool, "_unlink_owned_reservation", lambda *_args: False)
    monkeypatch.setattr(
        spool,
        "_cleanup_owner_links",
        lambda _stat: cleanup_observations.append(reservation.exists()),
    )

    spool.pending()

    assert cleanup_observations
    assert all(cleanup_observations)
    assert reservation.exists() is False


def test_consume_one_keeps_accepted_event_retryable_when_ack_is_unconfirmed(
    tmp_path: Path,
) -> None:
    spool = EventSpool(tmp_path)
    spool.enqueue(make_event(), event_id="accepted-unacked")
    spool.ack = lambda _event_id: False  # type: ignore[method-assign]

    class AcceptedTransport:
        def post_hook(self, event: HookEvent, *, event_id: str = "") -> Accepted:
            return Accepted(status_code=202, body={"accepted": True})

    assert consume_one(spool, AcceptedTransport()) is False
    assert [entry.event_id for entry in spool.pending()] == ["accepted-unacked"]


def test_consume_one_forwards_spool_event_id(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path)
    spool.enqueue(make_event(), event_id="durable-event")
    received_event_ids: list[str] = []

    class AcceptedTransport:
        def post_hook(
            self, event: HookEvent, *, event_id: str = "",
        ) -> Accepted:
            received_event_ids.append(event_id)
            return Accepted(status_code=202)

    assert consume_one(spool, AcceptedTransport()) is True
    assert received_event_ids == ["durable-event"]


def test_concurrent_enqueue_same_id_has_one_winner(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path)

    def enqueue_once():
        try:
            return spool.enqueue(make_event(), event_id="same")
        except Exception as exc:  # assert the exact race outcome below
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(enqueue_once) for _ in range(2)]
        results = [future.result() for future in futures]

    assert results.count("same") == 1
    assert sum(isinstance(result, FileExistsError) for result in results) == 1


def test_concurrent_consume_claims_event_once(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path)
    spool.enqueue(make_event(), event_id="once")
    started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    class SlowAcceptedTransport:
        def post_hook(self, event: HookEvent, *, event_id: str = "") -> Accepted:
            nonlocal calls
            with calls_lock:
                calls += 1
                if calls == 2:
                    second_started.set()
            started.set()
            release.wait(timeout=1)
            return Accepted(status_code=202)

    transport = SlowAcceptedTransport()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(consume_one, spool, transport)
        assert started.wait(timeout=2)
        second = pool.submit(consume_one, spool, transport)
        second_started.wait(timeout=0.25)
        release.set()
        second_result = second.result(timeout=2)
        first_result = first.result(timeout=2)

    assert calls == 1
    assert sorted([first_result, second_result]) == [False, True]
    assert spool.pending() == []


def test_pending_does_not_follow_symlink_outside_spool(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps({"event_id": "outside", "payload": {"event": "Stop"}}),
        encoding="utf-8",
    )
    spool_dir = tmp_path / "spool"
    spool = EventSpool(spool_dir)
    (spool_dir / "outside.json").symlink_to(outside)

    assert spool.pending() == []
    assert outside.exists()
    assert list((spool_dir / "quarantine").iterdir())


@pytest.mark.parametrize("which", ["root", "quarantine"])
def test_spool_rejects_symlinked_storage_directories(tmp_path: Path, which: str) -> None:
    target = tmp_path / "target"
    target.mkdir()
    if which == "root":
        root = tmp_path / "spool"
        root.symlink_to(target, target_is_directory=True)
    else:
        root = tmp_path / "spool"
        root.mkdir()
        (root / "quarantine").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError):
        EventSpool(root)
