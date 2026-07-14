#!/usr/bin/env python3
"""WorkBuddy hook: bounded, local, fire-and-forget event capture.

This file intentionally has no dependency on the ``copilot`` package.  It is
copied/linked into WorkBuddy's hook environment and must remain usable with a
plain Python standard library.  Network delivery is the Student Core agent's
responsibility; this process only writes one small JSON envelope atomically.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import uuid


DEFAULT_TAIL_BYTES = 256 * 1024
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_ORDER_RESERVATION_PREFIX = ".copilot-enqueue-order-"
_ORDER_RESERVATION_SUFFIX = ".lock"
_ORDER_OWNER_PREFIX = ".copilot-enqueue-order-owner-"
_ORDER_OWNER_SUFFIX = ".tmp"
_ORDER_HIGH_PREFIX = ".copilot-enqueue-high-"
_ORDER_HIGH_SUFFIX = ".mark"
_ORDER_USED_DIRECTORY = ".copilot-enqueue-used"
_ORDER_USED_SUFFIX = ".used"
DEFAULT_CONFIG = {
    "service": {"host": "COPILOT_SERVER_HOST", "port": 8765},
    "hook": {"transcript_tail_bytes": DEFAULT_TAIL_BYTES},
}


def _script_config_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _load_config() -> dict:
    """Load config and apply environment overrides without exposing secrets."""
    cfg_path = os.environ.get("COPILOT_CONFIG") or _script_config_path()
    cfg: dict = {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
            if isinstance(loaded, dict):
                cfg = loaded
    except FileNotFoundError:
        print("[copilot hook] config not found; using defaults", file=sys.stderr)
    except Exception as exc:
        print(
            f"[copilot hook] config load failed ({type(exc).__name__}); using defaults",
            file=sys.stderr,
        )

    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in cfg.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value

    # Keep these overrides in the config contract for the installer/agent.  A
    # hook never sends them over the network (or writes them to the spool).
    if os.environ.get("COPILOT_STUDENT_ID"):
        merged["student_id"] = os.environ["COPILOT_STUDENT_ID"]
    if os.environ.get("COPILOT_STUDENT_TOKEN"):
        merged.setdefault("auth", {})["student_token"] = os.environ["COPILOT_STUDENT_TOKEN"]
    if os.environ.get("COPILOT_TOKEN"):
        merged["token"] = os.environ["COPILOT_TOKEN"]
    if os.environ.get("COPILOT_SERVER_URL"):
        merged.setdefault("service", {})["public_base_url"] = os.environ["COPILOT_SERVER_URL"]
    return merged


def _spool_dir(cfg: dict) -> str:
    """Resolve spool path: env, config.student.spool_dir, then local spool/."""
    configured = os.environ.get("COPILOT_SPOOL_DIR")
    if not configured:
        student = cfg.get("student", {})
        if isinstance(student, dict):
            configured = student.get("spool_dir")
    if not configured:
        configured = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spool")
    return os.path.abspath(os.path.expanduser(str(configured)))


def _tail_size(cfg: dict) -> int:
    hook_cfg = cfg.get("hook", {})
    raw = hook_cfg.get("transcript_tail_bytes") if isinstance(hook_cfg, dict) else None
    if raw is None:
        raw = cfg.get("transcript_tail_bytes", DEFAULT_TAIL_BYTES)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_TAIL_BYTES
    # Keep a hard upper bound even if a stale/hostile config asks for more.
    return max(0, min(value, DEFAULT_TAIL_BYTES))


def _read_transcript_tail(path: str, max_bytes: int = DEFAULT_TAIL_BYTES) -> str:
    """Read at most ``max_bytes`` from the end, preserving raw-byte bounds."""
    if not isinstance(path, str) or not path or max_bytes <= 0:
        return ""
    max_bytes = min(int(max_bytes), DEFAULT_TAIL_BYTES)
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes), os.SEEK_SET)
        # Ignore an incomplete/invalid leading UTF-8 sequence rather than
        # expanding each bad byte to a three-byte replacement character.  This
        # keeps the serialized text within the raw-byte read bound and retains
        # the newest valid suffix.
        text = handle.read(max_bytes).decode("utf-8", errors="ignore")
    return _fit_serialized_tail(text, max_bytes)


def _serialized_text_size(value: str) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _fit_serialized_tail(text: str, budget: int) -> str:
    """Keep the newest suffix whose JSON string representation fits budget."""
    if budget <= 0 or not text:
        return ""
    if _serialized_text_size(text) <= budget:
        return text
    # Binary search avoids repeatedly trimming a large invalid/escaped tail.
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[-middle:]
        if _serialized_text_size(candidate) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[-low:] if low else ""


def _valid_event_id(event_id: str) -> bool:
    return isinstance(event_id, str) and _EVENT_ID.fullmatch(event_id) is not None


def _fsync_directory(directory: str) -> None:
    """Best-effort durability barrier for the atomic directory entry change."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _try_lock_order_reservation(fd: int) -> bool:
    try:
        if os.name == "nt":
            msvcrt = __import__("msvcrt")
            if os.fstat(fd).st_size == 0:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl = __import__("fcntl")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        return False
    return True


