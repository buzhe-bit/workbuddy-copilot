"""Windows student floating-view presenter and durable local UI inbox.

The module deliberately has no macOS UI dependency.  ``WindowsStudentView``
is a pure presenter that can be exercised headlessly; the narrow Tk adapter
only translates immutable view snapshots into widgets supplied by the Windows
composition root.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .student_core.transport import TemporaryNetworkError


_CLIENT_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
log = logging.getLogger("copilot.floating_windows")


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)


def dpi_scale(dpi: float | int) -> float:
    """Return the Windows logical-to-physical scale for a positive DPI."""

    resolved = float(dpi)
    if not math.isfinite(resolved) or resolved <= 0:
        raise ValueError("dpi must be positive")
    return resolved / 96.0


def _monitor_for_rect(rect: Rect, monitors: Sequence[Rect]) -> Rect:
    if not monitors:
        raise ValueError("at least one monitor is required")
    center_x, center_y = rect.center
    for monitor in monitors:
        if (
            monitor.x <= center_x <= monitor.right
            and monitor.y <= center_y <= monitor.bottom
        ):
            return monitor

    def distance_squared(monitor: Rect) -> float:
        closest_x = min(max(center_x, monitor.x), monitor.right)
        closest_y = min(max(center_y, monitor.y), monitor.bottom)
        return (center_x - closest_x) ** 2 + (center_y - closest_y) ** 2

    return min(monitors, key=distance_squared)


def panel_rect_for_anchor(
    anchor: Rect,
    panel_size: tuple[float, float],
    monitors: Sequence[Rect],
    *,
    dpi: float | int = 96,
    gap: float = 8.0,
) -> Rect:
    """Place a DPI-scaled panel beside an anchor and keep it on one monitor."""

    raw_width, raw_height = (float(panel_size[0]), float(panel_size[1]))
    if raw_width <= 0 or raw_height <= 0:
        raise ValueError("panel size must be positive")
    monitor = _monitor_for_rect(anchor, monitors)
    scale = dpi_scale(dpi)
    width = min(raw_width * scale, monitor.width)
    height = min(raw_height * scale, monitor.height)
    physical_gap = max(0.0, float(gap)) * scale

    right_x = anchor.right + physical_gap
    left_x = anchor.x - physical_gap - width
    x = right_x if right_x + width <= monitor.right else left_x
    x = min(max(x, monitor.x), monitor.right - width)
    y = min(max(anchor.y, monitor.y), monitor.bottom - height)
    return Rect(x=x, y=y, width=width, height=height)


def clamp_rect_to_monitor(rect: Rect, monitors: Sequence[Rect]) -> Rect:
    """Keep a window fully visible on the nearest available monitor."""

    monitor = _monitor_for_rect(rect, monitors)
    width = min(max(1.0, float(rect.width)), monitor.width)
    height = min(max(1.0, float(rect.height)), monitor.height)
    x = min(max(float(rect.x), monitor.x), monitor.right - width)
    y = min(max(float(rect.y), monitor.y), monitor.bottom - height)
    return Rect(x=x, y=y, width=width, height=height)


DisplayProvider = Callable[[Any], tuple[Sequence[Rect], float]]


def _tk_display_info(window: Any) -> tuple[tuple[Rect, ...], float]:
    """Best-effort virtual-screen fallback for non-Windows/headless Tk."""

    def metric(name: str, fallback: float) -> float:
        method = getattr(window, name, None)
        if not callable(method):
            return fallback
        try:
            return float(method())
        except (TypeError, ValueError, OSError):
            return fallback

    x = metric("winfo_vrootx", 0.0)
    y = metric("winfo_vrooty", 0.0)
    width = metric(
        "winfo_vrootwidth",
        metric("winfo_screenwidth", 1.0),
    )
    height = metric(
        "winfo_vrootheight",
        metric("winfo_screenheight", 1.0),
    )
    dpi = 96.0
    pixels = getattr(window, "winfo_fpixels", None)
    if callable(pixels):
        try:
            measured = float(pixels("1i"))
            if math.isfinite(measured) and measured > 0:
                dpi = measured
        except (TypeError, ValueError, OSError):
            pass
    return (Rect(x, y, max(1.0, width), max(1.0, height)),), dpi


def windows_display_info(window: Any) -> tuple[tuple[Rect, ...], float]:
    """Return Windows monitor work areas and the current window DPI.

    Win32 access stays lazy so importing the Windows presenter remains safe on
    macOS/Linux contract hosts.  If the native APIs are unavailable, Tk's
    virtual root still provides a conservative single-screen boundary.
    """

    fallback_monitors, fallback_dpi = _tk_display_info(window)
    if os.name != "nt":
        return fallback_monitors, fallback_dpi
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)

        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        monitor_callback = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.RECT),
            wintypes.LPARAM,
        )
        user32.GetMonitorInfoW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(MonitorInfo),
        ]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.EnumDisplayMonitors.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.RECT),
            monitor_callback,
            wintypes.LPARAM,
        ]
        user32.EnumDisplayMonitors.restype = wintypes.BOOL
        monitors: list[Rect] = []

        @monitor_callback
        def collect(
            monitor_handle: Any,
            _device_context: Any,
            _raw_rect: Any,
            _data: Any,
        ) -> bool:
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(MonitorInfo)
            if not user32.GetMonitorInfoW(monitor_handle, ctypes.byref(info)):
                return True
            work = info.rcWork
            monitors.append(
                Rect(
                    float(work.left),
                    float(work.top),
                    float(work.right - work.left),
                    float(work.bottom - work.top),
                )
            )
            return True

        if not user32.EnumDisplayMonitors(None, None, collect, 0) or not monitors:
            return fallback_monitors, fallback_dpi
        dpi = fallback_dpi
        get_dpi = getattr(user32, "GetDpiForWindow", None)
        window_id = getattr(window, "winfo_id", None)
        if callable(get_dpi) and callable(window_id):
            try:
                get_dpi.argtypes = [wintypes.HWND]
                get_dpi.restype = wintypes.UINT
                measured = float(get_dpi(wintypes.HWND(int(window_id()))))
                if math.isfinite(measured) and measured > 0:
                    dpi = measured
            except (TypeError, ValueError, OSError):
                pass
        return tuple(monitors), dpi
    except (AttributeError, OSError, TypeError, ValueError):
        return fallback_monitors, fallback_dpi


@dataclass(frozen=True)
class StoredWindowsItem:
    kind: str
    key: str
    payload: Mapping[str, Any]
    state: str
    created_at_ns: int
    terminal_at_ns: int | None
    read: bool


@dataclass(frozen=True)
class StoreUpsert:
    created: bool
    item: StoredWindowsItem


@dataclass(frozen=True)
class StoredWindowsAsk:
    client_request_id: str
    session_id: str
    question: str
    status: str
    ask_id: int | None
    answer: str
    error_code: str
    feedback: str
    feedback_status: str
    feedback_error_code: str
    created_at_ns: int
    updated_at_ns: int


class WindowsMessageStore:
    """Identity-scoped SQLite inbox for mentor messages and analyses.

    Only terminal rows are capacity-pruned.  Mentor rows become terminal after
    server acknowledgement (``acked``); analyses become terminal after a
    successful UI render (``rendered``).  Every pending delivery state remains
    durable regardless of backlog size.
    """

    DEFAULT_TERMINAL_LIMIT = 300
    _MENTOR_RANK = {
        "pending": 0,
        "unrendered": 1,
        "ack_pending": 2,
        "acked": 3,
    }
    _ANALYSIS_RANK = {"pending": 0, "unrendered": 1, "rendered": 2}
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        student_id: str,
        terminal_limit: int = DEFAULT_TERMINAL_LIMIT,
        clock_ns: Callable[[], int] | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.student_id = str(student_id or "").strip()
        if not self.student_id:
            raise ValueError("student identity is required")
        self.terminal_limit = int(terminal_limit)
        if self.terminal_limit < 1:
            raise ValueError("terminal_limit must be positive")
        self._clock_ns = clock_ns or time.time_ns
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("Windows message store must not be a symlink")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink():
            raise ValueError("Windows message store must not be a symlink")
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS windows_ui_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS windows_ui_mentor_messages (
                    message_id TEXT PRIMARY KEY,
                    student_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    delivery_state TEXT NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    terminal_at_ns INTEGER,
                    read_at_ns INTEGER
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS windows_ui_analyses (
                    local_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    analysis_id INTEGER,
                    report_id INTEGER NOT NULL UNIQUE,
                    student_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    delivery_state TEXT NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    terminal_at_ns INTEGER,
                    read_at_ns INTEGER
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_windows_ui_analysis_id
                ON windows_ui_analyses(analysis_id)
                WHERE analysis_id IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS windows_ui_current_ask (
                    student_id TEXT PRIMARY KEY,
                    client_request_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    status TEXT NOT NULL,
                    ask_id INTEGER,
                    answer TEXT NOT NULL,
                    error_code TEXT NOT NULL,
                    feedback TEXT NOT NULL,
                    feedback_status TEXT NOT NULL,
                    feedback_error_code TEXT NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT value FROM windows_ui_state WHERE key = 'student_id'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO windows_ui_state(key, value) VALUES('student_id', ?)",
                    (self.student_id,),
                )
            elif str(row["value"]) != self.student_id:
                raise ValueError("Windows message store identity mismatch")
            connection.commit()
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    @staticmethod
    def _encoded(payload: Mapping[str, Any]) -> str:
        return json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _validate_identity(self, payload: Mapping[str, Any]) -> None:
        if str(payload.get("student_id") or "").strip() != self.student_id:
            raise ValueError("message student identity mismatch")

    @staticmethod
    def _positive_int(value: Any, *, field: str, optional: bool = False) -> int | None:
        if optional and value in (None, ""):
            return None
        try:
            resolved = int(value)
        except (TypeError, ValueError):
            resolved = 0
        if resolved <= 0:
            raise ValueError(f"{field} is required")
        return resolved

    @staticmethod
    def _merged_state(current: str, requested: str, ranks: Mapping[str, int]) -> str:
        if requested not in ranks:
            raise ValueError("invalid delivery state")
        if current not in ranks:
            raise ValueError("invalid persisted delivery state")
        return requested if ranks[requested] > ranks[current] else current

    def upsert_mentor(
        self,
        payload: Mapping[str, Any],
        *,
        state: str = "unrendered",
    ) -> StoreUpsert:
        if not isinstance(payload, Mapping) or payload.get("type") != "mentor_message":
            raise ValueError("invalid mentor message")
        self._validate_identity(payload)
        message_id = str(payload.get("message_id") or "").strip()
        if not message_id or len(message_id) > 512:
            raise ValueError("mentor message_id is required")
        if state not in self._MENTOR_RANK:
            raise ValueError("invalid mentor delivery state")
        now = int(self._clock_ns())
        encoded = self._encoded(payload)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM windows_ui_mentor_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            created = row is None
            if row is None:
                terminal_at = now if state == "acked" else None
                connection.execute(
                    """
                    INSERT INTO windows_ui_mentor_messages(
                        message_id, student_id, payload_json, delivery_state,
                        created_at_ns, terminal_at_ns, read_at_ns
                    ) VALUES(?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (message_id, self.student_id, encoded, state, now, terminal_at),
                )
            else:
                if str(row["student_id"]) != self.student_id:
                    raise ValueError("mentor message identity collision")
                if str(row["payload_json"]) != encoded:
                    raise ValueError("mentor message payload collision")
                merged = self._merged_state(
                    str(row["delivery_state"]), state, self._MENTOR_RANK
                )
                terminal_at = row["terminal_at_ns"]
                if merged == "acked" and terminal_at is None:
                    terminal_at = now
                connection.execute(
                    """
                    UPDATE windows_ui_mentor_messages
                    SET payload_json = ?, delivery_state = ?, terminal_at_ns = ?
                    WHERE message_id = ?
                    """,
                    (encoded, merged, terminal_at, message_id),
                )
            updated = connection.execute(
                "SELECT * FROM windows_ui_mentor_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            assert updated is not None
            item = self._mentor_item(updated)
            self._prune_mentor(connection)
            connection.commit()
            return StoreUpsert(created=created, item=item)
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def upsert_analysis(
        self,
        payload: Mapping[str, Any],
        *,
        state: str = "unrendered",
    ) -> StoreUpsert:
        if not isinstance(payload, Mapping) or payload.get("type") not in {
            "analysis",
            "analysis_result",
        }:
            raise ValueError("invalid analysis")
        self._validate_identity(payload)
        report_id = self._positive_int(payload.get("report_id"), field="analysis report_id")
        analysis_id = self._positive_int(
            payload.get("analysis_id"), field="analysis_id", optional=True
        )
        assert report_id is not None
        if state not in self._ANALYSIS_RANK:
            raise ValueError("invalid analysis delivery state")
        encoded = self._encoded(payload)
        now = int(self._clock_ns())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if analysis_id is None:
                rows = connection.execute(
                    "SELECT * FROM windows_ui_analyses WHERE report_id = ?",
                    (report_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM windows_ui_analyses
                    WHERE report_id = ? OR analysis_id = ?
                    """,
                    (report_id, analysis_id),
                ).fetchall()
            if len(rows) > 1:
                raise ValueError("analysis identity collision")
            row = rows[0] if rows else None
            created = row is None
            if row is None:
                terminal_at = now if state == "rendered" else None
                connection.execute(
                    """
                    INSERT INTO windows_ui_analyses(
                        analysis_id, report_id, student_id, payload_json,
                        delivery_state, created_at_ns, terminal_at_ns, read_at_ns
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        analysis_id,
                        report_id,
                        self.student_id,
                        encoded,
                        state,
                        now,
                        terminal_at,
                    ),
                )
            else:
                if str(row["student_id"]) != self.student_id:
                    raise ValueError("analysis identity collision")
                if int(row["report_id"]) != report_id:
                    raise ValueError("analysis identity collision")
                if str(row["payload_json"]) != encoded:
                    raise ValueError("analysis payload collision")
                stored_analysis_id = (
                    int(row["analysis_id"]) if row["analysis_id"] is not None else None
                )
                if (
                    stored_analysis_id is not None
                    and analysis_id is not None
                    and stored_analysis_id != analysis_id
                ):
                    raise ValueError("analysis identity collision")
                resolved_analysis_id = stored_analysis_id or analysis_id
                merged = self._merged_state(
                    str(row["delivery_state"]), state, self._ANALYSIS_RANK
                )
                terminal_at = row["terminal_at_ns"]
                if merged == "rendered" and terminal_at is None:
                    terminal_at = now
                connection.execute(
                    """
                    UPDATE windows_ui_analyses
                    SET analysis_id = ?, payload_json = ?, delivery_state = ?,
                        terminal_at_ns = ?
                    WHERE local_id = ?
                    """,
                    (
                        resolved_analysis_id,
                        encoded,
                        merged,
                        terminal_at,
                        int(row["local_id"]),
                    ),
                )
            updated = connection.execute(
                "SELECT * FROM windows_ui_analyses WHERE report_id = ?",
                (report_id,),
            ).fetchone()
            assert updated is not None
            item = self._analysis_item(updated)
            self._prune_analysis(connection)
            connection.commit()
            return StoreUpsert(created=created, item=item)
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def mark_mentor_state(self, message_id: str, state: str) -> bool:
        if state not in self._MENTOR_RANK:
            raise ValueError("invalid mentor delivery state")
        resolved_id = str(message_id or "").strip()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT delivery_state, terminal_at_ns FROM windows_ui_mentor_messages WHERE message_id = ?",
                (resolved_id,),
            ).fetchone()
            if row is None:
                raise KeyError(resolved_id)
            merged = self._merged_state(
                str(row["delivery_state"]), state, self._MENTOR_RANK
            )
            changed = merged != str(row["delivery_state"])
            terminal_at = row["terminal_at_ns"]
            if merged == "acked" and terminal_at is None:
                terminal_at = int(self._clock_ns())
            connection.execute(
                """
                UPDATE windows_ui_mentor_messages
                SET delivery_state = ?, terminal_at_ns = ? WHERE message_id = ?
                """,
                (merged, terminal_at, resolved_id),
            )
            self._prune_mentor(connection)
            connection.commit()
            return changed
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def reconcile_mentor_receipts(self, receipt_ledger: Any) -> int:
        """Promote local rows only when the shared receipt ledger says acked.

        Renderer success is intentionally insufficient.  The composition root
        can call this at startup and periodically after Coordinator receipt
        retries; duplicate reconciliation is a no-op.
        """

        status = getattr(receipt_ledger, "status", None)
        if not callable(status):
            raise TypeError("receipt ledger must provide status")
        with self._connect() as connection:
            candidates = [
                str(row["message_id"])
                for row in connection.execute(
                    """
                    SELECT message_id FROM windows_ui_mentor_messages
                    WHERE student_id = ? AND delivery_state = 'ack_pending'
                    ORDER BY created_at_ns ASC, message_id ASC
                    """,
                    (self.student_id,),
                ).fetchall()
            ]
        confirmed = [
            message_id
            for message_id in candidates
            if status(self.student_id, message_id) == "acked"
        ]
        if not confirmed:
            return 0

        now = int(self._clock_ns())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = 0
            for message_id in confirmed:
                cursor = connection.execute(
                    """
                    UPDATE windows_ui_mentor_messages
                    SET delivery_state = 'acked',
                        terminal_at_ns = COALESCE(terminal_at_ns, ?)
                    WHERE student_id = ? AND message_id = ?
                      AND delivery_state = 'ack_pending'
                    """,
                    (now, self.student_id, message_id),
                )
                changed += max(0, int(cursor.rowcount))
            self._prune_mentor(connection)
            connection.commit()
            return changed
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def mark_analysis_state(
        self,
        *,
        state: str,
        analysis_id: int | None = None,
        report_id: int | None = None,
    ) -> bool:
        if state not in self._ANALYSIS_RANK:
            raise ValueError("invalid analysis delivery state")
        if analysis_id is None and report_id is None:
            raise ValueError("analysis identity is required")
        field = "analysis_id" if analysis_id is not None else "report_id"
        value = self._positive_int(
            analysis_id if analysis_id is not None else report_id,
            field=field,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT local_id, delivery_state, terminal_at_ns FROM windows_ui_analyses WHERE {field} = ?",
                (value,),
            ).fetchone()
            if row is None:
                raise KeyError(str(value))
            merged = self._merged_state(
                str(row["delivery_state"]), state, self._ANALYSIS_RANK
            )
            changed = merged != str(row["delivery_state"])
            terminal_at = row["terminal_at_ns"]
            if merged == "rendered" and terminal_at is None:
                terminal_at = int(self._clock_ns())
            connection.execute(
                """
                UPDATE windows_ui_analyses SET delivery_state = ?, terminal_at_ns = ?
                WHERE local_id = ?
                """,
                (merged, terminal_at, int(row["local_id"])),
            )
            self._prune_analysis(connection)
            connection.commit()
            return changed
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    @staticmethod
    def _ask_item(row: sqlite3.Row) -> StoredWindowsAsk:
        return StoredWindowsAsk(
            client_request_id=str(row["client_request_id"]),
            session_id=str(row["session_id"]),
            question=str(row["question"]),
            status=str(row["status"]),
            ask_id=(int(row["ask_id"]) if row["ask_id"] is not None else None),
            answer=str(row["answer"]),
            error_code=str(row["error_code"]),
            feedback=str(row["feedback"]),
            feedback_status=str(row["feedback_status"]),
            feedback_error_code=str(row["feedback_error_code"]),
            created_at_ns=int(row["created_at_ns"]),
            updated_at_ns=int(row["updated_at_ns"]),
        )

    def load_ask(self) -> StoredWindowsAsk | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM windows_ui_current_ask WHERE student_id = ?",
                (self.student_id,),
            ).fetchone()
        return self._ask_item(row) if row is not None else None

    def begin_ask(
        self,
        *,
        client_request_id: str,
        session_id: str,
        question: str,
    ) -> StoredWindowsAsk:
        request_id = str(client_request_id or "").strip()
        resolved_session = str(session_id or "").strip()
        resolved_question = str(question or "").strip()
        if _CLIENT_REQUEST_ID.fullmatch(request_id) is None:
            raise ValueError("invalid client_request_id")
        if not resolved_session or len(resolved_session) > 1024:
            raise ValueError("ask session_id is required")
        if not resolved_question or len(resolved_question) > 20_000:
            raise ValueError("ask question is required")
        now = int(self._clock_ns())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM windows_ui_current_ask WHERE student_id = ?",
                (self.student_id,),
            ).fetchone()
            if (
                current is not None
                and str(current["client_request_id"]) == request_id
                and str(current["status"]) != "pending"
            ):
                raise RuntimeError("client_request_id is already completed")
            if current is not None and str(current["status"]) == "pending":
                if (
                    str(current["client_request_id"]) == request_id
                    and str(current["session_id"]) == resolved_session
                    and str(current["question"]) == resolved_question
                ):
                    connection.commit()
                    return self._ask_item(current)
                raise RuntimeError("an ask request is already pending")
            created_at = now
            connection.execute(
                """
                INSERT INTO windows_ui_current_ask(
                    student_id, client_request_id, session_id, question, status,
                    ask_id, answer, error_code, feedback, feedback_status,
                    feedback_error_code, created_at_ns, updated_at_ns
                ) VALUES(?, ?, ?, ?, 'pending', NULL, '', '', '', 'idle', '', ?, ?)
                ON CONFLICT(student_id) DO UPDATE SET
                    client_request_id = excluded.client_request_id,
                    session_id = excluded.session_id,
                    question = excluded.question,
                    status = 'pending',
                    ask_id = NULL,
                    answer = '',
                    error_code = '',
                    feedback = '',
                    feedback_status = 'idle',
                    feedback_error_code = '',
                    created_at_ns = excluded.created_at_ns,
                    updated_at_ns = excluded.updated_at_ns
                """,
                (
                    self.student_id,
                    request_id,
                    resolved_session,
                    resolved_question,
                    created_at,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM windows_ui_current_ask WHERE student_id = ?",
                (self.student_id,),
            ).fetchone()
            assert row is not None
            connection.commit()
            return self._ask_item(row)
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def resolve_ask(
        self,
        *,
        client_request_id: str,
        status: str,
        ask_id: int | None,
        answer: str = "",
        error_code: str = "",
    ) -> StoredWindowsAsk:
        resolved_status = str(status or "").strip()
        if resolved_status not in {"answered", "degraded", "failed"}:
            raise ValueError("invalid ask status")
        resolved_ask_id = int(ask_id) if ask_id is not None else None
        if resolved_ask_id is not None and resolved_ask_id <= 0:
            raise ValueError("ask_id must be positive")
        if resolved_status in {"answered", "degraded"} and resolved_ask_id is None:
            raise ValueError("terminal answer requires ask_id")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM windows_ui_current_ask WHERE student_id = ?",
                (self.student_id,),
            ).fetchone()
            if current is None or str(current["client_request_id"]) != str(
                client_request_id
            ):
                raise KeyError(str(client_request_id))
            if str(current["status"]) != "pending":
                if (
                    str(current["status"]) == resolved_status
                    and current["ask_id"] == resolved_ask_id
                    and str(current["answer"]) == str(answer or "")
                    and str(current["error_code"]) == str(error_code or "")
                ):
                    connection.commit()
                    return self._ask_item(current)
                raise RuntimeError("ask request is already terminal")
            connection.execute(
                """
                UPDATE windows_ui_current_ask
                SET status = ?, ask_id = ?, answer = ?, error_code = ?,
                    feedback = '', feedback_status = 'idle',
                    feedback_error_code = '', updated_at_ns = ?
                WHERE student_id = ?
                """,
                (
                    resolved_status,
                    resolved_ask_id,
                    str(answer or ""),
                    str(error_code or ""),
                    int(self._clock_ns()),
                    self.student_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM windows_ui_current_ask WHERE student_id = ?",
                (self.student_id,),
            ).fetchone()
            assert row is not None
            connection.commit()
            return self._ask_item(row)
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def begin_feedback(
        self,
        *,
        client_request_id: str,
        feedback: str,
    ) -> StoredWindowsAsk:
        resolved_feedback = str(feedback or "").strip()
        if resolved_feedback not in {"helpful", "unresolved"}:
            raise ValueError("invalid ask feedback")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM windows_ui_current_ask WHERE student_id = ?",
                (self.student_id,),
            ).fetchone()
            if current is None or str(current["client_request_id"]) != str(
                client_request_id
            ):
                raise KeyError(str(client_request_id))
            if str(current["status"]) not in {"answered", "degraded"}:
                raise RuntimeError("feedback requires a persisted answer")
            current_feedback_status = str(current["feedback_status"])
            if current_feedback_status == "pending":
                if str(current["feedback"]) == resolved_feedback:
                    connection.commit()
                    return self._ask_item(current)
                raise RuntimeError("ask feedback is already pending")
            if current_feedback_status == "sent":
                raise RuntimeError("ask feedback was already sent")
            connection.execute(
                """
                UPDATE windows_ui_current_ask
                SET feedback = ?, feedback_status = 'pending',
                    feedback_error_code = '', updated_at_ns = ?
                WHERE student_id = ?
                """,
                (
                    resolved_feedback,
                    int(self._clock_ns()),
                    self.student_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM windows_ui_current_ask WHERE student_id = ?",
                (self.student_id,),
            ).fetchone()
            assert row is not None
            connection.commit()
            return self._ask_item(row)
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def finish_feedback(
        self,
        *,
        client_request_id: str,
        success: bool,
        error_code: str = "",
    ) -> StoredWindowsAsk:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM windows_ui_current_ask WHERE student_id = ?",
                (self.student_id,),
            ).fetchone()
            if current is None or str(current["client_request_id"]) != str(
                client_request_id
            ):
                raise KeyError(str(client_request_id))
            desired = "sent" if success else "failed"
            if str(current["feedback_status"]) != "pending":
                if str(current["feedback_status"]) == desired:
                    connection.commit()
                    return self._ask_item(current)
                raise RuntimeError("no ask feedback is pending")
            connection.execute(
                """
                UPDATE windows_ui_current_ask
                SET feedback_status = ?, feedback_error_code = ?, updated_at_ns = ?
                WHERE student_id = ?
                """,
                (
                    desired,
                    "" if success else str(error_code or "feedback_failed"),
                    int(self._clock_ns()),
                    self.student_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM windows_ui_current_ask WHERE student_id = ?",
                (self.student_id,),
            ).fetchone()
            assert row is not None
            connection.commit()
            return self._ask_item(row)
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def _prune_mentor(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT message_id FROM windows_ui_mentor_messages
            WHERE delivery_state = 'acked'
            ORDER BY terminal_at_ns DESC, message_id DESC
            LIMIT -1 OFFSET ?
            """,
            (self.terminal_limit,),
        ).fetchall()
        if rows:
            connection.executemany(
                "DELETE FROM windows_ui_mentor_messages WHERE message_id = ?",
                [(str(row["message_id"]),) for row in rows],
            )

    def _prune_analysis(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT local_id FROM windows_ui_analyses
            WHERE delivery_state = 'rendered'
            ORDER BY terminal_at_ns DESC,
                     COALESCE(analysis_id, report_id) DESC,
                     local_id DESC
            LIMIT -1 OFFSET ?
            """,
            (self.terminal_limit,),
        ).fetchall()
        if rows:
            connection.executemany(
                "DELETE FROM windows_ui_analyses WHERE local_id = ?",
                [(int(row["local_id"]),) for row in rows],
            )

    @staticmethod
    def _mentor_item(row: sqlite3.Row) -> StoredWindowsItem:
        return StoredWindowsItem(
            kind="mentor",
            key=str(row["message_id"]),
            payload=json.loads(str(row["payload_json"])),
            state=str(row["delivery_state"]),
            created_at_ns=int(row["created_at_ns"]),
            terminal_at_ns=(
                int(row["terminal_at_ns"])
                if row["terminal_at_ns"] is not None
                else None
            ),
            read=row["read_at_ns"] is not None,
        )

    @staticmethod
    def _analysis_item(row: sqlite3.Row) -> StoredWindowsItem:
        analysis_id = row["analysis_id"]
        key = (
            f"analysis:{int(analysis_id)}"
            if analysis_id is not None
            else f"report:{int(row['report_id'])}"
        )
        return StoredWindowsItem(
            kind="analysis",
            key=key,
            payload=json.loads(str(row["payload_json"])),
            state=str(row["delivery_state"]),
            created_at_ns=int(row["created_at_ns"]),
            terminal_at_ns=(
                int(row["terminal_at_ns"])
                if row["terminal_at_ns"] is not None
                else None
            ),
            read=row["read_at_ns"] is not None,
        )

    def list_mentor(self) -> list[StoredWindowsItem]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM windows_ui_mentor_messages
                WHERE student_id = ?
                ORDER BY created_at_ns ASC, message_id ASC
                """,
                (self.student_id,),
            ).fetchall()
        return [self._mentor_item(row) for row in rows]

    def list_analysis(self) -> list[StoredWindowsItem]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM windows_ui_analyses
                WHERE student_id = ?
                ORDER BY created_at_ns ASC,
                         COALESCE(analysis_id, report_id) ASC,
                         local_id ASC
                """,
                (self.student_id,),
            ).fetchall()
        return [self._analysis_item(row) for row in rows]

    @property
    def unread_count(self) -> int:
        with self._connect() as connection:
            mentor = connection.execute(
                """
                SELECT COUNT(*) FROM windows_ui_mentor_messages
                WHERE student_id = ? AND read_at_ns IS NULL
                """,
                (self.student_id,),
            ).fetchone()[0]
            analyses = connection.execute(
                """
                SELECT COUNT(*) FROM windows_ui_analyses
                WHERE student_id = ? AND read_at_ns IS NULL
                """,
                (self.student_id,),
            ).fetchone()[0]
        return int(mentor) + int(analyses)

    def mark_all_read(self) -> int:
        now = int(self._clock_ns())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            mentor_cursor = connection.execute(
                """
                UPDATE windows_ui_mentor_messages SET read_at_ns = ?
                WHERE student_id = ? AND read_at_ns IS NULL
                """,
                (now, self.student_id),
            )
            analysis_cursor = connection.execute(
                """
                UPDATE windows_ui_analyses SET read_at_ns = ?
                WHERE student_id = ? AND read_at_ns IS NULL
                """,
                (now, self.student_id),
            )
            connection.commit()
            return int(mentor_cursor.rowcount) + int(analysis_cursor.rowcount)
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()


