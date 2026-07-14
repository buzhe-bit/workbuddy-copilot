"""Contract tests for the deliberately narrow Windows WorkBuddy boundary."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from copilot.student_platform.windows import (
    WindowsWorkBuddyData,
    WindowsWorkBuddyProbe,
    probe_windows_config_dir,
)


pytestmark = [pytest.mark.contract, pytest.mark.windows, pytest.mark.critical]


def test_windows_config_dir_prefers_explicit_workbuddy_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    explicit_root = tmp_path / "explicit-workbuddy"
    monkeypatch.setenv("WORKBUDDY_CONFIG_DIR", str(explicit_root))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "other-user"))

    candidate = probe_windows_config_dir()

    assert candidate.path == explicit_root
    assert candidate.source == "WORKBUDDY_CONFIG_DIR"
    assert candidate.exists is False


def test_windows_config_dir_uses_only_existing_official_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("WORKBUDDY_CONFIG_DIR", raising=False)
    userprofile = tmp_path / "user"
    programdata = tmp_path / "ProgramData"
    fallback = programdata / "WorkBuddy" / "users" / "opaque-hash" / ".workbuddy"
    fallback.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(userprofile))
    monkeypatch.setenv("ProgramData", str(programdata))

    candidate = probe_windows_config_dir()

    assert candidate.path == fallback
    assert candidate.source == "ProgramData/WorkBuddy/users"


def test_windows_config_dir_does_not_invent_a_missing_default_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("WORKBUDDY_CONFIG_DIR", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "missing-user"))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "missing-programdata"))

    candidate = probe_windows_config_dir()

    assert candidate.path is None
    assert candidate.source is None


def test_missing_real_machine_manifest_is_explicitly_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WORKBUDDY_CONFIG_DIR", str(tmp_path / "config"))
    missing_manifest = tmp_path / "fixtures" / "workbuddy" / "windows" / "manifest.json"

    result = WindowsWorkBuddyProbe(manifest_path=missing_manifest).probe()

    assert result.status == "blocked"
    assert result.message == "real-machine evidence missing"
    assert result.verdict == "BLOCKED: real-machine evidence missing"
    assert result.config.path == tmp_path / "config"
    assert result.rollout_ready is False


def test_only_a_w0_manifest_with_required_evidence_can_be_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("WORKBUDDY_CONFIG_DIR", str(config_dir))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "synthetic": False,
                "evidence_level": "W0",
                "workbuddy_version": "5.1.2",
                "config_dir": str(config_dir),
                "hook_command": "verified-on-real-machine",
                "transcript_mapping": {
                    "database_relative_path": "workbuddy.db",
                    "projects_relative_path": "projects",
                    "session_metadata_keys": ["session_id", "sessionId"],
                },
            }
        ),
        encoding="utf-8",
    )

    result = WindowsWorkBuddyProbe(manifest_path=manifest).probe()

    assert result.status == "ready"
    assert result.rollout_ready is False
    assert result.config.path == config_dir


def test_legacy_four_string_manifest_cannot_claim_w0_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("WORKBUDDY_CONFIG_DIR", str(config_dir))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "workbuddy_version": "5.1.2",
                "config_dir": str(config_dir),
                "hook_command": "unverified-string",
                "transcript_mapping": "unverified-string",
            }
        ),
        encoding="utf-8",
    )

    result = WindowsWorkBuddyProbe(manifest_path=manifest).probe()

    assert result.status == "blocked"
    assert result.message == "real-machine evidence incomplete"


def test_complete_manifest_cannot_make_a_missing_explicit_config_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing_config = tmp_path / "missing-explicit-config"
    monkeypatch.setenv("WORKBUDDY_CONFIG_DIR", str(missing_config))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "synthetic": False,
                "evidence_level": "W0",
                "workbuddy_version": "5.1.2",
                "config_dir": str(missing_config),
                "hook_command": "verified-on-real-machine",
                "transcript_mapping": {
                    "database_relative_path": "workbuddy.db",
                    "projects_relative_path": "projects",
                    "session_metadata_keys": ["session_id", "sessionId"],
                },
            }
        ),
        encoding="utf-8",
    )

    result = WindowsWorkBuddyProbe(manifest_path=manifest).probe()

    assert result.status == "blocked"
    assert result.message == "WorkBuddy config directory is missing"
    assert result.config.path == missing_config
    assert result.config.exists is False


def test_windows_config_dir_enumerates_existing_workbuddy_env_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("WORKBUDDY_CONFIG_DIR", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "missing-user"))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "missing-programdata"))
    fallback = tmp_path / "WorkBuddy-env" / "opaque-hash" / ".workbuddy"
    fallback.mkdir(parents=True)
    monkeypatch.setenv("SystemDrive", str(tmp_path))

    candidate = probe_windows_config_dir()

    assert candidate.path == fallback
    assert candidate.source == "SystemDrive/WorkBuddy-env"
    assert candidate.exists is True


def test_invalid_or_partial_manifest_stays_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("WORKBUDDY_CONFIG_DIR", str(config_dir))
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"workbuddy_version":"5.1.2"}', encoding="utf-8")

    result = WindowsWorkBuddyProbe(manifest_path=manifest).probe()

    assert result.status == "blocked"
    assert result.message == "real-machine evidence incomplete"
    assert result.rollout_ready is False


def test_windows_data_adapter_uses_explicit_config_and_never_guesses_cwd_encoding(
    tmp_path: Path
) -> None:
    adapter = WindowsWorkBuddyData(tmp_path / "config")
    source = Path(adapter.__class__.__module__.replace(".", "/") + ".py")
    module_source = (Path(__file__).resolve().parents[1] / source).read_text(encoding="utf-8")

    assert adapter.config_dir == tmp_path / "config"
    assert "encode_cwd" not in module_source
    assert "compressedCwd" not in module_source
