from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess

import pytest

import start_student_agent
from copilot.student_core.models import HookEvent
from copilot.student_core.spool import EventSpool
from copilot.student_core.transport import Accepted


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_macos_spool_only_uses_client_config_and_delivers_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool_dir = tmp_path / "workbuddy" / "copilot" / "spool"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "student_id": "student-a",
                "student": {"spool_dir": str(spool_dir)},
                "service": {
                    "host": "ignored.local",
                    "port": 9999,
                    "public_base_url": "https://copilot.example",
                },
                "auth": {
                    "mode": "pilot",
                    "student_token": "student-secret",
                },
            }
        ),
        encoding="utf-8",
    )
    spool = EventSpool(spool_dir)
    spool.enqueue(
        HookEvent(event="UserPromptSubmit", student_id="student-a", prompt="help"),
        event_id="event-1",
    )
    constructed: dict[str, str] = {}
    ws_opened = 0

    class FakeTransport:
        def __init__(self, base_url: str, *, student_id: str, token: str) -> None:
            constructed.update(
                base_url=base_url,
                student_id=student_id,
                token=token,
            )

        def post_hook(self, _event: HookEvent, *, event_id: str = "") -> Accepted:
            assert event_id == "event-1"
            return Accepted(status_code=202, body={"report_id": 1})

        def open_ws(self):
            nonlocal ws_opened
            ws_opened += 1
            raise AssertionError("spool-only runtime must leave the WebSocket to native UI")

    monkeypatch.setattr(start_student_agent, "StudentTransport", FakeTransport)
    args = start_student_agent._parser().parse_args(
        [
            "--config",
            str(config_path),
            "--spool-only",
            "--interval",
            "0.001",
        ]
    )

    async def exercise() -> None:
        task = asyncio.create_task(start_student_agent._run(args))
        for _ in range(100):
            if not spool.pending():
                break
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert spool.pending() == []
    assert constructed == {
        "base_url": "https://copilot.example",
        "student_id": "student-a",
        "token": "student-secret",
    }
    assert ws_opened == 0


def test_start_menubar_stops_spool_agent_and_keeps_ui_exit_status(
    tmp_path: Path,
) -> None:
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    start_script = deploy_dir / "start_menubar.sh"
    start_script.write_text(
        (PROJECT_ROOT / "start_menubar.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    start_script.chmod(0o755)
    (deploy_dir / "config.json").write_text("{}", encoding="utf-8")
    (deploy_dir / "start_student_agent.py").write_text("", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    activate = deploy_dir / "venv" / "bin" / "activate"
    activate.parent.mkdir(parents=True)
    activate.write_text('export PATH="$FAKE_BIN:$PATH"\n', encoding="utf-8")
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CALL_LOG"
if [[ "$*" == *"start_student_agent.py"* ]]; then
  printf 'agent-start\\n' >> "$LIFECYCLE_LOG"
  trap 'printf "agent-term\\n" >> "$LIFECYCLE_LOG"; exit 0' TERM INT
  while true; do sleep 1; done
fi
if [[ "$*" == "-m copilot.floating_native" ]]; then
  for _ in {1..100}; do
    grep -q agent-start "$LIFECYCLE_LOG" 2>/dev/null && break
    sleep 0.01
  done
  exit 7
fi
exit 99
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    call_log = tmp_path / "calls.log"
    lifecycle_log = tmp_path / "lifecycle.log"
    env = os.environ.copy()
    env.update(
        FAKE_BIN=str(fake_bin),
        CALL_LOG=str(call_log),
        LIFECYCLE_LOG=str(lifecycle_log),
    )

    completed = subprocess.run(
        ["bash", str(start_script)],
        cwd=deploy_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 7
    calls = call_log.read_text(encoding="utf-8")
    assert f"{deploy_dir / 'start_student_agent.py'} --config {deploy_dir / 'config.json'} --spool-only" in calls
    assert "-m copilot.floating_native" in calls
    assert lifecycle_log.read_text(encoding="utf-8").splitlines() == [
        "agent-start",
        "agent-term",
    ]
