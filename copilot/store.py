"""SQLite 存储：对话上报记录 + 分析结果。

两表结构：reports（每次 hook 上报）+ analyses（LLM 分析结果）。
方便后续做历史回看 / 导师面板迭代。
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import (
    AnalysisEnvelope,
    AttentionDecision,
    normalize_confidence,
    normalize_event_id,
    normalize_evidence,
)

log = logging.getLogger("copilot.store")

LEGACY_PROMPT_BACKFILL_WINDOW_SECONDS = 5.0
LEGACY_RAW_MATCH_WINDOW_SECONDS = 5.0
EXPLICIT_RAW_TRANSCRIPT_MARKER = "copilot:explicit-raw-transcript"

SYSTEM_FAILURE_REASON_CODES = {
    "stop_input_unavailable": "system_stop_input_unavailable",
    "stop_retries_exhausted": "system_stop_retries_exhausted",
    "bulk_analysis": "system_bulk_analysis_failed",
    "upload_transfer": "system_upload_transfer_failed",
    "upload_analysis": "system_upload_analysis_failed",
}

ATTENTION_BACKFILL_SOURCE_KINDS = frozenset({
    "analysis",
    "student_ask",
    "stop",
    "bulk_analysis",
    "upload_transfer",
    "upload_analysis",
})


SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL,
    session_id TEXT,
    event TEXT,
    prompt TEXT,
    transcript_path TEXT,
    msg_count INTEGER,
    tool_calls INTEGER,
    analysis_pending INTEGER DEFAULT 0,
    event_id TEXT,
    analysis_input TEXT,
    analysis_status TEXT NOT NULL DEFAULT 'not_requested',
    analysis_attempts INTEGER NOT NULL DEFAULT 0,
    analysis_error TEXT NOT NULL DEFAULT '',
    analysis_next_retry_at REAL,
    analysis_model TEXT NOT NULL DEFAULT '',
    analysis_prompt_hash TEXT NOT NULL DEFAULT '',
    analysis_latency_ms INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    student_id TEXT NOT NULL,
    session_id TEXT,
    session_title TEXT,
    topic TEXT,
    understanding TEXT,
    off_topic INTEGER,
    stuck_at TEXT,
    is_technical INTEGER DEFAULT 0,
    severity TEXT DEFAULT 'info',
    diagnosis TEXT,
    suggestion TEXT,
    progress TEXT,
    guidance TEXT,
    alert TEXT,
    confidence REAL NOT NULL DEFAULT 0.5,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    model TEXT NOT NULL DEFAULT '',
    prompt_hash TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    raw TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (report_id) REFERENCES reports(id)
);

CREATE INDEX IF NOT EXISTS idx_reports_student ON reports(student_id, created_at);
CREATE INDEX IF NOT EXISTS idx_analyses_student ON analyses(student_id, created_at);

CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER,
    session_id TEXT,
    seq_in_session INTEGER,
    student_id TEXT,
    content TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id INTEGER,
    session_id TEXT,
    student_id TEXT,
    content TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (prompt_id) REFERENCES prompts(id)
);

CREATE TABLE IF NOT EXISTS students (
    student_id TEXT PRIMARY KEY,
    display_name TEXT,
    token_hash TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS attention_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL CHECK(source_type IN ('analysis', 'student_ask', 'system')),
    source_id TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('learning', 'system')),
    student_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL CHECK(priority IN ('high', 'medium')),
    reason_code TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    suggested_action TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open', 'in_progress', 'resolved', 'dismissed')),
    handled_by TEXT NOT NULL DEFAULT '',
    resolution_note TEXT NOT NULL DEFAULT '',
    handled_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(source_type, source_id, reason_code),
    FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_attention_queue
    ON attention_items(status, priority, created_at, id);
CREATE INDEX IF NOT EXISTS idx_attention_student_status
    ON attention_items(student_id, status);

CREATE TABLE IF NOT EXISTS system_failure_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN (
        'stop', 'bulk_analysis', 'upload_transfer', 'upload_analysis'
    )),
    logical_key TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation >= 1),
    student_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    reason_code TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(kind, logical_key, generation, reason_code),
    FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_system_failure_occurrence_kind_id
    ON system_failure_occurrences(kind, id);

CREATE TABLE IF NOT EXISTS attention_backfill_cursors (
    source_kind TEXT PRIMARY KEY,
    last_id INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    student_id TEXT,
    work_dir TEXT,
    title TEXT,
    group_type TEXT,
    space_name TEXT,
    created_at REAL,
    last_activity_at REAL
);

CREATE TABLE IF NOT EXISTS mentor_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT,
    mentor_id TEXT,
    session_id TEXT,
    text TEXT,
    message_id TEXT UNIQUE,
    client_request_id TEXT,
    created_at REAL,
    delivered_at REAL,
    read_at REAL,
    FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS raw_transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    student_id TEXT,
    content TEXT,
    content_sha256 TEXT,
    analysis_status TEXT DEFAULT '',
    analysis_error TEXT DEFAULT '',
    analysis_model TEXT NOT NULL DEFAULT '',
    analysis_prompt_hash TEXT NOT NULL DEFAULT '',
    analysis_latency_ms INTEGER NOT NULL DEFAULT 0,
    analysis_attempts INTEGER NOT NULL DEFAULT 0,
    analysis_generation INTEGER NOT NULL DEFAULT 0,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS stop_transcript_watermarks (
    student_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_report_id INTEGER NOT NULL,
    source_event_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(student_id, session_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    summary TEXT,
    source TEXT DEFAULT 'bulk',
    content_sha256 TEXT,
    created_at REAL NOT NULL,
    UNIQUE(session_id, seq, role)
);

CREATE TABLE IF NOT EXISTS upload_requests (
    request_id TEXT PRIMARY KEY,
    mentor_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    session_id TEXT,
    status TEXT NOT NULL,
    transfer_status TEXT NOT NULL DEFAULT 'pending',
    analysis_status TEXT NOT NULL DEFAULT 'not_requested',
    error_message TEXT DEFAULT '',
    transfer_error TEXT DEFAULT '',
    analysis_error TEXT DEFAULT '',
    transfer_failure_generation INTEGER NOT NULL DEFAULT 0,
    analysis_failure_generation INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    updated_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS upload_request_sessions (
    request_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    sha TEXT NOT NULL,
    analysis_status TEXT NOT NULL DEFAULT 'pending',
    analysis_error TEXT DEFAULT '',
    updated_at REAL NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (request_id, session_id),
    FOREIGN KEY (request_id) REFERENCES upload_requests(request_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS student_asks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL,
    session_id TEXT,
    question TEXT,
    answer TEXT,
    client_request_id TEXT NOT NULL DEFAULT '',
    answer_status TEXT NOT NULL DEFAULT 'answered',
    error_code TEXT NOT NULL DEFAULT '',
    feedback TEXT NOT NULL DEFAULT '',
    feedback_note TEXT NOT NULL DEFAULT '',
    feedback_at REAL,
    created_at REAL NOT NULL,
    FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS prompt_configs (
    key TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    updated_by TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

# 旧库迁移：analyses 表新增列（CREATE TABLE IF NOT EXISTS 不会改已有表）
# 注意：session 索引必须在迁移补列之后创建，否则旧库 executescript 会因缺列报错
_MIGRATIONS = [
    ("reports", "analysis_pending", "INTEGER DEFAULT 0"),
    ("reports", "event_id", "TEXT"),
    ("reports", "analysis_input", "TEXT"),
    ("reports", "analysis_status", "TEXT NOT NULL DEFAULT 'not_requested'"),
    ("reports", "analysis_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("reports", "analysis_error", "TEXT NOT NULL DEFAULT ''"),
    ("reports", "analysis_next_retry_at", "REAL"),
    ("reports", "analysis_model", "TEXT NOT NULL DEFAULT ''"),
    ("reports", "analysis_prompt_hash", "TEXT NOT NULL DEFAULT ''"),
    ("reports", "analysis_latency_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("analyses", "session_id", "TEXT"),
    ("analyses", "session_title", "TEXT"),
    ("analyses", "is_technical", "INTEGER DEFAULT 0"),
    ("analyses", "severity", "TEXT DEFAULT 'info'"),
    ("analyses", "diagnosis", "TEXT"),
    ("analyses", "suggestion", "TEXT"),
    ("analyses", "confidence", "REAL NOT NULL DEFAULT 0.5"),
    ("analyses", "evidence_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("analyses", "model", "TEXT NOT NULL DEFAULT ''"),
    ("analyses", "prompt_hash", "TEXT NOT NULL DEFAULT ''"),
    ("analyses", "latency_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("analyses", "attempt_count", "INTEGER NOT NULL DEFAULT 0"),
    ("prompts", "report_id", "INTEGER"),
    ("sessions", "group_type", "TEXT"),
    ("sessions", "space_name", "TEXT"),
    ("raw_transcripts", "content_sha256", "TEXT"),
    ("raw_transcripts", "analysis_status", "TEXT DEFAULT ''"),
    ("raw_transcripts", "analysis_error", "TEXT DEFAULT ''"),
    ("raw_transcripts", "analysis_model", "TEXT NOT NULL DEFAULT ''"),
    ("raw_transcripts", "analysis_prompt_hash", "TEXT NOT NULL DEFAULT ''"),
    ("raw_transcripts", "analysis_latency_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("raw_transcripts", "analysis_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("raw_transcripts", "analysis_generation", "INTEGER NOT NULL DEFAULT 0"),
    ("messages", "summary", "TEXT"),
    ("upload_requests", "error_message", "TEXT DEFAULT ''"),
    ("upload_requests", "result_json", "TEXT"),
    ("upload_requests", "updated_at", "REAL"),
    ("upload_requests", "transfer_status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("upload_requests", "analysis_status", "TEXT NOT NULL DEFAULT 'not_requested'"),
    ("upload_requests", "transfer_error", "TEXT DEFAULT ''"),
    ("upload_requests", "analysis_error", "TEXT DEFAULT ''"),
    (
        "upload_requests",
        "transfer_failure_generation",
        "INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "upload_requests",
        "analysis_failure_generation",
        "INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "attention_backfill_cursors",
        "version",
        "INTEGER NOT NULL DEFAULT 0",
    ),
    ("student_asks", "answer_status", "TEXT NOT NULL DEFAULT 'answered'"),
    ("student_asks", "error_code", "TEXT NOT NULL DEFAULT ''"),
    ("student_asks", "feedback", "TEXT NOT NULL DEFAULT ''"),
    ("student_asks", "feedback_note", "TEXT NOT NULL DEFAULT ''"),
    ("student_asks", "feedback_at", "REAL"),
    ("student_asks", "client_request_id", "TEXT NOT NULL DEFAULT ''"),
    ("mentor_messages", "client_request_id", "TEXT"),
]

_POST_MIGRATION_SQL = [
    """CREATE TABLE IF NOT EXISTS stop_transcript_watermarks (
           student_id TEXT NOT NULL,
           session_id TEXT NOT NULL,
           source_report_id INTEGER NOT NULL,
           source_event_id TEXT NOT NULL,
           content_sha256 TEXT NOT NULL,
           updated_at REAL NOT NULL,
           PRIMARY KEY(student_id, session_id)
       )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_student_event_id_unique
       ON reports(student_id, event_id)
       WHERE event_id IS NOT NULL AND event_id != ''""",
    """UPDATE reports
       SET analysis_status = 'done',
           analysis_pending = 0,
           analysis_input = NULL,
           analysis_error = '',
           analysis_next_retry_at = NULL
       WHERE EXISTS (SELECT 1 FROM analyses WHERE analyses.report_id = reports.id)""",
    """UPDATE reports SET analysis_status = 'pending'
       WHERE analysis_pending = 1 AND analysis_status = 'not_requested'""",
    "CREATE INDEX IF NOT EXISTS idx_analyses_session ON analyses(session_id, created_at)",
    # 回填旧数据的 session_id（从 reports 表关联）
    "UPDATE analyses SET session_id = (SELECT session_id FROM reports WHERE reports.id = analyses.report_id) WHERE analyses.session_id IS NULL",
    # 新表索引（prompts / ai_summaries 在迁移后建索引）
    "CREATE INDEX IF NOT EXISTS idx_prompts_session ON prompts(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_prompts_student ON prompts(student_id, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_prompts_report_id_unique ON prompts(report_id) WHERE report_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_ai_summaries_session ON ai_summaries(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ai_summaries_prompt ON ai_summaries(prompt_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_student ON sessions(student_id)",
    "CREATE INDEX IF NOT EXISTS idx_mentor_messages_student_delivered ON mentor_messages(student_id, delivered_at)",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_mentor_messages_client_request_unique
       ON mentor_messages(client_request_id)
       WHERE client_request_id IS NOT NULL AND client_request_id != ''""",
    "CREATE INDEX IF NOT EXISTS idx_raw_transcripts_session ON raw_transcripts(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_raw_transcripts_student_sha ON raw_transcripts(student_id, content_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_messages_student ON messages(student_id, session_id)",
    "CREATE INDEX IF NOT EXISTS idx_upload_requests_student_status ON upload_requests(student_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_upload_request_sessions_status ON upload_request_sessions(request_id, analysis_status)",
    "CREATE INDEX IF NOT EXISTS idx_student_asks_student ON student_asks(student_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_student_asks_session ON student_asks(session_id, created_at)",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_student_asks_client_request_unique
       ON student_asks(student_id, client_request_id)
       WHERE client_request_id != ''""",
    """INSERT OR IGNORE INTO sessions
       (session_id, student_id, work_dir, title, created_at, last_activity_at)
       SELECT
         a.session_id,
         (SELECT a_student.student_id
          FROM analyses a_student
          WHERE a_student.session_id = a.session_id
          ORDER BY a_student.created_at DESC, a_student.id DESC
          LIMIT 1),
         '',
         COALESCE((
           SELECT a_title.session_title
           FROM analyses a_title
           WHERE a_title.session_id = a.session_id
           ORDER BY a_title.created_at DESC, a_title.id DESC
           LIMIT 1
         ), ''),
         MIN(a.created_at),
         MAX(a.created_at)
       FROM analyses a
       WHERE a.session_id IS NOT NULL AND a.session_id != ''
       GROUP BY a.session_id""",
]


class UploadRetryClaimConflict(RuntimeError):
    """Raised when an analysis retry cannot be claimed atomically."""


class UploadSessionRegistrationConflict(RuntimeError):
    """Raised when an upload child cannot be registered atomically."""


class ActiveTranscriptAnalysisConflict(RuntimeError):
    """Raised when context-only replacement would invalidate active analysis."""


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @staticmethod
    def _failure_generation(value: object) -> int:
        try:
            generation = int(value or 0)
        except (TypeError, ValueError, OverflowError):
            generation = 0
        return max(1, generation)

    @staticmethod
    def _failure_reason_allowed(kind: str, reason_code: str) -> bool:
        allowed = {
            "stop": {
                SYSTEM_FAILURE_REASON_CODES["stop_input_unavailable"],
                SYSTEM_FAILURE_REASON_CODES["stop_retries_exhausted"],
            },
            "bulk_analysis": {SYSTEM_FAILURE_REASON_CODES["bulk_analysis"]},
            "upload_transfer": {SYSTEM_FAILURE_REASON_CODES["upload_transfer"]},
            "upload_analysis": {SYSTEM_FAILURE_REASON_CODES["upload_analysis"]},
        }
        return reason_code in allowed.get(kind, set())

    def _insert_system_failure_occurrence_with_conn(
        self,
        c: sqlite3.Connection,
        *,
        kind: str,
        logical_key: object,
        generation: object,
        student_id: object,
        session_id: object,
        reason_code: str,
        created_at: float | None = None,
    ) -> int | None:
        """Insert one privacy-bounded immutable failure occurrence."""
        normalized_key = str(logical_key or "")
        normalized_student = str(student_id or "")
        normalized_session = str(session_id or "")
        if not normalized_key.strip():
            raise ValueError("failure occurrence logical key is required")
        if not normalized_student.strip():
            raise ValueError("failure occurrence student id is required")
        if not self._failure_reason_allowed(kind, reason_code):
            raise ValueError("invalid failure occurrence reason")
        occurred_at = self._attention_timestamp(created_at, default=time.time())
        c.execute(
            """INSERT INTO students
               (student_id, display_name, token_hash, created_at)
               VALUES (?, '', NULL, ?)
               ON CONFLICT(student_id) DO NOTHING""",
            (normalized_student, occurred_at),
        )
        cur = c.execute(
            """INSERT INTO system_failure_occurrences
               (kind, logical_key, generation, student_id, session_id,
                reason_code, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(kind, logical_key, generation, reason_code) DO NOTHING""",
            (
                kind,
                normalized_key,
                self._failure_generation(generation),
                normalized_student,
                normalized_session,
                reason_code,
                occurred_at,
            ),
        )
        if cur.rowcount == 1:
            return int(cur.lastrowid)
        existing = c.execute(
            """SELECT student_id, session_id FROM system_failure_occurrences
               WHERE kind = ? AND logical_key = ? AND generation = ?
                 AND reason_code = ?""",
            (
                kind,
                normalized_key,
                self._failure_generation(generation),
                reason_code,
            ),
        ).fetchone()
        if existing is None or (
            str(existing["student_id"]) != normalized_student
            or str(existing["session_id"] or "") != normalized_session
        ):
            raise ValueError("failure occurrence identity conflict")
        return None

    def seed_legacy_system_failure_occurrences(self) -> int:
        """Idempotently capture only legacy failures still visible today."""
        before = 0
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            before = int(c.execute(
                "SELECT COUNT(*) FROM system_failure_occurrences",
            ).fetchone()[0])
            c.execute(
                """UPDATE reports
                   SET analysis_attempts = 1
                   WHERE event = 'Stop' AND analysis_status = 'failed'
                     AND analysis_attempts < 1
                     AND (
                       analysis_error = 'analysis_input_unavailable'
                       OR analysis_next_retry_at IS NULL
                     )"""
            )
            c.execute(
                """UPDATE raw_transcripts
                   SET analysis_generation = 1
                   WHERE analysis_status = 'failed' AND analysis_generation < 1"""
            )
            c.execute(
                """UPDATE upload_requests
                   SET transfer_failure_generation = 1
                   WHERE transfer_status = 'failed'
                     AND transfer_failure_generation < 1"""
            )
            c.execute(
                """UPDATE upload_requests
                   SET analysis_failure_generation = 1
                   WHERE analysis_status = 'failed'
                     AND analysis_failure_generation < 1"""
            )

            rows = c.execute(
                """SELECT id, student_id, session_id, analysis_attempts,
                          analysis_error, created_at
                   FROM reports
                   WHERE event = 'Stop' AND analysis_status = 'failed'
                     AND (
                       analysis_error = 'analysis_input_unavailable'
                       OR (
                         analysis_next_retry_at IS NULL
                         AND analysis_attempts > 0
                       )
                     )"""
            ).fetchall()
            for row in rows:
                reason_code = (
                    SYSTEM_FAILURE_REASON_CODES["stop_input_unavailable"]
                    if row["analysis_error"] == "analysis_input_unavailable"
                    else SYSTEM_FAILURE_REASON_CODES["stop_retries_exhausted"]
                )
                self._insert_system_failure_occurrence_with_conn(
                    c,
                    kind="stop",
                    logical_key=row["id"],
                    generation=row["analysis_attempts"],
                    student_id=row["student_id"],
                    session_id=row["session_id"],
                    reason_code=reason_code,
                    created_at=row["created_at"],
                )

            rows = c.execute(
                """SELECT id, student_id, session_id, analysis_generation, created_at
                   FROM raw_transcripts WHERE analysis_status = 'failed'"""
            ).fetchall()
            for row in rows:
                self._insert_system_failure_occurrence_with_conn(
                    c,
                    kind="bulk_analysis",
                    logical_key=row["id"],
                    generation=row["analysis_generation"],
                    student_id=row["student_id"],
                    session_id=row["session_id"],
                    reason_code=SYSTEM_FAILURE_REASON_CODES["bulk_analysis"],
                    created_at=row["created_at"],
                )

            rows = c.execute(
                """SELECT request_id, student_id, session_id,
                          transfer_status, analysis_status,
                          transfer_failure_generation,
                          analysis_failure_generation,
                          COALESCE(updated_at, created_at) AS failure_at
                   FROM upload_requests
                   WHERE transfer_status = 'failed' OR analysis_status = 'failed'"""
            ).fetchall()
            for row in rows:
                if row["transfer_status"] == "failed":
                    self._insert_system_failure_occurrence_with_conn(
                        c,
                        kind="upload_transfer",
                        logical_key=row["request_id"],
                        generation=row["transfer_failure_generation"],
                        student_id=row["student_id"],
                        session_id=row["session_id"],
                        reason_code=SYSTEM_FAILURE_REASON_CODES["upload_transfer"],
                        created_at=row["failure_at"],
                    )
                if row["analysis_status"] == "failed":
                    self._insert_system_failure_occurrence_with_conn(
                        c,
                        kind="upload_analysis",
                        logical_key=row["request_id"],
                        generation=row["analysis_failure_generation"],
                        student_id=row["student_id"],
                        session_id=row["session_id"],
                        reason_code=SYSTEM_FAILURE_REASON_CODES["upload_analysis"],
                        created_at=row["failure_at"],
                    )
            after = int(c.execute(
                "SELECT COUNT(*) FROM system_failure_occurrences",
            ).fetchone()[0])
        return after - before

    def get_system_failure_occurrence(
        self,
        occurrence_id: int,
    ) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM system_failure_occurrences WHERE id = ?",
                (occurrence_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_latest_system_failure_occurrence(
        self,
        *,
        kind: str,
        logical_key: str,
        generation: int | None = None,
    ) -> dict[str, Any] | None:
        if kind not in {
            "stop", "bulk_analysis", "upload_transfer", "upload_analysis",
        }:
            raise ValueError("invalid system failure occurrence kind")
        generation_clause = " AND generation = ?" if generation is not None else ""
        params: list[Any] = [kind, str(logical_key)]
        if generation is not None:
            params.append(self._failure_generation(generation))
        with self._conn() as c:
            row = c.execute(
                f"""SELECT * FROM system_failure_occurrences
                    WHERE kind = ? AND logical_key = ?{generation_clause}
                    ORDER BY generation DESC, id DESC LIMIT 1""",
                params,
            ).fetchone()
            return dict(row) if row else None

    def list_system_failure_occurrences(
        self,
        *,
        kind: str,
        logical_key: str | None = None,
        generation: int | None = None,
        after_id: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if kind not in {
            "stop", "bulk_analysis", "upload_transfer", "upload_analysis",
        }:
            raise ValueError("invalid system failure occurrence kind")
        clauses = ["kind = ?", "id > ?"]
        params: list[Any] = [kind, max(0, int(after_id))]
        if logical_key is not None:
            clauses.append("logical_key = ?")
            params.append(str(logical_key))
        if generation is not None:
            clauses.append("generation = ?")
            params.append(self._failure_generation(generation))
        params.append(min(max(1, int(limit)), 200))
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT * FROM system_failure_occurrences
                    WHERE {' AND '.join(clauses)}
                    ORDER BY id ASC LIMIT ?""",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _attention_text(value: object, *, limit: int) -> str:
        return str(value or "").strip()[:limit]

    @staticmethod
    def _attention_timestamp(value: object, *, default: float) -> float:
        if isinstance(value, bool):
            return default
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            return default
        return timestamp if math.isfinite(timestamp) else default

    @classmethod
    def _normalize_attention_decision(
        cls,
        decision: Mapping[str, Any] | AttentionDecision,
    ) -> dict[str, Any]:
        values: Mapping[str, Any]
        if isinstance(decision, AttentionDecision):
            values = asdict(decision)
        elif isinstance(decision, Mapping):
            values = decision
        else:
            raise TypeError("attention decision must be a mapping")

        source_type = cls._attention_text(values.get("source_type"), limit=40)
        category = cls._attention_text(values.get("category"), limit=40)
        priority = cls._attention_text(values.get("priority"), limit=40)
        if source_type not in {"analysis", "student_ask", "system"}:
            raise ValueError("invalid attention source type")
        if category not in {"learning", "system"}:
            raise ValueError("invalid attention category")
        if priority not in {"high", "medium"}:
            raise ValueError("invalid attention priority")

        source_id = str(values.get("source_id") or "")
        student_id = str(values.get("student_id") or "")
        session_id = str(values.get("session_id") or "")
        reason_code = str(values.get("reason_code") or "").strip()
        if not source_id.strip():
            raise ValueError("attention source id is required")
        if not student_id.strip():
            raise ValueError("attention student id is required")
        if not reason_code:
            raise ValueError("attention reason code is required")

        evidence_value: object
        if "evidence" in values:
            evidence_value = values.get("evidence")
        else:
            evidence_value = values.get("evidence_json", "[]")
            if isinstance(evidence_value, str):
                try:
                    evidence_value = json.loads(evidence_value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    evidence_value = []
        if isinstance(evidence_value, tuple):
            evidence_value = list(evidence_value)
        evidence_json = json.dumps(
            normalize_evidence(evidence_value),
            ensure_ascii=False,
        )
        now = time.time()
        return {
            "source_type": source_type,
            "source_id": source_id,
            "category": category,
            "student_id": student_id,
            "session_id": session_id,
            "priority": priority,
            "reason_code": reason_code,
            "reason": cls._attention_text(values.get("reason"), limit=500),
            "evidence_json": evidence_json,
            "suggested_action": cls._attention_text(
                values.get("suggested_action"),
                limit=500,
            ),
            "confidence": normalize_confidence(values.get("confidence")),
            "created_at": cls._attention_timestamp(
                values.get("created_at"),
                default=now,
            ),
            "updated_at": now,
        }

    def insert_attention_decisions(
        self,
        decisions: Iterable[Mapping[str, Any] | AttentionDecision],
    ) -> list[dict[str, Any]]:
        """Insert only genuinely new source/reason projections atomically."""
        records = [self._normalize_attention_decision(item) for item in decisions]
        if not records:
            return []

        created: list[dict[str, Any]] = []
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            for record in records:
                c.execute(
                    """INSERT INTO students
                       (student_id, display_name, token_hash, created_at)
                       VALUES (?, NULL, NULL, ?)
                       ON CONFLICT(student_id) DO NOTHING""",
                    (record["student_id"], record["created_at"]),
                )
                cur = c.execute(
                    """INSERT INTO attention_items
                       (source_type, source_id, category, student_id, session_id,
                        priority, reason_code, reason, evidence_json,
                        suggested_action, confidence, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source_type, source_id, reason_code) DO NOTHING""",
                    (
                        record["source_type"],
                        record["source_id"],
                        record["category"],
                        record["student_id"],
                        record["session_id"],
                        record["priority"],
                        record["reason_code"],
                        record["reason"],
                        record["evidence_json"],
                        record["suggested_action"],
                        record["confidence"],
                        record["created_at"],
                        record["updated_at"],
                    ),
                )
                if cur.rowcount != 1:
                    existing = c.execute(
                        """SELECT student_id, session_id FROM attention_items
                           WHERE source_type = ? AND source_id = ?
                             AND reason_code = ?""",
                        (
                            record["source_type"],
                            record["source_id"],
                            record["reason_code"],
                        ),
                    ).fetchone()
                    if existing is None or (
                        str(existing["student_id"]) != record["student_id"]
                        or str(existing["session_id"] or "")
                        != record["session_id"]
                    ):
                        raise ValueError("attention source identity conflict")
                    continue
                row = c.execute(
                    "SELECT * FROM attention_items WHERE id = ?",
                    (cur.lastrowid,),
                ).fetchone()
                if row is not None:
                    created.append(dict(row))
        return created

    def get_attention(self, item_id: int) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM attention_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_attention(
        self,
        *,
        status: str | None = None,
        priority: str | None = None,
        category: str | None = None,
        student_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return the mentor queue: high first, then oldest within priority."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("invalid attention limit")
        filters = {
            "status": ({"open", "in_progress", "resolved", "dismissed"}, status),
            "priority": ({"high", "medium"}, priority),
            "category": ({"learning", "system"}, category),
        }
        clauses: list[str] = []
        params: list[Any] = []
        for column, (allowed, value) in filters.items():
            if value is None:
                continue
            if value not in allowed:
                raise ValueError(f"invalid attention {column}")
            clauses.append(f"{column} = ?")
            params.append(value)
        if student_id is not None:
            clauses.append("student_id = ?")
            params.append(student_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT * FROM attention_items{where}
                    ORDER BY CASE priority WHEN 'high' THEN 0 ELSE 1 END,
                             created_at ASC, id ASC
                    LIMIT ?""",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def get_system_status_counts(self) -> dict[str, int]:
        """Return aggregate operational counts without exposing record content."""
        with self._conn() as c:
            row = c.execute(
                """SELECT
                       (SELECT COUNT(*) FROM reports
                        WHERE analysis_status IN ('pending', 'running'))
                       +
                       (SELECT COUNT(*) FROM raw_transcripts
                        WHERE analysis_status IN ('pending', 'running'))
                         AS pending_analyses,
                       (SELECT COUNT(*) FROM reports
                        WHERE analysis_status = 'failed')
                       +
                       (SELECT COUNT(*) FROM raw_transcripts
                        WHERE analysis_status = 'failed')
                         AS failed_analyses,
                       (SELECT COUNT(*) FROM attention_items
                        WHERE status = 'open') AS open_attention"""
            ).fetchone()
        return {
            "pending_analyses": int(row["pending_analyses"]),
            "failed_analyses": int(row["failed_analyses"]),
            "open_attention": int(row["open_attention"]),
        }

    def update_attention_status(
        self,
        item_id: int,
        *,
        status: str,
        mentor_id: str,
        note: str,
    ) -> tuple[dict[str, Any], bool]:
        """Apply the queue state machine with exact-payload replay semantics."""
        if status not in {"in_progress", "resolved", "dismissed"}:
            raise ValueError("invalid attention status")
        normalized_mentor = self._attention_text(mentor_id, limit=200)
        normalized_note = str(note or "").strip()
        if not normalized_mentor:
            raise ValueError("attention mentor id is required")
        if len(normalized_note) > 500:
            raise ValueError("attention resolution note is too long")

        allowed_transitions = {
            "open": {"in_progress", "resolved", "dismissed"},
            "in_progress": {"resolved", "dismissed"},
            "resolved": set(),
            "dismissed": set(),
        }
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute(
                "SELECT * FROM attention_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if current is None:
                raise LookupError("attention item not found")

            current_status = str(current["status"] or "")
            current_mentor = str(current["handled_by"] or "")
            current_note = str(current["resolution_note"] or "")
            if (
                current_status == status
                and current_mentor == normalized_mentor
                and current_note == normalized_note
            ):
                return dict(current), False
            if status not in allowed_transitions.get(current_status, set()):
                raise ValueError("attention status conflict")

            now = time.time()
            handled_at = now if status in {"resolved", "dismissed"} else None
            changed = c.execute(
                """UPDATE attention_items
                   SET status = ?, handled_by = ?, resolution_note = ?,
                       handled_at = ?, updated_at = ?
                   WHERE id = ? AND status = ?""",
                (
                    status,
                    normalized_mentor,
                    normalized_note,
                    handled_at,
                    now,
                    item_id,
                    current_status,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("attention status conflict")
            updated = c.execute(
                "SELECT * FROM attention_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            return dict(updated), True

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)
        # 旧库迁移：补齐新增列
        self._migrate()
        self._backfill_upload_request_axes()
        self._backfill_legacy_prompt_report_ids()
        # 迁移后创建依赖新列的索引
        with self._conn() as c:
            for sql in _POST_MIGRATION_SQL:
                c.execute(sql)
        # Seed only after legacy rows have reached their canonical source state.
        # Otherwise a report already owning an analysis can briefly look failed
        # and create a permanent false system occurrence.
        self.seed_legacy_system_failure_occurrences()

    def _migrate(self) -> None:
        with self._conn() as c:
            for table, col, coltype in _MIGRATIONS:
                cols = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
                if col not in cols:
                    try:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
                    except sqlite3.OperationalError as exc:
                        if "duplicate column name" not in str(exc).lower():
                            raise
                        log.info("迁移: %s.%s 已存在，跳过", table, col)
                    else:
                        log.info("迁移: %s.%s 已添加", table, col)

    def _backfill_upload_request_axes(self) -> None:
        """Map legacy upload state into the independent transfer axis once."""
        with self._conn() as c:
            c.execute(
                """UPDATE upload_requests
                   SET transfer_status = CASE status
                       WHEN 'running' THEN 'running'
                       WHEN 'done' THEN 'stored'
                       WHEN 'failed' THEN 'failed'
                       ELSE 'pending'
                   END
                   WHERE transfer_status IS NULL
                      OR transfer_status = ''
                      OR (transfer_status = 'pending' AND status != 'pending')"""
            )
            c.execute(
                """UPDATE upload_requests
                   SET analysis_status = 'not_requested'
                   WHERE analysis_status IS NULL OR analysis_status = ''"""
            )
            c.execute(
                """UPDATE upload_requests
                   SET transfer_error = error_message
                   WHERE (transfer_error IS NULL OR transfer_error = '')
                     AND error_message IS NOT NULL
                     AND error_message != ''"""
            )
            c.execute(
                """UPDATE upload_requests
                   SET status = CASE transfer_status
                       WHEN 'running' THEN 'running'
                       WHEN 'stored' THEN 'done'
                       WHEN 'failed' THEN 'failed'
                       ELSE 'pending'
                   END"""
            )
            c.execute(
                """UPDATE upload_requests
                   SET transfer_error = '' WHERE transfer_error IS NULL"""
            )
            c.execute(
                """UPDATE upload_requests
                   SET analysis_error = '' WHERE analysis_error IS NULL"""
            )

    def _backfill_legacy_prompt_report_ids(self) -> None:
        """Conservatively pair legacy pending Stop prompts before adding uniqueness."""
        with self._conn() as c:
            reports = [
                dict(row)
                for row in c.execute(
                    """SELECT id, student_id, session_id, prompt, created_at
                       FROM reports
                       WHERE event = 'Stop'
                         AND event_id IS NULL
                         AND analysis_pending = 1
                         AND NOT EXISTS (
                           SELECT 1 FROM prompts
                           WHERE prompts.report_id = reports.id
                         )
                       ORDER BY created_at ASC, id ASC"""
                ).fetchall()
            ]
            prompts = [
                dict(row)
                for row in c.execute(
                    """SELECT id, student_id, session_id, content, created_at
                       FROM prompts
                       WHERE report_id IS NULL
                       ORDER BY created_at ASC, id ASC"""
                ).fetchall()
            ]
            report_candidates: dict[int, set[int]] = {}
            prompt_candidates: dict[int, set[int]] = {}
            for report in reports:
                report_prompt = str(report.get("prompt") or "")
                if not report_prompt:
                    continue
                for prompt in prompts:
                    if prompt.get("student_id") != report.get("student_id"):
                        continue
                    if prompt.get("session_id") != report.get("session_id"):
                        continue
                    delta = float(prompt["created_at"]) - float(report["created_at"])
                    if not 0 <= delta <= LEGACY_PROMPT_BACKFILL_WINDOW_SECONDS:
                        continue
                    if str(prompt.get("content") or "") != report_prompt:
                        continue
                    report_id = int(report["id"])
                    prompt_id = int(prompt["id"])
                    report_candidates.setdefault(report_id, set()).add(prompt_id)
                    prompt_candidates.setdefault(prompt_id, set()).add(report_id)

            for report_id, matching_prompts in sorted(report_candidates.items()):
                if len(matching_prompts) != 1:
                    continue
                prompt_id = next(iter(matching_prompts))
                if prompt_candidates.get(prompt_id) != {report_id}:
                    continue
                updated = c.execute(
                    """UPDATE prompts SET report_id = ?
                       WHERE id = ? AND report_id IS NULL""",
                    (report_id, prompt_id),
                ).rowcount
                if updated != 1:
                    log.warning("迁移: prompt %s 未能关联 report %s", prompt_id, report_id)

    def add_report(
        self,
        student_id: str,
        session_id: str | None,
        event: str,
        prompt: str,
        transcript_path: str,
        msg_count: int,
        tool_calls: int,
    ) -> int:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._ensure_session_owner_with_conn(c, session_id, student_id)
            return self._add_report_with_conn(
                c,
                student_id=student_id,
                session_id=session_id,
                event=event,
                prompt=prompt,
                transcript_path=transcript_path,
                msg_count=msg_count,
                tool_calls=tool_calls,
            )

    def accept_report(
        self,
        *,
        student_id: str,
        session_id: str | None,
        event: str,
        event_id: str | None,
        prompt: str,
        transcript_path: str,
        msg_count: int,
        tool_calls: int,
        analysis_input: str | None,
        work_dir: str = "",
        title: str = "",
        raw_transcript_content: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically persist a report or return the original idempotent row."""
        normalized_event_id = normalize_event_id(event_id)
        analysis_status = "pending" if event == "Stop" else "not_requested"
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            if normalized_event_id is not None:
                existing = c.execute(
                    """SELECT * FROM reports
                       WHERE student_id = ? AND event_id = ?
                       LIMIT 1""",
                    (student_id, normalized_event_id),
                ).fetchone()
                if existing:
                    if str(existing["event"] or "") != event:
                        raise ValueError(
                            "event_id already belongs to a different event type"
                        )
                    return dict(existing), True

            self._ensure_session_owner_with_conn(c, session_id, student_id)

            report_id = self._add_report_with_conn(
                c,
                student_id=student_id,
                session_id=session_id,
                event=event,
                event_id=normalized_event_id,
                prompt=prompt,
                transcript_path=transcript_path,
                msg_count=msg_count,
                tool_calls=tool_calls,
                analysis_input=analysis_input if event == "Stop" else None,
                analysis_status=analysis_status,
                analysis_pending=event == "Stop",
            )
            if session_id:
                self._upsert_session_with_conn(
                    c,
                    session_id=session_id,
                    student_id=student_id,
                    work_dir=work_dir,
                    title=title,
                )
            if raw_transcript_content is not None and session_id:
                self._add_raw_transcript_with_conn(
                    c,
                    session_id=session_id,
                    student_id=student_id,
                    content=raw_transcript_content,
                )
            row = c.execute(
                "SELECT * FROM reports WHERE id = ?",
                (report_id,),
            ).fetchone()
            return dict(row), False

    def _add_report_with_conn(
        self,
        c: sqlite3.Connection,
        *,
        student_id: str,
        session_id: str | None,
        event: str,
        transcript_path: str,
        msg_count: int,
        tool_calls: int,
        prompt: str = "",
        event_id: str | None = None,
        analysis_input: str | None = None,
        analysis_status: str = "not_requested",
        analysis_pending: bool = False,
    ) -> int:
        """Insert a report using an existing transaction."""
        now = time.time()
        c.execute(
            """INSERT INTO students (student_id, display_name, token_hash, created_at)
               VALUES (?, ?, NULL, ?)
               ON CONFLICT(student_id) DO NOTHING""",
            (student_id, "", now),
        )
        cur = c.execute(
            """INSERT INTO reports
               (student_id, session_id, event, prompt, transcript_path,
                msg_count, tool_calls, analysis_pending, event_id,
                analysis_input, analysis_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                student_id,
                session_id,
                event,
                prompt,
                transcript_path,
                msg_count,
                tool_calls,
                1 if analysis_pending else 0,
                event_id,
                analysis_input,
                analysis_status,
                now,
            ),
        )
        return int(cur.lastrowid)

    def upsert_student(self, student_id: str, display_name: str | None = None) -> None:
        """Create or update a student row.

        `display_name=None` preserves an existing display name while still creating
        the parent row required by mentor_messages' FK.
        """
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO students (student_id, display_name, token_hash, created_at)
                   VALUES (?, ?, NULL, ?)
                   ON CONFLICT(student_id) DO UPDATE SET
                     display_name = CASE
                       WHEN excluded.display_name IS NULL THEN students.display_name
                       ELSE excluded.display_name
                     END""",
                (student_id, display_name, now),
            )

    def upsert_session(
        self,
        session_id: str,
        student_id: str,
        work_dir: str,
        title: str,
        created_at: float | None = None,
        last_activity_at: float | None = None,
        group_type: str | None = None,
        space_name: str | None = None,
    ) -> None:
        """Create or update a session row keyed by globally unique session_id."""
        now = time.time()
        created = now if created_at is None else created_at
        last_activity = now if last_activity_at is None else last_activity_at
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._upsert_session_with_conn(
                c,
                session_id=session_id,
                student_id=student_id,
                work_dir=work_dir,
                title=title,
                created_at=created,
                last_activity_at=last_activity,
                group_type=group_type,
                space_name=space_name,
            )

    def _upsert_session_with_conn(
        self,
        c: sqlite3.Connection,
        *,
        session_id: str,
        student_id: str,
        work_dir: str,
        title: str,
        created_at: float | None = None,
        last_activity_at: float | None = None,
        group_type: str | None = None,
        space_name: str | None = None,
    ) -> None:
        now = time.time()
        created = now if created_at is None else created_at
        last_activity = now if last_activity_at is None else last_activity_at
        self._ensure_session_owner_with_conn(c, session_id, student_id)
        c.execute(
            """INSERT INTO sessions
               (session_id, student_id, work_dir, title, group_type, space_name,
                created_at, last_activity_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 student_id = CASE
                   WHEN sessions.student_id IS NULL OR sessions.student_id = ''
                   THEN excluded.student_id
                   ELSE sessions.student_id
                 END,
                 work_dir = CASE
                   WHEN sessions.work_dir IS NULL OR sessions.work_dir = ''
                   THEN excluded.work_dir
                   ELSE sessions.work_dir
                 END,
                 title = COALESCE(NULLIF(excluded.title, ''), sessions.title),
                 group_type = COALESCE(NULLIF(excluded.group_type, ''), sessions.group_type),
                 space_name = COALESCE(NULLIF(excluded.space_name, ''), sessions.space_name),
                 last_activity_at = excluded.last_activity_at""",
            (
                session_id,
                student_id,
                work_dir,
                title,
                group_type,
                space_name,
                created,
                last_activity,
            ),
        )

    def _ensure_session_owner_with_conn(
        self,
        c: sqlite3.Connection,
        session_id: str | None,
        student_id: str,
    ) -> None:
        if not session_id:
            return
        existing = c.execute(
            "SELECT student_id FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if existing is None:
            return
        existing_student = str(existing["student_id"] or "")
        if not existing_student:
            updated = c.execute(
                """UPDATE sessions
                   SET student_id = ?
                   WHERE session_id = ?
                     AND (student_id IS NULL OR student_id = '')""",
                (student_id, session_id),
            ).rowcount
            if updated == 1:
                return
            current = c.execute(
                "SELECT student_id FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            existing_student = str(current["student_id"] or "") if current else ""
        if existing_student != student_id:
            raise ValueError(
                f"session {session_id!r} belongs to {existing_student!r}, "
                f"not {student_id!r}"
            )

    def ensure_session_owner(self, session_id: str, student_id: str) -> None:
        """Atomically bind an unknown session or reject another student's session."""
        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._upsert_session_with_conn(
                c,
                session_id=session_id,
                student_id=student_id,
                work_dir="",
                title="",
                created_at=now,
                last_activity_at=now,
            )

    def get_sessions_by_student_from_table(self, student_id: str, limit: int = 1000) -> list[dict]:
        """Read a student's sessions from the new copilot.db sessions table."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT
                     s.session_id,
                     s.student_id,
                     s.title AS session_title,
                     s.work_dir,
                     s.group_type,
                     s.space_name,
                     s.created_at,
                     s.last_activity_at AS last_ts,
                     COUNT(a.id) AS analysis_count,
                     COALESCE(MAX(mc.c), 0) AS message_count,
                     COALESCE(SUM(CASE
                       WHEN a.alert != '' OR a.understanding IN ('low','stuck')
                       THEN 1 ELSE 0 END), 0) AS alert_count,
                     COALESCE((
                       SELECT a2.diagnosis
                       FROM analyses a2
                       WHERE a2.student_id = s.student_id
                         AND a2.session_id = s.session_id
                       ORDER BY a2.created_at DESC, a2.id DESC
                       LIMIT 1
                     ), '') AS last_diagnosis,
                     COALESCE((
                       SELECT a2.topic
                       FROM analyses a2
                       WHERE a2.student_id = s.student_id
                         AND a2.session_id = s.session_id
                       ORDER BY a2.created_at DESC, a2.id DESC
                       LIMIT 1
                     ), '') AS last_topic,
                     CASE MAX(CASE a.severity
                       WHEN 'error' THEN 3
                       WHEN 'warn' THEN 2
                       ELSE 1
                     END)
                       WHEN 3 THEN 'error'
                       WHEN 2 THEN 'warn'
                       ELSE 'info'
                     END AS last_severity,
                     COALESCE(MAX(a.is_technical), 0) AS last_is_technical
                   FROM sessions s
                   LEFT JOIN analyses a
                     ON a.student_id = s.student_id
                    AND a.session_id = s.session_id
                   LEFT JOIN (
                     SELECT session_id, COUNT(*) AS c
                     FROM messages
                     GROUP BY session_id
                   ) mc
                     ON mc.session_id = s.session_id
                   WHERE s.student_id = ?
                   GROUP BY s.session_id
                   ORDER BY s.last_activity_at DESC, s.created_at DESC
                   LIMIT ?""",
                (student_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_session_title(self, session_id: str) -> str:
        """Return a session title from copilot.db."""
        with self._conn() as c:
            row = c.execute(
                "SELECT title FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return (row["title"] or "") if row else ""

    def get_active_session_from_table(
        self,
        work_dir: str | None = None,
        student_id: str | None = None,
    ) -> dict | None:
        """Return the most recently active session from copilot.db."""
        clauses = []
        params: list[Any] = []
        if work_dir:
            clauses.append("work_dir = ?")
            params.append(work_dir)
        if student_id:
            clauses.append("student_id = ?")
            params.append(student_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as c:
            row = c.execute(
                f"""SELECT
                      session_id,
                      student_id,
                      work_dir,
                      title,
                      created_at,
                      last_activity_at AS resumed_at
                    FROM sessions
                    {where}
                    ORDER BY last_activity_at DESC, created_at DESC
                    LIMIT 1""",
                params,
            ).fetchone()
            return dict(row) if row else None

    def list_sessions_from_table(
        self,
        work_dir: str | None = None,
        student_id: str | None = None,
        limit: int = 8,
    ) -> list[dict]:
        """Return recent sessions from copilot.db."""
        clauses = []
        params: list[Any] = []
        if work_dir:
            clauses.append("work_dir = ?")
            params.append(work_dir)
        if student_id:
            clauses.append("student_id = ?")
            params.append(student_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT
                      session_id,
                      student_id,
                      work_dir,
                      title,
                      created_at,
                      last_activity_at AS resumed_at
                    FROM sessions
                    {where}
                    ORDER BY last_activity_at DESC, created_at DESC
                    LIMIT ?""",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def add_mentor_message(
        self,
        student_id: str,
        mentor_id: str,
        session_id: str,
        text: str,
        message_id: str,
    ) -> int:
        """Persist a mentor message as undelivered and return its row id."""
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO mentor_messages
                   (student_id, mentor_id, session_id, text, message_id,
                    created_at, delivered_at, read_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)""",
                (student_id, mentor_id, session_id, text, message_id, time.time()),
            )
            return cur.lastrowid

    def get_or_create_mentor_message(
        self,
        *,
        student_id: str,
        mentor_id: str,
        session_id: str,
        text: str,
        message_id: str,
        client_request_id: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically persist one retry-safe mentor message.

        The client key is global and opaque. Reusing it with a different
        payload is a conflict rather than permission to mutate the original
        message. The returned boolean is true only for the inserting caller.
        """
        request_id = str(client_request_id or "")
        if not request_id or len(request_id) > 128:
            raise ValueError("invalid client_request_id")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
        if any(char not in allowed for char in request_id):
            raise ValueError("invalid client_request_id")

        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute(
                "SELECT * FROM mentor_messages WHERE client_request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                row = dict(existing)
                identity = (
                    str(row.get("student_id") or ""),
                    str(row.get("mentor_id") or ""),
                    str(row.get("session_id") or ""),
                    str(row.get("text") or ""),
                )
                if identity != (student_id, mentor_id, session_id, text):
                    raise ValueError("client_request_id payload conflict")
                return row, False

            created_at = time.time()
            c.execute(
                """INSERT INTO students
                   (student_id, display_name, token_hash, created_at)
                   VALUES (?, '', NULL, ?)
                   ON CONFLICT(student_id) DO NOTHING""",
                (student_id, created_at),
            )
            cur = c.execute(
                """INSERT INTO mentor_messages
                   (student_id, mentor_id, session_id, text, message_id,
                    client_request_id, created_at, delivered_at, read_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)""",
                (
                    student_id,
                    mentor_id,
                    session_id,
                    text,
                    message_id,
                    request_id,
                    created_at,
                ),
            )
            row = c.execute(
                "SELECT * FROM mentor_messages WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
            return dict(row), True

    def list_mentor_messages_by_client_request_ids(
        self,
        client_request_ids: Iterable[str],
    ) -> list[dict[str, Any]]:
        """Return found rows once, preserving the caller's first-seen order."""
        ordered_ids = list(dict.fromkeys(
            str(value) for value in client_request_ids if str(value)
        ))
        if not ordered_ids:
            return []
        if len(ordered_ids) > 300:
            raise ValueError("too many client_request_ids")
        placeholders = ",".join("?" for _ in ordered_ids)
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT client_request_id, message_id, id, student_id,
                           delivered_at
                    FROM mentor_messages
                    WHERE client_request_id IN ({placeholders})""",
                ordered_ids,
            ).fetchall()
        by_request_id = {str(row["client_request_id"]): dict(row) for row in rows}
        return [by_request_id[value] for value in ordered_ids if value in by_request_id]

    def _message_cursor_id(
        self,
        c: sqlite3.Connection,
        student_id: str,
        message_id: int | str | None,
    ) -> int:
        if message_id is None:
            return 0
        if isinstance(message_id, int):
            row = c.execute(
                "SELECT id FROM mentor_messages WHERE id = ? AND student_id = ?",
                (message_id, student_id),
            ).fetchone()
            return int(row["id"]) if row else 0
        try:
            numeric_id = int(message_id)
        except ValueError:
            row = c.execute(
                "SELECT id FROM mentor_messages WHERE message_id = ? AND student_id = ?",
                (message_id, student_id),
            ).fetchone()
            return int(row["id"]) if row else 0
        row = c.execute(
            "SELECT id FROM mentor_messages WHERE id = ? AND student_id = ?",
            (numeric_id, student_id),
        ).fetchone()
        return int(row["id"]) if row else 0

    def list_undelivered_messages(
        self,
        student_id: str,
        after_message_id: int | str | None = None,
        *,
        limit: int | None = None,
    ) -> list[dict]:
        with self._conn() as c:
            cursor_id = self._message_cursor_id(c, student_id, after_message_id)
            query = """SELECT * FROM mentor_messages
                       WHERE student_id = ? AND delivered_at IS NULL AND id > ?
                       ORDER BY id ASC"""
            params: list[Any] = [student_id, cursor_id]
            if limit is not None:
                query += " LIMIT ?"
                params.append(max(0, int(limit)))
            rows = c.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def list_pending_message_receipts(
        self,
        student_id: str,
        *,
        limit: int,
        after_id: int = 0,
    ) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM mentor_messages
                   WHERE student_id = ? AND delivered_at IS NULL AND id > ?
                   ORDER BY id ASC LIMIT ?""",
                (student_id, max(0, int(after_id)), max(1, int(limit))),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_messages_since(
        self,
        student_id: str,
        last_seen_message_id: int | str | None,
        *,
        limit: int | None = None,
    ) -> list[dict]:
        with self._conn() as c:
            cursor_id = self._message_cursor_id(c, student_id, last_seen_message_id)
            query = """SELECT * FROM mentor_messages
                       WHERE student_id = ? AND id > ?
                       ORDER BY id ASC"""
            params: list[Any] = [student_id, cursor_id]
            if limit is not None:
                query += " LIMIT ?"
                params.append(max(0, int(limit)))
            rows = c.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def mark_message_delivered(self, message_id: str, student_id: str | None = None) -> int:
        with self._conn() as c:
            if student_id is not None:
                cur = c.execute(
                    """UPDATE mentor_messages
                       SET delivered_at = COALESCE(delivered_at, ?)
                       WHERE message_id = ? AND student_id = ?""",
                    (time.time(), message_id, student_id),
                )
            else:
                cur = c.execute(
                    """UPDATE mentor_messages
                       SET delivered_at = COALESCE(delivered_at, ?)
                       WHERE message_id = ?""",
                    (time.time(), message_id),
                )
            return cur.rowcount

    def mark_message_read(self, message_id: str, student_id: str | None = None) -> int:
        with self._conn() as c:
            if student_id is not None:
                cur = c.execute(
                    """UPDATE mentor_messages
                       SET read_at = COALESCE(read_at, ?)
                       WHERE message_id = ? AND student_id = ?""",
                    (time.time(), message_id, student_id),
                )
            else:
                cur = c.execute(
                    """UPDATE mentor_messages
                       SET read_at = COALESCE(read_at, ?)
                       WHERE message_id = ?""",
                    (time.time(), message_id),
                )
            return cur.rowcount

    def add_raw_transcript(
        self,
        session_id: str,
        student_id: str,
        content: str,
        content_sha256: str | None = None,
    ) -> int:
        """Persist complete raw transcript content without truncation."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            return self._add_raw_transcript_with_conn(
                c,
                session_id=session_id,
                student_id=student_id,
                content=content,
                content_sha256=content_sha256,
            )

    def _add_raw_transcript_with_conn(
        self,
        c: sqlite3.Connection,
        *,
        session_id: str,
        student_id: str,
        content: str,
        content_sha256: str | None = None,
    ) -> int:
        self._ensure_session_owner_with_conn(c, session_id, student_id)
        cur = c.execute(
            """INSERT INTO raw_transcripts
               (session_id, student_id, content, content_sha256,
                analysis_status, analysis_error, created_at)
               VALUES (?, ?, ?, ?, '', '', ?)""",
            (session_id, student_id, content, content_sha256, time.time()),
        )
        return int(cur.lastrowid)

    def replace_session_messages(
        self,
        session_id: str,
        student_id: str,
        turns: list[dict[str, Any]],
        raw: str,
        sha: str,
    ) -> int:
        """Replace one session's bulk-uploaded message content atomically."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            return self._replace_session_messages_with_conn(
                c,
                session_id=session_id,
                student_id=student_id,
                turns=turns,
                raw=raw,
                sha=sha,
            )

    def replace_session_messages_from_stop(
        self,
        *,
        session_id: str,
        student_id: str,
        turns: list[dict[str, Any]],
        raw: str,
        sha: str,
        source_report_id: int,
        source_event_id: str,
    ) -> dict[str, Any]:
        """Atomically validate the source Stop and store context only."""
        normalized_event_id = normalize_event_id(source_event_id)
        if normalized_event_id is None:
            raise ValueError("source_event_id is required")
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            report = c.execute(
                """SELECT student_id, session_id, event, event_id
                   FROM reports WHERE id = ?""",
                (int(source_report_id),),
            ).fetchone()
            if report is None:
                raise ValueError("source Stop report not found")
            if (
                str(report["student_id"] or "") != student_id
                or str(report["session_id"] or "") != session_id
                or str(report["event"] or "") != "Stop"
                or str(report["event_id"] or "") != normalized_event_id
            ):
                raise ValueError("source Stop report mismatch")
            source_id = int(source_report_id)
            watermark = c.execute(
                """SELECT source_report_id, source_event_id, content_sha256
                   FROM stop_transcript_watermarks
                   WHERE student_id = ? AND session_id = ?""",
                (student_id, session_id),
            ).fetchone()
            if watermark is not None:
                applied_report_id = int(watermark["source_report_id"])
                if source_id < applied_report_id:
                    stored = int(c.execute(
                        """SELECT COUNT(*) FROM messages
                           WHERE student_id = ? AND session_id = ?
                             AND source = 'bulk'""",
                        (student_id, session_id),
                    ).fetchone()[0])
                    return {"stored": stored, "skipped": True, "obsolete": True}
                if source_id == applied_report_id:
                    if (
                        str(watermark["source_event_id"]) != normalized_event_id
                        or str(watermark["content_sha256"]) != sha
                    ):
                        raise ValueError("source Stop transcript collision")
                    stored = int(c.execute(
                        """SELECT COUNT(*) FROM messages
                           WHERE student_id = ? AND session_id = ?
                             AND source = 'bulk'""",
                        (student_id, session_id),
                        ).fetchone()[0])
                    return {"stored": stored, "skipped": True, "obsolete": False}
            active_raw = c.execute(
                """SELECT 1 FROM raw_transcripts
                   WHERE student_id = ? AND session_id = ?
                     AND content_sha256 != ?
                     AND analysis_status IN ('pending', 'running')
                   LIMIT 1""",
                (student_id, session_id, sha),
            ).fetchone()
            active_request = c.execute(
                """SELECT 1 FROM upload_request_sessions
                   WHERE student_id = ? AND session_id = ? AND sha != ?
                     AND analysis_status IN ('pending', 'running')
                   LIMIT 1""",
                (student_id, session_id, sha),
            ).fetchone()
            if active_raw is not None or active_request is not None:
                raise ActiveTranscriptAnalysisConflict(
                    "mentor transcript analysis is active; retry store_only later"
                )
            existing = c.execute(
                """SELECT 1 FROM raw_transcripts
                   WHERE student_id = ? AND session_id = ? AND content_sha256 = ?
                   LIMIT 1""",
                (student_id, session_id, sha),
            ).fetchone()
            stored = self._replace_session_messages_with_conn(
                c,
                session_id=session_id,
                student_id=student_id,
                turns=turns,
                raw=raw,
                sha=sha,
            )
            c.execute(
                """INSERT INTO stop_transcript_watermarks
                   (student_id, session_id, source_report_id, source_event_id,
                    content_sha256, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(student_id, session_id) DO UPDATE SET
                     source_report_id = excluded.source_report_id,
                     source_event_id = excluded.source_event_id,
                     content_sha256 = excluded.content_sha256,
                     updated_at = excluded.updated_at""",
                (
                    student_id,
                    session_id,
                    source_id,
                    normalized_event_id,
                    sha,
                    time.time(),
                ),
            )
            return {
                "stored": stored,
                "skipped": existing is not None,
                "obsolete": False,
            }

    def _replace_session_messages_with_conn(
        self,
        c: sqlite3.Connection,
        *,
        session_id: str,
        student_id: str,
        turns: list[dict[str, Any]],
        raw: str,
        sha: str,
    ) -> int:
        now = time.time()
        self._ensure_session_owner_with_conn(c, session_id, student_id)
        c.execute(
            """INSERT INTO students (student_id, display_name, token_hash, created_at)
               VALUES (?, '', NULL, ?)
               ON CONFLICT(student_id) DO NOTHING""",
            (student_id, now),
        )
        c.execute(
            """INSERT INTO sessions
               (session_id, student_id, work_dir, title, created_at, last_activity_at)
               VALUES (?, ?, '', '', ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 last_activity_at = CASE
                   WHEN sessions.last_activity_at IS NULL
                     OR sessions.last_activity_at < excluded.last_activity_at
                   THEN excluded.last_activity_at
                   ELSE sessions.last_activity_at
                END""",
            (session_id, student_id, now, now),
        )
        current_bulk = c.execute(
            """SELECT content_sha256
               FROM raw_transcripts
               WHERE session_id = ? AND student_id = ?
                 AND content_sha256 IS NOT NULL AND content_sha256 != ''
               ORDER BY created_at DESC, id DESC
               LIMIT 1""",
            (session_id, student_id),
        ).fetchone()
        if current_bulk is not None and str(current_bulk["content_sha256"]) == sha:
            return int(c.execute(
                """SELECT COUNT(*) FROM messages
                   WHERE session_id = ? AND source = 'bulk'""",
                (session_id,),
            ).fetchone()[0])
        preserved_summaries: dict[tuple[int, str], str] = {}
        if sha:
            rows = c.execute(
                """SELECT seq, text, summary
                   FROM messages
                   WHERE session_id = ?
                     AND source = 'bulk'
                     AND role = 'user'
                     AND content_sha256 = ?
                     AND COALESCE(summary, '') != ''""",
                (session_id, sha),
            ).fetchall()
            preserved_summaries = {
                (int(row["seq"]), str(row["text"] or "")): str(row["summary"] or "")
                for row in rows
            }
        c.execute(
            "DELETE FROM messages WHERE session_id = ? AND source = 'bulk'",
            (session_id,),
        )

        inserted = 0
        for idx, turn in enumerate(turns):
            role = str(turn.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            text = str(turn.get("text") or "")
            if not text:
                continue
            seq = int(turn.get("seq") or 0)
            ts = turn.get("ts")
            created_at = ts if isinstance(ts, (int, float)) else now + (idx / 1000.0)
            summary = preserved_summaries.get((seq, text)) if role == "user" else None
            c.execute(
                """INSERT INTO messages
                   (session_id, student_id, seq, role, text, summary, source,
                    content_sha256, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'bulk', ?, ?)""",
                (session_id, student_id, seq, role, text, summary, sha, float(created_at)),
            )
            inserted += 1

        raw_row = c.execute(
            """SELECT id FROM raw_transcripts
               WHERE session_id = ? AND student_id = ?
               ORDER BY created_at DESC, id DESC
               LIMIT 1""",
            (session_id, student_id),
        ).fetchone()
        if raw_row:
            c.execute(
                """UPDATE raw_transcripts
                   SET content = ?, content_sha256 = ?, created_at = ?,
                       analysis_status = '', analysis_error = '',
                       analysis_model = '', analysis_prompt_hash = '',
                       analysis_latency_ms = 0, analysis_attempts = 0
                   WHERE id = ?""",
                (raw, sha, now, raw_row["id"]),
            )
        else:
            c.execute(
                """INSERT INTO raw_transcripts
                   (session_id, student_id, content, content_sha256,
                    analysis_status, analysis_error, created_at)
                   VALUES (?, ?, ?, ?, '', '', ?)""",
                (session_id, student_id, raw, sha, now),
            )
        return inserted

    def set_raw_transcript_analysis_status(
        self,
        session_id: str,
        student_id: str,
        *,
        status: str,
        error_message: str | None = None,
        content_sha256: str | None = None,
        analysis_model: str | None = None,
        prompt_hash: str | None = None,
        latency_ms: int = 0,
        increment_attempt: bool = False,
    ) -> int:
        """Update the latest raw transcript analysis status for a student session."""
        where = "session_id = ? AND student_id = ?"
        params: list[Any] = [session_id, student_id]
        if content_sha256:
            where += " AND content_sha256 = ?"
            params.append(content_sha256)
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                f"""SELECT id, session_id, student_id, analysis_status,
                           analysis_generation
                    FROM raw_transcripts
                    WHERE {where}
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1""",
                params,
            ).fetchone()
            if not row:
                return 0
            try:
                normalized_latency = max(0, int(latency_ms))
            except (TypeError, ValueError, OverflowError):
                normalized_latency = 0
            normalized_model = (
                str(analysis_model)[:200] if analysis_model is not None else None
            )
            normalized_hash = (
                str(prompt_hash)[:128] if prompt_hash is not None else None
            )
            update_where = "id = ?"
            update_params: list[Any] = [row["id"]]
            if content_sha256:
                update_where += " AND content_sha256 = ?"
                update_params.append(content_sha256)
            cur = c.execute(
                f"""UPDATE raw_transcripts
                   SET analysis_status = ?,
                       analysis_error = ?,
                       analysis_model = CASE
                         WHEN ? IS NULL THEN analysis_model ELSE ? END,
                       analysis_prompt_hash = CASE
                         WHEN ? IS NULL THEN analysis_prompt_hash ELSE ? END,
                       analysis_latency_ms = analysis_latency_ms + ?,
                       analysis_attempts = analysis_attempts + ?,
                       analysis_generation = CASE
                         WHEN ? = 'failed' AND analysis_generation < 1 THEN 1
                         ELSE analysis_generation END
                   WHERE {update_where}""",
                (
                    status,
                    error_message or "",
                    normalized_model,
                    normalized_model,
                    normalized_hash,
                    normalized_hash,
                    normalized_latency,
                    1 if increment_attempt else 0,
                    status,
                    *update_params,
                ),
            )
            if (
                cur.rowcount == 1
                and status == "failed"
                and str(row["analysis_status"] or "") != "failed"
            ):
                self._insert_system_failure_occurrence_with_conn(
                    c,
                    kind="bulk_analysis",
                    logical_key=row["id"],
                    generation=self._failure_generation(row["analysis_generation"]),
                    student_id=row["student_id"],
                    session_id=row["session_id"],
                    reason_code=SYSTEM_FAILURE_REASON_CODES["bulk_analysis"],
                )
            return cur.rowcount

    @staticmethod
    def _upload_request_ids_for_transcript_with_conn(
        c: sqlite3.Connection,
        student_id: str,
        session_id: str,
        sha: str,
        statuses: tuple[str, ...],
    ) -> list[str]:
        placeholders = ", ".join("?" for _ in statuses)
        rows = c.execute(
            f"""SELECT DISTINCT request_id
                FROM upload_request_sessions
                WHERE student_id = ? AND session_id = ? AND sha = ?
                  AND analysis_status IN ({placeholders})
                ORDER BY request_id""",
            (student_id, session_id, sha, *statuses),
        ).fetchall()
        return [str(row["request_id"]) for row in rows]

    def queue_raw_transcript_analysis(
        self,
        *,
        student_id: str,
        session_id: str,
        content_sha256: str,
    ) -> int:
        """Queue only an unqueued current SHA; never reopen running or done work."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            raw_row = c.execute(
                """SELECT id, content_sha256
                   FROM raw_transcripts
                   WHERE student_id = ? AND session_id = ?
                     AND content_sha256 IS NOT NULL AND content_sha256 != ''
                   ORDER BY created_at DESC, id DESC
                   LIMIT 1""",
                (student_id, session_id),
            ).fetchone()
            if raw_row is None or str(raw_row["content_sha256"]) != content_sha256:
                return 0
            return c.execute(
                """UPDATE raw_transcripts
                   SET analysis_status = 'pending', analysis_error = ''
                   WHERE id = ? AND student_id = ? AND session_id = ?
                     AND content_sha256 = ?
                     AND analysis_status IN ('', 'skipped')""",
                (
                    raw_row["id"],
                    student_id,
                    session_id,
                    content_sha256,
                ),
            ).rowcount

    def mark_raw_transcript_store_only(
        self,
        *,
        session_id: str,
        student_id: str,
        content_sha256: str,
    ) -> int:
        """Mark new context-only data without mutating existing analysis work."""
        with self._conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT id FROM raw_transcripts
                   WHERE student_id = ? AND session_id = ?
                     AND content_sha256 = ?
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (student_id, session_id, content_sha256),
            ).fetchone()
            if row is None:
                return 0
            return connection.execute(
                """UPDATE raw_transcripts
                   SET analysis_status = 'skipped', analysis_error = ''
                   WHERE id = ? AND student_id = ? AND session_id = ?
                     AND content_sha256 = ?
                     AND COALESCE(analysis_status, '') IN ('', 'not_requested')""",
                (row["id"], student_id, session_id, content_sha256),
            ).rowcount

    def claim_raw_transcript_analysis(
        self,
        *,
        student_id: str,
        session_id: str,
        content_sha256: str,
        prompt_hash: str,
    ) -> dict[str, Any]:
        """Claim exactly one current raw transcript generation for analysis."""
        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            raw_row = c.execute(
                """SELECT id, content_sha256, analysis_status,
                          analysis_attempts, analysis_generation
                   FROM raw_transcripts
                   WHERE student_id = ? AND session_id = ?
                     AND content_sha256 IS NOT NULL AND content_sha256 != ''
                   ORDER BY created_at DESC, id DESC
                   LIMIT 1""",
                (student_id, session_id),
            ).fetchone()
            if raw_row is None or str(raw_row["content_sha256"]) != content_sha256:
                request_ids = self._upload_request_ids_for_transcript_with_conn(
                    c,
                    student_id,
                    session_id,
                    content_sha256,
                    ("pending", "running"),
                )
                c.execute(
                    """UPDATE upload_request_sessions
                       SET analysis_status = 'failed',
                           analysis_error = 'analysis stale transcript',
                           updated_at = ?
                       WHERE student_id = ? AND session_id = ? AND sha = ?
                         AND analysis_status IN ('pending', 'running')""",
                    (now, student_id, session_id, content_sha256),
                )
                return {"state": "stale", "request_ids": request_ids}

            status = str(raw_row["analysis_status"] or "")
            if status == "running":
                request_ids = self._upload_request_ids_for_transcript_with_conn(
                    c,
                    student_id,
                    session_id,
                    content_sha256,
                    ("pending",),
                )
                c.execute(
                    """UPDATE upload_request_sessions
                       SET analysis_status = 'running', analysis_error = '', updated_at = ?
                       WHERE student_id = ? AND session_id = ? AND sha = ?
                         AND analysis_status = 'pending'""",
                    (now, student_id, session_id, content_sha256),
                )
                return {"state": "running", "request_ids": request_ids}
            if status == "done":
                request_ids = self._upload_request_ids_for_transcript_with_conn(
                    c,
                    student_id,
                    session_id,
                    content_sha256,
                    ("pending", "running"),
                )
                c.execute(
                    """UPDATE upload_request_sessions
                       SET analysis_status = 'done', analysis_error = '', updated_at = ?
                       WHERE student_id = ? AND session_id = ? AND sha = ?
                         AND analysis_status IN ('pending', 'running')""",
                    (now, student_id, session_id, content_sha256),
                )
                return {"state": "done", "request_ids": request_ids}
            if status not in {"pending", "failed"}:
                return {"state": "not_ready", "request_ids": []}

            old_generation = max(0, int(raw_row["analysis_generation"] or 0))
            updated = c.execute(
                """UPDATE raw_transcripts
                   SET analysis_status = 'running', analysis_error = '',
                       analysis_prompt_hash = ?,
                       analysis_attempts = analysis_attempts + 1,
                       analysis_generation = analysis_generation + 1
                   WHERE id = ? AND student_id = ? AND session_id = ?
                     AND content_sha256 = ?
                     AND analysis_status IN ('pending', 'failed')
                     AND analysis_generation = ?""",
                (
                    str(prompt_hash)[:128],
                    raw_row["id"],
                    student_id,
                    session_id,
                    content_sha256,
                    old_generation,
                ),
            ).rowcount
            if updated != 1:
                return {"state": "running", "request_ids": []}

            request_ids = self._upload_request_ids_for_transcript_with_conn(
                c,
                student_id,
                session_id,
                content_sha256,
                ("pending",),
            )
            c.execute(
                """UPDATE upload_request_sessions
                   SET analysis_status = 'running', analysis_error = '', updated_at = ?
                   WHERE student_id = ? AND session_id = ? AND sha = ?
                     AND analysis_status = 'pending'""",
                (now, student_id, session_id, content_sha256),
            )
            return {
                "state": "claimed",
                "raw_id": int(raw_row["id"]),
                "generation": old_generation + 1,
                "attempt": max(0, int(raw_row["analysis_attempts"] or 0)) + 1,
                "request_ids": request_ids,
            }

    def fail_raw_transcript_analysis(
        self,
        *,
        student_id: str,
        session_id: str,
        content_sha256: str,
        raw_id: int,
        generation: int,
        error_message: str,
        analysis_model: str,
        prompt_hash: str,
        latency_ms: int,
    ) -> dict[str, Any] | None:
        """Fail only the worker that still owns the current raw generation."""
        now = time.time()
        normalized_latency = max(0, int(latency_ms))
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute(
                """SELECT id, content_sha256, analysis_status, analysis_generation
                   FROM raw_transcripts
                   WHERE student_id = ? AND session_id = ?
                     AND content_sha256 IS NOT NULL AND content_sha256 != ''
                   ORDER BY created_at DESC, id DESC
                   LIMIT 1""",
                (student_id, session_id),
            ).fetchone()
            if (
                current is None
                or int(current["id"]) != int(raw_id)
                or str(current["content_sha256"]) != content_sha256
                or str(current["analysis_status"] or "") != "running"
                or int(current["analysis_generation"] or 0) != int(generation)
            ):
                return None
            request_ids = self._upload_request_ids_for_transcript_with_conn(
                c,
                student_id,
                session_id,
                content_sha256,
                ("pending", "running"),
            )
            updated = c.execute(
                """UPDATE raw_transcripts
                   SET analysis_status = 'failed', analysis_error = ?,
                       analysis_model = ?, analysis_prompt_hash = ?,
                       analysis_latency_ms = analysis_latency_ms + ?
                   WHERE id = ? AND student_id = ? AND session_id = ?
                     AND content_sha256 = ? AND analysis_status = 'running'
                     AND analysis_generation = ?""",
                (
                    str(error_message),
                    str(analysis_model).strip()[:200],
                    str(prompt_hash)[:128],
                    normalized_latency,
                    raw_id,
                    student_id,
                    session_id,
                    content_sha256,
                    generation,
                ),
            ).rowcount
            if updated != 1:
                return None
            self._insert_system_failure_occurrence_with_conn(
                c,
                kind="bulk_analysis",
                logical_key=raw_id,
                generation=generation,
                student_id=student_id,
                session_id=session_id,
                reason_code=SYSTEM_FAILURE_REASON_CODES["bulk_analysis"],
                created_at=now,
            )
            c.execute(
                """UPDATE upload_request_sessions
                   SET analysis_status = 'failed', analysis_error = ?, updated_at = ?
                   WHERE student_id = ? AND session_id = ? AND sha = ?
                     AND analysis_status IN ('pending', 'running')""",
                (str(error_message), now, student_id, session_id, content_sha256),
            )
            return {"request_ids": request_ids}

    def recover_interrupted_raw_transcript_analyses(
        self,
        error: str = "analysis interrupted; retry",
    ) -> int:
        """Move crash-left raw claims to retryable failure without changing trace."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            interrupted = c.execute(
                """SELECT id, student_id, session_id, analysis_generation
                   FROM raw_transcripts WHERE analysis_status = 'running'
                   ORDER BY id"""
            ).fetchall()
            cur = c.execute(
                """UPDATE raw_transcripts
                   SET analysis_status = 'failed', analysis_error = ?,
                       analysis_generation = CASE
                         WHEN analysis_generation < 1 THEN 1
                         ELSE analysis_generation END
                   WHERE analysis_status = 'running'""",
                (str(error),),
            )
            for row in interrupted:
                self._insert_system_failure_occurrence_with_conn(
                    c,
                    kind="bulk_analysis",
                    logical_key=row["id"],
                    generation=row["analysis_generation"],
                    student_id=row["student_id"],
                    session_id=row["session_id"],
                    reason_code=SYSTEM_FAILURE_REASON_CODES["bulk_analysis"],
                )
            return cur.rowcount

    def fail_stale_upload_request_sessions(
        self,
        *,
        student_id: str,
        session_id: str,
        content_sha256: str,
        error: str = "analysis stale transcript",
    ) -> list[str]:
        """Fail old-SHA children only when that SHA is no longer current."""
        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute(
                """SELECT content_sha256
                   FROM raw_transcripts
                   WHERE student_id = ? AND session_id = ?
                     AND content_sha256 IS NOT NULL AND content_sha256 != ''
                   ORDER BY created_at DESC, id DESC
                   LIMIT 1""",
                (student_id, session_id),
            ).fetchone()
            if current is not None and str(current["content_sha256"]) == content_sha256:
                return []
            request_ids = self._upload_request_ids_for_transcript_with_conn(
                c,
                student_id,
                session_id,
                content_sha256,
                ("pending", "running"),
            )
            c.execute(
                """UPDATE upload_request_sessions
                   SET analysis_status = 'failed', analysis_error = ?, updated_at = ?
                   WHERE student_id = ? AND session_id = ? AND sha = ?
                     AND analysis_status IN ('pending', 'running')""",
                (str(error), now, student_id, session_id, content_sha256),
            )
            return request_ids

    def get_known_session_shas(self, student_id: str) -> dict[str, dict[str, str]]:
        """Return latest sha and analysis status per session for one student."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT session_id, content_sha256, analysis_status
                   FROM raw_transcripts
                   WHERE student_id = ?
                     AND session_id IS NOT NULL
                     AND session_id != ''
                     AND content_sha256 IS NOT NULL
                     AND content_sha256 != ''
                   ORDER BY created_at DESC, id DESC""",
                (student_id,),
            ).fetchall()
        known: dict[str, dict[str, str]] = {}
        for row in rows:
            sid = str(row["session_id"])
            if sid not in known:
                known[sid] = {
                    "sha": str(row["content_sha256"]),
                    "analysis_status": str(row["analysis_status"] or ""),
                }
        return known

    def add_upload_request(
        self,
        mentor_id: str,
        student_id: str,
        session_id: str | None = None,
        status: str = "pending",
        request_id: str | None = None,
    ) -> str:
        """Persist a mentor-triggered upload request for audit/catch-up."""
        rid = request_id or uuid.uuid4().hex
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO upload_requests
                   (request_id, mentor_id, student_id, session_id, status,
                    transfer_status, analysis_status, error_message,
                    transfer_error, analysis_error, result_json, updated_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'not_requested', '', '', '', NULL, ?, ?)""",
                (
                    rid,
                    mentor_id,
                    student_id,
                    session_id,
                    status,
                    {"done": "stored"}.get(status, status),
                    now,
                    now,
                ),
            )
        return rid

    def list_upload_requests(
        self,
        student_id: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """List upload requests, optionally scoped by student and status."""
        clauses = []
        params: list[Any] = []
        if student_id:
            clauses.append("student_id = ?")
            params.append(student_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT * FROM upload_requests
                    {where}
                    ORDER BY created_at ASC""",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def list_pending_upload_requests(self, student_id: str | None = None) -> list[dict]:
        """List pending upload requests, optionally scoped to one student."""
        return self.list_upload_requests(student_id=student_id, status="pending")

    def get_upload_request(self, request_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM upload_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return dict(row) if row else None

    def upsert_upload_request_session(
        self,
        request_id: str,
        student_id: str,
        session_id: str,
        sha: str,
        *,
        analysis_status: str = "pending",
        analysis_error: str = "",
    ) -> dict:
        """Validate the parent and register a child in one write transaction."""
        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            parent = c.execute(
                """SELECT student_id, session_id, analysis_status
                   FROM upload_requests WHERE request_id = ?""",
                (request_id,),
            ).fetchone()
            if parent is None or str(parent["student_id"] or "") != student_id:
                raise UploadSessionRegistrationConflict(
                    f"upload request not found: {request_id}"
                )
            requested_session = str(parent["session_id"] or "")
            if requested_session and requested_session != session_id:
                raise UploadSessionRegistrationConflict(
                    f"upload request is scoped to session_id={requested_session}"
                )

            current_raw = c.execute(
                """SELECT content_sha256, analysis_status
                   FROM raw_transcripts
                   WHERE student_id = ? AND session_id = ?
                     AND content_sha256 IS NOT NULL AND content_sha256 != ''
                   ORDER BY created_at DESC, id DESC
                   LIMIT 1""",
                (student_id, session_id),
            ).fetchone()
            if analysis_status == "not_requested":
                projected_status = "not_requested"
            elif current_raw is not None and str(current_raw["content_sha256"]) == sha:
                raw_status = str(current_raw["analysis_status"] or "")
                projected_status = raw_status if raw_status in {"running", "done"} else "pending"
            else:
                projected_status = "pending"

            existing = c.execute(
                """SELECT * FROM upload_request_sessions
                   WHERE request_id = ? AND session_id = ?""",
                (request_id, session_id),
            ).fetchone()
            if str(parent["analysis_status"] or "") == "done":
                exact_done_replay = (
                    existing is not None
                    and str(existing["student_id"] or "") == student_id
                    and str(existing["sha"] or "") == sha
                    and str(existing["analysis_status"] or "") == "done"
                    and current_raw is not None
                    and str(current_raw["content_sha256"] or "") == sha
                    and str(current_raw["analysis_status"] or "") == "done"
                )
                if not exact_done_replay:
                    raise UploadSessionRegistrationConflict(
                        "upload request analysis is already done"
                    )
                return dict(existing)

            if existing is None:
                c.execute(
                    """INSERT INTO upload_request_sessions
                       (request_id, student_id, session_id, sha, analysis_status,
                        analysis_error, updated_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        request_id,
                        student_id,
                        session_id,
                        sha,
                        projected_status,
                        analysis_error if projected_status == "failed" else "",
                        now,
                        now,
                    ),
                )
            else:
                old_sha = str(existing["sha"] or "")
                old_status = str(existing["analysis_status"] or "pending")
                desired_status: str | None = None
                if old_sha != sha:
                    desired_status = projected_status
                elif old_status == "failed" and projected_status == "pending":
                    desired_status = "pending"
                elif old_status == "pending" and projected_status == "running":
                    desired_status = "running"
                elif old_status in {"pending", "running"} and projected_status == "done":
                    desired_status = "done"
                if desired_status is not None and (
                    old_sha != sha or desired_status != old_status
                ):
                    c.execute(
                        """UPDATE upload_request_sessions
                           SET student_id = ?, sha = ?, analysis_status = ?,
                               analysis_error = '', updated_at = ?
                           WHERE request_id = ? AND session_id = ?""",
                        (
                            student_id,
                            sha,
                            desired_status,
                            now,
                            request_id,
                            session_id,
                        ),
                    )
            row = c.execute(
                """SELECT * FROM upload_request_sessions
                   WHERE request_id = ? AND session_id = ?""",
                (request_id, session_id),
            ).fetchone()
            return dict(row)

    def list_upload_request_sessions(self, request_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM upload_request_sessions
                   WHERE request_id = ? ORDER BY created_at, session_id""",
                (request_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def compare_and_set_upload_request_session(
        self,
        request_id: str,
        student_id: str,
        session_id: str,
        *,
        expected: str,
        new_status: str,
        error: str = "",
        sha: str | None = None,
    ) -> int:
        sha_clause = " AND sha = ?" if sha is not None else ""
        params: list[Any] = [
            new_status, error, time.time(), request_id, student_id,
            session_id, expected,
        ]
        if sha is not None:
            params.append(sha)
        with self._conn() as c:
            cur = c.execute(
                f"""UPDATE upload_request_sessions
                   SET analysis_status = ?, analysis_error = ?, updated_at = ?
                   WHERE request_id = ? AND student_id = ? AND session_id = ?
                     AND analysis_status = ?{sha_clause}""",
                params,
            )
            return cur.rowcount

    def list_active_upload_request_sessions(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM upload_request_sessions
                   WHERE analysis_status IN ('pending', 'running')"""
            ).fetchall()
            return [dict(row) for row in rows]

    def list_active_upload_request_analyses(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM upload_requests
                   WHERE analysis_status IN ('pending', 'running')"""
            ).fetchall()
            return [dict(row) for row in rows]

    def claim_upload_analysis_retry(
        self,
        request_id: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Atomically validate and claim all retry targets for one request."""
        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            parent_row = c.execute(
                "SELECT * FROM upload_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if parent_row is None:
                raise UploadRetryClaimConflict("upload request not found")
            parent = dict(parent_row)
            if (
                parent.get("transfer_status") != "stored"
                or parent.get("analysis_status") != "failed"
            ):
                raise UploadRetryClaimConflict(
                    "analysis retry requires transfer=stored and analysis=failed"
                )
            student_id = str(parent.get("student_id") or "")
            requested_session = str(parent.get("session_id") or "").strip()
            children = [
                dict(row) for row in c.execute(
                    """SELECT * FROM upload_request_sessions
                       WHERE request_id = ? ORDER BY created_at, session_id""",
                    (request_id,),
                ).fetchall()
            ]
            targets = [
                child for child in children
                if child.get("analysis_status") == "failed"
                and (
                    not requested_session
                    or child.get("session_id") == requested_session
                )
            ]

            legacy_target: dict[str, Any] | None = None
            if requested_session and not targets:
                existing = next(
                    (
                        child for child in children
                        if child.get("session_id") == requested_session
                    ),
                    None,
                )
                if existing is not None:
                    raise UploadRetryClaimConflict(
                        "analysis retry requires a failed child"
                    )
                raw_row = c.execute(
                    """SELECT * FROM raw_transcripts
                       WHERE student_id = ? AND session_id = ?
                       ORDER BY created_at DESC, id DESC LIMIT 1""",
                    (student_id, requested_session),
                ).fetchone()
                if raw_row is None or not str(raw_row["content_sha256"] or ""):
                    raise UploadRetryClaimConflict(
                        f"analysis retry raw missing for session: {requested_session}"
                    )
                legacy_target = {
                    "request_id": request_id,
                    "student_id": student_id,
                    "session_id": requested_session,
                    "sha": str(raw_row["content_sha256"]),
                    "analysis_status": "failed",
                    "analysis_error": str(parent.get("analysis_error") or "analysis failed"),
                    "created_at": now,
                    "updated_at": now,
                }
                targets = [legacy_target]
            if not targets:
                raise UploadRetryClaimConflict(
                    "analysis retry requires failed children"
                )

            work_items: list[dict[str, Any]] = []
            for child in targets:
                child_student = str(child.get("student_id") or "")
                child_session = str(child.get("session_id") or "")
                child_sha = str(child.get("sha") or "")
                if child_student != student_id or not child_session or not child_sha:
                    raise UploadRetryClaimConflict(
                        f"analysis retry child ownership invalid: {child_session}"
                    )
                raw_row = c.execute(
                    """SELECT * FROM raw_transcripts
                       WHERE student_id = ? AND session_id = ? AND content_sha256 = ?
                       ORDER BY created_at DESC, id DESC LIMIT 1""",
                    (student_id, child_session, child_sha),
                ).fetchone()
                if raw_row is None:
                    raise UploadRetryClaimConflict(
                        f"analysis retry raw missing for session: {child_session}"
                    )
                raw_status = str(raw_row["analysis_status"] or "")
                if raw_status not in {"", "pending", "failed"}:
                    raise UploadRetryClaimConflict(
                        f"analysis retry raw is not retryable: {child_session}"
                    )
                work_items.append({
                    "session_id": child_session,
                    "sha": child_sha,
                    "raw": dict(raw_row),
                })

            if legacy_target is not None:
                c.execute(
                    """INSERT INTO upload_request_sessions
                       (request_id, student_id, session_id, sha, analysis_status,
                        analysis_error, updated_at, created_at)
                       VALUES (?, ?, ?, ?, 'failed', ?, ?, ?)""",
                    (
                        request_id,
                        student_id,
                        requested_session,
                        legacy_target["sha"],
                        legacy_target["analysis_error"],
                        now,
                        now,
                    ),
                )

            claimed = c.execute(
                """UPDATE upload_requests
                   SET analysis_status = 'pending', analysis_error = '', updated_at = ?
                   WHERE request_id = ? AND transfer_status = 'stored'
                     AND analysis_status = 'failed'""",
                (now, request_id),
            ).rowcount
            if claimed != 1:
                raise UploadRetryClaimConflict("analysis retry already claimed")

            for child in targets:
                updated = c.execute(
                    """UPDATE upload_request_sessions
                       SET analysis_status = 'pending', analysis_error = '', updated_at = ?
                       WHERE request_id = ? AND student_id = ? AND session_id = ?
                         AND sha = ? AND analysis_status = 'failed'""",
                    (
                        now,
                        request_id,
                        student_id,
                        str(child["session_id"]),
                        str(child["sha"]),
                    ),
                ).rowcount
                if updated != 1:
                    raise UploadRetryClaimConflict(
                        f"analysis retry child already claimed: {child['session_id']}"
                    )
            for item in work_items:
                raw_row = item["raw"]
                updated = c.execute(
                    """UPDATE raw_transcripts
                       SET analysis_status = 'pending', analysis_error = ''
                       WHERE id = ? AND student_id = ? AND session_id = ?
                         AND content_sha256 = ?
                         AND analysis_status IN ('', 'pending', 'failed')""",
                    (
                        raw_row["id"],
                        student_id,
                        str(item["session_id"]),
                        str(item["sha"]),
                    ),
                ).rowcount
                if updated != 1:
                    raise UploadRetryClaimConflict(
                        f"analysis retry raw already claimed: {item['session_id']}"
                    )
            claimed_parent = c.execute(
                "SELECT * FROM upload_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return dict(claimed_parent), work_items

    def update_upload_request_status(
        self,
        request_id: str,
        *,
        student_id: str,
        status: str,
        error_message: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> int:
        """Update one upload request only if it belongs to the reporting student."""
        result_json = json.dumps(result, ensure_ascii=False) if result is not None else None
        transfer_status = {"done": "stored"}.get(status, status)
        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute(
                """SELECT transfer_status, transfer_failure_generation,
                          session_id
                   FROM upload_requests
                   WHERE request_id = ? AND student_id = ?""",
                (request_id, student_id),
            ).fetchone()
            cur = c.execute(
                """UPDATE upload_requests
                   SET status = ?,
                       transfer_status = ?,
                       error_message = ?,
                       transfer_error = ?,
                       result_json = ?,
                       transfer_failure_generation = CASE
                         WHEN ? = 'failed' AND transfer_status != 'failed'
                         THEN transfer_failure_generation + 1
                         ELSE transfer_failure_generation END,
                       updated_at = ?
                   WHERE request_id = ? AND student_id = ?""",
                (
                    status,
                    transfer_status,
                    error_message or "",
                    error_message or "",
                    result_json,
                    transfer_status,
                    now,
                    request_id,
                    student_id,
                ),
            )
            if (
                cur.rowcount == 1
                and current is not None
                and transfer_status == "failed"
                and str(current["transfer_status"] or "") != "failed"
            ):
                generation = int(current["transfer_failure_generation"] or 0) + 1
                self._insert_system_failure_occurrence_with_conn(
                    c,
                    kind="upload_transfer",
                    logical_key=request_id,
                    generation=generation,
                    student_id=student_id,
                    session_id=current["session_id"],
                    reason_code=SYSTEM_FAILURE_REASON_CODES["upload_transfer"],
                    created_at=now,
                )
            return cur.rowcount

    def compare_and_set_upload_request_axis(
        self,
        request_id: str,
        *,
        student_id: str,
        axis: str,
        expected: str,
        new_status: str,
        error: str,
        result: dict[str, Any] | None = None,
    ) -> int:
        """Atomically update one allowlisted state axis from an expected value."""
        if axis not in {"transfer", "analysis"}:
            raise ValueError(f"unsupported upload request axis: {axis}")
        now = time.time()
        if axis == "transfer":
            legacy_status = {"stored": "done"}.get(new_status, new_status)
            generation_sql = (
                "transfer_failure_generation = transfer_failure_generation + 1,"
                if new_status == "failed" and expected != "failed"
                else ""
            )
            sql = f"""UPDATE upload_requests
                     SET transfer_status = ?, transfer_error = ?,
                         status = ?, error_message = ?, result_json = ?,
                         {generation_sql} updated_at = ?
                     WHERE request_id = ? AND student_id = ?
                       AND transfer_status = ?"""
            params: tuple[Any, ...] = (
                new_status,
                error,
                legacy_status,
                error,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                now,
                request_id,
                student_id,
                expected,
            )
        else:
            child_guard = ""
            if new_status == "done":
                child_guard = """
                       AND (
                         NOT EXISTS (
                           SELECT 1 FROM upload_request_sessions AS child
                           WHERE child.request_id = upload_requests.request_id
                         )
                         OR (
                           EXISTS (
                             SELECT 1 FROM upload_request_sessions AS child
                             WHERE child.request_id = upload_requests.request_id
                               AND child.analysis_status != 'not_requested'
                           )
                           AND NOT EXISTS (
                             SELECT 1 FROM upload_request_sessions AS child
                             WHERE child.request_id = upload_requests.request_id
                               AND child.analysis_status NOT IN ('done', 'not_requested')
                           )
                         )
                       )"""
            generation_sql = (
                "analysis_failure_generation = analysis_failure_generation + 1,"
                if new_status == "failed" and expected != "failed"
                else ""
            )
            sql = f"""UPDATE upload_requests
                      SET analysis_status = ?, analysis_error = ?,
                          {generation_sql} updated_at = ?
                      WHERE request_id = ? AND student_id = ?
                        AND analysis_status = ?{child_guard}"""
            params = (
                new_status,
                error,
                now,
                request_id,
                student_id,
                expected,
            )
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(sql, params)
            if cur.rowcount == 1 and new_status == "failed" and expected != "failed":
                row = c.execute(
                    """SELECT request_id, student_id, session_id,
                              transfer_failure_generation,
                              analysis_failure_generation
                       FROM upload_requests WHERE request_id = ?""",
                    (request_id,),
                ).fetchone()
                if row is not None:
                    if axis == "transfer":
                        kind = "upload_transfer"
                        generation = row["transfer_failure_generation"]
                    else:
                        kind = "upload_analysis"
                        generation = row["analysis_failure_generation"]
                    self._insert_system_failure_occurrence_with_conn(
                        c,
                        kind=kind,
                        logical_key=request_id,
                        generation=generation,
                        student_id=row["student_id"],
                        session_id=row["session_id"],
                        reason_code=SYSTEM_FAILURE_REASON_CODES[kind],
                        created_at=now,
                    )
            return cur.rowcount

    def get_raw_transcript(self, session_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                """SELECT * FROM raw_transcripts
                   WHERE session_id = ?
                   ORDER BY created_at DESC, id DESC
                   LIMIT 1""",
                (session_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_raw_transcript_analysis_source(
        self,
        raw_id: int,
        generation: int,
    ) -> dict[str, Any] | None:
        """Return only bounded durable axes for one bulk failure generation."""
        with self._conn() as c:
            row = c.execute(
                """SELECT id, student_id, session_id, analysis_status,
                          analysis_generation, created_at
                   FROM raw_transcripts
                   WHERE id = ? AND analysis_generation = ?""",
                (raw_id, generation),
            ).fetchone()
            return dict(row) if row else None

    def get_raw_transcript_for_student_session(self, student_id: str, session_id: str) -> dict | None:
        """Return the latest raw transcript scoped to one student and session."""
        with self._conn() as c:
            row = c.execute(
                """SELECT * FROM raw_transcripts
                   WHERE student_id = ? AND session_id = ?
                   ORDER BY created_at DESC, id DESC
                   LIMIT 1""",
                (student_id, session_id),
            ).fetchone()
            return dict(row) if row else None

    def get_raw_transcript_for_student_session_sha(
        self,
        student_id: str,
        session_id: str,
        content_sha256: str,
    ) -> dict | None:
        """Return the raw transcript matching one student's exact bulk SHA."""
        with self._conn() as c:
            row = c.execute(
                """SELECT * FROM raw_transcripts
                   WHERE student_id = ? AND session_id = ? AND content_sha256 = ?
                   ORDER BY created_at DESC, id DESC
                   LIMIT 1""",
                (student_id, session_id, content_sha256),
            ).fetchone()
            return dict(row) if row else None

    def get_raw_transcript_for_report(
        self,
        report_id: int,
        *,
        max_delay_seconds: float = LEGACY_RAW_MATCH_WINDOW_SECONDS,
    ) -> dict | None:
        """Return a provably unique immediate raw for one legacy report.

        Both directions must be unique inside the short timestamp window: the
        report has one candidate raw, and that raw has one candidate report.
        """
        if max_delay_seconds < 0:
            return None
        with self._conn() as c:
            report = c.execute(
                """SELECT * FROM reports
                   WHERE id = ?
                     AND event = 'Stop'
                     AND transcript_path = ?
                     AND analysis_input IS NULL""",
                (report_id, EXPLICIT_RAW_TRANSCRIPT_MARKER),
            ).fetchone()
            if not report:
                return None
            student_id = str(report["student_id"] or "")
            session_id = str(report["session_id"] or "")
            if not student_id or not session_id or report["created_at"] is None:
                return None
            report_created_at = float(report["created_at"])
            raws = c.execute(
                """SELECT * FROM raw_transcripts
                   WHERE student_id = ?
                     AND session_id = ?
                     AND content_sha256 IS NULL
                     AND created_at >= ?
                     AND created_at <= ?
                   ORDER BY created_at ASC, id ASC
                   LIMIT 2""",
                (
                    student_id,
                    session_id,
                    report_created_at,
                    report_created_at + max_delay_seconds,
                ),
            ).fetchall()
            if len(raws) != 1:
                return None
            raw = raws[0]
            raw_created_at = float(raw["created_at"])
            competing_reports = c.execute(
                """SELECT id FROM reports
                   WHERE student_id = ?
                     AND session_id = ?
                     AND event = 'Stop'
                     AND transcript_path = ?
                     AND created_at <= ?
                     AND created_at >= ?
                   ORDER BY id ASC
                   LIMIT 2""",
                (
                    student_id,
                    session_id,
                    EXPLICIT_RAW_TRANSCRIPT_MARKER,
                    raw_created_at,
                    raw_created_at - max_delay_seconds,
                ),
            ).fetchall()
            if len(competing_reports) != 1:
                return None
            if int(competing_reports[0]["id"]) != report_id:
                return None
            return dict(raw)

    def add_student_ask(
        self,
        student_id: str,
        session_id: str | None,
        question: str,
        answer: str,
        answer_status: str = "answered",
        error_code: str = "",
    ) -> int:
        """Persist a student-initiated Copilot question and answer."""
        if answer_status not in {"answered", "degraded", "failed"}:
            raise ValueError("invalid student ask answer status")
        self.upsert_student(student_id)
        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            if session_id:
                self._upsert_session_with_conn(
                    c,
                    session_id=session_id,
                    student_id=student_id,
                    work_dir="",
                    title="",
                    created_at=now,
                    last_activity_at=now,
                )
            cur = c.execute(
                """INSERT INTO student_asks
                   (student_id, session_id, question, answer,
                    answer_status, error_code, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    student_id,
                    session_id,
                    question,
                    answer,
                    answer_status,
                    error_code,
                    now,
                ),
            )
            return cur.lastrowid

    def reserve_student_ask(
        self,
        *,
        student_id: str,
        session_id: str | None,
        question: str,
        client_request_id: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically reserve one retry-safe student question.

        The first caller owns the model invocation. Replays receive the same
        pending or terminal row and must never invoke a second model call.
        Reusing a key with another session or question is a conflict.
        """
        request_id = str(client_request_id or "")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
        if (
            not request_id
            or len(request_id) > 128
            or any(char not in allowed for char in request_id)
        ):
            raise ValueError("invalid client_request_id")
        normalized_student_id = str(student_id or "").strip()
        normalized_session_id = str(session_id or "").strip() or None
        normalized_question = str(question or "").strip()
        if not normalized_student_id or not normalized_question:
            raise ValueError("student ask identity and question are required")

        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute(
                """SELECT * FROM student_asks
                   WHERE student_id = ? AND client_request_id = ?""",
                (normalized_student_id, request_id),
            ).fetchone()
            if existing is not None:
                row = dict(existing)
                identity = (
                    str(row.get("session_id") or ""),
                    str(row.get("question") or ""),
                )
                if identity != (normalized_session_id or "", normalized_question):
                    raise ValueError("client_request_id payload conflict")
                return row, False

            now = time.time()
            c.execute(
                """INSERT INTO students
                   (student_id, display_name, token_hash, created_at)
                   VALUES (?, '', NULL, ?)
                   ON CONFLICT(student_id) DO NOTHING""",
                (normalized_student_id, now),
            )
            if normalized_session_id:
                self._upsert_session_with_conn(
                    c,
                    session_id=normalized_session_id,
                    student_id=normalized_student_id,
                    work_dir="",
                    title="",
                    created_at=now,
                    last_activity_at=now,
                )
            cur = c.execute(
                """INSERT INTO student_asks
                   (student_id, session_id, question, answer,
                    client_request_id, answer_status, error_code, created_at)
                   VALUES (?, ?, ?, '', ?, 'pending', '', ?)""",
                (
                    normalized_student_id,
                    normalized_session_id,
                    normalized_question,
                    request_id,
                    now,
                ),
            )
            row = c.execute(
                "SELECT * FROM student_asks WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
            return dict(row), True

    def complete_student_ask(
        self,
        *,
        ask_id: int,
        student_id: str,
        answer: str,
        answer_status: str,
        error_code: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """Commit one reserved ask exactly once and return its durable row."""
        if answer_status not in {"answered", "degraded", "failed"}:
            raise ValueError("invalid student ask answer status")
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute(
                "SELECT * FROM student_asks WHERE id = ?",
                (ask_id,),
            ).fetchone()
            if existing is None:
                raise LookupError("student ask not found")
            row = dict(existing)
            if str(row.get("student_id") or "") != str(student_id or ""):
                raise PermissionError("student ask owner mismatch")
            if str(row.get("answer_status") or "") != "pending":
                terminal = (
                    str(row.get("answer") or ""),
                    str(row.get("answer_status") or ""),
                    str(row.get("error_code") or ""),
                )
                if terminal != (str(answer), answer_status, str(error_code or "")):
                    raise ValueError("student ask is already complete")
                return row, False

            c.execute(
                """UPDATE student_asks
                   SET answer = ?, answer_status = ?, error_code = ?
                   WHERE id = ? AND answer_status = 'pending'""",
                (str(answer), answer_status, str(error_code or ""), ask_id),
            )
            completed = c.execute(
                "SELECT * FROM student_asks WHERE id = ?",
                (ask_id,),
            ).fetchone()
            return dict(completed), True

    def get_student_ask_by_client_request(
        self,
        student_id: str,
        client_request_id: str,
    ) -> dict[str, Any] | None:
        """Return one ask scoped to the authenticated student identity."""
        with self._conn() as c:
            row = c.execute(
                """SELECT * FROM student_asks
                   WHERE student_id = ? AND client_request_id = ?""",
                (str(student_id or ""), str(client_request_id or "")),
            ).fetchone()
            return dict(row) if row else None

    def record_student_ask_feedback(
        self,
        ask_id: int,
        student_id: str,
        feedback: str,
        note: str = "",
    ) -> tuple[dict, bool]:
        """Record an ask's immutable first feedback write atomically."""
        normalized_feedback = str(feedback or "").strip()
        normalized_note = str(note or "").strip()
        if normalized_feedback not in {"helpful", "unresolved"}:
            raise ValueError("invalid student ask feedback")
        if len(normalized_note) > 500:
            raise ValueError("student ask feedback note is too long")

        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM student_asks WHERE id = ?",
                (ask_id,),
            ).fetchone()
            if row is None:
                raise LookupError("student ask not found")
            if row["student_id"] != student_id:
                raise PermissionError("student ask owner mismatch")

            existing_feedback = str(row["feedback"] or "")
            existing_note = str(row["feedback_note"] or "")
            if existing_feedback:
                if (
                    existing_feedback == normalized_feedback
                    and existing_note == normalized_note
                ):
                    return dict(row), False
                raise ValueError("student ask feedback is immutable")

            feedback_at = time.time()
            c.execute(
                """UPDATE student_asks
                   SET feedback = ?, feedback_note = ?, feedback_at = ?
                   WHERE id = ?""",
                (normalized_feedback, normalized_note, feedback_at, ask_id),
            )
            updated = c.execute(
                "SELECT * FROM student_asks WHERE id = ?",
                (ask_id,),
            ).fetchone()
            return dict(updated), True

    def set_prompt_config(
        self,
        key: str,
        prompt: str,
        updated_by: str | None = None,
    ) -> str:
        """Create or update one global prompt configuration."""
        now = time.time()
        normalized_key = str(key or "").strip()
        if not normalized_key:
            raise ValueError("prompt config key is required")
        with self._conn() as c:
            c.execute(
                """INSERT INTO prompt_configs
                   (key, prompt, updated_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     prompt = excluded.prompt,
                     updated_by = excluded.updated_by,
                     updated_at = excluded.updated_at""",
                (normalized_key, prompt, updated_by or "", now, now),
            )
        return normalized_key

    def get_prompt_config(self, key: str) -> dict | None:
        normalized_key = str(key or "").strip()
        if not normalized_key:
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM prompt_configs WHERE key = ?",
                (normalized_key,),
            ).fetchone()
            return dict(row) if row else None

    def list_student_asks(self, student_id: str, session_id: str | None = None) -> list[dict]:
        """List a student's Copilot questions, newest first."""
        with self._conn() as c:
            params: list[Any] = [student_id]
            where = "student_id = ?"
            if session_id is not None:
                where += " AND session_id = ?"
                params.append(session_id)
            rows = c.execute(
                f"""SELECT * FROM student_asks
                    WHERE {where}
                    ORDER BY created_at DESC, id DESC""",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def get_student_ask(self, ask_id: int) -> dict[str, Any] | None:
        """Return one durable ask for projection after its source commit."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM student_asks WHERE id = ?",
                (ask_id,),
            ).fetchone()
            return dict(row) if row else None

    def set_analysis_pending(self, report_id: int, pending: bool) -> int:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE reports SET analysis_pending = ? WHERE id = ?",
                (1 if pending else 0, report_id),
            )
            return cur.rowcount

    def mark_report_analysis_done(self, report_id: int) -> int:
        """Finish one durable delivery and erase its bounded analysis input."""
        with self._conn() as c:
            cur = c.execute(
                """UPDATE reports
                   SET analysis_pending = 0,
                       analysis_input = NULL,
                       analysis_status = 'done',
                       analysis_error = '',
                       analysis_next_retry_at = NULL
                   WHERE id = ?""",
                (report_id,),
            )
            return cur.rowcount

    def get_report(self, report_id: int) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM reports WHERE id = ?",
                (report_id,),
            ).fetchone()
            return dict(row) if row else None

    def set_report_analysis_input_if_missing(
        self,
        report_id: int,
        analysis_input: str,
    ) -> int:
        """Persist a bounded legacy recovery input before any claim."""
        with self._conn() as c:
            cur = c.execute(
                """UPDATE reports
                   SET analysis_input = ?
                   WHERE id = ?
                     AND event = 'Stop'
                     AND analysis_input IS NULL
                     AND analysis_status IN ('pending', 'failed')""",
                (analysis_input, report_id),
            )
            return cur.rowcount

    def claim_report_analysis(
        self,
        report_id: int,
        *,
        max_attempts: int,
    ) -> dict[str, Any] | None:
        """Atomically claim one pending/failed report analysis attempt."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM reports WHERE id = ?",
                (report_id,),
            ).fetchone()
            if not row:
                return None
            current = dict(row)
            current_status = str(current.get("analysis_status") or "")
            if (
                current.get("event") != "Stop"
                or current_status not in {"pending", "failed"}
                or (
                    current_status == "failed"
                    and current.get("analysis_next_retry_at") is None
                )
                or current.get("analysis_input") is None
                or int(current.get("analysis_attempts") or 0) >= max_attempts
            ):
                return None
            next_attempt = int(current.get("analysis_attempts") or 0) + 1
            updated = c.execute(
                """UPDATE reports
                   SET analysis_status = 'running',
                       analysis_attempts = ?,
                       analysis_error = '',
                       analysis_next_retry_at = NULL,
                       analysis_pending = 1
                   WHERE id = ?
                     AND analysis_status IN ('pending', 'failed')
                     AND (
                       analysis_status = 'pending'
                       OR analysis_next_retry_at IS NOT NULL
                     )
                     AND analysis_input IS NOT NULL
                     AND analysis_attempts < ?""",
                (next_attempt, report_id, max_attempts),
            ).rowcount
            if updated != 1:
                return None
            claimed = c.execute(
                "SELECT * FROM reports WHERE id = ?",
                (report_id,),
            ).fetchone()
            return dict(claimed)

    def mark_report_analysis_input_unavailable(
        self,
        report_id: int,
        *,
        max_attempts: int = 3,
    ) -> int:
        """Terminally fail a legacy report whose durable input is unknowable."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(
                """UPDATE reports
                   SET analysis_status = 'failed',
                       analysis_pending = 1,
                       analysis_attempts = ?,
                       analysis_error = 'analysis_input_unavailable',
                       analysis_next_retry_at = NULL
                   WHERE id = ?
                     AND event = 'Stop'
                     AND analysis_input IS NULL
                     AND analysis_status IN ('pending', 'failed')
                     AND analysis_attempts < ?""",
                (max_attempts, report_id, max_attempts),
            )
            if cur.rowcount == 1:
                row = c.execute(
                    """SELECT id, student_id, session_id, analysis_attempts
                       FROM reports WHERE id = ?""",
                    (report_id,),
                ).fetchone()
                if row is not None:
                    self._insert_system_failure_occurrence_with_conn(
                        c,
                        kind="stop",
                        logical_key=row["id"],
                        generation=row["analysis_attempts"],
                        student_id=row["student_id"],
                        session_id=row["session_id"],
                        reason_code=SYSTEM_FAILURE_REASON_CODES[
                            "stop_input_unavailable"
                        ],
                    )
            return cur.rowcount

    def mark_report_analysis_failed(
        self,
        report_id: int,
        *,
        attempt: int,
        error_code: str,
        next_retry_at: float | None,
        model: str = "",
        prompt_hash: str = "",
        latency_ms: int = 0,
    ) -> int:
        """Persist one failed attempt without erasing its durable input."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(
                """UPDATE reports
                   SET analysis_status = 'failed',
                       analysis_pending = 1,
                       analysis_error = ?,
                       analysis_next_retry_at = ?,
                       analysis_model = CASE
                           WHEN ? != '' THEN ? ELSE analysis_model END,
                       analysis_prompt_hash = CASE
                           WHEN ? != '' THEN ? ELSE analysis_prompt_hash END,
                       analysis_latency_ms = analysis_latency_ms + ?
                   WHERE id = ?
                     AND analysis_status = 'running'
                     AND analysis_attempts = ?""",
                (
                    error_code,
                    next_retry_at,
                    model,
                    model,
                    prompt_hash,
                    prompt_hash,
                    max(0, int(latency_ms)),
                    report_id,
                    attempt,
                ),
            )
            if cur.rowcount == 1 and next_retry_at is None:
                row = c.execute(
                    """SELECT id, student_id, session_id, analysis_attempts
                       FROM reports WHERE id = ?""",
                    (report_id,),
                ).fetchone()
                if row is not None:
                    self._insert_system_failure_occurrence_with_conn(
                        c,
                        kind="stop",
                        logical_key=row["id"],
                        generation=row["analysis_attempts"],
                        student_id=row["student_id"],
                        session_id=row["session_id"],
                        reason_code=SYSTEM_FAILURE_REASON_CODES[
                            "stop_retries_exhausted"
                        ],
                    )
            return cur.rowcount

    def recover_interrupted_report_analyses(self, *, max_attempts: int = 3) -> int:
        """Make process-crash ``running`` claims visible to startup recovery."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            terminal = c.execute(
                """SELECT id, student_id, session_id, analysis_attempts
                   FROM reports
                   WHERE event = 'Stop' AND analysis_status = 'running'
                     AND analysis_attempts >= ?""",
                (max_attempts,),
            ).fetchall()
            cur = c.execute(
                """UPDATE reports
                   SET analysis_status = 'failed',
                       analysis_pending = 1,
                       analysis_error = 'analysis_interrupted',
                       analysis_next_retry_at = CASE
                           WHEN analysis_attempts < ? THEN 0
                           ELSE NULL
                       END
                   WHERE event = 'Stop' AND analysis_status = 'running'""",
                (max_attempts,),
            )
            for row in terminal:
                self._insert_system_failure_occurrence_with_conn(
                    c,
                    kind="stop",
                    logical_key=row["id"],
                    generation=row["analysis_attempts"],
                    student_id=row["student_id"],
                    session_id=row["session_id"],
                    reason_code=SYSTEM_FAILURE_REASON_CODES[
                        "stop_retries_exhausted"
                    ],
                )
            return cur.rowcount

    def list_recoverable_reports(self, *, max_attempts: int = 3) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM reports
                   WHERE event = 'Stop'
                     AND (
                       analysis_status = 'pending'
                       OR (
                         analysis_status = 'failed'
                         AND analysis_next_retry_at IS NOT NULL
                       )
                     )
                     AND analysis_attempts < ?
                   ORDER BY id ASC""",
                (max_attempts,),
            ).fetchall()
            return [dict(row) for row in rows]

    def complete_report_analysis(
        self,
        *,
        report_id: int,
        prompt_id: int | None,
        session_id: str,
        student_id: str,
        result: dict[str, Any],
        session_title: str,
    ) -> tuple[int, bool]:
        """Commit summary, analysis, done state, and input erasure atomically."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute(
                "SELECT id FROM analyses WHERE report_id = ? ORDER BY id LIMIT 1",
                (report_id,),
            ).fetchone()
            if existing:
                c.execute(
                    """UPDATE reports
                       SET analysis_pending = 0,
                           analysis_input = NULL,
                           analysis_status = 'done',
                           analysis_error = '',
                           analysis_next_retry_at = NULL
                       WHERE id = ?""",
                    (report_id,),
                )
                return int(existing["id"]), False

            report_trace = c.execute(
                """SELECT analysis_attempts, analysis_latency_ms
                   FROM reports WHERE id = ?""",
                (report_id,),
            ).fetchone()
            if not report_trace:
                raise sqlite3.IntegrityError("report disappeared during analysis commit")
            attempt_count = max(1, int(report_trace["analysis_attempts"] or 0))
            current_latency_ms = max(0, int(result.get("latency_ms") or 0))
            cumulative_latency_ms = (
                max(0, int(report_trace["analysis_latency_ms"] or 0))
                + current_latency_ms
            )

            summary = str(result.get("ai_reply_summary") or "")
            if summary:
                summary_row = (
                    c.execute(
                        """SELECT id FROM ai_summaries
                           WHERE prompt_id = ? ORDER BY id LIMIT 1""",
                        (prompt_id,),
                    ).fetchone()
                    if prompt_id is not None
                    else None
                )
                if summary_row:
                    c.execute(
                        """UPDATE ai_summaries
                           SET session_id = ?, student_id = ?, content = ?, created_at = ?
                           WHERE id = ?""",
                        (session_id, student_id, summary, time.time(), summary_row["id"]),
                    )
                    c.execute(
                        "DELETE FROM ai_summaries WHERE prompt_id = ? AND id != ?",
                        (prompt_id, summary_row["id"]),
                    )
                else:
                    c.execute(
                        """INSERT INTO ai_summaries
                           (prompt_id, session_id, student_id, content, created_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (prompt_id, session_id, student_id, summary, time.time()),
                    )

            analysis_id = self._add_analysis_with_conn(
                c,
                report_id=report_id,
                student_id=student_id,
                result=result,
                session_id=session_id,
                session_title=session_title,
                attempt_count=attempt_count,
            )
            updated = c.execute(
                """UPDATE reports
                   SET analysis_pending = 0,
                       analysis_input = NULL,
                       analysis_status = 'done',
                       analysis_error = '',
                       analysis_next_retry_at = NULL,
                       analysis_model = ?,
                       analysis_prompt_hash = ?,
                       analysis_latency_ms = ?
                   WHERE id = ?""",
                (
                    str(result.get("model") or "")[:200],
                    str(result.get("prompt_hash") or "")[:128],
                    cumulative_latency_ms,
                    report_id,
                ),
            ).rowcount
            if updated != 1:
                raise sqlite3.IntegrityError("report disappeared during analysis commit")
            return analysis_id, True

    def list_pending_reports(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM reports
                   WHERE analysis_pending = 1
                   ORDER BY id ASC"""
            ).fetchall()
            return [dict(r) for r in rows]

    def analysis_exists_for_report(self, report_id: int) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM analyses WHERE report_id = ? LIMIT 1",
                (report_id,),
            ).fetchone()
            return row is not None

    def add_analysis(
        self,
        report_id: int,
        student_id: str,
        result: dict[str, Any],
        session_id: str | None = None,
        session_title: str | None = None,
    ) -> int:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            return self._add_analysis_with_conn(
                c,
                report_id=report_id,
                student_id=student_id,
                result=result,
                session_id=session_id,
                session_title=session_title,
            )

    def _add_analysis_with_conn(
        self,
        c: sqlite3.Connection,
        *,
        report_id: int,
        student_id: str,
        result: dict[str, Any],
        session_id: str | None,
        session_title: str | None,
        attempt_count: int = 0,
    ) -> int:
        """Insert analysis details using an existing transaction."""
        report = c.execute(
            "SELECT student_id FROM reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if report is None:
            raise sqlite3.IntegrityError("analysis report does not exist")
        report_student_id = str(report["student_id"] or "")
        if report_student_id != student_id:
            raise ValueError("report owner mismatch")

        cur = c.execute(
            """INSERT INTO analyses
               (report_id, student_id, session_id, session_title,
                topic, understanding, off_topic, stuck_at,
                is_technical, severity, diagnosis, suggestion,
                progress, guidance, alert, confidence, evidence_json,
                model, prompt_hash, latency_ms, attempt_count, raw, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                report_id,
                report_student_id,
                session_id,
                session_title,
                result.get("topic", ""),
                result.get("understanding", "unknown"),
                1 if result.get("off_topic") else 0,
                result.get("stuck_at", ""),
                1 if result.get("is_technical") else 0,
                result.get("severity", "info"),
                result.get("diagnosis", ""),
                result.get("suggestion", ""),
                result.get("progress", ""),
                result.get("guidance", ""),
                result.get("alert", ""),
                result.get("confidence", 0.5),
                json.dumps(result.get("evidence", []), ensure_ascii=False),
                str(result.get("model") or "")[:200],
                str(result.get("prompt_hash") or "")[:128],
                max(0, int(result.get("latency_ms") or 0)),
                max(0, int(attempt_count)),
                json.dumps(result, ensure_ascii=False),
                time.time(),
            ),
        )
        return int(cur.lastrowid)

    def commit_bulk_analysis_if_current(
        self,
        *,
        student_id: str,
        session_id: str,
        content_sha256: str,
        raw_id: int,
        generation: int,
        result: dict[str, Any],
        session_title: str,
        msg_count: int,
    ) -> dict[str, Any] | None:
        """Commit only the worker that still owns the current raw generation."""
        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            raw_row = c.execute(
                """SELECT id, content_sha256, analysis_status,
                          analysis_latency_ms, analysis_attempts, analysis_generation
                   FROM raw_transcripts
                   WHERE student_id = ? AND session_id = ?
                     AND content_sha256 IS NOT NULL AND content_sha256 != ''
                   ORDER BY created_at DESC, id DESC
                   LIMIT 1""",
                (student_id, session_id),
            ).fetchone()
            if (
                raw_row is None
                or int(raw_row["id"]) != int(raw_id)
                or str(raw_row["content_sha256"]) != content_sha256
                or str(raw_row["analysis_status"] or "") != "running"
                or int(raw_row["analysis_generation"] or 0) != int(generation)
            ):
                return None

            attempt_latency_ms = max(0, int(result.get("latency_ms") or 0))
            attempt_count = max(1, int(raw_row["analysis_attempts"] or 0))
            cumulative_latency_ms = (
                max(0, int(raw_row["analysis_latency_ms"] or 0))
                + attempt_latency_ms
            )
            request_ids = self._upload_request_ids_for_transcript_with_conn(
                c,
                student_id,
                session_id,
                content_sha256,
                ("pending", "running", "failed"),
            )

            report_id = self._add_report_with_conn(
                c,
                student_id=student_id,
                session_id=session_id,
                event="BulkUpload",
                transcript_path="",
                msg_count=msg_count,
                tool_calls=0,
            )
            analysis_id = self._add_analysis_with_conn(
                c,
                report_id=report_id,
                student_id=student_id,
                result=result,
                session_id=session_id,
                session_title=session_title,
                attempt_count=attempt_count,
            )
            c.execute(
                """UPDATE reports
                   SET analysis_status = 'done',
                       analysis_attempts = ?,
                       analysis_model = ?,
                       analysis_prompt_hash = ?,
                       analysis_latency_ms = ?
                   WHERE id = ?""",
                (
                    attempt_count,
                    str(result.get("model") or "").strip()[:200],
                    str(result.get("prompt_hash") or "")[:128],
                    cumulative_latency_ms,
                    report_id,
                ),
            )
            updated = c.execute(
                """UPDATE raw_transcripts
                   SET analysis_status = 'done',
                       analysis_error = '',
                       analysis_model = ?,
                       analysis_prompt_hash = ?,
                       analysis_latency_ms = ?,
                       analysis_attempts = ?
                   WHERE id = ? AND student_id = ? AND session_id = ?
                     AND content_sha256 = ? AND analysis_status = 'running'
                     AND analysis_generation = ?""",
                (
                    str(result.get("model") or "").strip()[:200],
                    str(result.get("prompt_hash") or "")[:128],
                    cumulative_latency_ms,
                    attempt_count,
                    raw_id,
                    student_id,
                    session_id,
                    content_sha256,
                    generation,
                ),
            ).rowcount
            if updated != 1:
                raise sqlite3.IntegrityError("bulk transcript changed during analysis commit")
            c.execute(
                """UPDATE upload_request_sessions
                   SET analysis_status = 'done', analysis_error = '', updated_at = ?
                   WHERE student_id = ? AND session_id = ? AND sha = ?
                     AND analysis_status IN ('pending', 'running', 'failed')""",
                (now, student_id, session_id, content_sha256),
            )
            return {
                "report_id": report_id,
                "analysis_id": analysis_id,
                "request_ids": request_ids,
            }

    def recent_analyses(
        self,
        student_id: str | None,
        limit: int = 20,
        session_id: str | None = None,
    ) -> list[dict]:
        with self._conn() as c:
            clauses = []
            params: list = []
            if student_id:
                clauses.append("a.student_id = ? AND r.student_id = ?")
                params.extend((student_id, student_id))
            if session_id:
                clauses.append("a.session_id = ?")
                params.append(session_id)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(limit)
            rows = c.execute(
                f"""SELECT a.*, r.event, r.prompt, r.created_at AS report_at
                    FROM analyses a JOIN reports r
                      ON a.report_id = r.id AND a.student_id = r.student_id
                    {where}
                    ORDER BY a.created_at DESC LIMIT ?""",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def analysis_envelopes_after(
        self,
        student_id: str,
        *,
        after_report_id: int = 0,
        limit: int = 64,
    ) -> list[dict[str, Any]]:
        """Return one durable analysis per report in ascending cursor order."""
        resolved_student_id = str(student_id or "").strip()
        if not resolved_student_id:
            return []
        bounded_limit = max(1, min(int(limit), 101))
        with self._conn() as c:
            rows = c.execute(
                """SELECT a.*, r.event
                   FROM reports r
                   JOIN analyses a ON a.id = (
                       SELECT a2.id
                       FROM analyses a2
                       WHERE a2.report_id = r.id
                         AND a2.student_id = r.student_id
                       ORDER BY a2.created_at DESC, a2.id DESC
                       LIMIT 1
                   )
                   WHERE r.student_id = ? AND r.id > ?
                   ORDER BY r.id ASC
                   LIMIT ?""",
                (resolved_student_id, max(0, int(after_report_id)), bounded_limit),
            ).fetchall()
        envelopes: list[dict[str, Any]] = []
        for row in rows:
            values = dict(row)
            try:
                result = json.loads(str(values.get("raw") or "{}"))
            except (TypeError, json.JSONDecodeError):
                result = {}
            if not isinstance(result, dict):
                result = {}
            envelopes.append(AnalysisEnvelope(
                analysis_id=int(values.get("id") or 0),
                student_id=resolved_student_id,
                session_id=str(values.get("session_id") or ""),
                report_id=int(values["report_id"]),
                event=str(values.get("event") or ""),
                result=result,
                timestamp=float(values.get("created_at") or 0.0),
            ).to_dict())
        return envelopes

    def analysis_envelopes_after_commit(
        self,
        student_id: str,
        *,
        after_analysis_id: int = 0,
        limit: int = 64,
    ) -> list[dict[str, Any]]:
        """Return analyses in durable commit order, independent of report order."""
        resolved_student_id = str(student_id or "").strip()
        if not resolved_student_id:
            return []
        bounded_limit = max(1, min(int(limit), 101))
        with self._conn() as connection:
            rows = connection.execute(
                """SELECT a.*, r.event
                   FROM analyses a
                   JOIN reports r
                     ON r.id = a.report_id AND r.student_id = a.student_id
                   WHERE a.student_id = ? AND a.id > ?
                   ORDER BY a.id ASC
                   LIMIT ?""",
                (
                    resolved_student_id,
                    max(0, int(after_analysis_id)),
                    bounded_limit,
                ),
            ).fetchall()
        envelopes: list[dict[str, Any]] = []
        for row in rows:
            values = dict(row)
            try:
                result = json.loads(str(values.get("raw") or "{}"))
            except (TypeError, json.JSONDecodeError):
                result = {}
            if not isinstance(result, dict):
                result = {}
            envelopes.append(AnalysisEnvelope(
                analysis_id=int(values.get("id") or 0),
                student_id=resolved_student_id,
                session_id=str(values.get("session_id") or ""),
                report_id=int(values["report_id"]),
                event=str(values.get("event") or ""),
                result=result,
                timestamp=float(values.get("created_at") or 0.0),
            ).to_dict())
        return envelopes

    def get_analysis(self, analysis_id: int) -> dict[str, Any] | None:
        """Return one durable analysis for projection after its source commit."""
        with self._conn() as c:
            row = c.execute(
                """SELECT a.*, r.event, r.prompt, r.created_at AS report_at
                   FROM analyses a JOIN reports r ON a.report_id = r.id
                   WHERE a.id = ?""",
                (analysis_id,),
            ).fetchone()
            return dict(row) if row else None

    def recent_analyses_as_of(
        self,
        *,
        student_id: str,
        created_at: float,
        analysis_id: int,
        session_id: str | None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Return a historical policy window bounded by ``(created_at, id)``."""
        if limit <= 0:
            return []
        clauses = [
            "a.student_id = ?",
            "(a.created_at < ? OR (a.created_at = ? AND a.id <= ?))",
        ]
        params: list[Any] = [student_id, created_at, created_at, analysis_id]
        if session_id:
            clauses.append("a.session_id = ?")
            params.append(session_id)
        params.append(min(int(limit), 100))
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT a.*, r.event, r.prompt, r.created_at AS report_at
                    FROM analyses a JOIN reports r ON a.report_id = r.id
                    WHERE {' AND '.join(clauses)}
                    ORDER BY a.created_at DESC, a.id DESC
                    LIMIT ?""",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def get_attention_backfill_cursor_state(
        self,
        source_kind: str,
    ) -> tuple[int, int]:
        if source_kind not in ATTENTION_BACKFILL_SOURCE_KINDS:
            raise ValueError("invalid attention backfill source kind")
        with self._conn() as c:
            row = c.execute(
                """SELECT last_id, version FROM attention_backfill_cursors
                   WHERE source_kind = ?""",
                (source_kind,),
            ).fetchone()
            if row is None:
                return 0, 0
            return (
                max(0, int(row["last_id"] or 0)),
                max(0, int(row["version"] or 0)),
            )

    def compare_and_set_attention_backfill_cursor(
        self,
        source_kind: str,
        *,
        expected_version: int,
        last_id: int,
    ) -> int | None:
        """Advance or wrap a cursor only for the caller's observed version."""
        if source_kind not in ATTENTION_BACKFILL_SOURCE_KINDS:
            raise ValueError("invalid attention backfill source kind")
        normalized_id = max(0, int(last_id))
        normalized_version = max(0, int(expected_version))
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                """SELECT version FROM attention_backfill_cursors
                   WHERE source_kind = ?""",
                (source_kind,),
            ).fetchone()
            current_version = max(0, int(row["version"] or 0)) if row else 0
            if current_version != normalized_version:
                return None
            next_version = current_version + 1
            if row is None:
                c.execute(
                    """INSERT INTO attention_backfill_cursors
                       (source_kind, last_id, version, updated_at)
                       VALUES (?, ?, ?, ?)""",
                    (source_kind, normalized_id, next_version, time.time()),
                )
            else:
                changed = c.execute(
                    """UPDATE attention_backfill_cursors
                       SET last_id = ?, version = ?, updated_at = ?
                       WHERE source_kind = ? AND version = ?""",
                    (
                        normalized_id,
                        next_version,
                        time.time(),
                        source_kind,
                        current_version,
                    ),
                ).rowcount
                if changed != 1:
                    return None
            return next_version

    def list_attention_analysis_sources(
        self,
        *,
        after_id: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT id FROM analyses
                   WHERE id > ? ORDER BY id ASC LIMIT ?""",
                (max(0, int(after_id)), limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_attention_ask_sources(
        self,
        *,
        after_id: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT id FROM student_asks
                   WHERE id > ? ORDER BY id ASC LIMIT ?""",
                (max(0, int(after_id)), limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_attention_stop_sources(
        self,
        *,
        after: tuple[float, int] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        cursor_clause = ""
        params: list[Any] = []
        if after is not None:
            cursor_clause = "AND (created_at > ? OR (created_at = ? AND id > ?))"
            params.extend((after[0], after[0], after[1]))
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT id, created_at FROM reports
                    WHERE event = 'Stop'
                      AND (
                        analysis_error = 'analysis_input_unavailable'
                        OR (analysis_status = 'failed' AND analysis_attempts >= 3)
                      )
                      {cursor_clause}
                    ORDER BY created_at ASC, id ASC LIMIT ?""",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def list_attention_upload_sources(
        self,
        *,
        after: tuple[float, str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        cursor_clause = ""
        params: list[Any] = []
        if after is not None:
            cursor_clause = (
                "AND (created_at > ? OR (created_at = ? AND request_id > ?))"
            )
            params.extend((after[0], after[0], after[1]))
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT request_id, created_at FROM upload_requests
                    WHERE transfer_status = 'failed'
                      {cursor_clause}
                    ORDER BY created_at ASC, request_id ASC LIMIT ?""",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def list_attention_raw_sources(
        self,
        *,
        after: tuple[int, int] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        cursor_clause = ""
        params: list[Any] = []
        if after is not None:
            cursor_clause = (
                "AND (id > ? OR (id = ? AND analysis_generation > ?))"
            )
            params.extend((after[0], after[0], after[1]))
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT id, analysis_generation FROM raw_transcripts
                    WHERE analysis_status = 'failed'
                      {cursor_clause}
                    ORDER BY id ASC, analysis_generation ASC LIMIT ?""",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def get_prompt_for_report(self, report_id: int) -> dict | None:
        """Return the durable Stop prompt associated with one report."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM prompts WHERE report_id = ? LIMIT 1",
                (report_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_or_create_prompt_for_report(
        self,
        *,
        report_id: int,
        session_id: str,
        student_id: str,
        content: str,
    ) -> tuple[dict, bool]:
        """Persist one prompt per Stop report and allocate its sequence atomically."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute(
                "SELECT * FROM prompts WHERE report_id = ? LIMIT 1",
                (report_id,),
            ).fetchone()
            if existing:
                return dict(existing), False

            seq_row = c.execute(
                """SELECT COALESCE(MAX(seq_in_session), -1) + 1 AS next_seq
                   FROM prompts WHERE session_id = ?""",
                (session_id,),
            ).fetchone()
            seq = int(seq_row["next_seq"])
            cur = c.execute(
                """INSERT INTO prompts
                   (report_id, session_id, seq_in_session, student_id, content, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (report_id, session_id, seq, student_id, content, time.time()),
            )
            row = c.execute(
                "SELECT * FROM prompts WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
            return dict(row), True

    def add_prompt(
        self,
        session_id: str,
        seq_in_session: int,
        student_id: str,
        content: str,
    ) -> int:
        """存入学员提示词全文（不截断），返回 prompt_id。"""
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO prompts
                   (session_id, seq_in_session, student_id, content, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, seq_in_session, student_id, content, time.time()),
            )
            return cur.lastrowid

    def add_ai_summary(
        self,
        prompt_id: int | None,
        session_id: str,
        student_id: str,
        content: str,
    ) -> int:
        """Upsert a prompt-scoped AI summary, preserving the legacy method name."""
        return self.upsert_ai_summary(prompt_id, session_id, student_id, content)

    def upsert_ai_summary(
        self,
        prompt_id: int | None,
        session_id: str,
        student_id: str,
        content: str,
    ) -> int:
        """Store one AI reply summary per prompt.

        Old rows may already contain duplicate prompt_id values, so this uses a
        small transaction instead of adding a unique index that could break
        migration on existing databases.
        """
        now = time.time()
        with self._conn() as c:
            if prompt_id is not None:
                existing = c.execute(
                    """SELECT id FROM ai_summaries
                       WHERE prompt_id = ?
                       ORDER BY created_at ASC, id ASC
                       LIMIT 1""",
                    (prompt_id,),
                ).fetchone()
                if existing:
                    summary_id = int(existing["id"])
                    c.execute(
                        """UPDATE ai_summaries
                           SET session_id = ?, student_id = ?, content = ?, created_at = ?
                           WHERE id = ?""",
                        (session_id, student_id, content, now, summary_id),
                    )
                    c.execute(
                        "DELETE FROM ai_summaries WHERE prompt_id = ? AND id != ?",
                        (prompt_id, summary_id),
                    )
                    return summary_id
            cur = c.execute(
                """INSERT INTO ai_summaries
                   (prompt_id, session_id, student_id, content, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (prompt_id, session_id, student_id, content, now),
            )
            return cur.lastrowid

    def get_prompt(self, prompt_id: int) -> dict | None:
        """Return one prompt by primary key."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM prompts WHERE id = ?",
                (prompt_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_prompts_by_session(self, session_id: str) -> list[dict]:
        """按 session 取提示词，按 seq_in_session 升序。"""
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM prompts
                   WHERE session_id = ?
                   ORDER BY seq_in_session ASC, created_at ASC""",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_recent_sessions(self, limit: int = 2) -> list[dict]:
        """Return recently active sessions for maintenance jobs."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT *
                   FROM sessions
                   ORDER BY COALESCE(last_activity_at, created_at, 0) DESC,
                            created_at DESC,
                            session_id ASC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def _get_prompt_reply_with_conn(
        self,
        c: sqlite3.Connection,
        session_id: str,
        prompt_seq: int,
    ) -> str:
        user_rows = c.execute(
            """SELECT id, seq, created_at
               FROM messages
               WHERE session_id = ? AND role = 'user'
               ORDER BY seq ASC, created_at ASC, id ASC""",
            (session_id,),
        ).fetchall()
        if not user_rows:
            return ""

        anchor_index = next(
            (idx for idx, row in enumerate(user_rows) if int(row["seq"]) == int(prompt_seq)),
            None,
        )
        if anchor_index is None:
            if prompt_seq < 0 or prompt_seq >= len(user_rows):
                return ""
            anchor_index = prompt_seq

        anchor = user_rows[anchor_index]
        next_user = user_rows[anchor_index + 1] if anchor_index + 1 < len(user_rows) else None

        where = [
            "session_id = ?",
            "role = 'assistant'",
            "(seq > ? OR (seq = ? AND created_at >= ?))",
        ]
        params: list[Any] = [
            session_id,
            int(anchor["seq"]),
            int(anchor["seq"]),
            float(anchor["created_at"] or 0),
        ]
        if next_user is not None:
            where.append("(seq < ? OR (seq = ? AND created_at < ?))")
            params.extend([
                int(next_user["seq"]),
                int(next_user["seq"]),
                float(next_user["created_at"] or 0),
            ])

        rows = c.execute(
            f"""SELECT text
                FROM messages
                WHERE {' AND '.join(where)}
                ORDER BY seq ASC, created_at ASC, id ASC""",
            params,
        ).fetchall()
        return "\n\n".join(str(row["text"]).strip() for row in rows if str(row["text"]).strip())

    def get_prompt_reply(self, session_id: str, prompt_seq: int) -> str:
        """Return all assistant text after one prompt and before the next user prompt."""
        with self._conn() as c:
            return self._get_prompt_reply_with_conn(c, session_id, int(prompt_seq))

    def get_prompt_reply_by_id(self, prompt_id: int) -> str:
        """Return concatenated assistant reply text for a prompt id."""
        prompt = self.get_prompt(prompt_id)
        if not prompt:
            return ""
        return self.get_prompt_reply(
            str(prompt.get("session_id") or ""),
            int(prompt.get("seq_in_session") or 0),
        )

    def _message_reply_rows_with_conn(
        self,
        c: sqlite3.Connection,
        message_id: int,
    ) -> list[sqlite3.Row]:
        anchor = c.execute(
            """SELECT id, session_id, seq, created_at
               FROM messages
               WHERE id = ? AND role = 'user'""",
            (int(message_id),),
        ).fetchone()
        if not anchor:
            return []

        next_user = c.execute(
            """SELECT id, seq, created_at
               FROM messages
               WHERE session_id = ?
                 AND role = 'user'
                 AND (seq > ? OR (seq = ? AND created_at > ?) OR (seq = ? AND created_at = ? AND id > ?))
               ORDER BY seq ASC, created_at ASC, id ASC
               LIMIT 1""",
            (
                anchor["session_id"],
                int(anchor["seq"]),
                int(anchor["seq"]),
                float(anchor["created_at"] or 0),
                int(anchor["seq"]),
                float(anchor["created_at"] or 0),
                int(anchor["id"]),
            ),
        ).fetchone()

        where = [
            "session_id = ?",
            "role = 'assistant'",
            "(seq > ? OR (seq = ? AND created_at >= ?))",
        ]
        params: list[Any] = [
            anchor["session_id"],
            int(anchor["seq"]),
            int(anchor["seq"]),
            float(anchor["created_at"] or 0),
        ]
        if next_user is not None:
            where.append("(seq < ? OR (seq = ? AND created_at < ?))")
            params.extend([
                int(next_user["seq"]),
                int(next_user["seq"]),
                float(next_user["created_at"] or 0),
            ])

        return c.execute(
            f"""SELECT id, text, created_at
                FROM messages
                WHERE {' AND '.join(where)}
                ORDER BY seq ASC, created_at ASC, id ASC""",
            params,
        ).fetchall()

    def get_message_reply_by_id(self, message_id: int) -> str:
        """Return concatenated assistant reply text for a user message id."""
        with self._conn() as c:
            rows = self._message_reply_rows_with_conn(c, int(message_id))
            return "\n\n".join(str(row["text"]).strip() for row in rows if str(row["text"]).strip())

    def get_message_rounds_by_session(self, session_id: str) -> list[dict]:
        """Return user message rounds and their assistant reply text for one session."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT id, session_id, student_id, seq, text, summary,
                          content_sha256, created_at
                   FROM messages
                   WHERE session_id = ? AND role = 'user'
                   ORDER BY seq ASC, created_at ASC, id ASC""",
                (session_id,),
            ).fetchall()
            rounds: list[dict[str, Any]] = []
            for row in rows:
                reply_rows = self._message_reply_rows_with_conn(c, int(row["id"]))
                rounds.append({
                    "id": int(row["id"]),
                    "session_id": row["session_id"],
                    "student_id": row["student_id"],
                    "seq": int(row["seq"]),
                    "content": row["text"],
                    "summary": row["summary"] or "",
                    "content_sha256": row["content_sha256"],
                    "created_at": row["created_at"],
                    "reply": "\n\n".join(
                        str(reply_row["text"]).strip()
                        for reply_row in reply_rows
                        if str(reply_row["text"]).strip()
                    ),
                    "reply_created_at": (
                        float(reply_rows[0]["created_at"] or 0)
                        if reply_rows else None
                    ),
                })
            return rounds

    def set_message_summary(self, message_id: int, summary: str) -> int:
        """Store a generated summary on the user message row."""
        with self._conn() as c:
            cur = c.execute(
                """UPDATE messages
                   SET summary = ?
                   WHERE id = ? AND role = 'user'""",
                (summary, int(message_id)),
            )
            return cur.rowcount

    def get_ai_summaries_by_session(self, session_id: str) -> list[dict]:
        """按 session 取 AI 摘要，按 created_at 升序。"""
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM ai_summaries
                   WHERE session_id = ?
                   ORDER BY created_at ASC""",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def _analysis_timeline_rows(self, c: sqlite3.Connection, session_id: str) -> list[dict]:
        rows = c.execute(
            """SELECT a.id, a.session_id, a.student_id,
                      COALESCE(a.diagnosis, '') AS content, a.created_at,
                      'analysis' AS type,
                      NULL AS seq_in_session,
                      NULL AS prompt_id,
                      NULL AS reply_ref,
                      a.report_id,
                      a.severity,
                      a.understanding,
                      a.suggestion,
                      a.is_technical,
                      a.topic,
                      NULL AS has_summary,
                      NULL AS has_full_reply
               FROM analyses a
               WHERE a.session_id = ?""",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _prompt_timeline_rows(self, c: sqlite3.Connection, session_id: str) -> list[dict]:
        prompts = c.execute(
            """SELECT *
               FROM prompts
               WHERE session_id = ?
               ORDER BY seq_in_session ASC, created_at ASC, id ASC""",
            (session_id,),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for prompt in prompts:
            prompt_id = int(prompt["id"])
            prompt_seq = int(prompt["seq_in_session"] or 0)
            has_full_reply = 1 if self._get_prompt_reply_with_conn(c, session_id, prompt_seq) else 0
            events.append({
                "id": prompt_id,
                "session_id": prompt["session_id"],
                "student_id": prompt["student_id"],
                "content": prompt["content"],
                "created_at": prompt["created_at"],
                "type": "prompt",
                "seq_in_session": prompt_seq,
                "prompt_id": prompt_id,
                "reply_ref": None,
                "report_id": None,
                "severity": None,
                "understanding": None,
                "suggestion": None,
                "is_technical": None,
                "topic": None,
                "has_summary": None,
                "has_full_reply": has_full_reply,
            })
            summary = c.execute(
                """SELECT *
                   FROM ai_summaries
                   WHERE prompt_id = ?
                     AND COALESCE(content, '') != ''
                   ORDER BY created_at DESC, id DESC
                   LIMIT 1""",
                (prompt_id,),
            ).fetchone()
            if summary:
                events.append({
                    "id": summary["id"],
                    "session_id": summary["session_id"],
                    "student_id": summary["student_id"],
                    "content": summary["content"],
                    "created_at": summary["created_at"],
                    "type": "ai_summary",
                    "seq_in_session": prompt_seq,
                    "prompt_id": prompt_id,
                    "reply_ref": f"prompt:{prompt_id}",
                    "report_id": None,
                    "severity": None,
                    "understanding": None,
                    "suggestion": None,
                    "is_technical": None,
                    "topic": None,
                    "has_summary": 1,
                    "has_full_reply": has_full_reply,
                })
        return events

    def _bulk_message_timeline_rows(self, c: sqlite3.Connection, session_id: str) -> list[dict]:
        users = c.execute(
            """SELECT id, session_id, student_id, seq, text, summary, created_at
               FROM messages
               WHERE session_id = ? AND source = 'bulk' AND role = 'user'
               ORDER BY seq ASC, created_at ASC, id ASC""",
            (session_id,),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in users:
            text = str(row["text"] or "").strip()
            if not text:
                continue
            prompt_seq = int(row["seq"])
            message_id = int(row["id"])
            reply_rows = self._message_reply_rows_with_conn(c, message_id)
            has_full_reply = 1 if any(str(reply["text"] or "").strip() for reply in reply_rows) else 0
            summary = str(row["summary"] or "").strip()
            events.append({
                "id": message_id,
                "session_id": row["session_id"],
                "student_id": row["student_id"],
                "content": text,
                "created_at": row["created_at"],
                "type": "prompt",
                "seq_in_session": prompt_seq,
                "prompt_id": None,
                "reply_ref": None,
                "report_id": None,
                "severity": None,
                "understanding": None,
                "suggestion": None,
                "is_technical": None,
                "topic": None,
                "has_summary": None,
                "has_full_reply": has_full_reply,
            })
            if has_full_reply:
                events.append({
                    "id": message_id,
                    "session_id": row["session_id"],
                    "student_id": row["student_id"],
                    "content": summary,
                    "created_at": float(reply_rows[0]["created_at"] or 0),
                    "type": "ai_summary",
                    "seq_in_session": prompt_seq,
                    "prompt_id": None,
                    "reply_ref": f"msg:{message_id}",
                    "report_id": None,
                    "severity": None,
                    "understanding": None,
                    "suggestion": None,
                    "is_technical": None,
                    "topic": None,
                    "has_summary": 1 if summary else 0,
                    "has_full_reply": has_full_reply,
                })
        return events

    def get_timeline_by_session(self, session_id: str) -> list[dict]:
        """Timeline aggregation by session.

        AI summary cards are prompt-scoped: at most one LLM summary per
        student prompt, with the full assistant reply loaded lazily by prompt_id.
        """
        with self._conn() as c:
            events = self._prompt_timeline_rows(c, session_id)
            if not events:
                events = self._bulk_message_timeline_rows(c, session_id)
            events.extend(self._analysis_timeline_rows(c, session_id))
            if events:
                priority = {"prompt": 0, "ai_summary": 1, "analysis": 2}
                events.sort(key=lambda item: (
                    float(item.get("created_at") or 0),
                    priority.get(str(item.get("type") or ""), 99),
                    int(item.get("id") or 0),
                ))
            return events

    def sessions_overview(self, student_id: str | None = None, limit: int = 10) -> list[dict]:
        """最近活跃对话概览：以 copilot.db sessions 表为权威源。"""
        sid = student_id or "student-1"
        return self.get_sessions_by_student(sid, limit=limit)

    def students_overview(self, limit: int = 50) -> list[dict]:
        """学员列表 + 状态概览（按 student_id 聚合）。"""
        with self._conn() as c:
            rows = c.execute(
                """WITH analysis_aggregate AS (
                     SELECT
                       student_id,
                       COUNT(id) AS analysis_count,
                       COUNT(DISTINCT session_id) AS session_count,
                       MAX(created_at) AS last_ts,
                       MAX(CASE severity
                         WHEN 'error' THEN 3
                         WHEN 'warn' THEN 2
                         ELSE 1
                       END) AS severity_rank,
                       SUM(CASE
                         WHEN COALESCE(alert, '') != ''
                           OR understanding IN ('low','stuck')
                         THEN 1 ELSE 0 END) AS alert_count
                     FROM analyses
                     GROUP BY student_id
                   ),
                   active_attention AS (
                     SELECT
                       student_id,
                       COUNT(id) AS open_attention_count,
                       MAX(CASE priority
                         WHEN 'high' THEN 2
                         WHEN 'medium' THEN 1
                         ELSE 0
                       END) AS priority_rank,
                       MAX(created_at) AS last_attention_at
                     FROM attention_items
                     WHERE status IN ('open', 'in_progress')
                     GROUP BY student_id
                   )
                   SELECT
                     s.student_id,
                     s.display_name AS display_name,
                     COALESCE(a.analysis_count, 0) AS analysis_count,
                     COALESCE(a.session_count, 0) AS session_count,
                     COALESCE(a.last_ts, s.created_at, 0) AS last_ts,
                     COALESCE((
                       SELECT a2.topic
                       FROM analyses a2
                       WHERE a2.student_id = s.student_id
                       ORDER BY a2.created_at DESC, a2.id DESC
                       LIMIT 1
                     ), '') AS last_topic,
                     CASE COALESCE(a.severity_rank, 1)
                       WHEN 3 THEN 'error'
                       WHEN 2 THEN 'warn'
                       ELSE 'info'
                     END AS last_severity,
                     COALESCE(a.alert_count, 0) AS alert_count,
                     COALESCE((
                       SELECT a2.diagnosis
                       FROM analyses a2
                       WHERE a2.student_id = s.student_id
                       ORDER BY a2.created_at DESC, a2.id DESC
                       LIMIT 1
                     ), '') AS last_diagnosis,
                     COALESCE(att.open_attention_count, 0) AS open_attention_count,
                     CASE COALESCE(att.priority_rank, 0)
                       WHEN 2 THEN 'high'
                       WHEN 1 THEN 'medium'
                       ELSE ''
                     END AS highest_attention_priority,
                     COALESCE(att.last_attention_at, 0) AS last_attention_at
                   FROM students s
                   LEFT JOIN analysis_aggregate a ON a.student_id = s.student_id
                   LEFT JOIN active_attention att ON att.student_id = s.student_id
                   ORDER BY COALESCE(a.last_ts, s.created_at, 0) DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_sessions_by_student(self, student_id: str, limit: int = 1000) -> list[dict]:
        """某学员的最近活跃对话列表，读取 copilot.db sessions 表。"""
        return self.get_sessions_by_student_from_table(student_id, limit=limit)

    def latest_for_student(self, student_id: str) -> dict | None:
        rows = self.recent_analyses(student_id, limit=1)
        return rows[0] if rows else None

    def unread_alerts(self, since_ts: float, student_id: str | None = None) -> list[dict]:
        with self._conn() as c:
            if student_id:
                rows = c.execute(
                    """SELECT * FROM analyses
                       WHERE created_at > ? AND student_id = ?
                         AND (alert != '' OR understanding IN ('low', 'stuck'))
                       ORDER BY created_at DESC LIMIT 50""",
                    (since_ts, student_id),
                ).fetchall()
            else:
                rows = c.execute(
                    """SELECT * FROM analyses
                       WHERE created_at > ?
                         AND (alert != '' OR understanding IN ('low', 'stuck'))
                       ORDER BY created_at DESC LIMIT 50""",
                    (since_ts,),
                ).fetchall()
            return [dict(r) for r in rows]

    def delete_student(self, student_id: str) -> dict[str, int]:
        """Delete one student's persisted data in a single transaction."""
        deleted: dict[str, int] = {}
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM attention_items WHERE student_id = ?",
                (student_id,),
            )
            if cur.rowcount:
                deleted["attention_items"] = cur.rowcount

            cur = c.execute(
                """DELETE FROM analyses
                   WHERE student_id = ?
                      OR report_id IN (SELECT id FROM reports WHERE student_id = ?)""",
                (student_id, student_id),
            )
            deleted["analyses"] = cur.rowcount

            cur = c.execute(
                """DELETE FROM ai_summaries
                   WHERE student_id = ?
                      OR prompt_id IN (SELECT id FROM prompts WHERE student_id = ?)""",
                (student_id, student_id),
            )
            deleted["ai_summaries"] = cur.rowcount

            c.execute("DELETE FROM student_asks WHERE student_id = ?", (student_id,))

            for table in [
                "messages",
                "upload_requests",
                "stop_transcript_watermarks",
            ]:
                cur = c.execute(f"DELETE FROM {table} WHERE student_id = ?", (student_id,))
                if cur.rowcount:
                    deleted[table] = cur.rowcount

            for table in [
                "prompts",
                "raw_transcripts",
                "mentor_messages",
                "reports",
                "sessions",
                "students",
            ]:
                cur = c.execute(f"DELETE FROM {table} WHERE student_id = ?", (student_id,))
                deleted[table] = cur.rowcount
        return deleted
