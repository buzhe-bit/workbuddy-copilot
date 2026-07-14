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
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from ..models import UploadOutcome
from .process_liveness import FileClaimStore
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


async def _call_transport_nonblocking(
    transport: Any,
    async_name: str,
    sync_name: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Prefer an async transport seam and offload legacy sync adapters."""
    async_method = getattr(transport, async_name, None)
    if callable(async_method):
        return await _maybe_await(async_method(*args, **kwargs))
    sync_method = getattr(transport, sync_name, None)
    if not callable(sync_method):
        raise PermanentTransportError(f"transport cannot {sync_name}")
    import asyncio

    value = await asyncio.to_thread(sync_method, *args, **kwargs)
    return await _maybe_await(value)


async def _call_injected_nonblocking(
    handler: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Await native async adapters and offload legacy synchronous ones."""
    if inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
        getattr(handler, "__call__", None)
    ):
        return await _maybe_await(handler(*args, **kwargs))
    import asyncio

    result = await asyncio.to_thread(handler, *args, **kwargs)
    return await _maybe_await(result)


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


def _supports_analysis_commit_cursor(method: Callable[..., Any]) -> bool:
    """Keep old fake/platform adapters working while production uses commit IDs."""
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.name == "after_analysis_id"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _analysis_delivery_id(payload: Mapping[str, Any]) -> int:
    """Use durable commit order; fall back only for old local adapters."""
    try:
        analysis_id = int(payload.get("analysis_id") or 0)
    except (TypeError, ValueError):
        analysis_id = 0
    if analysis_id > 0:
        return analysis_id
    try:
        return int(payload.get("report_id") or 0)
    except (TypeError, ValueError):
        return 0


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
        analysis_handler: Callable[[Mapping[str, Any]], MaybeAwaitable] | None = None,
        analysis_cursor: int = 0,
        transcript_queue: Any | None = None,
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
        self.analysis_handler = analysis_handler
        self.analysis_cursor = max(0, int(analysis_cursor))
        self._inflight_analysis_ids: set[int] = set()
        self.analysis_catchup_exhausted = False
        self.transcript_queue = transcript_queue
        # Rendering and receipt confirmation are deliberately distinct: a
        # failed receipt must retry without showing the same mentor message
        # twice, whereas a failed renderer remains retryable.
        self.rendered_message_ids: set[tuple[str, str]] = set()
        self._pending_message_after_id = 0
        self._inflight_message_keys: set[tuple[str, str]] = set()
        self.handled_request_ids = handled_request_ids if handled_request_ids is not None else set()
        self._inflight_request_ids: set[str] = set()
        self._command_state_dir = self._prepare_command_state_dir()
        self._command_claim_store = FileClaimStore(
            self._command_state_dir,
            process_identity=self.spool.process_identity,
            process_liveness=self.spool.process_liveness,
            claim_path_factory=lambda claim_id: self._command_state_dir / f"{claim_id}.claim",
        )

    async def flush_spool_once(self) -> int:
        """Try each currently pending event and return the accepted count.

        Errors are intentionally swallowed after releasing the claim: a
        temporary failure stays in the spool for a later cycle, and a
        permanent rejection also remains visible for diagnosis/recovery.
        """
        accepted_count = 0
        # Include claimed rows in the ordered view. If another process owns
        # the head row, claim() below fails and the whole cycle stops instead
        # of silently sending a later event out of order.
        for entry in self.spool.pending(include_claimed=True):
            if not self.spool.claim(entry.event_id):
                # Another worker or an ambiguous claim owns the oldest event.
                # Later context must not overtake it on the wire.
                break
            accepted = False
            try:
                try:
                    result = await _call_transport_nonblocking(
                        self.transport,
                        "post_hook_async",
                        "post_hook",
                        entry.payload,
                        event_id=entry.event_id,
                    )
                except (TemporaryNetworkError, PermanentTransportError):
                    result = None
                except Exception as exc:
                    # A custom adapter must not be able to make the resident
                    # loop die. Do not include event content or local paths.
                    log.warning("student hook delivery failed type=%s", type(exc).__name__)
                    result = None
                accepted = isinstance(result, Accepted)
                if (
                    accepted
                    and entry.payload.event == "Stop"
                    and self.transcript_queue is not None
                ):
                    try:
                        report_id = int(result.body.get("report_id") or 0)
                        if report_id <= 0:
                            raise ValueError("accepted Stop response has no report_id")
                        session_id = str(entry.payload.session_id or "").strip()
                        # StudentTransport authenticates and overwrites stale
                        # spool identities on the wire. The durable follow-up
                        # job must use that same authority after account/token
                        # rotation, never the historical hook payload value.
                        student_id = str(
                            getattr(self.transport, "student_id", "") or ""
                        ).strip()
                        if not student_id:
                            raise ValueError("authenticated student identity is required")
                        self.transcript_queue.enqueue(
                            event_id=entry.event_id,
                            report_id=report_id,
                            student_id=student_id,
                            session_id=session_id,
                        )
                    except Exception as exc:
                        log.warning(
                            "Stop transcript job commit failed type=%s",
                            type(exc).__name__,
                        )
                        accepted = False
                if accepted:
                    try:
                        acked = bool(self.spool.ack(entry.event_id))
                    except Exception as exc:
                        log.warning(
                            "student spool ack failed type=%s",
                            type(exc).__name__,
                        )
                        acked = False
                    if acked:
                        accepted_count += 1
                    else:
                        # The server response remains safe to replay under the
                        # same event_id. Do not strand a locally owned claim
                        # when unlink/claim completion was not confirmed.
                        accepted = False
            finally:
                if not accepted:
                    try:
                        self.spool.release_claim(entry.event_id)
                    except Exception as exc:
                        log.warning(
                            "student spool claim release failed type=%s",
                            type(exc).__name__,
                        )
            if not accepted:
                # Preserve delivery FIFO, not merely file-list ordering. A
                # temporary network/ack/job failure remains the head item for
                # the next cycle and blocks later prompt/Stop context.
                break
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
        # A headless process is not a rendering surface. It must not create a
        # false delivered receipt merely because the network path is healthy.
        if self.message_handler is None:
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
        async_pull_method = getattr(self.transport, "get_pending_messages_async", None)
        pull_method = getattr(self.transport, "get_pending_messages", None)
        if not callable(async_pull_method) and not callable(pull_method):
            return 0
        confirmed = 0
        after_id = self._pending_message_after_id
        for _ in range(MAX_PENDING_MESSAGE_PAGES_PER_PULL):
            try:
                method_for_signature = async_pull_method if callable(async_pull_method) else pull_method
                if _supports_pending_cursor(method_for_signature):
                    payloads = await _call_transport_nonblocking(
                        self.transport,
                        "get_pending_messages_async",
                        "get_pending_messages",
                        after_id=after_id,
                    )
                else:
                    # Existing test/platform adapters that predate the cursor
                    # retain their one-page behavior during the migration.
                    payloads = await _call_transport_nonblocking(
                        self.transport,
                        "get_pending_messages_async",
                        "get_pending_messages",
                    )
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
        if self.message_handler is None:
            return 0
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
        result = await _call_transport_nonblocking(
            self.transport,
            "ack_message_async",
            "ack_message",
            message_id,
            student_id=student_id,
        )
        if not isinstance(result, Accepted):
            raise TemporaryNetworkError("message receipt rejected")

    async def handle_analysis(self, payload: Mapping[str, Any]) -> bool:
        """Persist/render one scoped analysis before advancing its cursor."""
        if not isinstance(payload, Mapping) or payload.get("type") not in {
            "analysis",
            "analysis_result",
        }:
            return False
        expected_student = str(getattr(self.transport, "student_id", "") or "").strip()
        student_id = str(payload.get("student_id") or "").strip()
        if not expected_student or student_id != expected_student:
            return False
        delivery_id = _analysis_delivery_id(payload)
        if delivery_id <= 0:
            return False
        if delivery_id <= self.analysis_cursor or delivery_id in self._inflight_analysis_ids:
            return False
        if self.analysis_handler is None:
            return False
        self._inflight_analysis_ids.add(delivery_id)
        try:
            try:
                await _maybe_await(self.analysis_handler(payload))
            except Exception as exc:
                log.warning("analysis handler failed type=%s", type(exc).__name__)
                return False
            self.analysis_cursor = max(self.analysis_cursor, delivery_id)
            return True
        finally:
            self._inflight_analysis_ids.discard(delivery_id)

    async def pull_analysis_catchup(
        self,
        *,
        page_limit: int = 64,
        max_pages: int = 16,
    ) -> int:
        """Drain bounded durable analysis pages without skipping handler failures."""
        if self.analysis_handler is None:
            self.analysis_catchup_exhausted = True
            return 0
        if not callable(getattr(self.transport, "get_recent_analyses_async", None)) and not callable(
            getattr(self.transport, "get_recent_analyses", None)
        ):
            self.analysis_catchup_exhausted = True
            return 0
        self.analysis_catchup_exhausted = False
        bounded_limit = max(1, min(int(page_limit), 100))
        bounded_pages = max(1, min(int(max_pages), 64))
        handled = 0
        for _ in range(bounded_pages):
            requested_cursor = self.analysis_cursor
            try:
                async_method = getattr(
                    self.transport,
                    "get_recent_analyses_async",
                    None,
                )
                sync_method = getattr(self.transport, "get_recent_analyses", None)
                method = async_method if callable(async_method) else sync_method
                cursor_argument = (
                    "after_analysis_id"
                    if callable(method) and _supports_analysis_commit_cursor(method)
                    else "after_report_id"
                )
                page = await _call_transport_nonblocking(
                    self.transport,
                    "get_recent_analyses_async",
                    "get_recent_analyses",
                    **{cursor_argument: requested_cursor, "limit": bounded_limit},
                )
            except Exception as exc:
                log.warning("student analysis backlog failed type=%s", type(exc).__name__)
                return handled
            if not isinstance(page, Mapping):
                return handled
            raw_items = page.get("items", [])
            if not isinstance(raw_items, list):
                return handled
            sortable: list[tuple[int, Mapping[str, Any]]] = []
            for item in raw_items:
                if not isinstance(item, Mapping):
                    continue
                delivery_id = _analysis_delivery_id(item)
                if delivery_id > 0:
                    sortable.append((delivery_id, item))
            for delivery_id, item in sorted(sortable, key=lambda pair: pair[0]):
                if delivery_id <= self.analysis_cursor:
                    continue
                if not await self.handle_analysis(item):
                    # The item remains available from the unchanged server
                    # cursor on the next pull/reconnect.
                    return handled
                handled += 1
            has_more = bool(page.get("has_more"))
            if not has_more:
                self.analysis_catchup_exhausted = True
                return handled
            if self.analysis_cursor <= requested_cursor:
                log.warning("student analysis backlog made no cursor progress")
                return handled
        return handled

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
        except (OSError, sqlite3.Error, ValueError) as exc:
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
            outcome = await _call_injected_nonblocking(
                handler,
                request_id=request_id,
                session_id=(str(payload.get("session_id")) if payload.get("session_id") else None),
            )
            if not isinstance(outcome, UploadOutcome) or not outcome.complete:
                error_code = getattr(outcome, "error_code", "invalid_upload_outcome")
                log.warning("student upload incomplete code=%s", str(error_code)[:80])
                return False
            self._mark_upload_request_complete(request_id)
            self.handled_request_ids.add(request_id)
            return True
        except Exception as exc:
            log.warning("student upload command failed type=%s", type(exc).__name__)
            return False
        finally:
            self._inflight_request_ids.discard(request_id)
            try:
                self._command_claim_store.release(
                    self._request_marker_stem(request_id),
                    expected_identity=self.spool.process_identity,
                )
            except (OSError, sqlite3.Error, ValueError) as exc:
                log.warning("student upload command claim release failed type=%s", type(exc).__name__)

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
        connection = self._open_claim_lock()
        if connection is None:
            return None
        try:
            if self._completion_exists(connection, request_id):
                return None
            claim_id = self._request_marker_stem(request_id)
            return claim_path if self._command_claim_store.acquire(claim_id) else None
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

    def command_claim_health(self) -> dict[str, object]:
        """Expose ambiguous command claims without guessing that work is free."""

        return self._command_claim_store.health()

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
        if event_type in {"analysis", "analysis_result"}:
            return await self.handle_analysis(payload)
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
