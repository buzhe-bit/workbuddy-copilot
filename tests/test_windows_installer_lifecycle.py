from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

import pytest


pytestmark = [pytest.mark.windows]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OWNER_ID = "workbuddy-copilot-v1"


def _source(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _windows_powershell_51_env() -> dict[str, str]:
    """Build the module path that powershell.exe 5.1 expects on Windows."""

    env = os.environ.copy()
    program_files = Path(env.get("ProgramFiles", r"C:\Program Files"))
    windows_dir = Path(env.get("WINDIR", r"C:\Windows"))
    module_paths = []
    if env.get("USERPROFILE"):
        module_paths.append(
            Path(env["USERPROFILE"]) / "Documents" / "WindowsPowerShell" / "Modules"
        )
    module_paths.extend(
        (
            program_files / "WindowsPowerShell" / "Modules",
            windows_dir / "System32" / "WindowsPowerShell" / "v1.0" / "Modules",
        )
    )
    env["PSModulePath"] = os.pathsep.join(str(path) for path in module_paths)
    return env


def _protect_private_windows_directory(path: Path) -> None:
    script = r"""
$ErrorActionPreference = 'Stop'
$path = $args[0]
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = Get-Acl -LiteralPath $path
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleAll($rule) }
$acl.SetOwner($sid)
$inheritance = [System.Security.AccessControl.InheritanceFlags]::None
if ((Get-Item -LiteralPath $path).PSIsContainer) {
    $inheritance = ([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                    [System.Security.AccessControl.InheritanceFlags]::ObjectInherit)
}
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $sid,
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    $inheritance,
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow
)
$acl.SetAccessRule($rule)
Set-Acl -LiteralPath $path -AclObject $acl
"""
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
        env=_windows_powershell_51_env(),
    )
    assert completed.returncode == 0, completed.stderr


def test_installer_pins_and_rechecks_python_313() -> None:
    source = _source("install_windows.ps1")

    assert "-3.13" in source
    assert "sys.version_info[:2] == (3, 13)" in source
    assert "-3 -m venv" not in source
    assert "Python 3.13" in source
    state_write = source.index("New-Item -ItemType Directory -Force -Path $StateDir")
    python_preflight = source.index(
        "Invoke-Python313 @('-c', 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 13)')"
    )
    assert python_preflight < state_write


@pytest.mark.parametrize("script_name", ("install_windows.ps1", "uninstall_windows.ps1"))
def test_windows_scripts_support_powershell_51_path_and_security_modules(
    script_name: str,
) -> None:
    source = _source(script_name)

    assert "[System.IO.Path]::IsPathFullyQualified" not in source
    assert "function Test-FullyQualifiedPath" in source
    assert "Join-Path $PSHOME 'Modules'" in source
    assert "Import-Module Microsoft.PowerShell.Security -ErrorAction Stop" in source


def test_installer_accepts_only_secure_interactive_or_private_token_file() -> None:
    source = _source("install_windows.ps1")
    parameter_block = source.split("param(", 1)[1].split(")\n\n", 1)[0]

    assert "[string]$TokenFile" in parameter_block
    assert not re.search(r"\[string\]\s*\$Token(?:\s|,|=)", parameter_block)
    assert "Read-Host" in source
    assert "-AsSecureString" in source
    assert "Assert-PrivateAcl" in source
    assert source.index("Assert-PrivateAcl $TokenFile") < source.index(
        "ReadAllText($TokenFile"
    )
    assert "Write-Output $token" not in source
    assert "Write-Host $token" not in source
    state_write = source.index("New-Item -ItemType Directory -Force -Path $StateDir")
    assert source.rindex("Assert-PrivateAcl $TokenFile") < state_write


def test_installer_protects_every_sensitive_runtime_artifact_fail_closed() -> None:
    source = _source("install_windows.ps1")

    for artifact in (
        "$StateDir",
        "$LogDir",
        "$SpoolDir",
        "$runtimeConfigPath",
        "$runtimeTokenPath",
        "$manifestPath",
        "$baselineBackupPath",
    ):
        assert f"Protect-PrivatePath {artifact}" in source
        assert f"Assert-PrivateAcl {artifact}" in source
    assert "SetAccessRuleProtection($true, $false)" in source
    assert "WindowsIdentity]::GetCurrent().User" in source
    assert "AreAccessRulesProtected" in source
    assert "AccessControlType]::Allow" in source


