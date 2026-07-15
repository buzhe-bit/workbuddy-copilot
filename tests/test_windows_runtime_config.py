from __future__ import annotations

import asyncio
import json
import logging
import inspect
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from copilot.student_core.transport import (
    Accepted,
    PermanentTransportError,
    StudentAskNotFound,
)
from start_windows_client import (
    BoundedUiBridge,
    RuntimeConfigError,
    WindowsClientSupervisor,
    WindowsNamedMutex,
    _parser,
    check_client_health,
    configure_logging,
    load_runtime_config,
    require_python_313,
)


pytestmark = [pytest.mark.windows, pytest.mark.critical]


def _write_config(tmp_path: Path, *, extra: dict[str, Any] | None = None) -> Path:
    state = tmp_path / "state"
    logs = state / "logs"
    spool = state / "spool"
    state.mkdir(parents=True)
    logs.mkdir()
    spool.mkdir()
    token = state / "student.token"
    token.write_text("secret-token\n", encoding="utf-8")
    profile = tmp_path / "workbuddy-profile.json"
    profile.write_text("{}", encoding="utf-8")
    workbuddy = tmp_path / "workbuddy"
    workbuddy.mkdir()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "base_url": "https://copilot.example",
        "student_id": "student-a",
        "state_dir": str(state),
        "spool_dir": str(spool),
        "log_dir": str(logs),
        "token_file": str(token),
        "workbuddy_config_dir": str(workbuddy),
        "workbuddy_profile": str(profile),
        "single_instance_name": "WorkBuddyCopilot-student-a",
        "install_id": "a" * 32,
    }
    payload.update(extra or {})
    path = state / "client-config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _allow_private(path: Path, *, directory: bool) -> None:
    assert path.is_dir() if directory else path.is_file()


def test_parser_has_no_token_or_runtime_identity_override() -> None:
    parser = _parser()
    parsed = parser.parse_args(["--config", "client.json", "--health-check"])

    assert parsed.config == "client.json"
    assert parsed.health_check is True
    for forbidden in ("--token", "--student-id", "--state-dir", "--base-url"):
        with pytest.raises(SystemExit):
            parser.parse_args(["--config", "client.json", forbidden, "secret"])


def test_runtime_is_pinned_to_python_313() -> None:
    require_python_313((3, 13, 9))

    for unsupported in ((3, 12, 10), (3, 14, 0), (4, 0, 0)):
        with pytest.raises(RuntimeConfigError, match="Python 3.13"):
            require_python_313(unsupported)


def test_config_loads_token_only_after_all_private_acl_checks(tmp_path: Path) -> None:
    path = _write_config(tmp_path)
    checked: list[tuple[Path, bool]] = []

    def verifier(candidate: Path, *, directory: bool) -> None:
        checked.append((candidate, directory))
        _allow_private(candidate, directory=directory)

    config = load_runtime_config(path, acl_verifier=verifier)

    assert config.read_token() == "secret-token"
    assert "secret-token" not in repr(config)
    assert checked == [
        (path.parent, True),
        (path, False),
        (config.state_dir, True),
        (config.log_dir, True),
        (config.spool_dir, True),
        (config.token_file, False),
        (config.token_file, False),
    ]


def test_acl_check_is_fail_closed_before_token_read(tmp_path: Path) -> None:
    path = _write_config(tmp_path)
    token = Path(json.loads(path.read_text())["token_file"])

    def reject(candidate: Path, *, directory: bool) -> None:
        if candidate == token:
            raise RuntimeConfigError("private ACL required")

    with pytest.raises(RuntimeConfigError, match="private ACL"):
        load_runtime_config(path, acl_verifier=reject)


def test_config_cannot_self_declare_rollout_ready(tmp_path: Path) -> None:
    for field in ("rollout_ready", "windows_rollout_status"):
        path = _write_config(tmp_path / field, extra={field: True})
        with pytest.raises(RuntimeConfigError, match="evidence validator"):
            load_runtime_config(path, acl_verifier=_allow_private)


