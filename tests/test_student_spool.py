from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from copilot.student_core.models import HookEvent
from copilot.student_core.spool import EventSpool, consume_one
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


def test_pending_order_is_deterministic(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path)
    first = spool.enqueue(make_event(session_id="first"), event_id="0002")
    second = spool.enqueue(make_event(session_id="second"), event_id="0001")

    assert [entry.event_id for entry in spool.pending()] == [second, first]


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


def test_post_is_acked_only_after_accepted(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path)
    spool.enqueue(make_event(), event_id="accepted")

    class AcceptedTransport:
        def post_hook(self, event: HookEvent, *, event_id: str = "") -> Accepted:
            return Accepted(status_code=202, body={"accepted": True})

    assert consume_one(spool, AcceptedTransport()) is True
    assert spool.pending() == []


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
