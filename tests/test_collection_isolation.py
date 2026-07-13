from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_config(path: Path, db_path: Path) -> None:
    config = json.loads((PROJECT_ROOT / "config.example.json").read_text(encoding="utf-8"))
    config["store"]["db_path"] = str(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config), encoding="utf-8")


def test_collection_config_owns_the_global_app_store():
    config_path = Path(os.environ["COPILOT_CONFIG"])
    collection_home = Path(os.environ["HOME"])

    from copilot.service import app

    store_path = Path(app.state.context.store.db_path)
    assert config_path.is_relative_to(collection_home.parent)
    assert store_path.is_relative_to(collection_home.parent)


def test_service_import_ignores_root_config_when_sandbox_config_is_explicit(tmp_path):
    isolated_repo = tmp_path / "repo"
    shutil.copytree(PROJECT_ROOT / "copilot", isolated_repo / "copilot")
    shutil.copy2(PROJECT_ROOT / "config.example.json", isolated_repo / "config.example.json")

    forbidden_db = tmp_path / "forbidden" / "copilot.db"
    sandbox_db = tmp_path / "sandbox" / "copilot.db"
    sandbox_config = tmp_path / "sandbox" / "config.json"
    forbidden_db.parent.mkdir()
    connection = sqlite3.connect(forbidden_db)
    try:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('must remain unchanged')")
        connection.commit()
    finally:
        connection.close()
    forbidden_before = forbidden_db.read_bytes()
    _write_config(isolated_repo / "config.json", forbidden_db)
    _write_config(sandbox_config, sandbox_db)

    environment = os.environ.copy()
    environment.update({
        "COPILOT_CONFIG": str(sandbox_config),
        "HOME": str(tmp_path / "sandbox" / "home"),
        "USERPROFILE": str(tmp_path / "sandbox" / "home"),
        "APPDATA": str(tmp_path / "sandbox" / "home" / "AppData"),
        "PYTHONPATH": str(isolated_repo),
    })
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from copilot.service import app; "
                "print(app.state.context.store.db_path)"
            ),
        ],
        cwd=isolated_repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == str(sandbox_db)
    assert sandbox_db.is_file()
    assert forbidden_db.read_bytes() == forbidden_before
    assert not forbidden_db.with_name(forbidden_db.name + "-wal").exists()
    assert not forbidden_db.with_name(forbidden_db.name + "-shm").exists()
