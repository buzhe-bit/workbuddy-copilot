from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = PROJECT_ROOT / "deploy"


def test_systemd_unit_uses_external_state_and_one_worker():
    unit = (DEPLOY_DIR / "workbuddy-copilot.service").read_text(encoding="utf-8")

    assert "WorkingDirectory=/srv/workbuddy-copilot/current" in unit
    assert "COPILOT_CONFIG=/etc/workbuddy-copilot/config.json" in unit
    assert "EnvironmentFile=-/etc/workbuddy-copilot/secrets.env" in unit
    assert "COPILOT_WORKERS=1" in unit
    assert "ReadWritePaths=/var/lib/workbuddy-copilot" in unit
    assert "ExecStart=/srv/workbuddy-copilot/current/start_service.sh" in unit


def test_nginx_include_preserves_root_and_proxies_all_copilot_routes():
    nginx = (DEPLOY_DIR / "nginx-server.inc").read_text(encoding="utf-8")

    assert "location / {" not in nginx
    for route in (
        "health",
        "report",
        "recent",
        "sessions",
        "current_session",
        "alerts",
        "api",
        "mentor",
    ):
        assert route in nginx
    assert "client_max_body_size 20m" in nginx
    assert "proxy_http_version 1.1" in nginx
    assert "proxy_set_header Upgrade $http_upgrade" in nginx
    assert 'proxy_set_header Connection "upgrade"' in nginx
    assert "^/ws" in nginx
    ws_location = nginx.split("location ~ ^/ws", 1)[1]
    assert "access_log off;" in ws_location


def _fake_python(tmp_path: Path) -> Path:
    executable = tmp_path / "python3.13"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'Python 3.13.9'; exit 0; fi\n"
        f"exec {sys.executable!s} \"$@\"\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _run_preflight(tmp_path: Path, db_path: Path) -> subprocess.CompletedProcess[str]:
    config_path = tmp_path / "etc" / "config.json"
    config_path.parent.mkdir()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"store": {"db_path": str(db_path)}}),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "COPILOT_CONFIG": str(config_path),
            "PYTHON_BIN": str(_fake_python(tmp_path)),
            "WORKBUDDY_RELEASES_DIR": str(tmp_path / "srv" / "releases"),
        }
    )
    return subprocess.run(
        ["bash", str(DEPLOY_DIR / "preflight.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_preflight_accepts_external_config_and_persistent_db(tmp_path: Path):
    completed = _run_preflight(tmp_path, tmp_path / "var" / "copilot.db")

    assert completed.returncode == 0, completed.stderr


def test_preflight_rejects_database_inside_releases(tmp_path: Path):
    completed = _run_preflight(
        tmp_path,
        tmp_path / "srv" / "releases" / "bad" / "copilot.db",
    )

    assert completed.returncode != 0
    assert "releases" in completed.stderr.lower()


def test_runbook_has_backup_health_gate_and_rollback():
    runbook = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    assert "Connection.backup" in runbook
    assert "curl --fail" in runbook
    assert "PREVIOUS_RELEASE" in runbook


def test_first_deploy_installs_systemd_unit_before_starting_service():
    runbook = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    assert runbook.index("deploy/workbuddy-copilot.service") < runbook.index(
        "sudo systemctl start workbuddy-copilot"
    )


def test_manual_preflight_runs_as_service_account():
    runbook = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")
    preflight_block = runbook.rsplit('"$DEST/deploy/preflight.sh"', 1)[0].rsplit(
        "\n\n", 1
    )[-1]

    assert "sudo -u workbuddy-copilot" in preflight_block


def test_runbook_warns_about_nginx_prefix_shadowing_and_requires_real_llm_smoke():
    runbook = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")

    assert "location ^~ /" in runbook
    assert "model" in runbook
    assert "fallback" in runbook


def test_pilot_runbook_uses_systemd_writable_database_path():
    runbook = (PROJECT_ROOT / "docs" / "pilot-runbook.md").read_text(
        encoding="utf-8"
    )

    assert '"db_path": "/var/lib/workbuddy-copilot/copilot.db"' in runbook
    assert '"db_path": "/srv/workbuddy-copilot/data/copilot.db"' not in runbook
