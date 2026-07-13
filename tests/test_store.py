"""store.py 单元测试：SQLite 存储逻辑。

用临时 DB 文件，测试 report/analysis 增删查。
"""
from __future__ import annotations

import sqlite3
import time
import pytest

from copilot.services import SessionQueryService
from copilot.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.db")


def _add_analysis_at(
    store: Store,
    student_id: str,
    session_id: str,
    created_at: float,
    result: dict,
) -> int:
    report_id = store.add_report(student_id, session_id, "Stop", "prompt", "/tmp/x", 1, 0)
    analysis_id = store.add_analysis(report_id, student_id, result, session_id, "标题")
    with store._conn() as conn:
        conn.execute("UPDATE analyses SET created_at = ? WHERE id = ?", (created_at, analysis_id))
    return analysis_id


class TestStore:
    def test_init_creates_tables(self, tmp_path):
        db = tmp_path / "test.db"
        s = Store(db)
        assert db.exists()

    def test_legacy_prompt_backfill_does_not_guess_for_empty_stop_prompt(self, tmp_path):
        db = tmp_path / "legacy-prompts.db"
        with sqlite3.connect(db) as conn:
            conn.executescript(
                """CREATE TABLE reports (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       student_id TEXT NOT NULL,
                       session_id TEXT,
                       event TEXT,
                       prompt TEXT,
                       transcript_path TEXT,
                       msg_count INTEGER,
                       tool_calls INTEGER,
                       analysis_pending INTEGER DEFAULT 0,
                       created_at REAL NOT NULL
                   );
                   CREATE TABLE prompts (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       session_id TEXT,
                       seq_in_session INTEGER,
                       student_id TEXT,
                       content TEXT,
                       created_at REAL NOT NULL
                   );
                   INSERT INTO reports
                       (student_id, session_id, event, prompt, transcript_path,
                        msg_count, tool_calls, analysis_pending, created_at)
                   VALUES ('legacy-stu', 'legacy-sess', 'Stop', '', '', 1, 0, 1, 10.0);
                   INSERT INTO prompts
                       (session_id, seq_in_session, student_id, content, created_at)
                   VALUES ('legacy-sess', 0, 'legacy-stu', 'nearby prompt', 10.5);
                   INSERT INTO prompts
                       (session_id, seq_in_session, student_id, content, created_at)
                   VALUES ('legacy-sess', 1, 'legacy-stu', 'far prompt', 1000.0);"""
            )

        store = Store(db)

        with store._conn() as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(prompts)").fetchall()
            }
            indexes = {
                row[1] for row in conn.execute("PRAGMA index_list(prompts)").fetchall()
            }
            prompts = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM prompts ORDER BY id",
                ).fetchall()
            ]
        assert "report_id" in columns
        assert "idx_prompts_report_id_unique" in indexes
        assert prompts[0]["report_id"] is None
        assert prompts[1]["report_id"] is None

    def test_legacy_prompt_backfill_leaves_many_to_many_candidates_unlinked(self, tmp_path):
        db = tmp_path / "legacy-ambiguous-prompts.db"
        with sqlite3.connect(db) as conn:
            conn.executescript(
                """CREATE TABLE reports (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       student_id TEXT NOT NULL,
                       session_id TEXT,
                       event TEXT,
                       prompt TEXT,
                       transcript_path TEXT,
                       msg_count INTEGER,
                       tool_calls INTEGER,
                       analysis_pending INTEGER DEFAULT 0,
                       created_at REAL NOT NULL
                   );
                   CREATE TABLE prompts (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       session_id TEXT,
                       seq_in_session INTEGER,
                       student_id TEXT,
                       content TEXT,
                       created_at REAL NOT NULL
                   );
                   INSERT INTO reports
                       (student_id, session_id, event, prompt, transcript_path,
                        msg_count, tool_calls, analysis_pending, created_at)
                   VALUES ('legacy-stu', 'legacy-sess', 'Stop', '', '', 1, 0, 1, 10.0),
                          ('legacy-stu', 'legacy-sess', 'Stop', '', '', 1, 0, 1, 11.0);
                   INSERT INTO prompts
                       (session_id, seq_in_session, student_id, content, created_at)
                   VALUES ('legacy-sess', 0, 'legacy-stu', 'first prompt', 12.0),
                          ('legacy-sess', 1, 'legacy-stu', 'second prompt', 12.5);"""
            )

        store = Store(db)

        with store._conn() as conn:
            report_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT report_id FROM prompts ORDER BY id",
                ).fetchall()
            ]
        assert report_ids == [None, None]

    def test_legacy_prompt_backfill_restart_excludes_already_bound_reports(self, tmp_path):
        db = tmp_path / "legacy-partially-migrated-prompts.db"
        with sqlite3.connect(db) as conn:
            conn.executescript(
                """CREATE TABLE reports (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       student_id TEXT NOT NULL,
                       session_id TEXT,
                       event TEXT,
                       prompt TEXT,
                       transcript_path TEXT,
                       msg_count INTEGER,
                       tool_calls INTEGER,
                       analysis_pending INTEGER DEFAULT 0,
                       created_at REAL NOT NULL
                   );
                   CREATE TABLE prompts (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       report_id INTEGER,
                       session_id TEXT,
                       seq_in_session INTEGER,
                       student_id TEXT,
                       content TEXT,
                       created_at REAL NOT NULL
                   );
                   INSERT INTO reports
                       (student_id, session_id, event, prompt, transcript_path,
                        msg_count, tool_calls, analysis_pending, created_at)
                   VALUES ('legacy-stu', 'legacy-sess', 'Stop', '', '', 1, 0, 1, 10.0);
                   INSERT INTO prompts
                       (report_id, session_id, seq_in_session, student_id, content, created_at)
                   VALUES (1, 'legacy-sess', 0, 'legacy-stu', 'already bound', 10.1),
                          (NULL, 'legacy-sess', 1, 'legacy-stu', 'still unbound', 10.2);"""
            )

        store = Store(db)

        with store._conn() as conn:
            report_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT report_id FROM prompts ORDER BY id",
                ).fetchall()
            ]
            indexes = {
                row[1] for row in conn.execute("PRAGMA index_list(prompts)").fetchall()
            }
        assert report_ids == [1, None]
        assert "idx_prompts_report_id_unique" in indexes

    def test_legacy_prompt_backfill_exact_content_respects_time_window(self, tmp_path):
        db = tmp_path / "legacy-exact-prompt-outside-window.db"
        with sqlite3.connect(db) as conn:
            conn.executescript(
                """CREATE TABLE reports (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       student_id TEXT NOT NULL,
                       session_id TEXT,
                       event TEXT,
                       prompt TEXT,
                       transcript_path TEXT,
                       msg_count INTEGER,
                       tool_calls INTEGER,
                       analysis_pending INTEGER DEFAULT 0,
                       created_at REAL NOT NULL
                   );
                   CREATE TABLE prompts (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       session_id TEXT,
                       seq_in_session INTEGER,
                       student_id TEXT,
                       content TEXT,
                       created_at REAL NOT NULL
                   );
                   INSERT INTO reports
                       (student_id, session_id, event, prompt, transcript_path,
                        msg_count, tool_calls, analysis_pending, created_at)
                   VALUES ('legacy-stu', 'legacy-sess', 'Stop', 'exact prompt', '',
                           1, 0, 1, 10.0);
                   INSERT INTO prompts
                       (session_id, seq_in_session, student_id, content, created_at)
                   VALUES ('legacy-sess', 0, 'legacy-stu', 'exact prompt', 1000.0);"""
            )

        store = Store(db)

        with store._conn() as conn:
            report_id = conn.execute(
                "SELECT report_id FROM prompts",
            ).fetchone()[0]
        assert report_id is None

    def test_legacy_prompt_backfill_nonempty_pair_is_causal_and_immediate(self, tmp_path):
        db = tmp_path / "legacy-causal-prompts.db"
        store = Store(db)
        immediate_report = store.add_report(
            "legacy-stu",
            "legacy-immediate",
            "Stop",
            "same prompt",
            "",
            1,
            0,
        )
        backwards_report = store.add_report(
            "legacy-stu",
            "legacy-backwards",
            "Stop",
            "same prompt",
            "",
            1,
            0,
        )
        immediate_prompt = store.add_prompt(
            "legacy-immediate",
            0,
            "legacy-stu",
            "same prompt",
        )
        backwards_prompt = store.add_prompt(
            "legacy-backwards",
            0,
            "legacy-stu",
            "same prompt",
        )
        with store._conn() as conn:
            conn.execute(
                """UPDATE reports
                   SET analysis_pending = 1, created_at = 10.0
                   WHERE id = ?""",
                (immediate_report,),
            )
            conn.execute(
                """UPDATE reports
                   SET analysis_pending = 1, created_at = 20.0
                   WHERE id = ?""",
                (backwards_report,),
            )
            conn.execute(
                "UPDATE prompts SET created_at = 15.0 WHERE id = ?",
                (immediate_prompt,),
            )
            conn.execute(
                "UPDATE prompts SET created_at = 19.9 WHERE id = ?",
                (backwards_prompt,),
            )

        Store(db)
        store = Store(db)

        assert store.get_prompt(immediate_prompt)["report_id"] == immediate_report
        assert store.get_prompt(backwards_prompt)["report_id"] is None

    def test_add_report(self, store):
        rid = store.add_report(
            student_id="alice",
            session_id="sess-1",
            event="Stop",
            prompt="学 Python",
            transcript_path="/tmp/x.jsonl",
            msg_count=5,
            tool_calls=2,
        )
        assert rid > 0

    def test_add_analysis(self, store):
        rid = store.add_report("alice", "s1", "Stop", "hi", "/tmp/x", 3, 0)
        aid = store.add_analysis(rid, "alice", {
            "topic": "学 Python",
            "understanding": "high",
            "off_topic": False,
            "stuck_at": "",
            "progress": "进展顺利",
            "guidance": "继续",
            "alert": "",
        })
        assert aid > 0

    def test_recent_analyses_empty(self, store):
        rows = store.recent_analyses(student_id=None, limit=10)
        assert rows == []

    def test_recent_analyses_with_data(self, store):
        rid = store.add_report("alice", "s1", "Stop", "hi", "/tmp/x", 3, 0)
        store.add_analysis(rid, "alice", {
            "topic": "学 asyncio",
            "understanding": "low",
            "off_topic": False,
            "stuck_at": "await 概念",
            "progress": "刚开始",
            "guidance": "先看示例",
            "alert": "理解度低",
        })
        rows = store.recent_analyses(student_id="alice", limit=10)
        assert len(rows) == 1
        assert rows[0]["topic"] == "学 asyncio"
        assert rows[0]["understanding"] == "low"

    def test_recent_analyses_filter_by_student(self, store):
        r1 = store.add_report("alice", "s1", "Stop", "hi", "/tmp/x", 1, 0)
        store.add_analysis(r1, "alice", {"topic": "A", "understanding": "high"})
        r2 = store.add_report("bob", "s2", "Stop", "hi", "/tmp/x", 1, 0)
        store.add_analysis(r2, "bob", {"topic": "B", "understanding": "medium"})

        alice_rows = store.recent_analyses("alice", limit=10)
        assert len(alice_rows) == 1
        assert alice_rows[0]["topic"] == "A"

    def test_latest_for_student(self, store):
        r1 = store.add_report("alice", "s1", "Stop", "hi", "/tmp/x", 1, 0)
        store.add_analysis(r1, "alice", {"topic": "旧", "understanding": "high"})
        time.sleep(0.01)
        r2 = store.add_report("alice", "s2", "Stop", "hi", "/tmp/x", 1, 0)
        store.add_analysis(r2, "alice", {"topic": "新", "understanding": "medium"})

        latest = store.latest_for_student("alice")
        assert latest is not None
        assert latest["topic"] == "新"

    def test_latest_for_student_none(self, store):
        assert store.latest_for_student("nobody") is None

    def test_unread_alerts(self, store):
        old_ts = time.time() - 3600
        r1 = store.add_report("alice", "s1", "Stop", "hi", "/tmp/x", 1, 0)
        store.add_analysis(r1, "alice", {
            "topic": "正常", "understanding": "high", "alert": "",
        })
        r2 = store.add_report("alice", "s2", "Stop", "hi", "/tmp/x", 1, 0)
        store.add_analysis(r2, "alice", {
            "topic": "卡住", "understanding": "stuck", "alert": "需要关注",
        })

        alerts = store.unread_alerts(since_ts=old_ts, student_id="alice")
        assert len(alerts) == 1
        assert alerts[0]["understanding"] == "stuck"

    def test_report_prompt_is_persisted_for_crash_recovery(self, store):
        long_prompt = "x" * 5000
        rid = store.add_report("alice", "s1", "Stop", long_prompt, "/tmp/x", 1, 0)
        with store._conn() as conn:
            prompt = conn.execute(
                "SELECT prompt FROM reports WHERE id = ?",
                (rid,),
            ).fetchone()[0]
        assert prompt == long_prompt

    def test_students_overview_uses_worst_severity_but_latest_text_fields(self, store):
        _add_analysis_at(store, "alice", "sess-1", 100.0, {
            "topic": "z-old-error-topic",
            "understanding": "low",
            "severity": "error",
            "diagnosis": "z-old-error-diagnosis",
        })
        _add_analysis_at(store, "alice", "sess-1", 200.0, {
            "topic": "a-new-info-topic",
            "understanding": "high",
            "severity": "info",
            "diagnosis": "a-new-info-diagnosis",
        })

        row = store.students_overview()[0]

        assert row["last_severity"] == "error"
        assert row["last_topic"] == "a-new-info-topic"
        assert row["last_diagnosis"] == "a-new-info-diagnosis"

    def test_list_students_includes_students_without_analysis(self, store):
        store.upsert_student("bob", "Bob")

        students = SessionQueryService(store, {
            "student_id": "host",
            "student_name": "Host",
        }).list_students()

        assert [student.student_id for student in students] == ["bob"]
        assert students[0].analysis_count == 0
        assert students[0].session_count == 0
        assert students[0].last_ts > 0
        assert students[0].last_severity == "info"
        assert students[0].alert_count == 0
        assert students[0].last_topic == ""
        assert students[0].last_diagnosis == ""

    def test_list_students_uses_stored_display_name_for_non_config_student(self, store):
        store.upsert_student("stu-x", "张三")
        _add_analysis_at(store, "stu-x", "sess-x", 100.0, {
            "topic": "Python",
            "understanding": "high",
        })

        students = SessionQueryService(store, {
            "student_id": "host",
            "student_name": "Host",
        }).list_students()

        assert len(students) == 1
        assert students[0].student_id == "stu-x"
        assert students[0].display_name == "张三"

    def test_get_timeline_includes_analysis_topic(self, store):
        _add_analysis_at(store, "alice", "sess-topic", 100.0, {
            "topic": "loop debugging",
            "understanding": "low",
            "severity": "warn",
            "diagnosis": "The loop boundary is inverted.",
            "suggestion": "Print the loop index.",
            "is_technical": True,
        })

        entries = SessionQueryService(store, {}).get_timeline("sess-topic")
        analysis = [entry for entry in entries if entry.type == "analysis"][0]

        assert analysis.topic == "loop debugging"
        assert analysis.severity == "warn"
        assert analysis.suggestion == "Print the loop index."

    def test_raw_transcript_for_report_does_not_use_unmatched_latest(self, store):
        report_id = store.add_report(
            "alice",
            "sess-raw",
            "Stop",
            "legacy prompt",
            "copilot:explicit-raw-transcript",
            1,
            0,
        )
        raw_id = store.add_raw_transcript("sess-raw", "alice", "future raw")
        with store._conn() as conn:
            conn.execute(
                """UPDATE reports
                   SET analysis_pending = 1,
                       analysis_status = 'pending',
                       created_at = 100.0
                   WHERE id = ?""",
                (report_id,),
            )
            conn.execute(
                "UPDATE raw_transcripts SET created_at = ? WHERE id = ?",
                (50.0, raw_id),
            )

        assert store.get_raw_transcript_for_report(report_id) is None

    def test_add_and_list_student_asks(self, store):
        first_id = store.add_student_ask(
            student_id="alice",
            session_id="sess-1",
            question="为什么 pytest 找不到模块？",
            answer="先确认运行目录和 PYTHONPATH。",
        )
        second_id = store.add_student_ask(
            student_id="alice",
            session_id="sess-2",
            question="怎么定位 off-by-one？",
            answer="用最小输入打印边界值。",
        )
        store.add_student_ask(
            student_id="bob",
            session_id="sess-bob",
            question="Bob 的问题",
            answer="Bob 的答案",
        )

        all_alice = store.list_student_asks("alice")
        sess_1 = store.list_student_asks("alice", "sess-1")

        assert [row["id"] for row in all_alice] == [second_id, first_id]
        assert [row["id"] for row in sess_1] == [first_id]
        assert sess_1[0]["question"] == "为什么 pytest 找不到模块？"
        assert sess_1[0]["answer"] == "先确认运行目录和 PYTHONPATH。"

    def test_sessions_overview_uses_worst_severity_but_latest_text_fields(self, store):
        store.upsert_student("alice", "Alice")
        store.upsert_session(
            "sess-1", "alice", "/work/alice", "会话标题",
            created_at=50.0, last_activity_at=200.0,
        )
        _add_analysis_at(store, "alice", "sess-1", 100.0, {
            "topic": "z-old-error-topic",
            "understanding": "low",
            "severity": "error",
            "diagnosis": "z-old-error-diagnosis",
        })
        _add_analysis_at(store, "alice", "sess-1", 200.0, {
            "topic": "a-new-info-topic",
            "understanding": "high",
            "severity": "info",
            "diagnosis": "a-new-info-diagnosis",
        })

        row = store.get_sessions_by_student("alice")[0]

        assert row["last_severity"] == "error"
        assert row["last_topic"] == "a-new-info-topic"
        assert row["last_diagnosis"] == "a-new-info-diagnosis"
        assert row["session_title"] == "会话标题"

    def test_get_sessions_by_student_is_scoped_to_student(self, store):
        store.upsert_student("alice", "Alice")
        store.upsert_student("bob", "Bob")
        store.upsert_session("sess-a", "alice", "/work/a", "Alice 会话", 1.0, 2.0)
        store.upsert_session("sess-b", "bob", "/work/b", "Bob 会话", 3.0, 4.0)

        rows = store.get_sessions_by_student("alice")

        assert [row["session_id"] for row in rows] == ["sess-a"]

    def test_get_sessions_by_student_returns_message_count_without_analysis_inflation(self, store):
        store.upsert_student("alice", "Alice")
        store.upsert_session("sess-content", "alice", "/work/content", "有内容", 1.0, 2.0)
        store.upsert_session("sess-both", "alice", "/work/both", "有内容有分析", 3.0, 4.0)
        store.replace_session_messages(
            "sess-content",
            "alice",
            [
                {"seq": 0, "role": "user", "text": "第一问", "ts": 10.0},
                {"seq": 0, "role": "assistant", "text": "第一答", "ts": 11.0},
            ],
            raw="raw-content",
            sha="sha-content",
        )
        store.replace_session_messages(
            "sess-both",
            "alice",
            [
                {"seq": 0, "role": "user", "text": "第二问", "ts": 12.0},
                {"seq": 0, "role": "assistant", "text": "第二答", "ts": 13.0},
            ],
            raw="raw-both",
            sha="sha-both",
        )
        for created_at in (20.0, 21.0):
            _add_analysis_at(store, "alice", "sess-both", created_at, {
                "topic": "debug",
                "understanding": "high",
                "severity": "info",
                "diagnosis": "ok",
            })

        by_id = {row["session_id"]: row for row in store.get_sessions_by_student("alice")}

        assert by_id["sess-content"]["message_count"] == 2
        assert by_id["sess-content"]["analysis_count"] == 0
        assert by_id["sess-both"]["message_count"] == 2
        assert by_id["sess-both"]["analysis_count"] == 2

    def test_foreign_keys_are_enforced_for_each_connection(self, store):
        with pytest.raises(sqlite3.IntegrityError):
            store.add_analysis(999, "alice", {"topic": "bad", "understanding": "high"}, "sess-1")

    def test_replace_session_messages_is_idempotent_and_updates_raw_sha(self, store):
        turns = [
            {"seq": 0, "role": "user", "text": "第一问", "ts": 10.0},
            {"seq": 0, "role": "assistant", "text": "第一答", "ts": 11.0},
        ]

        store.replace_session_messages(
            session_id="sess-bulk",
            student_id="alice",
            turns=turns,
            raw="raw-v1",
            sha="sha-v1",
        )
        store.replace_session_messages(
            session_id="sess-bulk",
            student_id="alice",
            turns=turns,
            raw="raw-v1",
            sha="sha-v1",
        )

        with store._conn() as conn:
            message_count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                ("sess-bulk",),
            ).fetchone()[0]
            raw_rows = conn.execute(
                "SELECT content, content_sha256 FROM raw_transcripts WHERE session_id = ?",
                ("sess-bulk",),
            ).fetchall()

        assert message_count == 2
        assert len(raw_rows) == 1
        assert raw_rows[0]["content"] == "raw-v1"
        assert raw_rows[0]["content_sha256"] == "sha-v1"

    def test_replace_session_messages_rejects_cross_student_session_collision(self, store):
        store.replace_session_messages(
            session_id="sess-collision",
            student_id="alice",
            turns=[{"seq": 0, "role": "user", "text": "Alice 原文", "ts": 1.0}],
            raw="alice raw",
            sha="sha-alice",
        )

        with pytest.raises(ValueError):
            store.replace_session_messages(
                session_id="sess-collision",
                student_id="bob",
                turns=[{"seq": 0, "role": "user", "text": "Bob 覆盖", "ts": 2.0}],
                raw="bob raw",
                sha="sha-bob",
            )

        timeline = store.get_timeline_by_session("sess-collision")
        assert [row["content"] for row in timeline] == ["Alice 原文"]
        assert store.get_known_session_shas("alice") == {
            "sess-collision": {"sha": "sha-alice", "analysis_status": ""},
        }
        assert store.get_known_session_shas("bob") == {}

    def test_get_timeline_uses_prompt_summaries_when_bulk_messages_exist(self, store):
        first_prompt_id = store.add_prompt("sess-bulk", 0, "alice", "第一问")
        second_prompt_id = store.add_prompt("sess-bulk", 1, "alice", "第二问")
        store.add_ai_summary(first_prompt_id, "sess-bulk", "alice", "第一问 LLM 摘要")
        store.add_ai_summary(second_prompt_id, "sess-bulk", "alice", "第二问 LLM 摘要")
        report_id = store.add_report("alice", "sess-bulk", "Stop", "prompt", "", 1, 0)
        store.add_analysis(
            report_id,
            "alice",
            {
                "topic": "bulk topic",
                "understanding": "medium",
                "severity": "info",
                "diagnosis": "诊断仍叠加展示",
                "suggestion": "继续观察",
            },
            "sess-bulk",
            "标题",
        )

        store.replace_session_messages(
            session_id="sess-bulk",
            student_id="alice",
            turns=[
                {"seq": 0, "role": "user", "text": "第一问", "ts": 1.0},
                {"seq": 0, "role": "assistant", "text": "第一问完整回复", "ts": 2.0},
                {"seq": 1, "role": "user", "text": "第二问", "ts": 3.0},
                {"seq": 1, "role": "assistant", "text": "第二问完整回复", "ts": 4.0},
            ],
            raw="bulk raw",
            sha="sha-bulk",
        )

        timeline = store.get_timeline_by_session("sess-bulk")

        contents = [row["content"] for row in timeline]
        assert "第一问" in contents
        assert "第二问" in contents
        assert "第一问 LLM 摘要" in contents
        assert "第二问 LLM 摘要" in contents
        assert "诊断仍叠加展示" in contents
        assert "第一问完整回复" not in contents
        summaries = [row for row in timeline if row["type"] == "ai_summary"]
        assert [row["prompt_id"] for row in summaries] == [first_prompt_id, second_prompt_id]
        assert [row["has_full_reply"] for row in summaries] == [1, 1]

    def test_get_known_session_shas_returns_latest_sha_per_student(self, store):
        store.replace_session_messages("sess-1", "alice", [], "raw-1", "sha-1")
        store.replace_session_messages("sess-2", "alice", [], "raw-2", "sha-2")
        store.replace_session_messages("sess-bob", "bob", [], "raw-bob", "sha-bob")

        assert store.get_known_session_shas("alice") == {
            "sess-1": {"sha": "sha-1", "analysis_status": ""},
            "sess-2": {"sha": "sha-2", "analysis_status": ""},
        }

    def test_upload_requests_can_be_added_and_listed_by_student(self, store):
        first_id = store.add_upload_request(
            mentor_id="mentor-1",
            student_id="alice",
            session_id=None,
        )
        store.add_upload_request(
            mentor_id="mentor-1",
            student_id="bob",
            session_id="sess-bob",
            status="pending",
        )

        rows = store.list_pending_upload_requests("alice")

        assert [row["request_id"] for row in rows] == [first_id]
        assert rows[0]["mentor_id"] == "mentor-1"
        assert rows[0]["student_id"] == "alice"
        assert rows[0]["status"] == "pending"

    def test_upsert_session_rejects_cross_student_owner_change(self, store):
        store.upsert_session(
            "sess-owner",
            "alice",
            "/work/alice",
            "Alice title",
            created_at=1.0,
            last_activity_at=10.0,
        )

        with pytest.raises(ValueError):
            store.upsert_session(
                "sess-owner",
                "bob",
                "/work/bob",
                "Bob title",
                created_at=2.0,
                last_activity_at=20.0,
            )

        rows = store.get_sessions_by_student("alice")
        assert len(rows) == 1
        assert rows[0]["student_id"] == "alice"
        assert rows[0]["session_title"] == "Alice title"
        assert rows[0]["work_dir"] == "/work/alice"
        assert store.get_sessions_by_student("bob") == []
