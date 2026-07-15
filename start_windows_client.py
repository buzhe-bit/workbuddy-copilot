#!/usr/bin/env python3
"""Secure Windows student-client composition root.

The Tk widget implementation lives in :mod:`copilot.floating_windows`.  This
module owns the process contract around it: one protected config file, a named
mutex, a bounded UI bridge, a non-daemon asyncio runtime thread, heartbeat and
rotating-log health, and bounded shutdown.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import Future
from dataclasses import dataclass, field
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from copilot.student_core.transport import Accepted, StudentAskNotFound
from copilot.student_platform.windows_runtime import WindowsStudentRuntime


log = logging.getLogger("copilot.windows_client")
_INSTANCE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


class RuntimeConfigError(RuntimeError):
    """Fail-closed Windows runtime configuration error."""


def require_python_313(version_info: Any | None = None) -> None:
    resolved = tuple(version_info or sys.version_info)
    if resolved[:2] != (3, 13):
        raise RuntimeConfigError("Windows client requires Python 3.13 exactly")


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def _powershell_acl_probe(path: Path) -> None:
    """Ask Windows/.NET for a language-neutral private ACL verdict."""

    if os.name != "nt":
        raise RuntimeConfigError("private Windows ACL validation is unavailable")
    script = r"""
$ErrorActionPreference = 'Stop'
$path = $args[0]
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $identity.User.Value
$acl = Get-Acl -LiteralPath $path
$owner = (New-Object System.Security.Principal.NTAccount($acl.Owner)).Translate(
    [System.Security.Principal.SecurityIdentifier]
).Value
if (-not $acl.AreAccessRulesProtected) { exit 21 }
if ($owner -ne $sid) { exit 22 }
$rules = $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
foreach ($rule in $rules) {
    if ($rule.IsInherited) { exit 23 }
    if ($rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow -and
        $rule.IdentityReference.Value -ne $sid) { exit 24 }
}
exit 0
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
    )
    if completed.returncode != 0:
        raise RuntimeConfigError(
            f"private ACL required for Windows runtime path (code={completed.returncode})"
        )


def assert_private_windows_acl(path: Path, *, directory: bool) -> None:
    candidate = Path(path)
    if candidate.is_symlink():
        raise RuntimeConfigError("private runtime path must not be a symlink")
    if directory and not candidate.is_dir():
        raise RuntimeConfigError("private runtime directory is missing")
    if not directory and not candidate.is_file():
        raise RuntimeConfigError("private runtime file is missing")
    _powershell_acl_probe(candidate)


AclVerifier = Callable[..., None]


@dataclass(frozen=True)
class WindowsClientConfig:
    config_path: Path
    base_url: str
    student_id: str
    state_dir: Path
    spool_dir: Path
    log_dir: Path
    token_file: Path
    workbuddy_config_dir: Path
    workbuddy_profile: Path
    single_instance_name: str
    install_id: str
    interval: float = 1.0
    bridge_timeout: float = 5.0
    heartbeat_interval: float = 2.0
    _acl_verifier: AclVerifier = field(repr=False, compare=False, default=assert_private_windows_acl)

    def read_token(self) -> str:
        # Re-check immediately before the secret read. The installer also
        # checks the source TokenFile before it ever copies the token here.
        self._acl_verifier(self.token_file, directory=False)
        token = self.token_file.read_text(encoding="utf-8").strip()
        if not token or len(token) > 4096:
            raise RuntimeConfigError("student token file is empty or invalid")
        return token


def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = str(payload.get(name) or "").strip()
    if not value:
        raise RuntimeConfigError(f"Windows client config requires {name}")
    return value


def _positive_float(payload: Mapping[str, Any], name: str, default: float) -> float:
    try:
        value = float(payload.get(name, default))
    except (TypeError, ValueError):
        raise RuntimeConfigError(f"Windows client config has invalid {name}") from None
    if value <= 0 or value > 300:
        raise RuntimeConfigError(f"Windows client config has invalid {name}")
    return value


