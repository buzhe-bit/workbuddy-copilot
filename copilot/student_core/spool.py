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
from .transport import Accepted, PermanentTransportError, TemporaryNetworkError

from .models import _EVENT_ID


def _validate_event_id(event_id: str) -> str:
    if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
        raise ValueError("invalid event_id")
    return event_id


class ReceiptLedger:
    """Crash-safe rendered/acknowledged receipt state beside the event spool."""

    _FILENAME = ".copilot-receipts.sqlite3"
    _VALID_STATES = {"rendered", "acked"}
    _MAX_ACKED_PER_STUDENT = 256

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

    def __init__(self, directory: str | os.PathLike[str]) -> None:
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
        self.receipt_ledger = ReceiptLedger(self.directory)

    def _path(self, event_id: str) -> Path:
        return self.directory / f"{_validate_event_id(event_id)}.json"

    def enqueue(self, event: HookEvent, *, event_id: str | None = None) -> str:
        if not isinstance(event, HookEvent):
            raise TypeError("event must be a HookEvent")
        identifier = _validate_event_id(str(uuid.uuid4()) if event_id is None else event_id)
        destination = self._path(identifier)
        entry = SpoolEntry(event_id=identifier, payload=event)
        temporary: Path | None = None
        reserved = False
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.directory,
                prefix=f".{identifier}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(entry.to_dict(), handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            # Reserve the final name with O_EXCL before replacing the empty
            # placeholder. This closes the exists+replace race across threads
            # and processes while retaining an atomic temp-file commit.
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            reserved = True
            os.replace(temporary, destination)
            temporary = None
            reserved = False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if reserved:
                destination.unlink(missing_ok=True)
        return identifier

    def pending(self) -> list[SpoolEntry]:
        entries: list[SpoolEntry] = []
        for path in sorted(self.directory.glob("*.json"), key=lambda item: item.name):
            if path.is_symlink() or not path.is_file():
                self._quarantine(path)
                continue
            try:
                event_id = _validate_event_id(path.stem)
            except ValueError:
                self._quarantine(path)
                continue
            if self._claim_is_active(event_id):
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    entry = SpoolEntry.from_dict(json.load(handle))
                if entry.event_id != path.stem:
                    raise ValueError("event_id does not match spool filename")
                entries.append(entry)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                self._quarantine(path)
        return entries

    def ack(self, event_id: str) -> None:
        self._path(event_id).unlink(missing_ok=True)
        self._claim_path(event_id).unlink(missing_ok=True)

    def claim(self, event_id: str) -> bool:
        """Claim an event with an exclusive lock file before sending it."""
        identifier = _validate_event_id(event_id)
        event_path = self._path(identifier)
        if event_path.is_symlink() or not event_path.is_file():
            return False
        claim_path = self._claim_path(identifier)
        if claim_path.is_symlink():
            claim_path.unlink(missing_ok=True)
        try:
            fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(f"{os.getpid()} {time.time_ns()}\n")
            return True
        except FileExistsError:
            if not self._claim_is_active(identifier):
                claim_path.unlink(missing_ok=True)
                return self.claim(identifier)
            return False

    def release_claim(self, event_id: str) -> None:
        self._claim_path(event_id).unlink(missing_ok=True)

    def _claim_path(self, event_id: str) -> Path:
        return self.directory / f".{_validate_event_id(event_id)}.claim"

    def _claim_is_active(self, event_id: str) -> bool:
        claim_path = self._claim_path(event_id)
        if claim_path.is_symlink():
            claim_path.unlink(missing_ok=True)
            return False
        if not claim_path.exists():
            return False
        try:
            owner = claim_path.read_text(encoding="ascii").split(maxsplit=1)[0]
            pid = int(owner)
        except (OSError, ValueError, IndexError):
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            claim_path.unlink(missing_ok=True)
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _quarantine(self, path: Path) -> None:
        if not path.exists() and not path.is_symlink():
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
    pending = spool.pending()
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
            spool.ack(entry.event_id)
        return accepted
    finally:
        if not accepted:
            spool.release_claim(entry.event_id)
