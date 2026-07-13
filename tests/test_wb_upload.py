from __future__ import annotations

import hashlib
import json
import sqlite3

from copilot import wb_upload
from copilot.student_platform.workbuddy import TranscriptReadResult, WorkBuddySession


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _session(session_id: str, work_dir: str) -> WorkBuddySession:
    return WorkBuddySession(
        session_id=session_id,
        title=session_id,
        work_dir=work_dir,
        created_at=1.0,
        last_activity_at=2.0,
        deleted=False,
        group_type="",
        space_name="",
    )


class _SyntheticAdapter:
    def __init__(self, sessions, transcripts):
        self._sessions = sessions
        self._transcripts = transcripts
        self.read_session_ids = []

    def list_sessions(self):
        return list(self._sessions)

    def read_transcript(self, session_id):
        self.read_session_ids.append(session_id)
        return TranscriptReadResult(content=self._transcripts[session_id])


def test_encode_cwd_matches_workbuddy_project_path_for_chinese_directory():
    assert (
        wb_upload.encode_cwd("/Users/student/projects/示例项目")
        == "Users-student-projects-示例项目"
    )
    assert wb_upload.encode_cwd("relative/path") == "relative-path"


def test_transcript_path_for_session_with_db_does_not_expose_verified_local_path(tmp_path):
    config_dir = tmp_path / ".workbuddy"
    projects = config_dir / "projects"
    projects.mkdir(parents=True)
    database = config_dir / "workbuddy.db"
    conn = sqlite3.connect(database)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, cwd TEXT, title TEXT, custom_title TEXT,
                created_at INTEGER, last_activity_at INTEGER, deleted_at INTEGER
            );
            CREATE TABLE workspaces (path TEXT PRIMARY KEY, name TEXT, last_opened_at INTEGER);
            """
        )
        conn.commit()
    finally:
        conn.close()
    transcript = projects / "opaque" / "metadata.jsonl"
    transcript.parent.mkdir()
    transcript.write_text(
        _line({"type": "message", "session_id": "session-from-metadata", "content": "hello"}),
        encoding="utf-8",
    )

    path = wb_upload.transcript_path_for_session(
        {"session_id": "session-from-metadata", "work_dir": "/a/cwd/that-is-not-an-index"},
        projects_dir=projects,
        db_path=database,
    )

    assert path is None


def test_filter_jsonl_keeps_only_message_line_text(tmp_path):
    jsonl = tmp_path / "sess-1.jsonl"
    message_user = _line({
        "type": "message",
        "role": "user",
        "content": "<user_query>你好</user_query>",
    })
    message_assistant = _line({
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "你好，继续。"}],
    })
    jsonl.write_text(
        message_user
        + _line({"type": "reasoning", "text": "hidden"})
        + _line({"type": "function_call", "name": "read_file"})
        + _line({"type": "function_call_result", "content": "secret"})
        + _line({"type": "file-history-snapshot", "content": "local file"})
        + _line({"type": "ai-title", "aiTitle": "标题"})
        + "not-json\n"
        + message_assistant,
        encoding="utf-8",
    )

    filtered = wb_upload.filter_message_jsonl(jsonl)

    assert filtered == message_user + message_assistant
    assert "function_call" not in filtered
    assert "secret" not in filtered
    assert "file-history-snapshot" not in filtered


def test_filter_jsonl_keeps_legacy_user_home_expansion(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    jsonl = tmp_path / "legacy.jsonl"
    message = _line({"type": "message", "content": "still expands home"})
    jsonl.write_text(message, encoding="utf-8")

    assert wb_upload.filter_message_jsonl("~/legacy.jsonl") == message


def test_content_sha256_is_deterministic_for_filtered_content():
    content = _line({"type": "message", "role": "user", "content": "hi"})

    assert wb_upload.content_sha256(content) == wb_upload.content_sha256(content)
    assert wb_upload.content_sha256(content) == hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_get_known_shas_normalizes_new_and_legacy_manifests(monkeypatch):
    requested_urls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "new": {"sha": "sha-new", "analysis_status": "failed"},
                "legacy": "sha-old",
                "invalid": {"analysis_status": "done"},
            }).encode("utf-8")

    def fake_urlopen(req, timeout):
        requested_urls.append(req.full_url)
        return FakeResponse()

    monkeypatch.setattr(wb_upload.urllib.request, "urlopen", fake_urlopen)

    manifest = wb_upload.get_known_shas("https://server.example", "student-a")

    assert manifest == {
        "new": {"sha": "sha-new", "analysis_status": "failed"},
        "legacy": {"sha": "sha-old", "analysis_status": "unknown"},
    }
    assert "manifest_version=2" in requested_urls[0]


def test_should_upload_retries_failed_same_sha_and_skips_other_same_sha_states():
    sha = "same-sha"

    assert wb_upload.should_upload(sha, {"sha": sha, "analysis_status": "failed"}) is True
    for status in ("done", "pending", "running", "skipped", ""):
        assert wb_upload.should_upload(sha, {"sha": sha, "analysis_status": status}) is False
    assert wb_upload.should_upload(sha, sha) is True
    assert wb_upload.should_upload(sha, {"sha": "different", "analysis_status": "done"}) is True
    assert wb_upload.should_upload(sha, None) is True


def test_upload_conversations_probes_legacy_same_sha_without_resending_content(monkeypatch):
    content = _line({"type": "message", "session_id": "sess-legacy", "role": "user", "content": "legacy"})
    sha = wb_upload.content_sha256(content)
    adapter = _SyntheticAdapter(
        [_session("sess-legacy", "/work/legacy")], {"sess-legacy": content},
    )
    monkeypatch.setattr(wb_upload, "get_known_shas", lambda *args, **kwargs: {
        "sess-legacy": {"sha": sha, "analysis_status": "unknown"},
    })
    posted = []
    monkeypatch.setattr(
        wb_upload,
        "post_transcript",
        lambda server_url, session_id, payload, **kwargs: posted.append(payload) or {"ok": True},
    )

    result = wb_upload.upload_conversations(
        {"service": {"host": "127.0.0.1", "port": 8765}},
        "student-a",
        data_adapter=adapter,
    )

    assert result == {"total": 1, "synced": 1, "skipped": 0, "failed": 0}
    assert posted == [{
        "student_id": "student-a",
        "filtered_content": "",
        "sha": sha,
    }]


def test_upload_conversations_skips_session_when_known_sha_matches(monkeypatch):
    content = _line({"type": "message", "session_id": "sess-1", "role": "user", "content": "hi"})
    sha = wb_upload.content_sha256(content)
    adapter = _SyntheticAdapter(
        [_session("sess-1", "/Users/student/项目")], {"sess-1": content},
    )
    monkeypatch.setattr(wb_upload, "get_known_shas", lambda server_url, student_id, token="", timeout=10.0: {
        "sess-1": {"sha": sha, "analysis_status": "done"},
    })

    posted = []
    monkeypatch.setattr(wb_upload, "post_transcript", lambda *args, **kwargs: posted.append((args, kwargs)))

    result = wb_upload.upload_conversations(
        {"service": {"host": "127.0.0.1", "port": 8765}},
        "student-a",
        data_adapter=adapter,
    )

    assert result == {"total": 1, "synced": 0, "skipped": 1, "failed": 0}
    assert posted == []


def test_requested_upload_probes_same_sha_without_resending_and_counts_skipped(
    monkeypatch,
):
    content = _line({"type": "message", "session_id": "sess-same", "role": "user", "content": "same"})
    sha = wb_upload.content_sha256(content)
    adapter = _SyntheticAdapter(
        [_session("sess-same", "/work/same")], {"sess-same": content},
    )
    monkeypatch.setattr(wb_upload, "get_known_shas", lambda *args, **kwargs: {
        "sess-same": {"sha": sha, "analysis_status": "done"},
    })
    posted = []
    monkeypatch.setattr(
        wb_upload,
        "post_transcript",
        lambda server_url, session_id, payload, **kwargs: posted.append(payload) or {
            "ok": True, "skipped": True,
        },
    )

    result = wb_upload.upload_conversations(
        {"service": {"host": "127.0.0.1", "port": 8765}},
        "student-a",
        request_id="req-1",
        data_adapter=adapter,
    )

    assert result == {"total": 1, "synced": 0, "skipped": 1, "failed": 0}
    assert posted == [{
        "student_id": "student-a",
        "filtered_content": "",
        "sha": sha,
        "request_id": "req-1",
    }]


def test_specific_requested_upload_filters_other_local_sessions(monkeypatch):
    wanted_content = _line({
        "type": "message", "session_id": "wanted", "role": "user", "content": "wanted",
    })
    adapter = _SyntheticAdapter(
        [_session("wanted", "/work/wanted"), _session("other", "/work/other")],
        {"wanted": wanted_content},
    )
    monkeypatch.setattr(wb_upload, "get_known_shas", lambda *args, **kwargs: {})
    posted = []
    monkeypatch.setattr(
        wb_upload, "post_transcript",
        lambda server_url, session_id, payload, **kwargs: posted.append(session_id) or {"ok": True},
    )

    result = wb_upload.upload_conversations(
        {}, "student-a",
        request_id="req-specific", session_id="wanted",
        data_adapter=adapter,
    )

    assert result == {"total": 1, "synced": 1, "skipped": 0, "failed": 0}
    assert posted == ["wanted"]


def test_upload_conversations_posts_same_sha_when_remote_analysis_failed(monkeypatch):
    content = _line({"type": "message", "session_id": "sess-retry", "role": "user", "content": "retry"})
    sha = wb_upload.content_sha256(content)
    adapter = _SyntheticAdapter(
        [_session("sess-retry", "/work/retry")], {"sess-retry": content},
    )
    monkeypatch.setattr(wb_upload, "get_known_shas", lambda *args, **kwargs: {
        "sess-retry": {"sha": sha, "analysis_status": "failed"},
    })
    posted = []
    monkeypatch.setattr(
        wb_upload,
        "post_transcript",
        lambda server_url, session_id, payload, **kwargs: posted.append(payload) or {"ok": True},
    )

    result = wb_upload.upload_conversations(
        {"service": {"host": "127.0.0.1", "port": 8765}},
        "student-a",
        data_adapter=adapter,
    )

    assert result == {"total": 1, "synced": 1, "skipped": 0, "failed": 0}
    assert posted == [{"student_id": "student-a", "filtered_content": content, "sha": sha}]


def test_upload_conversations_posts_filtered_content_and_continues_after_failure(monkeypatch):
    ok_content = _line({"type": "message", "session_id": "sess-ok", "role": "user", "content": "ok"})
    fail_content = _line({"type": "message", "session_id": "sess-fail", "role": "user", "content": "fail"})
    adapter = _SyntheticAdapter(
        [_session("sess-ok", "/work/ok"), _session("sess-fail", "/work/fail")],
        {
            "sess-ok": ok_content + _line({
                "type": "function_call_result", "content": "must not upload",
            }),
            "sess-fail": fail_content,
        },
    )
    monkeypatch.setattr(wb_upload, "get_known_shas", lambda *args, **kwargs: {})

    posts: list[dict] = []

    def fake_post(server_url, session_id, payload, *, token="", timeout=60.0):
        posts.append({"session_id": session_id, "payload": payload, "timeout": timeout})
        if session_id == "sess-fail":
            raise TimeoutError("slow")
        return {"ok": True}

    monkeypatch.setattr(wb_upload, "post_transcript", fake_post)

    result = wb_upload.upload_conversations(
        {"service": {"host": "127.0.0.1", "port": 8765}},
        "student-a",
        data_adapter=adapter,
    )

    assert result == {"total": 2, "synced": 1, "skipped": 0, "failed": 1}
    assert posts[0]["payload"] == {
        "student_id": "student-a",
        "filtered_content": ok_content,
        "sha": wb_upload.content_sha256(ok_content),
    }
    assert "must not upload" not in posts[0]["payload"]["filtered_content"]
    assert posts[0]["timeout"] == 60.0
    assert posts[1]["session_id"] == "sess-fail"


def test_upload_conversations_uses_injected_adapter_without_accessing_home(monkeypatch):
    fixture_content = (
        _line({"type": "message", "session_id": "fixture-session", "content": "fixture"})
        + _line({"type": "reasoning", "text": "must not upload"})
    )
    adapter = _SyntheticAdapter(
        [_session("fixture-session", "/fixture")],
        {"fixture-session": fixture_content},
    )
    monkeypatch.setattr(
        wb_upload.Path,
        "home",
        classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("forbidden HOME access"))),
    )
    monkeypatch.setattr(
        wb_upload,
        "read_sessions",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("forbidden HOME DB access")),
    )
    monkeypatch.setattr(wb_upload, "get_known_shas", lambda *args, **kwargs: {})
    posted = []
    monkeypatch.setattr(
        wb_upload,
        "post_transcript",
        lambda server_url, session_id, payload, **kwargs: posted.append(payload) or {"ok": True},
    )

    result = wb_upload.upload_conversations(
        {"service": {"host": "127.0.0.1", "port": 8765}},
        "student-a",
        data_adapter=adapter,
    )

    assert result == {"total": 1, "synced": 1, "skipped": 0, "failed": 0}
    assert adapter.read_session_ids == ["fixture-session"]
    assert posted == [{
        "student_id": "student-a",
        "filtered_content": _line({
            "type": "message", "session_id": "fixture-session", "content": "fixture",
        }),
        "sha": wb_upload.content_sha256(_line({
            "type": "message", "session_id": "fixture-session", "content": "fixture",
        })),
    }]
