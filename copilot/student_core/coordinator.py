"""Platform-neutral orchestration for the resident student client.

The coordinator deliberately owns no UI or WorkBuddy implementation.  It
accepts small injectable collaborators so the same retry, deduplication and
acknowledgement rules can run on macOS, Windows, and in unit tests.
"""
from __future__ import annotations

import hashlib
import inspect
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from .spool import EventSpool, ReceiptLedger
from .transport import Accepted, PermanentTransportError, TemporaryNetworkError

log = logging.getLogger("copilot.student_core.coordinator")
MAX_PENDING_MESSAGES_PER_PULL = 64
MAX_PENDING_MESSAGE_PAGES_PER_PULL = 8

MaybeAwaitable = Any | Awaitable[Any]


async def _default_sleeper(delay: float) -> None:
    # Import asyncio only when a real runtime loop is used.  Keeping it lazy
    # lets the Student Core import gate run on Windows without loading Unix's
    # optional ``fcntl`` module.
    import asyncio

    await asyncio.sleep(delay)


async def _maybe_await(value: MaybeAwaitable) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _supports_pending_cursor(method: Callable[..., Any]) -> bool:
    """Distinguish a legacy adapter signature from an internal TypeError."""
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.name == "after_id"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


class StudentCoordinator:
    """Coordinate durable hook delivery and inbound mentor commands.

    ``spool`` and ``transport`` are intentionally concrete protocol objects,
    while ``uploader`` and ``message_handler`` are optional platform adapters.
    A spool file is claimed before posting and is acknowledged only when the
    transport returns :class:`Accepted`.
    """

    def __init__(
        self,
        spool: EventSpool,
        transport: Any,
        uploader: Any | None = None,
        *,
        message_handler: Callable[[Mapping[str, Any]], MaybeAwaitable] | None = None,
        sleeper: Callable[[float], MaybeAwaitable] | None = None,
        clock: Callable[[], float] | None = None,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 30.0,
        stale_claim_after: float = 300.0,
        seen_message_ids: set[tuple[str, str]] | None = None,
        handled_request_ids: set[str] | None = None,
        receipt_ledger: ReceiptLedger | None = None,
    ) -> None:
        if reconnect_initial <= 0 or reconnect_max < reconnect_initial:
            raise ValueError("invalid reconnect backoff")
        if stale_claim_after <= 0:
            raise ValueError("stale_claim_after must be positive")
        self.spool = spool
        self.transport = transport
        self.uploader = uploader
        self.message_handler = message_handler
        self._sleeper = sleeper or _default_sleeper
        self._clock = clock or time.monotonic
        self._last_reconnect_at: float | None = None
        self.reconnect_initial = float(reconnect_initial)
        self.reconnect_max = float(reconnect_max)
        self.stale_claim_after = float(stale_claim_after)
        self._next_reconnect_delay = self.reconnect_initial
        self.seen_message_ids = seen_message_ids if seen_message_ids is not None else set()
        self.receipt_ledger = receipt_ledger or spool.receipt_ledger
        # Rendering and receipt confirmation are deliberately distinct: a
        # failed receipt must retry without showing the same mentor message
        # twice, whereas a failed renderer remains retryable.
        self.rendered_message_ids: set[tuple[str, str]] = set()
        self._pending_message_after_id = 0
        self._inflight_message_keys: set[tuple[str, str]] = set()
        self.handled_request_ids = handled_request_ids if handled_request_ids is not None else set()
        self._inflight_request_ids: set[str] = set()
        self._command_state_dir = self._prepare_command_state_dir()

    async def flush_spool_once(self) -> int:
        """Try each currently pending event and return the accepted count.

        Errors are intentionally swallowed after releasing the claim: a
        temporary failure stays in the spool for a later cycle, and a
        permanent rejection also remains visible for diagnosis/recovery.
        """
        accepted_count = 0
        for entry in self.spool.pending():
            if not self.spool.claim(entry.event_id):
                continue
            accepted = False
            try:
                try:
                    result = await _maybe_await(
                        self.transport.post_hook(
                            entry.payload,
                            event_id=entry.event_id,
                        )
                    )
                except (TemporaryNetworkError, PermanentTransportError):
                    result = None
                except Exception as exc:
                    # A custom adapter must not be able to make the resident
                    # loop die. Do not include event content or local paths.
                    log.warning("student hook delivery failed type=%s", type(exc).__name__)
                    result = None
                accepted = isinstance(result, Accepted)
                if accepted:
                    self.spool.ack(entry.event_id)
                    accepted_count += 1
            finally:
                if not accepted:
                    self.spool.release_claim(entry.event_id)
        return accepted_count

    async def handle_message(self, payload: Mapping[str, Any]) -> bool:
        """Handle one inbound ``mentor_message`` at most once.

        The receipt is sent only after the optional handler succeeds.  This
        keeps a failed platform/UI adapter retryable while making duplicate
        live-vs-catch-up deliveries harmless.
        """
        if not isinstance(payload, Mapping) or payload.get("type") != "mentor_message":
            return False
        student_id = str(payload.get("student_id") or "").strip()
        expected_student = str(getattr(self.transport, "student_id", "") or "").strip()
        if not expected_student or student_id != expected_student:
            return False
        message_id = str(payload.get("message_id") or "")
        if not message_id:
            return False
        message_key = (student_id, message_id)
        try:
            receipt_state = self.receipt_ledger.status(student_id, message_id)
        except (OSError, sqlite3.Error, ValueError) as exc:
            log.warning("student receipt ledger read failed type=%s", type(exc).__name__)
            receipt_state = None
        if receipt_state == "acked":
            self.seen_message_ids.add(message_key)
            return False
        if receipt_state == "rendered":
            self.rendered_message_ids.add(message_key)
        # This check/add has no await between it, so it is atomic within the
        # asyncio event loop. The inflight set closes the duplicate window
        # while the handler or receipt is awaiting network/UI work.
        if message_key in self.seen_message_ids or message_key in self._inflight_message_keys:
            return False
        self._inflight_message_keys.add(message_key)

        try:
            if message_key not in self.rendered_message_ids and self.message_handler is not None:
                try:
                    await _maybe_await(self.message_handler(payload))
                except Exception as exc:
                    log.warning("mentor message handler failed type=%s", type(exc).__name__)
                    return False
            if message_key not in self.rendered_message_ids:
                try:
                    self.receipt_ledger.mark_rendered(student_id, message_id)
                except (OSError, sqlite3.Error, ValueError) as exc:
                    log.warning("student receipt ledger write failed type=%s", type(exc).__name__)
                    return False
                self.rendered_message_ids.add(message_key)

            try:
                await self._send_message_receipt(student_id, message_id)
            except Exception as exc:
                log.warning("mentor message receipt failed type=%s", type(exc).__name__)
                return False
            try:
                self.receipt_ledger.mark_acked(student_id, message_id)
            except (OSError, sqlite3.Error, ValueError) as exc:
                log.warning("student receipt ledger ack write failed type=%s", type(exc).__name__)
            self.seen_message_ids.add(message_key)
            return True
        finally:
            # Handler/receipt failures must be retryable on a later delivery.
            self._inflight_message_keys.discard(message_key)

    async def pull_pending_messages(self) -> int:
        """Retry bounded server backlog receipts with cursor progress.

        Transport failures are intentionally non-fatal: the resident loop will
        make another attempt on its next safe cycle or WebSocket reconnect.
        """
        pull_method = getattr(self.transport, "get_pending_messages", None)
        if not callable(pull_method):
            return 0
        confirmed = 0
        after_id = self._pending_message_after_id
        for _ in range(MAX_PENDING_MESSAGE_PAGES_PER_PULL):
            try:
                if _supports_pending_cursor(pull_method):
                    payloads = await _maybe_await(pull_method(after_id=after_id))
                else:
                    # Existing test/platform adapters that predate the cursor
                    # retain their one-page behavior during the migration.
                    payloads = await _maybe_await(pull_method())
            except Exception as exc:
                log.warning("student message backlog failed type=%s", type(exc).__name__)
                return confirmed
            if not isinstance(payloads, list) or not payloads:
                self._pending_message_after_id = 0
                return confirmed + await self._retry_durable_rendered_receipts()

            next_after_id = after_id
            for payload in payloads[:MAX_PENDING_MESSAGES_PER_PULL]:
                if not isinstance(payload, Mapping):
                    continue
                try:
                    numeric_id = int(payload.get("id") or 0)
                except (TypeError, ValueError):
                    numeric_id = 0
                if numeric_id > next_after_id:
                    next_after_id = numeric_id
                if await self.handle_message(payload):
                    confirmed += 1
            if next_after_id <= after_id:
                self._pending_message_after_id = 0
                return confirmed
            after_id = next_after_id
            self._pending_message_after_id = after_id
            if len(payloads) < MAX_PENDING_MESSAGES_PER_PULL:
                self._pending_message_after_id = 0
                return confirmed + await self._retry_durable_rendered_receipts()
        return confirmed

    async def _retry_durable_rendered_receipts(self) -> int:
        """Idempotently confirm local rendered state absent from server pages."""
        student_id = str(getattr(self.transport, "student_id", "") or "").strip()
        if not student_id:
            return 0
        try:
            message_ids = self.receipt_ledger.rendered_message_ids(
                student_id,
                limit=MAX_PENDING_MESSAGES_PER_PULL,
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            log.warning("student rendered receipt lookup failed type=%s", type(exc).__name__)
            return 0
        confirmed = 0
        for message_id in message_ids:
            try:
                await self._send_message_receipt(student_id, message_id)
            except Exception as exc:
                log.warning("student durable receipt retry failed type=%s", type(exc).__name__)
                continue
            try:
                self.receipt_ledger.mark_acked(student_id, message_id)
            except (OSError, sqlite3.Error, ValueError) as exc:
                log.warning("student durable receipt ack write failed type=%s", type(exc).__name__)
            self.seen_message_ids.add((student_id, message_id))
            confirmed += 1
        return confirmed

    async def _send_message_receipt(self, student_id: str, message_id: str) -> None:
        """Acknowledge with the persisted server API, never a fake WS frame."""
        ack_method = getattr(self.transport, "ack_message", None)
        if ack_method is None:
            raise PermanentTransportError("transport cannot acknowledge mentor messages")
        result = await _maybe_await(ack_method(message_id, student_id=student_id))
        if not isinstance(result, Accepted):
            raise TemporaryNetworkError("message receipt rejected")

    async def handle_command(self, payload: Mapping[str, Any]) -> bool:
        """Run a supported mentor command once; ignore unknown commands."""
        if not isinstance(payload, Mapping) or payload.get("type") != "mentor_command":
            return False
        student_id = str(payload.get("student_id") or "").strip()
        expected_student = str(getattr(self.transport, "student_id", "") or "").strip()
        if not expected_student or student_id != expected_student:
            return False
        if payload.get("command") != "upload_conversations":
            return False
        request_id = str(payload.get("request_id") or "")
        if not request_id or self.uploader is None:
            return False
        if request_id in self.handled_request_ids or request_id in self._inflight_request_ids:
            return False

        self._inflight_request_ids.add(request_id)
        try:
            claim_path = self._claim_upload_request(request_id)
        except (OSError, ValueError) as exc:
            log.warning("student upload command claim failed type=%s", type(exc).__name__)
            self._inflight_request_ids.discard(request_id)
            return False
        if claim_path is None:
            self._inflight_request_ids.discard(request_id)
            return False
        try:
            handler = getattr(self.uploader, "upload", self.uploader)
            if not callable(handler):
                return False
            await _maybe_await(
                handler(
                    request_id=request_id,
                    session_id=(str(payload.get("session_id")) if payload.get("session_id") else None),
                )
            )
            self._mark_upload_request_complete(request_id)
            self.handled_request_ids.add(request_id)
            return True
        except Exception as exc:
            log.warning("student upload command failed type=%s", type(exc).__name__)
            return False
        finally:
            self._inflight_request_ids.discard(request_id)
            claim_path.unlink(missing_ok=True)

    def _prepare_command_state_dir(self) -> Path:
        state_dir = self.spool.directory / ".copilot-upload-commands"
        if state_dir.is_symlink():
            raise ValueError("upload command state directory must not be a symlink")
        state_dir.mkdir(parents=True, exist_ok=True)
        if not state_dir.is_dir() or state_dir.is_symlink():
            raise ValueError("upload command state directory must be a directory")
        return state_dir

    @staticmethod
    def _request_marker_stem(request_id: str) -> str:
        return hashlib.sha256(request_id.encode("utf-8")).hexdigest()

    def _request_paths(self, request_id: str) -> tuple[Path, Path]:
        """Return the active claim and legacy (non-authoritative) done path."""
        stem = self._request_marker_stem(request_id)
        return (
            self._command_state_dir / f"{stem}.claim",
            self._command_state_dir / f"{stem}.done",
        )

    def _claim_upload_request(self, request_id: str) -> Path | None:
        claim_path, _legacy_done_path = self._request_paths(request_id)
        if claim_path.is_symlink():
            raise ValueError("upload command marker must not be a symlink")
        connection = self._open_claim_lock()
        if connection is None:
            return None
        try:
            if self._completion_exists(connection, request_id):
                return None
            try:
                return self._create_upload_claim(claim_path)
            except FileExistsError:
                if not self._stale_upload_claim(claim_path):
                    return None
                # Every creator obtains the recovery gate first. Renaming the
                # observed stale file is therefore safe: no second creator can
                # replace this path before the winner installs its new claim.
                stale_path = self._command_state_dir / (
                    f"{self._request_marker_stem(request_id)}.stale-{uuid.uuid4().hex}"
                )
                try:
                    os.replace(claim_path, stale_path)
                except FileNotFoundError:
                    return None
                return self._create_upload_claim(claim_path)
        finally:
            try:
                # Claiming only reads the ledger; rollback releases the mutex
                # on every path without changing completion state.
                connection.rollback()
            finally:
                connection.close()

    @staticmethod
    def _write_marker(path: Path, content: str | None = None) -> os.stat_result:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created_stat = os.fstat(fd)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(content if content is not None else f"{os.getpid()} {time.time_ns()}\n")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            # O_EXCL proves we created this path.  Before cleanup, compare the
            # still-named inode so an external replacement is never unlinked.
            StudentCoordinator._unlink_if_same(path, created_stat)
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        return created_stat

    @staticmethod
    def _unlink_if_same(path: Path, created_stat: os.stat_result) -> None:
        try:
            if os.path.samestat(created_stat, path.lstat()):
                path.unlink()
        except OSError:
            pass

    def _open_claim_lock(self) -> sqlite3.Connection | None:
        """Open the completion ledger and acquire its crash-released mutex."""
        lock_db = self._command_state_dir / ".claim-locks.sqlite3"
        if lock_db.is_symlink():
            raise ValueError("upload command lock must not be a symlink")
        try:
            connection = sqlite3.connect(lock_db, timeout=0, isolation_level=None)
            connection.execute(
                """CREATE TABLE IF NOT EXISTS completed_commands (
                   request_key TEXT PRIMARY KEY,
                   completed_at_ns INTEGER NOT NULL
                )"""
            )
            connection.execute("BEGIN IMMEDIATE")
            return connection
        except sqlite3.Error:
            if "connection" in locals():
                connection.close()
            return None

    def _create_upload_claim(self, claim_path: Path) -> Path:
        self._write_marker(claim_path)
        return claim_path

    def _stale_upload_claim(self, claim_path: Path) -> bool:
        """Return true only for a definitely dead or safely expired owner."""
        if claim_path.is_symlink():
            raise ValueError("upload command marker must not be a symlink")
        try:
            owner, created_ns = claim_path.read_text(encoding="ascii").split(maxsplit=1)
            owner_pid = int(owner)
            created_at_ns = int(created_ns)
        except (OSError, ValueError):
            # An unreadable or malformed claim is ambiguous; retain it rather
            # than risking a duplicate upload.
            return False
        if owner_pid <= 0 or owner_pid == os.getpid():
            return False
        expired = (time.time_ns() - created_at_ns) >= int(self.stale_claim_after * 1_000_000_000)
        try:
            os.kill(owner_pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # A process owned by another account may be live; preserve claim.
            return False
        except OSError:
            # Some platforms cannot inspect a foreign PID. Only a clearly old
            # marker may be reclaimed in that case.
            return expired
        return False

    def _mark_upload_request_complete(self, request_id: str) -> None:
        """Commit authoritative completion; legacy .done files are ignored."""
        connection = self._open_claim_lock()
        if connection is None:
            raise OSError("upload command completion lock unavailable")
        committed = False
        try:
            if self._completion_exists(connection, request_id):
                return
            connection.execute(
                "INSERT INTO completed_commands (request_key, completed_at_ns) VALUES (?, ?)",
                (self._request_marker_stem(request_id), time.time_ns()),
            )
            self._commit_completion(connection)
            committed = True
        except sqlite3.Error as exc:
            raise OSError("upload command completion commit failed") from exc
        finally:
            if not committed:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            connection.close()

    def _completion_exists(self, connection: sqlite3.Connection, request_id: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM completed_commands WHERE request_key = ?",
            (self._request_marker_stem(request_id),),
        ).fetchone()
        return row is not None

    @staticmethod
    def _commit_completion(connection: sqlite3.Connection) -> None:
        connection.commit()

    async def handle_event(self, payload: Mapping[str, Any]) -> bool:
        """Dispatch either a mentor message or command without raising."""
        event_type = payload.get("type") if isinstance(payload, Mapping) else None
        if event_type == "mentor_message":
            return await self.handle_message(payload)
        if event_type == "mentor_command":
            return await self.handle_command(payload)
        return False

    async def reconnect_once(self, connector: Callable[[], MaybeAwaitable]) -> bool:
        """Attempt one connection and sleep with injectable exponential backoff."""
        self._last_reconnect_at = self._clock()
        try:
            connected = await _maybe_await(connector())
        except Exception as exc:
            log.warning("student WS reconnect failed type=%s", type(exc).__name__)
            connected = False
        if connected:
            self.reset_reconnect_backoff()
            return True
        delay = self._next_reconnect_delay
        await _maybe_await(self._sleeper(delay))
        self._next_reconnect_delay = min(delay * 2.0, self.reconnect_max)
        return False

    def reset_reconnect_backoff(self) -> None:
        self._next_reconnect_delay = self.reconnect_initial

    @property
    def next_reconnect_delay(self) -> float:
        return self._next_reconnect_delay

    @property
    def last_reconnect_at(self) -> float | None:
        return self._last_reconnect_at
