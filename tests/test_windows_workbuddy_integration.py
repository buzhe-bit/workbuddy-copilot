"""Windows WorkBuddy profile/scanner contracts.

The committed directory is deliberately *synthetic*.  It proves only schema,
mapping and parser behaviour.  Tests tagged ``real_ntfs`` create Windows OS
objects dynamically on a hosted Windows runner; neither group is W0/W1 proof.
"""
from __future__ import annotations

import ctypes
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess

import pytest

from copilot import wb_upload
from copilot.student_platform.windows import (
    LocalWindowsTranscriptBackend,
    WindowsTranscriptEntry,
    WindowsTranscriptScanner,
    WindowsWorkBuddyData,
    load_windows_workbuddy_profile,
    windows_extended_path,
)
from copilot.student_platform.workbuddy import WorkBuddyDataAdapter


pytestmark = [pytest.mark.contract, pytest.mark.windows]

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "workbuddy" / "windows_synthetic"
FIXTURE_MANIFEST = FIXTURE_ROOT / "manifest.json"


def _materialize_fixture(tmp_path: Path) -> Path:
    manifest = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    config_dir = tmp_path / ".workbuddy-synthetic"
    config_dir.mkdir()
    database = sqlite3.connect(config_dir / "workbuddy.db")
    try:
        database.executescript((FIXTURE_ROOT / "schema.sql").read_text(encoding="utf-8"))
        database.executemany(
            """INSERT INTO sessions
               (id, cwd, title, custom_title, created_at, last_activity_at, deleted_at)
               VALUES (:id, :cwd, :title, :custom_title, :created_at,
                       :last_activity_at, :deleted_at)""",
            manifest["sessions"],
        )
        database.executemany(
            """INSERT INTO workspaces (path, name, last_opened_at)
               VALUES (:path, :name, :last_opened_at)""",
            manifest["workspaces"],
        )
        database.commit()
    finally:
        database.close()
    shutil.copytree(FIXTURE_ROOT / "projects", config_dir / "projects")
    return config_dir


def _synthetic_profile(config_dir: Path):
    result = load_windows_workbuddy_profile(
        FIXTURE_MANIFEST,
        config_dir=config_dir,
        allow_synthetic_fixture=True,
    )
    assert result.failure is None
    assert result.profile is not None
    return result.profile


def test_fixture_is_explicitly_synthetic_and_contains_no_personal_identifiers() -> None:
    fixture_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(FIXTURE_ROOT.rglob("*"))
        if path.is_file()
    )
    manifest = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))

    assert manifest["synthetic"] is True
    assert manifest["evidence_level"] == "synthetic"
    assert "<synthetic-runtime-config-dir>" in fixture_text
    assert "/Users/" not in fixture_text
    assert "C:\\Users\\" not in fixture_text
    assert re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", fixture_text) is None
    assert re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", fixture_text) is None


def test_synthetic_profile_is_typed_blocked_for_production_loading(tmp_path: Path) -> None:
    result = load_windows_workbuddy_profile(FIXTURE_MANIFEST, config_dir=tmp_path)

    assert result.profile is None
    assert result.failure is not None
    assert result.failure.code == "windows_profile_synthetic"


def test_fixture_profile_can_only_be_opened_through_explicit_synthetic_seam(
    tmp_path: Path,
) -> None:
    profile = _synthetic_profile(tmp_path)

    assert profile.synthetic is True
    assert profile.evidence_level == "synthetic"
    assert profile.projects_relative_path == "projects"
    assert profile.database_relative_path == "workbuddy.db"
    assert profile.session_metadata_keys == ("session_id", "sessionId")


@pytest.mark.parametrize(
    ("patch", "expected_code"),
    [
        ({"schema_version": 2}, "windows_profile_invalid"),
        ({"hook_command": ""}, "windows_profile_invalid"),
        ({"evidence_level": "W0", "synthetic": True}, "windows_profile_invalid"),
        ({"synthetic": False, "evidence_level": "draft"}, "windows_profile_not_w0"),
        ({"transcript_mapping": {"projects_relative_path": "projects"}}, "windows_profile_invalid"),
        ({"transcript_mapping": {"projects_relative_path": "../escape", "database_relative_path": "workbuddy.db", "session_metadata_keys": ["session_id", "sessionId"]}}, "windows_profile_invalid"),
        ({"transcript_mapping": {"projects_relative_path": "C:\\escape", "database_relative_path": "workbuddy.db", "session_metadata_keys": ["session_id", "sessionId"]}}, "windows_profile_invalid"),
        ({"transcript_mapping": {"projects_relative_path": "projects/.. /escape", "database_relative_path": "workbuddy.db", "session_metadata_keys": ["session_id", "sessionId"]}}, "windows_profile_invalid"),
    ],
)
def test_invalid_or_escaping_profile_is_typed_blocked(
    tmp_path: Path, patch: dict[str, object], expected_code: str
) -> None:
    raw = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    raw.update(patch)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    result = load_windows_workbuddy_profile(
        path,
        config_dir=tmp_path,
        allow_synthetic_fixture=True,
    )

    assert result.profile is None
    assert result.failure is not None
    assert result.failure.code == expected_code