def load_runtime_config(
    path: str | os.PathLike[str],
    *,
    acl_verifier: AclVerifier = assert_private_windows_acl,
) -> WindowsClientConfig:
    config_path = Path(path).expanduser()
    if config_path.is_symlink():
        raise RuntimeConfigError("Windows client config must not be a symlink")
    # Validate the protected directory and file before parsing path or secret
    # locations supplied by the file itself.
    acl_verifier(config_path.parent, directory=True)
    acl_verifier(config_path, directory=False)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeConfigError("Windows client config is unreadable") from exc
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise RuntimeConfigError("Windows client config schema is unsupported")
    if "token" in raw:
        raise RuntimeConfigError("token values must never be stored in client config")
    if "rollout_ready" in raw or "windows_rollout_status" in raw:
        raise RuntimeConfigError("rollout status must come from the evidence validator")

    base_url = _required_text(raw, "base_url").rstrip("/")
    parsed_url = urlsplit(base_url)
    loopback = parsed_url.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        not parsed_url.hostname
        or parsed_url.scheme not in ({"http", "https"} if loopback else {"https"})
    ):
        raise RuntimeConfigError("Windows client base_url must use HTTPS or loopback HTTP")
    if (
        parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
        or parsed_url.path not in {"", "/"}
    ):
        raise RuntimeConfigError("Windows client base_url must not contain credentials/query")

    student_id = _required_text(raw, "student_id")
    state_dir = Path(_required_text(raw, "state_dir")).expanduser()
    spool_dir = Path(_required_text(raw, "spool_dir")).expanduser()
    log_dir = Path(_required_text(raw, "log_dir")).expanduser()
    token_file = Path(_required_text(raw, "token_file")).expanduser()
    workbuddy_config_dir = Path(
        _required_text(raw, "workbuddy_config_dir")
    ).expanduser()
    workbuddy_profile = Path(_required_text(raw, "workbuddy_profile")).expanduser()
    instance_name = _required_text(raw, "single_instance_name")
    if not _INSTANCE_NAME.fullmatch(instance_name):
        raise RuntimeConfigError("invalid Windows single-instance name")
    install_id = _required_text(raw, "install_id")
    if re.fullmatch(r"[0-9a-f]{32}", install_id) is None:
        raise RuntimeConfigError("invalid Windows install_id")
    if not state_dir.is_dir() or state_dir.is_symlink():
        raise RuntimeConfigError("Windows state_dir is missing or unsafe")
    for owned_path in (spool_dir, log_dir, token_file, config_path):
        if not _inside(owned_path, state_dir):
            raise RuntimeConfigError("runtime-owned paths must stay inside state_dir")
    if not workbuddy_config_dir.is_dir() or workbuddy_config_dir.is_symlink():
        raise RuntimeConfigError("WorkBuddy config directory is missing or unsafe")
    if not workbuddy_profile.is_file() or workbuddy_profile.is_symlink():
        raise RuntimeConfigError("WorkBuddy profile is missing or unsafe")

    acl_verifier(state_dir, directory=True)
    acl_verifier(log_dir, directory=True)
    acl_verifier(spool_dir, directory=True)
    acl_verifier(token_file, directory=False)
    return WindowsClientConfig(
        config_path=config_path,
        base_url=base_url,
        student_id=student_id,
        state_dir=state_dir,
        spool_dir=spool_dir,
        log_dir=log_dir,
        token_file=token_file,
        workbuddy_config_dir=workbuddy_config_dir,
        workbuddy_profile=workbuddy_profile,
        single_instance_name=instance_name,
        install_id=install_id,
        interval=_positive_float(raw, "interval", 1.0),
        bridge_timeout=_positive_float(raw, "bridge_timeout", 5.0),
        heartbeat_interval=_positive_float(raw, "heartbeat_interval", 2.0),
        _acl_verifier=acl_verifier,
    )


class _MutexApi(Protocol):
    ERROR_ALREADY_EXISTS: int

    def create(self, name: str) -> int: ...
    def last_error(self) -> int: ...
    def close(self, handle: int) -> None: ...


class _CtypesMutexApi:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeConfigError("Windows named mutex is unavailable")
        import ctypes

        self._ctypes = ctypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateMutexW.restype = ctypes.c_void_p
        self._kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    def create(self, name: str) -> int:
        self._ctypes.set_last_error(0)
        return int(self._kernel32.CreateMutexW(None, False, name) or 0)

    def last_error(self) -> int:
        return int(self._ctypes.get_last_error())

    def close(self, handle: int) -> None:
        self._kernel32.CloseHandle(handle)


