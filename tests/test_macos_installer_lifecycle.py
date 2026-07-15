from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_HELPER = PROJECT_ROOT / "scripts" / "macos_install_state.py"
OWNER_ID = "workbuddy-copilot-macos-v1"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _run_helper(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(STATE_HELPER), *(str(arg) for arg in args)],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _valid_student_config(token: str = "student-secret-for-test") -> dict[str, object]:
    return {
        "student_id": "student-001",
        "student_name": "Test Student",
        "service": {
            "host": "127.0.0.1",
            "port": 8765,
            "public_base_url": "https://copilot.example.com",
            "analysis_max_concurrency": 2,
        },
        "auth": {
            "mode": "pilot",
            "allow_shared_student_token": False,
            "student_token": token,
            "student_tokens": {},
            "mentor_token": "",
            "token": "",
        },
        "llm": {"api_key": "", "api_key_env": ""},
    }


def _fixture(tmp_path: Path) -> dict[str, Path]:
    project_root = tmp_path / "release"
    (project_root / "copilot").mkdir(parents=True)
    (project_root / "copilot" / "hook.py").write_text("# hook\n", encoding="utf-8")
    config = project_root / "config.json"
    config.write_text(json.dumps(_valid_student_config()), encoding="utf-8")
    config.chmod(0o644)

    workbuddy = tmp_path / "home" / ".workbuddy"
    workbuddy.mkdir(parents=True)
    settings = workbuddy / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "theme": "dark",
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "echo unrelated",
                                    "timeout": 9,
                                }
                            ]
                        }
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {
        "project_root": project_root,
        "config": config,
        "workbuddy": workbuddy,
        "settings": settings,
        "hook_link": workbuddy / "copilot" / "hook.py",
        "spool": workbuddy / "copilot" / "spool",
        "state": tmp_path / "home" / ".workbuddy-copilot",
    }


def _prepare(paths: dict[str, Path]) -> Path:
    completed = _run_helper(
        "prepare",
        "--project-root",
        paths["project_root"],
        "--config",
        paths["config"],
        "--workbuddy-root",
        paths["workbuddy"],
        "--settings",
        paths["settings"],
        "--hook-link",
        paths["hook_link"],
        "--spool-dir",
        paths["spool"],
        "--state-dir",
        paths["state"],
    )
    assert completed.returncode == 0, completed.stderr
    transaction = Path(completed.stdout.strip())
    assert transaction.is_file()
    return transaction


def _installed_settings() -> dict[str, object]:
    return {
        "theme": "dark",
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": "echo unrelated", "timeout": 9},
                        {
                            "type": "command",
                            "command": f"COPILOT_ENTRY_OWNER={OWNER_ID} run-hook",
                            "timeout": 2,
                        },
                    ]
                }
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"COPILOT_ENTRY_OWNER={OWNER_ID} run-hook",
                            "timeout": 2,
                        }
                    ]
                }
            ],
        },
    }


