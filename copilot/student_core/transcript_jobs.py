"""Durable Stop-transcript supplement queue shared by student runtimes."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..models import UploadOutcome, normalize_event_id


@dataclass(frozen=True)
class TranscriptUploadJob:
    event_id: str
    report_id: int
    student_id: str
    session_id: str
    filtered_content: str = ""
    content_sha256: str = ""
    attempts: int = 0
    last_error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    next_attempt_at: float = 0.0

    @property
    def payload_pinned(self) -> bool:
        return bool(self.content_sha256)


class TranscriptUploadQueue:
    """Small SQLite outbox; a job disappears only after confirmed upload."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self._clock = clock or time.time
        if self.path.is_symlink():
            raise ValueError("transcript job database must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or not self.path.parent.is_dir():
            raise ValueError("transcript job directory must be a real directory")
        with self._conn() as connection:
            connection.executescript(
                """CREATE TABLE IF NOT EXISTS transcript_upload_jobs (
                       event_id TEXT PRIMARY KEY,
                       report_id INTEGER NOT NULL,
                       student_id TEXT NOT NULL,
                       session_id TEXT NOT NULL,
                       filtered_content TEXT NOT NULL DEFAULT '',
                       content_sha256 TEXT NOT NULL DEFAULT '',
                       attempts INTEGER NOT NULL DEFAULT 0,
                       last_error TEXT NOT NULL DEFAULT '',
                       created_at REAL NOT NULL,
                       updated_at REAL NOT NULL,
                       next_attempt_at REAL NOT NULL DEFAULT 0
                   );
                   CREATE INDEX IF NOT EXISTS idx_transcript_jobs_created
                     ON transcript_upload_jobs(created_at, event_id);"""
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(transcript_upload_jobs)"
                ).fetchall()
            }
            if "filtered_content" not in columns:
                connection.execute(
                    """ALTER TABLE transcript_upload_jobs
                       ADD COLUMN filtered_content TEXT NOT NULL DEFAULT ''"""
                )
            if "content_sha256" not in columns:
                connection.execute(
                    """ALTER TABLE transcript_upload_jobs
                       ADD COLUMN content_sha256 TEXT NOT NULL DEFAULT ''"""
                )
            if "next_attempt_at" not in columns:
                connection.execute(
                    """ALTER TABLE transcript_upload_jobs
                       ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0"""
                )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_transcript_jobs_due
                   ON transcript_upload_jobs(next_attempt_at, created_at, event_id)"""
            )

    def _conn(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _validated_values(
        *,
        event_id: str,
        report_id: int,
        student_id: str,
        session_id: str,
    ) -> tuple[str, int, str, str]:
        normalized_event_id = normalize_event_id(event_id)
        normalized_student_id = str(student_id or "").strip()
        normalized_session_id = str(session_id or "").strip()
        normalized_report_id = int(report_id)
        if normalized_event_id is None:
            raise ValueError("event_id is required")
        if normalized_report_id <= 0:
            raise ValueError("report_id must be positive")
        if not normalized_student_id or not normalized_session_id:
            raise ValueError("student_id and session_id are required")
        return (
            normalized_event_id,
            normalized_report_id,
            normalized_student_id,
            normalized_session_id,
        )

    def enqueue(
        self,
        *,
        event_id: str,
        report_id: int,
        student_id: str,
        session_id: str,
    ) -> TranscriptUploadJob:
        values = self._validated_values(
            event_id=event_id,
            report_id=report_id,
            student_id=student_id,
            session_id=session_id,
        )
        normalized_event_id, normalized_report_id, normalized_student_id, normalized_session_id = values
        now = self._clock()
        with self._conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM transcript_upload_jobs WHERE event_id = ?",
                (normalized_event_id,),
            ).fetchone()
            if existing is not None:
                observed = (
                    str(existing["event_id"]),
                    int(existing["report_id"]),
                    str(existing["student_id"]),
                    str(existing["session_id"]),
                )
                if observed != values:
                    raise ValueError("transcript job event collision")
                return self._from_row(existing)
            connection.execute(
                """INSERT INTO transcript_upload_jobs
                   (event_id, report_id, student_id, session_id,
                    filtered_content, content_sha256,
                    attempts, last_error, created_at, updated_at, next_attempt_at)
                   VALUES (?, ?, ?, ?, '', '', 0, '', ?, ?, 0)""",
                (*values, now, now),
            )
            row = connection.execute(
                "SELECT * FROM transcript_upload_jobs WHERE event_id = ?",
                (normalized_event_id,),
            ).fetchone()
            if row is None:
                raise sqlite3.IntegrityError("transcript job commit failed")
            return self._from_row(row)

    def pin_payload(
        self,
        event_id: str,
        *,
        filtered_content: str,
        content_sha256: str,
    ) -> TranscriptUploadJob:
        """Atomically freeze the exact upload body before network delivery."""
        normalized_event_id = normalize_event_id(event_id)
        if normalized_event_id is None:
            raise ValueError("event_id is required")
        if not isinstance(filtered_content, str):
            raise TypeError("filtered_content must be text")
        normalized_sha = str(content_sha256 or "")
        expected_sha = hashlib.sha256(filtered_content.encode("utf-8")).hexdigest()
        if normalized_sha != expected_sha:
            raise ValueError("transcript payload sha mismatch")
        now = self._clock()
        with self._conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM transcript_upload_jobs WHERE event_id = ?",
                (normalized_event_id,),
            ).fetchone()
            if existing is None:
                raise ValueError("transcript job not found")
            current_content = str(existing["filtered_content"] or "")
            current_sha = str(existing["content_sha256"] or "")
            if current_sha:
                if current_content != filtered_content or current_sha != normalized_sha:
                    raise ValueError("transcript job payload collision")
                return self._from_row(existing)
            if current_content:
                raise ValueError("transcript job payload is incomplete")
            connection.execute(
                """UPDATE transcript_upload_jobs
                   SET filtered_content = ?, content_sha256 = ?, updated_at = ?
                   WHERE event_id = ?""",
                (filtered_content, normalized_sha, now, normalized_event_id),
            )
            pinned = connection.execute(
                "SELECT * FROM transcript_upload_jobs WHERE event_id = ?",
                (normalized_event_id,),
            ).fetchone()
            if pinned is None:
                raise sqlite3.IntegrityError("transcript payload pin failed")
            return self._from_row(pinned)

    def pending(self, *, limit: int = 64) -> list[TranscriptUploadJob]:
        bounded_limit = max(1, min(int(limit), 1000))
        with self._conn() as connection:
            rows = connection.execute(
                """SELECT * FROM transcript_upload_jobs
                   ORDER BY created_at ASC, event_id ASC LIMIT ?""",
                (bounded_limit,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def ready(self, *, limit: int = 64) -> list[TranscriptUploadJob]:
        """Return only jobs whose durable retry deadline has elapsed."""
        bounded_limit = max(1, min(int(limit), 1000))
        with self._conn() as connection:
            rows = connection.execute(
                """SELECT * FROM transcript_upload_jobs
                   WHERE next_attempt_at <= ?
                   ORDER BY created_at ASC, event_id ASC LIMIT ?""",
                (self._clock(), bounded_limit),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def record_outcome(self, event_id: str, outcome: UploadOutcome) -> bool:
        normalized_event_id = normalize_event_id(event_id)
        if normalized_event_id is None:
            raise ValueError("event_id is required")
        if not isinstance(outcome, UploadOutcome):
            raise TypeError("outcome must be UploadOutcome")
        with self._conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if outcome.complete:
                deleted = connection.execute(
                    """DELETE FROM transcript_upload_jobs
                       WHERE event_id = ? AND content_sha256 != ''""",
                    (normalized_event_id,),
                ).rowcount
                return deleted == 1
            row = connection.execute(
                """SELECT attempts FROM transcript_upload_jobs
                   WHERE event_id = ?""",
                (normalized_event_id,),
            ).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"] or 0) + 1
            backoff_seconds = min(300.0, float(2 ** min(attempts, 8)))
            now = self._clock()
            updated = connection.execute(
                """UPDATE transcript_upload_jobs
                   SET attempts = ?, last_error = ?, updated_at = ?,
                       next_attempt_at = ?
                   WHERE event_id = ?""",
                (
                    attempts,
                    str(outcome.error_code or "upload_incomplete")[:80],
                    now,
                    now + backoff_seconds,
                    normalized_event_id,
                ),
            ).rowcount
            return updated == 1

    @staticmethod
    def _from_row(row: sqlite3.Row) -> TranscriptUploadJob:
        filtered_content = str(row["filtered_content"] or "")
        content_sha256 = str(row["content_sha256"] or "")
        if content_sha256:
            if re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None:
                raise ValueError("invalid pinned transcript sha")
            observed_sha = hashlib.sha256(filtered_content.encode("utf-8")).hexdigest()
            if observed_sha != content_sha256:
                raise ValueError("pinned transcript payload sha mismatch")
        elif filtered_content:
            raise ValueError("transcript job payload is incomplete")
        return TranscriptUploadJob(
            event_id=str(row["event_id"]),
            report_id=int(row["report_id"]),
            student_id=str(row["student_id"]),
            session_id=str(row["session_id"]),
            filtered_content=filtered_content,
            content_sha256=content_sha256,
            attempts=int(row["attempts"] or 0),
            last_error=str(row["last_error"] or ""),
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
            next_attempt_at=float(row["next_attempt_at"] or 0.0),
        )