@dataclass(frozen=True)
class SessionOption:
    session_id: str
    title: str


@dataclass(frozen=True)
class AskViewState:
    status: str = "idle"
    session_id: str = ""
    question: str = ""
    answer: str = ""
    ask_id: int | None = None
    client_request_id: str = ""
    error_code: str = ""
    feedback: str = ""
    feedback_status: str = "idle"
    feedback_error_code: str = ""


@dataclass(frozen=True)
class WindowsViewState:
    expanded: bool
    unread_count: int
    mentor_messages: tuple[StoredWindowsItem, ...]
    analyses: tuple[StoredWindowsItem, ...]
    sessions: tuple[SessionOption, ...]
    selected_session_id: str | None
    session_selection_source: str
    session_selection_required: bool
    ask: AskViewState
    focus_target: str | None


class WindowsViewRenderer(Protocol):
    def render(self, state: WindowsViewState) -> None: ...


class WindowsStudentView:
    """Headless-testable Windows view model and durable render presenter."""

    _ASK_TERMINAL = {"answered", "degraded", "failed"}
    _FEEDBACK = {"helpful", "unresolved"}

    def __init__(
        self,
        store: WindowsMessageStore,
        *,
        renderer: WindowsViewRenderer | None = None,
    ) -> None:
        self.store = store
        self.renderer = renderer
        self._expanded = False
        self._sessions: tuple[SessionOption, ...] = ()
        self._selected_session_id: str | None = None
        self._session_selection_source = "none"
        self._ask = self._ask_state(self.store.load_ask())
        self._focus_target: str | None = None
        self.state = self._snapshot()

    @staticmethod
    def _ask_state(stored: StoredWindowsAsk | None) -> AskViewState:
        if stored is None:
            return AskViewState()
        return AskViewState(
            status=stored.status,
            session_id=stored.session_id,
            question=stored.question,
            answer=stored.answer,
            ask_id=stored.ask_id,
            client_request_id=stored.client_request_id,
            error_code=stored.error_code,
            feedback=stored.feedback,
            feedback_status=stored.feedback_status,
            feedback_error_code=stored.feedback_error_code,
        )

    def _snapshot(self) -> WindowsViewState:
        session_ids = {session.session_id for session in self._sessions}
        selected = (
            self._selected_session_id
            if self._selected_session_id in session_ids
            else None
        )
        return WindowsViewState(
            expanded=self._expanded,
            unread_count=self.store.unread_count,
            mentor_messages=tuple(self.store.list_mentor()),
            analyses=tuple(self.store.list_analysis()),
            sessions=self._sessions,
            selected_session_id=selected,
            session_selection_source=(
                self._session_selection_source if selected is not None else "none"
            ),
            session_selection_required=selected is None,
            ask=self._ask,
            focus_target=self._focus_target,
        )

    def _refresh(self, *, render: bool) -> WindowsViewState:
        self.state = self._snapshot()
        if render and self.renderer is not None:
            self.renderer.render(self.state)
        return self.state

    def restore(self) -> WindowsViewState:
        self._ask = self._ask_state(self.store.load_ask())
        return self._refresh(render=True)

    def present_mentor_message(self, payload: Mapping[str, Any]) -> bool:
        result = self.store.upsert_mentor(payload, state="unrendered")
        if self.renderer is None:
            raise RuntimeError("Windows UI renderer is unavailable")
        # Render the complete keyed snapshot. Replays replace the model rather
        # than append a second card, including after a process restart.
        self._refresh(render=True)
        self.store.mark_mentor_state(
            str(payload.get("message_id") or ""), "ack_pending"
        )
        if self._expanded:
            self.store.mark_all_read()
        self._refresh(render=False)
        return result.created

    def present_analysis(self, payload: Mapping[str, Any]) -> bool:
        result = self.store.upsert_analysis(payload, state="unrendered")
        if self.renderer is None:
            raise RuntimeError("Windows UI renderer is unavailable")
        self._refresh(render=True)
        analysis_id = payload.get("analysis_id")
        report_id = payload.get("report_id")
        self.store.mark_analysis_state(
            state="rendered",
            analysis_id=(int(analysis_id) if analysis_id not in (None, "") else None),
            report_id=(int(report_id) if report_id not in (None, "") else None),
        )
        if self._expanded:
            self.store.mark_all_read()
        self._refresh(render=False)
        return result.created

    def open_panel(self) -> None:
        was_expanded = self._expanded
        self._expanded = True
        try:
            self._refresh(render=True)
        except BaseException:
            self._expanded = was_expanded
            self._refresh(render=False)
            raise
        self.store.mark_all_read()
        self._refresh(render=False)

    def close_panel(self) -> None:
        was_expanded = self._expanded
        self._expanded = False
        try:
            self._refresh(render=True)
        except BaseException:
            self._expanded = was_expanded
            self._refresh(render=False)
            raise

    def toggle_panel(self) -> None:
        if self._expanded:
            self.close_panel()
        else:
            self.open_panel()

    def update_sessions(
        self,
        sessions: Sequence[Mapping[str, Any]],
        *,
        active_session_id: str | None = None,
        active_reliable: bool = False,
    ) -> None:
        options: list[SessionOption] = []
        seen: set[str] = set()
        for raw in sessions:
            session_id = str(raw.get("session_id") or "").strip()
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            options.append(
                SessionOption(
                    session_id=session_id,
                    title=str(raw.get("title") or raw.get("session_title") or session_id),
                )
            )
        self._sessions = tuple(options)
        available = {option.session_id for option in options}
        active = str(active_session_id or "").strip()
        if active_reliable and active in available:
            self._selected_session_id = active
            self._session_selection_source = "reliable_active"
        elif self._selected_session_id not in available:
            # Never infer current from recency.  If WorkBuddy cannot prove an
            # active session, the student must choose one explicitly.
            self._selected_session_id = None
            self._session_selection_source = "none"
        self._refresh(render=True)

    def select_session(self, session_id: str) -> None:
        resolved = str(session_id or "").strip()
        if resolved not in {option.session_id for option in self._sessions}:
            raise ValueError("unknown session")
        self._selected_session_id = resolved
        self._session_selection_source = "manual"
        self._refresh(render=True)

    def begin_ask(
        self,
        question: str,
        *,
        client_request_id: str | None = None,
    ) -> dict[str, Any]:
        if self._ask.status == "pending":
            raise RuntimeError("an ask request is already pending")
        selected = self.state.selected_session_id
        if selected is None:
            raise ValueError("a reliable or manually selected session is required")
        resolved_question = str(question or "").strip()
        if not resolved_question:
            raise ValueError("question is required")
        request_id = str(client_request_id or uuid.uuid4()).strip()
        if not request_id:
            raise ValueError("client_request_id is required")
        stored = self.store.begin_ask(
            client_request_id=request_id,
            session_id=selected,
            question=resolved_question,
        )
        self._ask = self._ask_state(stored)
        self._refresh(render=True)
        return {
            "session_id": selected,
            "question": resolved_question,
            "client_request_id": request_id,
        }

    def resolve_ask(
        self,
        status: str,
        *,
        ask_id: int | None = None,
        answer: str = "",
        error_code: str = "",
    ) -> None:
        if self._ask.status != "pending":
            raise RuntimeError("no ask request is pending")
        if status not in self._ASK_TERMINAL:
            raise ValueError("invalid ask status")
        resolved_ask_id: int | None = None
        if ask_id is not None:
            resolved_ask_id = int(ask_id)
            if resolved_ask_id <= 0:
                raise ValueError("ask_id must be positive")
        if status in {"answered", "degraded"} and resolved_ask_id is None:
            raise ValueError("terminal answer requires ask_id")
        stored = self.store.resolve_ask(
            client_request_id=self._ask.client_request_id,
            status=status,
            ask_id=resolved_ask_id,
            answer=str(answer or ""),
            error_code=str(error_code or ""),
        )
        self._ask = self._ask_state(stored)
        self._refresh(render=True)

    def begin_feedback(self, feedback: str) -> dict[str, Any]:
        resolved = str(feedback or "").strip()
        if resolved not in self._FEEDBACK:
            raise ValueError("invalid ask feedback")
        if self._ask.status not in {"answered", "degraded"} or self._ask.ask_id is None:
            raise RuntimeError("feedback requires a persisted answer")
        if self._ask.feedback_status == "pending":
            raise RuntimeError("ask feedback is already pending")
        if self._ask.feedback_status == "sent":
            raise RuntimeError("ask feedback was already sent")
        stored = self.store.begin_feedback(
            client_request_id=self._ask.client_request_id,
            feedback=resolved,
        )
        self._ask = self._ask_state(stored)
        self._refresh(render=True)
        return {"ask_id": self._ask.ask_id, "feedback": resolved}

    def finish_feedback(self, *, success: bool, error_code: str = "") -> None:
        if self._ask.feedback_status != "pending":
            raise RuntimeError("no ask feedback is pending")
        stored = self.store.finish_feedback(
            client_request_id=self._ask.client_request_id,
            success=success,
            error_code=error_code,
        )
        self._ask = self._ask_state(stored)
        self._refresh(render=True)

    def pending_ask_query(self) -> dict[str, str]:
        """Return the recovery lookup, never a replacement POST request."""

        if self._ask.status != "pending" or not self._ask.client_request_id:
            raise RuntimeError("no ask request is pending")
        return {
            "action": "query",
            "client_request_id": self._ask.client_request_id,
            "session_id": self._ask.session_id,
        }

    def focus_ask_input(self) -> None:
        self._focus_target = "ask_input"
        self._refresh(render=True)
        self._focus_target = None
        self._refresh(render=False)


