"""Application composition root for WorkBuddy Copilot.

This is the only place that wires config, persistence, services, EventBus, and
the placeholder WebSocket registry together. Controllers should obtain these
dependencies through FastAPI Depends/app.state rather than importing service
module globals.
"""
from __future__ import annotations

import fcntl
import hmac
import logging
import multiprocessing
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from fastapi import Header, HTTPException, Request, status

from .attention import AttentionService
from .config import load_config
from .connections import WSRegistry
from .eventbus import EventBus
from .llm import analyze as llm_analyze
from .services import AnalysisService, MessageService, SessionQueryService
from .store import Store
from .upload_service import UploadRequestService

log = logging.getLogger("copilot.app_context")


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _uvicorn_cli_worker_settings(argv: Sequence[str]) -> list[tuple[str, int]]:
    if not argv:
        return []
    executable = Path(argv[0])
    if executable.stem != "uvicorn" and not (
        executable.name == "__main__.py" and executable.parent.name == "uvicorn"
    ):
        return []

    settings: list[tuple[str, int]] = []
    for index, argument in enumerate(argv[1:], start=1):
        if argument == "--workers" and index + 1 < len(argv):
            raw = argv[index + 1]
            try:
                settings.append((f"--workers {raw}", int(raw)))
            except ValueError:
                continue
        elif argument.startswith("--workers="):
            raw = argument.partition("=")[2]
            try:
                settings.append((argument, int(raw)))
            except ValueError:
                continue
    return settings


def assert_single_worker() -> None:
    """Reject startup when a multi-worker deployment is requested."""
    for name in ("COPILOT_WORKERS", "UVICORN_WORKERS", "WEB_CONCURRENCY"):
        workers = _env_int(name)
        if workers is not None and workers > 1:
            raise RuntimeError(
                "WorkBuddy Copilot requires a single uvicorn worker; "
                f"{name}={workers} would split in-memory WS/EventBus state."
            )
    if multiprocessing.parent_process() is None:
        for setting, workers in _uvicorn_cli_worker_settings(sys.argv):
            if workers > 1:
                raise RuntimeError(
                    "WorkBuddy Copilot requires a single uvicorn worker; "
                    f"{setting} would split in-memory WS/EventBus state."
                )


def _terminate_uvicorn_multiprocess_supervisor() -> None:
    """Ask only a recognized Uvicorn CLI supervisor to stop its workers."""
    parent = multiprocessing.parent_process()
    if parent is None or parent.pid is None:
        return
    if not any(workers > 1 for _, workers in _uvicorn_cli_worker_settings(sys.argv)):
        return
    try:
        os.kill(parent.pid, signal.SIGTERM)
    except ProcessLookupError:
        log.warning("uvicorn multiprocess supervisor already exited pid=%s", parent.pid)
    else:
        log.critical(
            "single-worker lock collision; terminating uvicorn supervisor pid=%s",
            parent.pid,
        )


@dataclass
class AppContext:
    config: dict[str, Any]
    store: Store
    analysis_svc: AnalysisService
    session_svc: SessionQueryService
    message_svc: MessageService
    bus: EventBus
    ws_registry: WSRegistry
    attention_svc: AttentionService | None = None
    upload_svc: UploadRequestService | None = None
    worker_lock_file: Any | None = None
    report_recovery_prepared: bool = False
    report_recovery_task: Any | None = None


@dataclass(frozen=True)
class StudentPrincipal:
    """Authenticated student identity used by every student REST/WS route."""

    student_id: str
    auth_mode: Literal["mapped", "shared"]