class WindowsNamedMutex:
    def __init__(self, name: str, *, api: _MutexApi | None = None) -> None:
        if not _INSTANCE_NAME.fullmatch(str(name or "")):
            raise RuntimeConfigError("invalid Windows single-instance name")
        self.name = f"Local\\{name}"
        self.api = api or _CtypesMutexApi()
        self._handle: int | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        handle = self.api.create(self.name)
        if not handle:
            raise RuntimeConfigError("cannot create Windows single-instance mutex")
        if self.api.last_error() == self.api.ERROR_ALREADY_EXISTS:
            self.api.close(handle)
            raise RuntimeConfigError("Windows student client is already running")
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            self.api.close(handle)

    def __enter__(self) -> "WindowsNamedMutex":
        self.acquire()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()


@dataclass
class _UiCall:
    callback: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    future: Future[Any]


class BoundedUiBridge:
    """Bounded agent-to-main-thread RPC; timeout never counts as rendered."""

    def __init__(self, *, max_pending: int = 64) -> None:
        if int(max_pending) < 1:
            raise ValueError("max_pending must be positive")
        self._owner = threading.get_ident()
        self._queue: queue.Queue[_UiCall] = queue.Queue(maxsize=int(max_pending))
        self._closed = False

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    def call(
        self,
        callback: Callable[..., Any],
        *args: Any,
        timeout: float,
        **kwargs: Any,
    ) -> Any:
        if self._closed:
            raise RuntimeError("UI bridge is closed")
        future: Future[Any] = Future()
        call = _UiCall(callback, tuple(args), dict(kwargs), future)
        try:
            self._queue.put(call, timeout=max(0.001, float(timeout)))
        except queue.Full:
            raise TimeoutError("UI bridge queue is full") from None
        try:
            return future.result(timeout=max(0.001, float(timeout)))
        except TimeoutError:
            future.cancel()
            raise

    def pump(self, *, limit: int = 32) -> int:
        if threading.get_ident() != self._owner:
            raise RuntimeError("UI bridge must be pumped on its owner thread")
        completed = 0
        for _ in range(max(0, int(limit))):
            try:
                call = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                if call.future.set_running_or_notify_cancel():
                    try:
                        result = call.callback(*call.args, **call.kwargs)
                    except BaseException as exc:
                        call.future.set_exception(exc)
                    else:
                        call.future.set_result(result)
                completed += 1
            finally:
                self._queue.task_done()
        return completed

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                call = self._queue.get_nowait()
            except queue.Empty:
                return
            call.future.cancel()
            self._queue.task_done()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_heartbeat(config: WindowsClientConfig, component: str) -> None:
    _atomic_json(
        config.state_dir / f"{component}-heartbeat.json",
        {
            "component": component,
            "student_id": config.student_id,
            "install_id": config.install_id,
            "instance_name": config.single_instance_name,
            "pid": os.getpid(),
            "timestamp": time.time(),
        },
    )


@dataclass(frozen=True)
class ClientHealth:
    healthy: bool
    status: str
    ages: Mapping[str, float]


def check_client_health(
    config: WindowsClientConfig,
    *,
    now: float | None = None,
    max_age: float = 10.0,
) -> ClientHealth:
    current = time.time() if now is None else float(now)
    ages: dict[str, float] = {}
    status = "healthy"
    for component in ("ui", "agent"):
        path = config.state_dir / f"{component}-heartbeat.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("component") != component:
                raise ValueError("component mismatch")
            if payload.get("student_id") != config.student_id:
                ages[component] = float("inf")
                status = "identity_mismatch"
                continue
            if (
                payload.get("install_id") != config.install_id
                or payload.get("instance_name") != config.single_instance_name
            ):
                ages[component] = float("inf")
                status = "generation_mismatch"
                continue
            if int(payload.get("pid", 0)) <= 0:
                ages[component] = float("inf")
                status = "invalid"
                continue
            timestamp = float(payload["timestamp"])
            age = current - timestamp
            if age < -1.0:
                ages[component] = float("inf")
                status = "future"
                continue
            age = max(0.0, age)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            ages[component] = float("inf")
            status = "missing"
            continue
        ages[component] = age
        if age > max(0.0, float(max_age)) and status == "healthy":
            status = "stale"
    return ClientHealth(healthy=status == "healthy", status=status, ages=ages)