def test_non_synthetic_w0_profile_must_match_explicit_config_dir(tmp_path: Path) -> None:
    raw = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    raw.update(
        {
            "synthetic": False,
            "evidence_level": "W0",
            "config_dir": str(tmp_path / "observed-config"),
        }
    )
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    result = load_windows_workbuddy_profile(path, config_dir=tmp_path / "different-config")

    assert result.profile is None
    assert result.failure is not None
    assert result.failure.code == "windows_profile_mismatch"


def test_windows_data_without_explicit_w0_profile_blocks_transcripts(tmp_path: Path) -> None:
    adapter = WindowsWorkBuddyData(tmp_path / "config")

    result = adapter.read_transcript("any-session")

    assert result.failure is not None
    assert result.failure.code == "windows_profile_required"


def test_windows_data_rejects_direct_synthetic_profile_even_when_parsed(
    tmp_path: Path,
) -> None:
    config_dir = _materialize_fixture(tmp_path)
    profile = _synthetic_profile(config_dir)
    adapter = WindowsWorkBuddyData(config_dir, profile=profile)

    result = adapter.read_transcript("synthetic-session-space")

    assert result.failure is not None
    assert result.failure.code == "windows_profile_synthetic"


def test_windows_data_revalidates_direct_profile_mapping_before_joining_paths(
    tmp_path: Path,
) -> None:
    config_dir = _materialize_fixture(tmp_path)
    parsed = _synthetic_profile(config_dir)
    forged = replace(
        parsed,
        synthetic=False,
        evidence_level="W0",
        projects_relative_path="../outside",
    )
    adapter = WindowsWorkBuddyData(config_dir, profile=forged)

    result = adapter.read_transcript("synthetic-session-space")

    assert result.failure is not None
    assert result.failure.code == "windows_profile_invalid"


def test_windows_scanner_reads_synthetic_schema_unicode_and_both_session_keys(
    tmp_path: Path,
) -> None:
    config_dir = _materialize_fixture(tmp_path)
    profile = _synthetic_profile(config_dir)
    adapter = WorkBuddyDataAdapter(
        config_dir,
        database_path=config_dir / profile.database_relative_path,
        projects_dir=config_dir / profile.projects_relative_path,
        transcript_scanner=WindowsTranscriptScanner(),
    )

    sessions = adapter.list_sessions()
    snake = adapter.read_transcript("synthetic-session-space")
    camel = adapter.read_transcript("synthetic-session-task")

    assert sessions[0].title == "Unicode 标题 ✓"
    assert sessions[0].group_type == "space"
    assert sessions[0].space_name == "合成空间 🧪"
    assert snake.failure is None
    assert "Unicode 路径" in snake.content
    assert camel.failure is None
    assert "camelCase" in camel.content


def test_windows_scanner_refreshes_successful_snapshot_for_each_read(
    tmp_path: Path,
) -> None:
    config_dir = _materialize_fixture(tmp_path)
    adapter = WorkBuddyDataAdapter(
        config_dir,
        transcript_scanner=WindowsTranscriptScanner(),
    )
    transcript = config_dir / "projects" / "学习空间" / "space-session.jsonl"

    first = adapter.read_transcript("synthetic-session-space")
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "message",
                    "session_id": "synthetic-session-space",
                    "content": "SECOND STOP MUST SEE THIS",
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    second = adapter.read_transcript("synthetic-session-space")

    assert first.failure is None
    assert "SECOND STOP MUST SEE THIS" not in first.content
    assert second.failure is None
    assert "SECOND STOP MUST SEE THIS" in second.content