def test_installer_uses_atomic_owned_merge_and_private_state_manifest() -> None:
    source = _source("install_windows.ps1")

    assert OWNER_ID in source
    assert "COPILOT_ENTRY_OWNER" in source
    assert "installer-manifest.json" in source
    assert "settings_before_sha256" in source
    assert "settings_after_sha256" in source
    assert "baseline_backup_sha256" in source
    assert "Write-JsonAtomically" in source
    assert "Move-Item -LiteralPath $temporary" in source
    assert source.index("Write-JsonAtomically") < source.index("Register-ScheduledTask")


def test_installer_registers_interactive_current_user_task_with_restart_policy() -> None:
    source = _source("install_windows.ps1")

    assert "New-ScheduledTaskPrincipal" in source
    assert "-LogonType Interactive" in source
    assert "New-ScheduledTaskTrigger -AtLogOn" in source
    assert "RestartCount" in source
    assert "RestartInterval" in source
    assert "Register-ScheduledTask" in source
    assert "-Force" in source
    assert "start_windows_client.py" in source
    assert "--config" in source
    action_section = source.split("New-ScheduledTaskAction", 1)[1].split(
        "Register-ScheduledTask", 1
    )[0]
    assert "--token" not in action_section
    assert "$token" not in action_section
    assert source.index("Stop-ScheduledTask") < source.index("Start-ScheduledTask")
    assert "Start-Process -FilePath $venvPython" not in source


def test_installer_requires_explicit_owned_paths_and_rejects_reparse_escape() -> None:
    source = _source("install_windows.ps1")

    assert "StateDir and LogDir are required for a normal installation" in source
    assert "Assert-NoReparsePoint $StateDir" in source
    assert "Assert-NoReparsePoint $LogDir" in source
    assert "Assert-NoReparsePoint $SpoolDir" in source
    assert "Assert-PathInside $LogDir $StateDir" in source
    assert "Assert-PathInside $SpoolDir $StateDir" in source
    assert "[System.IO.Directory]::GetParent($current)" in source
    for name in ("ProjectRoot", "ConfigDir", "ProfilePath", "StateDir", "LogDir"):
        assert f"Assert-AbsolutePath ${name} '{name}'" in source
    for path in ("$ProjectRoot", "$ConfigDir", "$StateDir", "$LogDir", "$SpoolDir"):
        assert f"Assert-NoReparsePointChain {path}" in source
    assert source.index("Assert-NoReparsePointChain $StateDir") < source.index(
        "New-Item -ItemType Directory -Force -Path $StateDir"
    )


def test_upgrade_stages_runtime_then_stops_owned_task_before_atomic_venv_swap() -> None:
    source = _source("install_windows.ps1")

    assert ".venv-win.staging-" in source
    assert ".venv-win.rollback-" in source
    assert "Invoke-Python313 @('-m', 'venv', $venvStagingDir)" in source
    assert "Move-Item -LiteralPath $venvStagingDir -Destination $venvDir" in source
    assert "Restore-PreviousVenv" in source
    stop = source.index("Stop-ScheduledTask -TaskName $taskName")
    swap = source.index("Move-Item -LiteralPath $venvStagingDir -Destination $venvDir")
    assert stop < swap


def test_upgrade_keeps_versioned_baseline_until_manifest_switch_succeeds() -> None:
    source = _source("install_windows.ps1")

    assert "$installId = [Guid]::NewGuid().ToString('N')" in source
    assert "settings-baseline-$installId.json" in source
    assert "install_id = $installId" in source
    assert "Write-JsonAtomically $manifestPath $manifest" in source
    assert source.index("Start-ScheduledTask -TaskName $taskName") < source.index(
        "Write-JsonAtomically $manifestPath $manifest"
    )


def test_failed_upgrade_restores_settings_task_definition_and_first_install_task() -> None:
    source = _source("install_windows.ps1")

    assert "settings-rollback-$installId.json" in source
    assert "Restore-SettingsAtomically $settingsRollbackPath $settingsPath" in source
    assert "Export-ScheduledTask -TaskName $taskName" in source
    assert "Register-ScheduledTask -TaskName $taskName -Xml $previousTaskXml -Force" in source
    assert "Unregister-ScheduledTask -TaskName $taskName -Confirm:$false" in source
    assert "$hadPreviousTask" in source
    assert source.index("Export-ScheduledTask -TaskName $taskName") < source.index(
        "Register-ScheduledTask -TaskName $taskName -Action $action"
    )