def configure_logging(
    config: WindowsClientConfig,
    *,
    logger: logging.Logger = log,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 3,
) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        config.log_dir / "windows-client.log",
        maxBytes=max(1024, int(max_bytes)),
        backupCount=max(1, int(backup_count)),
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return handler


class _View(Protocol):
    def present_mentor_message(self, payload: Mapping[str, Any]) -> Any: ...
    def present_analysis(self, payload: Mapping[str, Any]) -> Any: ...


RuntimeFactory = Callable[..., Any]


class WindowsClientSupervisor:
    def __init__(
        self,
        config: WindowsClientConfig,
        *,
        view: _View,
        runtime_factory: RuntimeFactory = WindowsStudentRuntime.build,
        bridge: BoundedUiBridge | None = None,
    ) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeConfigError("Windows client composition must start on main thread")
        self.config = config
        self.view = view
        self.runtime_factory = runtime_factory
        self.bridge = bridge or BoundedUiBridge()
        self.runtime: Any | None = None
        self.agent_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._stopping = threading.Event()
        self._thread_error: BaseException | None = None
        self._main_task: asyncio.Task[Any] | None = None

    def _message_handler(self, payload: Mapping[str, Any]) -> Any:
        return self.bridge.call(
            self.view.present_mentor_message,
            dict(payload),
            timeout=self.config.bridge_timeout,
        )

    def _analysis_handler(self, payload: Mapping[str, Any]) -> Any:
        return self.bridge.call(
            self.view.present_analysis,
            dict(payload),
            timeout=self.config.bridge_timeout,
        )

    def start(self) -> None:
        if self.agent_thread is not None:
            raise RuntimeError("Windows client supervisor is already started")
        try:
            token = self.config.read_token()
            self.runtime = self.runtime_factory(
                base_url=self.config.base_url,
                student_id=self.config.student_id,
                token=token,
                spool_dir=self.config.spool_dir,
                state_dir=self.config.state_dir,
                workbuddy_config_dir=self.config.workbuddy_config_dir,
                profile_path=self.config.workbuddy_profile,
                interval=self.config.interval,
                message_handler=self._message_handler,
                analysis_handler=self._analysis_handler,
            )
            self.agent_thread = threading.Thread(
                target=self._thread_main,
                name="workbuddy-copilot-agent",
                daemon=False,
            )
            self.agent_thread.start()
            if not self._loop_ready.wait(5.0):
                raise RuntimeConfigError("Windows agent loop did not start")
        except BaseException:
            thread = self.agent_thread
            if thread is not None and thread.is_alive():
                try:
                    self.stop(timeout=1.0)
                except BaseException:
                    pass
            else:
                self.bridge.close()
            raise

    async def _run_runtime(self) -> None:
        assert self.runtime is not None
        # This coroutine begins only after run_until_complete has entered the
        # loop, so start() cannot release an immediate ask into a non-running
        # event loop.
        self._loop_ready.set()

        async def heartbeat() -> None:
            while not self._stopping.is_set():
                try:
                    _write_heartbeat(self.config, "agent")
                except OSError as exc:
                    log.warning("agent heartbeat failed type=%s", type(exc).__name__)
                await asyncio.sleep(self.config.heartbeat_interval)

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            await self.runtime.run()
        finally:
            self._stopping.set()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._main_task = loop.create_task(self._run_runtime())
            loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            if not self._stopping.is_set():
                self._thread_error = RuntimeError("Windows agent loop was cancelled")
        except BaseException as exc:
            self._thread_error = exc
        finally:
            self._main_task = None
            self._loop = None
            loop.close()

    def pump_ui(self, *, limit: int = 32) -> int:
        _write_heartbeat(self.config, "ui")
        if self._thread_error is not None:
            raise RuntimeError("Windows agent loop failed") from self._thread_error
        return self.bridge.pump(limit=limit)

    def _submit(self, coroutine: Any) -> Future[Any]:
        loop = self._loop
        if loop is None or not loop.is_running():
            coroutine.close()
            raise RuntimeError("Windows agent loop is not running")
        return asyncio.run_coroutine_threadsafe(coroutine, loop)

    async def _recover_or_ask(
        self,
        question: str,
        *,
        session_id: str,
        client_request_id: str,
    ) -> dict[str, Any]:
        transport = self.runtime.transport
        try:
            return dict(
                await transport.get_ask_by_client_request_async(client_request_id)
            )
        except StudentAskNotFound:
            accepted = await transport.ask_async(
                question,
                session_id=session_id,
                client_request_id=client_request_id,
            )
            if not isinstance(accepted, Accepted):
                raise RuntimeError("student ask response was not accepted")
            return dict(accepted.body)

    def ask(
        self,
        question: str,
        *,
        session_id: str,
        client_request_id: str,
    ) -> Future[Any]:
        return self._submit(
            self._recover_or_ask(
                question,
                session_id=session_id,
                client_request_id=client_request_id,
            )
        )

    def query_ask(self, client_request_id: str) -> Future[Any]:
        return self._submit(
            self.runtime.transport.get_ask_by_client_request_async(client_request_id)
        )

    def feedback(self, ask_id: int, feedback: str, *, note: str = "") -> Future[Any]:
        return self._submit(
            self.runtime.transport.submit_ask_feedback_async(
                ask_id,
                feedback,
                note=note,
            )
        )

    def stop(self, *, timeout: float = 10.0) -> None:
        self._stopping.set()
        runtime = self.runtime
        loop = self._loop
        deadline = time.monotonic() + max(0.1, float(timeout))
        stop_error: BaseException | None = None
        timed_out = False
        stop_future: Future[Any] | None = None
        try:
            if runtime is not None and loop is not None and loop.is_running():
                remaining = max(0.1, deadline - time.monotonic())
                try:
                    stop_future = asyncio.run_coroutine_threadsafe(runtime.stop(), loop)
                    stop_future.result(timeout=remaining)
                except TimeoutError as exc:
                    timed_out = True
                    stop_error = exc
                    stop_future.cancel()
                    task = self._main_task
                    if task is not None:
                        loop.call_soon_threadsafe(task.cancel)
                except BaseException as exc:
                    stop_error = exc
        finally:
            thread = self.agent_thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(
                    timeout=(
                        0.5
                        if timed_out
                        else max(0.0, deadline - time.monotonic())
                    )
                )
            self.bridge.close()
        if thread is not None and thread.is_alive():
            message = (
                "Windows runtime stop timed out"
                if timed_out
                else "Windows agent did not stop within the bound"
            )
            raise TimeoutError(message) from stop_error
        if timed_out:
            raise TimeoutError("Windows runtime stop timed out") from stop_error
        if stop_error is not None:
            raise stop_error


