"""测试 store.py 导师观察台数据层：prompts + ai_summaries 新表 + timeline 聚合。"""
import tempfile
import os
from pathlib import Path

import pytest

from copilot.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


class TestPromptsTable:
    """prompts 表：学员提示词全文存储。"""

    def test_add_prompt_returns_id(self, store):
        pid = store.add_prompt(
            session_id="sess-1", seq_in_session=0,
            student_id="stu-1", content="你好，帮我学Python",
        )
        assert isinstance(pid, int) and pid > 0

    def test_prompt_full_text_not_truncated(self, store):
        long_text = "字" * 100000
        pid = store.add_prompt(
            session_id="sess-1", seq_in_session=0,
            student_id="stu-1", content=long_text,
        )
        rows = store.get_prompts_by_session("sess-1")
        assert len(rows) == 1
        assert len(rows[0]["content"]) == 100000

    def test_get_prompts_by_session_ordered(self, store):
        for i in range(3):
            store.add_prompt("sess-1", i, "stu-1", f"prompt-{i}")
        rows = store.get_prompts_by_session("sess-1")
        assert len(rows) == 3
        assert rows[0]["seq_in_session"] == 0
        assert rows[2]["seq_in_session"] == 2

    def test_get_prompts_isolated_by_session(self, store):
        store.add_prompt("sess-1", 0, "stu-1", "a")
        store.add_prompt("sess-2", 0, "stu-1", "b")
        assert len(store.get_prompts_by_session("sess-1")) == 1
        assert len(store.get_prompts_by_session("sess-2")) == 1


class TestAISummariesTable:
    """ai_summaries 表：AI 回答摘要，外键关联 prompts。"""

    def test_add_ai_summary_with_prompt_id(self, store):
        pid = store.add_prompt("sess-1", 0, "stu-1", "提问")
        sid = store.add_ai_summary(
            prompt_id=pid, session_id="sess-1",
            student_id="stu-1", content="AI回答的客观摘要",
        )
        assert isinstance(sid, int) and sid > 0

    def test_ai_summary_content_stored(self, store):
        pid = store.add_prompt("sess-1", 0, "stu-1", "提问")
        store.add_ai_summary(pid, "sess-1", "stu-1", "摘要内容ABC")
        rows = store.get_ai_summaries_by_session("sess-1")
        assert len(rows) == 1
        assert rows[0]["content"] == "摘要内容ABC"
        assert rows[0]["prompt_id"] == pid

    def test_ai_summary_upsert_keeps_one_row_per_prompt(self, store):
        pid = store.add_prompt("sess-1", 0, "stu-1", "提问")
        first_id = store.add_ai_summary(pid, "sess-1", "stu-1", "旧摘要")
        second_id = store.upsert_ai_summary(pid, "sess-1", "stu-1", "新摘要")

        rows = store.get_ai_summaries_by_session("sess-1")

        assert second_id == first_id
        assert len(rows) == 1
        assert rows[0]["content"] == "新摘要"