def test_config_rejects_token_or_logs_outside_private_state(tmp_path: Path) -> None:
    for field in ("token_file", "log_dir", "spool_dir"):
        root = tmp_path / field
        root.mkdir()
        outside = root / "outside"
        if field.endswith("_dir"):
            outside.mkdir()
        else:
            outside.write_text("secret", encoding="utf-8")
        path = _write_config(root / "config", extra={field: str(outside)})
        with pytest.raises(RuntimeConfigError, match="inside state_dir"):
            load_runtime_config(path, acl_verifier=_allow_private)


@pytest.mark.parametrize(
    "base_url",
    (
        "https://",
        "https://user:pass@copilot.example",
        "https://copilot.example/prefix",
        "https://copilot.example?debug=1",
        "http://copilot.example",
    ),
)
def test_config_rejects_ambiguous_or_unsafe_base_url(
    tmp_path: Path,
    base_url: str,
) -> None:
    path = _write_config(tmp_path, extra={"base_url": base_url})

    with pytest.raises(RuntimeConfigError, match="base_url"):
        load_runtime_config(path, acl_verifier=_allow_private)


class _FakeMutexApi:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self, *, already_exists: bool = False) -> None:
        self.already_exists = already_exists
        self.closed: list[int] = []

    def create(self, name: str) -> int:
        assert name == "Local\\WorkBuddyCopilot-student-a"
        return 17

    def last_error(self) -> int:
        return self.ERROR_ALREADY_EXISTS if self.already_exists else 0

    def close(self, handle: int) -> None:
        self.closed.append(handle)


def test_named_mutex_rejects_a_second_instance_and_closes_its_handle() -> None:
    first_api = _FakeMutexApi()
    first = WindowsNamedMutex("WorkBuddyCopilot-student-a", api=first_api)
    first.acquire()
    first.release()
    assert first_api.closed == [17]

    second_api = _FakeMutexApi(already_exists=True)
    second = WindowsNamedMutex("WorkBuddyCopilot-student-a", api=second_api)
    with pytest.raises(RuntimeConfigError, match="already running"):
        second.acquire()
    assert second_api.closed == [17]


def test_ui_bridge_is_bounded_times_out_and_executes_only_when_pumped() -> None:
    bridge = BoundedUiBridge(max_pending=1)
    caller_result: list[Any] = []

    def caller() -> None:
        caller_result.append(bridge.call(lambda value: value + 1, 4, timeout=0.5))

    thread = threading.Thread(target=caller)
    thread.start()
    deadline = time.monotonic() + 0.5
    while bridge.pending_count == 0 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert bridge.pending_count == 1
    assert bridge.pump(limit=1) == 1
    thread.join(timeout=1)
    assert caller_result == [5]

    with pytest.raises(TimeoutError):
        bridge.call(lambda: None, timeout=0.01)


class _View:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.analyses: list[dict[str, Any]] = []

    def present_mentor_message(self, payload: dict[str, Any]) -> bool:
        self.messages.append(dict(payload))
        return True

    def present_analysis(self, payload: dict[str, Any]) -> bool:
        self.analyses.append(dict(payload))
        return True


class _Transport:
    student_id = "student-a"

    def __init__(self) -> None:
        self.asks = 0
        self.feedback: list[tuple[int, str]] = []

    async def get_ask_by_client_request_async(self, request_id: str) -> dict[str, Any]:
        if request_id == "known":
            return {"ask_id": 7, "status": "answered", "answer": "restored"}
        if request_id == "forbidden":
            raise PermanentTransportError("forbidden")
        raise StudentAskNotFound("not found")

    async def ask_async(self, question: str, **kwargs: Any) -> Accepted:
        self.asks += 1
        return Accepted(200, {"ask_id": 8, "status": "answered", "answer": question})

    async def submit_ask_feedback_async(
        self, ask_id: int, feedback: str, **_kwargs: Any
    ) -> Accepted:
        self.feedback.append((ask_id, feedback))
        return Accepted(200, {"ask_id": ask_id, "feedback": feedback})


class _Runtime:
    def __init__(self, transport: _Transport) -> None:
        self.transport = transport
        self.started = threading.Event()
        self.stopped = threading.Event()

    async def run(self) -> None:
        self.started.set()
        while not self.stopped.is_set():
            await asyncio.sleep(0.005)

    async def stop(self) -> None:
        self.stopped.set()


