"""Hook contract: bounded local spool, no network, and graceful degradation."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
from unittest.mock import MagicMock

import pytest

from copilot.hook import (
    DEFAULT_TAIL_BYTES,
    _load_config,
    _read_transcript_tail,
    _spool_dir,
    _write_spool_event,
    main,
)


def _set_stdin(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    monkeypatch.setattr("sys.stdin", MagicMock(read=lambda: json.dumps(value)))


def _read_spool(spool_dir: Path) -> dict:
    files = sorted(spool_dir.glob("*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


def _write_config(path: Path, **values: object) -> None:
    path.write_text(json.dumps(values), encoding="utf-8")


def test_config_spool_env_overrides_student_spool_dir(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_spool = tmp_path / "config-spool"
    env_spool = tmp_path / "env-spool"
    _write_config(
        config_path,
        student_id="config-student",
        student={"spool_dir": str(config_spool)},
    )
    monkeypatch.setenv("COPILOT_CONFIG", str(config_path))
    monkeypatch.setenv("COPILOT_SPOOL_DIR", str(env_spool))

    cfg = _load_config()

    assert Path(_spool_dir(cfg)) == env_spool


def test_config_spool_falls_back_to_student_config(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_spool = tmp_path / "config-spool"
    _write_config(
        config_path,
        student_id="config-student",
        student={"spool_dir": str(config_spool)},
    )
    monkeypatch.setenv("COPILOT_CONFIG", str(config_path))
    monkeypatch.delenv("COPILOT_SPOOL_DIR", raising=False)

    assert Path(_spool_dir(_load_config())) == config_spool


def test_student_id_environment_overrides_config(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path, student_id="config-student")
    monkeypatch.setenv("COPILOT_CONFIG", str(config_path))
    monkeypatch.setenv("COPILOT_STUDENT_ID", "env-student")

    assert _load_config()["student_id"] == "env-student"


def test_reads_only_bounded_tail_bytes(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes((b"a" * (DEFAULT_TAIL_BYTES + 1024)) + b"TAIL")

    result = _read_transcript_tail(str(transcript), max_bytes=DEFAULT_TAIL_BYTES)

    assert len(result.encode("utf-8")) <= DEFAULT_TAIL_BYTES
    assert result.endswith("TAIL")


def test_stop_hook_writes_bounded_spool_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spool_dir = tmp_path / "spool"
    transcript = tmp_path / "huge.jsonl"
    transcript.write_bytes((b"x" * (DEFAULT_TAIL_BYTES + 8192)) + b"last-bytes")
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        student_id="student-1",
        student={"spool_dir": str(tmp_path / "wrong-spool")},
        hook={"transcript_tail_bytes": DEFAULT_TAIL_BYTES},
    )
    monkeypatch.setenv("COPILOT_CONFIG", str(config_path))
    monkeypatch.setenv("COPILOT_SPOOL_DIR", str(spool_dir))

    # A network call would violate the hook boundary.  The implementation is
    # deliberately not allowed to import urllib or call an opener at all.
    monkeypatch.setattr("socket.socket", lambda *a, **k: pytest.fail("network forbidden"))
    _set_stdin(
        monkeypatch,
        {
            "session_id": "session-1",
            "hook_event_name": "Stop",
            "prompt": "continue",
            "transcript_path": str(transcript),
            "cwd": "/workspace",
        },
    )

    assert main() == 0
    entry = _read_spool(spool_dir)
    payload = entry["payload"]
    assert entry["event_id"]
    assert payload["student_id"] == "student-1"
    assert payload["event"] == "Stop"
    assert len(payload["transcript_tail"].encode("utf-8")) <= DEFAULT_TAIL_BYTES
    assert payload["transcript_tail"].endswith("last-bytes")
    assert "transcript_full" not in payload


def test_locked_transcript_still_spools_stop_with_empty_tail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    spool_dir = tmp_path / "spool"
    _write_config(config_path, student_id="student-locked")
    monkeypatch.setenv("COPILOT_CONFIG", str(config_path))
    monkeypatch.setenv("COPILOT_SPOOL_DIR", str(spool_dir))
    monkeypatch.setattr(
        "copilot.hook._read_transcript_tail",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("sharing violation")),
    )
    _set_stdin(
        monkeypatch,
        {
            "hook_event_name": "Stop",
            "session_id": "locked-session",
            "transcript_path": str(tmp_path / "locked.jsonl"),
        },
    )

    assert main() == 0
    entry = _read_spool(spool_dir)
    assert entry["payload"]["event"] == "Stop"
    assert entry["payload"]["session_id"] == "locked-session"
    assert entry["payload"]["transcript_tail"] == ""


def test_hook_event_matches_student_core_spool_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.json"
    spool_dir = tmp_path / "spool"
    _write_config(config_path, student_id="student-1")
    monkeypatch.setenv("COPILOT_CONFIG", str(config_path))
    monkeypatch.setenv("COPILOT_SPOOL_DIR", str(spool_dir))
    _set_stdin(monkeypatch, {"hook_event_name": "UserPromptSubmit", "session_id": "s1"})

    assert main() == 0

    entry = _read_spool(spool_dir)
    from copilot.student_core.models import SpoolEntry

    restored = SpoolEntry.from_dict(entry)
    assert restored.event_id == entry["event_id"]
    assert restored.payload.session_id == "s1"


def test_hook_spool_fifo_survives_equal_and_rolling_back_clock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    timestamps = iter((100, 100, 50))
    event_ids = iter(("z-first", "y-second", "a-third"))
    monkeypatch.setattr("copilot.hook.time.time_ns", lambda: next(timestamps))
    monkeypatch.setattr(
        "copilot.hook.uuid.uuid4",
        lambda: type("Uuid", (), {"hex": next(event_ids)})(),
    )
    spool_dir = tmp_path / "spool"
    for session_id in ("first", "second", "third"):
        _write_spool_event(
            str(spool_dir),
            {"event": "Stop", "student_id": "student-1", "session_id": session_id},
        )

    from copilot.student_core.spool import EventSpool

    pending = EventSpool(spool_dir).pending()
    assert [entry.event_id for entry in pending] == [
        "z-first",
        "y-second",
        "a-third",
    ]
    orders = [entry.enqueued_at_ns for entry in pending]
    assert orders == [orders[0], orders[0] + 1, orders[0] + 2]


def test_hook_high_watermark_avoids_rescanning_event_backlog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spool_dir = tmp_path / "spool"
    _write_spool_event(
        str(spool_dir),
        {"event": "Stop", "student_id": "student-1", "session_id": "first"},
    )
    assert len(list(spool_dir.glob(".copilot-enqueue-high-*.mark"))) == 1

    real_open = open

    def reject_backlog_read(path, *args, **kwargs):
        mode = kwargs.get("mode", args[0] if args else "r")
        if str(path).endswith(".json") and "r" in str(mode):
            raise AssertionError("hook enqueue order lookup rescanned event backlog")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", reject_backlog_read)
    _write_spool_event(
        str(spool_dir),
        {"event": "Stop", "student_id": "student-1", "session_id": "second"},
    )


def test_hook_legacy_backlog_never_opens_event_payloads_for_order_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    (spool_dir / ".copilot-enqueue-used").mkdir()
    legacy = spool_dir / "legacy.json"
    legacy.write_bytes(b"{" + (b"x" * (256 * 1024)))
    future_mtime = spool_dir.stat().st_mtime_ns + 10_000_000_000
    __import__("os").utime(legacy, ns=(future_mtime, future_mtime))
    monkeypatch.setattr("copilot.hook.time.time_ns", lambda: 1)
    real_open = open

    def reject_legacy_read(path, *args, **kwargs):
        mode = kwargs.get("mode", args[0] if args else "r")
        if Path(path) == legacy and "r" in str(mode):
            raise AssertionError("hook parsed a legacy event payload")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", reject_legacy_read)
    _write_spool_event(
        str(spool_dir),
        {"event": "Stop", "student_id": "student-1", "session_id": "new"},
    )

    committed = [
        path for path in spool_dir.glob("*.json") if path.name != legacy.name
    ]
    assert len(committed) == 1
    row = json.loads(committed[0].read_text(encoding="utf-8"))
    assert row["enqueued_at_ns"] > future_mtime


def test_stale_hook_candidate_cannot_reuse_core_committed_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from copilot.student_core.models import HookEvent
    from copilot.student_core.spool import EventSpool

    spool_dir = tmp_path / "spool"
    spool = EventSpool(spool_dir)
    __import__("os").utime(spool_dir, ns=(100, 100))
    monkeypatch.setattr("copilot.hook.time.time_ns", lambda: 100)
    monkeypatch.setattr(
        "copilot.hook.uuid.uuid4",
        lambda: type("Uuid", (), {"hex": "a-hook-second"})(),
    )
    stale_before_link = threading.Event()
    release_stale = threading.Event()
    errors: list[BaseException] = []
    real_link = __import__("os").link

    def pause_hook_link(source, destination) -> None:
        if (
            threading.current_thread().name == "stale-hook-writer"
            and Path(destination).name.startswith(".copilot-enqueue-order-")
            and not stale_before_link.is_set()
        ):
            stale_before_link.set()
            assert release_stale.wait(1)
        real_link(source, destination)

    monkeypatch.setattr("copilot.hook.os.link", pause_hook_link)

    def write_stale_hook() -> None:
        try:
            _write_spool_event(
                str(spool_dir),
                {"event": "Stop", "student_id": "student-1", "session_id": "second"},
            )
        except BaseException as exc:
            errors.append(exc)

    stale = threading.Thread(target=write_stale_hook, name="stale-hook-writer")
    stale.start()
    assert stale_before_link.wait(1)
    spool.enqueue(
        HookEvent(event="Stop", student_id="student-1", session_id="first"),
        event_id="z-core-first",
    )
    release_stale.set()
    stale.join(timeout=1)

    assert stale.is_alive() is False
    assert errors == []
    pending = spool.pending()
    assert [entry.event_id for entry in pending] == ["z-core-first", "a-hook-second"]
    assert [entry.enqueued_at_ns for entry in pending] == [100, 101]


def test_hook_high_watermark_failure_leaves_event_for_agent_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from copilot.student_core.spool import EventSpool

    spool_dir = tmp_path / "spool"
    monkeypatch.setattr(
        "copilot.hook.uuid.uuid4",
        lambda: type("Uuid", (), {"hex": "recoverable-hook-event"})(),
    )
    monkeypatch.setattr(
        "copilot.hook._record_enqueue_high_watermark",
        lambda *_args: (_ for _ in ()).throw(OSError("high marker unavailable")),
    )

    with pytest.raises(OSError, match="high marker unavailable"):
        _write_spool_event(
            str(spool_dir),
            {"event": "Stop", "student_id": "student-1", "session_id": "recover"},
        )

    assert (spool_dir / "recoverable-hook-event.json").is_file()
    assert len(list(spool_dir.glob(".copilot-enqueue-order-*.lock"))) == 1
    assert [entry.event_id for entry in EventSpool(spool_dir).pending()] == [
        "recoverable-hook-event"
    ]
    assert list(spool_dir.glob(".copilot-enqueue-order-*.lock")) == []


def test_hook_contention_stays_bounded_and_uses_durable_order_barrier(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from copilot.student_core.models import HookEvent
    from copilot.student_core.spool import EventSpool

    spool_dir = tmp_path / "spool"
    older_spool = EventSpool(spool_dir)
    older_at_commit = threading.Event()
    release_older = threading.Event()
    later_done = threading.Event()
    errors: list[BaseException] = []
    real_replace = __import__("os").replace

    def paused_replace(source, destination) -> None:
        if Path(destination).name == "z-oldest.json":
            older_at_commit.set()
            assert release_older.wait(1)
        real_replace(source, destination)

    monkeypatch.setattr(
        "copilot.student_core.spool.os.replace",
        paused_replace,
    )
    monkeypatch.setattr(
        "copilot.hook.uuid.uuid4",
        lambda: type("Uuid", (), {"hex": "a-later"})(),
    )

    def write_older() -> None:
        try:
            older_spool.enqueue(
                HookEvent(event="Stop", student_id="student-1", session_id="old"),
                event_id="z-oldest",
            )
        except BaseException as exc:
            errors.append(exc)

    def write_later_hook() -> None:
        try:
            _write_spool_event(
                str(spool_dir),
                {"event": "Stop", "student_id": "student-1", "session_id": "new"},
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            later_done.set()

    older = threading.Thread(target=write_older)
    later = threading.Thread(target=write_later_hook)
    older.start()
    assert older_at_commit.wait(1)
    later.start()
    try:
        # The hook does not wait on the slow writer, but its committed event is
        # hidden from delivery by the older writer's crash-released barrier.
        assert later_done.wait(0.5)
        assert EventSpool(spool_dir).pending() == []
    finally:
        release_older.set()
        older.join(timeout=1)
        later.join(timeout=1)

    assert errors == []
    assert [entry.event_id for entry in EventSpool(spool_dir).pending()] == [
        "z-oldest",
        "a-later",
    ]


def test_hook_fsyncs_spool_directory_after_atomic_replace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    synced: list[str] = []
    monkeypatch.setattr(
        "copilot.hook._fsync_directory",
        lambda path: synced.append(str(path)),
        raising=False,
    )
    spool_dir = tmp_path / "spool"

    _write_spool_event(
        str(spool_dir),
        {"event": "Stop", "student_id": "student-1", "session_id": "session-1"},
    )

    # Persist the used-order directory, locked reservation, exact tombstone,
    # complete event, and high watermark. POSIX also fsyncs reservation unlink;
    # Windows may defer that cleanup until the recovery pass closes the handle.
    used_orders = str(spool_dir / ".copilot-enqueue-used")
    assert synced[:5] == [
        str(spool_dir),
        str(spool_dir),
        used_orders,
        str(spool_dir),
        str(spool_dir),
    ]
    assert synced[5:] in ([], [str(spool_dir)])


def test_spool_never_contains_local_transcript_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.json"
    spool_dir = tmp_path / "spool"
    transcript = tmp_path / "private" / "transcript.jsonl"
    transcript.parent.mkdir()
    transcript.write_text("tail", encoding="utf-8")
    _write_config(config_path, student_id="student-1")
    monkeypatch.setenv("COPILOT_CONFIG", str(config_path))
    monkeypatch.setenv("COPILOT_SPOOL_DIR", str(spool_dir))
    _set_stdin(
        monkeypatch,
        {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(transcript)},
    )

    assert main() == 0

    raw = next(spool_dir.glob("*.json")).read_text(encoding="utf-8")
    payload = json.loads(raw)["payload"]
    assert payload["transcript_path"] == ""
    assert str(transcript) not in raw


def test_invalid_utf8_tail_stays_bounded_after_json_serialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.json"
    spool_dir = tmp_path / "spool"
    transcript = tmp_path / "invalid.bin"
    transcript.write_bytes((b"\xff" * (DEFAULT_TAIL_BYTES + 64)) + b"END")
    _write_config(config_path, student_id="student-1")
    monkeypatch.setenv("COPILOT_CONFIG", str(config_path))
    monkeypatch.setenv("COPILOT_SPOOL_DIR", str(spool_dir))
    _set_stdin(
        monkeypatch,
        {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(transcript)},
    )

    assert main() == 0

    payload = json.loads(next(spool_dir.glob("*.json")).read_text(encoding="utf-8"))["payload"]
    assert len(payload["transcript_tail"].encode("utf-8")) <= DEFAULT_TAIL_BYTES
    assert payload["transcript_tail"].endswith("END")


def test_escaped_tail_stays_bounded_in_serialized_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.json"
    spool_dir = tmp_path / "spool"
    transcript = tmp_path / "escaped.jsonl"
    transcript.write_bytes((b'"\\' * (DEFAULT_TAIL_BYTES // 2 + 64)) + b"END")
    _write_config(config_path, student_id="student-1")
    monkeypatch.setenv("COPILOT_CONFIG", str(config_path))
    monkeypatch.setenv("COPILOT_SPOOL_DIR", str(spool_dir))
    _set_stdin(
        monkeypatch,
        {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(transcript)},
    )

    assert main() == 0

    raw = next(spool_dir.glob("*.json")).read_text(encoding="utf-8")
    payload = json.loads(raw)["payload"]
    serialized_tail = json.dumps(
        payload["transcript_tail"], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert len(serialized_tail) <= DEFAULT_TAIL_BYTES
    assert payload["transcript_tail"].endswith("END")


def test_invalid_stdin_always_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", MagicMock(read=lambda: "not-json"))

    assert main() == 0


def test_missing_event_and_missing_student_id_degrade_without_spool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spool_dir = tmp_path / "spool"
    config_path = tmp_path / "config.json"
    _write_config(config_path, student_id="")
    monkeypatch.setenv("COPILOT_CONFIG", str(config_path))
    monkeypatch.setenv("COPILOT_SPOOL_DIR", str(spool_dir))
    _set_stdin(monkeypatch, {"session_id": "s1"})

    assert main() == 0
    assert not spool_dir.exists()


def test_spool_write_failure_always_returns_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path, student_id="student-1")
    monkeypatch.setenv("COPILOT_CONFIG", str(config_path))
    monkeypatch.setenv("COPILOT_SPOOL_DIR", str(tmp_path / "spool"))
    _set_stdin(monkeypatch, {"hook_event_name": "Stop", "session_id": "s1"})
    monkeypatch.setattr("copilot.hook._write_spool_event", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    assert main() == 0