def test_windows_scanner_discovers_session_created_after_an_earlier_read(
    tmp_path: Path,
) -> None:
    config_dir = _materialize_fixture(tmp_path)
    adapter = WorkBuddyDataAdapter(
        config_dir,
        transcript_scanner=WindowsTranscriptScanner(),
    )

    missing = adapter.read_transcript("session-created-later")
    (config_dir / "projects" / "created-later.jsonl").write_text(
        json.dumps(
            {
                "type": "message",
                "session_id": "session-created-later",
                "content": "new session",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    discovered = adapter.read_transcript("session-created-later")

    assert missing.failure is not None
    assert missing.failure.code == "transcript_not_found"
    assert discovered.failure is None
    assert "new session" in discovered.content


class _CountingSnapshotBackend(LocalWindowsTranscriptBackend):
    def __init__(self, projects_root: Path) -> None:
        self.projects_root = projects_root
        self.root_scans = 0

    def iter_entries(self, directory: Path) -> tuple[WindowsTranscriptEntry, ...]:
        if directory == self.projects_root:
            self.root_scans += 1
        return super().iter_entries(directory)


def test_one_windows_bulk_upload_reuses_one_snapshot_then_next_read_refreshes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = _materialize_fixture(tmp_path)
    projects_root = config_dir / "projects"
    backend = _CountingSnapshotBackend(projects_root)
    adapter = WorkBuddyDataAdapter(
        config_dir,
        transcript_scanner=WindowsTranscriptScanner(backend=backend),
    )
    monkeypatch.setattr(
        wb_upload,
        "post_transcript",
        lambda _url, session_id, payload, **_kwargs: {
            "ok": True,
            "session_id": session_id,
            "sha": payload["sha"],
        },
    )

    result = wb_upload.upload_conversations(
        {"service": {"host": "127.0.0.1", "port": 8765}},
        "student-a",
        mode="full",
        data_adapter=adapter,
    )

    assert result.complete is True
    assert result.attempted == 2
    assert backend.root_scans == 1

    transcript = projects_root / "学习空间" / "space-session.jsonl"
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "message",
                    "session_id": "synthetic-session-space",
                    "content": "fresh after bulk snapshot",
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    refreshed = adapter.read_transcript("synthetic-session-space")

    assert refreshed.failure is None
    assert "fresh after bulk snapshot" in refreshed.content
    assert backend.root_scans == 2


def test_windows_scanner_recovers_after_an_in_progress_jsonl_becomes_valid(
    tmp_path: Path,
) -> None:
    config_dir = _materialize_fixture(tmp_path)
    transcript = config_dir / "projects" / "writing-now.jsonl"
    transcript.write_text('{"type":"message","session_id":"writing-now"', encoding="utf-8")
    adapter = WorkBuddyDataAdapter(
        config_dir,
        transcript_scanner=WindowsTranscriptScanner(),
    )

    incomplete = adapter.read_transcript("writing-now")
    transcript.write_text(
        json.dumps(
            {
                "type": "message",
                "session_id": "writing-now",
                "content": "write completed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    recovered = adapter.read_transcript("writing-now")

    assert incomplete.failure is not None
    assert incomplete.failure.code == "transcript_index_incomplete"
    assert recovered.failure is None
    assert "write completed" in recovered.content


def test_windows_scanner_returns_typed_zero_and_multiple_mapping_results(
    tmp_path: Path,
) -> None:
    config_dir = _materialize_fixture(tmp_path)
    duplicate = config_dir / "projects" / "duplicate.jsonl"
    duplicate.write_text(
        json.dumps(
            {
                "type": "message",
                "sessionId": "synthetic-session-task",
                "content": "duplicate",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    adapter = WorkBuddyDataAdapter(
        config_dir,
        transcript_scanner=WindowsTranscriptScanner(),
    )

    missing = adapter.read_transcript("missing-session")
    ambiguous = adapter.read_transcript("synthetic-session-task")

    assert missing.failure is not None
    assert missing.failure.code == "transcript_not_found"
    assert ambiguous.failure is not None
    assert ambiguous.failure.code == "transcript_ambiguous"


class _BusyOnceBackend(LocalWindowsTranscriptBackend):
    def __init__(self) -> None:
        self.calls = 0

    def read_bytes(self, path: Path, limit: int) -> bytes:
        self.calls += 1
        if self.calls == 1:
            error = OSError("synthetic sharing violation")
            error.winerror = 32  # type: ignore[attr-defined]
            raise error
        return super().read_bytes(path, limit)


def test_sharing_violation_is_retryable_busy_and_is_not_cached(tmp_path: Path) -> None:
    config_dir = _materialize_fixture(tmp_path)
    backend = _BusyOnceBackend()
    adapter = WorkBuddyDataAdapter(
        config_dir,
        transcript_scanner=WindowsTranscriptScanner(backend=backend),
    )

    first = adapter.read_transcript("synthetic-session-space")
    second = adapter.read_transcript("synthetic-session-space")

    assert first.failure is not None
    assert first.failure.code == "busy"
    assert second.failure is None
    assert backend.calls >= 2


class _SecureTraversalSeamBackend(LocalWindowsTranscriptBackend):
    def __init__(self) -> None:
        self.secure_directory_calls = 0
        self.secure_file_calls = 0

    def iter_entries(self, directory: Path) -> tuple[WindowsTranscriptEntry, ...]:
        raise AssertionError("scanner bypassed the handle-pinned directory seam")

    def iter_entries_secure(
        self,
        root: Path,
        directory: Path,
    ) -> tuple[WindowsTranscriptEntry, ...]:
        self.secure_directory_calls += 1
        return LocalWindowsTranscriptBackend.iter_entries(self, directory)

    def read_bytes(self, path: Path, limit: int) -> bytes:
        raise AssertionError("scanner bypassed the handle-pinned file seam")

    def read_bytes_secure(self, root: Path, path: Path, limit: int) -> bytes:
        self.secure_file_calls += 1
        return LocalWindowsTranscriptBackend.read_bytes(self, path, limit)


def test_scanner_uses_handle_pinned_backend_seams_for_directory_and_file(
    tmp_path: Path,
) -> None:
    config_dir = _materialize_fixture(tmp_path)
    backend = _SecureTraversalSeamBackend()
    adapter = WorkBuddyDataAdapter(
        config_dir,
        transcript_scanner=WindowsTranscriptScanner(backend=backend),
    )

    result = adapter.read_transcript("synthetic-session-space")

    assert result.failure is None
    assert backend.secure_directory_calls >= 1
    assert backend.secure_file_calls >= 1


def test_production_windows_backend_pins_and_validates_win32_handles() -> None:
    source = (Path(__file__).resolve().parents[1] / "copilot/student_platform/windows.py").read_text(
        encoding="utf-8"
    )

    for required in (
        "CreateFileW",
        "FILE_FLAG_OPEN_REPARSE_POINT",
        "GetFinalPathNameByHandleW",
        "GetFileInformationByHandle",
        "ReadFile",
    ):
        assert required in source


class _ReparseAndEscapeBackend(LocalWindowsTranscriptBackend):
    def __init__(self, *, escape: Path | None = None) -> None:
        self.escape = escape

    def iter_entries(self, directory: Path) -> tuple[WindowsTranscriptEntry, ...]:
        entries = super().iter_entries(directory)
        if self.escape is not None:
            return entries + (
                WindowsTranscriptEntry(
                    name="escape.jsonl",
                    path=self.escape,
                    is_directory=False,
                    is_file=True,
                    is_reparse_point=False,
                ),
            )
        return tuple(
            WindowsTranscriptEntry(
                name=entry.name,
                path=entry.path,
                is_directory=entry.is_directory,
                is_file=entry.is_file,
                is_reparse_point=entry.name == "学习空间" or entry.is_reparse_point,
            )
            for entry in entries
        )


class _LateReparseBackend(LocalWindowsTranscriptBackend):
    def is_reparse_point(self, path: Path) -> bool:
        return path.name == "独立任务" or super().is_reparse_point(path)


class _LateFileReparseBackend(LocalWindowsTranscriptBackend):
    def is_reparse_point(self, path: Path) -> bool:
        return path.name == "task-session.jsonl" or super().is_reparse_point(path)


def test_reparse_directory_is_not_followed_by_injected_backend(tmp_path: Path) -> None:
    config_dir = _materialize_fixture(tmp_path)
    adapter = WorkBuddyDataAdapter(
        config_dir,
        transcript_scanner=WindowsTranscriptScanner(
            backend=_ReparseAndEscapeBackend()
        ),
    )

    blocked = adapter.read_transcript("synthetic-session-space")
    ordinary = adapter.read_transcript("synthetic-session-task")

    assert blocked.failure is not None
    assert blocked.failure.code == "transcript_not_found"
    assert ordinary.failure is None


def test_directory_is_rechecked_for_reparse_before_each_traversal(tmp_path: Path) -> None:
    config_dir = _materialize_fixture(tmp_path)
    adapter = WorkBuddyDataAdapter(
        config_dir,
        transcript_scanner=WindowsTranscriptScanner(backend=_LateReparseBackend()),
    )

    result = adapter.read_transcript("synthetic-session-task")

    assert result.failure is not None
    assert result.failure.code == "transcript_not_found"


def test_file_is_rechecked_for_reparse_immediately_before_read(tmp_path: Path) -> None:
    config_dir = _materialize_fixture(tmp_path)
    adapter = WorkBuddyDataAdapter(
        config_dir,
        transcript_scanner=WindowsTranscriptScanner(
            backend=_LateFileReparseBackend()
        ),
    )

    result = adapter.read_transcript("synthetic-session-task")

    assert result.failure is not None
    assert result.failure.code == "transcript_not_found"


def test_backend_entry_outside_projects_root_fails_closed(tmp_path: Path) -> None:
    config_dir = _materialize_fixture(tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_text(
        '{"type":"message","session_id":"outside"}\n', encoding="utf-8"
    )
    adapter = WorkBuddyDataAdapter(
        config_dir,
        transcript_scanner=WindowsTranscriptScanner(
            backend=_ReparseAndEscapeBackend(escape=outside)
        ),
    )

    result = adapter.read_transcript("synthetic-session-space")

    assert result.failure is not None
    assert result.failure.code == "transcript_index_incomplete"


def test_unc_and_extended_path_normalization_is_stable() -> None:
    assert windows_extended_path(r"\\server\share\资料") == r"\\?\UNC\server\share\资料"
    assert windows_extended_path(r"C:\很长\路径") == r"\\?\C:\很长\路径"
    assert windows_extended_path(r"\\?\C:\already") == r"\\?\C:\already"
    assert windows_extended_path("relative/path") == "relative/path"


@pytest.mark.skipif(os.name != "nt", reason="requires hosted Windows NTFS")
def test_hosted_windows_dynamic_junction_is_not_followed(tmp_path: Path) -> None:
    config_dir = _materialize_fixture(tmp_path)
    target = tmp_path / "outside-target"
    target.mkdir()
    (target / "outside.jsonl").write_text(
        '{"type":"message","session_id":"junction-escape"}\n', encoding="utf-8"
    )
    junction = config_dir / "projects" / "junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    adapter = WorkBuddyDataAdapter(
        config_dir,
        transcript_scanner=WindowsTranscriptScanner(),
    )

    result = adapter.read_transcript("junction-escape")

    assert result.failure is not None
    assert result.failure.code == "transcript_not_found"


@pytest.mark.skipif(os.name != "nt", reason="requires hosted Windows NTFS")
def test_hosted_windows_sharing_violation_is_busy(tmp_path: Path) -> None:
    from ctypes import wintypes

    config_dir = _materialize_fixture(tmp_path)
    locked_path = config_dir / "projects" / "locked.jsonl"
    locked_path.write_text(
        '{"type":"message","session_id":"locked"}\n', encoding="utf-8"
    )
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(str(locked_path), 0x80000000, 0, None, 3, 0x80, None)
    assert handle not in (None, 0, ctypes.c_void_p(-1).value)
    try:
        adapter = WorkBuddyDataAdapter(
            config_dir,
            transcript_scanner=WindowsTranscriptScanner(),
        )
        result = adapter.read_transcript("locked")
    finally:
        close_handle = ctypes.windll.kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        assert close_handle(handle)

    assert result.failure is not None
    assert result.failure.code == "busy"


@pytest.mark.skipif(os.name != "nt", reason="requires hosted Windows NTFS")
def test_hosted_windows_unicode_long_path_is_readable(tmp_path: Path) -> None:
    config_dir = _materialize_fixture(tmp_path)
    long_directory = config_dir / "projects"
    for index in range(8):
        long_directory /= f"长路径-{index}-" + ("字" * 28)
    os.makedirs(windows_extended_path(str(long_directory)), exist_ok=True)
    transcript = long_directory / "unicode.jsonl"
    with open(windows_extended_path(str(transcript)), "w", encoding="utf-8") as handle:
        handle.write('{"type":"message","sessionId":"long-unicode"}\n')
    adapter = WorkBuddyDataAdapter(
        config_dir,
        transcript_scanner=WindowsTranscriptScanner(),
    )

    result = adapter.read_transcript("long-unicode")

    assert result.failure is None