class TkWindowsStudentAdapter:
    """Narrow synchronous adapter for Tk widgets owned by the UI main thread.

    Widget creation and the cross-thread bridge belong to ``start_windows_client``.
    Keeping them outside this class makes the presenter testable without a
    display server and prevents accidental Tk calls from the Agent loop.
    """

    def __init__(
        self,
        *,
        icon_window: Any,
        panel_window: Any,
        unread_badge: Any,
        content_widget: Any,
        ask_entry: Any | None = None,
        drag_surface: Any | None = None,
        session_selector: Any | None = None,
        ask_status_widget: Any | None = None,
        ask_send_button: Any | None = None,
        helpful_button: Any | None = None,
        unresolved_button: Any | None = None,
        on_toggle: Callable[[], Any] | None = None,
        on_session_selected: Callable[[str], Any] | None = None,
        on_ask_submit: Callable[[str], Any] | None = None,
        on_feedback: Callable[[str], Any] | None = None,
        on_drag_release: Callable[[], Any] | None = None,
        on_callback_error: Callable[[Exception], Any] | None = None,
    ) -> None:
        self.icon_window = icon_window
        self.panel_window = panel_window
        self.unread_badge = unread_badge
        self.content_widget = content_widget
        self.ask_entry = ask_entry
        self.drag_surface = drag_surface or icon_window
        self.session_selector = session_selector
        self.ask_status_widget = ask_status_widget
        self.ask_send_button = ask_send_button
        self.helpful_button = helpful_button
        self.unresolved_button = unresolved_button
        self.on_toggle = on_toggle
        self.on_session_selected = on_session_selected
        self.on_ask_submit = on_ask_submit
        self.on_feedback = on_feedback
        self.on_drag_release = on_drag_release
        self.on_callback_error = on_callback_error
        self._session_by_label: dict[str, str] = {}
        self._drag_offset = (0, 0)
        self._press_root = (0, 0)
        self._dragged = False

        self.icon_window.overrideredirect(True)
        self.icon_window.attributes("-topmost", True)
        self.panel_window.attributes("-topmost", True)
        self.panel_window.withdraw()
        self.drag_surface.bind("<ButtonPress-1>", self._begin_drag)
        self.drag_surface.bind("<B1-Motion>", self._drag)
        self.drag_surface.bind("<ButtonRelease-1>", self._release)
        if self.session_selector is not None:
            self.session_selector.bind(
                "<<ComboboxSelected>>", self._session_selected
            )
        if self.ask_send_button is not None:
            self.ask_send_button.configure(command=self._submit_ask)
        if self.helpful_button is not None:
            self.helpful_button.configure(
                command=lambda: self._submit_feedback("helpful")
            )
        if self.unresolved_button is not None:
            self.unresolved_button.configure(
                command=lambda: self._submit_feedback("unresolved")
            )

    def _begin_drag(self, event: Any) -> None:
        self._press_root = (int(event.x_root), int(event.y_root))
        self._dragged = False
        self._drag_offset = (
            int(event.x_root) - int(self.icon_window.winfo_x()),
            int(event.y_root) - int(self.icon_window.winfo_y()),
        )

    def _drag(self, event: Any) -> None:
        if (
            abs(int(event.x_root) - self._press_root[0]) > 3
            or abs(int(event.y_root) - self._press_root[1]) > 3
        ):
            self._dragged = True
        x = int(event.x_root) - self._drag_offset[0]
        y = int(event.y_root) - self._drag_offset[1]
        self.icon_window.geometry(f"{x:+d}{y:+d}")

    def _release(self, event: Any) -> None:
        if self._dragged:
            self._invoke(self.on_drag_release)
        else:
            self._invoke(self.on_toggle)

    def _invoke(self, callback: Callable[..., Any] | None, *args: Any) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as exc:
            if self.on_callback_error is None:
                raise
            self.on_callback_error(exc)

    def _session_selected(self, event: Any) -> None:
        if self.session_selector is None or self.on_session_selected is None:
            return
        session_id = self._session_by_label.get(str(self.session_selector.get()))
        if session_id:
            self._invoke(self.on_session_selected, session_id)

    def _submit_ask(self) -> None:
        if self.ask_entry is None or self.on_ask_submit is None:
            return
        question = str(self.ask_entry.get() or "").strip()
        if question:
            self._invoke(self.on_ask_submit, question)

    def _submit_feedback(self, feedback: str) -> None:
        self._invoke(self.on_feedback, feedback)

    @staticmethod
    def _text(state: WindowsViewState) -> str:
        blocks: list[str] = []
        for item in state.mentor_messages:
            content = str(
                item.payload.get("content")
                or item.payload.get("message")
                or item.payload.get("text")
                or ""
            )
            blocks.append(f"导师 · {content}".rstrip())
        for item in state.analyses:
            raw_result = item.payload.get("result")
            result = raw_result if isinstance(raw_result, Mapping) else {}
            diagnosis = str(
                result.get("diagnosis")
                or result.get("summary")
                or item.payload.get("diagnosis")
                or ""
            )
            blocks.append(f"诊断 · {diagnosis}".rstrip())
        return "\n\n".join(blocks)

    def _render_sessions(self, state: WindowsViewState) -> None:
        if self.session_selector is None:
            return
        labels: list[str] = []
        selected_label = ""
        mapping: dict[str, str] = {}
        for session in state.sessions:
            label = f"{session.title} [{session.session_id}]"
            labels.append(label)
            mapping[label] = session.session_id
            if session.session_id == state.selected_session_id:
                selected_label = label
        self._session_by_label = mapping
        self.session_selector.configure(
            values=tuple(labels),
            state=("readonly" if labels else "disabled"),
        )
        self.session_selector.set(selected_label)

    @staticmethod
    def _ask_text(ask: AskViewState) -> str:
        labels = {
            "idle": "有问题可以直接问 Copilot。",
            "pending": "处理中…",
            "answered": "已回答",
            "degraded": "降级回答",
            "failed": "回答失败",
        }
        blocks = [labels.get(ask.status, ask.status)]
        if ask.answer:
            blocks.append(ask.answer)
        if ask.error_code:
            blocks.append(f"错误：{ask.error_code}")
        feedback_labels = {
            "pending": "反馈发送中…",
            "sent": "反馈已提交",
            "failed": "反馈发送失败，可重试",
        }
        if ask.feedback_status in feedback_labels:
            blocks.append(feedback_labels[ask.feedback_status])
        if ask.feedback_error_code:
            blocks.append(f"反馈错误：{ask.feedback_error_code}")
        return "\n".join(blocks)

    def _render_ask(self, state: WindowsViewState) -> None:
        if self.ask_status_widget is not None:
            self.ask_status_widget.configure(text=self._ask_text(state.ask))
        if self.ask_send_button is not None:
            self.ask_send_button.configure(
                state=(
                    "disabled"
                    if state.ask.status == "pending"
                    or state.selected_session_id is None
                    else "normal"
                )
            )
        feedback_enabled = (
            state.ask.status in {"answered", "degraded"}
            and state.ask.ask_id is not None
            and state.ask.feedback_status not in {"pending", "sent"}
        )
        for button in (self.helpful_button, self.unresolved_button):
            if button is not None:
                button.configure(state="normal" if feedback_enabled else "disabled")

    def render(self, state: WindowsViewState) -> None:
        if state.unread_count > 0 and not state.expanded:
            self.unread_badge.configure(
                text=(str(state.unread_count) if state.unread_count <= 99 else "99+")
            )
            self.unread_badge.place(relx=1.0, rely=0.0, anchor="ne")
        else:
            self.unread_badge.place_forget()

        if state.expanded:
            self.panel_window.deiconify()
            self.panel_window.lift()
        else:
            self.panel_window.withdraw()

        self.content_widget.configure(state="normal")
        self.content_widget.delete("1.0", "end")
        self.content_widget.insert("end", self._text(state))
        self.content_widget.configure(state="disabled")
        self._render_sessions(state)
        self._render_ask(state)
        if state.focus_target == "ask_input" and self.ask_entry is not None:
            self.ask_entry.focus_set()


