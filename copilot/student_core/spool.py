"""Durable local event spool for the platform-neutral student agent."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .models import HookEvent, SpoolEntry
from .process_liveness import FileClaimStore, ProcessIdentity, ProcessLiveness
from .transport import Accepted, PermanentTransportError, TemporaryNetworkError

from .models import _EVENT_ID


def _validate_event_id(event_id: str) -> str:
    if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
        raise ValueError("invalid event_id")
    return event_id


def _fsync_directory(directory: str | os.PathLike[str]) -> None:
    """Best-effort durability barrier for directory entry changes."""
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
    """Take a nonblocking byte lock that is released automatically on crash."""

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


class ReceiptLedger:
    """Crash-safe rendered/acknowledged receipt state beside the event spool."""

    _FILENAME = ".copilot-receipts.sqlite3"
    _VALID_STATES = {"rendered", "acked"}
    # Match the native inbox's terminal-history window so a startup receipt
    # reconciliation cannot lose an otherwise retained Windows card.
    _MAX_ACKED_PER_STUDENT = 300

    def __init__(self, directory: Path) -> None:
        self.path = directory / self._FILENAME
        if self.path.is_symlink():
            raise ValueError("receipt ledger must not be a symlink")
        self._initialize()

    @staticmethod
    def _key(student_id: str, message_id: str) -> tuple[str, str]:
        student = str(student_id or "").strip()
        message = str(message_id or "").strip()
        if not student or not message or len(student) > 512 or len(message) > 512:
            raise ValueError("invalid receipt ledger key")
        return student, message

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink():
            raise ValueError("receipt ledger must not be a symlink")
        return sqlite3.connect(self.path, timeout=1.0, isolation_level=None)

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS mentor_message_receipts (
                   student_id TEXT NOT NULL,
                   message_id TEXT NOT NULL,
                   state TEXT NOT NULL CHECK(state IN ('rendered', 'acked')),
                   updated_at_ns INTEGER NOT NULL,
                   PRIMARY KEY(student_id, message_id)
                )"""
            )
        finally:
            connection.close()

    def status(self, student_id: str, message_id: str) -> str | None:
        student, message = self._key(student_id, message_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT state FROM mentor_message_receipts WHERE student_id = ? AND message_id = ?",
                (student, message),
            ).fetchone()
            return str(row[0]) if row and row[0] in self._VALID_STATES else None
        finally:
            connection.close()

    def mark_rendered(self, student_id: str, message_id: str) -> None:
        student, message = self._key(student_id, message_id)
        self._mark(student, message, "rendered")

    def mark_acked(self, student_id: str, message_id: str) -> None:
        student, message = self._key(student_id, message_id)
        self._mark(student, message, "acked")

    def rendered_message_ids(self, student_id: str, *, limit: int = 64) -> list[str]:
        student = str(student_id or "").strip()
        if not student:
            raise ValueError("invalid receipt ledger student")
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT message_id FROM mentor_message_receipts
                   WHERE student_id = ? AND state = 'rendered'
                   ORDER BY updated_at_ns ASC LIMIT ?""",
                (student, max(1, int(limit))),
            ).fetchall()
            return [str(row[0]) for row in rows]
        finally:
            connection.close()

    def _mark(self, student_id: str, message_id: str, state: str) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if state == "rendered":
                connection.execute(
                    """INSERT INTO mentor_message_receipts
                       (student_id, message_id, state, updated_at_ns)
                       VALUES (?, ?, 'rendered', ?)
                       ON CONFLICT(student_id, message_id) DO UPDATE SET
                         state = CASE WHEN state = 'acked' THEN 'acked' ELSE 'rendered' END,
                         updated_at_ns = excluded.updated_at_ns""",
                    (student_id, message_id, time.time_ns()),
                )
            else:
                connection.execute(
                    """INSERT INTO mentor_message_receipts
                       (student_id, message_id, state, updated_at_ns)
                       VALUES (?, ?, 'acked', ?)
                       ON CONFLICT(student_id, message_id) DO UPDATE SET
                         state = 'acked', updated_at_ns = excluded.updated_at_ns""",
                    (student_id, message_id, time.time_ns()),
                )
                # Keep only a bounded idempotency window for completed
                # receipts. Unacknowledged rendered rows are never pruned.
                connection.execute(
                    """DELETE FROM mentor_message_receipts
                       WHERE student_id = ? AND state = 'acked'
                         AND message_id NOT IN (
                           SELECT message_id FROM mentor_message_receipts
                           WHERE student_id = ? AND state = 'acked'
                           ORDER BY updated_at_ns DESC, message_id DESC LIMIT ?
                         )""",
                    (student_id, student_id, self._MAX_ACKED_PER_STUDENT),
                )
            connection.commit()
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()


class EventSpool:
    """Store one JSON envelope per event and acknowledge only after delivery."""

    _ORDER_RESERVATION_PREFIX = ".copilot-enqueue-order-"
    _ORDER_RESERVATION_SUFFIX = ".lock"
    _ORDER_OWNER_PREFIX = ".copilot-enqueue-order-owner-"
    _ORDER_OWNER_SUFFIX = ".tmp"
    _ORDER_HIGH_PREFIX = ".copilot-enqueue-high-"
    _ORDER_HIGH_SUFFIX = ".mark"
    _ORDER_USED_DIRECTORY = ".copilot-enqueue-used"
    _ORDER_USED_SUFFIX = ".used"
    _EVENT_RESERVATION_PREFIX = ".copilot-event-id-"
    _EVENT_RESERVATION_SUFFIX = ".lock"

    def __init__(
        self,
        directory: str | os.PathLike[str],
        *,
        process_identity: ProcessIdentity | None = None,
        process_liveness: Any | None = None,
        claim_store: FileClaimStore | None = None,
    ) -> None:
        self.directory = Path(directory).expanduser()
        if self.directory.is_symlink():
            raise ValueError("spool directory must not be a symlink")
        self.directory.mkdir(parents=True, exist_ok=True)
        if not self.directory.is_dir() or self.directory.is_symlink():
            raise ValueError("spool directory must be a directory")
        self.quarantine = self.directory / "quarantine"
        if self.quarantine.is_symlink():
            raise ValueError("quarantine directory must not be a symlink")
        self.quarantine.mkdir(parents=True, exist_ok=True)
        if not self.quarantine.is_dir() or self.quarantine.is_symlink():
            raise ValueError("quarantine directory must be a directory")
        self.used_orders = self.directory / self._ORDER_USED_DIRECTORY
        if self.used_orders.is_symlink():
            raise ValueError("used-order directory must not be a symlink")
        used_orders_created = not self.used_orders.exists()
        self.used_orders.mkdir(parents=False, exist_ok=True)
        if not self.used_orders.is_dir() or self.used_orders.is_symlink():
            raise ValueError("used-order directory must be a directory")
        if used_orders_created:
            _fsync_directory(self.directory)
        self.receipt_ledger = ReceiptLedger(self.directory)
        if claim_store is not None:
            if process_identity is not None or process_liveness is not None:
                raise ValueError(
                    "claim_store cannot be combined with process identity overrides"
                )
            if claim_store.directory.resolve() != self.directory.resolve():
                raise ValueError("claim store must use the spool directory")
            self.claim_store = claim_store
        else:
            liveness = process_liveness or ProcessLiveness()
            identity = process_identity
            if identity is None:
                current_identity = getattr(liveness, "current_identity", None)
                if not callable(current_identity):
                    raise TypeError(
                        "process_liveness must provide current_identity when identity is omitted"
                    )
                identity = current_identity()
            self.claim_store = FileClaimStore(
                self.directory,
                process_identity=identity,
                process_liveness=liveness,
            )
        self.process_identity = self.claim_store.process_identity
        self.process_liveness = self.claim_store.process_liveness

    def _path(self, event_id: str) -> Path:
        return self.directory / f"{_validate_event_id(event_id)}.json"

    def _highest_persisted_enqueue_order(self) -> int:
        """Read the durable high watermark without mutating bad spool rows."""
        highest = 0
        high_markers = list(self.directory.glob(
            f"{self._ORDER_HIGH_PREFIX}*{self._ORDER_HIGH_SUFFIX}"
        ))
        has_valid_high = False
        for path in high_markers:
            raw_order = path.name[
                len(self._ORDER_HIGH_PREFIX) : -len(self._ORDER_HIGH_SUFFIX)
            ]
            if raw_order.isdigit():
                has_valid_high = True
                highest = max(highest, int(raw_order))
        pattern = f"{self._ORDER_RESERVATION_PREFIX}*{self._ORDER_RESERVATION_SUFFIX}"
        for path in self.directory.glob(pattern):
            name = path.name
            raw_order = name[
                len(self._ORDER_RESERVATION_PREFIX) : -len(self._ORDER_RESERVATION_SUFFIX)
            ]
            if raw_order.isdigit():
                highest = max(highest, int(raw_order))
        # One migration scan establishes the first high marker above all
        # legacy rows. Thereafter hook enqueue stays O(number of tiny markers),
        # never O(total transcript-tail bytes in the offline backlog).
        if not has_valid_high:
            for path in self.directory.glob("*.json"):
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    with path.open("r", encoding="utf-8") as handle:
                        entry = SpoolEntry.from_dict(json.load(handle))
                        file_mtime_ns = os.fstat(handle.fileno()).st_mtime_ns
                    order = entry.enqueued_at_ns or file_mtime_ns
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
                highest = max(highest, order)
        return highest

    def _record_enqueue_high_watermark(self, order: int) -> None:
        marker = self.directory / (
            f"{self._ORDER_HIGH_PREFIX}{int(order)}{self._ORDER_HIGH_SUFFIX}"
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
            _fsync_directory(self.directory)

        markers: list[tuple[int, Path]] = []
        for path in self.directory.glob(
            f"{self._ORDER_HIGH_PREFIX}*{self._ORDER_HIGH_SUFFIX}"
        ):
            raw_order = path.name[
                len(self._ORDER_HIGH_PREFIX) : -len(self._ORDER_HIGH_SUFFIX)
            ]
            if raw_order.isdigit():
                markers.append((int(raw_order), path))
        if not markers:
            raise OSError("spool enqueue high watermark was not persisted")
        highest = max(value for value, _path in markers)
        removed = False
        for value, path in markers:
            if value < highest:
                try:
                    path.unlink()
                    removed = True
                except OSError:
                    pass
        if removed:
            _fsync_directory(self.directory)

    def _used_order_path(self, order: int) -> Path:
        return self.used_orders / f"{int(order)}{self._ORDER_USED_SUFFIX}"

    def _order_was_used(self, order: int) -> bool:
        path = self._used_order_path(order)
        return path.exists() or path.is_symlink()

    def _record_used_order(self, source: Path, order: int) -> None:
        """Persist an exact, never-reused order tombstone from the locked inode."""

        marker = self._used_order_path(order)
        try:
            os.link(source, marker)
        except FileExistsError:
            try:
                if os.path.samestat(source.stat(), marker.stat()):
                    _fsync_directory(self.used_orders)
                    return
            except OSError:
                pass
            # An existing marker from another inode proves this candidate was
            # already committed. Callers must never silently reuse it.
            raise FileExistsError(f"spool enqueue order already used: {order}")
        _fsync_directory(self.used_orders)

    def _unlink_owned_reservation(self, path: Path, fd: int) -> bool:
        """Best-effort removal while the exact reservation inode is locked."""

        try:
            if not os.path.samestat(os.fstat(fd), path.lstat()):
                return False
            path.unlink()
            _fsync_directory(self.directory)
            return True
        except OSError:
            # Windows may refuse unlink while a CRT handle is open. Leaving
            # the marker is safe: the exact used-order tombstone prevents ABA
            # reuse and the resident recovery pass removes it after unlock.
            return False

    def _reserve_enqueue_order(self) -> tuple[int, list[Path], int, Path]:
        """Atomically publish a marker whose inode is already OS-locked."""
        candidate = max(
            0,
            time.time_ns(),
            self._highest_persisted_enqueue_order() + 1,
        )
        fd, raw_owner_path = tempfile.mkstemp(
            dir=self.directory,
            prefix=self._ORDER_OWNER_PREFIX,
            suffix=self._ORDER_OWNER_SUFFIX,
        )
        owner_path = Path(raw_owner_path)
        reservations: list[Path] = []
        try:
            if not _try_lock_order_reservation(fd):
                raise OSError("cannot lock spool enqueue order owner")
            os.fsync(fd)
            while True:
                reservation = self.directory / (
                    f"{self._ORDER_RESERVATION_PREFIX}{candidate}"
                    f"{self._ORDER_RESERVATION_SUFFIX}"
                )
                try:
                    # Hard-link publication is no-replace and points at the
                    # exact inode whose byte lock is already held above.
                    os.link(owner_path, reservation)
                except FileExistsError:
                    candidate = max(
                        candidate + 1,
                        time.time_ns(),
                        self._highest_persisted_enqueue_order() + 1,
                    )
                    continue
                _fsync_directory(self.directory)
                reservations.append(reservation)
                if self._order_was_used(candidate):
                    # A writer may have calculated this candidate, paused,
                    # then resumed after an earlier writer committed and
                    # removed the active marker. Keep any Windows-unlinkable
                    # alias on this locked inode and allocate a fresh order.
                    if self._unlink_owned_reservation(reservation, fd):
                        reservations.remove(reservation)
                    candidate = max(
                        candidate + 1,
                        time.time_ns(),
                        self._highest_persisted_enqueue_order() + 1,
                    )
                    continue
                return candidate, reservations, fd, owner_path
        except BaseException:
            _unlock_order_reservation(fd)
            os.close(fd)
            owner_path.unlink(missing_ok=True)
            raise

    def _active_enqueue_barrier(self) -> int | None:
        """Return the oldest in-flight order and reap crash-released markers."""

        active: list[int] = []
        pattern = f"{self._ORDER_RESERVATION_PREFIX}*{self._ORDER_RESERVATION_SUFFIX}"
        reservations: list[tuple[int, Path]] = []
        for path in self.directory.glob(pattern):
            raw_order = path.name[
                len(self._ORDER_RESERVATION_PREFIX) : -len(self._ORDER_RESERVATION_SUFFIX)
            ]
            if not raw_order.isdigit():
                continue
            reservations.append((int(raw_order), path))
        for order, path in sorted(reservations, key=lambda item: item[0]):
            try:
                fd = os.open(path, os.O_RDWR)
            except FileNotFoundError:
                continue
            except OSError:
                active.append(order)
                continue
            try:
                opened_stat = os.fstat(fd)
                acquired = _try_lock_order_reservation(fd)
                if not acquired:
                    active.append(order)
                    continue
                try:
                    if not self._order_was_used(order):
                        self._record_used_order(path, order)
                    self._record_enqueue_high_watermark(order)
                    self._unlink_owned_reservation(path, fd)
                    # Clean the matching private owner while this open handle
                    # still pins the exact inode. This avoids post-close inode
                    # reuse making samestat match an unrelated new writer.
                    self._cleanup_owner_links(opened_stat)
                except OSError:
                    active.append(order)
                    continue
            finally:
                _unlock_order_reservation(fd)
                os.close(fd)
            # Windows may deny owner/reservation deletion while the CRT handle
            # is open. Retry owner cleanup while the reservation path still
            # pins the old inode, then remove that reservation last.
            self._cleanup_owner_links(opened_stat)
            try:
                if os.path.samestat(opened_stat, path.lstat()):
                    path.unlink()
                    _fsync_directory(self.directory)
            except OSError:
                pass
        return min(active) if active else None

    def _cleanup_owner_links(self, reservation_stat: os.stat_result) -> None:
        pattern = f"{self._ORDER_OWNER_PREFIX}*{self._ORDER_OWNER_SUFFIX}"
        removed = False
        for owner in self.directory.glob(pattern):
            try:
                if os.path.samestat(reservation_stat, owner.lstat()):
                    owner.unlink()
                    removed = True
            except OSError:
                continue
        if removed:
            _fsync_directory(self.directory)

    def _reserve_event_id(self, event_id: str) -> Path:
        """Reserve an ID without ever exposing an invalid final ``.json``."""

        reservation = self.directory / (
            f"{self._EVENT_RESERVATION_PREFIX}{event_id}"
            f"{self._EVENT_RESERVATION_SUFFIX}"
        )
        try:
            fd = os.open(reservation, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise FileExistsError(f"spool event is already reserved: {event_id}") from exc
        os.close(fd)
        destination = self._path(event_id)
        if destination.exists() or destination.is_symlink():
            reservation.unlink(missing_ok=True)
            raise FileExistsError(f"spool event already exists: {event_id}")
        return reservation

    def enqueue(self, event: HookEvent, *, event_id: str | None = None) -> str:
        if not isinstance(event, HookEvent):
            raise TypeError("event must be a HookEvent")
        identifier = _validate_event_id(str(uuid.uuid4()) if event_id is None else event_id)
        destination = self._path(identifier)
        event_reservation = self._reserve_event_id(identifier)
        order_reservations: list[Path] = []
        order_owner_path: Path | None = None
        order_reservation_fd: int | None = None
        order_recorded = False
        temporary: Path | None = None
        try:
            (
                enqueue_order,
                order_reservations,
                order_reservation_fd,
                order_owner_path,
            ) = self._reserve_enqueue_order()
            entry = SpoolEntry(
                event_id=identifier,
                payload=event,
                enqueued_at_ns=enqueue_order,
            )
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.directory,
                prefix=f".{identifier}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(
                    entry.to_dict(),
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            # Mark the exact order as used while the reservation inode is
            # still locked. A crash before final publication only leaves a
            # harmless sequence gap; it can never create a duplicate order.
            self._record_used_order(order_owner_path, enqueue_order)
            # Cooperating writers honor the hidden event reservation. The
            # public final path appears only when a complete fsynced envelope
            # is atomically committed.
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(f"spool event already exists: {identifier}")
            os.replace(temporary, destination)
            _fsync_directory(self.directory)
            temporary = None
            self._record_enqueue_high_watermark(enqueue_order)
            order_recorded = True
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            if order_reservation_fd is not None:
                try:
                    if order_recorded:
                        for reservation in order_reservations:
                            self._unlink_owned_reservation(
                                reservation,
                                order_reservation_fd,
                            )
                finally:
                    _unlock_order_reservation(order_reservation_fd)
                    try:
                        os.close(order_reservation_fd)
                    except OSError:
                        pass
            if order_owner_path is not None:
                try:
                    order_owner_path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                event_reservation.unlink(missing_ok=True)
            except OSError:
                pass
        return identifier

    def pending(self, *, include_claimed: bool = False) -> list[SpoolEntry]:
        """Return durable FIFO rows; optionally retain claimed head blockers."""
        enqueue_barrier = self._active_enqueue_barrier()
        ordered_entries: list[tuple[int, str, SpoolEntry]] = []
        for path in sorted(self.directory.glob("*.json"), key=lambda item: item.name):
            try:
                listed_stat = path.lstat()
            except OSError:
                continue
            if path.is_symlink() or not path.is_file():
                self._quarantine(path, expected_stat=listed_stat)
                continue
            try:
                event_id = _validate_event_id(path.stem)
            except ValueError:
                self._quarantine(path, expected_stat=listed_stat)
                continue
            if not include_claimed and self._claim_is_active(event_id):
                continue
            opened_stat: os.stat_result | None = None
            try:
                with path.open("r", encoding="utf-8") as handle:
                    opened_stat = os.fstat(handle.fileno())
                    entry = SpoolEntry.from_dict(json.load(handle))
                    file_mtime_ns = opened_stat.st_mtime_ns
                if entry.event_id != path.stem:
                    raise ValueError("event_id does not match spool filename")
                enqueue_order = entry.enqueued_at_ns or file_mtime_ns
                if enqueue_barrier is not None and enqueue_order >= enqueue_barrier:
                    continue
                ordered_entries.append((enqueue_order, entry.event_id, entry))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                self._quarantine(path, expected_stat=opened_stat or listed_stat)
        ordered_entries.sort(key=lambda item: (item[0], item[1]))
        return [entry for _order, _event_id, entry in ordered_entries]

    def ack(self, event_id: str) -> bool:
        event_path = self._path(event_id)
        completed = self.claim_store.complete(
            event_id,
            lambda: event_path.unlink(missing_ok=True),
            expected_identity=self.process_identity,
            allow_unclaimed=True,
        )
        if completed:
            _fsync_directory(self.directory)
        return completed

    def claim(self, event_id: str) -> bool:
        """Claim an event with an exclusive lock file before sending it."""
        identifier = _validate_event_id(event_id)
        event_path = self._path(identifier)
        if event_path.is_symlink() or not event_path.is_file():
            return False
        return self.claim_store.acquire(identifier)

    def release_claim(self, event_id: str) -> bool:
        return self.claim_store.release(
            _validate_event_id(event_id),
            expected_identity=self.process_identity,
        )

    def repair_claim(
        self,
        event_id: str,
        *,
        expected_identity: ProcessIdentity,
        expected_owner_token: str,
        reason: str,
    ) -> bool:
        return self.claim_store.repair(
            _validate_event_id(event_id),
            expected_identity=expected_identity,
            expected_owner_token=expected_owner_token,
            reason=reason,
        )

    def claim_health(self) -> dict[str, object]:
        return self.claim_store.health()

    def _claim_path(self, event_id: str) -> Path:
        return self.claim_store.path_for(_validate_event_id(event_id))

    def _claim_is_active(self, event_id: str) -> bool:
        return self.claim_store.is_active(_validate_event_id(event_id))

    def _quarantine(
        self,
        path: Path,
        *,
        expected_stat: os.stat_result | None = None,
    ) -> None:
        if not path.exists() and not path.is_symlink():
            return
        if expected_stat is not None:
            try:
                if not os.path.samestat(expected_stat, path.lstat()):
                    return
            except OSError:
                return
        destination = self.quarantine / f"{path.stem}-{uuid.uuid4().hex}.json"
        try:
            os.replace(path, destination)
        except OSError:
            # A transient filesystem failure should leave the original event for
            # a later pass rather than silently discard it.
            return


def consume_one(spool: EventSpool, transport: Any) -> bool:
    """Post the oldest event and delete it only after an ``Accepted`` result."""
    pending = spool.pending(include_claimed=True)
    if not pending:
        return False
    entry = pending[0]
    if not spool.claim(entry.event_id):
        return False
    accepted = False
    try:
        try:
            result = transport.post_hook(entry.payload, event_id=entry.event_id)
        except (TemporaryNetworkError, PermanentTransportError):
            return False
        accepted = isinstance(result, Accepted)
        if accepted:
            try:
                accepted = bool(spool.ack(entry.event_id))
            except Exception:
                accepted = False
        return accepted
    finally:
        if not accepted:
            try:
                spool.release_claim(entry.event_id)
            except Exception:
                pass