class _StopFailingRuntime(_Runtime):
    async def stop(self) -> None:
        self.stopped.set()
        raise RuntimeError("stop failed")


def test_supervisor_composes_handlers_agent_thread_and_bounded_stop(tmp_path: Path) -> None:
    config = load_runtime_config(_write_config(tmp_path), acl_verifier=_allow_private)
    view = _View()
    transport = _Transport()
    captured: dict[str, Any] = {}

    def runtime_factory(**kwargs: Any) -> _Runtime:
        captured.update(kwargs)
        return _Runtime(transport)

    supervisor = WindowsClientSupervisor(
        config,
        view=view,
        runtime_factory=runtime_factory,
        bridge=BoundedUiBridge(max_pending=8),
    )
    supervisor.start()
    assert supervisor.agent_thread is not None
    assert supervisor.agent_thread.daemon is False
    assert supervisor.agent_thread is not threading.current_thread()
    assert supervisor.runtime.started.wait(1)

    message_done = threading.Event()

    def deliver() -> None:
        captured["message_handler"]({"message_id": "m1"})
        captured["analysis_handler"]({"report_id": 2})
        message_done.set()

    delivery = threading.Thread(target=deliver)
    delivery.start()
    deadline = time.monotonic() + 1
    while not message_done.is_set() and time.monotonic() < deadline:
        supervisor.pump_ui()
        time.sleep(0.001)
    delivery.join(timeout=1)
    assert view.messages == [{"message_id": "m1"}]
    assert view.analyses == [{"report_id": 2}]

    assert supervisor.ask("old", session_id="s", client_request_id="known").result(1)[
        "ask_id"
    ] == 7
    assert transport.asks == 0
    assert supervisor.ask("new", session_id="s", client_request_id="new").result(1)[
        "ask_id"
    ] == 8
    assert transport.asks == 1
    with pytest.raises(PermanentTransportError, match="forbidden"):
        supervisor.ask(
            "must-not-retry", session_id="s", client_request_id="forbidden"
        ).result(1)
    assert transport.asks == 1
    supervisor.feedback(8, "helpful").result(1)
    assert transport.feedback == [(8, "helpful")]

    supervisor.stop(timeout=1)
    assert supervisor.agent_thread.is_alive() is False


def test_health_requires_fresh_ui_and_agent_heartbeats(tmp_path: Path) -> None:
    config = load_runtime_config(_write_config(tmp_path), acl_verifier=_allow_private)
    now = 1_720_000_000.0
    for component in ("ui", "agent"):
        (config.state_dir / f"{component}-heartbeat.json").write_text(
            json.dumps(
                {
                    "component": component,
                    "student_id": config.student_id,
                    "install_id": config.install_id,
                    "instance_name": config.single_instance_name,
                    "pid": 1234,
                    "timestamp": now,
                }
            ),
            encoding="utf-8",
        )

    healthy = check_client_health(config, now=now + 5, max_age=10)
    stale = check_client_health(config, now=now + 20, max_age=10)

    assert healthy.healthy is True
    assert healthy.status == "healthy"
    assert stale.healthy is False
    assert stale.status == "stale"


