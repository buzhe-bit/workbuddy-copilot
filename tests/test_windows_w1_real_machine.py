"""Stateful Windows W1 checks that must run on the designated pilot PC.

Every input is explicit. Missing hardware/setup evidence is a failure, never a
skip, so a selected W1 run cannot become green through deselection.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest


pytestmark = [
    pytest.mark.windows,
    pytest.mark.real_machine,
    pytest.mark.critical,
]


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    assert value, f"required W1 input is missing: {name}"
    return value


def _json_file(env_name: str) -> tuple[Path, dict[str, object]]:
    path = Path(_env(env_name))
    assert path.is_file(), f"{env_name} is not a file: {path}"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), f"{env_name} must contain a JSON object"
    return path, payload


def _manifest() -> tuple[Path, dict[str, object]]:
    return _json_file("WORKBUDDY_W1_INSTALLER_MANIFEST")


def _hook_commands(settings: object) -> list[str]:
    commands: list[str] = []
    if isinstance(settings, dict):
        command = settings.get("command")
        if isinstance(command, str):
            commands.append(command)
        for value in settings.values():
            commands.extend(_hook_commands(value))
    elif isinstance(settings, list):
        for value in settings:
            commands.extend(_hook_commands(value))
    return commands


def test_w1_is_real_windows_python_313_with_current_owned_manifest() -> None:
    assert sys.platform == "win32"
    assert sys.version_info[:2] == (3, 13)
    manifest_path, manifest = _manifest()
    assert manifest.get("owner_id") == "workbuddy-copilot-v1"
    assert manifest.get("schema_version") == 1
    assert Path(str(manifest["state_dir"])).resolve() == manifest_path.parent.resolve()
    assert Path(str(manifest["project_root"]), "start_windows_client.py").is_file()


def test_w1_workbuddy_profile_and_owned_hook_are_real_git_bash_inputs() -> None:
    _, manifest = _manifest()
    profile = Path(str(manifest["workbuddy_profile"]))
    settings_path = Path(str(manifest["settings_path"]))
    bash = Path(_env("WORKBUDDY_W1_GIT_BASH"))
    assert profile.is_file() and profile.stat().st_size > 0
    assert settings_path.is_file()
    assert bash.is_file()
    version = subprocess.run(
        [str(bash), "--version"], text=True, capture_output=True, check=False, timeout=15
    )
    assert version.returncode == 0, version.stderr
    assert "bash" in version.stdout.lower()
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    owned = [
        command
        for command in _hook_commands(settings)
        if "COPILOT_ENTRY_OWNER=workbuddy-copilot-v1" in command
    ]
    assert len(owned) == 2
    assert all("copilot/hook.py" in command.replace("\\", "/") for command in owned)


def test_w1_owned_scheduled_task_is_running_and_client_health_is_fresh() -> None:
    _, manifest = _manifest()
    task_name = str(manifest["task_name"])
    task = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-ScheduledTask -TaskName $args[0] -ErrorAction Stop).State.ToString()",
            task_name,
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert task.returncode == 0, task.stderr
    assert task.stdout.strip() == "Running"
    project = Path(str(manifest["project_root"]))
    python = Path(str(manifest["venv_dir"])) / "Scripts" / "python.exe"
    health = subprocess.run(
        [
            str(python),
            str(project / "start_windows_client.py"),
            "--config",
            str(manifest["runtime_config_path"]),
            "--health-check",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert health.returncode == 0, health.stderr
    assert json.loads(health.stdout)["healthy"] is True


def test_w1_chinese_user_path_supports_real_atomic_file_io() -> None:
    root = Path(_env("WORKBUDDY_W1_CHINESE_TEST_ROOT"))
    assert root.is_dir()
    assert any(ord(char) > 127 for char in str(root)), "test path must contain Chinese text"
    payload = f"workbuddy-w1-{time.time_ns()}"
    handle, raw = tempfile.mkstemp(prefix="助教-", suffix=".txt", dir=root)
    path = Path(raw)
    try:
        os.close(handle)
        path.write_text(payload, encoding="utf-8")
        assert path.read_text(encoding="utf-8") == payload
    finally:
        path.unlink(missing_ok=True)


def test_w1_install_upgrade_uninstall_reinstall_lifecycle_was_observed() -> None:
    _, payload = _json_file("WORKBUDDY_W1_LIFECYCLE_LOG")
    stages = payload.get("stages")
    assert isinstance(stages, list)
    passed = {
        str(row.get("id"))
        for row in stages
        if isinstance(row, dict) and row.get("status") == "passed"
    }
    assert {"install", "upgrade", "uninstall", "reinstall"} <= passed


def test_w1_student_a_token_cannot_read_student_b_resource() -> None:
    token_path = Path(_env("WORKBUDDY_W1_STUDENT_A_TOKEN_FILE"))
    assert token_path.is_file()
    token = token_path.read_text(encoding="utf-8").strip()
    assert token
    request = urllib.request.Request(
        _env("WORKBUDDY_W1_IDENTITY_CHECK_URL"),
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    assert status in {401, 403, 404}, f"cross-student request unexpectedly returned {status}"


def test_w1_disconnect_recovery_was_durable_and_exactly_once() -> None:
    _, payload = _json_file("WORKBUDDY_W1_RECOVERY_RESULT")
    assert payload.get("offline_event_spooled") is True
    assert payload.get("reconnected") is True
    assert payload.get("delivered_once") is True
    assert payload.get("duplicate_delivery_count") == 0
    assert payload.get("final_spool_pending") == 0