def test_uninstaller_stops_running_task_before_unregistering_or_deleting_venv() -> None:
    source = _source("uninstall_windows.ps1")

    stop = source.index("Stop-ScheduledTask -TaskName $expectedTaskName")
    unregister = source.index("Unregister-ScheduledTask -TaskName $expectedTaskName")
    remove_venv = source.index("Remove-Item -LiteralPath $manifestVenv")
    assert stop < unregister < remove_venv
    assert "owned Windows client task did not stop during uninstall" in source


def test_uninstaller_is_manifest_scoped_and_hash_safe() -> None:
    source = _source("uninstall_windows.ps1")

    assert "installer-manifest.json" in source
    assert OWNER_ID in source
    assert "settings_after_sha256" in source
    assert "baseline_backup_sha256" in source
    assert "Unregister-ScheduledTask" in source
    assert "Remove-OwnedHookEntries" in source
    assert "Restore-SettingsAtomically" in source
    assert "Remove-Item -LiteralPath $manifestVenv" in source
    assert "settings hash changed" in source
    assert "Remove-Item -LiteralPath $ConfigDir" not in source
    assert "Remove-Item -LiteralPath $StateDir" not in source
    assert "Assert-PathInside $settingsPath $configDir" in source
    assert "Assert-PathInside $baselineBackupPath $stateDir" in source
    assert "expectedTaskName" in source
    for path in ("$projectRoot", "$configDir", "$stateDir", "$baselineBackupPath"):
        assert f"Assert-NoReparsePointChain {path}" in source


def test_production_wheel_declares_windows_evidence_schema_package_data() -> None:
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["tool"]["setuptools"]["package-data"][
        "copilot.student_platform"
    ] == ["windows_evidence.schema.json"]


def test_register_hook_owned_entry_upgrade_is_idempotent_and_atomic(tmp_path: Path) -> None:
    config_dir = tmp_path / "workbuddy"
    config_dir.mkdir()
    settings = {
        "theme": "dark",
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": "unrelated", "timeout": 9}
                    ]
                }
            ]
        },
    }
    (config_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "WORKBUDDY_CONFIG_DIR": str(config_dir),
            "COPILOT_HOOK_COMMAND": "/verified/python /verified/copilot/hook.py || true",
            "COPILOT_STUDENT_ID": "student-a",
            "COPILOT_SPOOL_DIR": str(tmp_path / "spool"),
            "COPILOT_ENTRY_OWNER": OWNER_ID,
        }
    )

    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "register_hook.py")],
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

    merged = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
    for event in ("UserPromptSubmit", "Stop"):
        commands = [
            hook["command"]
            for block in merged["hooks"][event]
            for hook in block.get("hooks", [])
        ]
        assert sum(f"COPILOT_ENTRY_OWNER={OWNER_ID}" in item for item in commands) == 1
    stop_commands = [
        hook["command"]
        for block in merged["hooks"]["Stop"]
        for hook in block.get("hooks", [])
    ]
    assert "unrelated" in stop_commands
    assert merged["theme"] == "dark"
    assert not list(config_dir.glob(".settings.json.*.tmp"))
    register_source = _source("register_hook.py")
    assert "os.replace" in register_source
    assert "os.fsync" in register_source


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows ACL semantics")
def test_token_file_acl_is_checked_before_prepare_state(tmp_path: Path) -> None:
    insecure = tmp_path / "insecure.token"
    insecure.write_text("do-not-print-this-secret", encoding="utf-8")
    config_dir = tmp_path / "workbuddy"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text("{}", encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PROJECT_ROOT / "install_windows.ps1"),
            "-ProjectRoot",
            str(PROJECT_ROOT),
            "-ConfigDir",
            str(config_dir),
            "-ProfilePath",
            str(profile),
            "-StudentId",
            "student-a",
            "-GitBashHookCommand",
            "/verified/python /verified/copilot/hook.py",
            "-BaseUrl",
            "https://copilot.example",
            "-TokenFile",
            str(insecure),
            "-StateDir",
            str(tmp_path / "state"),
            "-ValidateSecurityOnly",
        ],
        text=True,
        capture_output=True,
        check=False,
        env=_windows_powershell_51_env(),
    )

    assert completed.returncode != 0
    combined = completed.stdout + completed.stderr
    assert "private ACL" in combined
    assert "do-not-print-this-secret" not in combined
    assert not (tmp_path / "state").exists()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Python/ACL preflight")