@pytest.mark.parametrize(
    ("field", "value", "expected_status"),
    [
        ("student_id", "other-student", "identity_mismatch"),
        ("install_id", "b" * 32, "generation_mismatch"),
        ("instance_name", "WorkBuddyCopilot-other", "generation_mismatch"),
        ("pid", 0, "invalid"),
    ],
)
def test_health_rejects_wrong_identity_generation_or_pid(
    tmp_path: Path,
    field: str,
    value: Any,
    expected_status: str,
) -> None:
    config = load_runtime_config(_write_config(tmp_path), acl_verifier=_allow_private)
    now = 1_720_000_000.0
    for component in ("ui", "agent"):
        payload = {
            "component": component,
            "student_id": config.student_id,
            "install_id": config.install_id,
            "instance_name": config.single_instance_name,
            "pid": 1234,
            "timestamp": now,
        }
        payload[field] = value
        (config.state_dir / f"{component}-heartbeat.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    health = check_client_health(config, now=now + 1, max_age=10)

    assert health.healthy is False
    assert health.status == expected_status


def test_health_rejects_future_timestamp_instead_of_clamping_it_fresh(
    tmp_path: Path,
) -> None:
    config = load_runtime_config(_write_config(tmp_path), acl_verifier=_allow_private)
    now = 1_720_000_000.0
    for component in ("ui", "agent"):
        (config.state_dir / f"{component}-heartbeat.json").write_text(
            json.dumps(
                {
                    "component": component,
                    "student_id": config.student_id,
                    "install_id": config.install_id,
                    "instance_name": config.single_instance_name,
                    "pid": 1234,
                    "timestamp": now + 3600,
                }
            ),
            encoding="utf-8",
        )

    health = check_client_health(config, now=now, max_age=10)

    assert health.healthy is False
    assert health.status == "future"


def test_stop_failure_still_joins_agent_and_closes_bridge(tmp_path: Path) -> None:
    config = load_runtime_config(_write_config(tmp_path), acl_verifier=_allow_private)
    bridge = BoundedUiBridge(max_pending=2)
    supervisor = WindowsClientSupervisor(
        config,
        view=_View(),
        runtime_factory=lambda **_kwargs: _StopFailingRuntime(_Transport()),
        bridge=bridge,
    )
    supervisor.start()
    assert supervisor.runtime.started.wait(1)

    with pytest.raises(RuntimeError, match="stop failed"):
        supervisor.stop(timeout=1)

    assert supervisor.agent_thread is not None
    assert supervisor.agent_thread.is_alive() is False
    with pytest.raises(RuntimeError, match="closed"):
        bridge.call(lambda: None, timeout=0.01)


def test_loop_ready_is_signalled_only_from_inside_running_coroutine() -> None:
    thread_source = inspect.getsource(WindowsClientSupervisor._thread_main)
    runtime_source = inspect.getsource(WindowsClientSupervisor._run_runtime)

    assert "_loop_ready.set()" not in thread_source
    assert "_loop_ready.set()" in runtime_source


def test_start_failure_closes_bridge_without_leaking_thread(tmp_path: Path) -> None:
    config = load_runtime_config(_write_config(tmp_path), acl_verifier=_allow_private)
    bridge = BoundedUiBridge(max_pending=2)

    def fail_factory(**_kwargs: Any) -> Any:
        raise RuntimeError("composition failed")

    supervisor = WindowsClientSupervisor(
        config,
        view=_View(),
        runtime_factory=fail_factory,
        bridge=bridge,
    )

    with pytest.raises(RuntimeError, match="composition failed"):
        supervisor.start()

    assert supervisor.agent_thread is None
    with pytest.raises(RuntimeError, match="closed"):
        bridge.call(lambda: None, timeout=0.01)


class _HangingStopRuntime(_Runtime):
    async def stop(self) -> None:
        await asyncio.Event().wait()


def test_stop_timeout_cancels_runtime_task_and_does_not_leak_thread(
    tmp_path: Path,
) -> None:
    config = load_runtime_config(_write_config(tmp_path), acl_verifier=_allow_private)
    supervisor = WindowsClientSupervisor(
        config,
        view=_View(),
        runtime_factory=lambda **_kwargs: _HangingStopRuntime(_Transport()),
    )
    supervisor.start()
    assert supervisor.runtime.started.wait(1)

    with pytest.raises(TimeoutError, match="stop timed out"):
        supervisor.stop(timeout=0.2)

    assert supervisor.agent_thread is not None
    assert supervisor.agent_thread.is_alive() is False


def test_runtime_logging_is_rotating_and_never_contains_token(tmp_path: Path) -> None:
    config = load_runtime_config(_write_config(tmp_path), acl_verifier=_allow_private)
    logger = logging.getLogger(f"test.windows.client.{id(tmp_path)}")

    handler = configure_logging(config, logger=logger, max_bytes=1024, backup_count=2)
    logger.info("client started for %s", config.student_id)
    handler.flush()

    assert handler.baseFilename == str(config.log_dir / "windows-client.log")
    assert handler.maxBytes == 1024
    assert handler.backupCount == 2
    assert "secret-token" not in Path(handler.baseFilename).read_text(encoding="utf-8")
    logger.removeHandler(handler)
    handler.close()
