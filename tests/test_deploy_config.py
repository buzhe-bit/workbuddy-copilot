from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_start_service_allows_public_host_override_without_editing_script():
    script = (PROJECT_ROOT / "start_service.sh").read_text(encoding="utf-8")

    assert "COPILOT_HOST" in script
    assert "--host \"$COPILOT_HOST\"" in script
    assert "--host 127.0.0.1" not in script


def test_start_service_reads_llm_label_from_effective_config():
    script = (PROJECT_ROOT / "start_service.sh").read_text(encoding="utf-8")

    assert "from copilot.config import load_config" in script
    assert 'open("config.json")' not in script


def _run_start_service(tmp_path: Path, worker_env: dict[str, str]):
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    start_script = deploy_dir / "start_service.sh"
    start_script.write_text(
        (PROJECT_ROOT / "start_service.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    start_script.chmod(0o755)

    activate = deploy_dir / "venv" / "bin" / "activate"
    activate.parent.mkdir(parents=True)
    activate.write_text("# isolated test environment\n", encoding="utf-8")

    fake_bin = deploy_dir / "test-bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/sh\nprintf 'test test-model\\n'\n", encoding="utf-8")
    fake_python.chmod(0o755)
    uvicorn_args = tmp_path / "uvicorn-args.txt"
    fake_uvicorn = fake_bin / "uvicorn"
    fake_uvicorn.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$UVICORN_ARGS_FILE\"\n",
        encoding="utf-8",
    )
    fake_uvicorn.chmod(0o755)

    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["UVICORN_ARGS_FILE"] = str(uvicorn_args)
    for name in ("COPILOT_WORKERS", "UVICORN_WORKERS", "WEB_CONCURRENCY"):
        env.pop(name, None)
    env.update(worker_env)

    completed = subprocess.run(
        ["bash", str(start_script)],
        cwd=deploy_dir,
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    args = uvicorn_args.read_text(encoding="utf-8").splitlines() if uvicorn_args.exists() else []
    return completed, args


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("COPILOT_WORKERS", "2"),
        ("UVICORN_WORKERS", "0"),
        ("WEB_CONCURRENCY", "many"),
        ("COPILOT_WORKERS", "01"),
    ],
)
def test_start_service_rejects_nonempty_worker_env_other_than_exactly_one(
    tmp_path: Path,
    name: str,
    value: str,
):
    completed, args = _run_start_service(tmp_path, {name: value})

    assert completed.returncode != 0
    assert name in completed.stderr
    assert args == []


@pytest.mark.parametrize(
    "worker_env",
    [
        {},
        {"COPILOT_WORKERS": "", "UVICORN_WORKERS": "", "WEB_CONCURRENCY": ""},
        {"COPILOT_WORKERS": "1", "UVICORN_WORKERS": "1", "WEB_CONCURRENCY": "1"},
    ],
)
def test_start_service_launches_exactly_one_worker_for_allowed_env(
    tmp_path: Path,
    worker_env: dict[str, str],
):
    completed, args = _run_start_service(tmp_path, worker_env)

    assert completed.returncode == 0, completed.stderr
    assert args[args.index("--workers") + 1] == "1"


def test_install_registers_local_spool_for_hook():
    script = (PROJECT_ROOT / "install.sh").read_text(encoding="utf-8")

    assert "COPILOT_SPOOL_DIR" in script
    assert "copilot/spool" in script


def test_hook_installation_injects_local_spool_and_short_deadline():
    install = (PROJECT_ROOT / "install.sh").read_text(encoding="utf-8")
    register = (PROJECT_ROOT / "register_hook.py").read_text(encoding="utf-8")

    for script in (install, register):
        assert "COPILOT_SPOOL_DIR" in script
        assert '"timeout": 2' in script


def test_hook_is_standalone_and_does_not_import_core_or_network():
    source = (PROJECT_ROOT / "copilot" / "hook.py").read_text(encoding="utf-8")

    assert "copilot.student_core" not in source
    assert "urllib" not in source
    assert "os.replace" in source


def test_register_upgrades_existing_copilot_hook_in_place(tmp_path: Path):
    settings_path = tmp_path / ".workbuddy" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    old_command = "COPILOT_STUDENT_ID=old python3 /old/copilot/hook.py --network || true"
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": old_command, "timeout": 30}]},
                        {"hooks": [{"type": "command", "command": "echo user-hook", "timeout": 10}]},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["COPILOT_SPOOL_DIR"] = str(tmp_path / "spool")
    env.pop("WORKBUDDY_CONFIG_DIR", None)

    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "register_hook.py")],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    stop_hooks = [hook for block in settings["hooks"]["Stop"] for hook in block["hooks"]]
    copilot_hooks = [hook for hook in stop_hooks if "copilot/hook.py" in hook["command"]]
    assert len(copilot_hooks) == 1
    assert "COPILOT_SPOOL_DIR" in copilot_hooks[0]["command"]
    assert "--network" not in copilot_hooks[0]["command"]
    assert copilot_hooks[0]["timeout"] == 2
    assert any(hook["command"] == "echo user-hook" for hook in stop_hooks)


def test_example_config_documents_public_auth_shape():
    cfg = json.loads((PROJECT_ROOT / "config.example.json").read_text(encoding="utf-8"))

    assert cfg["auth"]["mode"] == "local"
    assert cfg["auth"]["allow_shared_student_token"] is True
    assert "student_token" in cfg["auth"]
    assert cfg["auth"]["student_tokens"] == {}
    assert "mentor_token" in cfg["auth"]
    assert cfg["service"]["public_base_url"] == ""