class WindowsTkHost:
    """Concrete Tk host used by ``start_windows_client`` on the main thread."""

    def __init__(
        self,
        config: Any,
        *,
        root: Any,
        panel: Any,
        icon_surface: Any,
        unread_badge: Any,
        content_widget: Any,
        session_selector: Any,
        ask_status_widget: Any,
        ask_entry: Any,
        ask_send_button: Any,
        helpful_button: Any,
        unresolved_button: Any,
        display_provider: DisplayProvider = windows_display_info,
    ) -> None:
        self.config = config
        self.root = root
        self.panel = panel
        self._display_provider = display_provider
        self.store = WindowsMessageStore(
            Path(config.state_dir) / "windows-ui.sqlite3",
            student_id=str(config.student_id),
        )
        self._supervisor: Any | None = None
        self._running = False
        self._ask_future: Any | None = None
        self._feedback_future: Any | None = None
        self._ask_retry_at = 0.0
        self._feedback_retry_at = 0.0
        self._next_session_sync_at = 0.0
        self._next_receipt_reconcile_at = 0.0
        self.adapter = TkWindowsStudentAdapter(
            icon_window=root,
            panel_window=panel,
            unread_badge=unread_badge,
            content_widget=content_widget,
            ask_entry=ask_entry,
            drag_surface=icon_surface,
            session_selector=session_selector,
            ask_status_widget=ask_status_widget,
            ask_send_button=ask_send_button,
            helpful_button=helpful_button,
            unresolved_button=unresolved_button,
            on_toggle=self._toggle_panel,
            on_session_selected=self._select_session,
            on_ask_submit=self._submit_ask,
            on_feedback=self._submit_feedback,
            on_drag_release=self._clamp_icon,
            on_callback_error=self._callback_error,
        )
        self.view = WindowsStudentView(self.store, renderer=self.adapter)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.panel.protocol("WM_DELETE_WINDOW", self.view.close_panel)

    def _callback_error(self, exc: Exception) -> None:
        log.warning("Windows Tk callback failed type=%s", type(exc).__name__)

    @staticmethod
    def _window_metric(window: Any, names: Sequence[str], fallback: float) -> float:
        for name in names:
            method = getattr(window, name, None)
            if not callable(method):
                continue
            try:
                value = float(method())
            except (TypeError, ValueError, OSError):
                continue
            if math.isfinite(value):
                return value
        return float(fallback)

    def _icon_rect(self) -> Rect:
        update = getattr(self.root, "update_idletasks", None)
        if callable(update):
            update()
        x = self._window_metric(self.root, ("winfo_x", "winfo_rootx"), 0.0)
        y = self._window_metric(self.root, ("winfo_y", "winfo_rooty"), 0.0)
        width = self._window_metric(
            self.root,
            ("winfo_width", "winfo_reqwidth"),
            56.0,
        )
        height = self._window_metric(
            self.root,
            ("winfo_height", "winfo_reqheight"),
            56.0,
        )
        # Tk reports 1x1 before its first idle layout; use the declared icon
        # size instead of clamping/placing against that transient sentinel.
        if width <= 1:
            width = 56.0
        if height <= 1:
            height = 56.0
        return Rect(x, y, width, height)

    def _display_info(self) -> tuple[tuple[Rect, ...], float]:
        monitors, dpi = self._display_provider(self.root)
        resolved_monitors = tuple(monitors)
        if not resolved_monitors or any(
            not isinstance(monitor, Rect)
            or monitor.width <= 0
            or monitor.height <= 0
            for monitor in resolved_monitors
        ):
            raise ValueError("display provider returned no valid monitors")
        dpi_scale(dpi)
        return resolved_monitors, float(dpi)

    @staticmethod
    def _geometry(rect: Rect, *, include_size: bool) -> str:
        position = f"{round(rect.x):+d}{round(rect.y):+d}"
        if not include_size:
            return position
        return f"{round(rect.width)}x{round(rect.height)}{position}"

    def _place_panel(self) -> None:
        monitors, dpi = self._display_info()
        placed = panel_rect_for_anchor(
            self._icon_rect(),
            (420.0, 640.0),
            monitors,
            dpi=dpi,
        )
        self.panel.geometry(self._geometry(placed, include_size=True))

    def _clamp_icon(self) -> None:
        monitors, _dpi = self._display_info()
        clamped = clamp_rect_to_monitor(self._icon_rect(), monitors)
        self.root.geometry(self._geometry(clamped, include_size=False))

    def _toggle_panel(self) -> None:
        opening = not self.view.state.expanded
        if opening:
            self._place_panel()
        self.view.toggle_panel()
        if opening:
            self.view.focus_ask_input()

    def _select_session(self, session_id: str) -> None:
        self.view.select_session(session_id)

    def _submit_ask(self, question: str) -> None:
        if self._supervisor is None:
            raise RuntimeError("Windows client supervisor is unavailable")
        request = self.view.begin_ask(question)
        self._start_pending_ask(request)

    def _submit_feedback(self, feedback: str) -> None:
        supervisor = self._supervisor
        if supervisor is None:
            raise RuntimeError("Windows client supervisor is unavailable")
        self.view.begin_feedback(feedback)
        self._start_pending_feedback()

    @staticmethod
    def _session_payload(session: Any) -> Mapping[str, Any] | None:
        if isinstance(session, Mapping):
            return session
        to_dict = getattr(session, "to_dict", None)
        if callable(to_dict):
            payload = to_dict()
            return payload if isinstance(payload, Mapping) else None
        return None

    def _sync_sessions(self) -> None:
        supervisor = self._supervisor
        runtime = getattr(supervisor, "runtime", None)
        data = getattr(runtime, "data", None)
        if data is None:
            return
        try:
            raw_sessions = data.list_sessions()
            sessions = [
                payload
                for payload in (
                    self._session_payload(session) for session in raw_sessions
                )
                if payload is not None
            ]
            active = data.detect_active_session()
        except Exception as exc:
            log.warning(
                "Windows WorkBuddy session refresh failed type=%s",
                type(exc).__name__,
            )
            return
        active_session_id = str(getattr(active, "session_id", "") or "").strip()
        active_reliable = bool(
            active_session_id and getattr(active, "failure", None) is None
        )
        self.view.update_sessions(
            sessions,
            active_session_id=(active_session_id or None),
            active_reliable=active_reliable,
        )

    def _receipt_ledger(self) -> Any | None:
        supervisor = self._supervisor
        runtime = getattr(supervisor, "runtime", None)
        coordinator = getattr(runtime, "coordinator", None)
        ledger = getattr(coordinator, "receipt_ledger", None)
        if ledger is not None:
            return ledger
        spool = getattr(runtime, "spool", None)
        return getattr(spool, "receipt_ledger", None)

    def _reconcile_receipts(self) -> None:
        ledger = self._receipt_ledger()
        if ledger is None:
            return
        try:
            changed = self.store.reconcile_mentor_receipts(ledger)
        except Exception as exc:
            log.warning(
                "Windows mentor receipt reconcile failed type=%s",
                type(exc).__name__,
            )
            return
        if changed:
            self.view.restore()

    def _start_pending_ask(
        self,
        request: Mapping[str, Any] | None = None,
    ) -> None:
        supervisor = self._supervisor
        if supervisor is None or self._ask_future is not None:
            return
        if request is None:
            try:
                recovery = self.view.pending_ask_query()
            except RuntimeError:
                return
            ask = self.view.state.ask
            request = {
                "question": ask.question,
                "session_id": recovery["session_id"],
                "client_request_id": recovery["client_request_id"],
            }
        try:
            self._ask_future = supervisor.ask(
                str(request.get("question") or ""),
                session_id=str(request.get("session_id") or ""),
                client_request_id=str(request.get("client_request_id") or ""),
            )
        except Exception as exc:
            log.warning("Windows ask start deferred type=%s", type(exc).__name__)
            self._ask_retry_at = time.monotonic() + 2.0

    def _start_pending_feedback(self) -> None:
        supervisor = self._supervisor
        ask = self.view.state.ask
        if (
            supervisor is None
            or self._feedback_future is not None
            or ask.feedback_status != "pending"
            or ask.ask_id is None
            or not ask.feedback
        ):
            return
        try:
            self._feedback_future = supervisor.feedback(
                int(ask.ask_id), ask.feedback
            )
        except Exception as exc:
            log.warning("Windows feedback start deferred type=%s", type(exc).__name__)
            self._feedback_retry_at = time.monotonic() + 2.0

    @staticmethod
    def _future_done(future: Any) -> bool:
        done = getattr(future, "done", None)
        return bool(done()) if callable(done) else False

    @staticmethod
    def _exception_code(exc: BaseException) -> str:
        return re.sub(
            r"(?<!^)(?=[A-Z])",
            "_",
            type(exc).__name__,
        ).lower()

    def _poll_ask(self, now: float) -> None:
        future = self._ask_future
        if future is None:
            if now >= self._ask_retry_at and self.view.state.ask.status == "pending":
                self._start_pending_ask()
            return
        if not self._future_done(future):
            return
        self._ask_future = None
        try:
            payload = future.result()
        except TemporaryNetworkError as exc:
            log.warning("Windows ask recovery deferred type=%s", type(exc).__name__)
            self._ask_retry_at = now + 2.0
            return
        except Exception as exc:
            try:
                self.view.resolve_ask(
                    "failed",
                    error_code=self._exception_code(exc),
                )
            except Exception as render_exc:
                log.warning(
                    "Windows ask failure update failed type=%s",
                    type(render_exc).__name__,
                )
            return
        if not isinstance(payload, Mapping):
            self._ask_retry_at = now + 2.0
            return
        status = str(payload.get("status") or "pending")
        if status == "pending":
            self._ask_retry_at = now + 1.0
            return
        if status not in {"answered", "degraded", "failed"}:
            self._ask_retry_at = now + 2.0
            return
        try:
            raw_ask_id = int(payload.get("ask_id") or 0)
            self.view.resolve_ask(
                status,
                ask_id=(raw_ask_id if raw_ask_id > 0 else None),
                answer=str(payload.get("answer") or ""),
                error_code=str(payload.get("error_code") or ""),
            )
        except Exception as exc:
            log.warning("Windows ask UI update failed type=%s", type(exc).__name__)

    def _poll_feedback(self, now: float) -> None:
        future = self._feedback_future
        if future is None:
            if (
                now >= self._feedback_retry_at
                and self.view.state.ask.feedback_status == "pending"
            ):
                self._start_pending_feedback()
            return
        if not self._future_done(future):
            return
        self._feedback_future = None
        try:
            future.result()
        except Exception as exc:
            self.view.finish_feedback(
                success=False,
                error_code=type(exc).__name__.lower(),
            )
        else:
            self.view.finish_feedback(success=True)

    def _tick(self) -> None:
        if not self._running:
            return
        supervisor = self._supervisor
        if supervisor is None:
            self.close()
            return
        try:
            supervisor.pump_ui(limit=32)
        except Exception as exc:
            log.warning("Windows UI pump failed type=%s", type(exc).__name__)
            self.close()
            return
        now = time.monotonic()
        self._poll_ask(now)
        self._poll_feedback(now)
        if now >= self._next_session_sync_at:
            self._sync_sessions()
            self._next_session_sync_at = now + 2.5
        if now >= self._next_receipt_reconcile_at:
            self._reconcile_receipts()
            self._next_receipt_reconcile_at = now + 1.0
        if self._running:
            self.root.after(50, self._tick)

    def run(self, supervisor: Any) -> None:
        if self._running:
            raise RuntimeError("Windows Tk host is already running")
        self._supervisor = supervisor
        self._running = True
        self._reconcile_receipts()
        self.view.restore()
        self._sync_sessions()
        self._start_pending_ask()
        self._start_pending_feedback()
        self.root.after(0, self._tick)
        try:
            self.root.mainloop()
        finally:
            self._running = False

    def close(self) -> None:
        self._running = False
        try:
            self.root.quit()
        except Exception as exc:
            log.warning("Windows Tk quit failed type=%s", type(exc).__name__)