def test_python_preflight_fails_before_state_mutation(tmp_path: Path) -> None:
    _protect_private_windows_directory(tmp_path)
    token = tmp_path / "private.token"
    token.write_text("secret", encoding="utf-8")
    _protect_private_windows_directory(token)
    config_dir = tmp_path / "workbuddy"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text("{}", encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    state = tmp_path / "state"

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PROJECT_ROOT / "install_windows.ps1"),
            "-ProjectRoot",
            str(PROJECT_ROOT),
            "-ConfigDir",
            str(config_dir),
            "-ProfilePath",
            str(profile),
            "-StudentId",
            "student-a",
            "-GitBashHookCommand",
            "/verified/python /verified/copilot/hook.py",
            "-BaseUrl",
            "https://copilot.example",
            "-TokenFile",
            str(token),
            "-StateDir",
            str(state),
            "-LogDir",
            str(state / "logs"),
            "-PythonCommand",
            str(tmp_path / "missing-python.exe"),
        ],
        text=True,
        capture_output=True,
        check=False,
        env=_windows_powershell_51_env(),
    )

    assert completed.returncode != 0
    assert not state.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires PowerShell Task/ACL runtime")
def test_uninstall_changed_settings_removes_only_owned_hooks(tmp_path: Path) -> None:
    _protect_private_windows_directory(tmp_path)
    fixture_project_root = tmp_path / "fixture-project"
    fixture_project_root.mkdir()
    uninstall_script = fixture_project_root / "uninstall_windows.ps1"
    shutil.copy2(PROJECT_ROOT / "uninstall_windows.ps1", uninstall_script)

    config_dir = tmp_path / "workbuddy"
    config_dir.mkdir()
    settings_path = config_dir / "settings.json"
    settings = {
        "theme": "user-change",
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": "unrelated"},
                        {
                            "type": "command",
                            "command": f"COPILOT_ENTRY_OWNER={OWNER_ID} owned",
                        },
                    ]
                }
            ]
        },
    }
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    student_id = f"test-{hashlib.sha256(str(tmp_path).encode('utf-8')).hexdigest()[:16]}"
    instance_digest = hashlib.sha256(student_id.encode("utf-8")).hexdigest()[:16]
    install_id = hashlib.sha256(
        f"install:{tmp_path}".encode("utf-8")
    ).hexdigest()[:32]
    baseline = state_dir / f"settings-baseline-{install_id}.json"
    baseline.write_text(json.dumps({"theme": "old"}), encoding="utf-8")
    baseline_sha256 = _sha256(baseline)
    manifest = state_dir / "installer-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner_id": OWNER_ID,
                "install_id": install_id,
                "student_id": student_id,
                "instance_digest": instance_digest,
                "installed_at": "2026-01-01T00:00:00Z",
                "project_root": str(fixture_project_root),
                "config_dir": str(config_dir),
                "workbuddy_profile": str(tmp_path / "profile.json"),
                "settings_path": str(settings_path),
                "settings_before_sha256": baseline_sha256,
                "settings_after_sha256": "0" * 64,
                "baseline_backup_path": str(baseline),
                "baseline_backup_sha256": baseline_sha256,
                "venv_dir": str(fixture_project_root / ".venv-win"),
                "task_name": f"WorkBuddyCopilot-{instance_digest}",
                "runtime_config_path": str(state_dir / "client-config.json"),
                "token_file": str(state_dir / "student.token"),
                "state_dir": str(state_dir),
                "spool_dir": str(state_dir / "spool"),
                "log_dir": str(state_dir / "logs"),
            }
        ),
        encoding="utf-8",
    )
    _protect_private_windows_directory(baseline)
    _protect_private_windows_directory(manifest)

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(uninstall_script),
            "-ManifestPath",
            str(manifest),
        ],
        text=True,
        capture_output=True,
        check=False,
        env=_windows_powershell_51_env(),
    )

    assert completed.returncode == 0, completed.stderr
    remaining = json.loads(settings_path.read_text(encoding="utf-8"))
    assert remaining["theme"] == "user-change"
    commands = [
        hook["command"]
        for block in remaining["hooks"]["Stop"]
        for hook in block.get("hooks", [])
    ]
    assert commands == ["unrelated"]