class _UiHost(Protocol):
    view: _View

    def run(self, supervisor: WindowsClientSupervisor) -> None: ...


def _default_ui_factory(config: WindowsClientConfig) -> _UiHost:
    # UI construction remains in the Windows adapter. This delayed seam keeps
    # config/health commands headless and guarantees Tk is created on main.
    from copilot import floating_windows

    factory = getattr(floating_windows, "create_windows_ui_host", None)
    if not callable(factory):
        raise RuntimeConfigError("Windows Tk UI host is not available")
    return factory(config)


def run_windows_client(
    config: WindowsClientConfig,
    *,
    ui_factory: Callable[[WindowsClientConfig], _UiHost] = _default_ui_factory,
    runtime_factory: RuntimeFactory = WindowsStudentRuntime.build,
    mutex_factory: Callable[[str], WindowsNamedMutex] = WindowsNamedMutex,
) -> None:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeConfigError("Tk must run on the Windows main thread")
    with mutex_factory(config.single_instance_name):
        host = ui_factory(config)
        supervisor = WindowsClientSupervisor(
            config,
            view=host.view,
            runtime_factory=runtime_factory,
        )
        supervisor.start()
        try:
            host.run(supervisor)
        finally:
            supervisor.stop()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the WorkBuddy Windows client")
    parser.add_argument("--config", required=True)
    parser.add_argument("--health-check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        require_python_313()
        args = _parser().parse_args(argv)
        config = load_runtime_config(args.config)
        configure_logging(config)
        if args.health_check:
            health = check_client_health(config)
            print(json.dumps({"status": health.status, "healthy": health.healthy}))
            return 0 if health.healthy else 2
        run_windows_client(config)
    except RuntimeConfigError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