def create_windows_ui_host(
    config: Any,
    *,
    tk_module: Any | None = None,
    ttk_module: Any | None = None,
    display_provider: DisplayProvider = windows_display_info,
) -> WindowsTkHost:
    """Create the real Tk host lazily on the caller's (main) thread."""

    if tk_module is None:
        import tkinter as tk_module
    if ttk_module is None:
        ttk_module = getattr(tk_module, "ttk", None)
    if ttk_module is None:
        from tkinter import ttk as ttk_module

    root = tk_module.Tk()
    root.title("WorkBuddy Copilot")
    root.configure(bg="#2563eb")
    screen_width = max(56, int(root.winfo_screenwidth()))
    root.geometry(f"56x56+{max(0, screen_width - 80)}+80")
    icon_surface = tk_module.Label(
        root,
        text="AI",
        bg="#2563eb",
        fg="white",
        font=("Segoe UI", 14, "bold"),
        cursor="hand2",
    )
    icon_surface.pack(fill="both", expand=True)
    unread_badge = tk_module.Label(
        root,
        text="",
        bg="#dc2626",
        fg="white",
        font=("Segoe UI", 8, "bold"),
    )

    panel = tk_module.Toplevel(root)
    panel.title("Copilot 技术助教")
    panel.geometry("420x640")
    panel.configure(bg="#f8fafc")
    session_selector = ttk_module.Combobox(panel)
    session_selector.pack(fill="x", padx=12, pady=(12, 6))
    content_widget = tk_module.Text(
        panel,
        height=20,
        wrap="word",
        state="disabled",
        bg="#ffffff",
        fg="#0f172a",
    )
    content_widget.pack(fill="both", expand=True, padx=12, pady=6)
    ask_status_widget = tk_module.Label(
        panel,
        text="有问题可以直接问 Copilot。",
        anchor="w",
        justify="left",
        bg="#f8fafc",
        fg="#334155",
    )
    ask_status_widget.pack(fill="x", padx=12, pady=4)
    ask_entry = tk_module.Entry(panel)
    ask_entry.pack(fill="x", padx=12, pady=4)
    ask_send_button = tk_module.Button(panel, text="发送")
    ask_send_button.pack(fill="x", padx=12, pady=4)
    helpful_button = tk_module.Button(panel, text="有帮助")
    helpful_button.pack(side="left", expand=True, fill="x", padx=(12, 4), pady=8)
    unresolved_button = tk_module.Button(panel, text="未解决")
    unresolved_button.pack(side="left", expand=True, fill="x", padx=(4, 12), pady=8)

    return WindowsTkHost(
        config,
        root=root,
        panel=panel,
        icon_surface=icon_surface,
        unread_badge=unread_badge,
        content_widget=content_widget,
        session_selector=session_selector,
        ask_status_widget=ask_status_widget,
        ask_entry=ask_entry,
        ask_send_button=ask_send_button,
        helpful_button=helpful_button,
        unresolved_button=unresolved_button,
        display_provider=display_provider,
    )


__all__ = [
    "AskViewState",
    "DisplayProvider",
    "Rect",
    "SessionOption",
    "StoredWindowsAsk",
    "StoredWindowsItem",
    "StoreUpsert",
    "TkWindowsStudentAdapter",
    "WindowsMessageStore",
    "WindowsStudentView",
    "WindowsTkHost",
    "WindowsViewState",
    "clamp_rect_to_monitor",
    "create_windows_ui_host",
    "dpi_scale",
    "panel_rect_for_anchor",
    "windows_display_info",
]