def build_context(config_path: str | os.PathLike[str] | None = None) -> AppContext:
    """Build all runtime dependencies for the FastAPI app."""
    assert_single_worker()
    config = load_config(config_path)
    validate_auth_config(config)
    store = Store(config["store"]["db_path"])
    event_bus = EventBus()
    ws_registry = WSRegistry()
    event_bus.subscribe(ws_registry.handle_event)
    attention_svc = AttentionService(store=store, event_bus=event_bus)
    analysis_svc = AnalysisService(
        copilot_repo=store,
        llm_analyzer=llm_analyze,
        config=config,
        event_bus=event_bus,
        attention_service=attention_svc,
    )
    session_svc = SessionQueryService(copilot_repo=store, config=config)
    message_svc = MessageService(copilot_repo=store, event_bus=event_bus)
    upload_svc = UploadRequestService(store)
    return AppContext(
        config=config,
        store=store,
        analysis_svc=analysis_svc,
        session_svc=session_svc,
        message_svc=message_svc,
        bus=event_bus,
        ws_registry=ws_registry,
        attention_svc=attention_svc,
        upload_svc=upload_svc,
    )


def _worker_lock_path(context: AppContext) -> Path:
    configured_path = context.config.get("store", {}).get("db_path")
    db_path = Path(configured_path or context.store.db_path).expanduser()
    return db_path.parent / ".worker.lock"


def acquire_worker_lock(context: AppContext) -> None:
    """Hold a cross-process lock for the lifetime of the ASGI lifespan."""
    if context.worker_lock_file is not None:
        return
    assert_single_worker()
    lock_path = _worker_lock_path(context)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        _terminate_uvicorn_multiprocess_supervisor()
        raise RuntimeError(
            "WorkBuddy Copilot requires a single uvicorn worker; "
            f"another service process already holds {lock_path}."
        ) from exc
    context.worker_lock_file = lock_file
    log.info("single-worker lock acquired path=%s", lock_path)


def release_worker_lock(context: AppContext) -> None:
    lock_file = context.worker_lock_file
    if lock_file is None:
        return
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()
        context.worker_lock_file = None
        log.info("single-worker lock released")


def get_context(request: Request) -> AppContext:
    return request.app.state.context


def get_store(request: Request) -> Store:
    return get_context(request).store


def get_analysis_service(request: Request) -> AnalysisService:
    return get_context(request).analysis_svc


def get_session_service(request: Request) -> SessionQueryService:
    return get_context(request).session_svc


def get_message_service(request: Request) -> MessageService:
    return get_context(request).message_svc


def get_ws_registry(request: Request) -> WSRegistry:
    return get_context(request).ws_registry


def get_upload_service(request: Request) -> UploadRequestService:
    context = get_context(request)
    if context.upload_svc is None:
        context.upload_svc = UploadRequestService(context.store)
    return context.upload_svc


def ensure_attention_service(context: AppContext) -> AttentionService:
    """Return one canonical projection service and repair split app wiring."""
    attention_svc = context.attention_svc
    if attention_svc is not None and (
        getattr(attention_svc, "store", None) is not context.store
        or getattr(attention_svc, "event_bus", None) is not context.bus
    ):
        log.error(
            "replacing attention service bound to a different store or event bus"
        )
        attention_svc = None
    if attention_svc is None:
        attention_svc = AttentionService(
            store=context.store,
            event_bus=context.bus,
        )
        context.attention_svc = attention_svc
    if context.analysis_svc.attention_service is not attention_svc:
        context.analysis_svc.attention_service = attention_svc
    return attention_svc


def get_attention_service(request: Request) -> AttentionService:
    return ensure_attention_service(get_context(request))


def _legacy_token(config: dict[str, Any]) -> str:
    return (
        os.environ.get("COPILOT_TOKEN")
        or str(config.get("auth", {}).get("token", "") or "")
        or str(config.get("service", {}).get("token", "") or "")
        or str(config.get("token", "") or "")
    )


def _auth_is_public(config: dict[str, Any]) -> bool:
    mode = str(config.get("auth", {}).get("mode", "") or "").lower()
    env_public = str(os.environ.get("COPILOT_PUBLIC", "") or "").lower()
    return mode in {"public", "production", "prod"} or env_public in {"1", "true", "yes"}


def _role_token(config: dict[str, Any], role: str | None = None) -> str:
    auth = config.get("auth", {}) or {}
    if role == "student":
        return (
            os.environ.get("COPILOT_STUDENT_TOKEN")
            or str(auth.get("student_token", "") or "")
            or _legacy_token(config)
        )
    if role == "mentor":
        return (
            os.environ.get("COPILOT_MENTOR_TOKEN")
            or str(auth.get("mentor_token", "") or "")
            or _legacy_token(config)
        )
    return _legacy_token(config)


