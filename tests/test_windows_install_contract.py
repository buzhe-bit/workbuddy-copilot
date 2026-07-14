"""Textual contracts for the PowerShell W0 probe and cautious installer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


pytestmark = [pytest.mark.contract, pytest.mark.windows, pytest.mark.critical]


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_windows_probe_is_read_only_and_outputs_only_redacted_metadata() -> None:
    script = (PROJECT_ROOT / "probe_windows_workbuddy.ps1").read_text(encoding="utf-8")

    assert "function Redact" in script
    assert "WORKBUDDY_CONFIG_DIR" in script
    assert "$env:ProgramData" in script
    assert "WorkBuddy\\users" in script
    assert "ConvertTo-Json" in script
    assert "Get-Process WorkBuddy" in script
    assert "Get-ScheduledTask" in script
    assert "top_keys" in script
    assert "hook_events" in script
    assert "function Cwd-Shape" in script
    assert "cwd_shape" in script
    assert "cwd_redacted" not in script
    assert "Redact ([string]$line.cwd)" not in script
    assert r"$Value -split '[\\/]'" in script
    assert r"$Value.Contains('\')" in script
    assert r"$Value -match '\s'" in script
    assert "if ($env:SystemDrive) {" in script
    assert "transcript_content" not in script
    assert "Set-Content" not in script
    assert "Add-Content" not in script
    assert "Remove-Item" not in script
    assert "Invoke-WebRequest" not in script
    assert "Invoke-RestMethod" not in script


def test_windows_installer_uses_explicit_variables_and_atomic_settings_backup() -> None:
    script = (PROJECT_ROOT / "install_windows.ps1").read_text(encoding="utf-8")

    for required in (
        "ProjectRoot",
        "ConfigDir",
        "ProfilePath",
        "StudentId",
        "GitBashHookCommand",
        "Start-Process",
        "requirements-windows.txt",
        "-m venv",
        "Move-Item -LiteralPath $temporaryBackup -Destination $backupPath",
        "if ($LASTEXITCODE -ne 0)",
        "COPILOT_SPOOL_DIR",
        "COPILOT_STUDENT_ID",
    ):
        assert required in script
    assert "LOCALAPPDATA\\Programs\\WorkBuddy" not in script
    assert "python3" not in script
    assert "C:\\Users\\" not in script


def test_windows_installer_blocks_before_hook_registration_when_spool_probe_fails() -> None:
    script = (PROJECT_ROOT / "install_windows.ps1").read_text(encoding="utf-8")

    probe_call = "scripts\\windows_spool_preflight.py"
    assert probe_call in script
    assert "spool capability probe failed" in script
    assert script.index(probe_call) < script.index("register_hook.py")


def test_windows_spool_preflight_proves_hardlink_and_shared_byte_lock(
    tmp_path: Path,
) -> None:
    probe = PROJECT_ROOT / "scripts" / "windows_spool_preflight.py"

    completed = subprocess.run(
        [sys.executable, str(probe), "--spool-dir", str(tmp_path)],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "spool capability probe passed" in completed.stdout
    assert list(tmp_path.glob(".copilot-spool-capability-*")) == []


def test_windows_installer_passes_verified_profile_and_runtime_state_explicitly() -> None:
    script = (PROJECT_ROOT / "install_windows.ps1").read_text(encoding="utf-8")

    assert "[Parameter(Mandatory = $true)] [string]$ProfilePath" in script
    assert "Require-File $ProfilePath 'ProfilePath'" in script
    assert "[string]$StateDir = ''" in script
    assert "--platform', 'windows'" in script
    assert "--state-dir', $StateDir" in script
    assert "--workbuddy-config-dir', $ConfigDir" in script
    assert "--workbuddy-profile', $ProfilePath" in script
    assert "$env:COPILOT_WINDOWS_WORKBUDDY_PROFILE = $ProfilePath" in script


def test_windows_installer_never_deletes_or_repairs_claims_silently() -> None:
    script = (PROJECT_ROOT / "install_windows.ps1").read_text(encoding="utf-8")
    lowered = script.lower()

    assert "*.claim" not in lowered
    assert "repair-claim" not in lowered
    assert "claim-path" not in lowered


def test_windows_installer_refuses_to_construct_an_unverified_git_bash_command() -> None:
    script = (PROJECT_ROOT / "install_windows.ps1").read_text(encoding="utf-8")

    assert "GitBashHookCommand" in script
    assert "requires W0-verified Git Bash hook command" in script
    assert "register_hook.py" in script
    assert "--hook-command" not in script


def test_windows_hosted_ci_runs_the_real_non_w1_lane_and_emits_w1_blocked_artifact() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "quality.yml").read_text(
        encoding="utf-8"
    )
    windows_section = workflow.split("  windows-hosted:", 1)[1].split(
        "  windows-w1-evidence:", 1
    )[0]
    w1_section = workflow.split("  windows-w1-evidence:", 1)[1]

    for requirement in (
        "requirements-core.txt",
        "requirements-windows.txt",
        "requirements-server.txt",
        "requirements-dev.txt",
    ):
        assert requirement in windows_section
    assert '-m "windows and not real_machine"' in windows_section
    assert "tests/test_platform_imports.py -q" not in windows_section
    for test_path in (
        "tests/test_platform_imports.py",
        "tests/test_windows_adapter.py",
        "tests/test_windows_install_contract.py",
        "tests/test_windows_liveness.py",
        "tests/test_windows_runtime.py",
        "tests/test_windows_workbuddy_integration.py",
        "tests/test_hook_subprocess.py",
        "tests/component/test_windows_student_runtime.py",
    ):
        assert test_path in windows_section
    for shared_core_path in (
        "tests/test_student_spool.py",
        "tests/test_student_transport.py",
        "tests/test_student_coordinator.py",
        "tests/test_student_agent.py",
        "tests/test_app_context.py",
    ):
        assert shared_core_path in windows_section
    assert "core-pytest-output.txt" in windows_section
    assert "core-quality-summary.json" in windows_section
    assert "$windowsStatus" in windows_section
    assert "$coreStatus" in windows_section
    assert "windows-latest" in windows_section
    assert "w1-evidence.json" in w1_section
    assert "BLOCKED" in w1_section
    assert "real-machine evidence missing" in w1_section
    assert "actions/upload-artifact@v4" in w1_section


def test_windows_hosted_selector_contains_runtime_hook_and_loopback_component() -> None:
    expected_sources = (
        PROJECT_ROOT / "tests" / "test_platform_imports.py",
        PROJECT_ROOT / "tests" / "test_windows_runtime.py",
        PROJECT_ROOT / "tests" / "test_hook_subprocess.py",
        PROJECT_ROOT / "tests" / "component" / "test_windows_student_runtime.py",
    )

    for source in expected_sources:
        assert source.is_file(), f"missing hosted Windows test: {source.relative_to(PROJECT_ROOT)}"
        text = source.read_text(encoding="utf-8")
        assert "pytest.mark.windows" in text
    component = expected_sources[-1].read_text(encoding="utf-8")
    assert "pytest.mark.component" in component
    assert "uvicorn.Server" in component
    assert "subprocess.run" in component
    assert "WindowsStudentRuntime" in component
    assert "EventSpool" in component


def test_register_hook_accepts_an_explicit_windows_config_and_verified_command(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "confirmed-config-dir"
    config_dir.mkdir()
    command = "/verified/python /verified/hook.py || true"
    env = os.environ.copy()
    env.update(
        {
            "WORKBUDDY_CONFIG_DIR": str(config_dir),
            "COPILOT_HOOK_COMMAND": command,
            "COPILOT_STUDENT_ID": "student-9",
            "COPILOT_SPOOL_DIR": str(tmp_path / "spool"),
        }
    )

    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "register_hook.py")],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    settings = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
    for event in ("UserPromptSubmit", "Stop"):
        command_entry = settings["hooks"][event][0]["hooks"][0]
        installed = command_entry["command"]
        assert installed.endswith(command)
        assert "COPILOT_STUDENT_ID=student-9" in installed
        assert f"COPILOT_SPOOL_DIR={tmp_path / 'spool'}" in installed
        assert command_entry["timeout"] == 2


def test_register_hook_rejects_a_verified_command_that_overrides_owned_identity(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "confirmed-config-dir"
    config_dir.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "WORKBUDDY_CONFIG_DIR": str(config_dir),
            "COPILOT_HOOK_COMMAND": (
                "COPILOT_SPOOL_DIR=/wrong /verified/python /verified/hook.py || true"
            ),
            "COPILOT_STUDENT_ID": "student-9",
            "COPILOT_SPOOL_DIR": str(tmp_path / "spool"),
        }
    )

    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "register_hook.py")],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "reserved Copilot environment variable" in completed.stderr
    assert not (config_dir / "settings.json").exists()
