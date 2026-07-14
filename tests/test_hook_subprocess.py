"""Real subprocess/deadline checks for the stdlib-only WorkBuddy hook."""
from __future__ import annotations

import json
from pathlib import Path
import os
import subprocess
import sys

import pytest


pytestmark = [pytest.mark.windows, pytest.mark.component, pytest.mark.critical]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK = PROJECT_ROOT / "copilot" / "hook.py"


def _run_hook(tmp_path: Path, stdin: str) -> subprocess.CompletedProcess[str]:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"student_id": "student-subprocess"}), encoding="utf-8")
    spool = tmp_path / "spool"
    env = os.environ.copy()
    env.update(
        {
            "COPILOT_CONFIG": str(config),
            "COPILOT_SPOOL_DIR": str(spool),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return subprocess.run(
        [sys.executable, str(HOOK)],
        cwd=PROJECT_ROOT,
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        timeout=2,
        check=False,
    )


def test_bad_stdin_subprocess_exits_under_two_seconds(tmp_path: Path) -> None:
    completed = _run_hook(tmp_path, "not-json")

    assert completed.returncode == 0


def test_stop_subprocess_spools_without_waiting_for_network(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(b"x" * (256 * 1024 + 100))
    event = json.dumps(
        {
            "hook_event_name": "Stop",
            "session_id": "subprocess-session",
            "transcript_path": str(transcript),
        }
    )

    completed = _run_hook(tmp_path, event)

    assert completed.returncode == 0
    entries = sorted((tmp_path / "spool").glob("*.json"))
    assert len(entries) == 1
    payload = json.loads(entries[0].read_text(encoding="utf-8"))["payload"]
    assert len(payload["transcript_tail"].encode("utf-8")) <= 256 * 1024
    assert "transcript_full" not in payload


def test_stop_subprocess_stays_under_two_seconds_with_large_legacy_backlog(
    tmp_path: Path,
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    payload = json.dumps(
        {
            "event_id": "legacy-template",
            "payload": {
                "event": "Stop",
                "student_id": "student-subprocess",
                "transcript_tail": "x" * (256 * 1024),
            },
        },
        separators=(",", ":"),
    )
    # NTFS has a bounded hard-link count per inode. Use eleven source inodes so
    # Windows hosted CI still gets 10k realistic directory entries without any
    # one file approaching that filesystem limit.
    templates = []
    for shard in range(11):
        template = spool / f"legacy-template-{shard}.json"
        template.write_text(payload, encoding="utf-8")
        templates.append(template)
    for index in range(10_001):
        os.link(templates[index // 1000], spool / f"legacy-{index}.json")

    completed = _run_hook(
        tmp_path,
        json.dumps({"hook_event_name": "Stop", "session_id": "after-upgrade"}),
    )

    assert completed.returncode == 0
    assert len(list(spool.glob("*.json"))) == 10_013