def student_id_for_token(
    config: dict[str, Any],
    supplied_token: str | None,
) -> str | None:
    """Resolve one configured student token without changing current auth."""
    if not isinstance(supplied_token, str) or not supplied_token:
        return None
    auth = config.get("auth")
    if not isinstance(auth, dict):
        return None
    student_tokens = auth.get("student_tokens")
    if not isinstance(student_tokens, dict):
        return None

    supplied_bytes = supplied_token.encode("utf-8")
    matched_student_id: str | None = None
    for student_id, configured_token in student_tokens.items():
        if (
            not isinstance(student_id, str)
            or not student_id
            or not isinstance(configured_token, str)
            or not configured_token
        ):
            return None
        if hmac.compare_digest(supplied_bytes, configured_token.encode("utf-8")):
            if matched_student_id is not None:
                return None
            matched_student_id = student_id
    return matched_student_id


def _student_token_mapping_is_valid(config: dict[str, Any]) -> bool:
    auth = config.get("auth")
    if not isinstance(auth, dict):
        return True
    student_tokens = auth.get("student_tokens", {})
    if not isinstance(student_tokens, dict):
        return False
    return all(
        isinstance(student_id, str)
        and bool(student_id)
        and isinstance(configured_token, str)
        and bool(configured_token)
        for student_id, configured_token in student_tokens.items()
    )


def _has_student_token_mapping(config: dict[str, Any]) -> bool:
    auth = config.get("auth")
    if not isinstance(auth, dict):
        return False
    student_tokens = auth.get("student_tokens")
    return isinstance(student_tokens, dict) and bool(student_tokens)


def _student_token_mapping_has_duplicates(config: dict[str, Any]) -> bool:
    auth = config.get("auth")
    if not isinstance(auth, dict):
        return False
    student_tokens = auth.get("student_tokens")
    if not isinstance(student_tokens, dict):
        return False
    values = list(student_tokens.values())
    return len(values) != len(set(values))


def shared_student_token_is_allowed(config: dict[str, Any]) -> bool:
    """Return the strict mode-aware shared-token policy.

    Local/demo deployments (plus an empty legacy-local mode) retain the shared
    token behavior unless explicitly disabled. Every other mode rejects it;
    a per-student mapping is the only supported non-local student boundary.
    """
    if _auth_is_public(config):
        return False
    auth = config.get("auth")
    if not isinstance(auth, dict):
        auth = {}
    mode = str(auth.get("mode", "") or "").lower()
    if mode not in {"", "local", "demo"}:
        return False
    configured = auth.get("allow_shared_student_token")
    if configured is not None:
        return configured is True
    return True


def student_principal_for_token(
    config: dict[str, Any],
    supplied_token: str | None,
) -> StudentPrincipal | None:
    """Resolve a student token without allowing a mapped token to downgrade."""
    mapped_student_id = student_id_for_token(config, supplied_token)
    if mapped_student_id is not None:
        return StudentPrincipal(student_id=mapped_student_id, auth_mode="mapped")

    if not _student_token_mapping_is_valid(config):
        return None

    auth = config.get("auth")
    student_tokens = auth.get("student_tokens", {}) if isinstance(auth, dict) else {}
    if supplied_token and isinstance(student_tokens, dict):
        supplied_bytes = supplied_token.encode("utf-8")
        matching_values = sum(
            1
            for configured_token in student_tokens.values()
            if isinstance(configured_token, str)
            and configured_token
            and hmac.compare_digest(
                supplied_bytes,
                configured_token.encode("utf-8"),
            )
        )
        if matching_values:
            # Duplicate mapped tokens are ambiguous and must never become a
            # lower-assurance shared principal.
            return None

    if not shared_student_token_is_allowed(config):
        return None

    expected = _role_token(config, "student")
    if expected:
        if not hmac.compare_digest(supplied_token or "", expected):
            return None
    elif _has_student_token_mapping(config) or _auth_is_public(config):
        # A configured mapping must close the historical tokenless local
        # fallback for unknown callers.
        return None

    default_student_id = str(config.get("student_id", "") or "")
    return StudentPrincipal(student_id=default_student_id, auth_mode="shared")