def test_install_rejects_wrong_python_before_creating_a_venv(tmp_path: Path) -> None:
    deploy = tmp_path / "release"
    deploy.mkdir()
    install = deploy / "install.sh"
    install.write_text(
        (PROJECT_ROOT / "install.sh").read_text(encoding="utf-8"), encoding="utf-8"
    )
    install.chmod(0o755)
    (deploy / "config.example.json").write_text("{}\n", encoding="utf-8")
    (deploy / "requirements.txt").write_text("", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = tmp_path / "python3"
    call_log = tmp_path / "calls.log"
    fake_python.write_text(
        """#!/bin/sh
if [ "$1" = "--version" ]; then
  printf 'Python 3.14.4\n'
  exit 0
fi
case "$1" in
  *scripts/python_preflight.py)
    printf 'BLOCKED: Python 3.14.4 does not satisfy >=3.13,<3.14\n' >&2
    exit 1
    ;;
esac
printf '%s\n' "$*" >> "$CALL_LOG"
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  printf 'export PATH="%s:$PATH"\n' "$FAKE_BIN" > "$3/bin/activate"
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_pip = fake_bin / "pip"
    fake_pip.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_pip.chmod(0o755)
    env = os.environ.copy()
    env.update(
        PYTHON=str(fake_python),
        CALL_LOG=str(call_log),
        FAKE_BIN=str(fake_bin),
        HOME=str(tmp_path / "home"),
    )

    completed = subprocess.run(
        ["bash", str(install)],
        cwd=deploy,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert not call_log.exists(), "unsupported Python must be rejected before venv creation"
    assert not (tmp_path / "home" / ".workbuddy").exists()


def test_install_script_is_student_only_and_uses_manifest_lifecycle() -> None:
    source = (PROJECT_ROOT / "install.sh").read_text(encoding="utf-8")
    uninstall = (PROJECT_ROOT / "uninstall_macos.sh").read_text(encoding="utf-8")

    assert "scripts/python_preflight.py" in source
    assert "requirements-macos.txt" in source
    assert "requirements.txt" not in source.replace("requirements-macos.txt", "")
    assert "requirements-server.txt" not in source
    assert "DEEPSEEK_API_KEY" not in source
    assert "start_service.sh" not in source
    assert source.count('chmod 600 "$CONFIG_PATH"') >= 2
    assert source.rindex('chmod 600 "$CONFIG_PATH"') < source.index(' -m venv ')
    assert "COPILOT_ENTRY_OWNER" in source
    assert OWNER_ID in source
    assert '"$STATE_HELPER" prepare' in source
    assert '"$STATE_HELPER" rollback' in source
    assert '"$STATE_HELPER" finalize' in source
    assert '"$STATE_HELPER" uninstall' in uninstall
    assert "ROLLBACK FAILED" in source
    assert "COPILOT_MACOS_STATE_DIR" not in source
    assert "COPILOT_MACOS_STATE_DIR" not in uninstall
    assert "WORKBUDDY_CONFIG_DIR:-" not in source


@pytest.mark.parametrize(
    "mutate",
    [
        lambda cfg: cfg["service"].update(public_base_url="http://public.example.com"),
        lambda cfg: cfg["auth"].update(student_token=""),
        lambda cfg: cfg["auth"].update(mentor_token="must-not-reach-student"),
        lambda cfg: cfg["auth"].update(student_tokens={"other": "must-not-reach-student"}),
        lambda cfg: cfg["llm"].update(api_key="must-not-reach-student"),
    ],
)
def test_prepare_rejects_unsafe_student_config_before_state_mutation(
    tmp_path: Path,
    mutate,
) -> None:
    paths = _fixture(tmp_path)
    config = _valid_student_config()
    mutate(config)
    paths["config"].write_text(json.dumps(config), encoding="utf-8")

    completed = _run_helper(
        "prepare",
        "--project-root",
        paths["project_root"],
        "--config",
        paths["config"],
        "--workbuddy-root",
        paths["workbuddy"],
        "--settings",
        paths["settings"],
        "--hook-link",
        paths["hook_link"],
        "--spool-dir",
        paths["spool"],
        "--state-dir",
        paths["state"],
    )

    assert completed.returncode != 0
    assert "BLOCKED:" in completed.stderr
    assert not paths["state"].exists()
    assert paths["settings"].is_file()


def test_prepare_finalize_and_changed_settings_uninstall_are_manifest_scoped(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    transaction = _prepare(paths)
    transaction_payload = json.loads(transaction.read_text(encoding="utf-8"))
    baseline = Path(transaction_payload["baseline_backup_path"])
    rollback = Path(transaction_payload["rollback_path"])

    assert _mode(paths["config"]) == 0o600
    assert _mode(paths["state"]) == 0o700
    assert _mode(transaction) == _mode(baseline) == _mode(rollback) == 0o600
    assert not (paths["state"] / "installer-manifest.json").exists()

    paths["settings"].write_text(
        json.dumps(_installed_settings(), ensure_ascii=False), encoding="utf-8"
    )
    paths["hook_link"].parent.mkdir(parents=True, exist_ok=True)
    paths["hook_link"].symlink_to(paths["project_root"] / "copilot" / "hook.py")
    finalized = _run_helper(
        "finalize",
        "--state-dir",
        paths["state"],
        "--workbuddy-root",
        paths["workbuddy"],
    )
    assert finalized.returncode == 0, finalized.stderr

    manifest_path = Path(finalized.stdout.strip())
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["owner_id"] == OWNER_ID
    assert _mode(manifest_path) == 0o600
    assert not transaction.exists()
    assert not rollback.exists()
    assert "student-secret-for-test" not in manifest_path.read_text(encoding="utf-8")

    changed = _installed_settings()
    changed["user_change_after_install"] = True
    paths["settings"].write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")

    uninstalled = _run_helper(
        "uninstall",
        "--state-dir",
        paths["state"],
        "--workbuddy-root",
        paths["workbuddy"],
        "--project-root",
        paths["project_root"],
    )
    assert uninstalled.returncode == 0, uninstalled.stderr
    restored = json.loads(paths["settings"].read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for blocks in restored.get("hooks", {}).values()
        for block in blocks
        for hook in block.get("hooks", [])
    ]
    assert "echo unrelated" in commands
    assert not any(OWNER_ID in command for command in commands)
    assert restored["user_change_after_install"] is True
    assert not paths["hook_link"].exists()
    assert baseline.is_file() and _mode(baseline) == 0o600
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["uninstalled_at"]


def test_install_failure_rollback_restores_exact_settings_and_owned_link(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    before = paths["settings"].read_bytes()
    transaction = _prepare(paths)
    transaction_payload = json.loads(transaction.read_text(encoding="utf-8"))
    baseline = Path(transaction_payload["baseline_backup_path"])
    rollback = Path(transaction_payload["rollback_path"])

    paths["settings"].write_text(json.dumps(_installed_settings()), encoding="utf-8")
    paths["hook_link"].parent.mkdir(parents=True, exist_ok=True)
    paths["hook_link"].symlink_to(paths["project_root"] / "copilot" / "hook.py")

    completed = _run_helper(
        "rollback",
        "--state-dir",
        paths["state"],
        "--workbuddy-root",
        paths["workbuddy"],
    )

    assert completed.returncode == 0, completed.stderr
    assert paths["settings"].read_bytes() == before
    assert not paths["hook_link"].exists()
    assert not transaction.exists()
    assert not rollback.exists()
    assert not baseline.exists()


def test_uninstall_rejects_manifest_from_another_project(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    transaction = _prepare(paths)
    paths["settings"].write_text(json.dumps(_installed_settings()), encoding="utf-8")
    paths["hook_link"].parent.mkdir(parents=True, exist_ok=True)
    paths["hook_link"].symlink_to(paths["project_root"] / "copilot" / "hook.py")
    finalized = _run_helper(
        "finalize",
        "--state-dir",
        paths["state"],
        "--workbuddy-root",
        paths["workbuddy"],
    )
    assert finalized.returncode == 0, finalized.stderr
    manifest = Path(finalized.stdout.strip())
    before = paths["settings"].read_bytes()

    completed = _run_helper(
        "uninstall",
        "--state-dir",
        paths["state"],
        "--workbuddy-root",
        paths["workbuddy"],
        "--project-root",
        tmp_path / "different-release",
    )

    assert completed.returncode != 0
    assert paths["settings"].read_bytes() == before


def test_prepare_rejects_state_directory_through_a_symlink_parent(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    real_parent = tmp_path / "real-private-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-private-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    paths["state"] = linked_parent / "state"

    completed = _run_helper(
        "prepare",
        "--project-root",
        paths["project_root"],
        "--config",
        paths["config"],
        "--workbuddy-root",
        paths["workbuddy"],
        "--settings",
        paths["settings"],
        "--hook-link",
        paths["hook_link"],
        "--spool-dir",
        paths["spool"],
        "--state-dir",
        paths["state"],
    )

    assert completed.returncode != 0
    assert "symlink" in completed.stderr.lower()
    assert not (real_parent / "state").exists()


def test_prepare_rejects_workbuddy_hook_directory_symlink(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    external = tmp_path / "external-copilot"
    external.mkdir()
    paths["hook_link"].parent.symlink_to(external, target_is_directory=True)

    completed = _run_helper(
        "prepare",
        "--project-root",
        paths["project_root"],
        "--config",
        paths["config"],
        "--workbuddy-root",
        paths["workbuddy"],
        "--settings",
        paths["settings"],
        "--hook-link",
        paths["hook_link"],
        "--spool-dir",
        paths["spool"],
        "--state-dir",
        paths["state"],
    )

    assert completed.returncode != 0
    assert "symlink" in completed.stderr.lower()
    assert not paths["state"].exists()


def test_prepare_blocks_reinstall_until_the_installed_release_is_uninstalled(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    transaction = _prepare(paths)
    paths["settings"].write_text(json.dumps(_installed_settings()), encoding="utf-8")
    paths["hook_link"].parent.mkdir(parents=True, exist_ok=True)
    paths["hook_link"].symlink_to(paths["project_root"] / "copilot" / "hook.py")
    finalized = _run_helper(
        "finalize",
        "--state-dir",
        paths["state"],
        "--workbuddy-root",
        paths["workbuddy"],
    )
    assert finalized.returncode == 0, finalized.stderr
    before = paths["settings"].read_bytes()

    completed = _run_helper(
        "prepare",
        "--project-root",
        paths["project_root"],
        "--config",
        paths["config"],
        "--workbuddy-root",
        paths["workbuddy"],
        "--settings",
        paths["settings"],
        "--hook-link",
        paths["hook_link"],
        "--spool-dir",
        paths["spool"],
        "--state-dir",
        paths["state"],
    )

    assert completed.returncode != 0
    assert "uninstall_macos.sh" in completed.stderr
    assert paths["settings"].read_bytes() == before
    assert paths["hook_link"].is_symlink()
    assert Path(os.readlink(paths["hook_link"])) == paths["project_root"] / "copilot" / "hook.py"
    assert not transaction.exists()


def test_uninstall_rejects_manifest_paths_outside_trusted_workbuddy_root(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    transaction = _prepare(paths)
    paths["settings"].write_text(json.dumps(_installed_settings()), encoding="utf-8")
    paths["hook_link"].parent.mkdir(parents=True, exist_ok=True)
    paths["hook_link"].symlink_to(paths["project_root"] / "copilot" / "hook.py")
    finalized = _run_helper(
        "finalize",
        "--state-dir",
        paths["state"],
        "--workbuddy-root",
        paths["workbuddy"],
    )
    assert finalized.returncode == 0, finalized.stderr

    victim_root = tmp_path / "victim"
    victim_root.mkdir()
    victim_settings = victim_root / "settings.json"
    victim_settings.write_text('{"must_survive": true}\n', encoding="utf-8")
    manifest_path = Path(finalized.stdout.strip())
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        settings_path=str(victim_settings),
        hook_link_path=str(victim_root / "copilot" / "hook.py"),
        spool_dir=str(victim_root / "copilot" / "spool"),
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)
    before = victim_settings.read_bytes()

    completed = _run_helper(
        "uninstall",
        "--state-dir",
        paths["state"],
        "--workbuddy-root",
        paths["workbuddy"],
        "--project-root",
        paths["project_root"],
    )

    assert completed.returncode != 0
    assert "trusted WorkBuddy root" in completed.stderr
    assert victim_settings.read_bytes() == before