def _unlock_order_reservation(fd: int) -> None:
    try:
        if os.name == "nt":
            msvcrt = __import__("msvcrt")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl = __import__("fcntl")
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _highest_persisted_enqueue_order(spool_dir: str) -> int:
    """Read EventSpool-compatible rows and outstanding order reservations."""
    highest = 0
    try:
        with os.scandir(spool_dir) as iterator:
            entries = list(iterator)
    except OSError:
        return highest
    has_valid_high = False
    for entry in entries:
        name = entry.name
        if name.startswith(_ORDER_HIGH_PREFIX) and name.endswith(_ORDER_HIGH_SUFFIX):
            raw_order = name[len(_ORDER_HIGH_PREFIX) : -len(_ORDER_HIGH_SUFFIX)]
            if raw_order.isdigit():
                has_valid_high = True
                highest = max(highest, int(raw_order))
            continue
        if name.startswith(_ORDER_RESERVATION_PREFIX) and name.endswith(
            _ORDER_RESERVATION_SUFFIX
        ):
            raw_order = name[
                len(_ORDER_RESERVATION_PREFIX) : -len(_ORDER_RESERVATION_SUFFIX)
            ]
            if raw_order.isdigit():
                highest = max(highest, int(raw_order))
    # The WorkBuddy Hook has a hard two-second process budget. On first upgrade
    # it must never parse a potentially multi-gigabyte legacy backlog. Legacy
    # rows had no explicit order, so scan only DirEntry metadata (never their
    # 256KB payloads) until the resident agent creates a high marker.
    if not has_valid_high:
        try:
            highest = max(highest, os.stat(spool_dir).st_mtime_ns)
        except OSError:
            pass
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                highest = max(
                    highest,
                    entry.stat(follow_symlinks=False).st_mtime_ns,
                )
            except OSError:
                continue
    return highest


def _record_enqueue_high_watermark(spool_dir: str, order: int) -> None:
    marker = os.path.join(
        spool_dir,
        f"{_ORDER_HIGH_PREFIX}{int(order)}{_ORDER_HIGH_SUFFIX}",
    )
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        try:
            os.write(fd, b"1")
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(spool_dir)

    markers: list[tuple[int, str]] = []
    for name in os.listdir(spool_dir):
        if not name.startswith(_ORDER_HIGH_PREFIX) or not name.endswith(
            _ORDER_HIGH_SUFFIX
        ):
            continue
        raw_order = name[len(_ORDER_HIGH_PREFIX) : -len(_ORDER_HIGH_SUFFIX)]
        if raw_order.isdigit():
            markers.append((int(raw_order), os.path.join(spool_dir, name)))
    if not markers:
        raise OSError("spool enqueue high watermark was not persisted")
    highest = max(value for value, _path in markers)
    removed = False
    for value, path in markers:
        if value < highest:
            try:
                os.unlink(path)
                removed = True
            except OSError:
                pass
    if removed:
        _fsync_directory(spool_dir)


def _used_order_directory(spool_dir: str) -> str:
    directory = os.path.join(spool_dir, _ORDER_USED_DIRECTORY)
    if os.path.islink(directory):
        raise OSError("used-order directory must not be a symlink")
    created = not os.path.exists(directory)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.path.islink(directory) or not os.path.isdir(directory):
        raise OSError("used-order directory must be a directory")
    if created:
        _fsync_directory(spool_dir)
    return directory


def _used_order_path(spool_dir: str, order: int) -> str:
    return os.path.join(
        _used_order_directory(spool_dir),
        f"{int(order)}{_ORDER_USED_SUFFIX}",
    )


def _order_was_used(spool_dir: str, order: int) -> bool:
    return os.path.lexists(_used_order_path(spool_dir, order))


def _record_used_order(spool_dir: str, source: str, order: int) -> None:
    marker = _used_order_path(spool_dir, order)
    try:
        os.link(source, marker)
    except FileExistsError:
        try:
            if os.path.samestat(os.stat(source), os.stat(marker)):
                _fsync_directory(os.path.dirname(marker))
                return
        except OSError:
            pass
        raise FileExistsError(f"spool enqueue order already used: {order}")
    _fsync_directory(os.path.dirname(marker))


def _unlink_owned_reservation(path: str, fd: int, spool_dir: str) -> bool:
    try:
        if not os.path.samestat(os.fstat(fd), os.stat(path, follow_symlinks=False)):
            return False
        os.unlink(path)
        _fsync_directory(spool_dir)
        return True
    except OSError:
        # Windows may refuse unlink while the CRT handle is open. Keep the
        # marker for EventSpool recovery; exact used-order prevents ABA reuse.
        return False