def resolve_student_id(
    principal: StudentPrincipal,
    supplied_student_id: str | None,
) -> str:
    """Return the only student id a route may use for this request."""
    supplied = str(supplied_student_id or "")
    if supplied.strip():
        if principal.auth_mode == "mapped":
            if supplied != principal.student_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="student_id does not match authenticated student",
                )
            return principal.student_id
        return supplied

    if principal.student_id.strip():
        return principal.student_id
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="student_id is required",
    )


def validate_auth_config(config: dict[str, Any]) -> None:
    """Fail fast for internet-facing mode without role-specific tokens."""
    auth = config.get("auth", {}) or {}
    if not isinstance(auth, dict):
        raise RuntimeError("auth must be an object")
    configured_shared_policy = auth.get("allow_shared_student_token")
    if configured_shared_policy is not None and not isinstance(
        configured_shared_policy,
        bool,
    ):
        raise RuntimeError("auth.allow_shared_student_token must be a boolean")
    if not _student_token_mapping_is_valid(config):
        raise RuntimeError(
            "auth.student_tokens must map non-empty student ids to non-empty tokens"
        )
    if _student_token_mapping_has_duplicates(config):
        raise RuntimeError("auth.student_tokens contains duplicate mapped tokens")
    if not _auth_is_public(config):
        return
    missing: list[str] = []
    if not _has_student_token_mapping(config):
        missing.append("student_tokens")
    if not (os.environ.get("COPILOT_MENTOR_TOKEN") or auth.get("mentor_token")):
        missing.append("mentor_token")
    if missing:
        raise RuntimeError(
            "public auth mode requires an auth.student_tokens mapping and "
            "auth.mentor_token "
            f"(missing: {', '.join(missing)})"
        )


def token_is_valid(config: dict[str, Any], supplied: str | None, role: str | None = None) -> bool:
    if role == "student":
        return student_principal_for_token(config, supplied) is not None
    expected = _role_token(config, role)
    if _auth_is_public(config) and not expected:
        return False
    return (not expected) or hmac.compare_digest(supplied or "", expected)


def _extract_supplied_token(
    authorization: str | None,
    x_copilot_token: str | None,
) -> str:
    supplied = x_copilot_token or ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        supplied = value if scheme.lower() == "bearer" else authorization
    return supplied


async def _require_role_token(
    request: Request,
    authorization: str | None = Header(default=None),
    x_copilot_token: str | None = Header(default=None),
    role: str | None = None,
) -> None:
    """Shared-token auth for /report and /api/*.

    If no token is configured, local development remains open. Once
    COPILOT_TOKEN or config token is set, requests must send either
    Authorization: Bearer <token> or X-Copilot-Token: <token>.
    """
    supplied = _extract_supplied_token(authorization, x_copilot_token)
    if not token_is_valid(get_context(request).config, supplied, role=role):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid copilot token",
        )


async def require_token(
    request: Request,
    authorization: str | None = Header(default=None),
    x_copilot_token: str | None = Header(default=None),
) -> None:
    await _require_role_token(request, authorization, x_copilot_token, role=None)


async def require_student_token(
    request: Request,
    authorization: str | None = Header(default=None),
    x_copilot_token: str | None = Header(default=None),
) -> None:
    await require_student_principal(request, authorization, x_copilot_token)


async def require_student_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_copilot_token: str | None = Header(default=None),
) -> StudentPrincipal:
    supplied = _extract_supplied_token(authorization, x_copilot_token)
    principal = student_principal_for_token(get_context(request).config, supplied)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid copilot token",
        )
    return principal


async def require_mentor_token(
    request: Request,
    authorization: str | None = Header(default=None),
    x_copilot_token: str | None = Header(default=None),
) -> None:
    await _require_role_token(request, authorization, x_copilot_token, role="mentor")