class TestTimelineAggregation:
    """timeline 聚合：三表 UNION 按时间排序。"""

    def test_timeline_returns_mixed_types(self, store):
        # 存 prompt
        store.add_prompt("sess-1", 0, "stu-1", "学员提问1")
        # 存 ai_summary（需要先有 prompt）
        pid = store.add_prompt("sess-1", 1, "stu-1", "学员提问2")
        store.add_ai_summary(pid, "sess-1", "stu-1", "AI摘要")
        # 存 analysis（需要先有 report + analysis）
        report_id = store.add_report("stu-1", "sess-1", "Stop", "prompt", "path", 5, 2)
        store.add_analysis(report_id, "stu-1", {"topic": "test", "understanding": "medium"}, "sess-1", "标题")
        # 获取 timeline
        timeline = store.get_timeline_by_session("sess-1")
        types = {item["type"] for item in timeline}
        assert "prompt" in types
        assert "ai_summary" in types
        assert "analysis" in types

    def test_timeline_analysis_items_include_analysis_fields(self, store):
        report_id = store.add_report("stu-1", "sess-1", "Stop", "prompt", "path", 5, 2)
        store.add_analysis(report_id, "stu-1", {
            "topic": "函数调试",
            "understanding": "low",
            "severity": "warn",
            "diagnosis": "循环条件写反",
            "suggestion": "先打印边界变量",
            "is_technical": True,
        }, "sess-1", "标题")

        analysis = next(item for item in store.get_timeline_by_session("sess-1") if item["type"] == "analysis")

        assert analysis["severity"] == "warn"
        assert analysis["suggestion"] == "先打印边界变量"
        assert analysis["is_technical"] == 1
        assert analysis["topic"] == "函数调试"
        assert analysis["understanding"] == "low"

    def test_timeline_ordered_by_created_at(self, store):
        store.add_prompt("sess-1", 0, "stu-1", "第一")
        store.add_prompt("sess-1", 1, "stu-1", "第二")
        timeline = store.get_timeline_by_session("sess-1")
        assert len(timeline) == 2
        assert timeline[0]["created_at"] <= timeline[1]["created_at"]

    def test_timeline_empty_session(self, store):
        timeline = store.get_timeline_by_session("nonexistent")
        assert timeline == []

    def test_timeline_includes_only_its_session_mentor_messages(self, store):
        store.upsert_student("stu-1")
        store.upsert_session("sess-1", "stu-1", "", "", created_at=1, last_activity_at=1)
        store.upsert_session("sess-2", "stu-1", "", "", created_at=1, last_activity_at=1)
        store.add_mentor_message("stu-1", "mentor", "sess-1", "session message", "msg-session")
        store.add_mentor_message("stu-1", "mentor", "", "student notice", "msg-student")

        session_one = [row for row in store.get_timeline_by_session("sess-1") if row["type"] == "mentor_message"]
        session_two = [row for row in store.get_timeline_by_session("sess-2") if row["type"] == "mentor_message"]

        assert [row["content"] for row in session_one] == ["session message"]
        assert session_two == []

    def test_get_prompt_reply_concatenates_assistants_until_next_user(self, store):
        store.replace_session_messages(
            session_id="sess-reply",
            student_id="stu-1",
            turns=[
                {"seq": 0, "role": "user", "text": "第一问", "ts": 10.0},
                {"seq": 0, "role": "assistant", "text": "第一答 A", "ts": 11.0},
                {"seq": 1, "role": "assistant", "text": "第一答 B", "ts": 12.0},
                {"seq": 2, "role": "user", "text": "第二问", "ts": 20.0},
                {"seq": 2, "role": "assistant", "text": "第二答", "ts": 21.0},
            ],
            raw="raw",
            sha="sha",
        )

        assert store.get_prompt_reply("sess-reply", 0) == "第一答 A\n\n第一答 B"
        assert store.get_prompt_reply("sess-reply", 1) == "第二答"

    def test_bulk_timeline_uses_user_message_summary_and_reply_ref(self, store):
        store.replace_session_messages(
            session_id="sess-bulk",
            student_id="stu-1",
            turns=[
                {"seq": 0, "role": "user", "text": "第一问", "ts": 10.0},
                {"seq": 0, "role": "assistant", "text": "第一问完整回复 A", "ts": 11.0},
                {"seq": 1, "role": "assistant", "text": "第一问完整回复 B", "ts": 12.0},
                {"seq": 2, "role": "user", "text": "第二问", "ts": 20.0},
                {"seq": 2, "role": "assistant", "text": "第二问完整回复", "ts": 21.0},
            ],
            raw="raw",
            sha="sha",
        )
        with store._conn() as conn:
            user_rows = conn.execute(
                """SELECT id FROM messages
                   WHERE session_id = ? AND role = 'user'
                   ORDER BY seq ASC""",
                ("sess-bulk",),
            ).fetchall()
            conn.execute(
                "UPDATE messages SET summary = ? WHERE id = ?",
                ("第一问 LLM 摘要", user_rows[0]["id"]),
            )

        timeline = store.get_timeline_by_session("sess-bulk")
        summaries = [item for item in timeline if item["type"] == "ai_summary"]

        assert [item["content"] for item in summaries] == ["第一问 LLM 摘要", ""]
        assert [item["prompt_id"] for item in summaries] == [None, None]
        assert [item["reply_ref"] for item in summaries] == [
            f"msg:{user_rows[0]['id']}",
            f"msg:{user_rows[1]['id']}",
        ]
        assert [item["has_full_reply"] for item in summaries] == [1, 1]
        assert "第一问完整回复 A" not in [item["content"] for item in summaries]

    def test_timeline_has_one_ai_summary_per_prompt_with_reply_flag(self, store):
        first_prompt = store.add_prompt("sess-1", 0, "stu-1", "第一问")
        second_prompt = store.add_prompt("sess-1", 1, "stu-1", "第二问")
        store.upsert_ai_summary(first_prompt, "sess-1", "stu-1", "第一问 LLM 摘要")
        store.upsert_ai_summary(second_prompt, "sess-1", "stu-1", "第二问 LLM 摘要")
        store.replace_session_messages(
            session_id="sess-1",
            student_id="stu-1",
            turns=[
                {"seq": 0, "role": "user", "text": "第一问", "ts": 10.0},
                {"seq": 0, "role": "assistant", "text": "第一问完整回复 A", "ts": 11.0},
                {"seq": 1, "role": "assistant", "text": "第一问完整回复 B", "ts": 12.0},
                {"seq": 2, "role": "user", "text": "第二问", "ts": 20.0},
                {"seq": 2, "role": "assistant", "text": "第二问完整回复", "ts": 21.0},
            ],
            raw="raw",
            sha="sha",
        )

        timeline = store.get_timeline_by_session("sess-1")
        summaries = [item for item in timeline if item["type"] == "ai_summary"]

        assert [item["content"] for item in summaries] == ["第一问 LLM 摘要", "第二问 LLM 摘要"]
        assert [item["prompt_id"] for item in summaries] == [first_prompt, second_prompt]
        assert [item["reply_ref"] for item in summaries] == [f"prompt:{first_prompt}", f"prompt:{second_prompt}"]
        assert [item["has_full_reply"] for item in summaries] == [1, 1]
        assert "第一问完整回复 A" not in [item["content"] for item in summaries]


class TestBackwardCompatibility:
    """旧库迁移不破坏现有功能。"""

    def test_existing_reports_still_work(self, store):
        rid = store.add_report("stu-1", "sess-1", "Stop", "prompt", "path", 3, 1)
        rows = store.recent_analyses(None, limit=10)
        # 即使没 analysis，report 应该能存
        assert rid > 0

    def test_existing_analyses_still_work(self, store):
        rid = store.add_report("stu-1", "sess-1", "Stop", "p", "path", 3, 1)
        store.add_analysis(rid, "stu-1", {"topic": "t", "understanding": "high"}, "sess-1", "标题")
        rows = store.recent_analyses("stu-1", limit=10)
        assert len(rows) == 1