def _reserve_enqueue_order(spool_dir: str) -> tuple[int, list[str], int, str]:
    _used_order_directory(spool_dir)
    candidate = max(
        0,
        time.time_ns(),
        _highest_persisted_enqueue_order(spool_dir) + 1,
    )
    fd, owner_path = tempfile.mkstemp(
        dir=spool_dir,
        prefix=_ORDER_OWNER_PREFIX,
        suffix=_ORDER_OWNER_SUFFIX,
    )
    reservations: list[str] = []
    try:
        if not _try_lock_order_reservation(fd):
            raise OSError("cannot lock spool enqueue order owner")
        os.fsync(fd)
        while True:
            reservation = os.path.join(
                spool_dir,
                f"{_ORDER_RESERVATION_PREFIX}{candidate}{_ORDER_RESERVATION_SUFFIX}",
            )
            try:
                os.link(owner_path, reservation)
            except FileExistsError:
                candidate = max(
                    candidate + 1,
                    time.time_ns(),
                    _highest_persisted_enqueue_order(spool_dir) + 1,
                )
                continue
            _fsync_directory(spool_dir)
            reservations.append(reservation)
            if _order_was_used(spool_dir, candidate):
                if _unlink_owned_reservation(reservation, fd, spool_dir):
                    reservations.remove(reservation)
                candidate = max(
                    candidate + 1,
                    time.time_ns(),
                    _highest_persisted_enqueue_order(spool_dir) + 1,
                )
                continue
            return candidate, reservations, fd, owner_path
    except BaseException:
        _unlock_order_reservation(fd)
        os.close(fd)
        try:
            os.unlink(owner_path)
        except OSError:
            pass
        raise


def _write_spool_event(spool_dir: str, payload: dict[str, str]) -> str:
    """Atomically write the HookEvent/SpoolEntry-compatible JSON envelope."""
    event_id = uuid.uuid4().hex
    if not _valid_event_id(event_id):  # defensive: uuid4().hex is always safe
        raise ValueError("invalid generated event id")
    os.makedirs(spool_dir, mode=0o700, exist_ok=True)
    destination = os.path.join(spool_dir, f"{event_id}.json")
    order_reservations: list[str] = []
    order_owner_path: str | None = None
    order_reservation_fd: int | None = None
    order_recorded = False
    temporary: str | None = None
    try:
        (
            enqueue_order,
            order_reservations,
            order_reservation_fd,
            order_owner_path,
        ) = _reserve_enqueue_order(spool_dir)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=spool_dir,
            prefix=f".{event_id}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(
                {
                    "event_id": event_id,
                    "enqueued_at_ns": enqueue_order,
                    "payload": payload,
                },
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        _record_used_order(spool_dir, order_owner_path, enqueue_order)
        os.replace(temporary, destination)
        _fsync_directory(spool_dir)
        temporary = None
        _record_enqueue_high_watermark(spool_dir, enqueue_order)
        order_recorded = True
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        if order_reservation_fd is not None:
            if order_recorded:
                for reservation in order_reservations:
                    _unlink_owned_reservation(
                        reservation,
                        order_reservation_fd,
                        spool_dir,
                    )
            _unlock_order_reservation(order_reservation_fd)
            try:
                os.close(order_reservation_fd)
            except OSError:
                pass
        if order_owner_path is not None:
            try:
                os.unlink(order_owner_path)
            except OSError:
                pass
    return event_id


def _event_from_input(hook_input: dict, cfg: dict) -> dict[str, str]:
    event = hook_input.get("hook_event_name")
    if not isinstance(event, str) or not event.strip():
        raise ValueError("hook_event_name is required")
    student_id = cfg.get("student_id")
    if not isinstance(student_id, str) or not student_id.strip():
        raise ValueError("student_id is required")

    def text(name: str) -> str:
        value = hook_input.get(name, "")
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        return value

    transcript_path = text("transcript_path")
    transcript_tail = ""
    if transcript_path:
        try:
            transcript_tail = _read_transcript_tail(transcript_path, _tail_size(cfg))
        except OSError:
            # WorkBuddy/antivirus may hold an exclusive Windows handle during
            # Stop. Preserve the durable event even when this optional tail is
            # temporarily unavailable; the resident agent retries full
            # context through its transcript outbox.
            transcript_tail = ""
    return {
        "event": event,
        "student_id": student_id,
        "session_id": text("session_id"),
        "cwd": text("cwd"),
        "transcript_tail": transcript_tail,
        # A local path is never sent to the server.  Full transcript reads are
        # an agent-side WorkBuddyData concern, not a Hook payload concern.
        "transcript_path": "",
        "prompt": text("prompt"),
    }


def main() -> int:
    """Capture one event; all malformed input/filesystem failures return 0."""
    try:
        raw = sys.stdin.read()
        if not isinstance(raw, str) or not raw.strip():
            return 0
        hook_input = json.loads(raw)
        if not isinstance(hook_input, dict):
            return 0
        cfg = _load_config()
        payload = _event_from_input(hook_input, cfg)
        _write_spool_event(_spool_dir(cfg), payload)
    except Exception as exc:
        # Hook failures must never block or fail WorkBuddy.  The message is
        # deliberately generic and never includes transcript/token contents.
        print(f"[copilot hook] degraded after error: {type(exc).__name__}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
